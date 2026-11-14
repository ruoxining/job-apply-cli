from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from job_apply_agent.domain.schemas import CandidateProfile, ProfileFact
from job_apply_agent.models import ExperienceRecord, ExperienceRevisionRecord


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Experience(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    kind: str = "experience"
    description: list[str] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str
    revision: int = 1

    @model_validator(mode="after")
    def nonempty_description(self):
        self.description = [part.strip() for part in self.description if part.strip()]
        if not self.description:
            raise ValueError("description must not be empty")
        if sum(map(len, self.description)) > 50000:
            raise ValueError("one experience must be at most 50000 characters")
        return self

    def evidence(self) -> dict:
        return {"id": f"experience:{self.id}", **self.model_dump(exclude={"id"})}

    def as_profile_fact(self) -> ProfileFact:
        return ProfileFact(
            id=self.id,
            statement="\n".join(self.description)
            + "\n"
            + json.dumps(self.metadata, ensure_ascii=False),
            source=self.source,
            skills=self.metadata.get("skills", []),
        )


def read_canonical(path: Path) -> list[Experience]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    result = []
    for kind in ("experience", "projects", "education", "publications", "awards", "certificate"):
        for item in data.get(kind, []):
            if not isinstance(item, dict):
                continue
            metadata = {k: v for k, v in item.items() if k not in {"id", "description"}}
            descriptions = item.get("description") or [json.dumps(metadata, ensure_ascii=False)]
            if isinstance(descriptions, str):
                descriptions = [descriptions]
            result.append(
                Experience(
                    id=item.get("id") or f"{kind}-{fingerprint(item)[:12]}",
                    kind=kind,
                    description=descriptions,
                    metadata=metadata,
                    source=str(path),
                )
            )
    for key in ("profile", "skills", "coverage"):
        if data.get(key):
            result.append(
                Experience(
                    id="candidate-" + key,
                    kind=key,
                    description=[json.dumps(data[key], ensure_ascii=False)],
                    source=str(path),
                )
            )
    return result


def load_experiences(profile: CandidateProfile, path: Path, session=None) -> list[Experience]:
    # Canonical descriptions supersede the shortened PROFILE_JSON excerpt with the same ID.
    values = {
        fact.id: Experience(
            id=fact.id,
            description=[fact.statement],
            source=fact.source,
            metadata=fact.model_dump(mode="json", exclude={"id", "statement", "source"}),
        )
        for fact in profile.facts
    }
    if profile.skills:
        values["profile-skills"] = Experience(
            id="profile-skills",
            kind="skills",
            description=[json.dumps(profile.skills)],
            source="PROFILE_JSON.skills",
        )
    values.update({item.id: item for item in read_canonical(path)})
    if session is not None:
        for row in session.scalars(select(ExperienceRecord).order_by(ExperienceRecord.id)):
            values[row.id] = Experience.model_validate(row.payload)
    return list(values.values())


def save_experience(session, item: Experience, *, event_key: str) -> Experience:
    """Transaction belongs to caller. One immutable revision per Telegram update."""
    existing_event = session.scalar(
        select(ExperienceRevisionRecord).where(ExperienceRevisionRecord.event_key == event_key)
    )
    if existing_event is not None:
        return Experience.model_validate(existing_event.payload)
    row = session.get(ExperienceRecord, item.id)
    previous = Experience.model_validate(row.payload) if row else None
    content = item.model_dump(exclude={"revision", "source"})
    same = previous and content == previous.model_dump(exclude={"revision", "source"})
    revision = previous.revision + (0 if same else 1) if previous is not None else 1
    item = item.model_copy(update={"revision": revision})
    payload = item.model_dump(mode="json")
    if row is None:
        row = ExperienceRecord(id=item.id, payload=payload, content_hash=fingerprint(content))
        session.add(row)
    elif not same:
        row.payload, row.content_hash = payload, fingerprint(content)
    session.add(
        ExperienceRevisionRecord(
            experience_id=item.id,
            revision=item.revision,
            event_key=event_key,
            payload=payload,
        )
    )
    session.flush()
    return item
