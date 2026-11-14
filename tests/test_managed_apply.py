from datetime import UTC, datetime

import pytest

from job_apply_agent import managed_apply
from job_apply_agent.config import Settings
from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.models import JobPostingRecord


def job(*, state: str = ApplicationState.MATERIALS_READY.value) -> JobPostingRecord:
    return JobPostingRecord(
        id="managed-job",
        canonical_key="m" * 64,
        source="ashby",
        source_id="managed-job",
        sources=[],
        company="Example",
        title="Engineer",
        url="https://jobs.example.test/managed",
        location="Toronto",
        normalized={},
        application_state=state,
        resume_sha256="resume-sha",
        resume_pdf_path="/data/artifacts/resume.pdf",
    )


def configured_settings() -> Settings:
    return Settings(
        allow_live_submit=True,
        cua_enabled=True,
        glm_api_key="secret",
        browser_headless=False,
        browser_channel="chromium",
    )


def test_dispatch_hands_entire_run_to_remote_queue_and_returns(session, monkeypatch):
    record = job()
    session.add(record)
    session.commit()
    queued = []
    monkeypatch.setattr(managed_apply, "get_settings", configured_settings)

    result = managed_apply.dispatch_managed_application(
        record.id,
        session_factory=lambda: session,
        enqueue_run=lambda application_id, key: queued.append((application_id, key)),
    )

    assert result.action == "run"
    assert result.idempotency_key == "managed:managed-job:resume-sha"
    assert queued == [(record.id, result.idempotency_key)]


def test_dispatch_reuses_existing_idempotency_key_for_safe_retry(session, monkeypatch):
    record = job(state=ApplicationState.NEEDS_HUMAN.value)
    record.submission_key = "telegram:stable-key"
    record.blocker = {"kind": "unknown_question", "detail": "old blocker"}
    record.telegram_result_notified_at = datetime.now(UTC)
    session.add(record)
    session.commit()
    queued = []
    monkeypatch.setattr(managed_apply, "get_settings", configured_settings)
    result = managed_apply.dispatch_managed_application(
        record.id,
        session_factory=lambda: session,
        enqueue_run=lambda application_id, key: queued.append((application_id, key)),
    )

    assert result.idempotency_key == "telegram:stable-key"
    assert record.application_state == ApplicationState.MATERIALS_READY.value
    assert record.blocker is None
    assert record.telegram_result_notified_at is None
    assert queued == [(record.id, "telegram:stable-key")]


def test_dispatch_refuses_any_job_whose_submit_was_claimed(session, monkeypatch):
    record = job(state=ApplicationState.NEEDS_HUMAN.value)
    record.submit_started_at = datetime.now(UTC)
    record.blocker = {
        "kind": "submit_outcome_unknown",
        "detail": "confirmation was not observed",
    }
    session.add(record)
    session.commit()
    monkeypatch.setattr(managed_apply, "get_settings", configured_settings)

    with pytest.raises(managed_apply.ManagedApplicationError, match="already claimed"):
        managed_apply.dispatch_managed_application(
            record.id,
            session_factory=lambda: session,
            enqueue_run=lambda *_: pytest.fail("unsafe job must not be queued"),
        )
