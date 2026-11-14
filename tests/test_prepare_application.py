from pathlib import Path

from sqlalchemy.orm import sessionmaker

from job_apply_agent import tasks
from job_apply_agent.domain.enums import ApplicationState, ATSKind
from job_apply_agent.domain.schemas import JobPosting, ProfileFact
from job_apply_agent.models import JobPostingRecord


def test_prepare_application_creates_resume_and_enqueues_auto_job(
    session, tmp_path: Path, monkeypatch
):
    job = JobPosting(
        source=ATSKind.LEVER,
        source_id="job-prepare",
        company="Small Example",
        title="Software Engineer",
        url="https://example.test/job-prepare",
        location="Toronto",
        description="Build Python services.",
    )
    session.add(
        JobPostingRecord(
            id="prepare-job",
            canonical_key="p" * 64,
            source=ATSKind.LEVER.value,
            source_id="job-prepare",
            sources=[],
            company=job.company,
            title=job.title,
            url=job.url,
            location=job.location,
            normalized=job.model_dump(mode="json"),
            eligible=True,
            application_state=ApplicationState.AUTO_APPLY_QUEUED.value,
            application_channel="auto",
        )
    )
    session.commit()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)
    queued = []
    monkeypatch.setattr(tasks, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tasks.settings, "artifacts_dir", tmp_path)
    monkeypatch.setattr(tasks.run_application, "send", lambda *args: queued.append(args))
    profile = {
        "facts": [
            ProfileFact(
                id="python-fact",
                statement="Built reproducible Python services.",
                skills=["Python"],
                source="resume-metadata",
            ).model_dump(mode="json")
        ]
    }

    state = tasks.prepare_application.fn("prepare-job", profile)

    with factory() as check:
        record = check.get(JobPostingRecord, "prepare-job")
        assert record.application_state == ApplicationState.MATERIALS_READY.value
        assert record.resume_sha256
        assert Path(record.resume_docx_path).is_file()
        assert record.resume_fact_ids == ["python-fact"]
    assert state == ApplicationState.MATERIALS_READY.value
    assert queued[0][0] == "prepare-job"
    assert queued[0][1].startswith("auto:prepare-job:")
