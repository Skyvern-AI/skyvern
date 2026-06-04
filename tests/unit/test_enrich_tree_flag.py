from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.api.llm.api_handler_factory import (
    _enrich_tree_log_fields,
    _llm_screenshots_enabled_metric,
    _llm_screenshots_for_call,
)
from skyvern.forge.sdk.core.skyvern_context import EnrichTreeMode, SkyvernContext, parse_enrich_tree_mode
from skyvern.schemas.llm import LLMConfig


def _config(*, supports_vision: bool = True) -> MagicMock:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = supports_vision
    return config


def _step(retry_index: int) -> SimpleNamespace:
    return SimpleNamespace(retry_index=retry_index)


@pytest.mark.parametrize(
    ("mode", "retry_index", "screenshots_enabled", "enriched_enabled", "fallback_active"),
    [
        (EnrichTreeMode.CONTROL, 0, True, False, False),
        (EnrichTreeMode.CONTROL, 1, True, False, False),
        (EnrichTreeMode.ENRICHED_TREE, 0, True, True, False),
        (EnrichTreeMode.ENRICHED_TREE, 1, True, True, False),
        (EnrichTreeMode.ENRICHED_TREE_NO_IMAGES, 0, False, True, False),
        (EnrichTreeMode.ENRICHED_TREE_NO_IMAGES, 1, False, True, False),
        (EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK, 0, False, True, False),
        (EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK, 1, True, True, True),
    ],
)
def test_enrich_tree_modes_control_screenshots_and_enrichment(
    mode: EnrichTreeMode,
    retry_index: int,
    screenshots_enabled: bool,
    enriched_enabled: bool,
    fallback_active: bool,
) -> None:
    ctx = SkyvernContext(enrich_tree_mode=mode, step_retry_index=retry_index)
    expected_screenshots = [b"png"] if screenshots_enabled else None

    assert _llm_screenshots_for_call([b"png"], _config(), ctx) == expected_screenshots
    assert _llm_screenshots_enabled_metric(_config(), ctx) is screenshots_enabled
    assert ctx.enriched_tree_enabled() is enriched_enabled
    assert ctx.enrich_tree_fallback_active() is fallback_active

    log_fields = _enrich_tree_log_fields(ctx)
    assert log_fields["enrich_tree_mode"] == mode.value
    assert log_fields["enrich_tree_fallback_active"] is fallback_active
    assert log_fields["enriched_tree_enabled"] is enriched_enabled


@pytest.mark.parametrize(
    "mode",
    [
        EnrichTreeMode.ENRICHED_TREE_NO_IMAGES,
        EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK,
    ],
)
def test_vision_fallback_prompt_keeps_screenshots_in_no_image_modes(mode: EnrichTreeMode) -> None:
    ctx = SkyvernContext(enrich_tree_mode=mode)

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, "extract-text-from-image") == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), ctx, "extract-text-from-image") is True


def test_step_argument_controls_fallback_for_speculative_next_step() -> None:
    ctx = SkyvernContext(
        enrich_tree_mode=EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK,
        step_retry_index=2,
    )

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, step=_step(0)) is None
    assert _llm_screenshots_enabled_metric(_config(), ctx, step=_step(0)) is False
    assert _enrich_tree_log_fields(ctx, _step(0))["enrich_tree_fallback_active"] is False

    assert _llm_screenshots_for_call([b"png"], _config(), ctx, step=_step(1)) == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), ctx, step=_step(1)) is True
    assert _enrich_tree_log_fields(ctx, _step(1))["enrich_tree_fallback_active"] is True


def test_non_vision_model_never_attaches() -> None:
    ctx = SkyvernContext(enrich_tree_mode=EnrichTreeMode.CONTROL)

    assert _llm_screenshots_for_call([b"png"], _config(supports_vision=False), ctx) is None
    assert _llm_screenshots_enabled_metric(_config(supports_vision=False), ctx) is False


def test_none_context_treated_as_screenshots_enabled_for_vision() -> None:
    assert _llm_screenshots_for_call([b"png"], _config(), None) == [b"png"]
    assert _llm_screenshots_enabled_metric(_config(), None) is True
    assert _enrich_tree_log_fields(None) == {
        "enrich_tree_mode": EnrichTreeMode.CONTROL.value,
        "enrich_tree_fallback_active": False,
        "enriched_tree_enabled": False,
    }


def test_none_screenshots_input_still_reports_cohort_when_vision() -> None:
    ctx = SkyvernContext(enrich_tree_mode=EnrichTreeMode.CONTROL)

    assert _llm_screenshots_for_call(None, _config(), ctx) is None
    assert _llm_screenshots_enabled_metric(_config(), ctx) is True


def test_invalid_or_missing_enrich_tree_mode_defaults_to_control() -> None:
    assert parse_enrich_tree_mode(None) == EnrichTreeMode.CONTROL
    assert parse_enrich_tree_mode("unknown-mode") == EnrichTreeMode.CONTROL
    assert parse_enrich_tree_mode("control") == EnrichTreeMode.CONTROL


def test_extract_action_prompt_uses_llm_screenshots_enabled_not_non_vision() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module

    with_screenshots = engine_module.load_prompt(
        "extract-action",
        llm_screenshots_enabled=True,
        elements="<input id='a'>",
        navigation_goal="Submit form",
        navigation_payload_str="{}",
        action_history="",
        local_datetime="2026-05-27T12:00:00",
    )
    without_screenshots = engine_module.load_prompt(
        "extract-action",
        llm_screenshots_enabled=False,
        enriched_tree_enabled=True,
        elements="<input id='a' validationMessage='Invalid email'>",
        navigation_goal="Submit form",
        navigation_payload_str="{}",
        action_history="",
        local_datetime="2026-05-27T12:00:00",
    )

    assert "non_vision_page_context" not in with_screenshots
    assert "non_vision_page_context" not in without_screenshots
    assert "screenshot of the website" in with_screenshots
    assert "screenshot of the website" not in without_screenshots
    assert "validation messages" in without_screenshots
