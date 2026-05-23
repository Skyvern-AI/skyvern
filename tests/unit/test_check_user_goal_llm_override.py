"""Tests for CHECK_USER_GOAL_LLM_NAME multivariate override.

Covers:
- ``get_check_user_goal_llm_override`` — control variants, valid variant,
  factory failure logs WARNING (not error), provider failure, argument shape.
- Process-local caches — success caching (one factory call per variant),
  failure log-dedup (one warning per variant), transient-then-success retry.
- ``resolve_check_user_goal_handler`` — override absent returns the default
  unchanged; override set returns a flex-wrapped handler; provider exception
  falls back to the default without raising.

A module-level autouse fixture resets the process-local caches between tests
so cache-state doesn't bleed and create order-dependence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.experimentation import llm_prompt_config as module


@pytest.fixture(autouse=True)
def _reset_check_user_goal_module_caches() -> Any:
    """Module-level caches in llm_prompt_config bleed across tests; reset before each."""
    module._resolved_check_user_goal_handler_cache.clear()
    module._invalid_check_user_goal_variants_logged.clear()
    yield
    module._resolved_check_user_goal_handler_cache.clear()
    module._invalid_check_user_goal_variants_logged.clear()


def _make_log_recorder() -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
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


def _stub_experimentation_provider(monkeypatch: pytest.MonkeyPatch, value: Any) -> MagicMock:
    provider = MagicMock()
    provider.get_value_cached = AsyncMock(return_value=value)
    fake_app = MagicMock()
    fake_app.EXPERIMENTATION_PROVIDER = provider
    monkeypatch.setattr(module, "app", fake_app)
    return provider


# ---------------------------------------------------------------------------
# get_check_user_goal_llm_override — control / happy / failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [None, False, "False", "false", "", "control"])
async def test_control_variant_returns_none(monkeypatch: pytest.MonkeyPatch, variant: Any) -> None:
    _stub_experimentation_provider(monkeypatch, variant)
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    result = await module.get_check_user_goal_llm_override("wr_123", "org_1")

    assert result is None
    info_calls = [c for level, c in calls if level == "info"]
    assert not info_calls, f"Expected no info logs for control variant, got: {info_calls}"


@pytest.mark.asyncio
async def test_valid_variant_returns_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_experimentation_provider(monkeypatch, "GEMINI_3_1_FLASH_LITE_FLEX_WITH_FALLBACK")
    fake_handler = object()
    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        return_value=fake_handler,
    ) as factory:
        result = await module.get_check_user_goal_llm_override("wr_123", "org_1")

    assert result is fake_handler
    factory.assert_called_once_with("GEMINI_3_1_FLASH_LITE_FLEX_WITH_FALLBACK")


@pytest.mark.asyncio
async def test_invalid_variant_logs_warning_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid llm_key is operator misconfiguration, not an unexpected exception."""
    _stub_experimentation_provider(monkeypatch, "NOT_A_REAL_LLM_KEY")
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        side_effect=RuntimeError("unknown config"),
    ):
        result = await module.get_check_user_goal_llm_override("wr_123", "org_1")

    assert result is None
    warning_calls = [c for level, c in calls if level == "warning"]
    error_calls = [c for level, c in calls if level == "error"]
    assert warning_calls, "Expected one LOG.warning for invalid variant"
    assert not error_calls, "Misconfigured variant should NOT log at error level"
    # No exc_info should be passed (stack trace adds no signal for a known failure mode).
    assert "exc_info" not in warning_calls[0]


@pytest.mark.asyncio
async def test_provider_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MagicMock()
    provider.get_value_cached = AsyncMock(side_effect=RuntimeError("posthog down"))
    fake_app = MagicMock()
    fake_app.EXPERIMENTATION_PROVIDER = provider
    monkeypatch.setattr(module, "app", fake_app)

    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    result = await module.get_check_user_goal_llm_override("wr_123", "org_1")

    assert result is None
    warning_calls = [c for level, c in calls if level == "warning"]
    assert warning_calls, "Expected one LOG.warning when the provider raises"


@pytest.mark.asyncio
async def test_passes_organization_id_and_distinct_id_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _stub_experimentation_provider(monkeypatch, None)

    await module.get_check_user_goal_llm_override("wr_999", "org_42")

    provider.get_value_cached.assert_awaited_once_with(
        "CHECK_USER_GOAL_LLM_NAME",
        "wr_999",
        properties={"organization_id": "org_42"},
    )


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_resolution_cached_per_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls for the same variant -> factory invoked once."""
    _stub_experimentation_provider(monkeypatch, "GEMINI_3_1_FLASH_LITE_FLEX_WITH_FALLBACK")
    fake_handler = object()
    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        return_value=fake_handler,
    ) as factory:
        first = await module.get_check_user_goal_llm_override("wr_123", "org_1")
        second = await module.get_check_user_goal_llm_override("wr_456", "org_2")

    assert first is fake_handler
    assert second is fake_handler
    factory.assert_called_once()


@pytest.mark.asyncio
async def test_invalid_variant_logs_warning_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Misconfigured variant called twice -> factory called twice, warning logged once.

    Factory is NOT negative-cached (success-only cache). A transient failure on a
    valid variant can recover next call.
    """
    _stub_experimentation_provider(monkeypatch, "NOT_A_REAL_LLM_KEY")
    log, calls = _make_log_recorder()
    monkeypatch.setattr(module, "LOG", log)

    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        side_effect=RuntimeError("unknown config"),
    ) as factory:
        first = await module.get_check_user_goal_llm_override("wr_123", "org_1")
        second = await module.get_check_user_goal_llm_override("wr_456", "org_2")

    assert first is None
    assert second is None
    assert factory.call_count == 2, "Failure NOT negative-cached; factory must be re-tried"
    warning_calls = [c for level, c in calls if level == "warning"]
    assert len(warning_calls) == 1, f"Expected exactly one warning across two calls; got {len(warning_calls)}"


@pytest.mark.asyncio
async def test_transient_failure_then_success_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient factory error on a valid variant must not permanently disable it."""
    _stub_experimentation_provider(monkeypatch, "GEMINI_3_1_FLASH_LITE_FLEX_WITH_FALLBACK")
    fake_handler = object()
    with patch(
        "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
        side_effect=[RuntimeError("transient"), fake_handler, fake_handler],
    ) as factory:
        first = await module.get_check_user_goal_llm_override("wr_1", "org_1")
        second = await module.get_check_user_goal_llm_override("wr_1", "org_1")
        third = await module.get_check_user_goal_llm_override("wr_1", "org_1")

    assert first is None
    assert second is fake_handler
    assert third is fake_handler
    # Factory: 1st (raises), 2nd (success — cached), 3rd hits cache.
    assert factory.call_count == 2


# ---------------------------------------------------------------------------
# resolve_check_user_goal_handler — shared resolver used by both call sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_default_when_override_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control arm: default handler is returned untouched (no flex wrap)."""
    _stub_experimentation_provider(monkeypatch, None)
    default_handler = object()

    result = await module.resolve_check_user_goal_handler("wr_123", "org_1", default_handler)

    assert result is default_handler


@pytest.mark.asyncio
async def test_resolve_wraps_override_with_flex_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treatment arm: override is wrapped with wrap_for_flex_routing for consistency
    across both call sites (complete_verify and the after-click verifier)."""
    _stub_experimentation_provider(monkeypatch, "GEMINI_3_1_FLASH_LITE_FLEX_WITH_FALLBACK")
    override_handler = MagicMock()
    flex_wrapped = object()

    with (
        patch(
            "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.get_llm_api_handler",
            return_value=override_handler,
        ),
        patch(
            "skyvern.forge.sdk.experimentation.llm_prompt_config.LLMAPIHandlerFactory.wrap_for_flex_routing",
            return_value=flex_wrapped,
        ) as wrap_call,
    ):
        result = await module.resolve_check_user_goal_handler("wr_123", "org_1", object())

    assert result is flex_wrapped
    wrap_call.assert_called_once_with(override_handler)


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default_when_override_helper_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If anything in the override path raises unexpectedly, return default."""
    default_handler = object()

    async def _raise(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(module, "get_check_user_goal_llm_override", _raise)

    result = await module.resolve_check_user_goal_handler("wr_123", "org_1", default_handler)

    assert result is default_handler
