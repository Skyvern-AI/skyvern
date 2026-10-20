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

from skyvern.exceptions import NoIncrementalElementFoundForAutoCompletion, NoSuitableAutoCompleteOption
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext
from skyvern.webeye.actions.handler import (
    AutoCompletionResult,
    choose_auto_completion_dropdown,
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
    # get_locator().click() needs to be async for the discover fallback's click call
    mock_locator = MagicMock()
    mock_locator.click = AsyncMock()
    el.get_locator.return_value = mock_locator
    return el


def _mock_frame(locator_count: int = 1) -> MagicMock:
    frame = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=locator_count)
    locator.click = AsyncMock()
    locator.element_handle = AsyncMock(return_value=MagicMock())
    locator.bounding_box = AsyncMock(return_value={"x": 0, "y": 0, "width": 100, "height": 30})
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.is_visible = AsyncMock(return_value=True)
    frame.locator.return_value = locator
    frame.evaluate = AsyncMock(return_value=None)
    # locator.first.click needs to be async for option clicking
    locator.first = MagicMock()
    locator.first.click = AsyncMock()
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
    """Click reveals options, LLM picks a match, then types discovered value and clicks matched option."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)
    selection = object()
    cleanup_factory = MagicMock(return_value=AsyncMock())

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "value": "I do not wish to disclose",
        "reasoning": "'I do not wish to disclose' matches 'Decline to Self Identify'",
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
        patch(
            "skyvern.webeye.actions.handler.resolve_engine_selection_for_task",
            return_value=selection,
        ) as resolve,
        patch(
            "skyvern.webeye.actions.handler.clean_and_remove_element_tree_factory",
            cleanup_factory,
        ),
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await discover_and_select_from_full_dropdown(
            context=_make_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        skyvern_el.input_clear.assert_awaited()
        skyvern_el.get_locator().click.assert_awaited()
        # Types the discovered value, then finds and clicks matched option via Playwright locator
        skyvern_el.press_fill.assert_awaited_with("I do not wish to disclose")
        inc_scrape.stop_listen_dom_increment.assert_awaited()
        resolve.assert_called_once_with(_TASK, mock_app.BROWSER_MANAGER)
        assert cleanup_factory.call_count == 1
        assert cleanup_factory.call_args.kwargs["engine_selection"] is selection


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
            scraped_page=MagicMock(),
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
            scraped_page=MagicMock(),
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
            scraped_page=MagicMock(),
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
        scraped_page=MagicMock(),
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
            scraped_page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None


@pytest.mark.asyncio
async def test_discover_fallback_handles_arrow_down_timeout() -> None:
    """If click yields no options and ArrowDown times out, fallback succeeds when options found after ArrowDown."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    # ArrowDown for opening dropdown times out, but subsequent keyboard presses succeed
    call_count = 0

    async def press_key_side_effect(key, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PlaywrightTimeout("timeout")

    skyvern_el.press_key = AsyncMock(side_effect=press_key_side_effect)
    # Click yields no options on first call, ArrowDown (despite timeout) yields options on second
    inc_scrape = _mock_incremental_scrape([])
    # 3 calls: click→empty, ArrowDown→options, press_fill→options (for select_one_element_by_value)
    inc_scrape.get_incremental_element_tree = AsyncMock(
        side_effect=[[], copy.deepcopy(DROPDOWN_OPTIONS), copy.deepcopy(DROPDOWN_OPTIONS)]
    )
    inc_scrape.id_to_element_dict = {e["id"]: e for e in DROPDOWN_OPTIONS}

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "value": "I do not wish to disclose",
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
            scraped_page=MagicMock(),
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        # Should still succeed — ArrowDown timeout is handled gracefully
        assert isinstance(result, ActionSuccess)


# ---------------------------------------------------------------------------
# Tests for re-scrape diff fallback (Strategy 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_fallback_rescrape_diff_succeeds() -> None:
    """When click and ArrowDown both return empty, re-scrape diff finds new elements and keyboard selects."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    # Both click and ArrowDown produce no incremental elements
    inc_scrape = _mock_incremental_scrape([])
    inc_scrape.get_incremental_element_tree = AsyncMock(
        side_effect=[[], [], copy.deepcopy(DROPDOWN_OPTIONS)]  # click→empty, ArrowDown→empty, press_fill→options
    )
    inc_scrape.id_to_element_dict = {}

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.95,
        "id": "OPT3",
        "value": "I do not wish to disclose",
        "reasoning": "Best semantic match",
    }

    # Mock scraped_page for re-scrape diff
    mock_scraped_page = MagicMock()
    mock_scraped_page.id_to_css_dict = {"EXISTING1": "css1"}  # before scrape

    # After re-scrape: has new elements
    mock_scraped_after = MagicMock()
    mock_scraped_after.id_to_css_dict = {
        "EXISTING1": "css1",
        "OPT1": "css2",
        "OPT2": "css3",
        "OPT3": "css4",
    }
    mock_scraped_after.id_to_element_dict = {e["id"]: e for e in DROPDOWN_OPTIONS}
    mock_scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=mock_scraped_after)

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
            scraped_page=mock_scraped_page,
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert isinstance(result, ActionSuccess)
        # Verify re-scrape was triggered
        mock_scraped_page.generate_scraped_page_without_screenshots.assert_awaited_once()
        # Verify discovered value was typed + keyboard selection
        skyvern_el.press_fill.assert_awaited_with("I do not wish to disclose")
        # ArrowDown + Enter for keyboard selection
        assert skyvern_el.press_key.call_count >= 2


@pytest.mark.asyncio
async def test_discover_fallback_rescrape_diff_no_new_elements() -> None:
    """When re-scrape diff finds no new elements, returns None."""
    skyvern_el = _mock_skyvern_element()
    inc_scrape = _mock_incremental_scrape([])

    # Mock scraped_page where re-scrape returns same elements (no diff)
    mock_scraped_page = MagicMock()
    mock_scraped_page.id_to_css_dict = {"EXISTING1": "css1"}
    mock_scraped_after = MagicMock()
    mock_scraped_after.id_to_css_dict = {"EXISTING1": "css1"}  # same as before
    mock_scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=mock_scraped_after)

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
            scraped_page=mock_scraped_page,
            dom=MagicMock(),
            original_text="Decline to Self Identify",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

        assert result is None
        mock_scraped_page.generate_scraped_page_without_screenshots.assert_awaited_once()
        # LLM should not be called since no new elements
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for integration with input_or_auto_complete_input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_or_auto_complete_calls_discover_fallback_on_failure() -> None:
    """When all auto-completion attempts fail, the discover fallback is called."""
    skyvern_el = _mock_skyvern_element()
    probe = AsyncMock(
        return_value=AutoCompletionResult(
            incremental_elements=DROPDOWN_OPTIONS,
            action_result=ActionFailure(
                exception=NoSuitableAutoCompleteOption(reasoning=None, target_value="Decline to Self Identify")
            ),
        )
    )

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=probe),
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
                    {"value": "Prefer not to say", "relevance_float": 0.8, "reasoning": "synonym"},
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
            is_secret_value=False,
        )

        assert isinstance(result, ActionSuccess)
        assert [attempt.kwargs["text"] for attempt in probe.await_args_list] == [
            "Decline to Self Identify",
            "I prefer not to answer",
            "Prefer not to say",
        ]
        mock_discover.assert_awaited_once()
        mock_app.SECONDARY_LLM_API_HANDLER.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attributes", "tag_name", "is_location_input"),
    [
        ({"role": "combobox", "aria-autocomplete": "list"}, "input", False),
        ({"data-x-bind": "someAutocomplete"}, "input", False),
        ({"class": "autocomplete-input"}, "input", False),
        ({"class": "skillset tagsinput"}, "input", False),
        ({}, "input", True),
    ],
)
async def test_input_or_auto_complete_retries_candidate_for_query_only_typeahead(
    attributes: dict[str, str], tag_name: str, is_location_input: bool
) -> None:
    """Regression pin: query-only inputs can return California for CA despite no initial options."""
    skyvern_el = _mock_skyvern_element()
    skyvern_el.get_attributes.return_value = attributes
    skyvern_el.get_tag_name.return_value = tag_name
    probe = AsyncMock(
        side_effect=[
            AutoCompletionResult(
                action_result=ActionFailure(
                    exception=NoIncrementalElementFoundForAutoCompletion(element_id="elem-sex", text="CA")
                )
            ),
            AutoCompletionResult(action_result=ActionSuccess()),
        ]
    )

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=probe),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=ActionSuccess()),
        ) as mock_discover,
    ):
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(
            return_value={"potential_values": [{"value": "California", "relevance_float": 0.9}]}
        )
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(field="State", is_location_input=is_location_input),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="CA",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_secret_value=False,
        )

        assert isinstance(result, ActionSuccess)
        assert [attempt.kwargs["text"] for attempt in probe.await_args_list] == ["CA", "California"]
        assert probe.await_args_list[1].kwargs["allow_full_page_rescrape"] is False
        mock_app.SECONDARY_LLM_API_HANDLER.assert_awaited_once()
        mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_or_auto_complete_retries_remaining_candidates_after_empty_probe() -> None:
    """Keep trying generated queries until a query-only typeahead returns options."""
    skyvern_el = _mock_skyvern_element()
    probe = AsyncMock(
        side_effect=[
            AutoCompletionResult(
                action_result=ActionFailure(
                    exception=NoIncrementalElementFoundForAutoCompletion(element_id="elem-sex", text="Barnstable")
                )
            ),
            AutoCompletionResult(
                action_result=ActionFailure(
                    exception=NoIncrementalElementFoundForAutoCompletion(
                        element_id="elem-sex", text="Barnstable County"
                    )
                )
            ),
            AutoCompletionResult(action_result=ActionSuccess()),
        ]
    )

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=probe),
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
                    {"value": "Barnstable County", "relevance_float": 0.9},
                    {"value": "Barnstable Massachusetts", "relevance_float": 0.8},
                ]
            }
        )
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(field="County"),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="Barnstable",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_secret_value=False,
        )

        assert isinstance(result, ActionSuccess)
        assert [attempt.kwargs["text"] for attempt in probe.await_args_list] == [
            "Barnstable",
            "Barnstable County",
            "Barnstable Massachusetts",
        ]
        assert all(attempt.kwargs["allow_full_page_rescrape"] is False for attempt in probe.await_args_list[1:])
        mock_app.SECONDARY_LLM_API_HANDLER.assert_awaited_once()
        mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_or_auto_complete_continues_after_unmatched_candidate_options() -> None:
    skyvern_el = _mock_skyvern_element()
    no_options = AutoCompletionResult(
        action_result=ActionFailure(
            exception=NoIncrementalElementFoundForAutoCompletion(element_id="elem-sex", text="Barnstable")
        )
    )
    unmatched_options = AutoCompletionResult(
        incremental_elements=DROPDOWN_OPTIONS,
        action_result=ActionFailure(
            exception=NoSuitableAutoCompleteOption(reasoning=None, target_value="Barnstable County")
        ),
    )
    probe = AsyncMock(side_effect=[no_options, unmatched_options, AutoCompletionResult(action_result=ActionSuccess())])

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=probe),
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
                    {"value": "Barnstable County", "relevance_float": 0.9},
                    {"value": "Barnstable Massachusetts", "relevance_float": 0.8},
                ]
            }
        )
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(field="County"),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="Barnstable",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_secret_value=False,
        )

        assert isinstance(result, ActionSuccess)
        assert [attempt.kwargs["text"] for attempt in probe.await_args_list] == [
            "Barnstable",
            "Barnstable County",
            "Barnstable Massachusetts",
        ]
        assert all(attempt.kwargs["allow_full_page_rescrape"] is False for attempt in probe.await_args_list[1:])
        mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_or_auto_complete_uses_one_fallback_after_empty_candidate_probes() -> None:
    skyvern_el = _mock_skyvern_element()
    no_options = AutoCompletionResult(
        action_result=ActionFailure(
            exception=NoIncrementalElementFoundForAutoCompletion(element_id="elem-sex", text="")
        )
    )
    probe = AsyncMock(side_effect=[no_options, no_options, no_options])

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=probe),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=ActionSuccess()),
        ) as mock_discover,
    ):
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(
            return_value={"potential_values": [{"value": "Barnstable County"}, {"value": "Barnstable MA"}]}
        )
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(field="County"),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="Barnstable",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_secret_value=False,
        )

        assert isinstance(result, ActionSuccess)
        assert [attempt.kwargs["text"] for attempt in probe.await_args_list] == [
            "Barnstable",
            "Barnstable County",
            "Barnstable MA",
        ]
        assert all(attempt.kwargs["allow_full_page_rescrape"] is False for attempt in probe.await_args_list[1:])
        mock_app.SECONDARY_LLM_API_HANDLER.assert_awaited_once()
        mock_discover.assert_awaited_once()


@pytest.mark.asyncio
async def test_candidate_probe_uses_one_fill_without_full_rescrape_or_selection_llm() -> None:
    skyvern_el = _mock_skyvern_element()
    scraped_page = MagicMock()
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock()
    incremental_scrape = _mock_incremental_scrape([])
    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=incremental_scrape),
        patch("skyvern.webeye.actions.handler.resolve_engine_selection_for_task", return_value=None),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_context(),
            page=MagicMock(),
            scraped_page=scraped_page,
            dom=MagicMock(),
            text="Barnstable County",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            allow_full_page_rescrape=False,
            is_secret_value=False,
        )

        assert isinstance(result.action_result, ActionFailure)
        assert result.action_result.exception_type == NoIncrementalElementFoundForAutoCompletion.__name__
        skyvern_el.press_fill.assert_awaited_once_with("Barnstable County")
        incremental_scrape.get_incremental_element_tree.assert_awaited_once()
        scraped_page.generate_scraped_page_without_screenshots.assert_not_awaited()
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_awaited()


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
            is_secret_value=False,
        )

        # Search bar returns None early, never reaching discover fallback
        assert result is None
        mock_discover.assert_not_called()
