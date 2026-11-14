from job_apply_agent import tasks
from job_apply_agent.config import LIVE_SUBMIT_ACK, Settings
from job_apply_agent.domain.enums import ApplicationState, ATSKind, RunStatus
from job_apply_agent.domain.schemas import RawJob
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.tasks import _filter_jobs


def _job(title: str, location: str) -> RawJob:
    return RawJob(
        source=ATSKind.GREENHOUSE,
        source_id=f"{title}-{location}",
        company="Example",
        title=title,
        location=location,
        url="https://example.test/job",
    )


def test_source_filters_run_before_max_jobs_truncation():
    jobs = [
        _job("Account Executive", "Toronto, Ontario"),
        _job("Machine Learning Engineer", "San Francisco, California"),
        _job("Machine Learning Engineer", "Toronto, Ontario"),
    ]
    result = _filter_jobs(jobs, ["machine learning"], ["Ontario", "Toronto"])
    assert [job.location for job in result] == ["Toronto, Ontario"]


def test_empty_source_filters_preserve_all_jobs():
    jobs = [_job("Software Engineer", "Toronto"), _job("Data Scientist", "Ottawa")]
    assert _filter_jobs(jobs, [], []) == jobs


def test_pause_persists_structured_planner_diagnostics(session, monkeypatch):
    job = JobPostingRecord(
        id="planner-diagnostics",
        canonical_key="p" * 64,
        source="ashby",
        source_id="planner-diagnostics",
        sources=[],
        company="Example",
        title="Engineer",
        url="https://example.test/diagnostics",
        location="Toronto",
        normalized={},
    )
    session.add(job)
    session.commit()
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: session)

    result = tasks._pause(
        job.id,
        "computer_use_stopped",
        "planner loop detected",
        extra={
            "reason_code": "planner_repeated_action",
            "planner_trace": [{"step": 1, "action": "fill", "outcome": "executed"}],
        },
    )

    assert result == RunStatus.NEEDS_HUMAN.value
    assert job.blocker["reason_code"] == "planner_repeated_action"
    assert job.blocker["planner_trace"][0]["action"] == "fill"


async def test_live_submit_switch_blocks_before_browser_launch(session, monkeypatch):
    job = JobPostingRecord(
        id="guarded-job",
        canonical_key="g" * 64,
        source="lever",
        source_id="guarded-job",
        sources=[],
        company="Example",
        title="Engineer",
        url="https://example.test/guarded",
        location="Toronto",
        normalized={},
        application_state=ApplicationState.MATERIALS_READY.value,
        resume_sha256="abc",
        resume_pdf_path="/tmp/resume.pdf",
    )
    session.add(job)
    session.commit()
    monkeypatch.setattr(tasks, "settings", Settings(allow_live_submit=False))
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: session)

    result = await tasks._run_application(job.id, "test-key")

    assert result == RunStatus.NEEDS_HUMAN.value
    assert job.application_state == ApplicationState.NEEDS_HUMAN.value
    assert job.blocker["kind"] == "live_submit_disabled"


async def test_enabled_cua_requires_glm_api_key_before_browser_launch(session, monkeypatch):
    job = JobPostingRecord(
        id="no-cua-key",
        canonical_key="k" * 64,
        source="lever",
        source_id="no-cua-key",
        sources=[],
        company="Example",
        title="Engineer",
        url="https://example.test/guarded",
        location="Toronto",
        normalized={},
        application_state=ApplicationState.MATERIALS_READY.value,
        resume_sha256="abc",
        resume_pdf_path="/tmp/resume.pdf",
    )
    session.add(job)
    session.commit()
    monkeypatch.setattr(
        tasks,
        "settings",
        Settings(
            allow_live_submit=True, live_submit_ack=LIVE_SUBMIT_ACK,
            cua_enabled=True,
            glm_api_key=None,
            browser_headless=False,
        ),
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: session)

    result = await tasks._run_application(job.id, "test-key")

    assert result == RunStatus.NEEDS_HUMAN.value
    assert job.blocker["kind"] == "computer_use_not_configured"
