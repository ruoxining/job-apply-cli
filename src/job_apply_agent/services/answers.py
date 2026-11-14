from __future__ import annotations

from datetime import date

from job_apply_agent.domain.enums import BlockerKind, Sensitivity
from job_apply_agent.domain.schemas import AnswerBankEntry, FormQuestion, ResolvedAnswer

ANSWER_KEY_ALIASES = {
    # Keep the form classifier stable while accepting the canonical key already
    # used by the persisted Answer Bank.
    "salary_expectation": frozenset({"salary_expectations"}),
}


class AnswerResolver:
    """Resolve form answers without inferring sensitive attributes."""

    def resolve(
        self,
        question: FormQuestion,
        entries: list[AnswerBankEntry],
        *,
        region: str | None = None,
        today: date | None = None,
    ) -> ResolvedAnswer:
        now = today or date.today()
        semantic_keys = {question.semantic_key} | ANSWER_KEY_ALIASES.get(
            question.semantic_key or "", frozenset()
        )
        candidates = [
            entry
            for entry in entries
            if question.semantic_key
            and entry.semantic_key in semantic_keys
            and (entry.valid_until is None or entry.valid_until >= now)
            and (not entry.regions or (region is not None and region in entry.regions))
        ]
        unique_answers = {entry.answer for entry in candidates}
        if len(unique_answers) == 1:
            answer = next(iter(unique_answers))
            if question.options and answer not in question.options:
                return self._block(question, "stored answer does not match available options")
            return ResolvedAnswer(
                field_id=question.field_id,
                value=answer,
                source_key=question.semantic_key,
            )

        if len(unique_answers) > 1:
            return self._block(question, "multiple conflicting answer-bank values")

        if question.required or question.sensitivity is not Sensitivity.NORMAL:
            return self._block(question, "no explicit answer-bank value")
        return ResolvedAnswer(
            field_id=question.field_id, value=None, reason="optional and unanswered"
        )

    @staticmethod
    def _block(question: FormQuestion, reason: str) -> ResolvedAnswer:
        blocker = (
            BlockerKind.SENSITIVE_ANSWER_MISSING
            if question.sensitivity is not Sensitivity.NORMAL
            else BlockerKind.UNKNOWN_QUESTION
        )
        return ResolvedAnswer(
            field_id=question.field_id,
            value=None,
            requires_human=True,
            blocker=blocker,
            reason=reason,
        )
