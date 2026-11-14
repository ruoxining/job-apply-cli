from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from sqlalchemy.orm import Session

from job_apply_agent.domain.schemas import JobPosting, ProfileFact
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.resume.composer import ResumeComposer


def read_resume(path: Path) -> bytes:
    """Read a user-reviewed PDF/DOCX without parsing or rewriting its claims."""
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("resume must be an existing file of at most 20 MiB")
    content = path.read_bytes()
    if path.suffix.lower() == ".pdf" and content.startswith(b"%PDF-"):
        return content
    if path.suffix.lower() == ".docx":
        try:
            with ZipFile(path) as archive:
                if "word/document.xml" in archive.namelist():
                    return content
        except BadZipFile:
            pass
    raise ValueError("resume must be a PDF or DOCX with a matching file format")


class ResumeVariantService:
    """Replace the current temporary resume artifact on the canonical job."""

    def __init__(
        self, output_dir: Path, retention_days: int = 7, *, provided_file: Path | None = None
    ) -> None:
        self.composer = ResumeComposer(output_dir)
        self.retention_days = retention_days
        self.provided_file = provided_file

    def create(
        self,
        session: Session,
        *,
        job_id: str,
        job: JobPosting,
        facts: list[ProfileFact],
        revision: str | None = None,
        template: str = "default",
    ) -> JobPostingRecord:
        record = session.get(JobPostingRecord, job_id)
        if record is None:
            raise LookupError(f"job {job_id} not found")
        if self.provided_file is not None:
            if revision:
                raise ValueError("revise the provided resume file directly before preparing again")
            content = read_resume(self.provided_file)
            directory = self.composer.output_dir / str(uuid4())
            directory.mkdir(parents=True, mode=0o700)
            snapshot = directory / ("resume" + self.provided_file.suffix.lower())
            snapshot.write_bytes(content)
            snapshot.chmod(0o600)
            record.resume_template = "user-provided"
            # A prewritten document was reviewed by its owner. Do not claim that
            # this service verified its text against the structured facts.
            record.resume_fact_ids = []
            record.resume_validation_errors = []
            record.resume_docx_path = str(snapshot) if snapshot.suffix == ".docx" else None
            record.resume_pdf_path = str(snapshot) if snapshot.suffix == ".pdf" else None
            record.resume_sha256 = hashlib.sha256(content).hexdigest()
        else:
            _, artifact = self.composer.compose(job, facts, revision=revision, template=template)
            record.resume_template = template
            record.resume_fact_ids = artifact.fact_ids
            record.resume_validation_errors = artifact.validation_errors
            record.resume_docx_path = artifact.docx_path
            record.resume_pdf_path = artifact.pdf_path
            record.resume_sha256 = artifact.sha256
        now = datetime.now(UTC)
        record.resume_generated_at = now
        record.resume_expires_at = now + timedelta(days=self.retention_days)
        record.resume_purged_at = None
        session.flush()
        return record
