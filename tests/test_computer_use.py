import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from job_apply_agent.ats.cua_harness import (
    CuaBrowserSession,
    FormReadiness,
    GLMActionPlanner,
    PlannerStopped,
    UnsafeComputerAction,
    _is_final_submit_ref,
    fill_application_with_driver,
    find_linux_browser_pid,
    parse_action,
    scroll_amount,
)
from job_apply_agent.services.model_errors import TransientModelError


def result(payload=None, *, error_code=None, text="ok"):
    return SimpleNamespace(
        structured_json=json.dumps(payload or {}),
        is_error=error_code is not None,
        error_code=error_code,
        text=text,
    )


def refusal(code, text="refused"):
    return SimpleNamespace(
        structured_json=json.dumps(
            {"status": "refused", "refusal": {"code": code, "message": text}}
        ),
        is_error=False,
        error_code=code,
        text=text,
    )


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.shutdown_calls = 0
        self.snapshots = [
            {
                "status": "ok",
                "page": {"url": "https://jobs.example.test/apply", "title": "Apply"},
                "outline": "textbox Full name",
                "refs": [
                    {
                        "ref": "p1:1",
                        "role": "textbox",
                        "name": "Full name",
                        "value": "",
                        "actions": ["type"],
                    }
                ],
            },
            {
                "status": "ok",
                "page": {"url": "https://jobs.example.test/apply", "title": "Apply"},
                "outline": "textbox Full name Alex Example",
                "refs": [
                    {
                        "ref": "p2:1",
                        "role": "textbox",
                        "name": "Full name",
                        "value": "Alex Example",
                        "actions": ["type"],
                    }
                ],
            },
        ]

    async def call_tool(self, name, arguments_json):
        arguments = json.loads(arguments_json)
        self.calls.append((name, arguments))
        if name == "start_session":
            return result({"session": arguments["session"], "active": True})
        if name == "list_apps":
            return result({"apps": [{"pid": 42, "name": "Microsoft Edge", "running": True}]})
        if name == "list_windows":
            return result({"windows": [{"pid": 42, "window_id": 7}]})
        if name == "get_browser_state" and "pid" in arguments:
            return result(
                {
                    "status": "ok",
                    "binding_quality": "exact",
                    "mutation_allowed": True,
                    "target_id": "target-1",
                    "tabs": [
                        {
                            "tab_id": "tab-1",
                            "url": "https://jobs.example.test/apply",
                            "active": True,
                        }
                    ],
                }
            )
        if name == "get_browser_state":
            return result(self.snapshots.pop(0))
        return result({"status": "ok"})

    async def shutdown(self):
        self.shutdown_calls += 1


def test_action_parser_accepts_json_and_fences():
    expected = {"action": "fill", "ref": "p1:1", "value": "Toronto", "reason": None}
    assert parse_action(json.dumps(expected)) == expected
    assert parse_action(f"```json\n{json.dumps(expected)}\n```") == expected


def test_action_parser_normalizes_safe_planner_aliases():
    assert parse_action('{"action":"type","ref":"p1:1","value":"Toronto"}') == {
        "action": "fill",
        "ref": "p1:1",
        "value": "Toronto",
    }
    assert parse_action('{"action":"done"}') == {"action": "finish"}


def test_action_parser_rejects_missing_ref_and_value():
    with pytest.raises(UnsafeComputerAction, match="element ref"):
        parse_action('{"action":"click","ref":null}')
    with pytest.raises(UnsafeComputerAction, match="text value"):
        parse_action('{"action":"fill","ref":"p1:1","value":null}')
    with pytest.raises(UnsafeComputerAction, match="unsupported action name: 'select'"):
        parse_action('{"action":"select","ref":"p1:1"}')


def test_scroll_amount_accepts_numbers_and_direction_words():
    assert scroll_amount(1200) == 1200
    assert scroll_amount("down") == 600
    assert scroll_amount("up") == -600
    with pytest.raises(UnsafeComputerAction):
        scroll_amount("sideways")


def test_submit_controls_are_rejected_but_entry_gate_is_allowed():
    assert _is_final_submit_ref({"role": "button", "name": "Submit application"})
    assert _is_final_submit_ref({"role": "button", "name": "Apply now"})
    assert not _is_final_submit_ref({"role": "button", "name": "Apply for this job"})
    assert not _is_final_submit_ref({"role": "textbox", "name": "Submit"})


async def test_glm_planner_calls_compatible_api_and_parses_json_action():
    async def handler(request):
        assert request.url.path == "/api/paas/v4/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["model"] == "glm-test"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "finish",
                                    "ref": None,
                                    "value": None,
                                    "reason": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    planner = GLMActionPlanner(
        base_url="https://glm.example.test/api/paas/v4",
        api_key="secret",
        model="glm-test",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        action = await planner.next_action("Form is complete")
    finally:
        await planner.close()
    assert action["action"] == "finish"


async def test_glm_planner_exposes_rate_limit_as_cloud_retryable():
    async def handler(request):
        del request
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            json={"error": {"message": "quota exceeded"}},
        )

    planner = GLMActionPlanner(
        base_url="https://glm.example.test/api/paas/v4",
        api_key="secret",
        model="glm-test",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TransientModelError) as error:
            await planner.next_action("Form is complete")
    finally:
        await planner.close()
    assert error.value.reason_code == "glm_quota_or_rate_limit"
    assert error.value.retry_after_seconds == 120


async def test_harness_binds_driver_and_executes_driver_type_action():
    class SequencePlanner:
        def __init__(self):
            self.calls = 0

        async def next_action(self, prompt):
            self.calls += 1
            if self.calls == 1:
                assert "Full name" in prompt
                return {
                    "action": "fill",
                    "ref": "p1:1",
                    "value": "Alex Example",
                    "reason": None,
                }
            return {"action": "finish", "ref": None, "value": None, "reason": None}

    driver = FakeDriver()
    browser = await CuaBrowserSession.attach(
        expected_url="https://jobs.example.test/apply",
        application_id="job-1",
        timeout_seconds=1,
        input_route="dom_event",
        driver=driver,
    )
    run = await fill_application_with_driver(
        browser,
        planner=SequencePlanner(),
        max_steps=3,
        candidate_instructions="Use approved facts only.",
        answer_lines=["full_name: Alex Example"],
        approved_fill_values=["Alex Example"],
    )
    await browser.close()

    type_calls = [arguments for name, arguments in driver.calls if name == "browser_type"]
    assert type_calls == [
        {
            "target_id": "target-1",
            "tab_id": "tab-1",
            "session": "job-apply-job-1",
            "ref": "p1:1",
            "text": "Alex Example",
            "replace": True,
        }
    ]
    assert run.output_text == "form ready"
    assert driver.shutdown_calls == 1


async def test_harness_honors_structured_refusal_when_sdk_is_error_is_false():
    class ConsentDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.prepared = False

        async def call_tool(self, name, arguments_json):
            arguments = json.loads(arguments_json)
            if name == "get_browser_state" and "pid" in arguments and not self.prepared:
                self.calls.append((name, arguments))
                return refusal("browser_consent_required")
            if name == "browser_prepare":
                self.calls.append((name, arguments))
                self.prepared = True
                return result({"prepared": True})
            return await super().call_tool(name, arguments_json)

    driver = ConsentDriver()
    browser = await CuaBrowserSession.attach(
        expected_url="https://jobs.example.test/apply",
        application_id="job-consent",
        timeout_seconds=1,
        input_route="dom_event",
        driver=driver,
    )
    await browser.close()

    assert any(name == "browser_prepare" for name, _ in driver.calls)


async def test_harness_bounds_a_hung_native_attach_call():
    class HangingDriver:
        def __init__(self):
            self.shutdown_calls = 0

        async def call_tool(self, _name, _arguments_json):
            await asyncio.Event().wait()

        async def shutdown(self):
            self.shutdown_calls += 1

    driver = HangingDriver()
    with pytest.raises(UnsafeComputerAction, match="attach timed out"):
        await CuaBrowserSession.attach(
            expected_url="https://jobs.example.test/apply",
            application_id="job-timeout",
            timeout_seconds=0.01,
            input_route="dom_event",
            driver=driver,
        )
    assert driver.shutdown_calls == 1


def test_find_linux_browser_pid_requires_one_exact_profile_process(tmp_path):
    executable = tmp_path / "chrome"
    executable.touch()
    profile = tmp_path / "profile"
    profile.mkdir()
    proc_root = tmp_path / "proc"
    root_process = proc_root / "42"
    root_process.mkdir(parents=True)
    os.symlink(executable, root_process / "exe")
    (root_process / "cmdline").write_bytes(
        bytes(executable) + b"\0--user-data-dir=" + bytes(profile) + b"\0"
    )
    renderer = proc_root / "43"
    renderer.mkdir()
    os.symlink(executable, renderer / "exe")
    (renderer / "cmdline").write_bytes(
        bytes(executable) + b"\0--type=renderer\0--user-data-dir=" + bytes(profile) + b"\0"
    )

    assert (
        find_linux_browser_pid(
            executable=executable,
            profile_dir=profile,
            proc_root=proc_root,
        )
        == 42
    )


async def test_harness_blocks_final_submit_before_calling_driver():
    driver = FakeDriver()
    browser = CuaBrowserSession(
        driver=driver,
        runtime_manifest_path=None,
        session="job-apply-test",
        initial_host="jobs.example.test",
        pid=42,
        window_id=7,
        target_id="target-1",
        tab_id="tab-1",
        input_route="dom_event",
    )
    observation = {
        "refs": [
            {
                "ref": "p1:9",
                "role": "button",
                "name": "Submit application",
                "actions": ["click"],
            }
        ]
    }
    with pytest.raises(UnsafeComputerAction, match="final submit"):
        await browser.execute(
            {"action": "click", "ref": "p1:9", "value": None, "reason": None},
            observation,
        )
    assert all(name != "browser_click" for name, _ in driver.calls)


async def test_harness_uses_deterministic_readiness_without_calling_planner():
    class UnusedBrowser:
        async def observe(self):
            raise AssertionError("ready form must not be observed by the planner")

    class UnusedPlanner:
        async def next_action(self, _prompt):
            raise AssertionError("ready form must not call the planner")

    async def ready():
        return FormReadiness(ready=True)

    run = await fill_application_with_driver(
        UnusedBrowser(),
        planner=UnusedPlanner(),
        max_steps=3,
        candidate_instructions="",
        answer_lines=[],
        approved_fill_values=[],
        readiness_check=ready,
    )

    assert run.output_text == "form ready"


async def test_harness_stops_repeated_action_and_redacts_fill_value_from_trace():
    secret = "candidate-secret-answer"

    class RerenderingBrowser:
        def __init__(self):
            self.observations = 0

        async def observe(self):
            self.observations += 1
            ref = f"p{self.observations}:{self.observations * 10}"
            return {
                "page": {"url": "https://jobs.example.test/apply", "title": "Apply"},
                "outline": f"{ref} textbox Additional information",
                "refs": [
                    {
                        "ref": ref,
                        "role": "textbox",
                        "name": "Additional information",
                        "value": "" if self.observations == 1 else "first attempt",
                        "actions": ["type"],
                    }
                ],
            }

        async def execute(self, _action, _observation):
            return False

    class RepeatingPlanner:
        def __init__(self):
            self.calls = 0

        async def next_action(self, _prompt):
            self.calls += 1
            ref = f"p{self.calls}:{self.calls * 10}"
            return {"action": "fill", "ref": ref, "value": secret, "reason": None}

    async def not_ready():
        return FormReadiness(False, ("required field is empty: additional",))

    with pytest.raises(PlannerStopped) as stopped:
        await fill_application_with_driver(
            RerenderingBrowser(),
            planner=RepeatingPlanner(),
            max_steps=5,
            candidate_instructions="",
            answer_lines=[],
            approved_fill_values=[secret],
            readiness_check=not_ready,
        )

    assert stopped.value.reason_code == "planner_repeated_action"
    assert [item["outcome"] for item in stopped.value.trace] == [
        "executed",
        "blocked_repetition",
    ]
    assert secret not in json.dumps(stopped.value.trace)


async def test_harness_reports_step_budget_with_bounded_trace():
    class ChangingBrowser:
        def __init__(self):
            self.calls = 0

        async def observe(self):
            self.calls += 1
            return {
                "page": {"url": "https://jobs.example.test/apply", "title": "Apply"},
                "outline": f"page {self.calls}",
                "refs": [],
            }

        async def execute(self, _action, _observation):
            return False

    class ScrollingPlanner:
        async def next_action(self, _prompt):
            return {"action": "scroll", "ref": None, "value": 600, "reason": None}

    with pytest.raises(PlannerStopped) as stopped:
        await fill_application_with_driver(
            ChangingBrowser(),
            planner=ScrollingPlanner(),
            max_steps=2,
            candidate_instructions="",
            answer_lines=[],
            approved_fill_values=[],
        )

    assert stopped.value.reason_code == "planner_step_budget_exhausted"
    assert len(stopped.value.trace) == 2


async def test_harness_blocks_fill_value_outside_approved_answer_bank():
    class Browser:
        executed = False

        async def observe(self):
            return {
                "page": {"url": "https://jobs.example.test/apply", "title": "Apply"},
                "outline": "textbox Impact example",
                "refs": [
                    {
                        "ref": "p1:1",
                        "role": "textbox",
                        "name": "Impact example",
                        "value": "",
                        "actions": ["type"],
                    }
                ],
            }

        async def execute(self, _action, _observation):
            self.executed = True
            return False

    class InventingPlanner:
        async def next_action(self, _prompt):
            return {
                "action": "fill",
                "ref": "p1:1",
                "value": "invented candidate claim",
                "reason": None,
            }

    browser = Browser()
    with pytest.raises(PlannerStopped) as stopped:
        await fill_application_with_driver(
            browser,
            planner=InventingPlanner(),
            max_steps=2,
            candidate_instructions="",
            answer_lines=["impact_example: approved answer"],
            approved_fill_values=["approved answer"],
        )

    assert stopped.value.reason_code == "planner_unapproved_fill_value"
    assert stopped.value.trace[0]["outcome"] == "blocked_unapproved_fill"
    assert "invented candidate claim" not in json.dumps(stopped.value.trace)
    assert browser.executed is False
