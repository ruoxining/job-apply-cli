from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cua_harness import UnsafeComputerAction, scroll_amount

_ROW = re.compile(r"^(\s*)(\d+) (.+)$")
_ROLE = re.compile(
    r"^(entry|text field|text area|text input|textbox|push button|button|check box|"
    r"radio button|toggle button|combo box|list item|menu item|link)\b"
)
_SUBMIT = re.compile(r"submit|send application|apply now|complete application", re.IGNORECASE)


def document_observation(text: str, *, url: str, generation: int) -> dict[str, Any]:
    """Only expose descendants of the web document, never browser chrome."""
    if "Accessibility source: at_spi" not in text:
        raise UnsafeComputerAction("LCU accessibility tree unavailable")
    depth: int | None = None
    document_ref: str | None = None
    outline: list[str] = []
    refs: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        indent, index, body = match.groups()
        if body.startswith("document web"):
            if depth is not None:
                raise UnsafeComputerAction("LCU found multiple web documents")
            depth = len(indent)
            document_ref = f"p{generation}:{index}"
            outline.append(line)
            continue
        if depth is None:
            continue
        if len(indent) <= depth:
            break
        outline.append(line)
        role_match = _ROLE.match(body)
        if not role_match or re.search(r"\b(disabled|unavailable)\b", body):
            continue
        role = role_match[1]
        detail = body[len(role) :].strip()
        name = re.sub(r"^\([^)]*\)\s*", "", detail)
        name = re.split(r", (?:Value|Secondary Actions):", name, maxsplit=1)[0]
        state = re.match(r"^\(([^)]*)\)", detail)
        actions = ["click"]
        if role in {"entry", "text field", "text area", "text input", "textbox"} or (
            role == "combo box" and state and "settable" in state[1].split(", ")
        ):
            actions.append("fill")
        if _SUBMIT.search(name):
            actions = []
        refs.append(
            {
                "ref": f"p{generation}:{index}",
                "role": role,
                "name": name,
                "actions": actions,
                "state": state[1] if state else "",
            }
        )
    if depth is None:
        raise UnsafeComputerAction("LCU returned no web document")
    return {
        "page": {"url": url},
        "outline": "\n".join(outline),
        "refs": refs,
        "document_ref": document_ref,
    }


class LcuBrowserSession:
    """LCU native AT-SPI actions, bound to the worker's single Chromium page."""

    def __init__(self, process: Any, page: Any, timeout: int):
        self.process = process
        self.page = page
        self.origin = urlparse(page.url).netloc
        self.timeout = timeout
        self.generation = 0
        self.latest: dict[str, Any] | None = None

    @classmethod
    async def attach(
        cls, *, page: Any, browser_pid: int, prefix: Path, node_path: Path, timeout_seconds: int
    ) -> LcuBrowserSession:
        # Never leak GLM, database, Telegram or other production credentials to LCU.
        keep = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "DISPLAY",
            "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "XDG_RUNTIME_DIR",
            "AT_SPI_BUS_ADDRESS",
        }
        process = await asyncio.create_subprocess_exec(
            str(node_path),
            str(Path(__file__).with_name("lcu_bridge.mjs")),
            str(prefix),
            env={k: v for k, v in os.environ.items() if k in keep},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2**22,
        )
        browser = cls(process, page, timeout_seconds)
        try:
            await browser._request({"op": "init", "pid": browser_pid})
            await browser.observe()
            return browser
        except BaseException:
            await browser.close()
            raise

    async def _request(self, request: dict) -> dict:
        async with asyncio.timeout(self.timeout):
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            await self.process.stdin.drain()
            line = await self.process.stdout.readline()
        if not line:
            raise UnsafeComputerAction("LCU bridge exited without a response")
        result = json.loads(line)
        if not result.get("ok"):
            raise UnsafeComputerAction(result.get("error", "LCU bridge error"))
        return result

    def _check_scope(self) -> None:
        if urlparse(self.page.url).netloc != self.origin:
            raise UnsafeComputerAction("LCU page navigated outside the initial ATS origin")
        if len(self.page.context.pages) != 1:
            raise UnsafeComputerAction("LCU requires exactly one browser tab")

    async def observe(self) -> dict[str, Any]:
        self._check_scope()
        result = await self._request({"op": "observe"})
        self.generation += 1
        self.latest = document_observation(
            result["text"], url=self.page.url, generation=self.generation
        )
        return self.latest

    async def execute(self, action: dict, observation: dict) -> bool:
        self._check_scope()
        if observation is not self.latest:
            raise UnsafeComputerAction("LCU action uses a stale observation")
        if action["action"] == "stop":
            raise UnsafeComputerAction(action.get("reason") or "planner requested review")
        if action["action"] == "finish":
            return True
        if action["action"] == "scroll":
            fresh = await self.observe()
            self._check_scope()
            await self._request(
                {
                    "op": "action",
                    "action": "scroll",
                    "index": int(fresh["document_ref"].split(":")[1]),
                    "value": max(-1200, min(1200, scroll_amount(action.get("value")))),
                }
            )
            self.latest = None
            return False
        target = next((r for r in observation["refs"] if r["ref"] == action.get("ref")), None)
        if not target or action["action"] not in target["actions"]:
            raise UnsafeComputerAction(
                f"LCU action {action['action']!r} with ref {action.get('ref')!r} "
                "is not allowed in the current document"
            )
        if _SUBMIT.search(target["name"]):
            raise UnsafeComputerAction("Final submission belongs to the deterministic host")

        # The ATS may replace AX nodes while the model is thinking (for example
        # after uploading a resume). Native indices are not durable identities.
        # Rebind only a uniquely named control with the same role; never reuse
        # an old index or guess between repeated Yes/No controls.
        def matches(candidate: dict) -> bool:
            return candidate["name"] == target["name"] and candidate["role"] == target["role"]

        if not target["name"] or sum(matches(r) for r in observation["refs"]) != 1:
            raise UnsafeComputerAction("LCU target has no unique accessible identity")
        fresh = await self.observe()
        candidates = [r for r in fresh["refs"] if matches(r)]
        if len(candidates) != 1 or action["action"] not in candidates[0]["actions"]:
            raise UnsafeComputerAction("LCU target changed before execution; replan required")
        current = candidates[0]
        if target["role"] in {"check box", "radio button", "toggle button"}:
            selection = re.compile(
                r"\b(?:not pressed|pressed|unpressed|checked|unchecked|selected|unselected|on|off)\b"
            )
            if selection.findall(target["state"]) != selection.findall(current["state"]):
                raise UnsafeComputerAction("LCU selection changed before execution")
        self._check_scope()
        await self._request(
            {
                "op": "action",
                "action": action["action"],
                "index": int(current["ref"].split(":")[1]),
                "value": action.get("value"),
            }
        )
        self.latest = None
        return False

    async def close(self) -> None:
        if self.process.returncode is not None:
            return
        try:
            self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except (TimeoutError, BrokenPipeError):
            self.process.kill()
            await self.process.wait()
