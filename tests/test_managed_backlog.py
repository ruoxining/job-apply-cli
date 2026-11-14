from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from job_apply_agent.config import Settings
from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.managed_backlog import (
    BASE_RETRY_SECONDS,
    STATE_KEY,
    dispatch_managed_backlog_if_enabled,
    start_managed_backlog,
)
from job_apply_agent.models import JobPostingRecord


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, nx=False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def expire(self, key, seconds):
        del key, seconds


def configured_settings() -> Settings:
    return Settings(allow_live_submit=True, redis_url="redis://unused")


def test_explicit_canary_scope_never_sweeps_other_jobs(session):
    session.add_all([job("canary"), job("other")])
    session.commit()
    client = FakeRedis()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)
    start_managed_backlog(
        settings=configured_settings(),
        redis_client=client,
        application_ids=["canary"],
        session_factory=factory,
    )
    assert json.loads(client.get(STATE_KEY))["target_ids"] == ["canary"]
    queued = []
    dispatch_managed_backlog_if_enabled(
        configured_settings(),
        redis_client=client,
        session_factory=factory,
        dispatcher=queued.append,
    )
    assert queued == ["canary"]


def job(
    job_id: str,
    *,
    source: str = "ashby",
    blocker: dict | None = None,
    claimed: bool = False,
) -> JobPostingRecord:
    return JobPostingRecord(
        id=job_id,
        canonical_key=job_id.ljust(64, "x"),
        source=source,
        source_id=job_id,
        sources=[],
        company="Example",
        title="Engineer",
        url=f"https://jobs.example.test/{job_id}",
        location="Remote",
        normalized={},
        application_state=ApplicationState.NEEDS_HUMAN.value,
        blocker=blocker or {"kind": "computer_use_stopped"},
        resume_sha256=f"sha-{job_id}",
        resume_pdf_path=f"/data/artifacts/{job_id}.pdf",
        submit_started_at=datetime.now(UTC) if claimed else None,
    )


def test_campaign_snapshots_only_safe_retry_targets(session):
    safe = job("safe")
    claimed = job(
        "claimed",
        blocker={"kind": "submit_outcome_unknown"},
        claimed=True,
    )
    unsupported = job("unsupported", source="unknown", blocker={"kind": "unsupported_ats"})
    permanent = job("permanent", blocker={"kind": "captcha"})
    session.add_all([safe, claimed, unsupported, permanent])
    session.commit()

    client = FakeRedis()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)
    queued: list[str] = []
    start_managed_backlog(settings=configured_settings(), redis_client=client)

    result = dispatch_managed_backlog_if_enabled(
        configured_settings(),
        redis_client=client,
        session_factory=factory,
        dispatcher=lambda application_id: queued.append(application_id),
    )

    state = json.loads(client.values[STATE_KEY])
    assert queued == [safe.id]
    assert result.dispatched == 1
    assert state["target_ids"] == [safe.id]
    assert state["initial_dispatch_complete"] is True
    assert state["excluded"] == {
        "non_retryable_blocker": 1,
        "submit_already_claimed": 1,
        "unsupported_ats": 1,
    }

    later = job("later")
    session.add(later)
    session.commit()
    dispatch_managed_backlog_if_enabled(
        configured_settings(),
        redis_client=client,
        session_factory=factory,
        dispatcher=lambda application_id: queued.append(application_id),
    )
    assert queued == [safe.id]


def test_transient_quota_failure_retries_after_cloud_backoff(session):
    retryable = job(
        "retryable",
        blocker={
            "kind": "transient_model_error",
            "retryable": True,
            "retry_after_seconds": 30,
        },
    )
    session.add(retryable)
    session.commit()
    client = FakeRedis()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)
    now = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    queued: list[str] = []
    start_managed_backlog(settings=configured_settings(), redis_client=client)
    dispatch_managed_backlog_if_enabled(
        configured_settings(),
        now=now,
        redis_client=client,
        session_factory=factory,
        dispatcher=lambda application_id: queued.append(application_id),
    )
    assert queued == [retryable.id]

    # Simulate the cloud worker pausing the same application on a GLM quota error.
    with factory() as db_session:
        row = db_session.get(JobPostingRecord, retryable.id)
        row.application_state = ApplicationState.NEEDS_HUMAN.value
        row.blocker = {
            "kind": "transient_model_error",
            "retryable": True,
            "retry_after_seconds": 30,
        }
        row.updated_at = now + timedelta(seconds=1)
        db_session.commit()

    first_retry_tick = dispatch_managed_backlog_if_enabled(
        configured_settings(),
        now=now + timedelta(seconds=2),
        redis_client=client,
        session_factory=factory,
        dispatcher=lambda application_id: queued.append(application_id),
    )
    assert first_retry_tick.retry_waiting == 1
    assert queued == [retryable.id]

    dispatch_managed_backlog_if_enabled(
        configured_settings(),
        now=now + timedelta(seconds=BASE_RETRY_SECONDS + 3),
        redis_client=client,
        session_factory=factory,
        dispatcher=lambda application_id: queued.append(application_id),
    )
    assert queued == [retryable.id, retryable.id]
