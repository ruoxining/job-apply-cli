from datetime import date

from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.quota import DailyQuotaService
from job_apply_agent.services.review_actions import ReviewActionService


def job(job_id: str, *, with_resume: bool) -> JobPostingRecord:
    return JobPostingRecord(
        id=job_id, canonical_key=job_id.ljust(64, "0"), source="lever", source_id=job_id,
        sources=[], company="Acme", title="Engineer", url=f"https://example.test/{job_id}",
        location="Toronto", normalized={},
        application_state=ApplicationState.AWAITING_RESUME_REVIEW.value,
        resume_sha256="abc" if with_resume else None,
        resume_pdf_path="/tmp/resume.pdf" if with_resume else None,
    )


def needs_human_job(
    job_id: str, *, with_resume: bool = True, blocker: dict | None = None
) -> JobPostingRecord:
    return JobPostingRecord(
        id=job_id, canonical_key=job_id.ljust(64, "0"), source="lever", source_id=job_id,
        sources=[], company="Acme", title="Engineer", url=f"https://example.test/{job_id}",
        location="Toronto", normalized={},
        application_state=ApplicationState.NEEDS_HUMAN.value,
        submission_status=ApplicationState.NEEDS_HUMAN.value,
        blocker=blocker,
        resume_sha256="abc" if with_resume else None,
        resume_pdf_path="/tmp/resume.pdf" if with_resume else None,
    )


def test_approval_requires_locked_resume(session):
    record = job("one", with_resume=False)
    session.add(record)
    session.flush()
    decision = ReviewActionService().approve_application(session, record.id)
    assert not decision.accepted
    assert "简历尚未锁定" in decision.message


def test_approval_claims_quota_on_the_job_row(session):
    record = job("two", with_resume=True)
    session.add(record)
    session.flush()
    decision = ReviewActionService(DailyQuotaService(apply_limit=1)).approve_application(
        session, record.id
    )
    session.commit()
    assert decision.accepted
    assert decision.idempotency_key.startswith(f"telegram:{record.id}:")
    assert record.application_state == ApplicationState.MATERIALS_READY.value
    assert record.apply_claimed_on == date.today()


def test_rejection_and_feedback_are_persisted_on_job(session):
    record = job("three", with_resume=True)
    session.add(record)
    session.flush()
    service = ReviewActionService()

    rejected = service.reject_application(session, record.id)
    feedback = service.record_feedback(
        session, record.id, "岗位太偏 CV，降低同类职位排名"
    )
    session.commit()

    assert rejected.accepted
    assert feedback.accepted
    assert record.application_state == ApplicationState.SKIPPED.value
    assert record.review_feedback == "岗位太偏 CV，降低同类职位排名"
    assert record.reviewed_at is not None


def test_manual_takeover_records_skip_and_clears_blocker(session):
    record = needs_human_job("manual-one", blocker={"kind": "captcha", "detail": "stopped"})
    session.add(record)
    session.flush()

    decision = ReviewActionService().mark_applied_manually(session, record.id)
    session.commit()

    assert decision.accepted
    assert record.application_state == ApplicationState.SKIPPED.value
    assert record.review_feedback == "用户决定自行投递"
    assert record.blocker is None
    assert record.reviewed_at is not None


def test_manual_takeover_requires_needs_human(session):
    record = job("manual-two", with_resume=True)
    session.add(record)
    session.flush()

    decision = ReviewActionService().mark_applied_manually(session, record.id)

    assert not decision.accepted
    assert "无需人工接管" in decision.message


def test_retry_requeues_locked_resume_for_submission(session):
    record = needs_human_job("retry-one", blocker={"kind": "browser_error", "detail": "x"})
    session.add(record)
    session.flush()

    decision = ReviewActionService().retry_application(session, record.id)
    session.commit()

    assert decision.accepted
    assert decision.next_action == "run"
    assert decision.idempotency_key == f"auto:{record.id}:abc"
    assert record.application_state == ApplicationState.MATERIALS_READY.value
    assert record.blocker is None
    assert record.telegram_result_notified_at is None


def test_retry_without_resume_requeues_for_preparation(session):
    record = needs_human_job("retry-two", with_resume=False, blocker={"kind": "captcha"})
    session.add(record)
    session.flush()

    decision = ReviewActionService().retry_application(session, record.id)
    session.commit()

    assert decision.accepted
    assert decision.next_action == "prepare"
    assert record.application_state == ApplicationState.AUTO_APPLY_QUEUED.value


def test_retry_refuses_unknown_submit_outcome(session):
    record = needs_human_job(
        "retry-three",
        blocker={"kind": "submit_outcome_unknown", "detail": "reconcile manually"},
    )
    session.add(record)
    session.flush()

    decision = ReviewActionService().retry_application(session, record.id)
    session.commit()

    assert not decision.accepted
    assert "禁止自动重试" in decision.message
    assert record.application_state == ApplicationState.NEEDS_HUMAN.value
