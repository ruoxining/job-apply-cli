import json

import httpx
import pytest

from job_apply_agent.domain.enums import Sensitivity
from job_apply_agent.domain.schemas import FormQuestion, JobPosting, ProfileFact
from job_apply_agent.services.model_errors import TransientModelError
from job_apply_agent.services.narratives import (
    GLMNarrativeComposer,
    NarrativeCompositionError,
    is_narrative_question,
    narrative_question_key,
    validate_saved_narrative,
)


def job() -> JobPosting:
    return JobPosting(
        source="ashby",
        source_id="job-1",
        company="Example AI",
        title="Machine Learning Engineer",
        url="https://jobs.example.test/job-1",
        description="Build reliable machine-learning services with Python and PostgreSQL.",
    )


def facts() -> list[ProfileFact]:
    return [
        ProfileFact(
            id="latency",
            statement="Reduced API latency by 35% using PostgreSQL indexes.",
            skills=["Python", "PostgreSQL"],
            metrics=["35%"],
            source="master-resume",
        ),
        ProfileFact(
            id="pipeline",
            statement="Built a machine-learning evaluation pipeline in Python.",
            skills=["Python", "machine learning"],
            source="master-resume",
        ),
    ]


def test_narrative_classifier_separates_prose_from_objective_and_sensitive_fields():
    prose = FormQuestion(
        field_id="impact",
        label="Share an example of a project where you made a significant impact.",
        tag_name="textarea",
        input_type="textarea",
        required=True,
    )
    ordinary = FormQuestion(
        field_id="employer",
        label="What is your current employer?",
        tag_name="input",
        input_type="text",
        required=True,
    )
    sensitive = prose.model_copy(
        update={
            "field_id": "salary",
            "label": "Describe your salary expectations",
            "semantic_key": "salary_expectation",
            "sensitivity": Sensitivity.SENSITIVE,
        }
    )

    assert is_narrative_question(prose)
    assert not is_narrative_question(ordinary)
    assert not is_narrative_question(sensitive)
    assert narrative_question_key(prose) == narrative_question_key(
        prose.model_copy(update={"field_id": "rerendered-id"})
    )


async def test_glm_composer_uses_jd_and_verified_facts_and_returns_traceable_answer():
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        prompt = payload["messages"][1]["content"]
        assert "Machine Learning Engineer" in prompt
        assert "Reduced API latency by 35%" in prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": (
                                        "I improved a Python service by diagnosing its database "
                                        "bottleneck and adding PostgreSQL indexes, reducing API "
                                        "latency by 35%. This work combined hands-on implementation "
                                        "with measurable reliability impact relevant to the role."
                                    ),
                                    "fact_ids": ["latency"],
                                }
                            )
                        }
                    }
                ]
            },
        )

    composer = GLMNarrativeComposer(
        base_url="https://glm.example.test/api/paas/v4",
        api_key="secret",
        model="glm-test",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        answer = await composer.compose(
            question=FormQuestion(
                field_id="impact",
                label="Share an example of a project where you made a significant impact.",
                tag_name="textarea",
                input_type="textarea",
                required=True,
            ),
            job=job(),
            facts=facts(),
        )
    finally:
        await composer.close()

    assert "35%" in answer.value
    assert answer.fact_ids == ("latency",)
    assert answer.model == "glm-test"


async def test_glm_composer_retries_a_draft_that_fails_grounding_validation():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        prompt = json.loads(request.content)["messages"][1]["content"]
        if calls == 1:
            content = {"answer": "I improved latency by 70%.", "fact_ids": ["latency"]}
        else:
            assert "failed validation" in prompt
            content = {"answer": "I improved latency by 35%.", "fact_ids": ["latency"]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    composer = GLMNarrativeComposer(
        base_url="https://glm.example.test/api/paas/v4",
        api_key="secret",
        model="glm-test",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        answer = await composer.compose(
            question=FormQuestion(
                field_id="impact",
                label="Describe a high-impact project.",
                tag_name="textarea",
                input_type="textarea",
            ),
            job=job(),
            facts=facts(),
        )
    finally:
        await composer.close()

    assert calls == 2
    assert "35%" in answer.value


async def test_glm_composer_exposes_provider_outage_as_cloud_retryable():
    async def handler(request):
        del request
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

    composer = GLMNarrativeComposer(
        base_url="https://glm.example.test/api/paas/v4",
        api_key="secret",
        model="glm-test",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TransientModelError) as error:
            await composer.compose(
                question=FormQuestion(
                    field_id="impact",
                    label="Describe a high-impact project.",
                    tag_name="textarea",
                    input_type="textarea",
                ),
                job=job(),
                facts=facts(),
            )
    finally:
        await composer.close()
    assert error.value.reason_code == "glm_transient_http"


@pytest.mark.parametrize(
    "record, match",
    [
        ({"answer": "Improved latency by 70%.", "fact_ids": ["latency"]}, "70%"),
        ({"answer": "Improved the service.", "fact_ids": ["unknown"]}, "unknown"),
        ({"answer": "Improved the service.", "fact_ids": []}, "did not cite"),
    ],
)
def test_saved_narrative_rejects_unsupported_or_untraceable_claims(record, match):
    with pytest.raises(NarrativeCompositionError, match=match):
        validate_saved_narrative(record, facts=facts(), maximum=None)
