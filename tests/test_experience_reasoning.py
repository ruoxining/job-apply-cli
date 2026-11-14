from datetime import date

import pytest

from job_apply_agent.domain.schemas import AnswerBankEntry, FormQuestion
from job_apply_agent.experience.library import Experience
from job_apply_agent.experience.reasoning import AnswerReasoner, answer_evidence


class Planner:
    def __init__(self, proposed, supported=True):
        self.proposed, self.supported = proposed, supported
        self.prompts = []

    async def request_json(self, prompt, schema):
        self.prompts.append(prompt)
        if "status" in schema["properties"]:
            return self.proposed
        return {"supported": self.supported, "reason": "checked fixture"}


def reasoner(proposal, *, supported=True):
    return AnswerReasoner(
        Planner(proposal, supported),
        [
            Experience(id="db", description=["Built SQL pipelines"], source="fixture"),
            Experience(id="skills", kind="skills", description=["Python, SQL"], source="fixture"),
        ],
        [],
        mode="full",
    )


def proposal(answer="SQL", ids=None):
    return {
        "status": "answered",
        "answer": answer,
        "evidence_ids": ids or ["experience:db"],
        "support_summary": "SQL pipelines",
    }


async def test_checked_proposal_reaches_binding_with_trace_and_memo():
    service = reasoner(proposal())
    q = FormQuestion(field_id="language", label="Which query language?", options=["SQL", "Java"])
    result = await service.resolve(q, job_context={})
    assert result.value == "SQL" and result.source_key.startswith("verified_reasoning:")
    assert len(service.planner.prompts) == 2
    assert "experience:skills" in service.planner.prompts[0]
    assert service.records[0]["status"] == "accepted"
    assert await service.resolve(q, job_context={}) == result
    assert len(service.planner.prompts) == 2
    await service.resolve(q.model_copy(update={"options": ["Java"]}), job_context={})
    assert len(service.planner.prompts) == 3  # changed options invalidate memo; no checker needed


@pytest.mark.parametrize(
    "value,ids,options,maximum",
    [
        ("SQL", ["invented"], [], None),
        ("SQL", None, ["Java"], None),
        ("SQL", None, [], 2),
        ("", None, [], None),
    ],
)
async def test_structural_rejection_does_not_call_verifier(value, ids, options, maximum):
    service = reasoner(proposal(value, ids))
    result = await service.resolve(
        FormQuestion(field_id="a", label="Language", options=options, max_length=maximum),
        job_context={},
    )
    assert result.requires_human and result.value is None
    assert len(service.planner.prompts) == 1
    assert service.records[0]["status"] == "error"


async def test_verifier_rejection_and_missing_information_never_authorize():
    service = reasoner(proposal("unsupported"), supported=False)
    q = FormQuestion(field_id="a", label="Current company", required=True)
    assert (await service.resolve(q, job_context={})).requires_human
    assert service.records[0]["status"] == "rejected"
    service = reasoner(
        {"status": "unknown", "answer": None, "evidence_ids": [], "support_summary": "missing"}
    )
    assert (await service.resolve(q, job_context={})).requires_human
    assert len(service.planner.prompts) == 1


async def test_model_error_has_no_answer_and_no_private_provider_error():
    service = reasoner(proposal())

    async def fail(*args):
        raise RuntimeError("sensitive provider response")

    service.planner.request_json = fail
    result = await service.resolve(FormQuestion(field_id="a", label="Language"), job_context={})
    assert result.requires_human
    assert "sensitive" not in str(service.records)


def test_expired_and_wrong_region_excluded_but_unscoped_facts_remain():
    entries = [
        AnswerBankEntry(semantic_key="old", answer="x", valid_until=date(2020, 1, 1)),
        AnswerBankEntry(semantic_key="ca", answer="y", regions=["Canada"]),
        AnswerBankEntry(semantic_key="all", answer="z"),
    ]
    assert [e["semantic_key"] for e in answer_evidence(entries, "Japan", date(2026, 10, 26))] == [
        "all"
    ]


def test_year_and_current_questions_skip_model_loading_even_in_hybrid():
    service = reasoner(proposal())
    service.mode = "hybrid"
    # No vector cache configured: these questions must not initialize an encoder.
    assert service._retrieve("Current company").route == "coverage_full"
    assert service._retrieve("What happened in 2024?").route == "coverage_full"


async def test_unknown_question_is_memoized_until_context_changes():
    service = reasoner(
        {"status": "unknown", "answer": None, "evidence_ids": [], "support_summary": "missing"}
    )
    q = FormQuestion(field_id="a", label="Current company")
    await service.resolve(q, job_context={})
    await service.resolve(q, job_context={})
    assert len(service.planner.prompts) == 1
    await service.resolve(q, job_context={"country": "Japan"})
    assert len(service.planner.prompts) == 2
