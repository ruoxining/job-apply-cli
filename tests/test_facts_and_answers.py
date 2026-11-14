from datetime import date

from job_apply_agent.domain.enums import Sensitivity
from job_apply_agent.domain.schemas import (
    AnswerBankEntry,
    FormQuestion,
    ProfileFact,
    ResumeContent,
)
from job_apply_agent.services.answers import AnswerResolver
from job_apply_agent.services.facts import FactValidator
from job_apply_agent.tasks import _resolve_form_answers


def fact() -> ProfileFact:
    return ProfileFact(
        id="fact-1",
        statement="Reduced API latency by 35% using PostgreSQL indexes.",
        allowed_rewrites=["Cut API latency 35% through PostgreSQL indexing."],
        skills=["PostgreSQL"],
        metrics=["35%"],
        source="master-resume",
    )


def test_revision_must_use_an_explicitly_allowed_fact_rewrite():
    validator = FactValidator()
    valid = ResumeContent(
        headline="Candidate Profile",
        summary="Selected experience aligned to the target role.",
        bullets=["Cut API latency 35% through PostgreSQL indexing."],
        fact_ids=["fact-1"],
    )
    invalid = valid.model_copy(
        update={"bullets": ["Cut API latency 70% through PostgreSQL indexing."]}
    )
    assert validator.validate(valid, [fact()]) == []
    errors = validator.validate(invalid, [fact()])
    assert any("untraceable bullet" in error for error in errors)
    assert any("70%" in error for error in errors)


def test_unknown_sensitive_question_always_pauses():
    question = FormQuestion(
        field_id="gender",
        label="Gender",
        semantic_key="eeo_gender",
        required=True,
        sensitivity=Sensitivity.SENSITIVE,
    )
    result = AnswerResolver().resolve(question, [])
    assert result.requires_human
    assert result.value is None


def test_exact_unexpired_sensitive_answer_can_be_used():
    question = FormQuestion(
        field_id="visa",
        label="Will you require sponsorship?",
        semantic_key="work_authorization",
        required=True,
        options=["Yes", "No"],
        sensitivity=Sensitivity.SENSITIVE,
    )
    entry = AnswerBankEntry(
        semantic_key="work_authorization",
        answer="No",
        regions=["US"],
        sensitivity=Sensitivity.SENSITIVE,
        valid_until=date(2030, 1, 1),
    )
    result = AnswerResolver().resolve(question, [entry], region="US", today=date(2029, 1, 1))
    assert not result.requires_human
    assert result.value == "No"


def test_canonical_salary_answer_key_resolves_form_classifier_alias():
    question = FormQuestion(
        field_id="salary",
        label="Please outline your base salary expectations",
        semantic_key="salary_expectation",
        required=True,
        sensitivity=Sensitivity.SENSITIVE,
    )
    entry = AnswerBankEntry(
        semantic_key="salary_expectations",
        answer="Use the approved salary range",
        sensitivity=Sensitivity.SENSITIVE,
    )

    result = AnswerResolver().resolve(question, [entry])

    assert not result.requires_human
    assert result.source_key == "salary_expectation"
    assert result.value == "Use the approved salary range"


def test_required_narrative_is_deferred_for_fact_grounded_composition():
    question = FormQuestion(
        field_id="impact",
        label="Share an example of a project where you made a significant impact.",
        input_type="textarea",
        tag_name="textarea",
        required=True,
    )

    result = _resolve_form_answers([question], [])[0]

    assert not result.requires_human
    assert result.value is None
    assert result.source_key == "fact_grounded_narrative"
