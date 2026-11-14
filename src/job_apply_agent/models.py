from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .domain.enums import ApplicationChannel, ApplicationState, RunStatus, Sensitivity


def _uuid() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AnswerBankRecord(Base, TimestampMixin):
    """Human-confirmed reusable form answers; not a derived cache."""

    __tablename__ = "answer_bank"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    semantic_key: Mapped[str] = mapped_column(String(255), index=True)
    answer: Mapped[str] = mapped_column(Text)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list)
    sensitivity: Mapped[str] = mapped_column(String(32), default=Sensitivity.NORMAL.value)
    valid_until: Mapped[date | None] = mapped_column(Date)


class JobPostingRecord(Base, TimestampMixin):
    """One canonical job and its current ranking, application, and artifact state."""

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_job_source_id"),
        UniqueConstraint("canonical_key", name="uq_job_canonical_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    canonical_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(255), index=True)
    # Ingestion channel that created the row; NULL = collect-discoverable and
    # counted toward the daily fetch quota. "telegram_jd" rows are quota-exempt.
    ingested_via: Mapped[str | None] = mapped_column(String(32))
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    company: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(Text, index=True)
    location: Mapped[str] = mapped_column(String(500), default="")
    normalized: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ranking_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    ranker_version: Mapped[str | None] = mapped_column(String(64))
    ranked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application_channel: Mapped[str] = mapped_column(
        String(32), default=ApplicationChannel.AUTO.value
    )
    application_state: Mapped[str] = mapped_column(
        String(40), default=ApplicationState.DISCOVERED.value, index=True
    )
    apply_claimed_on: Mapped[date | None] = mapped_column(Date, index=True)
    form_answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    blocker: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_review_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_result_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_feedback: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    resume_template: Mapped[str | None] = mapped_column(String(100))
    resume_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    resume_validation_errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    resume_docx_path: Mapped[str | None] = mapped_column(Text)
    resume_pdf_path: Mapped[str | None] = mapped_column(Text)
    resume_sha256: Mapped[str | None] = mapped_column(String(64))
    resume_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    resume_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submission_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    submission_status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value)
    last_error: Mapped[str | None] = mapped_column(Text)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    submit_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submit_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperienceRecord(Base, TimestampMixin):
    __tablename__ = "experiences"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))


class ExperienceRevisionRecord(Base, TimestampMixin):
    __tablename__ = "experience_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    experience_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column()
    event_key: Mapped[str] = mapped_column(String(255), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
