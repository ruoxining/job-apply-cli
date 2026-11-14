"""Server-local Codex structured decisions; browser effects remain host-owned."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .cua_harness import UnsafeComputerAction

ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["click", "fill", "select", "check", "uncheck", "scroll", "finish", "stop"],
        },
        "ref": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
    },
    "required": ["action", "ref", "value", "reason"],
}


class CodexCLIPlanner:
    def __init__(self, *, binary: str, model: str, timeout_seconds: int = 120) -> None:
        self.binary, self.model, self.timeout_seconds = binary, model, timeout_seconds
        self.observed_model: str | None = None
        self.calls = 0
        self.telemetry: list[dict[str, Any]] = []

    async def check_ready(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "login",
                "status",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise UnsafeComputerAction("Codex CLI is not installed or executable") from exc
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except BaseException as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise UnsafeComputerAction("Codex login status timed out") from exc
            raise
        if process.returncode:
            raise UnsafeComputerAction("Codex CLI is not signed in in this runtime")

    async def request_json(
        self, prompt: str, schema: dict[str, Any], screenshot_path: Path | None = None
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="job-codex-") as temporary:
            root = Path(temporary)
            schema_path, output_path = root / "schema.json", root / "answer.json"
            schema_path.write_text(json.dumps(schema))
            command = [
                self.binary,
                "exec",
                "--ephemeral",
                "--json",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.model:
                command += ["--model", self.model]
            if screenshot_path is not None:
                command += ["--image", str(screenshot_path)]
            command += ["-"]
            self.calls += 1
            started = time.perf_counter()
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=root,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise UnsafeComputerAction("Codex CLI is not installed or executable") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode()), timeout=self.timeout_seconds
                )
            except BaseException as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                self.telemetry.append(
                    {
                        "elapsed_seconds": time.perf_counter() - started,
                        "usage": None,
                        "exit_code": process.returncode,
                        "error_type": type(exc).__name__,
                    }
                )
                if isinstance(exc, TimeoutError):
                    raise UnsafeComputerAction("Codex planner timed out") from exc
                raise
            usage = []
            unexpected_tools = False
            for line in (stdout or b"").splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usage.append(
                        {
                            k: v
                            for k, v in event["usage"].items()
                            if k.endswith("tokens") and isinstance(v, int) and v >= 0
                        }
                    )
                item = event.get("item") or {}
                if item.get("type") in {
                    "command_execution",
                    "mcp_tool_call",
                    "web_search",
                    "file_change",
                }:
                    unexpected_tools = True
            self.telemetry.append(
                {
                    "elapsed_seconds": time.perf_counter() - started,
                    "usage": usage or None,
                    "exit_code": process.returncode,
                    "unexpected_tools": unexpected_tools,
                }
            )
            if unexpected_tools:
                raise UnsafeComputerAction("Codex decision unexpectedly used a tool")
            matched = re.search(r"(?m)^model:\s*(\S+)", stderr.decode(errors="replace"))
            if matched:
                self.observed_model = matched.group(1)
            if process.returncode or not output_path.is_file():
                # CLI stderr can contain the entire candidate prompt or credentials.
                raise UnsafeComputerAction(f"Codex planner failed (exit={process.returncode})")
            try:
                result = json.loads(output_path.read_text())
            except (ValueError, OSError) as exc:
                raise UnsafeComputerAction("Codex planner returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise UnsafeComputerAction("Codex planner returned a non-object")
            return result

    async def next_action(self, prompt: str, screenshot_path: Path) -> dict[str, Any]:
        action = await self.request_json(prompt, ACTION_SCHEMA, screenshot_path)
        if action.get("action") not in ACTION_SCHEMA["properties"]["action"]["enum"]:
            raise UnsafeComputerAction("Codex returned an unsupported action")
        return action

    async def close(self) -> None:
        pass
