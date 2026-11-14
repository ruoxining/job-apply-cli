import json
from datetime import UTC, datetime

import pytest

from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.application_report import application_report


def seed(session, key, **values):
    record = JobPostingRecord(
        id=key,
        canonical_key=key,
        source="lever",
        source_id=key,
        company="Private employer",
        title="Engineer",
        url="https://jobs.example.test/private",
        normalized={},
        **values,
    )
    session.add(record)
    session.commit()
    return record


def test_report_requires_receipt_and_claim_evidence_not_submitted_label(session):
    now = datetime.now(UTC)
    seed(
        session,
        "confirmed",
        application_state="submitted",
        submit_started_at=now,
        submit_completed_at=now,
        receipt={"status": "submission_confirmed", "confirmation": "Thank you"},
    )
    seed(session, "label-only", application_state="submitted")
    seed(session, "claim", application_state="submitting", submit_started_at=now)
    seed(session, "timestamp-only", application_state="submitted", submit_completed_at=now)
    report = application_report(session)
    assert report["total"] == 4
    assert report["outcomes"] == {
        "confirmed_submission": 1,
        "claim_without_confirmation": 1,
        "inconsistent_submission_evidence": 2,
    }
    assert sum(report["outcomes"].values()) == report["total"]


def test_report_redacts_answers_and_does_not_change_or_requeue_unknown_claim(session):
    now = datetime.now(UTC)
    record = seed(
        session,
        "protected",
        application_state="needs_human",
        submit_started_at=now,
        blocker={
            "kind": "browser_error",
            "detail": "private@example.test",
            "reason_code": "private@example.test",
            "missing_questions": ["private"],
        },
        form_answers={"secret": "private answer"},
    )
    session.refresh(record)
    before = (record.application_state, record.submit_started_at, dict(record.blocker))
    report = application_report(session, application_ids=[record.id])
    item = report["items"][0]
    assert item["outcome"] == "claim_without_confirmation"
    assert item["next_action"] == "inspect_process_and_reconcile_receipt"
    serialized = json.dumps(report)
    assert "private" not in serialized.lower()
    assert item["reason_code"] == "unknown"
    assert item["missing_question_count"] == 1
    session.refresh(record)
    assert (record.application_state, record.submit_started_at, record.blocker) == before
    assert not session.dirty


def test_explicit_scope_must_not_silently_expand_or_drop_missing_jobs(session):
    seed(session, "selected", application_state="needs_human", blocker={"kind": "unknown_question"})
    seed(session, "excluded", application_state="needs_human", blocker={"kind": "captcha"})
    report = application_report(session, application_ids=["selected"])
    assert report["total"] == 1
    assert report["outcomes"] == {"unresolved_answer": 1}
    assert report["items"][0]["next_action"] == "inspect_available_facts_and_question_mapping"
    with pytest.raises(ValueError, match="missing"):
        application_report(session, application_ids=["selected", "missing"])
    with pytest.raises(ValueError, match="empty"):
        application_report(session, application_ids=[])


def test_known_duplicate_preserves_claim_without_counting_new_success(session):
    now = datetime.now(UTC)
    record = seed(
        session,
        "duplicate",
        application_state="skipped",
        submit_started_at=now,
        receipt={
            "status": "previous_application_confirmed",
            "confirmation": "Your application was already submitted.",
            "previous_application_date": "2025-01-15",
            "private_email": "private@example.test",
        },
        blocker={"kind": "ats_application_limit", "reason_code": "previous_application"},
    )
    session.refresh(record)
    before = (record.submit_started_at, record.submit_completed_at, dict(record.receipt))
    report = application_report(session)
    assert report["outcomes"] == {"existing_application": 1}
    assert report["items"][0]["next_action"] == "respect_reapplication_policy"
    assert report["items"][0]["submit_claimed"]
    assert not report["items"][0]["submit_completed"]
    assert "private@example.test" not in json.dumps(report)
    session.refresh(record)
    assert (record.submit_started_at, record.submit_completed_at, record.receipt) == before
    assert not session.dirty


@pytest.mark.parametrize(
    "invalid", ["no_claim", "empty_confirmation", "submitted_label", "completed"]
)
def test_inconsistent_duplicate_evidence_requires_reconciliation(session, invalid):
    now = datetime.now(UTC)
    seed(
        session,
        invalid,
        application_state="submitted" if invalid == "submitted_label" else "skipped",
        submit_started_at=None if invalid == "no_claim" else now,
        submit_completed_at=now if invalid == "completed" else None,
        receipt={
            "status": "previous_application_confirmed",
            "confirmation": " " if invalid == "empty_confirmation" else "Already applied",
        },
    )
    report = application_report(session)
    assert report["outcomes"] == {"inconsistent_submission_evidence": 1}
