import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_deployment.py"
SPEC = importlib.util.spec_from_file_location("validate_deployment", SCRIPT)
validate_deployment = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(validate_deployment)


def valid_values():
    return {
        "APP_ENV": "production",
        "API_TOKEN": "test-only-api-token-with-at-least-32-characters",
        "POSTGRES_PASSWORD": "not-a-default-password",
        "ALLOW_LIVE_SUBMIT": "false",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_CHAT_ID": "123456",
        "SOURCE_BOARDS_JSON": '[{"ats":"greenhouse","board_slug":"acme"}]',
        "PROFILE_JSON": '{"skills":["python"]}',
        "COLLECTION_HOUR_UTC": "6",
    }


def test_valid_production_configuration_passes():
    assert validate_deployment.validate(valid_values()) == []


def test_unsafe_or_empty_configuration_fails_closed():
    values = valid_values()
    values.update(
        POSTGRES_PASSWORD="change-me",
        ALLOW_LIVE_SUBMIT="true",
        SOURCE_BOARDS_JSON="[]",
    )

    errors = validate_deployment.validate(values)

    assert any("POSTGRES_PASSWORD" in error for error in errors)
    assert any("LIVE_SUBMIT_ACK" in error for error in errors)
    assert any("SOURCE_BOARDS_JSON" in error for error in errors)


def test_live_submit_requires_and_accepts_explicit_acknowledgement():
    values = valid_values()
    values["ALLOW_LIVE_SUBMIT"] = "true"
    assert any("LIVE_SUBMIT_ACK" in error for error in validate_deployment.validate(values))
    values["LIVE_SUBMIT_ACK"] = "I_UNDERSTAND_REAL_APPLICATIONS_WILL_BE_SENT"
    assert validate_deployment.validate(values) == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_all_supported_true_values_require_live_ack(value):
    values = valid_values()
    values["ALLOW_LIVE_SUBMIT"] = value
    assert any("LIVE_SUBMIT_ACK" in error for error in validate_deployment.validate(values))


def test_api_token_required_without_echoing_it():
    values = valid_values()
    values["API_TOKEN"] = "short-test-value"
    errors = validate_deployment.validate(values)
    assert any("API_TOKEN" in error for error in errors)
    assert all("short-test-value" not in error for error in errors)


def test_cua_requires_glm_key_and_a_visible_virtual_browser():
    values = valid_values()
    values["CUA_ENABLED"] = "true"

    errors = validate_deployment.validate(values)

    assert any("GLM_API_KEY" in error for error in errors)
    assert any("BROWSER_HEADLESS" in error for error in errors)

    values["GLM_API_KEY"] = "test-key"
    values["BROWSER_HEADLESS"] = "false"
    assert validate_deployment.validate(values) == []

    values["BROWSER_CHANNEL"] = "chrome"
    assert any("BROWSER_CHANNEL" in error for error in validate_deployment.validate(values))

    values["BROWSER_CHANNEL"] = "chromium"
    values["GLM_REASONING_EFFORT"] = "unbounded"
    assert any("GLM_REASONING_EFFORT" in error for error in validate_deployment.validate(values))


def test_unknown_ats_is_rejected():
    values = valid_values()
    values["SOURCE_BOARDS_JSON"] = '[{"ats":"unknown","board_slug":"acme"}]'

    assert any("unsupported ATS" in error for error in validate_deployment.validate(values))


def test_codex_configuration_does_not_require_legacy_model_credentials():
    values = valid_values()
    values.update(CUA_ENABLED="true", CUA_BACKEND="codex", BROWSER_HEADLESS="false")
    assert validate_deployment.validate(values) == []
    values["CODEX_MODEL"] = " "
    assert any("CODEX_MODEL" in error for error in validate_deployment.validate(values))


def test_unknown_backend_and_ineffective_reasoning_configuration_are_rejected():
    values = valid_values()
    values.update(CUA_BACKEND="typo", EXPERIENCE_REASONING_ENABLED="true")
    errors = validate_deployment.validate(values)
    assert any("CUA_BACKEND must" in error for error in errors)
    assert any("EXPERIENCE_REASONING_ENABLED" in error for error in errors)
    values.update(CUA_ENABLED="true", CUA_BACKEND="codex", BROWSER_HEADLESS="false")
    assert validate_deployment.validate(values) == []


def test_missing_telegram_credentials_are_rejected():
    values = valid_values()
    values["TELEGRAM_BOT_TOKEN"] = ""
    values["TELEGRAM_CHAT_ID"] = "not-an-integer"

    errors = validate_deployment.validate(values)

    assert any("TELEGRAM_BOT_TOKEN" in error for error in errors)
    assert any("TELEGRAM_CHAT_ID" in error for error in errors)
