import logging
from datetime import UTC, datetime

import pytest

from skyvern.config import CodeBlockMode, Settings


@pytest.mark.parametrize(
    ("legacy_setting", "legacy_value", "expected_mode"),
    [
        ("DISABLE_CODE_BLOCK_EXECUTION", True, CodeBlockMode.disabled),
        ("ENABLE_CODE_BLOCK", False, CodeBlockMode.entitlement),
        ("ENABLE_CODE_BLOCK", True, CodeBlockMode.enabled),
    ],
)
def test_legacy_code_block_settings_map_to_mode_with_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
    legacy_setting: str,
    legacy_value: bool,
    expected_mode: CodeBlockMode,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skyvern.config"):
        configured = Settings(_env_file=None, **{legacy_setting: legacy_value})

    assert configured.CODE_BLOCK_MODE is expected_mode
    assert legacy_setting in caplog.text
    assert str(legacy_value) in caplog.text
    assert f"CODE_BLOCK_MODE={expected_mode.value}" in caplog.text
    assert "deprecated" in caplog.text


def test_disable_code_block_execution_false_emits_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skyvern.config"):
        configured = Settings(_env_file=None, DISABLE_CODE_BLOCK_EXECUTION=False)

    assert configured.CODE_BLOCK_MODE is CodeBlockMode.enabled
    assert "DISABLE_CODE_BLOCK_EXECUTION=False" in caplog.text
    assert "CODE_BLOCK_MODE=enabled" in caplog.text
    assert "deprecated" in caplog.text


def test_legacy_code_block_setting_warns_when_another_legacy_setting_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skyvern.config"):
        configured = Settings(
            _env_file=None,
            DISABLE_CODE_BLOCK_EXECUTION=True,
            ENABLE_CODE_BLOCK=False,
        )

    assert configured.CODE_BLOCK_MODE is CodeBlockMode.disabled
    deprecation_messages = [record.getMessage() for record in caplog.records if "deprecated" in record.getMessage()]
    assert len(deprecation_messages) == 2
    assert any("DISABLE_CODE_BLOCK_EXECUTION=True" in message for message in deprecation_messages)
    assert any("ENABLE_CODE_BLOCK=False" in message for message in deprecation_messages)
    assert all("CODE_BLOCK_MODE=disabled" in message for message in deprecation_messages)


def test_code_block_mode_defaults_to_enabled_without_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skyvern.config"):
        configured = Settings(_env_file=None)

    assert configured.CODE_BLOCK_MODE is CodeBlockMode.enabled
    assert "ENABLE_CODE_BLOCK" not in caplog.text
    assert "DISABLE_CODE_BLOCK_EXECUTION" not in caplog.text


def test_explicit_code_block_mode_ignores_legacy_setting_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skyvern.config"):
        configured = Settings(
            _env_file=None,
            CODE_BLOCK_MODE=CodeBlockMode.disabled,
            ENABLE_CODE_BLOCK=True,
        )

    assert configured.CODE_BLOCK_MODE is CodeBlockMode.disabled
    assert "ENABLE_CODE_BLOCK" in caplog.text
    assert "True" in caplog.text
    assert "CODE_BLOCK_MODE=disabled" in caplog.text
    assert "deprecated" in caplog.text
    assert "ignored" in caplog.text


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("256", 256),
        # The gt=0 bound would otherwise make "turn shedding off" unreachable from an env file,
        # and a boot-time ValidationError takes the API down rather than uncapping it.
        ("", None),
        ("0", None),
        ("none", None),
        ("null", None),
    ],
)
def test_api_limit_concurrency_env_values(
    monkeypatch: pytest.MonkeyPatch, raw_value: str, expected: int | None
) -> None:
    monkeypatch.setenv("API_LIMIT_CONCURRENCY", raw_value)

    assert Settings(_env_file=None).API_LIMIT_CONCURRENCY == expected


def test_api_limit_concurrency_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_LIMIT_CONCURRENCY", "-1")

    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("2026-09-15T00:00:00Z", datetime(2026, 9, 15, tzinfo=UTC)),
        ("2026-09-15 00:00:00", datetime(2026, 9, 15)),
        ("2026-09-15", datetime(2026, 9, 15)),
        # This setting is the settings-side kill path for the new-workflow v3 default, and blanking
        # an already-set env var is how it gets turned off. A boot-time ValidationError would
        # CrashLoopBackOff every arm-resolving process during the incident the operator is ending.
        ("", None),
        ("0", None),
        ("none", None),
        ("null", None),
        ("off", None),
        ("disabled", None),
    ],
)
@pytest.mark.parametrize("setting", ["TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF", "TASK_V3_CHOSEN_ENGINE_CUTOFF"])
def test_task_v3_default_engine_workflow_cutoff_env_values(
    monkeypatch: pytest.MonkeyPatch, setting: str, raw_value: str, expected: datetime | None
) -> None:
    monkeypatch.setenv(setting, raw_value)

    assert getattr(Settings(_env_file=None), setting) == expected


EMBER_RENAMED_SETTINGS = {
    "COPILOT_EVAL_AUTH_FIXTURE_ENABLED": "EMBER_EVAL_AUTH_FIXTURE_ENABLED",
    "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS": "EMBER_RAW_SECRET_SAFETY_TIMEOUT_SECONDS",
    "COPILOT_RAW_SECRET_SAFETY_HEDGE_ENABLED": "EMBER_RAW_SECRET_SAFETY_HEDGE_ENABLED",
    "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS": "EMBER_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS",
    "COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS": "EMBER_COMPLETION_JUDGE_TIMEOUT_SECONDS",
    "COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS": "EMBER_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS",
    "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS": "EMBER_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS",
    "COPILOT_CLICKABLE_CONTROLS_EVIDENCE_ENABLED": "EMBER_CLICKABLE_CONTROLS_EVIDENCE_ENABLED",
    "WORKFLOW_COPILOT_BROWSER_ABLATION_ENABLED": "EMBER_BROWSER_ABLATION_ENABLED",
    "WORKFLOW_COPILOT_BROWSER_ABLATION_ORGANIZATION_IDS": "EMBER_BROWSER_ABLATION_ORGANIZATION_IDS",
    "WORKFLOW_COPILOT_CODE_BLOCK_MODE": "EMBER_CODE_BLOCK_MODE",
    "COPILOT_NARROW_AGENT_STEP_ACCOUNTS": "EMBER_NARROW_AGENT_STEP_ACCOUNTS",
    "WORKFLOW_COPILOT_QA_TOKEN_BUDGET": "EMBER_QA_TOKEN_BUDGET",
    "WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS": "EMBER_TOTAL_TIMEOUT_SECONDS",
    "WORKFLOW_COPILOT_CREDENTIAL_PAUSE_ENABLED": "EMBER_CREDENTIAL_PAUSE_ENABLED",
    "WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS": "EMBER_CREDENTIAL_PAUSE_TIMEOUT_SECONDS",
    "WORKFLOW_COPILOT_MANUAL_SIGN_IN_TIMEOUT_SECONDS": "EMBER_MANUAL_SIGN_IN_TIMEOUT_SECONDS",
    "WORKFLOW_COPILOT_CODEGEN_PROGRESS_ENABLED": "EMBER_CODEGEN_PROGRESS_ENABLED",
    "WORKFLOW_COPILOT_ODYSSEYS_EVAL_INPUTS_ENABLED": "EMBER_ODYSSEYS_EVAL_INPUTS_ENABLED",
    "WORKFLOW_COPILOT_ODYSSEYS_EVAL_ORGANIZATION_IDS": "EMBER_ODYSSEYS_EVAL_ORGANIZATION_IDS",
    "WORKFLOW_COPILOT_EVAL_LLM_OVERRIDES_ORGANIZATION_IDS": "EMBER_EVAL_LLM_OVERRIDES_ORGANIZATION_IDS",
    "WORKFLOW_COPILOT_EVAL_COST_ENABLED": "EMBER_EVAL_COST_ENABLED",
    "WORKFLOW_COPILOT_EVAL_COST_ORGANIZATION_IDS": "EMBER_EVAL_COST_ORGANIZATION_IDS",
    "COPILOT_ALLOW_INLINE_CODE_EXECUTION": "EMBER_ALLOW_INLINE_CODE_EXECUTION",
    "WORKFLOW_COPILOT_LLM_KEY": "EMBER_LLM_KEY",
    "WORKFLOW_COPILOT_AGENT_LLM_KEY": "EMBER_AGENT_LLM_KEY",
    "WORKFLOW_COPILOT_FAST_LLM_KEY": "EMBER_FAST_LLM_KEY",
    "WORKFLOW_COPILOT_LITE_LLM_KEY": "EMBER_LITE_LLM_KEY",
    "COPILOT_ACCOUNT_GROUP_SUBMIT_ENABLED": "EMBER_ACCOUNT_GROUP_SUBMIT_ENABLED",
    "COPILOT_CREDENTIAL_DELETE_ENABLED": "EMBER_CREDENTIAL_DELETE_ENABLED",
}

# Two env strings per field type, with their parsed values.
_SAMPLE_PAIRS = {
    bool: (("true", True), ("false", False)),
    float: (("7.5", 7.5), ("1.5", 1.5)),
    int: (("42", 42), ("7", 7)),
    int | None: (("42", 42), ("7", 7)),
    str | None: (("NEW_KEY", "NEW_KEY"), ("OLD_KEY", "OLD_KEY")),
    list[str]: (('["org-new"]', ["org-new"]), ('["org-old"]', ["org-old"])),
}


@pytest.mark.parametrize(("old_name", "new_name"), EMBER_RENAMED_SETTINGS.items())
def test_ember_settings_read_the_old_env_name_and_prefer_the_new_one(
    monkeypatch: pytest.MonkeyPatch, old_name: str, new_name: str
) -> None:
    field = Settings.model_fields[new_name]
    first, second = _SAMPLE_PAIRS[field.annotation]
    # The value read must differ from the default, or reading nothing would pass.
    wanted, other = (first, second) if first[1] != field.default else (second, first)

    monkeypatch.setenv(old_name, wanted[0])
    assert getattr(Settings(_env_file=None), new_name) == wanted[1]

    monkeypatch.setenv(old_name, other[0])
    monkeypatch.setenv(new_name, wanted[0])
    assert getattr(Settings(_env_file=None), new_name) == wanted[1]
