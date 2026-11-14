from pathlib import Path

import pytest

from job_apply_agent.collectors.base import BOARD_SLUG_RE
from job_apply_agent.config import Settings

SHIPPED_BOARDS_PATH = (
    Path(__file__).resolve().parents[1] / "src/job_apply_agent/examples/source-boards.json"
)


def test_shipped_source_boards_file_matches_runtime_schema():
    raw_text = SHIPPED_BOARDS_PATH.read_text()
    boards = Settings(source_boards_json=raw_text).source_boards()

    pairs = [(board["ats"], board["board_slug"]) for board in boards]
    assert len(pairs) == len(set(pairs)), "duplicate (ats, board_slug) entries"
    assert boards and all(board["enabled"] is False for board in boards)
    for board in boards:
        assert BOARD_SLUG_RE.fullmatch(board["board_slug"])
        assert board["max_jobs"] == 3
    title_keywords = {tuple(board["title_keywords"]) for board in boards}
    location_keywords = {tuple(board["location_keywords"]) for board in boards}
    assert len(title_keywords) == 1
    assert len(location_keywords) == 1


def test_source_boards_config_is_parsed():
    settings = Settings(
        source_boards_json='[{"ats":"greenhouse","board_slug":"acme","max_jobs":20}]'
    )
    assert settings.source_boards()[0]["board_slug"] == "acme"


@pytest.mark.parametrize("value", ["not-json", "{}", '[{"ats":"greenhouse"}]'])
def test_source_boards_config_is_rejected_when_malformed(value):
    settings = Settings(source_boards_json=value)
    with pytest.raises(ValueError):
        settings.source_boards()


def test_source_board_filters_must_be_lists_of_non_empty_strings():
    settings = Settings(
        source_boards_json=(
            '[{"ats":"greenhouse","board_slug":"acme","title_keywords":"software engineer"}]'
        )
    )
    with pytest.raises(ValueError, match="title_keywords"):
        settings.source_boards()


@pytest.mark.parametrize(
    "board",
    [
        {"ats": "unsupported", "board_slug": "sample"},
        {"ats": "lever", "board_slug": "https://example.test/jobs"},
        {"ats": "lever", "board_slug": "sample", "enabled": "false"},
    ],
)
def test_source_boards_reject_unsafe_or_ambiguous_configuration(board):
    import json

    with pytest.raises(ValueError):
        Settings(_env_file=None, source_boards_json=json.dumps([board])).source_boards()


def test_candidate_profile_config_is_parsed():
    settings = Settings(profile_json='{"years_experience": 6, "skills": ["python"]}')
    assert settings.candidate_profile()["years_experience"] == 6


@pytest.mark.parametrize("value", ["not-json", '["profile"]'])
def test_candidate_profile_config_is_rejected_when_malformed(value):
    settings = Settings(profile_json=value)
    with pytest.raises(ValueError):
        settings.candidate_profile()


def test_empty_optional_telegram_chat_id_is_unset():
    assert Settings(telegram_chat_id="").telegram_chat_id is None


def test_empty_optional_glm_key_is_unset():
    assert Settings(glm_api_key="").glm_api_key is None


def test_cua_step_limit_is_bounded():
    with pytest.raises(ValueError):
        Settings(cua_max_steps=0)


def test_glm_reasoning_is_explicit_and_bounded():
    assert Settings().glm_reasoning_effort == "high"
    with pytest.raises(ValueError):
        Settings(glm_reasoning_effort="unbounded")


def test_cua_requires_glm_key_and_bounded_virtual_browser():
    assert "GLM_API_KEY" in Settings(cua_enabled=True).cua_configuration_error()
    assert (
        "BROWSER_HEADLESS"
        in Settings(
            cua_enabled=True, glm_api_key="secret", browser_headless=True
        ).cua_configuration_error()
    )
    ready = Settings(cua_enabled=True, glm_api_key="secret", browser_headless=False)
    assert ready.cua_configuration_error() is None
    assert (
        "BROWSER_CHANNEL"
        in Settings(
            cua_enabled=True,
            glm_api_key="secret",
            browser_headless=False,
            browser_channel="chrome",
        ).cua_configuration_error()
    )


def test_unknown_cua_browser_input_route_is_rejected():
    with pytest.raises(ValueError, match="CUA_BROWSER_INPUT_ROUTE"):
        Settings(cua_browser_input_route="magic")


def test_search_policy_is_loaded_from_explicit_private_file(tmp_path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"version": 7, "low_stakes_title_keywords": ["technician"]}')
    settings = Settings(search_policy_path=policy_path)

    assert settings.search_policy()["version"] == 7
    assert settings.search_policy()["low_stakes_title_keywords"] == ["technician"]


def test_private_files_and_legacy_override_precedence(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text('{"skills": ["Python"]}')
    boards = tmp_path / "boards.json"
    boards.write_text('[{"ats":"lever","board_slug":"sample"}]')
    settings = Settings(_env_file=None, profile_path=profile, source_boards_path=boards)
    assert settings.candidate_profile()["skills"] == ["Python"]
    assert settings.source_boards()[0]["ats"] == "lever"
    settings.profile_json = '{"skills": ["Rust"]}'
    assert settings.candidate_profile()["skills"] == ["Rust"]
