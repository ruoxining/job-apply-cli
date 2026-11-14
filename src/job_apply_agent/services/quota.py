from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from job_apply_agent.models import JobPostingRecord


class QuotaExceeded(RuntimeError):
    """Raised when the derived daily application budget is exhausted."""


@dataclass(frozen=True)
class QuotaSnapshot:
    quota_date: date
    fetched_count: int
    fetch_limit: int
    applied_count: int
    apply_limit: int


class DailyQuotaService:
    """Derive quotas from job rows instead of maintaining duplicate counters."""

    _ADVISORY_LOCK_KEY = 0x4A4F4241
    _FETCH_ADVISORY_LOCK_KEY = 0x4A4F4246

    def __init__(self, *, fetch_limit: int = 100, apply_limit: int = 20) -> None:
        if fetch_limit < 0 or apply_limit < 0:
            raise ValueError("quota limits cannot be negative")
        self.fetch_limit = fetch_limit
        self.apply_limit = apply_limit

    def snapshot(self, session: Session, on: date | None = None) -> QuotaSnapshot:
        quota_date = on or date.today()
        start = datetime.combine(quota_date, time.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        fetched = (
            session.scalar(
                select(func.count())
                .select_from(JobPostingRecord)
                .where(
                    JobPostingRecord.first_seen_at >= start,
                    JobPostingRecord.first_seen_at < end,
                    # Manually ingested rows (/jd) never consume the collect budget.
                    JobPostingRecord.ingested_via.is_(None),
                )
            )
            or 0
        )
        applied = (
            session.scalar(
                select(func.count())
                .select_from(JobPostingRecord)
                .where(JobPostingRecord.apply_claimed_on == quota_date)
            )
            or 0
        )
        return QuotaSnapshot(quota_date, fetched, self.fetch_limit, applied, self.apply_limit)

    def claim_application(
        self, session: Session, job: JobPostingRecord, *, on: date | None = None
    ) -> None:
        quota_date = on or date.today()
        if job.apply_claimed_on == quota_date:
            return
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": self._ADVISORY_LOCK_KEY},
            )
        if self.snapshot(session, quota_date).applied_count >= self.apply_limit:
            raise QuotaExceeded("daily application quota exceeded")
        job.apply_claimed_on = quota_date
        session.flush()

    def has_fetch_capacity(self, session: Session, *, on: date | None = None) -> bool:
        """Serialize collectors before checking the derived daily row count."""
        quota_date = on or date.today()
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": self._FETCH_ADVISORY_LOCK_KEY},
            )
        return self.snapshot(session, quota_date).fetched_count < self.fetch_limit
