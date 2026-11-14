"""Local candidate setup. No network calls, model calls, or job dispatch."""

from __future__ import annotations

import json
from datetime import date
from importlib.resources import files
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain.schemas import AnswerBankEntry, CandidateProfile, SearchPolicy
from .experience.library import read_canonical
from .models import AnswerBankRecord
from .services.resumes import read_resume

TEMPLATES = (
    "profile.json",
    "answer-bank.json",
    "experiences.json",
    "source-boards.json",
    "search-policy.json",
)


def initialize(directory: Path) -> list[str]:
    """Copy templates without overwriting existing candidate information."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    for name in TEMPLATES:
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            continue
        content = files("job_apply_agent.examples").joinpath(name).read_bytes()
        with destination.open("xb") as stream:
            stream.write(content)
        destination.chmod(0o600)
        created.append(name)
    return created


def read_answers(path: Path) -> list[AnswerBankEntry]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise TypeError("answer bank must be a JSON list")
    entries = [AnswerBankEntry.model_validate(item) for item in raw]
    seen = set()
    for entry in entries:
        key = (entry.semantic_key, tuple(sorted(set(entry.regions))))
        if key in seen:
            raise ValueError("duplicate answer key and region scope")
        seen.add(key)
        if not entry.semantic_key.strip() or not entry.answer.strip():
            raise ValueError("answer keys and values must be non-empty")
        if "REPLACE_" in entry.answer or "example.test" in entry.answer:
            raise ValueError("replace the example answers before importing")
        if entry.valid_until is not None and entry.valid_until < date.today():
            raise ValueError("answer bank contains an expired answer")
    return entries


def check(settings: Settings) -> dict:
    """Validate structure and readiness without printing personal field values."""
    errors = []
    counts = {}
    operations = {
        "profile": lambda: CandidateProfile.model_validate(settings.candidate_profile()),
        "policy": lambda: SearchPolicy.model_validate(settings.search_policy()),
        "sources": settings.source_boards,
        "answers": lambda: read_answers(settings.answer_bank_path),
        "experiences": lambda: read_canonical(settings.experience_library_path),
    }
    for label, operation in operations.items():
        try:
            value = operation()
            if label == "profile":
                if not value.facts or not value.allowed_countries:
                    errors.append("profile: provide supported facts and target countries")
                if "REPLACE_" in value.model_dump_json():
                    errors.append("profile: replace all example markers")
                counts["facts"] = len(value.facts)
            elif label == "sources":
                enabled = [b for b in value if b.get("enabled", True)]
                counts["enabled_sources"] = len(enabled)
                if not enabled:
                    errors.append("sources: enable at least one real ATS board")
                if any(b["board_slug"].startswith("REPLACE_") for b in enabled):
                    errors.append("sources: replace example board slugs")
            elif label == "answers":
                counts["answers"] = len(value)
                keys = {entry.semantic_key for entry in value}
                missing = {"first_name", "last_name", "email"} - keys
                if missing:
                    errors.append("answers: missing keys " + ", ".join(sorted(missing)))
            elif label == "experiences":
                counts["experiences"] = len(value)
                if any("REPLACE_" in e.model_dump_json() for e in value):
                    errors.append("experiences: replace example markers or remove unused entries")
        except ValidationError as exc:
            fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
            errors.append(f"{label}: invalid schema at {', '.join(fields)}")
        except (OSError, ValueError, TypeError, AttributeError):
            errors.append(f"{label}: missing, malformed, expired, or placeholder data")
    if settings.resume_file is None:
        errors.append("resume: set RESUME_FILE to a reviewed PDF or DOCX for real applications")
    else:
        try:
            read_resume(settings.resume_file)
        except (OSError, ValueError):
            errors.append("resume: missing or invalid PDF/DOCX (maximum 20 MiB)")
    return {
        "ready": not errors,
        "mode": "live_enabled" if settings.allow_live_submit else "shadow",
        "counts": counts,
        "errors": errors,
        "limits": [
            "Checks validate structure, not the truth of experience or resume claims.",
            "A specific form may require additional scoped answers or consent.",
            "No credentials, browser, network, or submission were exercised.",
        ],
    }


def import_answers(session: Session, entries: list[AnswerBankEntry]) -> int:
    """Apply the explicitly supplied, reviewed answer file in the caller's transaction."""
    for entry in entries:
        regions = sorted(set(entry.regions))
        matches = [
            row
            for row in session.scalars(
                select(AnswerBankRecord).where(AnswerBankRecord.semantic_key == entry.semantic_key)
            )
            if sorted(set(row.regions or [])) == regions
        ]
        if len(matches) > 1:
            raise ValueError("existing answer bank contains ambiguous duplicate scopes")
        if matches:
            row = matches[0]
        else:
            row = AnswerBankRecord(semantic_key=entry.semantic_key)
            session.add(row)
        row.answer = entry.answer
        row.regions = regions
        row.sensitivity = entry.sensitivity.value
        row.valid_until = entry.valid_until
    session.flush()
    return len(entries)
