#!/usr/bin/env python3
"""Validate production deployment configuration without printing secrets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate(
    values: dict[str, str], *, allow_empty_sources: bool = False, base_dir: Path = Path(".")
) -> list[str]:
    errors: list[str] = []
    if values.get("APP_ENV") != "production":
        errors.append("APP_ENV must be production")
    password = values.get("POSTGRES_PASSWORD", "")
    if not password or password == "change-me":
        errors.append("POSTGRES_PASSWORD must be set to a non-default value")
    live_submit = values.get("ALLOW_LIVE_SUBMIT", "false").lower() == "true"
    if (
        live_submit
        and values.get("LIVE_SUBMIT_ACK") != "I_UNDERSTAND_REAL_APPLICATIONS_WILL_BE_SENT"
    ):
        errors.append(
            "LIVE_SUBMIT_ACK must explicitly acknowledge that real applications will be sent"
        )
    cua_enabled = values.get("CUA_ENABLED", "false").lower() == "true"
    backend = values.get("CUA_BACKEND", "driver")
    if backend not in {"driver", "lcu", "codex"}:
        errors.append("CUA_BACKEND must be driver, lcu, or codex")
    if cua_enabled and backend in {"driver", "lcu"} and not values.get("GLM_API_KEY"):
        errors.append("GLM_API_KEY is required for the driver and lcu backends")
    if cua_enabled and backend == "codex":
        for key in ("CODEX_BINARY", "CODEX_MODEL"):
            if key in values and not values[key].strip():
                errors.append(f"{key} must not be empty for the codex backend")
    if cua_enabled and values.get("BROWSER_HEADLESS", "true").lower() != "false":
        errors.append("BROWSER_HEADLESS must be false when CUA_ENABLED=true")
    if cua_enabled and values.get("BROWSER_CHANNEL", "chromium") != "chromium":
        errors.append("BROWSER_CHANNEL must be chromium when CUA_ENABLED=true")
    if (
        cua_enabled
        and backend in {"driver", "lcu"}
        and values.get("GLM_REASONING_EFFORT", "high")
        not in {
            "low",
            "high",
            "max",
        }
    ):
        errors.append("GLM_REASONING_EFFORT must be low, high, or max")
    if values.get("EXPERIENCE_REASONING_ENABLED", "false").lower() == "true" and (
        not cua_enabled or backend != "codex"
    ):
        errors.append(
            "EXPERIENCE_REASONING_ENABLED requires CUA_ENABLED=true and CUA_BACKEND=codex"
        )
    if values.get("EXPERIENCE_RETRIEVAL_MODE", "full") not in {"full", "hybrid", "auto"}:
        errors.append("EXPERIENCE_RETRIEVAL_MODE must be full, hybrid, or auto")
    if not values.get("TELEGRAM_BOT_TOKEN"):
        errors.append("TELEGRAM_BOT_TOKEN must be set")
    try:
        if int(values.get("TELEGRAM_CHAT_ID", "")) == 0:
            raise ValueError
    except ValueError:
        errors.append("TELEGRAM_CHAT_ID must be a non-zero integer")

    try:
        raw_boards = values.get("SOURCE_BOARDS_JSON", "[]")
        if raw_boards == "[]" and values.get("SOURCE_BOARDS_PATH"):
            raw_boards = (base_dir / values["SOURCE_BOARDS_PATH"]).read_text()
        boards = json.loads(raw_boards)
        if not isinstance(boards, list):
            raise TypeError
        for board in boards:
            if not isinstance(board, dict) or not all(
                isinstance(board.get(key), str) for key in ("ats", "board_slug")
            ):
                raise TypeError
            if board["ats"] not in {"greenhouse", "lever", "ashby"}:
                errors.append(f"unsupported ATS type: {board['ats']}")
        if not boards and not allow_empty_sources:
            errors.append("SOURCE_BOARDS_JSON must contain at least one source board")
    except (OSError, json.JSONDecodeError, TypeError):
        errors.append("source boards must be a readable JSON list of ats/board_slug objects")

    try:
        raw_profile = values.get("PROFILE_JSON", "{}")
        if raw_profile == "{}" and values.get("PROFILE_PATH"):
            raw_profile = (base_dir / values["PROFILE_PATH"]).read_text()
        profile = json.loads(raw_profile)
        if not isinstance(profile, dict):
            raise TypeError
    except (OSError, json.JSONDecodeError, TypeError):
        errors.append("profile must be a readable JSON object")

    try:
        hour = int(values.get("COLLECTION_HOUR_UTC", "6"))
        if not 0 <= hour <= 23:
            raise ValueError
    except ValueError:
        errors.append("COLLECTION_HOUR_UTC must be an integer from 0 to 23")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--allow-empty-sources",
        action="store_true",
        help="allow a safe infrastructure-only deployment with no collection",
    )
    args = parser.parse_args()
    if not args.env_file.is_file():
        print(f"configuration file not found: {args.env_file}", file=sys.stderr)
        return 2
    try:
        values = load_env(args.env_file)
        errors = validate(values, allow_empty_sources=args.allow_empty_sources)
    except ValueError as exc:
        errors = [str(exc)]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("deployment configuration is valid (secrets not displayed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
