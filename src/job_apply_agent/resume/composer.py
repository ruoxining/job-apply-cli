from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from docx import Document
from docx.shared import Pt

from job_apply_agent.domain.schemas import (
    JobPosting,
    ProfileFact,
    ResumeArtifact,
    ResumeContent,
)
from job_apply_agent.services.facts import FactValidator


class ResumeComposer:
    def __init__(self, output_dir: Path, validator: FactValidator | None = None) -> None:
        self.output_dir = output_dir
        self.validator = validator or FactValidator()

    def compose(
        self,
        job: JobPosting,
        facts: list[ProfileFact],
        *,
        revision: str | None = None,
        template: str = "default",
    ) -> tuple[ResumeContent, ResumeArtifact]:
        chosen = self._select_facts(job, facts, revision=revision)
        content = ResumeContent(
            headline="Candidate Profile",
            summary="Selected experience aligned to the target role.",
            bullets=[fact.statement for fact in chosen],
            fact_ids=[fact.id for fact in chosen],
        )
        errors = self.validator.validate(content, facts)
        if errors:
            raise ValueError("resume fact validation failed: " + "; ".join(errors))

        variant_id = str(uuid4())
        variant_dir = self.output_dir / variant_id
        variant_dir.mkdir(parents=True, exist_ok=False)
        docx_path = variant_dir / "resume.docx"
        self._write_docx(docx_path, content, template=template)
        pdf_path = self._convert_pdf(docx_path)
        digest = hashlib.sha256((pdf_path or docx_path).read_bytes()).hexdigest()
        artifact = ResumeArtifact(
            variant_id=variant_id,
            docx_path=str(docx_path),
            pdf_path=str(pdf_path) if pdf_path else None,
            sha256=digest,
            fact_ids=content.fact_ids,
            validation_errors=[],
        )
        return content, artifact

    @staticmethod
    def _select_facts(
        job: JobPosting, facts: list[ProfileFact], *, revision: str | None
    ) -> list[ProfileFact]:
        terms = set(re.findall(r"[a-z0-9+#.]+", f"{job.title} {job.description}".casefold()))

        def score(fact: ProfileFact) -> tuple[int, str]:
            fact_terms = set(
                re.findall(r"[a-z0-9+#.]+", f"{fact.statement} {' '.join(fact.skills)}".casefold())
            )
            overlap = len(terms & fact_terms)
            instruction = (revision or "").casefold()
            if "technical" in instruction or "技术" in instruction:
                overlap += len(set(map(str.casefold, fact.skills)) & terms) * 2
            if "business" in instruction or "业务" in instruction:
                overlap += len(fact.metrics)
            return overlap, fact.id

        limit = 3 if revision and ("concise" in revision.casefold() or "精简" in revision) else 6
        return sorted(facts, key=score, reverse=True)[:limit]

    @staticmethod
    def _write_docx(path: Path, content: ResumeContent, *, template: str) -> None:
        document = Document()
        normal = document.styles["Normal"]
        normal.font.name = "Arial"
        normal.font.size = Pt(10)
        document.add_heading(content.headline, level=0)
        document.add_paragraph(content.summary)
        document.add_heading("Relevant Experience", level=1)
        for bullet in content.bullets:
            document.add_paragraph(bullet, style="List Bullet")
        document.core_properties.subject = f"ATS resume template: {template}"
        document.save(str(path))

    @staticmethod
    def _convert_pdf(docx_path: Path) -> Path | None:
        executable = shutil.which("libreoffice") or shutil.which("soffice")
        if executable is None:
            return None
        result = subprocess.run(
            [
                executable,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(docx_path.parent),
                str(docx_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        pdf_path = docx_path.with_suffix(".pdf")
        if result.returncode != 0 or not pdf_path.exists():
            raise RuntimeError(f"LibreOffice PDF conversion failed: {result.stderr.strip()}")
        return pdf_path
