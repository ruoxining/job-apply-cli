from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import redis
from sqlalchemy import select

from job_apply_agent.config import Settings, get_settings
from job_apply_agent.db import get_session_factory
from job_apply_agent.domain.enums import ApplicationState, ATSKind, BlockerKind
from job_apply_agent.managed_apply import (
    ManagedApplicationError,
    dispatch_managed_application,
)
from job_apply_agent.models import JobPostingRecord

log = logging.getLogger(__name__)

STATE_KEY = "job-apply-agent:managed-backlog"
LOCK_KEY = f"{STATE_KEY}:lock"
RETRY_HASH_PREFIX = f"{STATE_KEY}:retry"
LOCK_SECONDS = 300
RETRY_STATE_TTL_SECONDS = 60 * 60 * 24 * 30
BASE_RETRY_SECONDS = 15 * 60
MAX_RETRY_SECONDS = 6 * 60 * 60

SUPPORTED_SOURCES = {
    ATSKind.GREENHOUSE.value,
    ATSKind.LEVER.value,
    ATSKind.ASHBY.value,
}
INITIAL_RETRYABLE_BLOCKERS = {
    "browser_error",
    "computer_use_not_configured",
    "computer_use_stopped",
    "narrative_generation_failed",
    "transient_model_error",
}
ACTIVE_STATES = {
    ApplicationState.MATERIALS_READY.value,
    ApplicationState.FILLING.value,
    ApplicationState.SUBMITTING.value,
}


@dataclass(frozen=True)
class BacklogStartResult:
    campaign_id: str
    status: str
    already_active: bool


@dataclass(frozen=True)
class BacklogTickResult:
    campaign_id: str | None
    status: str
    dispatched: int = 0
    active: int = 0
    retry_waiting: int = 0


def start_managed_backlog(
    *,
    settings: Settings | None = None,
    redis_client: Any | None = None,
    application_ids: list[str] | None = None,
    session_factory=None,
) -> BacklogStartResult:
    settings = settings or get_settings()
    live_error = settings.live_submission_error()
    if live_error:
        raise ManagedApplicationError(live_error)
    client = redis_client or redis.Redis.from_url(settings.redis_url)
    existing = _read_state(client)
    if existing and existing.get("status") == "active":
        if application_ids is not None and set(existing.get("target_ids") or []) != set(
            application_ids
        ):
            raise ManagedApplicationError("a different managed campaign is already active")
        return BacklogStartResult(
            campaign_id=str(existing["campaign_id"]),
            status="active",
            already_active=True,
        )

    now = datetime.now(UTC).isoformat()
    state = {
        "campaign_id": str(uuid4()),
        "status": "active",
        "started_at": now,
        "updated_at": now,
        "initial_dispatch_complete": False,
        "target_ids": None,
        "initial_pending_ids": [],
        "initial_total": 0,
        "initial_eligible": 0,
        "initial_dispatched": 0,
        "retry_dispatched": 0,
        "excluded": {},
        "dispatch_errors": 0,
        "active": 0,
        "retry_waiting": 0,
        "last_error": None,
    }
    if application_ids is not None:
        if not application_ids:
            raise ManagedApplicationError("explicit campaign scope must not be empty")
        factory = session_factory or get_session_factory()
        with factory() as session:
            rows = list(
                session.scalars(
                    select(JobPostingRecord).where(
                        JobPostingRecord.id.in_(application_ids),
                        JobPostingRecord.application_state == ApplicationState.NEEDS_HUMAN.value,
                    )
                )
            )
        candidates, excluded = _initial_candidates(rows)
        if excluded or {row.id for row in candidates} != set(application_ids):
            raise ManagedApplicationError(
                "explicit campaign includes missing or unsafe applications"
            )
        # Persist the exact single-canary scope up front. Existing schedulers
        # understand target_ids and cannot accidentally expand this to the backlog.
        state.update(
            initial_total=len(candidates),
            initial_eligible=len(candidates),
            target_ids=[row.id for row in candidates],
            initial_pending_ids=[row.id for row in candidates],
        )
    _write_state(client, state)
    return BacklogStartResult(
        campaign_id=str(state["campaign_id"]),
        status="active",
        already_active=False,
    )


def stop_managed_backlog(
    *,
    settings: Settings | None = None,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    client = redis_client or redis.Redis.from_url(settings.redis_url)
    state = _read_state(client) or {"campaign_id": None}
    state["status"] = "stopped"
    state["updated_at"] = datetime.now(UTC).isoformat()
    _write_state(client, state)
    return state


def get_managed_backlog_status(
    *,
    settings: Settings | None = None,
    redis_client: Any | None = None,
    session_factory=None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    client = redis_client or redis.Redis.from_url(settings.redis_url)
    state = _read_state(client) or {"status": "not_started", "campaign_id": None}
    factory = session_factory or get_session_factory()
    with factory() as session:
        rows = list(session.scalars(select(JobPostingRecord)))
    state_counts = Counter(row.application_state for row in rows)
    state["database"] = {
        "states": dict(state_counts),
        "active": sum(row.application_state in ACTIVE_STATES for row in rows),
        "needs_human": state_counts[ApplicationState.NEEDS_HUMAN.value],
        "submitted": state_counts[ApplicationState.SUBMITTED.value],
    }
    return state


def dispatch_managed_backlog_if_enabled(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    redis_client: Any | None = None,
    session_factory=None,
    dispatcher: Callable[[str], object] | None = None,
) -> BacklogTickResult:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    client = redis_client or redis.Redis.from_url(settings.redis_url)
    state = _read_state(client)
    if not state or state.get("status") != "active":
        return BacklogTickResult(
            campaign_id=str(state.get("campaign_id")) if state else None,
            status=str(state.get("status", "not_started")) if state else "not_started",
        )

    token = f"{state['campaign_id']}:{uuid4()}"
    if not client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
        return BacklogTickResult(
            campaign_id=str(state["campaign_id"]),
            status="locked",
            active=int(state.get("active", 0)),
            retry_waiting=int(state.get("retry_waiting", 0)),
        )

    factory = session_factory or get_session_factory()
    dispatch_one = dispatcher or (
        lambda application_id: dispatch_managed_application(application_id)
    )
    dispatched = 0
    try:
        if state.get("target_ids") is None:
            with factory() as session:
                rows = list(
                    session.scalars(
                        select(JobPostingRecord).where(
                            JobPostingRecord.application_state == ApplicationState.NEEDS_HUMAN.value
                        )
                    )
                )
            candidates, excluded = _initial_candidates(rows)
            state["initial_total"] = len(rows)
            state["initial_eligible"] = len(candidates)
            state["excluded"] = excluded
            state["target_ids"] = [job.id for job in candidates]
            state["initial_pending_ids"] = list(state["target_ids"])
            # Persist the exact campaign membership before enqueueing anything.
            # Future needs_human rows must never be swept into this one-off retry.
            _write_state(client, state)

        if not state.get("initial_dispatch_complete"):
            errors = 0
            pending_ids = list(state.get("initial_pending_ids", []))
            for application_id in pending_ids:
                try:
                    dispatch_one(application_id)
                    dispatched += 1
                    state["initial_pending_ids"].remove(application_id)
                    state["initial_dispatched"] = int(state.get("initial_dispatched", 0)) + 1
                    # Make successful handoffs durable one by one. A scheduler
                    # restart then resumes only entries that were not handed off.
                    _write_state(client, state)
                except ManagedApplicationError as exc:
                    errors += 1
                    state["last_error"] = str(exc)
                    log.warning(
                        "managed_backlog_dispatch_refused application_id=%s error=%s",
                        application_id,
                        exc,
                    )
                except Exception as exc:
                    errors += 1
                    state["last_error"] = type(exc).__name__
                    log.exception(
                        "managed_backlog_dispatch_failed application_id=%s", application_id
                    )
            state["dispatch_errors"] = int(state.get("dispatch_errors", 0)) + errors
            state["initial_dispatch_complete"] = not state["initial_pending_ids"]
        else:
            rows = _campaign_rows(factory, state.get("target_ids", []))
            for job in _transient_candidates(rows):
                due, _ = _transient_retry_due(client, state["campaign_id"], job, now)
                if not due:
                    continue
                try:
                    dispatch_one(job.id)
                    dispatched += 1
                    state["retry_dispatched"] = int(state.get("retry_dispatched", 0)) + 1
                except ManagedApplicationError as exc:
                    state["dispatch_errors"] = int(state.get("dispatch_errors", 0)) + 1
                    state["last_error"] = str(exc)
                    log.warning(
                        "managed_backlog_retry_refused application_id=%s error=%s",
                        job.id,
                        exc,
                    )

                except Exception as exc:
                    state["dispatch_errors"] = int(state.get("dispatch_errors", 0)) + 1
                    state["last_error"] = type(exc).__name__
                    log.exception("managed_backlog_retry_failed application_id=%s", job.id)

        rows = _campaign_rows(factory, state.get("target_ids", []))
        active = sum(row.application_state in ACTIVE_STATES for row in rows)
        transient = _transient_candidates(rows)
        state["active"] = active
        state["retry_waiting"] = len(transient)
        if state.get("initial_dispatch_complete") and active == 0 and not transient:
            state["status"] = "complete"
        state["updated_at"] = now.isoformat()
        _write_state(client, state)
        log.info(
            "managed_backlog_tick campaign_id=%s dispatched=%s active=%s retry_waiting=%s "
            "status=%s",
            state["campaign_id"],
            dispatched,
            active,
            len(transient),
            state["status"],
        )
        return BacklogTickResult(
            campaign_id=str(state["campaign_id"]),
            status=str(state["status"]),
            dispatched=dispatched,
            active=active,
            retry_waiting=len(transient),
        )
    finally:
        if _decode(client.get(LOCK_KEY)) == token:
            client.delete(LOCK_KEY)


def _initial_candidates(
    rows: list[JobPostingRecord],
) -> tuple[list[JobPostingRecord], dict[str, int]]:
    candidates: list[JobPostingRecord] = []
    excluded: Counter[str] = Counter()
    for job in rows:
        kind = str((job.blocker or {}).get("kind") or "")
        if job.submit_started_at or job.submit_completed_at:
            excluded["submit_already_claimed"] += 1
        elif job.source not in SUPPORTED_SOURCES:
            excluded["unsupported_ats"] += 1
        elif not job.resume_sha256 or not (job.resume_pdf_path or job.resume_docx_path):
            excluded["resume_not_locked"] += 1
        elif kind == BlockerKind.SUBMIT_OUTCOME_UNKNOWN.value:
            excluded["submit_outcome_unknown"] += 1
        elif kind not in INITIAL_RETRYABLE_BLOCKERS:
            excluded["non_retryable_blocker"] += 1
        else:
            candidates.append(job)
    candidates.sort(key=lambda job: (job.score, job.updated_at), reverse=True)
    return candidates, dict(excluded)


def _transient_candidates(rows: list[JobPostingRecord]) -> list[JobPostingRecord]:
    return [
        job
        for job in rows
        if not job.submit_started_at
        and not job.submit_completed_at
        and job.source in SUPPORTED_SOURCES
        and job.resume_sha256
        and (job.resume_pdf_path or job.resume_docx_path)
        and (job.blocker or {}).get("kind") == "transient_model_error"
        and bool((job.blocker or {}).get("retryable"))
    ]


def _transient_retry_due(
    client: Any,
    campaign_id: str,
    job: JobPostingRecord,
    now: datetime,
) -> tuple[bool, datetime]:
    key = f"{RETRY_HASH_PREFIX}:{campaign_id}"
    updated_at = job.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    else:
        updated_at = updated_at.astimezone(UTC)
    snapshot = updated_at.isoformat()
    previous_raw = client.hget(key, job.id)
    previous = json.loads(_decode(previous_raw)) if previous_raw else {}
    if previous.get("snapshot") != snapshot:
        attempt = int(previous.get("attempt", 0)) + 1
        provider_delay = int((job.blocker or {}).get("retry_after_seconds") or 0)
        delay = min(BASE_RETRY_SECONDS * (2 ** (attempt - 1)), MAX_RETRY_SECONDS)
        retry_at = now + timedelta(seconds=max(delay, provider_delay))
        client.hset(
            key,
            job.id,
            json.dumps(
                {
                    "snapshot": snapshot,
                    "attempt": attempt,
                    "retry_at": retry_at.isoformat(),
                },
                sort_keys=True,
            ),
        )
        client.expire(key, RETRY_STATE_TTL_SECONDS)
        return False, retry_at
    retry_at = datetime.fromisoformat(previous["retry_at"])
    return now >= retry_at, retry_at


def _read_state(client: Any) -> dict[str, Any] | None:
    raw = client.get(STATE_KEY)
    return json.loads(_decode(raw)) if raw else None


def _write_state(client: Any, state: dict[str, Any]) -> None:
    client.set(STATE_KEY, json.dumps(state, sort_keys=True))


def _decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _campaign_rows(factory: Any, target_ids: list[str]) -> list[JobPostingRecord]:
    if not target_ids:
        return []
    with factory() as session:
        return list(
            session.scalars(select(JobPostingRecord).where(JobPostingRecord.id.in_(target_ids)))
        )
