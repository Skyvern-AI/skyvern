"""Tests for SKY-9622: get_llm_handler_for_prompt_type warning behaviour.

Verified contracts:
- When config is None (flag disabled or error): return None silently.
- When config exists but prompt type is absent: return None at debug level only.
- When config has the prompt type: delegate to LLMAPIHandlerFactory.
"""

from __future__ import annotations

from types import SimpleNamespace
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
async def test_prompt_type_config_uses_cached_posthog_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SimpleNamespace(
        get_value_cached=AsyncMock(return_value="enabled"),
        get_payload_cached=AsyncMock(return_value='{"workflow-copilot-lite": "OPENAI_GPT5_4_MINI"}'),
        get_value=AsyncMock(side_effect=AssertionError("must use cached value lookup")),
        get_payload=AsyncMock(side_effect=AssertionError("must use cached payload lookup")),
    )
    monkeypatch.setattr(module, "app", SimpleNamespace(EXPERIMENTATION_PROVIDER=provider))

    config = await module.get_llm_config_by_prompt_type("wpid_1", "org_1")

    assert config == {"workflow-copilot-lite": "OPENAI_GPT5_4_MINI"}
    provider.get_value_cached.assert_awaited_once_with(
        "LLM_CONFIG_BY_PROMPT_TYPE", "wpid_1", properties={"organization_id": "org_1"}
    )
    provider.get_payload_cached.assert_awaited_once_with(
        "LLM_CONFIG_BY_PROMPT_TYPE", "wpid_1", properties={"organization_id": "org_1"}
    )


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


# ---------------------------------------------------------------------------
# resolve_prompt_type_handler: payload-first with default fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_prompt_type_handler_returns_flex_wrapped_payload_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_handler = MagicMock(name="payload_handler")
    wrapped_handler = MagicMock(name="wrapped_handler")
    default_handler = MagicMock(name="default_handler")
    monkeypatch.setattr(module, "get_llm_handler_for_prompt_type", AsyncMock(return_value=payload_handler))
    wrap = MagicMock(return_value=wrapped_handler)
    monkeypatch.setattr(module.LLMAPIHandlerFactory, "wrap_for_flex_routing", wrap)

    result = await module.resolve_prompt_type_handler(
        "confirm-multi-selection-finish", "wr_123", "org_1", default_handler
    )

    assert result is wrapped_handler
    wrap.assert_called_once_with(payload_handler)


@pytest.mark.asyncio
async def test_resolve_prompt_type_handler_falls_back_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    default_handler = MagicMock(name="default_handler")
    monkeypatch.setattr(module, "get_llm_handler_for_prompt_type", AsyncMock(return_value=None))

    result = await module.resolve_prompt_type_handler("checkbox-verification", "tsk_123", "org_1", default_handler)

    assert result is default_handler


@pytest.mark.asyncio
async def test_resolve_prompt_type_handler_falls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    default_handler = MagicMock(name="default_handler")
    monkeypatch.setattr(module, "get_llm_handler_for_prompt_type", AsyncMock(side_effect=RuntimeError("posthog down")))
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    result = await module.resolve_prompt_type_handler("checkbox-verification", "wr_123", None, default_handler)

    assert result is default_handler
    assert [level for level, _ in calls] == ["warning"]


# ---------------------------------------------------------------------------
# resolve_prompt_type_handler_with_override: explicit key opts out of routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_key_skips_prompt_type_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    default_handler = MagicMock(name="default_handler")
    payload_lookup = AsyncMock(side_effect=AssertionError("payload must not be consulted when override key is set"))
    monkeypatch.setattr(module, "get_llm_handler_for_prompt_type", payload_lookup)

    result = await module.resolve_prompt_type_handler_with_override(
        "confirm-multi-selection-finish", "SOME_EXPLICIT_KEY", "wr_123", "org_1", default_handler
    )

    assert result is default_handler
    payload_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_override_key_delegates_to_prompt_type_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    default_handler = MagicMock(name="default_handler")
    resolver = AsyncMock(return_value=default_handler)
    monkeypatch.setattr(module, "resolve_prompt_type_handler", resolver)

    result = await module.resolve_prompt_type_handler_with_override(
        "confirm-multi-selection-finish", None, "wr_123", "org_1", default_handler
    )

    assert result is default_handler
    resolver.assert_awaited_once_with("confirm-multi-selection-finish", "wr_123", "org_1", default_handler)
