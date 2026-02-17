"""Tests for the location auto-completion fast-path optimisation.

When the user types an address into a location field and exactly one autocomplete
suggestion appears, we skip the LLM call and click the suggestion directly.
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
    choose_auto_completion_dropdown,
    input_or_auto_complete_input,
)
from skyvern.webeye.actions.responses import ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={"address": "123 Main St"})
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

SINGLE_ELEMENT = [{"id": "AAAA", "tag": "div", "text": "123 Main St, Springfield, IL"}]
MULTI_ELEMENTS = [
    {"id": "AAAA", "tag": "div", "text": "123 Main St, Springfield, IL"},
    {"id": "AAAB", "tag": "div", "text": "123 Main St, Springfield, MO"},
]


def _make_location_context(**overrides: object) -> InputOrSelectContext:
    defaults = {
        "field": "Address",
        "is_location_input": True,
        "is_search_bar": False,
    }
    defaults.update(overrides)
    return InputOrSelectContext(**defaults)


def _make_non_location_context(**overrides: object) -> InputOrSelectContext:
    defaults = {
        "field": "Search",
        "is_location_input": False,
        "is_search_bar": False,
    }
    defaults.update(overrides)
    return InputOrSelectContext(**defaults)


def _mock_skyvern_element(frame: MagicMock | None = None) -> MagicMock:
    """Return a mock SkyvernElement whose helpers are async-safe."""
    el = MagicMock()
    el.get_id.return_value = "elem-1"
    el.get_frame.return_value = frame or _mock_frame()
    el.get_frame_id.return_value = "frame-1"
    el.is_interactable.return_value = True
    el.press_fill = AsyncMock()
    el.input_clear = AsyncMock()
    el.is_visible = AsyncMock(return_value=True)
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    return el


def _mock_frame(locator_count: int = 1) -> MagicMock:
    """Return a mock Playwright Frame with a configurable locator."""
    frame = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=locator_count)
    locator.click = AsyncMock()
    frame.locator.return_value = locator
    return frame


def _mock_incremental_scrape(elements: list[dict]) -> MagicMock:
    """Return a mock IncrementalScrapePage that yields *elements*."""
    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=copy.deepcopy(elements))
    inc.build_html_tree.return_value = "<div>mocked</div>"
    return inc


# ---------------------------------------------------------------------------
# Tests for choose_auto_completion_dropdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_location_single_option_skips_llm() -> None:
    """When is_location_input=True and exactly 1 option appears, the LLM must NOT be called."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

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

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        # The LLM should never have been called
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()

        # The locator should have been clicked
        frame.locator.assert_called_with(f'[{SKYVERN_ID_ATTR}="AAAA"]')
        frame.locator.return_value.click.assert_awaited_once()

        # Result should indicate success
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_location_whitespace_normalized_still_matches() -> None:
    """Input with extra whitespace should still match after normalization."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    # Option has single spaces, input will have double spaces
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

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

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123  Main  St",  # Double spaces - should still match after normalization
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        # LLM should NOT be called - whitespace normalization should make it match
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_location_multiple_options_calls_llm() -> None:
    """When is_location_input=True but multiple options appear, the LLM IS called."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(MULTI_ELEMENTS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.95,
        "id": "AAAA",
        "direct_searching": False,
        "reasoning": "First option matches",
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
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        # LLM should have been called because there are 2 options
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_location_single_option_calls_llm() -> None:
    """When is_location_input=False, even a single option goes through the LLM path."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.95,
        "id": "AAAA",
        "direct_searching": False,
        "reasoning": "Matches",
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
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="some search",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=False,
        )

        # LLM should be called — no fast-path for non-location inputs
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()


@pytest.mark.asyncio
async def test_location_fast_path_returns_action_success() -> None:
    """The fast-path must set action_result to ActionSuccess on the result object."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

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

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        assert isinstance(result, AutoCompletionResult)
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_location_fast_path_element_not_in_dom_falls_through() -> None:
    """If the single element's locator has count 0, the fast-path is skipped."""
    frame = _mock_frame(locator_count=0)  # element not found in DOM
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.95,
        "id": "AAAA",
        "direct_searching": False,
        "reasoning": "Matches",
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
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        # Should fall through to LLM path because locator.count() == 0
        await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests for input_or_auto_complete_input flag propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_or_auto_complete_passes_is_location_input() -> None:
    """input_or_auto_complete_input must forward is_location_input to choose_auto_completion_dropdown."""
    context = _make_location_context()

    with patch(
        "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
        new=AsyncMock(return_value=AutoCompletionResult(action_result=ActionSuccess())),
    ) as mock_choose:
        result = await input_or_auto_complete_input(
            input_or_select_context=context,
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=_mock_skyvern_element(),
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        # Verify is_location_input was passed
        call_kwargs = mock_choose.call_args.kwargs
        assert call_kwargs["is_location_input"] is True


@pytest.mark.asyncio
async def test_input_or_auto_complete_passes_false_for_non_location() -> None:
    """When is_location_input is None/False, the flag should be passed as False."""
    context = _make_non_location_context()

    with patch(
        "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
        new=AsyncMock(return_value=AutoCompletionResult(action_result=ActionSuccess())),
    ) as mock_choose:
        result = await input_or_auto_complete_input(
            input_or_select_context=context,
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="some query",
            skyvern_element=_mock_skyvern_element(),
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        call_kwargs = mock_choose.call_args.kwargs
        assert call_kwargs["is_location_input"] is False


# ---------------------------------------------------------------------------
# Integration tests: options that don't contain the input fall through to LLM
# ---------------------------------------------------------------------------

NO_RESULT_ELEMENTS = [{"id": "AAAA", "tag": "div", "text": "No results"}]
UNRELATED_ELEMENTS = [{"id": "AAAA", "tag": "div", "text": "Something completely different"}]


@pytest.mark.asyncio
async def test_location_no_results_option_falls_through_to_llm() -> None:
    """When the single option doesn't contain the input text, fall through to LLM."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(NO_RESULT_ELEMENTS)

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "reasoning": "No results shown",
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
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        # LLM should be called because "No results" doesn't contain "123 Main St"
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()


@pytest.mark.asyncio
async def test_location_unrelated_option_falls_through_to_llm() -> None:
    """When the single option doesn't contain the input text, fall through to LLM."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(UNRELATED_ELEMENTS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.5,
        "id": "AAAA",
        "direct_searching": False,
        "reasoning": "Only option available",
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
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        # LLM should be called because option doesn't contain the input
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
