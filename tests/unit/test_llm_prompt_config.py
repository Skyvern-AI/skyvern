"""Tests for SKY-9622: get_llm_handler_for_prompt_type warning behaviour.

Verified contracts:
- When config is None (flag disabled or error): return None silently.
- When config exists but prompt type is absent: return None at debug level only.
- When config has the prompt type: delegate to LLMAPIHandlerFactory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.experimentation import llm_prompt_config as module

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_log_recorder() -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
    """Return a mock LOG and a list that accumulates (level, kwargs) tuples."""
    calls: list[tuple[str, dict[str, Any]]] = []
    log = MagicMock()

    def _record(level: str) -> Any:
        def _call(*args: object, **kwargs: object) -> None:
            calls.append((level, dict(kwargs)))

        return _call

    log.warning = _record("warning")
    log.debug = _record("debug")
    log.info = _record("info")
    log.error = _record("error")
    return log, calls


# ---------------------------------------------------------------------------
# get_llm_handler_for_prompt_type: no-config path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_config_returns_none_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag disabled → config is None → return None, no LOG.warning."""
    monkeypatch.setattr(
        module,
        "get_llm_config_by_prompt_type",
        AsyncMock(return_value=None),
    )
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    result = await module.get_llm_handler_for_prompt_type("extract-information", "wr_123", "org_1")

    assert result is None
    warning_calls = [c for level, c in calls if level == "warning"]
    assert not warning_calls, f"Expected no warnings, got: {warning_calls}"


# ---------------------------------------------------------------------------
# get_llm_handler_for_prompt_type: prompt type absent from config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_type_missing_from_config_returns_none_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config exists but prompt type not present → return None, no LOG.warning.

    The four types absent from the PostHog payload at time of SKY-9622:
    single-input-action, single-click-action, single-select-action, custom-select.
    These fall back to the default app handler; warning was incorrect.
    """
    config = {
        "extract-information": "GEMINI_2_5_FLASH_WITH_FALLBACK",
        "normal-select": "GEMINI_2_5_FLASH_WITH_FALLBACK",
    }
    monkeypatch.setattr(
        module,
        "get_llm_config_by_prompt_type",
        AsyncMock(return_value=config),
    )
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    for prompt_type in ("single-input-action", "single-click-action", "single-select-action", "custom-select"):
        calls.clear()
        result = await module.get_llm_handler_for_prompt_type(prompt_type, "wr_123", "org_1")
        assert result is None, f"{prompt_type}: expected None"
        warning_calls = [c for level, c in calls if level == "warning"]
        assert not warning_calls, f"{prompt_type}: unexpected warnings: {warning_calls}"


# ---------------------------------------------------------------------------
# get_llm_handler_for_prompt_type: happy path (type present in config)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_type_in_config_returns_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config contains prompt type → LLMAPIHandlerFactory is called, handler returned."""
    config = {"extract-information": "GEMINI_2_5_FLASH_WITH_FALLBACK"}
    monkeypatch.setattr(
        module,
        "get_llm_config_by_prompt_type",
        AsyncMock(return_value=config),
    )

    fake_handler = object()
    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        return_value=fake_handler,
    ):
        result = await module.get_llm_handler_for_prompt_type("extract-information", "wr_123", "org_1")

    assert result is fake_handler
