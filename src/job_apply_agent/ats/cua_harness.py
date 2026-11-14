from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from job_apply_agent.services.model_errors import transient_model_error

log = logging.getLogger(__name__)


class UnsafeComputerAction(RuntimeError):
    """A model or driver action crossed a safety or reliability boundary."""


class PlannerStopped(UnsafeComputerAction):
    """The planner stopped at a bounded, diagnosable safety limit."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        trace: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.trace = trace


@dataclass(frozen=True)
class CUARunResult:
    response_id: str
    output_text: str


@dataclass(frozen=True)
class FormReadiness:
    ready: bool
    issues: tuple[str, ...] = ()
    recovery_feedback: tuple[dict, ...] = ()
    answer_bindings: tuple[dict, ...] | None = None
    missing_questions: tuple[dict, ...] = ()


ACTION_NAMES = {"click", "fill", "scroll", "finish", "stop"}
ACTION_ALIASES = {"type": "fill", "done": "finish", "complete": "finish"}
MAX_UNCHANGED_STATES = 3
MAX_IDENTICAL_STATE_ACTIONS = 2


class ActionPlanner(Protocol):
    async def next_action(self, prompt: str) -> dict[str, Any]: ...


class DriverClient(Protocol):
    async def call_tool(self, name: str, arguments_json: str) -> Any: ...

    async def shutdown(self) -> None: ...


class GLMActionPlanner:
    """Small JSON-action planner backed by GLM's chat-completions endpoint."""

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

    async def next_action(self, prompt: str) -> dict[str, Any]:
        try:
            response = await self.client.post(
                "chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Return one JSON object only. Never invent candidate facts, "
                                "never submit an application, and stop when human input is needed."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": self.reasoning_effort,
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except httpx.HTTPError as exc:
            transient = transient_model_error(exc, operation="planner request")
            if transient:
                raise transient from exc
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            suffix = f" with HTTP {status}" if status else ""
            raise UnsafeComputerAction(f"GLM planner request failed{suffix}") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise UnsafeComputerAction("GLM planner returned an unusable response") from exc
        if not isinstance(content, str) or not content.strip():
            raise UnsafeComputerAction("GLM planner returned an empty action")
        return parse_action(content)

    async def close(self) -> None:
        await self.client.aclose()


def parse_action(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        action = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnsafeComputerAction("planner returned invalid JSON") from exc
    if not isinstance(action, dict):
        raise UnsafeComputerAction("planner returned a non-object action")
    action_name = action.get("action")
    if action_name in ACTION_ALIASES:
        action = {**action, "action": ACTION_ALIASES[action_name]}
    if action.get("action") not in ACTION_NAMES:
        safe_name = _clean_trace_text(action_name, limit=40)
        raise UnsafeComputerAction(f"planner returned unsupported action name: {safe_name!r}")
    if action["action"] in {"click", "fill"} and not isinstance(action.get("ref"), str):
        raise UnsafeComputerAction(f"planner {action['action']} action has no element ref")
    if action["action"] == "fill" and not isinstance(action.get("value"), str):
        raise UnsafeComputerAction("planner fill action has no text value")
    return action


def scroll_amount(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 600
    if isinstance(value, (int, float)):
        return int(value) or 600
    text = str(value).strip().casefold()
    if text in {"up", "upward"}:
        return -600
    if text in {"down", "downward"}:
        return 600
    try:
        return int(float(text)) or 600
    except ValueError as exc:
        raise UnsafeComputerAction(f"unusable scroll amount: {value!r}") from exc


def _observation_fingerprint(observation: dict[str, Any]) -> str:
    """Hash semantic state without retaining page or candidate contents."""
    page = observation.get("page", {})
    parsed_url = urlparse(str(page.get("url", "")))
    outline = re.sub(r"\bp\d+:\d+\b", "<ref>", str(observation.get("outline", "")))
    stable_refs = [
        {key: value for key, value in item.items() if key != "ref"}
        for item in observation.get("refs", [])
        if isinstance(item, dict)
    ]
    state = {
        "page": {
            "origin": f"{parsed_url.scheme}://{parsed_url.netloc}",
            "path": parsed_url.path,
            "title": page.get("title"),
        },
        "outline": outline,
        "refs": stable_refs,
    }
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _clean_trace_text(value: Any, *, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _action_trace(
    *,
    step: int,
    state_fingerprint: str,
    action: dict[str, Any],
    observation: dict[str, Any],
    outcome: str,
    issues: tuple[str, ...] = (),
) -> dict[str, Any]:
    ref = action.get("ref")
    element = next(
        (item for item in observation.get("refs", []) if item.get("ref") == ref),
        {},
    )
    trace: dict[str, Any] = {
        "step": step,
        "state": state_fingerprint,
        "action": action["action"],
        "ref": ref if isinstance(ref, str) else None,
        "target_role": _clean_trace_text(element.get("role")),
        "target_name": _clean_trace_text(element.get("name")),
        "outcome": outcome,
    }
    if issues:
        trace["issues"] = [_clean_trace_text(issue) for issue in issues[:5]]
    return trace


def _action_signature(action: dict[str, Any], observation: dict[str, Any]) -> str:
    ref = action.get("ref")
    refs = observation.get("refs", [])
    target_index = next(
        (index for index, item in enumerate(refs) if item.get("ref") == ref),
        None,
    )
    element = refs[target_index] if target_index is not None else {}
    signature = {
        "action": action["action"],
        "target_index": target_index,
        "role": element.get("role"),
        "name": element.get("name"),
        "value_digest": hashlib.sha256(str(action.get("value", "")).encode()).hexdigest()[:16],
        "scroll": scroll_amount(action.get("value")) if action["action"] == "scroll" else None,
    }
    encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _log_planner_step(trace: dict[str, Any]) -> None:
    log.info(
        "cua_planner_step step=%s state=%s action=%s ref=%s target_role=%s "
        "target_name=%s outcome=%s issues=%s",
        trace["step"],
        trace["state"],
        trace["action"],
        trace["ref"],
        trace["target_role"],
        trace["target_name"],
        trace["outcome"],
        trace.get("issues", []),
    )


def create_cua_driver(*, origin: str, browser_executable: Path) -> tuple[DriverClient, Path]:
    """Create a browser-only bounded runtime for one executable and origin."""
    from cua_driver import (
        ConfiguredDriverOptions,
        CuaDriver,
        RuntimeAuthorizationOptions,
        SessionPermissionMode,
    )

    manifest = {
        "version": 3,
        "expires_after": "2h",
        "idle_timeout": "20m",
        "resources": {
            "apps": [
                {
                    "executable": str(browser_executable.resolve()),
                    "launch": False,
                    "windows": "all",
                    "terminate": "deny",
                }
            ],
            "browser": {
                "profiles": [{"kind": "existing_profile"}],
                "origins": [origin],
            },
            "desktop": {"display": False},
        },
        "allow": {
            "tools": [
                "start_session",
                "end_session",
                "list_windows",
                "get_browser_state",
                "browser_prepare",
                "browser_click",
                "browser_type",
                "browser_pointer",
            ]
        },
    }
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="job-apply-cua-", suffix=".json", delete=False
    ) as manifest_file:
        json.dump(manifest, manifest_file)
        manifest_path = Path(manifest_file.name)

    options = ConfiguredDriverOptions(
        claude_code_compatibility=False,
        authorization=RuntimeAuthorizationOptions(
            allowed_modes=[SessionPermissionMode.BOUNDED],
            compatibility_mode=SessionPermissionMode.BOUNDED,
            compatibility_capability_manifest_path=str(manifest_path),
            compatibility_bounded_manifest_path=None,
            unrestricted_acknowledged=False,
            max_session_ttl_seconds=3600,
            max_idle_ttl_seconds=900,
        ),
    )
    try:
        driver = CuaDriver.create_configured(options)
    except Exception:
        manifest_path.unlink(missing_ok=True)
        raise
    return driver, manifest_path


def find_linux_browser_pid(
    *, executable: Path, profile_dir: Path, proc_root: Path = Path("/proc")
) -> int:
    """Find the one root Chromium process for this worker-owned profile."""
    expected_executable = str(executable.resolve())
    profile_flag = f"--user-data-dir={profile_dir.resolve()}".encode()
    matches: list[int] = []
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            actual_executable = os.readlink(process_dir / "exe")
            command_line = (process_dir / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if (
            actual_executable == expected_executable
            and profile_flag in command_line
            and b"--type=" not in command_line
        ):
            matches.append(int(process_dir.name))
    if len(matches) != 1:
        raise UnsafeComputerAction(
            f"expected one Chromium root process for the worker profile, found {len(matches)}"
        )
    return matches[0]


class CuaBrowserSession:
    """Bind Cua Driver to the exact browser tab owned by this worker run."""

    BROWSER_NAMES = ("edge", "chrome", "chromium", "brave")

    def __init__(
        self,
        *,
        driver: DriverClient,
        runtime_manifest_path: Path | None,
        session: str,
        initial_host: str,
        pid: int,
        window_id: int,
        target_id: str,
        tab_id: str,
        input_route: str,
        step_timeout_seconds: int = 120,
    ) -> None:
        self.driver = driver
        self.runtime_manifest_path = runtime_manifest_path
        self.session = session
        self.initial_host = initial_host.casefold()
        self.pid = pid
        self.window_id = window_id
        self.target_id = target_id
        self.tab_id = tab_id
        self.input_route = input_route
        self.step_timeout_seconds = step_timeout_seconds

    @classmethod
    async def attach(
        cls,
        *,
        expected_url: str,
        application_id: str,
        timeout_seconds: int,
        input_route: str,
        step_timeout_seconds: int = 120,
        browser_pid: int | None = None,
        browser_executable: Path | None = None,
        driver: DriverClient | None = None,
    ) -> CuaBrowserSession:
        parsed_url = urlparse(expected_url)
        expected_host = (parsed_url.hostname or "").casefold()
        if not expected_host:
            raise UnsafeComputerAction("application page has no hostname")
        runtime_manifest_path: Path | None = None
        if driver is None:
            if browser_pid is None or browser_executable is None:
                raise UnsafeComputerAction(
                    "bounded CUA Driver startup requires the exact browser process"
                )
            try:
                driver, runtime_manifest_path = create_cua_driver(
                    origin=f"{parsed_url.scheme}://{parsed_url.netloc}",
                    browser_executable=browser_executable,
                )
            except Exception as exc:
                raise UnsafeComputerAction("CUA Driver failed to start") from exc
        session = f"job-apply-{application_id[:24]}"
        last_error = "browser window was not discovered"
        try:
            async with asyncio.timeout(timeout_seconds):
                await _call_tool(driver, "start_session", {"session": session})
                while True:
                    if browser_pid is None:
                        apps = await _call_tool(driver, "list_apps", {})
                        candidates = [
                            app
                            for app in apps.get("apps", [])
                            if app.get("running")
                            and any(
                                name in str(app.get("name", "")).casefold()
                                for name in cls.BROWSER_NAMES
                            )
                        ]
                    else:
                        candidates = [{"pid": browser_pid}]
                    for app in candidates:
                        pid = int(app["pid"])
                        windows = await _call_tool(
                            driver, "list_windows", {"pid": pid, "on_screen_only": False}
                        )
                        for window in windows.get("windows", []):
                            window_id = int(window["window_id"])
                            bind, error_code, detail = await _try_call_tool(
                                driver,
                                "get_browser_state",
                                {"pid": pid, "window_id": window_id, "session": session},
                            )
                            if error_code in {
                                "browser_requires_setup",
                                "browser_consent_required",
                            }:
                                _, prepare_error, prepare_detail = await _try_call_tool(
                                    driver,
                                    "browser_prepare",
                                    {
                                        "pid": pid,
                                        "window_id": window_id,
                                        "session": session,
                                        "strategy": {"kind": "existing_profile"},
                                    },
                                )
                                if prepare_error:
                                    last_error = f"{prepare_error}: {prepare_detail}"
                                    continue
                                bind, error_code, detail = await _try_call_tool(
                                    driver,
                                    "get_browser_state",
                                    {"pid": pid, "window_id": window_id, "session": session},
                                )
                            if error_code:
                                last_error = f"{error_code}: {detail}"
                                continue
                            if bind.get("binding_quality") != "exact" or not bind.get(
                                "mutation_allowed"
                            ):
                                last_error = "CUA Driver browser binding is not exact"
                                continue
                            tab = next(
                                (
                                    item
                                    for item in bind.get("tabs", [])
                                    if (
                                        urlparse(str(item.get("url", ""))).hostname or ""
                                    ).casefold()
                                    == expected_host
                                ),
                                None,
                            )
                            if tab is None:
                                continue
                            return cls(
                                driver=driver,
                                runtime_manifest_path=runtime_manifest_path,
                                session=session,
                                initial_host=expected_host,
                                pid=pid,
                                window_id=window_id,
                                target_id=str(bind["target_id"]),
                                tab_id=str(tab["tab_id"]),
                                input_route=input_route,
                                step_timeout_seconds=step_timeout_seconds,
                            )
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            await _cleanup_driver(driver, runtime_manifest_path)
            raise UnsafeComputerAction(
                f"CUA Driver browser attach timed out: {last_error}"
            ) from exc
        except Exception:
            await _cleanup_driver(driver, runtime_manifest_path)
            raise

    async def observe(self) -> dict[str, Any]:
        state = await _call_tool(
            self.driver,
            "get_browser_state",
            {
                "target_id": self.target_id,
                "tab_id": self.tab_id,
                "session": self.session,
                "snapshot_format": "semantic_v2",
                "include_screenshot": False,
            },
            timeout_seconds=self.step_timeout_seconds,
        )
        host = (urlparse(str(state.get("page", {}).get("url", ""))).hostname or "").casefold()
        if host != self.initial_host:
            raise UnsafeComputerAction(f"CUA Driver navigated outside the initial ATS host: {host}")
        if not isinstance(state.get("refs"), list) or not isinstance(state.get("outline"), str):
            raise UnsafeComputerAction("CUA Driver returned an incomplete browser snapshot")
        return state

    async def execute(self, action: dict[str, Any], observation: dict[str, Any]) -> bool:
        kind = action["action"]
        if kind == "finish":
            return True
        if kind == "stop":
            raise UnsafeComputerAction(action.get("reason") or "planner requested human review")
        if kind == "scroll":
            ref = action.get("ref")
            target = next((item for item in observation["refs"] if item.get("ref") == ref), None)
            if target is None or "pointer" not in set(target.get("actions") or []):
                raise UnsafeComputerAction(
                    "Driver scrolling requires a current pointer-capable ref"
                )
            await _call_tool(
                self.driver,
                "browser_pointer",
                {
                    "target_id": self.target_id,
                    "tab_id": self.tab_id,
                    "session": self.session,
                    "action": "scroll",
                    "ref": ref,
                    "delta_x": 0,
                    "delta_y": scroll_amount(action.get("value")),
                    "input_route": self.input_route,
                },
                timeout_seconds=self.step_timeout_seconds,
            )
            return False

        ref = action["ref"]
        element = next((item for item in observation["refs"] if item.get("ref") == ref), None)
        if element is None:
            raise UnsafeComputerAction(f"planner referenced missing CUA Driver element: {ref}")
        available_actions = set(element.get("actions") or [])
        if kind == "click":
            if "click" not in available_actions:
                raise UnsafeComputerAction(f"CUA Driver ref does not support click: {ref}")
            if _is_final_submit_ref(element):
                raise UnsafeComputerAction("planner attempted to use a final submit control")
            await _call_tool(
                self.driver,
                "browser_click",
                {
                    "target_id": self.target_id,
                    "tab_id": self.tab_id,
                    "session": self.session,
                    "ref": ref,
                    "input_route": self.input_route,
                },
                timeout_seconds=self.step_timeout_seconds,
            )
        elif kind == "fill":
            if "type" not in available_actions:
                raise UnsafeComputerAction(f"CUA Driver ref does not support typing: {ref}")
            await _call_tool(
                self.driver,
                "browser_type",
                {
                    "target_id": self.target_id,
                    "tab_id": self.tab_id,
                    "session": self.session,
                    "ref": ref,
                    "text": action["value"],
                    "replace": True,
                },
                timeout_seconds=self.step_timeout_seconds,
            )
        else:
            raise UnsafeComputerAction(f"unsupported planner action: {kind}")
        return False

    async def close(self) -> None:
        try:
            await _try_call_tool(
                self.driver,
                "end_session",
                {"session": self.session},
                timeout_seconds=self.step_timeout_seconds,
            )
        finally:
            await _cleanup_driver(self.driver, self.runtime_manifest_path)


def _is_final_submit_ref(element: dict[str, Any]) -> bool:
    name = str(element.get("name", "")).casefold()
    role = str(element.get("role", "")).casefold()
    if role not in {"button", "link"}:
        return False
    return bool(re.search(r"\bsubmit\b|send application|apply now|complete application", name))


async def _try_call_tool(
    driver: DriverClient,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], str | None, str]:
    try:
        call = driver.call_tool(name, json.dumps(arguments))
        result = (
            await call
            if timeout_seconds is None
            else await asyncio.wait_for(call, timeout=timeout_seconds)
        )
    except TimeoutError:
        return {}, "driver_call_timed_out", f"{name} exceeded {timeout_seconds} seconds"
    except Exception as exc:  # noqa: BLE001 - normalize native SDK failures at the boundary
        return {}, "driver_call_failed", str(exc)
    payload: dict[str, Any] = {}
    structured = getattr(result, "structured_json", None)
    if structured:
        try:
            decoded = json.loads(structured)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            return {}, "invalid_driver_response", "structured_json is not valid JSON"
    reported_error = getattr(result, "error_code", None)
    refusal = payload.get("refusal")
    refusal_code = refusal.get("code") if isinstance(refusal, dict) else None
    if getattr(result, "is_error", False) or reported_error or refusal_code:
        return (
            payload,
            str(reported_error or refusal_code or payload.get("code") or "driver_error"),
            str(getattr(result, "text", "") or payload),
        )
    return payload, None, str(getattr(result, "text", ""))


async def _call_tool(
    driver: DriverClient,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    payload, error_code, detail = await _try_call_tool(
        driver, name, arguments, timeout_seconds=timeout_seconds
    )
    if error_code:
        raise UnsafeComputerAction(f"CUA Driver {name} failed ({error_code}): {detail}")
    return payload


async def _cleanup_driver(driver: DriverClient, manifest_path: Path | None) -> None:
    try:
        await asyncio.wait_for(driver.shutdown(), timeout=5)
    except Exception:
        log.debug("CUA Driver shutdown failed during cleanup", exc_info=True)
    finally:
        if manifest_path is not None:
            await asyncio.to_thread(manifest_path.unlink, missing_ok=True)


async def fill_application_with_driver(
    browser: CuaBrowserSession,
    *,
    planner: ActionPlanner,
    max_steps: int,
    candidate_instructions: str,
    answer_lines: list[str],
    approved_fill_values: list[str],
    readiness_check: Callable[[], Awaitable[FormReadiness]] | None = None,
) -> CUARunResult:
    trace: list[dict[str, Any]] = []
    previous_state: str | None = None
    unchanged_states = 0
    state_action_counts: dict[tuple[str, str], int] = {}
    verification_feedback: tuple[str, ...] = ()
    previous_fill_signature: str | None = None
    approved_values = {value.strip() for value in approved_fill_values if value.strip()}

    for step in range(max_steps):
        if readiness_check is not None:
            readiness = await readiness_check()
            if readiness.ready:
                return CUARunResult(
                    response_id=f"glm:{type(planner).__name__}", output_text="form ready"
                )
            verification_feedback = readiness.issues

        observation = await browser.observe()
        state_fingerprint = _observation_fingerprint(observation)
        if state_fingerprint == previous_state:
            unchanged_states += 1
        else:
            unchanged_states = 1
        previous_state = state_fingerprint
        if unchanged_states >= MAX_UNCHANGED_STATES:
            raise PlannerStopped(
                "GLM planner made no observable progress across three consecutive states",
                reason_code="planner_no_progress",
                trace=trace,
            )

        prompt = f"""Fill one job application by choosing exactly one JSON action.
Allowed actions:
- click: {{"action":"click","ref":"p1:2","value":null,"reason":null}}
- fill: {{"action":"fill","ref":"p1:3","value":"text","reason":null}}
- scroll: {{"action":"scroll","ref":"a current pointer-capable ref","value":600,"reason":null}}
- finish or stop, with null ref/value and a concise reason for stop.

Use only refs whose actions include the requested operation. Every fill value must
exactly equal one value in the Approved answer bank; otherwise stop. An entry button such as
"Apply for this job" may be opened. Never click final Submit, Apply now, Send
application, or Complete application controls; the host verifies and submits later.
Never invent facts. Stop for CAPTCHA, login, password, OTP, 2FA, sensitive questions
without an approved answer, or any missing candidate fact. Finish only when the form is
complete and ready for deterministic verification.

Candidate rules:
{candidate_instructions or "(none)"}

Approved answer bank:
{chr(10).join(answer_lines) if answer_lines else "(empty)"}

Step: {step + 1}/{max_steps}
Deterministic verification issues:
{json.dumps(verification_feedback, ensure_ascii=False)}
Page: {json.dumps(observation.get("page", {}), ensure_ascii=False)}
Visible semantic outline:
{observation["outline"]}
Action refs:
{json.dumps(observation["refs"], ensure_ascii=False)}
"""
        action = await planner.next_action(prompt)
        signature = _action_signature(action, observation)
        if action["action"] == "fill" and action["value"].strip() not in approved_values:
            item = _action_trace(
                step=step + 1,
                state_fingerprint=state_fingerprint,
                action=action,
                observation=observation,
                outcome="blocked_unapproved_fill",
                issues=verification_feedback,
            )
            trace.append(item)
            _log_planner_step(item)
            raise PlannerStopped(
                "GLM planner attempted to fill a value outside the approved Answer Bank",
                reason_code="planner_unapproved_fill_value",
                trace=trace,
            )
        repeated_fill = action["action"] == "fill" and signature == previous_fill_signature
        previous_fill_signature = signature if action["action"] == "fill" else None
        state_action = (state_fingerprint, signature)
        state_action_counts[state_action] = state_action_counts.get(state_action, 0) + 1
        if repeated_fill or state_action_counts[state_action] >= MAX_IDENTICAL_STATE_ACTIONS:
            item = _action_trace(
                step=step + 1,
                state_fingerprint=state_fingerprint,
                action=action,
                observation=observation,
                outcome="blocked_repetition",
                issues=verification_feedback,
            )
            trace.append(item)
            _log_planner_step(item)
            raise PlannerStopped(
                "GLM planner repeated the same action without observable progress",
                reason_code="planner_repeated_action",
                trace=trace,
            )

        if action["action"] == "finish" and readiness_check is not None:
            item = _action_trace(
                step=step + 1,
                state_fingerprint=state_fingerprint,
                action=action,
                observation=observation,
                outcome="finish_rejected",
                issues=verification_feedback,
            )
            trace.append(item)
            _log_planner_step(item)
            continue

        try:
            finished = await browser.execute(action, observation)
        except UnsafeComputerAction:
            raise
        except Exception as exc:
            raise UnsafeComputerAction(f"CUA Driver action failed: {exc}") from exc
        item = _action_trace(
            step=step + 1,
            state_fingerprint=state_fingerprint,
            action=action,
            observation=observation,
            outcome="executed",
        )
        trace.append(item)
        _log_planner_step(item)
        if finished:
            return CUARunResult(
                response_id=f"glm:{type(planner).__name__}", output_text="form ready"
            )
        if readiness_check is not None:
            readiness = await readiness_check()
            if readiness.ready:
                return CUARunResult(
                    response_id=f"glm:{type(planner).__name__}", output_text="form ready"
                )
            verification_feedback = readiness.issues
    raise PlannerStopped(
        f"GLM planner exhausted its {max_steps}-step budget before deterministic verification",
        reason_code="planner_step_budget_exhausted",
        trace=trace,
    )
