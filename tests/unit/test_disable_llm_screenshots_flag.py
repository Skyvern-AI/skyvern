from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.api.llm.api_handler_factory import (
    _llm_screenshots_enabled_metric,
    _llm_screenshots_for_call,
    _llm_vision_log_fields,
)
from skyvern.forge.sdk.core.skyvern_context import LLMVisionMode, SkyvernContext, parse_llm_vision_mode
from skyvern.forge.sdk.experimentation import llm_vision_mode
from skyvern.schemas.llm import LLMConfig


def _config(*, supports_vision: bool = True) -> MagicMock:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = supports_vision
    return config


def _step(retry_index: int) -> SimpleNamespace:
    return SimpleNamespace(retry_index=retry_index)


@pytest.mark.parametrize(
    ("mode", "retry_index", "screenshots_enabled", "accessibility_enabled", "fallback_active"),
    [
        (LLMVisionMode.CONTROL, 0, True, False, False),
        (LLMVisionMode.CONTROL, 1, True, False, False),
        (LLMVisionMode.NO_IMAGES_WITH_A11Y, 0, False, True, False),
        (LLMVisionMode.NO_IMAGES_WITH_A11Y, 1, False, True, False),
        (LLMVisionMode.FALLBACK_WITH_A11Y, 0, False, True, False),
        (LLMVisionMode.FALLBACK_WITH_A11Y, 1, True, True, True),
        (LLMVisionMode.FALLBACK_WITHOUT_A11Y, 0, False, False, False),
        (LLMVisionMode.FALLBACK_WITHOUT_A11Y, 1, True, False, True),
    ],
)
def test_llm_vision_modes_control_screenshots_and_accessibility_context(
    mode: LLMVisionMode,
    retry_index: int,
    screenshots_enabled: bool,
    accessibility_enabled: bool,
    fallback_active: bool,
) -> None:
    ctx = SkyvernContext(llm_vision_mode=mode, step_retry_index=retry_index)
    expected_screenshots = [b"png"] if screenshots_enabled else None

    assert _llm_screenshots_for_call([b"png"], _config(), ctx) == expected_screenshots
    assert _llm_screenshots_enabled_metric(_config(), ctx) is screenshots_enabled
    assert ctx.llm_accessibility_context_enabled() is accessibility_enabled
    assert ctx.llm_vision_fallback_active() is fallback_active

    log_fields = _llm_vision_log_fields(ctx)
    assert log_fields["llm_vision_mode"] == mode.value
    assert log_fields["llm_vision_fallback_active"] is fallback_active
    assert log_fields["llm_accessibility_context_enabled"] is accessibility_enabled


def test_legacy_disable_llm_screenshots_still_maps_to_no_images_with_a11y() -> None:
    ctx = SkyvernContext(disable_llm_screenshots=True)

    assert ctx.effective_llm_vision_mode() == LLMVisionMode.NO_IMAGES_WITH_A11Y
    assert _llm_screenshots_for_call([b"png"], _config(), ctx) is None
    assert _llm_screenshots_enabled_metric(_config(), ctx) is False
    assert ctx.llm_accessibility_context_enabled() is True


@pytest.mark.parametrize(
    "mode",
    [
        LLMVisionMode.NO_IMAGES_WITH_A11Y,
        LLMVisionMode.FALLBACK_WITH_A11Y,
        LLMVisionMode.FALLBACK_WITHOUT_A11Y,
    ],
)
def test_vision_fallback_prompt_keeps_screenshots_in_no_image_modes(mode: LLMVisionMode) -> None:
    ctx = SkyvernContext(llm_vision_mode=mode)

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, "extract-text-from-image") == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), ctx, "extract-text-from-image") is True


def test_step_argument_controls_fallback_for_speculative_next_step() -> None:
    ctx = SkyvernContext(
        llm_vision_mode=LLMVisionMode.FALLBACK_WITHOUT_A11Y,
        step_retry_index=2,
    )

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, step=_step(0)) is None
    assert _llm_screenshots_enabled_metric(_config(), ctx, step=_step(0)) is False
    assert _llm_vision_log_fields(ctx, _step(0))["llm_vision_fallback_active"] is False

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, step=_step(1)) == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), ctx, step=_step(1)) is True
    assert _llm_vision_log_fields(ctx, _step(1))["llm_vision_fallback_active"] is True


def test_non_vision_model_never_attaches() -> None:
    ctx = SkyvernContext(llm_vision_mode=LLMVisionMode.CONTROL)

    assert _llm_screenshots_for_call([b"png"], _config(supports_vision=False), ctx) is None
    assert _llm_screenshots_enabled_metric(_config(supports_vision=False), ctx) is False


def test_none_context_treated_as_screenshots_enabled_for_vision() -> None:
    assert _llm_screenshots_for_call([b"png"], _config(), None) == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), None) is True
    assert _llm_vision_log_fields(None) == {
        "llm_vision_mode": LLMVisionMode.CONTROL.value,
        "llm_vision_fallback_active": False,
        "llm_accessibility_context_enabled": False,
    }


def test_none_screenshots_input_still_reports_cohort_when_vision() -> None:
    ctx = SkyvernContext(llm_vision_mode=LLMVisionMode.CONTROL)

    assert _llm_screenshots_for_call(None, _config(), ctx) is None
    assert _llm_screenshots_enabled_metric(_config(), ctx) is True


def test_invalid_or_missing_llm_vision_mode_defaults_to_control() -> None:
    assert parse_llm_vision_mode(None) == LLMVisionMode.CONTROL
    assert parse_llm_vision_mode("unknown-mode") == LLMVisionMode.CONTROL
    assert parse_llm_vision_mode("control") == LLMVisionMode.CONTROL


def test_set_llm_vision_mode_keeps_legacy_disable_flag_in_sync() -> None:
    ctx = SkyvernContext(disable_llm_screenshots=True)

    ctx.set_llm_vision_mode(LLMVisionMode.CONTROL)

    assert ctx.effective_llm_vision_mode() == LLMVisionMode.CONTROL
    assert ctx.disable_llm_screenshots is False


@pytest.mark.asyncio
async def test_resolve_llm_vision_mode_for_context_uses_run_distinct_id_and_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class Provider:
        async def get_value_cached(
            self,
            feature_name: str,
            distinct_id: str,
            *,
            properties: dict[str, str],
        ) -> str:
            calls.append(
                {
                    "feature_name": feature_name,
                    "distinct_id": distinct_id,
                    "properties": properties,
                }
            )
            return LLMVisionMode.FALLBACK_WITH_A11Y.value

    monkeypatch.setattr(llm_vision_mode.app, "EXPERIMENTATION_PROVIDER", Provider())
    monkeypatch.delenv("FORCE_DISABLE_LLM_SCREENSHOTS", raising=False)
    ctx = SkyvernContext()

    await llm_vision_mode.resolve_llm_vision_mode_for_context(
        ctx,
        "workflow-run-id",
        "organization-id",
        workflow_permanent_id="workflow-permanent-id",
    )

    assert ctx.llm_vision_mode == LLMVisionMode.FALLBACK_WITH_A11Y
    assert calls == [
        {
            "feature_name": "llm_vision_mode",
            "distinct_id": "workflow-run-id",
            "properties": {
                "organization_id": "organization-id",
                "workflow_permanent_id": "workflow-permanent-id",
            },
        }
    ]


@pytest.mark.asyncio
async def test_resolve_llm_vision_mode_for_context_defaults_invalid_values_to_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def get_value_cached(
            self,
            _feature_name: str,
            _distinct_id: str,
            *,
            properties: dict[str, str],
        ) -> str:
            assert properties == {"organization_id": "organization-id"}
            return "not-a-real-mode"

    monkeypatch.setattr(llm_vision_mode.app, "EXPERIMENTATION_PROVIDER", Provider())
    monkeypatch.delenv("FORCE_DISABLE_LLM_SCREENSHOTS", raising=False)
    ctx = SkyvernContext(llm_vision_mode=LLMVisionMode.NO_IMAGES_WITH_A11Y)

    await llm_vision_mode.resolve_llm_vision_mode_for_context(ctx, "task-id", "organization-id")

    assert ctx.llm_vision_mode == LLMVisionMode.CONTROL
    assert ctx.disable_llm_screenshots is False
