"""Tests for the discover-all-options fallback in auto-completion.

When all potential values fail to substring-match any dropdown option,
the fallback clears the input, presses ArrowDown to reveal all options,
and asks the LLM to pick the best semantic match.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext
from skyvern.webeye.actions.handler import (
    AutoCompletionResult,
    discover_and_select_from_full_dropdown,
    input_or_auto_complete_input,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(
    _NOW,
    _ORG,
    navigation_payload={"gender": "Decline to Self Identify"},
    navigation_goal="Fill out the job application form",
)
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

DROPDOWN_OPTIONS = [
    {"id": "OPT1", "tag": "div", "text": "Female"},
    {"id": "OPT2", "tag": "div", "text": "Male"},
    {"id": "OPT3", "tag": "div", "text": "I do not wish to disclose"},
]


def _make_context(**overrides: object) -> InputOrSelectContext:
    defaults = {
        "field": "Sex",
        "is_location_input": False,
        "is_search_bar": False,
    }
    defaults.update(overrides)
    return InputOrSelectContext(**defaults)


def _mock_skyvern_element(frame: MagicMock | None = None) -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = "elem-sex"
    el.get_frame.return_value = frame or _mock_frame()
    el.get_frame_id.return_value = "frame-1"
    el.is_interactable.return_value = True
    el.press_fill = AsyncMock()
    el.press_key = AsyncMock()
    el.input_clear = AsyncMock()
    el.scroll_into_view = AsyncMock()
    el.is_visible = AsyncMock(return_value=True)
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    el.click = AsyncMock()
    return el


def _mock_frame(locator_count: int = 1) -> MagicMock:
    frame = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=locator_count)
    locator.click = AsyncMock()
    locator.element_handle = AsyncMock(return_value=MagicMock())
    locator.bounding_box = AsyncMock(return_value={"x": 0, "y": 0, "width": 100, "height": 30})
    locator.scroll_into_view_if_needed = AsyncMock()
    # SkyvernElement.is_visible() needs locator.is_visible()
    locator.is_visible = AsyncMock(return_value=True)
    frame.locator.return_value = locator
    frame.evaluate = AsyncMock(return_value=None)
    return frame


def _mock_incremental_scrape(elements: list[dict]) -> MagicMock:
    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=copy.deepcopy(elements))
    inc.build_html_tree.return_value = "<div>mocked options</div>"
    inc.id_to_element_dict = {e["id"]: e for e in elements}
    return inc


# ---------------------------------------------------------------------------
# Tests for discover_and_select_from_full_dropdown
# ---------------------------------------------------------------------------


def _mock_selected_element() -> MagicMock:
    """Return a mock for the SkyvernElement created for the selected dropdown option."""
    el = MagicMock()
    el.scroll_into_view = AsyncMock()
    el.click = AsyncMock()
    return el


@pytest.mark.asyncio
async def test_discover_fallback_succeeds_when_options_appear() -> None:
    """ArrowDown reveals options, LLM picks a match with relevance >= 0.6 → ActionSuccess."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "reasoning": "'I do not wish to disclose' matches 'Decline to Self Identify'",
    }

    mock_selected_el = _mock_selected_element()

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch("skyvern.webeye.actions.handler.SkyvernElement", return_value=mock_selected_el),
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        skyvern_el.input_clear.assert_awaited()
        skyvern_el.press_key.assert_awaited_with("ArrowDown")
        frame.locator.assert_called_with(f'[{SKYVERN_ID_ATTR}="OPT3"]')
        mock_selected_el.scroll_into_view.assert_awaited()
        mock_selected_el.click.assert_awaited()
        inc_scrape.stop_listen_dom_increment.assert_awaited()


@pytest.mark.asyncio
async def test_discover_fallback_returns_none_when_no_options() -> None:
    """ArrowDown produces no incremental elements → returns None."""
    skyvern_el = _mock_skyvern_element()
    inc_scrape = _mock_incremental_scrape([])  # No options appear

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
        inc_scrape.stop_listen_dom_increment.assert_awaited()


@pytest.mark.asyncio
async def test_discover_fallback_returns_none_when_relevance_too_low() -> None:
    """LLM picks an option but with relevance below threshold → returns None."""
    skyvern_el = _mock_skyvern_element()
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.3,
        "id": "OPT1",
        "reasoning": "Female doesn't match Decline to Self Identify",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None


@pytest.mark.asyncio
async def test_discover_fallback_returns_none_when_llm_returns_no_id() -> None:
    """LLM returns empty id (no suitable option) → returns None."""
    skyvern_el = _mock_skyvern_element()
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 1.0,
        "id": "",
        "reasoning": "No suitable match found",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None


@pytest.mark.asyncio
async def test_discover_fallback_returns_none_when_element_invisible() -> None:
    """If the element is not visible, fallback should bail out immediately."""
    skyvern_el = _mock_skyvern_element()
    skyvern_el.is_visible = AsyncMock(return_value=False)

    result = await discover_and_select_from_full_dropdown(
        context=_make_context(),
        page=MagicMock(),
        dom=MagicMock(),
        original_text="Decline to Self Identify",
        skyvern_element=skyvern_el,
        step=_STEP,
        task=_TASK,
    )

    assert result is None


@pytest.mark.asyncio
async def test_discover_fallback_returns_none_when_element_not_in_dom() -> None:
    """LLM picks a valid option but the element is no longer in the DOM → returns None."""
    frame = _mock_frame(locator_count=0)  # Element not found
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "reasoning": "Match found",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None


@pytest.mark.asyncio
async def test_discover_fallback_handles_arrow_down_timeout() -> None:
    """If ArrowDown times out, fallback should continue (not crash)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    skyvern_el.press_key = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "reasoning": "Match found",
    }

    mock_selected_el = _mock_selected_element()

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            return_value=inc_scrape,
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch("skyvern.webeye.actions.handler.SkyvernElement", return_value=mock_selected_el),
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        # Should still succeed — ArrowDown timeout is handled gracefully
        assert isinstance(result, ActionSuccess)


# ---------------------------------------------------------------------------
# Tests for integration with input_or_auto_complete_input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_or_auto_complete_calls_discover_fallback_on_failure() -> None:
    """When all auto-completion attempts fail, the discover fallback is called."""
    skyvern_el = _mock_skyvern_element()

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(
                return_value=AutoCompletionResult(action_result=ActionFailure(exception=Exception("no match")))
            ),
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=ActionSuccess()),
        ) as mock_discover,
    ):
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(
            return_value={
                "potential_values": [
                    {"value": "I prefer not to answer", "relevance_float": 0.9, "reasoning": "synonym"},
                ]
            }
        )
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        mock_discover.assert_awaited_once()


@pytest.mark.asyncio
async def test_input_or_auto_complete_skips_discover_for_search_bar() -> None:
    """Search bars should NOT trigger the discover fallback."""
    skyvern_el = _mock_skyvern_element()

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(
                return_value=AutoCompletionResult(action_result=ActionFailure(exception=Exception("no match")))
            ),
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=ActionSuccess()),
        ) as mock_discover,
    ):
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        # Note: search bars bail out early at L2989 (before potential_values),
        # so discover_fallback is never reached. This test verifies that.
        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_search_bar=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        # Search bar returns None early, never reaching discover fallback
        assert result is None
        mock_discover.assert_not_called()
