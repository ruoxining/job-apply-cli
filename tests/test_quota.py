from datetime import date, datetime, timezone

import pytest

from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.quota import DailyQuotaService, QuotaExceeded


def job(
    job_id: str,
    *,
    first_seen: datetime,
    claimed: date | None = None,
    ingested_via: str | None = None,
) -> JobPostingRecord:
    return JobPostingRecord(
        id=job_id,
        canonical_key=job_id.ljust(64, "0"),
        source="lever",
        source_id=job_id,
        sources=[],
        company="Acme",
        title="Engineer",
        url=f"https://example.test/{job_id}",
        location="Toronto",
        normalized={},
        first_seen_at=first_seen,
        last_seen_at=first_seen,
        apply_claimed_on=claimed,
        ingested_via=ingested_via,
    )


def test_quota_is_derived_from_job_rows_and_blocks_over_limit(session):
    day = date(2026, 10, 10)
    session.add(job("one", first_seen=datetime(2026, 10, 10, tzinfo=timezone.utc), claimed=day))
    session.add(job("two", first_seen=datetime(2026, 10, 10, 1, tzinfo=timezone.utc)))
    session.flush()
    service = DailyQuotaService(fetch_limit=3, apply_limit=2)
    service.claim_application(session, session.get(JobPostingRecord, "two"), on=day)
    snapshot = service.snapshot(session, day)
    assert (snapshot.fetched_count, snapshot.applied_count) == (2, 2)

    third = job("three", first_seen=datetime(2026, 10, 10, 2, tzinfo=timezone.utc))
    session.add(third)
    session.flush()
    with pytest.raises(QuotaExceeded):
        service.claim_application(session, third, on=day)
    assert session.query(JobPostingRecord).count() == 3


def test_snapshot_excludes_manually_ingested_rows_from_fetch_count(session):
    day = date(2026, 10, 10)
    session.add(
        job(
            "manual",
            first_seen=datetime(2026, 10, 10, tzinfo=timezone.utc),
            ingested_via="telegram_jd",
        )
    )
    session.add(job("collected", first_seen=datetime(2026, 10, 10, 1, tzinfo=timezone.utc)))
    session.flush()
    snapshot = DailyQuotaService(fetch_limit=5, apply_limit=2).snapshot(session, day)
    assert snapshot.fetched_count == 1


def test_has_fetch_capacity_ignores_manual_rows(session):
    day = date(2026, 10, 10)
    first_seen = datetime(2026, 10, 10, tzinfo=timezone.utc)
    session.add(job("jd-one", first_seen=first_seen, ingested_via="telegram_jd"))
    session.add(job("jd-two", first_seen=first_seen, ingested_via="telegram_jd"))
    session.flush()
    service = DailyQuotaService(fetch_limit=2, apply_limit=2)
    assert service.has_fetch_capacity(session, on=day)
    session.add(job("col-one", first_seen=first_seen))
    session.add(job("col-two", first_seen=first_seen))
    session.flush()
    assert not service.has_fetch_capacity(session, on=day)
