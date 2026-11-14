"""Lightweight daily scheduler with Redis idempotency and structured logs."""

from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time

import redis

from .config import get_settings
from .db import get_session_factory
from .domain.schemas import SearchPolicy
from .managed_backlog import dispatch_managed_backlog_if_enabled
from .services.artifacts import purge_expired_resumes
from .tasks import collect_configured_boards

log = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 60


def _next_run_utc(collection_hour_utc: int = 6) -> datetime:
    now = datetime.now(UTC)
    target = now.replace(hour=collection_hour_utc, minute=0, second=0, microsecond=0)
    return target + timedelta(days=1) if target <= now else target


def _dispatch_payloads(settings) -> tuple[list[dict], dict, dict] | None:
    boards = settings.source_boards()
    if not boards:
        log.warning("SOURCE_BOARDS_JSON is empty; nothing to collect")
        return None
    return (
        boards,
        settings.candidate_profile(),
        SearchPolicy.model_validate(settings.search_policy()).model_dump(mode="json"),
    )


def dispatch_if_due(settings, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    scheduled_for = datetime.combine(
        now.date(), datetime_time(hour=settings.collection_hour_utc), tzinfo=UTC
    )
    if now < scheduled_for:
        return False
    payloads = _dispatch_payloads(settings)
    if payloads is None:
        return False
    boards, profile_payload, policy_payload = payloads
    if not any(board.get("enabled", True) for board in boards):
        log.warning("all configured source boards are disabled; nothing to collect")
        return False

    client = redis.Redis.from_url(settings.redis_url)
    cleanup_key = f"job-apply-agent:resume-cleanup:{now.date().isoformat()}"
    if client.set(cleanup_key, "running", nx=True, ex=172800):
        try:
            with get_session_factory()() as session:
                purged = purge_expired_resumes(session, settings.artifacts_dir, now=now)
                session.commit()
            client.set(cleanup_key, "complete", xx=True, ex=172800)
            log.info("resume_cleanup_completed purged=%s", purged)
        except Exception:
            client.delete(cleanup_key)
            raise
    key = f"job-apply-agent:scheduler:{now.date().isoformat()}"
    if not client.set(key, "dispatching", nx=True, ex=172800):
        return False
    try:
        collect_configured_boards.send(
            boards=boards,
            profile_payload=profile_payload,
            policy_payload=policy_payload,
        )
    except Exception:
        client.delete(key)
        raise
    client.set(key, "dispatched", xx=True, ex=172800)
    log.info("collection_dispatched date=%s boards=%s", now.date(), len(boards))
    return True


def run() -> None:
    settings = get_settings()
    stop = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    while not stop:
        try:
            if dispatch_if_due(settings):
                log.info("dispatched today's discovery run")
        except Exception:
            log.exception("failed to dispatch collection")
        try:
            dispatch_managed_backlog_if_enabled(settings)
        except Exception:
            log.exception("failed to supervise managed backlog")
        log.info("next collection at %s", _next_run_utc(settings.collection_hour_utc).isoformat())
        time.sleep(POLL_INTERVAL_SECONDS)
    log.info("scheduler shutting down")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run()
