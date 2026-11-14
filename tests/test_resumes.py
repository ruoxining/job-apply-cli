from pathlib import Path

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import JobPosting, ProfileFact
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.resumes import ResumeVariantService


def test_resume_variant_is_persisted_with_fact_trace(session, tmp_path: Path):
    service = ResumeVariantService(tmp_path)
    job = JobPosting(
        source=ATSKind.LEVER,
        source_id="job-1",
        company="Acme",
        title="Backend Engineer",
        url="https://example.test/job-1",
        description="Python PostgreSQL backend systems",
    )
    fact = ProfileFact(
        id="fact-1",
        statement="Reduced API latency by 35% using PostgreSQL indexes.",
        skills=["PostgreSQL"],
        metrics=["35%"],
        source="resume-metadata",
    )
    session.add(
        JobPostingRecord(
            id="posting-1",
            canonical_key="1" * 64,
            source="lever",
            source_id="job-1",
            sources=[],
            company="Acme",
            title="Backend Engineer",
            url="https://example.test/job-1",
            location="",
            normalized={},
        )
    )
    session.flush()
    record = service.create(session, job_id="posting-1", job=job, facts=[fact])

    assert session.query(JobPostingRecord).one().id == record.id
    assert record.resume_fact_ids == ["fact-1"]
    assert Path(record.resume_docx_path).exists()
    assert record.resume_validation_errors == []
