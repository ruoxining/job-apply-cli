from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from .domain.enums import RunStatus
from .models import JobPostingRecord


class SubmissionRepository:
    """Submit-once guard stored on the canonical job row."""

    def get_or_create_run(
        self, session: Session, *, application_id: str, idempotency_key: str
    ) -> JobPostingRecord:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise LookupError(f"job {application_id} not found")
        if job.submission_key and job.submission_key != idempotency_key:
            return job
        job.submission_key = idempotency_key
        job.submission_status = RunStatus.PENDING.value
        session.flush()
        return job

    def claim_submit(self, session: Session, run_id: str) -> bool:
        now = datetime.now(timezone.utc)
        result = session.execute(
            update(JobPostingRecord)
            .where(
                JobPostingRecord.id == run_id,
                JobPostingRecord.submit_started_at.is_(None),
                JobPostingRecord.submit_completed_at.is_(None),
            )
            .values(submit_started_at=now, submission_status=RunStatus.RUNNING.value)
        )
        session.flush()
        return result.rowcount == 1

    def complete_submit(self, session: Session, run_id: str, receipt: dict) -> None:
        now = datetime.now(timezone.utc)
        session.execute(
            update(JobPostingRecord)
            .where(JobPostingRecord.id == run_id)
            .values(
                submit_completed_at=now,
                submitted_at=now,
                submission_status=RunStatus.SUBMITTED.value,
                receipt=receipt,
            )
        )
