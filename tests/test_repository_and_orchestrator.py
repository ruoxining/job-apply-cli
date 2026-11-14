from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import CandidateProfile, RawJob, SearchPolicy
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.repository import SubmissionRepository
from job_apply_agent.services.orchestrator import ApplicationOrchestrator


def raw_job(source_id: str = "one") -> RawJob:
    return RawJob(
        source=ATSKind.LEVER,
        source_id=source_id,
        company="Acme",
        title="Software Engineer",
        url=f"https://jobs.lever.co/acme/{source_id}",
        location="Remote - US",
        country="US",
        description="Python role requiring 2 years experience.",
    )


def candidate() -> CandidateProfile:
    return CandidateProfile(
        allowed_countries=["US"],
        years_experience=4,
        max_years_gap=1,
        skills=["python"],
        target_titles=["Other"],
    )


def test_reingest_and_cross_source_match_update_one_canonical_job(session):
    orchestrator = ApplicationOrchestrator()
    policy = SearchPolicy(version=1, high_value_threshold=100)
    first = orchestrator.ingest(session, raw_job(), candidate(), policy)
    second = orchestrator.ingest(session, raw_job(), candidate(), policy)
    cross_source = raw_job("ashby-one").model_copy(update={"source": ATSKind.ASHBY})
    third = orchestrator.ingest(session, cross_source, candidate(), policy)
    session.commit()

    assert first.created is True
    assert second.created is False
    assert third.job_id == first.job_id
    assert session.query(JobPostingRecord).count() == 1
    assert len(session.get(JobPostingRecord, first.job_id).sources) == 2


def test_ingested_via_is_set_once_on_creation(session):
    orchestrator = ApplicationOrchestrator()
    policy = SearchPolicy(version=1, high_value_threshold=100)

    def jd_raw() -> RawJob:
        return raw_job("jd-one").model_copy(update={"description": "Manual telegram JD posting."})

    def collected_raw() -> RawJob:
        return raw_job("col-one").model_copy(
            update={"description": "Collected from a public ATS board."}
        )

    manual = orchestrator.ingest(session, jd_raw(), candidate(), policy, ingested_via="telegram_jd")
    collected = orchestrator.ingest(session, collected_raw(), candidate(), policy)
    # Re-ingest from the other channel must not flip the creation-time channel.
    orchestrator.ingest(session, jd_raw(), candidate(), policy)
    orchestrator.ingest(session, collected_raw(), candidate(), policy, ingested_via="telegram_jd")
    session.commit()

    assert manual.created and collected.created
    assert session.get(JobPostingRecord, manual.job_id).ingested_via == "telegram_jd"
    assert session.get(JobPostingRecord, collected.job_id).ingested_via is None


def test_ineligible_ranking_reasons_are_not_duplicated(session):
    orchestrator = ApplicationOrchestrator()
    policy = SearchPolicy(version=1, high_value_threshold=100)
    raw = raw_job().model_copy(update={"description": "Requires 8 years of experience."})

    result = orchestrator.ingest(session, raw, candidate(), policy)
    session.commit()

    record = session.get(JobPostingRecord, result.job_id)
    assert record.eligible is False
    assert "experience_gap_too_large" in record.ranking_reasons
    assert len(record.ranking_reasons) == len(set(record.ranking_reasons))


def test_one_job_can_claim_submit_only_once(session):
    app = ApplicationOrchestrator().ingest(
        session, raw_job(), candidate(), SearchPolicy(version=1, high_value_threshold=100)
    )
    repo = SubmissionRepository()
    run = repo.get_or_create_run(session, application_id=app.job_id, idempotency_key="stable")
    assert (
        repo.get_or_create_run(session, application_id=app.job_id, idempotency_key="stable").id
        == run.id
    )
    assert repo.claim_submit(session, run.id)
    assert not repo.claim_submit(session, run.id)
    assert session.query(JobPostingRecord).count() == 1
