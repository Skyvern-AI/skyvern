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

from skyvern.exceptions import AutoCompletionCommitFailure
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
    # get_locator().click() needs to be async for the discover fallback's click call
    mock_locator = MagicMock()
    mock_locator.click = AsyncMock()
    mock_locator.input_value = AsyncMock(return_value="I do not wish to disclose")
    el.get_locator.return_value = mock_locator
    el.get_tag_name.return_value = "input"
    el.get_attr = AsyncMock(return_value="false")
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
    inc.get_incremental_elements_num = AsyncMock(return_value=len(elements))
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


@pytest.mark.asyncio
async def test_discover_fallback_noop_keyboard_selection_is_not_a_commit() -> None:
    """The input still holding the fallback's own fill with the listbox open is not commit evidence."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    skyvern_el.get_attr = AsyncMock(return_value="true")
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

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
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        with pytest.raises(AutoCompletionCommitFailure) as exc_info:
            await discover_and_select_from_full_dropdown(
                context=_make_context(),
                page=MagicMock(),
                scraped_page=MagicMock(),
                dom=MagicMock(),
                original_text="Decline to Self Identify",
                skyvern_element=skyvern_el,
                step=_STEP,
                task=_TASK,
            )

    assert exc_info.value.stage == "clicked_but_not_committed"
    inc_scrape.stop_listen_dom_increment.assert_awaited()


@pytest.mark.asyncio
async def test_discover_fallback_assumes_commit_when_verification_disabled() -> None:
    """Flag off: the keyboard selection is assumed committed - same scenario the noop test above
    rejects with verification enabled."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    skyvern_el.get_attr = AsyncMock(return_value="true")
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

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
            verify_commit=False,
        )

    assert isinstance(result, ActionSuccess)
    inc_scrape.stop_listen_dom_increment.assert_awaited()


@pytest.mark.asyncio
async def test_discover_fallback_canonicalized_commit_succeeds() -> None:
    """A widget that expands the committed value beyond the fallback's own fill counts as a commit."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    skyvern_el.get_attr = AsyncMock(return_value=None)
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="I do not wish to disclose (private)")
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "value": "I do not wish to disclose",
        "reasoning": "Best semantic match",
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

    assert isinstance(result, ActionSuccess)


@pytest.mark.asyncio
async def test_discover_fallback_exact_echo_without_expanded_state_is_a_commit() -> None:
    """A widget that commits the option text verbatim and exposes no expanded state is a commit, not a failure."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    # No aria-expanded at all: the widget gives no open/closed signal, and the committed value is the
    # option text verbatim, so it is indistinguishable from the fallback's own fill by value alone.
    skyvern_el.get_attr = AsyncMock(return_value=None)
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="I do not wish to disclose")
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "value": "I do not wish to disclose",
        "reasoning": "Best semantic match",
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

    assert isinstance(result, ActionSuccess)


@pytest.mark.asyncio
async def test_discover_fallback_ignores_stale_cached_expanded_state() -> None:
    """A no-op Enter must not read as committed just because the element was scraped before the
    dropdown opened. get_attr is static-first, so the cached aria-expanded="false" from that scrape
    would otherwise stand in as commit evidence for an unchanged value."""
    frame = _mock_frame(locator_count=1)
    skyvern_el = _mock_skyvern_element(frame)
    # Behaves like the real SkyvernElement.get_attr: static cache says "false" (scraped while closed),
    # the live DOM says "true" (the listbox is still open, so Enter did nothing).
    static_attrs = {"aria-expanded": "false"}
    live_attrs = {"aria-expanded": "true"}

    async def _get_attr(name: str, mode: str = "auto", **_kwargs: object) -> object:
        if mode == "dynamic":
            return live_attrs.get(name)
        cached = static_attrs.get(name)
        return cached if cached is not None or mode == "static" else live_attrs.get(name)

    skyvern_el.get_attr = AsyncMock(side_effect=_get_attr)
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="I do not wish to disclose")
    inc_scrape = _mock_incremental_scrape(DROPDOWN_OPTIONS)

    llm_response = {
        "auto_completion_attempt": True,
        "relevance_float": 0.9,
        "id": "OPT3",
        "value": "I do not wish to disclose",
        "reasoning": "Best semantic match",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        with pytest.raises(AutoCompletionCommitFailure):
            await discover_and_select_from_full_dropdown(
                context=_make_context(),
                page=MagicMock(),
                scraped_page=MagicMock(),
                dom=MagicMock(),
                original_text="Decline to Self Identify",
                skyvern_element=skyvern_el,
                step=_STEP,
                task=_TASK,
            )


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

        # Search bars fall through to plain typing (None) instead of hard-failing,
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

        assert result is None
        mock_discover.assert_not_called()


@pytest.mark.asyncio
async def test_input_or_auto_complete_failure_includes_attempt_trail() -> None:
    """A location field with the commit-required flag on returns a typed failure with the attempt trail."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(AutoCompletionCommitFailure(stage="suggestions_never_rendered")),
        stage_outcome="suggestions_never_rendered",
    )

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(return_value=choose_result),
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_location_input=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="private value",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == AutoCompletionCommitFailure.__name__
    assert result.exception_message is not None
    assert "attempt_1:suggestions_never_rendered" in result.exception_message
    assert "attempt_2:suggestion_not_matched" in result.exception_message
    assert "private value" not in result.exception_message


def _side_effect_choose_result() -> AutoCompletionResult:
    return AutoCompletionResult(
        action_result=ActionFailure(AutoCompletionCommitFailure(stage="clicked_but_not_committed")),
        stage_outcome="clicked_but_not_committed",
        committed_side_effects=True,
    )


@pytest.mark.asyncio
async def test_side_effectful_click_stops_retrying_with_flag_on() -> None:
    """Once a click has mutated the field but could not be verified, the orchestration loop must
    stop instead of generating more values and clicking additional options on the dirty field."""
    skyvern_el = _mock_skyvern_element()
    choose_mock = AsyncMock(return_value=_side_effect_choose_result())
    discover_mock = AsyncMock(return_value=ActionSuccess())

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=choose_mock),
        patch("skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown", new=discover_mock),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_location_input=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="private value",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert choose_mock.call_count == 1
    discover_mock.assert_not_called()
    mock_app.SECONDARY_LLM_API_HANDLER.assert_not_called()
    assert isinstance(result, ActionFailure)
    assert result.exception_type == AutoCompletionCommitFailure.__name__
    assert "clicked_but_not_committed" in (result.exception_message or "")


@pytest.mark.asyncio
async def test_side_effectful_click_stops_retrying_with_flag_off() -> None:
    """With the flag off the loop still stops after a side-effectful click (returning None to fall
    through to plain typing) rather than clicking more options on the mutated field."""
    skyvern_el = _mock_skyvern_element()
    choose_mock = AsyncMock(return_value=_side_effect_choose_result())
    discover_mock = AsyncMock(return_value=ActionSuccess())

    with (
        patch("skyvern.webeye.actions.handler.choose_auto_completion_dropdown", new=choose_mock),
        patch("skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown", new=discover_mock),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_location_input=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="private value",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert choose_mock.call_count == 1
    discover_mock.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_input_or_auto_complete_falls_through_when_flag_disabled() -> None:
    """With the commit-required flag off, an exhausted location autocomplete falls through to plain typing."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(AutoCompletionCommitFailure(stage="suggestions_never_rendered")),
        stage_outcome="suggestions_never_rendered",
    )

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(return_value=choose_result),
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_location_input=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="private value",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert result is None


@pytest.mark.asyncio
async def test_fallback_stage_is_read_structurally_not_from_message_text() -> None:
    """The fallback stage comes from the exception's stage attribute, even when the
    message's attempt trail embeds every other stage name ahead of it."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(AutoCompletionCommitFailure(stage="suggestions_never_rendered")),
        stage_outcome="suggestions_never_rendered",
    )
    fallback_failure = AutoCompletionCommitFailure(
        stage="clicked_but_not_committed",
        attempt_trail=["attempt_1:suggestions_never_rendered", "attempt_2:suggestion_not_matched"],
    )
    assert "suggestions_never_rendered" in str(fallback_failure)

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(return_value=choose_result),
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.discover_and_select_from_full_dropdown",
            new=AsyncMock(side_effect=fallback_failure),
        ),
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await input_or_auto_complete_input(
            input_or_select_context=_make_context(is_location_input=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="private value",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == AutoCompletionCommitFailure.__name__
    assert "attempt_2:clicked_but_not_committed" in (result.exception_message or "")
    assert "stage=clicked_but_not_committed" in (result.exception_message or "")
