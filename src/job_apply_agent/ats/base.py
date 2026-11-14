from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from job_apply_agent.domain.enums import BlockerKind
from job_apply_agent.domain.schemas import FormQuestion, ResolvedAnswer


class ATSBlocked(RuntimeError):
    def __init__(
        self,
        kind: BlockerKind,
        detail: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.evidence = evidence or {}


@dataclass
class InspectionResult:
    questions: list[FormQuestion]
    blocker: BlockerKind | None = None
    blocker_detail: str | None = None


@dataclass
class ApplicationResult:
    submitted: bool
    receipt: dict[str, Any] = field(default_factory=dict)
    blocker: BlockerKind | None = None


class SubmissionClaim(Protocol):
    def claim(self) -> bool: ...

    def complete(self, receipt: dict[str, Any]) -> None: ...


class ATSAdapter(ABC):
    @abstractmethod
    async def inspect(self, page: Any) -> InspectionResult: ...

    @abstractmethod
    async def fill(
        self,
        page: Any,
        answers: list[ResolvedAnswer],
        *,
        resume_path: Path,
        cover_letter_path: Path | None = None,
    ) -> None: ...

    @abstractmethod
    async def verify(self, page: Any) -> list[str]: ...

    @abstractmethod
    async def submit(self, page: Any, claim: SubmissionClaim) -> ApplicationResult: ...
