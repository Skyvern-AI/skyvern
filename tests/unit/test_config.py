import logging

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
