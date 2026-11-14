from __future__ import annotations

import re

from job_apply_agent.domain.schemas import ProfileFact, ResumeContent


class FactValidationError(ValueError):
    pass


class FactValidator:
    """Fail-closed validator for resume claims.

    Resume bullets must be a fact's original statement or one of its explicitly
    approved rewrites. Numbers and named skills elsewhere in the document must
    also exist in the selected facts.
    """

    NUMBER_RE = re.compile(r"(?<![A-Za-z])[$+]?\d+(?:[.,]\d+)?%?[xXkKmM]?(?![A-Za-z])")

    def validate(self, content: ResumeContent, facts: list[ProfileFact]) -> list[str]:
        by_id = {fact.id: fact for fact in facts}
        errors: list[str] = []
        missing = [fact_id for fact_id in content.fact_ids if fact_id not in by_id]
        if missing:
            errors.append(f"unknown fact ids: {', '.join(sorted(missing))}")

        selected = [by_id[fact_id] for fact_id in content.fact_ids if fact_id in by_id]
        allowed_bullets = {
            value.strip()
            for fact in selected
            for value in (fact.statement, *fact.allowed_rewrites)
            if value.strip()
        }
        for bullet in content.bullets:
            if bullet.strip() not in allowed_bullets:
                errors.append(f"untraceable bullet: {bullet}")

        allowed_numbers = {
            token
            for fact in selected
            for text in (fact.statement, *fact.allowed_rewrites, *fact.metrics)
            for token in self.NUMBER_RE.findall(text)
        }
        for token in self.NUMBER_RE.findall(
            "\n".join((content.headline, content.summary, *content.bullets))
        ):
            if token not in allowed_numbers:
                errors.append(f"untraceable numeric claim: {token}")

        return errors

    def assert_valid(self, content: ResumeContent, facts: list[ProfileFact]) -> None:
        errors = self.validate(content, facts)
        if errors:
            raise FactValidationError("; ".join(errors))
