"""Derive country answers from one user-confirmed scope, with source provenance."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_apply_agent.domain.schemas import FormQuestion, ResolvedAnswer

from .answers import AnswerResolver

EMPLOYMENT_SCOPE_KEY = "employment_authorization_scope"


class EmploymentAuthorizationScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    currently_authorized_countries: list[str] = Field(min_length=1)
    authorized_countries_exhaustive: Literal[True]
    sponsorship_required_elsewhere: Literal[True]
    sponsorship_timeframe: Literal["now_and_future"]


def derive_employment_answer(question: FormQuestion, entries: list, *, region: str | None):
    resolver = AnswerResolver()
    if not region:
        return resolver._block(question, "job country needs resolution, not a new candidate fact")
    fact = resolver.resolve(
        question.model_copy(update={"semantic_key": EMPLOYMENT_SCOPE_KEY, "options": []}),
        entries,
    )
    if fact.requires_human or not fact.value:
        return fact
    try:
        scope = EmploymentAuthorizationScope.model_validate_json(fact.value)
    except ValidationError:
        return resolver._block(question, "employment scope is not a valid exhaustive rule")
    authorized = {name.strip().casefold() for name in scope.currently_authorized_countries}
    if any(not name for name in authorized):
        return resolver._block(question, "employment scope contains an empty country")
    outside = region.casefold() not in authorized
    if question.semantic_key == "work_authorization_by_country":
        value = "No" if outside else "Yes"
        rule = "current authorization from the confirmed exhaustive country set"
    elif question.semantic_key == "sponsorship_by_country" and outside:
        value = "Yes"
        rule = "sponsorship now and in the future outside the confirmed country set"
    else:
        # A temporary permit does not establish future sponsorship needs inside
        # the authorized set; existing country-specific answers handle that case.
        return resolver._block(question, "no applicable sponsorship rule for this country")
    if question.options and value not in question.options:
        return resolver._block(question, "stored answer does not match available options")
    return ResolvedAnswer(
        field_id=question.field_id,
        value=value,
        source_key=EMPLOYMENT_SCOPE_KEY,
        reason=f"Derived for {region}: {rule}.",
    )
