from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply_agent.collectors.normalizer import JobNormalizer
from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.domain.schemas import CandidateProfile, RawJob, SearchPolicy
from job_apply_agent.models import JobPostingRecord

from .policy import PriorityPolicy


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def canonical_job_key(raw: RawJob) -> str:
    """Conservative exact-content key; avoids merging distinct teams with one title."""
    payload = "\x1f".join(
        (_fold(raw.company), _fold(raw.title), _fold(raw.location), _fold(raw.description))
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class IngestedApplication:
    job_id: str
    application_id: str
    state: str
    created: bool


class ApplicationOrchestrator:
    def __init__(
        self,
        *,
        normalizer: JobNormalizer | None = None,
        priority_policy: PriorityPolicy | None = None,
    ) -> None:
        self.normalizer = normalizer or JobNormalizer()
        self.priority_policy = priority_policy or PriorityPolicy()

    def ingest(
        self,
        session: Session,
        raw: RawJob,
        profile: CandidateProfile,
        policy: SearchPolicy,
        *,
        ingested_via: str | None = None,
    ) -> IngestedApplication:
        key = canonical_job_key(raw)
        record = session.scalar(
            select(JobPostingRecord).where(
                (JobPostingRecord.source == raw.source.value)
                & (JobPostingRecord.source_id == raw.source_id)
            )
        )
        if record is None:
            record = session.scalar(
                select(JobPostingRecord).where(JobPostingRecord.canonical_key == key)
            )
        created = record is None
        job = self.normalizer.normalize(raw)
        normalized = job.model_dump(mode="json")
        source_ref = {"source": raw.source.value, "source_id": raw.source_id, "url": str(raw.url)}
        now = datetime.now(timezone.utc)
        raw_sha256 = (
            hashlib.sha256(
                json.dumps(raw.raw_snapshot, sort_keys=True, default=str).encode()
            ).hexdigest()
            if raw.raw_snapshot
            else None
        )
        if record is None:
            record = JobPostingRecord(
                canonical_key=key,
                source=raw.source.value,
                source_id=raw.source_id,
                # Ingestion channel is set once on creation and never flipped by
                # later dedupe merges, so quota accounting stays stable.
                ingested_via=ingested_via,
                sources=[source_ref],
                company=job.company,
                title=job.title,
                url=str(job.url),
                location=job.location,
                normalized=normalized,
                raw_sha256=raw_sha256,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(record)
            session.flush()
        else:
            sources = list(record.sources or [])
            if source_ref not in sources:
                sources.append(source_ref)
            record.sources = sources
            record.company = job.company
            record.title = job.title
            record.url = str(job.url)
            record.location = job.location
            record.normalized = normalized
            record.raw_sha256 = raw_sha256
            record.last_seen_at = now
            record.closed_at = None

        assessment = self.priority_policy.score(job, profile, policy)
        record.eligible = assessment.eligibility.eligible
        record.score = assessment.total_score
        record.score_breakdown = assessment.scores.model_dump()
        # Ineligible assessments already fold the eligibility reasons into
        # `reasons`; prepending them again duplicates every entry.
        record.ranking_reasons = list(assessment.reasons)
        record.ranker_version = f"policy-v{policy.version}"
        record.ranked_at = now
        if not assessment.eligibility.eligible:
            state = ApplicationState.INELIGIBLE
            channel = "auto"
        elif assessment.channel and assessment.channel.value == "telegram_review":
            state = ApplicationState.AWAITING_RESUME_REVIEW
            channel = assessment.channel.value
        else:
            state = ApplicationState.AUTO_APPLY_QUEUED
            channel = assessment.channel.value if assessment.channel else "auto"
        mutable_states = {
            ApplicationState.DISCOVERED.value,
            ApplicationState.INELIGIBLE.value,
            ApplicationState.AUTO_APPLY_QUEUED.value,
            ApplicationState.AWAITING_RESUME_REVIEW.value,
        }
        if created or record.application_state in mutable_states:
            record.application_state = state.value
            record.application_channel = channel
        session.flush()
        return IngestedApplication(
            job_id=record.id,
            application_id=record.id,
            state=record.application_state,
            created=created,
        )
