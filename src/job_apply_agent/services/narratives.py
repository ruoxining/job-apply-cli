from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import httpx

from job_apply_agent.domain.enums import Sensitivity
from job_apply_agent.domain.schemas import FormQuestion, JobPosting, ProfileFact
from job_apply_agent.services.facts import FactValidator
from job_apply_agent.services.model_errors import transient_model_error


class NarrativeCompositionError(RuntimeError):
    """A prose answer could not be generated with traceable candidate facts."""


@dataclass(frozen=True)
class NarrativeAnswer:
    value: str
    fact_ids: tuple[str, ...]
    model: str


NARRATIVE_PROMPT_RE = re.compile(
    r"(?:\bwhy\b|\bhow (?:did|have|would|do you|has)\b|"
    r"\bwhat (?:was|did|would|makes|interests)\b|describe|tell us|"
    r"share (?:an|a)|example|"
    r"project|impact|challenge|motivat|interest(?:ed)? in|proud|experience with|"
    r"additional information|anything else|cover letter|use of (?:ai|machine learning))",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-z0-9+#.]+", re.IGNORECASE)


def is_narrative_question(question: FormQuestion) -> bool:
    """Identify prose prompts without treating ordinary fact fields as essays."""
    if question.sensitivity is not Sensitivity.NORMAL:
        return False
    if question.semantic_key or question.options:
        return False
    if question.tag_name == "textarea" or question.input_type == "textarea":
        return True
    return question.input_type in {"", "text"} and bool(NARRATIVE_PROMPT_RE.search(question.label))


def narrative_question_key(question: FormQuestion) -> str:
    normalized = " ".join(question.label.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:20]


def validate_saved_narrative(
    record: dict,
    *,
    facts: list[ProfileFact],
    maximum: int | None,
) -> NarrativeAnswer:
    """Revalidate a persisted draft before putting it back into a live form."""
    return _validate_narrative_payload(
        {"answer": record.get("answer"), "fact_ids": record.get("fact_ids")},
        available_facts=facts,
        model=str(record.get("model") or "saved"),
        maximum=maximum,
    )


class GLMNarrativeComposer:
    """Compose short application prose from supplied facts and the target JD."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
        reasoning_effort: str = "high",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def compose(
        self,
        *,
        question: FormQuestion,
        job: JobPosting,
        facts: list[ProfileFact],
    ) -> NarrativeAnswer:
        selected = _select_relevant_facts(question, job, facts)
        if not selected:
            raise NarrativeCompositionError(
                "no verified profile facts are available for this narrative question"
            )
        facts_payload = [
            {
                "id": fact.id,
                "statement": fact.statement,
                "skills": fact.skills,
                "metrics": fact.metrics,
                "allowed_rewrites": fact.allowed_rewrites,
            }
            for fact in selected
        ]
        maximum = question.max_length if question.max_length and question.max_length > 0 else None
        length_rule = (
            f"The answer must not exceed {maximum} characters."
            if maximum
            else "Write 90-160 words unless the prompt clearly calls for a shorter answer."
        )
        prompt = f"""Write a concise answer to one job-application prose question.

Question:
{question.label}

Target role:
Company: {job.company}
Title: {job.title}
Job description: {job.description[:6000]}

Verified candidate facts (the only allowed source for claims about the candidate):
{json.dumps(facts_payload, ensure_ascii=False)}

Rules:
- Reason about which verified experience best answers the question and aligns with the JD.
- You may paraphrase supplied facts, but do not invent employers, projects, duties, tools,
  dates, metrics, outcomes, preferences, or personal attributes.
- Do not claim the candidate has a skill merely because it appears in the JD.
- Every candidate-experience claim must be supported by one or more returned fact_ids.
- {length_rule}
- Return JSON only: {{"answer":"...","fact_ids":["fact-id"]}}.
"""
        last_error: NarrativeCompositionError | None = None
        for attempt in range(2):
            retry_feedback = (
                f"\nThe prior draft failed validation: {last_error}. Return a corrected draft."
                if last_error
                else ""
            )
            try:
                response = await self.client.post(
                    "chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You draft factual job-application prose. Use only the supplied "
                                    "candidate facts for candidate claims and return one JSON object."
                                ),
                            },
                            {"role": "user", "content": prompt + retry_feedback},
                        ],
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": self.reasoning_effort,
                        "temperature": 0.2 if attempt == 0 else 0,
                    },
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                payload = _parse_json_object(raw)
                return _validate_narrative_payload(
                    payload,
                    available_facts=selected,
                    model=self.model,
                    maximum=maximum,
                )
            except NarrativeCompositionError as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                transient = transient_model_error(exc, operation="narrative request")
                if transient:
                    raise transient from exc
                last_error = NarrativeCompositionError("GLM narrative request failed")
                last_error.__cause__ = exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = NarrativeCompositionError("GLM narrative request failed")
                last_error.__cause__ = exc
        raise last_error or NarrativeCompositionError("GLM narrative request failed")

    async def close(self) -> None:
        await self.client.aclose()


def _select_relevant_facts(
    question: FormQuestion,
    job: JobPosting,
    facts: list[ProfileFact],
    *,
    limit: int = 8,
) -> list[ProfileFact]:
    target_terms = set(
        WORD_RE.findall(f"{question.label} {job.title} {job.description}".casefold())
    )

    def score(fact: ProfileFact) -> tuple[int, int, str]:
        fact_terms = set(
            WORD_RE.findall(
                f"{fact.statement} {' '.join(fact.skills)} {' '.join(fact.metrics)}".casefold()
            )
        )
        overlap = len(target_terms & fact_terms)
        impact_bonus = 2 if fact.metrics else 0
        return overlap, impact_bonus, fact.id

    return sorted(facts, key=score, reverse=True)[:limit]


def _parse_json_object(raw: object) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise NarrativeCompositionError("GLM narrative response was empty")
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NarrativeCompositionError("GLM narrative response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise NarrativeCompositionError("GLM narrative response was not an object")
    return payload


def _validate_narrative_payload(
    payload: dict,
    *,
    available_facts: list[ProfileFact],
    model: str,
    maximum: int | None,
) -> NarrativeAnswer:
    answer = payload.get("answer")
    fact_ids = payload.get("fact_ids")
    if not isinstance(answer, str) or not answer.strip():
        raise NarrativeCompositionError("GLM narrative answer was empty")
    value = answer.strip()
    if (
        not isinstance(fact_ids, list)
        or not fact_ids
        or not all(isinstance(item, str) for item in fact_ids)
    ):
        raise NarrativeCompositionError("GLM narrative answer did not cite profile facts")
    by_id = {fact.id: fact for fact in available_facts}
    unknown = sorted(set(fact_ids) - set(by_id))
    if unknown:
        raise NarrativeCompositionError(
            "GLM narrative cited unknown profile facts: " + ", ".join(unknown)
        )
    if maximum is not None and len(value) > maximum:
        raise NarrativeCompositionError(
            f"GLM narrative exceeded the field limit ({len(value)} > {maximum})"
        )
    cited = [by_id[fact_id] for fact_id in dict.fromkeys(fact_ids)]
    allowed_numbers = {
        token
        for fact in cited
        for text in (fact.statement, *fact.allowed_rewrites, *fact.metrics)
        for token in FactValidator.NUMBER_RE.findall(text)
    }
    unsupported_numbers = sorted(set(FactValidator.NUMBER_RE.findall(value)) - allowed_numbers)
    if unsupported_numbers:
        raise NarrativeCompositionError(
            "GLM narrative introduced unsupported numeric claims: " + ", ".join(unsupported_numbers)
        )
    return NarrativeAnswer(
        value=value,
        fact_ids=tuple(fact.id for fact in cited),
        model=model,
    )
