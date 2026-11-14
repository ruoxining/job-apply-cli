import asyncio

import pytest

from job_apply_agent.ats.codex_planner import CodexCLIPlanner
from job_apply_agent.ats.cua_harness import UnsafeComputerAction
from job_apply_agent.domain.schemas import FormQuestion, JobPosting, ProfileFact
from job_apply_agent.services.codex_narratives import CodexNarrativeComposer
from job_apply_agent.services.narratives import NarrativeCompositionError


async def test_missing_cli_has_normalized_error(tmp_path):
    planner = CodexCLIPlanner(binary=str(tmp_path / "missing"), model="test")
    with pytest.raises(UnsafeComputerAction, match="not installed"):
        await planner.check_ready()
    with pytest.raises(UnsafeComputerAction, match="not installed"):
        await planner.request_json("fixture", {})


async def test_cli_timeout_kills_and_reaps_process(monkeypatch):
    class Process:
        returncode = None
        reaped = False

        async def communicate(self, value):
            await asyncio.sleep(30)

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.reaped = True

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    planner = CodexCLIPlanner(binary="fixture", model="test", timeout_seconds=0.01)
    with pytest.raises(UnsafeComputerAction, match="timed out"):
        await planner.request_json("fixture", {})
    assert process.returncode == -9 and process.reaped


@pytest.mark.parametrize(
    "invalid",
    [
        {"answer": "I improved latency by 70%.", "fact_ids": ["latency"]},
        {"answer": "I improved latency by 35%.", "fact_ids": ["invented"]},
    ],
)
async def test_codex_narrative_retries_invalid_fact_claims(monkeypatch, invalid):
    composer = CodexNarrativeComposer(binary="fixture", model="test", timeout_seconds=1)
    calls = []

    async def request(prompt, schema):
        calls.append(prompt)
        assert schema["required"] == ["answer", "fact_ids"]
        if len(calls) == 1:
            return invalid
        assert "Validation:" in prompt
        return {"answer": "I improved latency by 35%.", "fact_ids": ["latency"]}

    monkeypatch.setattr(composer.planner, "request_json", request)
    answer = await composer.compose(
        question=FormQuestion(field_id="impact", label="Describe a project impact"),
        job=JobPosting(
            source="ashby",
            source_id="1",
            company="Fixture",
            title="Engineer",
            url="https://example.test/job",
            description="Build APIs",
        ),
        facts=[ProfileFact(id="latency", statement="Improved latency by 35%.", source="fixture")],
    )
    assert answer.fact_ids == ("latency",) and answer.model == "test"
    assert len(calls) == 2


async def test_codex_narrative_without_facts_never_calls_model(monkeypatch):
    composer = CodexNarrativeComposer(binary="fixture", model="test", timeout_seconds=1)

    async def forbidden(*args):
        pytest.fail("no facts means no generation")

    monkeypatch.setattr(composer.planner, "request_json", forbidden)
    with pytest.raises(NarrativeCompositionError, match="no verified facts"):
        await composer.compose(
            question=FormQuestion(field_id="impact", label="Describe a project impact"),
            job=JobPosting(
                source="ashby",
                source_id="1",
                company="Fixture",
                title="Engineer",
                url="https://example.test/job",
                description="Build APIs",
            ),
            facts=[],
        )


async def test_json_usage_is_recorded_without_storing_raw_events(monkeypatch):
    import json
    from pathlib import Path

    class Process:
        returncode = 0

        async def communicate(self, value):
            return (
                b'{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":10}}\n',
                b"model: fixture\n",
            )

    async def spawn(*args, **kwargs):
        assert "--json" in args
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps({"ok": True}))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    planner = CodexCLIPlanner(binary="fixture", model="fixture")
    assert await planner.request_json("private candidate", {}) == {"ok": True}
    assert planner.telemetry[0]["usage"][0]["input_tokens"] == 120
    assert "private candidate" not in str(planner.telemetry)
