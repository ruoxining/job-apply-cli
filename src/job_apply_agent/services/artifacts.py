from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply_agent.models import JobPostingRecord


def purge_expired_resumes(
    session: Session, artifacts_dir: Path, *, now: datetime | None = None
) -> int:
    """Delete expired resume files inside the artifact root and clear their paths."""
    now = now or datetime.now(timezone.utc)
    root = artifacts_dir.resolve()
    rows = list(
        session.scalars(
            select(JobPostingRecord).where(
                JobPostingRecord.resume_expires_at.is_not(None),
                JobPostingRecord.resume_expires_at <= now,
                JobPostingRecord.resume_purged_at.is_(None),
            )
        )
    )
    for job in rows:
        for value in (job.resume_docx_path, job.resume_pdf_path):
            if not value:
                continue
            path = Path(value).resolve()
            if path.is_relative_to(root) and path.is_file():
                path.unlink()
        job.resume_docx_path = None
        job.resume_pdf_path = None
        job.resume_purged_at = now
    session.flush()
    return len(rows)
