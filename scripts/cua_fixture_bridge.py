"""Synthetic-only CUA Driver baseline; no production queue or browser profile access."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from job_apply_agent.ats.cua_harness import (
    CuaBrowserSession,
    _call_tool,
    find_linux_browser_pid,
)


async def main() -> None:
    browser = None
    state = None
    refs = {}
    values = set()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            try:
                action_result = None
                if request["op"] == "close":
                    break
                if request["op"] == "init":
                    origin = urlparse(request["origin"])
                    if origin.scheme != "http" or origin.hostname != "127.0.0.1":
                        raise ValueError("Only loopback synthetic fixtures are allowed")
                    values = set(request["values"])
                    executable = Path("/opt/google/chrome/chrome")
                    for attempt in range(20):
                        try:
                            pid = find_linux_browser_pid(
                                executable=executable, profile_dir=Path(request["profile"])
                            )
                            break
                        except RuntimeError:
                            if attempt == 19:
                                raise
                            await asyncio.sleep(0.2)
                    browser = await CuaBrowserSession.attach(
                        expected_url=request["origin"],
                        application_id=request["runId"],
                        timeout_seconds=30,
                        input_route="dom_event",
                        browser_pid=pid,
                        browser_executable=executable,
                    )
                elif request["op"] == "action":
                    if browser is None or state is None:
                        raise ValueError("Not initialized")
                    action = request["action"]
                    ref = refs.get(action.get("index"))
                    if not ref or not re.search(
                        r"Full name|\bEmail\b|I confirm this is synthetic test data|"
                        r"Continue to experience|Describe a relevant project|"
                        r"Submit synthetic application",
                        ref.get("name", ""),
                    ):
                        raise ValueError("Target outside synthetic form")
                    kind = action["action"]
                    if kind not in {"click", "fill"}:
                        raise ValueError("Only click/fill allowed")
                    required_action = "type" if kind == "fill" else "click"
                    if required_action not in ref.get("actions", []):
                        raise ValueError("Action not supported by target")
                    arguments = {
                        "target_id": browser.target_id,
                        "tab_id": browser.tab_id,
                        "session": browser.session,
                        "ref": ref["ref"],
                        "input_route": "dom_event",
                    }
                    if kind == "fill":
                        if action.get("value") not in values:
                            raise ValueError("Unapproved fixture value")
                        arguments.update(text=action["value"], replace=True)
                    # This adapter is bound only to its new loopback fixture. The
                    # production adapter's final-submit restriction is unchanged.
                    action_result = await _call_tool(
                        browser.driver,
                        "browser_type" if kind == "fill" else "browser_click",
                        arguments,
                        timeout_seconds=30,
                    )
                    if action_result.get("effect") == "refused":
                        reason = action_result.get("escalation", {}).get("reason", "unknown")
                        raise RuntimeError(f"CUA action refused: {reason}")
                else:
                    raise ValueError("Unknown operation")
                state = await browser.observe()
                refs = {1000 + i: ref for i, ref in enumerate(state["refs"])}
                lines = [state["outline"], "Current normalized control indices:"]
                for index, ref in refs.items():
                    role = ref.get("role", "unknown")
                    role = {"textbox": "entry", "checkbox": "check box"}.get(role, role)
                    lines.append(f"{index} {role} {ref.get('name', '')}")
                result = {
                    "content": [{"type": "text", "text": "\n".join(lines)}],
                    "isError": False,
                    "driverSnapshot": state,
                    "actionResult": action_result,
                }
            except Exception as exc:  # noqa: BLE001 - return driver failures over the test protocol
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                    "actionResult": action_result,
                }
            print(json.dumps(result), flush=True)
    finally:
        if browser:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
