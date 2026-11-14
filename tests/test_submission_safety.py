import pytest

from job_apply_agent import managed_apply, tasks
from job_apply_agent.ats.playwright_adapter import AshbyAdapter, GreenhouseAdapter, LeverAdapter
from job_apply_agent.config import LIVE_SUBMIT_ACK, Settings
from job_apply_agent.domain.enums import ApplicationState, RunStatus
from job_apply_agent.managed_backlog import start_managed_backlog
from job_apply_agent.models import JobPostingRecord


@pytest.mark.parametrize("adapter_cls", [AshbyAdapter, GreenhouseAdapter, LeverAdapter])
@pytest.mark.parametrize("ack", ["", "yes", LIVE_SUBMIT_ACK.lower()])
async def test_final_submit_requires_ack_before_any_browser_or_claim_access(adapter_cls, ack):
    adapter = adapter_cls(Settings(_env_file=None, allow_live_submit=True, live_submit_ack=ack))
    # Bare objects cannot interact with a page or acquire a submission claim.
    with pytest.raises(PermissionError, match="LIVE_SUBMIT_ACK"):
        await adapter.submit(object(), object())


async def test_worker_without_ack_stops_before_browser_launch(session, monkeypatch):
    job = JobPostingRecord(
        id="unacknowledged", canonical_key="u" * 64, source="lever",
        source_id="unacknowledged", sources=[], company="Example", title="Engineer",
        url="https://example.test/job", location="Toronto", normalized={},
        application_state=ApplicationState.MATERIALS_READY.value,
        resume_sha256="fake-hash", resume_pdf_path="/tmp/example-resume.pdf",
    )
    session.add(job)
    session.commit()
    monkeypatch.setattr(tasks, "settings", Settings(
        _env_file=None, allow_live_submit=True, live_submit_ack=""
    ))
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: session)
    result = await tasks._run_application(job.id, "test-key")
    assert result == RunStatus.NEEDS_HUMAN.value
    assert job.blocker["kind"] == "live_submit_unacknowledged"
    assert job.submit_started_at is None


def test_managed_dispatch_without_ack_never_opens_database(monkeypatch):
    settings = Settings(_env_file=None, allow_live_submit=True, live_submit_ack="")
    monkeypatch.setattr(managed_apply, "get_settings", lambda: settings)

    def forbidden():
        pytest.fail("unacknowledged dispatch accessed the database")

    with pytest.raises(managed_apply.ManagedApplicationError, match="LIVE_SUBMIT_ACK"):
        managed_apply.dispatch_managed_application("example", session_factory=forbidden)
    with pytest.raises(managed_apply.ManagedApplicationError, match="LIVE_SUBMIT_ACK"):
        start_managed_backlog(settings=settings, redis_client=object())


def test_ack_alone_cannot_enable_live_submission():
    settings = Settings(_env_file=None, allow_live_submit=False, live_submit_ack=LIVE_SUBMIT_ACK)
    assert "ALLOW_LIVE_SUBMIT=false" in settings.live_submission_error()
