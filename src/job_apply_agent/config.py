from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./private/job_apply_agent.sqlite3"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    artifacts_dir: Path = Path("private/artifacts")
    browser_profile_dir: Path = Path("private/browser-profile")
    browser_channel: str = "chromium"
    browser_headless: bool = True
    browser_page_ready_timeout_seconds: int = Field(default=30, ge=1, le=120)
    cua_enabled: bool = False
    cua_backend: Literal["driver", "lcu", "codex"] = "driver"
    codex_binary: str = "codex"
    codex_model: str = "gpt-6-astra"
    codex_driver_fallback: bool = True
    codex_action_timeout_seconds: int = Field(default=10, ge=1, le=60)
    lcu_prefix: Path = Path("/opt/lcu/current")
    lcu_node_path: Path = Path("/opt/chatgpt/resources/cua_node/bin/node")
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_api_key: str | None = None
    glm_model: str = "glm-5.3-flash"
    glm_reasoning_effort: Literal["low", "high", "max"] = "high"
    cua_max_steps: int = Field(default=20, ge=1, le=100)
    cua_step_timeout_seconds: int = Field(default=120, ge=10, le=600)
    cua_driver_attach_timeout_seconds: int = Field(default=20, ge=5, le=120)
    cua_browser_input_route: str = "dom_event"
    cua_instructions: str = ""
    submit_confirmation_timeout_seconds: int = Field(default=20, ge=5, le=120)
    allow_live_submit: bool = False
    daily_fetch_limit: int = Field(default=20, ge=1, le=100)
    daily_auto_submit_limit: int = Field(default=10, ge=0, le=100)
    high_value_threshold: int = Field(default=65, ge=0, le=100)
    collection_hour_utc: int = Field(default=6, ge=0, le=23)
    source_boards_json: str = "[]"
    profile_json: str = "{}"
    profile_path: Path = Path("private/candidate/profile.json")
    source_boards_path: Path = Path("private/candidate/source-boards.json")
    answer_bank_path: Path = Path("private/candidate/answer-bank.json")
    resume_file: Path | None = None
    experience_library_path: Path = Path("private/candidate/experiences.json")
    experience_reasoning_enabled: bool = False
    experience_retrieval_mode: Literal["full", "hybrid", "auto"] = "full"
    experience_retrieval_top_k: int = Field(default=5, ge=1, le=30)
    # Provisional engineering threshold, not an experimentally established optimum.
    experience_auto_char_threshold: int = Field(default=24000, ge=1)
    experience_cache_path: Path = Path("private/artifacts/experience-vectors.sqlite3")
    search_policy_path: Path = Path("private/candidate/search-policy.json")

    @field_validator("resume_file", mode="before")
    @classmethod
    def empty_resume_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("browser_profile_dir")
    @classmethod
    def reject_repo_profile(cls, value: Path) -> Path:
        parts = set(value.parts)
        if ".git" in parts:
            raise ValueError("browser profile must never be stored inside .git")
        return value

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def empty_chat_id_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator(
        "glm_api_key",
        mode="before",
    )
    @classmethod
    def empty_secret_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("cua_browser_input_route")
    @classmethod
    def supported_browser_input_route(cls, value: str) -> str:
        if value not in {"trusted", "dom_event"}:
            raise ValueError("CUA_BROWSER_INPUT_ROUTE must be trusted or dom_event")
        return value

    def cua_configuration_error(self) -> str | None:
        if not self.cua_enabled:
            return None
        if self.cua_backend != "codex" and not self.glm_api_key:
            return "GLM_API_KEY is required when CUA_ENABLED=true"
        if self.browser_headless:
            return "BROWSER_HEADLESS=false is required for CUA Driver"
        if self.browser_channel != "chromium":
            return "BROWSER_CHANNEL=chromium is required for bounded CUA Driver binding"
        return None

    def source_boards(self) -> list[dict[str, Any]]:
        """Read private board configuration; explicit legacy JSON takes precedence."""
        try:
            value = json.loads(
                self.source_boards_path.read_text()
                if self.source_boards_json == "[]" and self.source_boards_path.is_file()
                else self.source_boards_json
            )
        except json.JSONDecodeError as exc:
            raise ValueError("SOURCE_BOARDS_JSON must be valid JSON") from exc
        if not isinstance(value, list):
            raise ValueError("SOURCE_BOARDS_JSON must contain a JSON list")  # noqa: TRY004
        for board in value:
            if not isinstance(board, dict) or not isinstance(board.get("ats"), str):
                raise ValueError(  # noqa: TRY004
                    "each source board must be an object with an ats string"
                )
            if not isinstance(board.get("board_slug"), str):
                raise ValueError(  # noqa: TRY004
                    "each source board must include a board_slug string"
                )
            if board["ats"] not in {"greenhouse", "lever", "ashby"}:
                raise ValueError("unsupported source ATS type")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", board["board_slug"]):
                raise ValueError("board_slug must be a simple ATS board identifier")
            if not isinstance(board.get("enabled", True), bool):
                raise ValueError("source enabled must be a boolean")  # noqa: TRY004
            if board.get("max_jobs") is not None and (
                not isinstance(board["max_jobs"], int) or board["max_jobs"] <= 0
            ):
                raise ValueError("max_jobs must be a positive integer")
            for key in ("title_keywords", "location_keywords"):
                terms = board.get(key, [])
                if not isinstance(terms, list) or not all(
                    isinstance(term, str) and term.strip() for term in terms
                ):
                    raise ValueError(f"{key} must contain non-empty strings")
        return value

    def candidate_profile(self) -> dict[str, Any]:
        """Read a private profile file; explicit legacy JSON takes precedence."""
        try:
            value = json.loads(
                self.profile_path.read_text()
                if self.profile_json == "{}" and self.profile_path.is_file()
                else self.profile_json
            )
        except json.JSONDecodeError as exc:
            raise ValueError("PROFILE_JSON must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("PROFILE_JSON must contain a JSON object")  # noqa: TRY004
        return value

    def search_policy(self) -> dict[str, Any]:
        """Load the user's private routing policy used by scheduled collection."""
        if not self.search_policy_path.is_file():
            raise ValueError(f"search policy file not found: {self.search_policy_path}")
        try:
            value = json.loads(self.search_policy_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError("search policy file must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(  # noqa: TRY004
                "search policy file must contain a JSON object"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
