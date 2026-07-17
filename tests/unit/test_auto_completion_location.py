"""Tests for the location auto-completion fast-path optimisation.

When the user types an address into a location field and exactly one autocomplete
suggestion appears, we skip the LLM call and click the suggestion directly.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.webeye.actions.handler as handler_module
from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.exceptions import AutoCompletionCommitFailure, MissingElement, NoIncrementalElementFoundForAutoCompletion
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext
from skyvern.webeye.actions.handler import (
    AutoCompletionResult,
    _poll_autocomplete_incremental_elements,
    _reset_autocomplete_for_llm_fallback,
    _verify_autocomplete_input_readback,
    choose_auto_completion_dropdown,
    input_or_auto_complete_input,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={"address": "123 Main St"})
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

SINGLE_ELEMENT = [
    {"id": "AAAA", "tagName": "li", "attributes": {"role": "option"}, "text": "123 Main St, Springfield, IL"}
]
MULTI_ELEMENTS = [
    {"id": "AAAA", "tagName": "li", "attributes": {"role": "option"}, "text": "123 Main St, Springfield, IL"},
    {"id": "AAAB", "tagName": "li", "attributes": {"role": "option"}, "text": "123 Main St, Springfield, MO"},
]
LOADING_ELEMENT = {"id": "LOAD", "tagName": "div", "text": "Loading..."}
OPTION_ELEMENT = {"id": "OPT", "tagName": "li", "attributes": {"role": "option"}, "text": "123 Main St, Springfield"}


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
    el.get_tag_name.return_value = "input"
    el.press_key = AsyncMock()
    el.get_attr = AsyncMock(return_value=None)
    input_locator = MagicMock()
    input_locator.input_value = AsyncMock(return_value="123 Main St, Springfield, IL")
    el.get_locator.return_value = input_locator
    return el


def _mock_frame(locator_count: int = 1) -> MagicMock:
    """Return a mock Playwright Frame with a configurable locator."""
    frame = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=locator_count)
    locator.click = AsyncMock()
    locator.element_handle = AsyncMock(return_value=MagicMock())
    locator.is_visible = AsyncMock(return_value=True)
    locator.get_attribute = AsyncMock(return_value=None)
    frame.locator.return_value = locator
    return frame


def _mock_incremental_scrape(elements: list[dict]) -> MagicMock:
    """Return a mock IncrementalScrapePage that yields *elements*."""
    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_elements_num = AsyncMock(return_value=len(elements))
    inc.get_incremental_element_tree = AsyncMock(return_value=copy.deepcopy(elements))
    inc.build_html_tree.return_value = "<div>mocked</div>"
    return inc


class _FakeSuggestionList:
    """Stateful listbox fake: each patched sleep advances the rendered frame."""

    def __init__(self, frames: list[list[dict]]) -> None:
        self._frames = frames
        self._tick = 0

    def _current(self) -> list[dict]:
        return self._frames[min(self._tick, len(self._frames) - 1)]

    async def advance(self, _delay: float) -> None:
        self._tick += 1

    def scrape(self) -> MagicMock:
        inc = MagicMock()
        inc.start_listen_dom_increment = AsyncMock()
        inc.stop_listen_dom_increment = AsyncMock()
        inc.get_incremental_elements_num = AsyncMock(side_effect=lambda: len(self._current()))
        inc.get_incremental_element_tree = AsyncMock(side_effect=lambda _cleanup: copy.deepcopy(self._current()))
        inc.build_html_tree.return_value = "<div>mocked</div>"
        return inc


def test_auto_completion_result_does_not_default_to_success() -> None:
    assert AutoCompletionResult().action_result is None


def test_removed_autocomplete_exception_name_still_catchable() -> None:
    """The deprecated OSS alias must keep downstream `except` clauses working for one release."""
    with pytest.raises(NoIncrementalElementFoundForAutoCompletion):
        raise AutoCompletionCommitFailure(stage="suggestion_not_matched")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_value", "matched_label", "typed_prefix", "expected"),
    [
        ("123 MAIN ST., Springfield!", "123 Main St Springfield", "123 Main", True),
        ("Selected: 123 Main St, Springfield", "123 Main St Springfield", "123 Main", True),
        ("123 Main", "123 Main St Springfield", "123 Main", False),
        ("scala 5", "ca", "sc", False),
        ("123 Main St Springfield", "123 Main St, Springfield, USA", "123 Main", True),
    ],
)
async def test_autocomplete_readback_matches_on_token_boundaries(
    actual_value: str,
    matched_label: str,
    typed_prefix: str,
    expected: bool,
) -> None:
    skyvern_el = _mock_skyvern_element()
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value=actual_value)

    outcome = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label=matched_label,
        typed_prefix=typed_prefix,
    )
    assert outcome.committed is expected


@pytest.mark.asyncio
async def test_autocomplete_readback_rejects_lone_token_of_multiword_label() -> None:
    # A single token of a multi-word label (here "Springfield" of "123 Main St Springfield") no longer
    # identifies the selection, even though the value changed - the field must still cover a substantial
    # share of the label. Substantial truncation ("123 Main St" of the same label) stays a commit.
    lone_token = _mock_skyvern_element()
    lone_token.get_locator.return_value.input_value = AsyncMock(return_value="Springfield")
    weak = await _verify_autocomplete_input_readback(
        skyvern_element=lone_token,
        matched_label="123 Main St Springfield",
        typed_prefix="123 Main",
    )
    assert weak.committed is False

    substantial = _mock_skyvern_element()
    substantial.get_locator.return_value.input_value = AsyncMock(return_value="123 Main St")
    strong = await _verify_autocomplete_input_readback(
        skyvern_element=substantial,
        matched_label="123 Main St Springfield",
        typed_prefix="123",
    )
    assert strong.committed is True


@pytest.mark.asyncio
async def test_autocomplete_readback_requires_secondary_signal_when_label_equals_typed_text() -> None:
    skyvern_el = _mock_skyvern_element()
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="Oakland, California")

    no_signal = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland, California",
    )
    assert no_signal.committed is False
    assert no_signal.side_effects_observed is False

    list_closed = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland, California",
        suggestions_closed=True,
    )
    assert list_closed.committed is True

    aria_selected = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland, California",
        selection_state_present=True,
    )
    assert aria_selected.committed is True


@pytest.mark.asyncio
async def test_autocomplete_readback_accepts_cleared_input_only_when_list_closed() -> None:
    skyvern_el = _mock_skyvern_element()
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="")

    chip_commit = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland",
        suggestions_closed=True,
    )
    assert chip_commit.committed is True

    still_open = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland",
    )
    assert still_open.committed is False


@pytest.mark.asyncio
async def test_autocomplete_readback_empty_value_needs_selection_signal_when_commit_required() -> None:
    # A click that dismisses the suggestions while clearing the field reads back identical to a chip
    # commit; under commit-required only an affirmative selection signal separates the two.
    skyvern_el = _mock_skyvern_element()
    skyvern_el.get_locator.return_value.input_value = AsyncMock(return_value="")

    dismissal = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland",
        suggestions_closed=True,
        commit_required=True,
    )
    assert dismissal.committed is False

    affirmed = await _verify_autocomplete_input_readback(
        skyvern_element=skyvern_el,
        matched_label="Oakland, California",
        typed_prefix="Oakland",
        suggestions_closed=True,
        selection_state_present=True,
        commit_required=True,
    )
    assert affirmed.committed is True


@pytest.mark.asyncio
async def test_autocomplete_poll_returns_immediately_when_suggestions_exist() -> None:
    fake_list = _FakeSuggestionList([SINGLE_ELEMENT])
    incremental_scrape = fake_list.scrape()

    with patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock(side_effect=fake_list.advance)) as sleep:
        result = await _poll_autocomplete_incremental_elements(
            incremental_scraped=incremental_scrape,
            cleanup_factory=MagicMock(),
        )

    assert result == SINGLE_ELEMENT
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_autocomplete_poll_waits_until_suggestions_render() -> None:
    fake_list = _FakeSuggestionList([[], [], SINGLE_ELEMENT])
    incremental_scrape = fake_list.scrape()

    with patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock(side_effect=fake_list.advance)) as sleep:
        result = await _poll_autocomplete_incremental_elements(
            incremental_scraped=incremental_scrape,
            cleanup_factory=MagicMock(),
        )

    assert result == SINGLE_ELEMENT
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_autocomplete_poll_keeps_waiting_past_loading_nodes_until_options_render() -> None:
    fake_list = _FakeSuggestionList([[LOADING_ELEMENT], [LOADING_ELEMENT], [LOADING_ELEMENT, OPTION_ELEMENT]])
    incremental_scrape = fake_list.scrape()

    with patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock(side_effect=fake_list.advance)):
        result = await _poll_autocomplete_incremental_elements(
            incremental_scraped=incremental_scrape,
            cleanup_factory=MagicMock(),
        )

    assert OPTION_ELEMENT in result


@pytest.mark.asyncio
async def test_autocomplete_poll_reextracts_when_placeholder_replaced_in_place() -> None:
    # A listbox can swap a loading placeholder for an option in the same node, so the incremental
    # count stays constant (1 -> 1). A count-delta guard would extract the placeholder once and never
    # re-extract, missing the rendered option.
    fake_list = _FakeSuggestionList([[LOADING_ELEMENT], [OPTION_ELEMENT]])
    incremental_scrape = fake_list.scrape()

    with patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock(side_effect=fake_list.advance)):
        result = await _poll_autocomplete_incremental_elements(
            incremental_scraped=incremental_scrape,
            cleanup_factory=MagicMock(),
        )

    assert OPTION_ELEMENT in result


@pytest.mark.asyncio
async def test_autocomplete_poll_is_bounded_when_suggestions_never_render() -> None:
    fake_list = _FakeSuggestionList([[]])
    incremental_scrape = fake_list.scrape()

    with patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock(side_effect=fake_list.advance)) as sleep:
        result = await _poll_autocomplete_incremental_elements(
            incremental_scraped=incremental_scrape,
            cleanup_factory=MagicMock(),
        )

    assert result == []
    incremental_scrape.get_incremental_element_tree.assert_not_awaited()
    assert sleep.await_count <= handler_module.AUTOCOMPLETE_POLL_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_autocomplete_poll_does_not_cancel_slow_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "AUTOCOMPLETE_POLL_INTERVAL_SECONDS", 0.05)
    incremental_scrape = MagicMock()
    incremental_scrape.get_incremental_elements_num = AsyncMock(return_value=1)

    async def _slow_extract(_cleanup: object) -> list[dict]:
        await asyncio.sleep(0.2)
        return copy.deepcopy(SINGLE_ELEMENT)

    incremental_scrape.get_incremental_element_tree = AsyncMock(side_effect=_slow_extract)

    result = await _poll_autocomplete_incremental_elements(
        incremental_scraped=incremental_scrape,
        cleanup_factory=MagicMock(),
    )

    assert result == SINGLE_ELEMENT
    assert incremental_scrape.get_incremental_element_tree.await_count == 1


@pytest.mark.asyncio
async def test_autocomplete_poll_bounds_a_hanging_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "AUTOCOMPLETE_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(handler_module, "AUTOCOMPLETE_POLL_MAX_ATTEMPTS", 2)
    incremental_scrape = MagicMock()

    async def _hanging_probe() -> int:
        await asyncio.sleep(10)
        return 1

    incremental_scrape.get_incremental_elements_num = AsyncMock(side_effect=_hanging_probe)
    incremental_scrape.get_incremental_element_tree = AsyncMock(return_value=copy.deepcopy(SINGLE_ELEMENT))

    result = await _poll_autocomplete_incremental_elements(
        incremental_scraped=incremental_scrape,
        cleanup_factory=MagicMock(),
    )

    assert result == []
    incremental_scrape.get_incremental_element_tree.assert_not_awaited()


@pytest.mark.asyncio
async def test_autocomplete_timeout_reports_suggestions_never_rendered() -> None:
    skyvern_el = _mock_skyvern_element()
    incremental_scrape = _mock_incremental_scrape([])
    scraped_page = MagicMock(id_to_css_dict={})
    scraped_after_open = MagicMock(id_to_css_dict={})
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=scraped_after_open)

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=incremental_scrape),
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock()),
    ):
        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=scraped_page,
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

    assert isinstance(result.action_result, ActionFailure)
    assert result.action_result.exception_type == AutoCompletionCommitFailure.__name__
    assert "suggestions_never_rendered" in (result.action_result.exception_message or "")


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
async def test_location_single_option_click_with_side_effects_does_not_refire() -> None:
    """A click that changed the value (but did not match) must hard-fail instead of re-firing."""
    frame, option_locator, skyvern_el, skyvern_frame = _mock_autocomplete_input(
        selected_value="Wrong city",
        click_closes_listbox=False,
    )
    inc_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

    option_locator.click.assert_awaited_once()
    skyvern_el.press_fill.assert_awaited_once()
    mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
    assert isinstance(result.action_result, ActionFailure)
    assert result.action_result.exception_type == AutoCompletionCommitFailure.__name__
    assert "clicked_but_not_committed" in (result.action_result.exception_message or "")


@pytest.mark.asyncio
async def test_location_single_option_side_effect_free_noncommit_falls_back_to_llm() -> None:
    """A no-op click (value unchanged, listbox still open) resets and runs the LLM chooser in the same call."""
    _frame, option_locator, skyvern_el, skyvern_frame = _mock_autocomplete_input(
        selected_value="123 Main",
        click_closes_listbox=False,
    )
    events: list[str] = []
    skyvern_el.input_clear = AsyncMock(side_effect=lambda: events.append("clear"))
    skyvern_el.press_fill = AsyncMock(side_effect=lambda value: events.append(f"fill:{value}"))
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)
    initial_scrape.build_html_tree.return_value = "<div>stale options</div>"
    fallback_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)
    fallback_scrape.build_html_tree.return_value = "<div>fresh options</div>"

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "123 Main",
        "reasoning": "Read-back mismatch",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ) as scrape_factory,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):

        async def _llm_handler(**_: object) -> dict[str, object]:
            events.append("llm")
            return llm_response

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(side_effect=_llm_handler)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

        option_locator.click.assert_awaited_once()
        assert scrape_factory.call_count == 2
        skyvern_el.input_clear.assert_awaited_once()
        assert skyvern_el.press_fill.await_count == 2
        assert events == ["fill:123 Main", "clear", "fill:123 Main", "llm"]
        assert mock_prompt.load_prompt.call_args.kwargs["elements"] == "<div>fresh options</div>"
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_direct_searching_unchanged_value_fails_when_commit_required() -> None:
    """Searching with the typed text leaves the value unchanged, which is indistinguishable from Enter
    doing nothing. On a field that must commit a real suggestion that is a failure, not a success."""
    _frame, _option_locator, skyvern_el, skyvern_frame = _mock_autocomplete_input(
        selected_value="123 Main",
        click_closes_listbox=False,
    )
    skyvern_el.input_clear = AsyncMock()
    skyvern_el.press_fill = AsyncMock()
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)
    initial_scrape.build_html_tree.return_value = "<div>stale options</div>"
    fallback_scrape = _mock_incremental_scrape(SINGLE_ELEMENT)
    fallback_scrape.build_html_tree.return_value = "<div>fresh options</div>"

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "123 Main",
        "reasoning": "Read-back mismatch",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
            commit_required=True,
        )

    assert isinstance(result.action_result, ActionFailure)
    assert result.action_result.exception_type == AutoCompletionCommitFailure.__name__
    assert "clicked_but_not_committed" in (result.action_result.exception_message or "")


@pytest.mark.asyncio
async def test_unexpected_exception_surfaces_as_itself_not_commit_failure() -> None:
    """A genuine bug-shaped error keeps its identity in the ActionFailure instead of being
    rewrapped as an expected no-match commit failure."""
    skyvern_el = _mock_skyvern_element()
    skyvern_el.press_fill = AsyncMock(side_effect=MissingElement(element_id="AAAA"))
    inc_scrape = _mock_incremental_scrape([])

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
    ):
        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

    assert isinstance(result.action_result, ActionFailure)
    assert result.action_result.exception_type == MissingElement.__name__
    assert result.stage_outcome == "suggestion_not_matched"


@pytest.mark.asyncio
async def test_commit_failure_keeps_its_stage_in_action_failure() -> None:
    """An AutoCompletionCommitFailure raised inside the dropdown flow keeps its own stage."""
    skyvern_el = _mock_skyvern_element()
    skyvern_el.press_fill = AsyncMock(side_effect=AutoCompletionCommitFailure(stage="suggestions_never_rendered"))
    inc_scrape = _mock_incremental_scrape([])

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
    ):
        result = await choose_auto_completion_dropdown(
            context=_make_location_context(),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="123 Main",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            is_location_input=True,
        )

    assert isinstance(result.action_result, ActionFailure)
    assert result.action_result.exception_type == AutoCompletionCommitFailure.__name__
    assert result.stage_outcome == "suggestions_never_rendered"


@pytest.mark.asyncio
async def test_unexpected_exception_still_hard_fails_location_field_when_flag_on() -> None:
    """A preserved unexpected exception from the dropdown flow still produces the staged
    location failure when the commit-required flag is on."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(MissingElement(element_id="AAAA")),
        stage_outcome="suggestion_not_matched",
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
            input_or_select_context=_make_location_context(),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == AutoCompletionCommitFailure.__name__
    assert "attempt_1:suggestion_not_matched" in (result.exception_message or "")


@pytest.mark.asyncio
async def test_location_search_bar_keeps_fallthrough_even_with_flag_on() -> None:
    """A location-search widget is both a location input and a search bar. Search bars keep their
    documented fall-through, so strict commit mode must not claim one and hard-fail it."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(MissingElement(element_id="AAAA")),
        stage_outcome="suggestion_not_matched",
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
            input_or_select_context=_make_location_context(is_search_bar=True),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert not isinstance(result, ActionFailure)


@pytest.mark.asyncio
async def test_unexpected_exception_falls_through_to_plain_typing_when_flag_off() -> None:
    """With the commit-required flag off, a preserved unexpected exception still lets the
    location field fall through to plain typing."""
    skyvern_el = _mock_skyvern_element()
    choose_result = AutoCompletionResult(
        action_result=ActionFailure(MissingElement(element_id="AAAA")),
        stage_outcome="suggestion_not_matched",
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
            input_or_select_context=_make_location_context(),
            scraped_page=MagicMock(),
            page=MagicMock(),
            dom=MagicMock(),
            text="123 Main St",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
        )

    assert result is None


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
    selected_element = MagicMock(scroll_into_view=AsyncMock(), click=AsyncMock())

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
        patch("skyvern.webeye.actions.handler.SkyvernElement", return_value=selected_element),
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

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

        # LLM should have been called because there are 2 options
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        selected_element.click.assert_awaited_once()
        assert isinstance(result.action_result, ActionSuccess)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_enabled", [True, False])
async def test_input_or_auto_complete_gates_verification_on_the_flag(flag_enabled: bool) -> None:
    """Commit verification runs only behind the flag; flag-off keeps the pre-verification behavior."""
    context = _make_location_context()

    with (
        patch(
            "skyvern.webeye.actions.handler.choose_auto_completion_dropdown",
            new=AsyncMock(return_value=AutoCompletionResult(action_result=ActionSuccess())),
        ) as mock_choose,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=flag_enabled)

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
        call_kwargs = mock_choose.call_args.kwargs
        assert call_kwargs["verify_commit"] is flag_enabled
        assert call_kwargs["commit_required"] is flag_enabled


# ---------------------------------------------------------------------------
# Integration tests: options that don't contain the input fall through to LLM
# ---------------------------------------------------------------------------

NO_RESULT_ELEMENTS = [{"id": "AAAA", "tagName": "div", "interactable": True, "text": "No results"}]
UNRELATED_ELEMENTS = [{"id": "AAAA", "tagName": "div", "interactable": True, "text": "Something completely different"}]
DETERMINISTIC_ELEMENTS = [
    {"id": "AAAA", "tagName": "li", "attributes": {"role": "option"}, "text": "San Francisco, California"},
    {"id": "AAAB", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"},
]
DUPLICATE_DETERMINISTIC_ELEMENTS = [
    {"id": "AAAA", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"},
    {"id": "AAAB", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"},
]


def _mock_autocomplete_input(
    *,
    selected_value: str,
    option_identity: dict[str, object] | None = None,
    click_closes_listbox: bool = True,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    frame = MagicMock()
    option_locator = MagicMock()
    option_locator.count = AsyncMock(return_value=1)
    option_locator.element_handle = AsyncMock(return_value=MagicMock())
    option_locator.is_visible = AsyncMock(return_value=True)
    option_locator.get_attribute = AsyncMock(return_value=None)

    async def _click(**_: object) -> None:
        if click_closes_listbox:
            option_locator.count = AsyncMock(return_value=0)

    option_locator.click = AsyncMock(side_effect=_click)
    frame.locator.return_value = option_locator

    input_locator = MagicMock()
    input_locator.input_value = AsyncMock(return_value=selected_value)

    skyvern_el = _mock_skyvern_element(frame)
    skyvern_el.get_locator.return_value = input_locator
    skyvern_el.get_tag_name.return_value = "input"
    skyvern_el.press_key = AsyncMock()

    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())
    skyvern_frame.read_autocomplete_option_identity = AsyncMock(
        return_value=option_identity or {"index": 1, "label": selected_value}
    )
    return frame, option_locator, skyvern_el, skyvern_frame


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
        "value": "123 Main St, Springfield, IL",
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


@pytest.mark.asyncio
async def test_collapse_autocomplete_exact_match_skips_llm() -> None:
    """When the collapse flag is on, an exact singleton option match is clicked without the LLM."""
    frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California",
        option_identity={"index": 0, "label": "Oakland, California"},
    )
    inc_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
        frame.locator.assert_called_with(f'[{SKYVERN_ID_ATTR}="AAAB"]')
        option_locator.click.assert_awaited_once()
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_collapse_autocomplete_stem_match_skips_llm() -> None:
    """The deterministic autocomplete path uses the same exact/stem tier as normal select."""
    frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Masters",
        option_identity={"index": 0, "label": "Masters"},
    )
    inc_scrape = _mock_incremental_scrape(
        [{"id": "AAAA", "tagName": "li", "attributes": {"role": "option"}, "text": "Masters"}]
    )

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Degree"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Master",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
        frame.locator.assert_called_with(f'[{SKYVERN_ID_ATTR}="AAAA"]')
        option_locator.click.assert_awaited_once()
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_collapse_autocomplete_search_bar_exact_match_uses_llm() -> None:
    """Search bars keep the existing LLM path even when an exact option is visible."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California"
    )
    skyvern_el.press_key = AsyncMock()
    inc_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Search bars should direct search",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        result = await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Search", is_search_bar=True),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        option_locator.click.assert_not_called()
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")
        assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_collapse_autocomplete_ambiguous_exact_match_falls_back_to_llm() -> None:
    """Duplicate exact labels are unsafe and must keep the existing LLM chooser path."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California"
    )
    skyvern_el.press_key = AsyncMock()
    inc_scrape = _mock_incremental_scrape(DUPLICATE_DETERMINISTIC_ELEMENTS)

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Ambiguous duplicate labels",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        option_locator.click.assert_not_called()
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_collapse_autocomplete_identity_mismatch_resets_before_llm() -> None:
    """An option identity mismatch must refresh the dropdown before the LLM chooser runs."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California",
        option_identity={"index": 1, "label": "San Jose, California"},
    )
    events: list[str] = []
    skyvern_el.input_clear = AsyncMock(side_effect=lambda: events.append("clear"))
    skyvern_el.press_fill = AsyncMock(side_effect=lambda value: events.append(f"fill:{value}"))
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)
    initial_scrape.build_html_tree.return_value = '<div data-stale-id="AAAB">stale options</div>'
    fallback_scrape = _mock_incremental_scrape(
        [{"id": "FRESH", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"}]
    )
    fallback_scrape.build_html_tree.return_value = '<div data-fresh-id="FRESH">fresh options</div>'

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Option rerendered",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ) as scrape_factory,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):

        async def _llm_handler(**_: object) -> dict[str, object]:
            events.append("llm")
            return llm_response

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(side_effect=_llm_handler)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        option_locator.click.assert_not_called()
        assert scrape_factory.call_count == 2
        skyvern_el.input_clear.assert_awaited_once()
        assert skyvern_el.press_fill.await_count == 2
        assert events == ["fill:Oakland, California", "clear", "fill:Oakland, California", "llm"]
        assert mock_prompt.load_prompt.call_args.kwargs["elements"] == '<div data-fresh-id="FRESH">fresh options</div>'
        assert "stale" not in mock_prompt.load_prompt.call_args.kwargs["elements"]
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_collapse_autocomplete_readback_mismatch_falls_back_to_llm() -> None:
    """A no-op deterministic click (value unchanged, listbox open) is not accepted and resets before the LLM."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California",
        option_identity={"index": 1, "label": "Oakland, California"},
        click_closes_listbox=False,
    )
    events: list[str] = []
    skyvern_el.input_clear = AsyncMock(side_effect=lambda: events.append("clear"))
    skyvern_el.press_fill = AsyncMock(side_effect=lambda value: events.append(f"fill:{value}"))
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)
    initial_scrape.build_html_tree.return_value = "<div>stale options</div>"
    fallback_scrape = _mock_incremental_scrape(
        [{"id": "AAAB", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"}]
    )
    fallback_scrape.build_html_tree.return_value = "<div>fresh options</div>"

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Read-back mismatch",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ) as scrape_factory,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):

        async def _llm_handler(**_: object) -> dict[str, object]:
            events.append("llm")
            return llm_response

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(side_effect=_llm_handler)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        option_locator.click.assert_awaited_once()
        assert scrape_factory.call_count == 2
        skyvern_el.input_clear.assert_awaited_once()
        assert skyvern_el.press_fill.await_count == 2
        assert events == ["fill:Oakland, California", "clear", "fill:Oakland, California", "llm"]
        assert mock_prompt.load_prompt.call_args.kwargs["elements"] == "<div>fresh options</div>"
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_collapse_autocomplete_deterministic_path_verifies_even_when_flag_off() -> None:
    # The deterministic read-back is the one verify site that exists on main, so it stays on even at
    # flag-off: a click that leaves the value unchanged with the listbox open is not a commit and must
    # still reset and fall back to the LLM chooser, never be reported as a silent success.
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California",
        option_identity={"index": 1, "label": "Oakland, California"},
        click_closes_listbox=False,
    )
    events: list[str] = []
    skyvern_el.input_clear = AsyncMock(side_effect=lambda: events.append("clear"))
    skyvern_el.press_fill = AsyncMock(side_effect=lambda value: events.append(f"fill:{value}"))
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)
    fallback_scrape = _mock_incremental_scrape(
        [{"id": "AAAB", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"}]
    )
    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Read-back mismatch",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):

        async def _llm_handler(**_: object) -> dict[str, object]:
            events.append("llm")
            return llm_response

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(side_effect=_llm_handler)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            verify_commit=False,
            collapse_autocomplete_fanout_enabled=True,
        )

    option_locator.click.assert_awaited_once()
    skyvern_el.get_locator.return_value.input_value.assert_awaited()
    mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()


@pytest.mark.asyncio
async def test_collapse_autocomplete_chip_widget_commit_succeeds() -> None:
    """A chip/token widget clears the input after committing; cleared value + closed listbox is a commit."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="",
        option_identity={"index": 1, "label": "Oakland, California"},
    )
    inc_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

    option_locator.click.assert_awaited_once()
    mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
    assert isinstance(result.action_result, ActionSuccess)


@pytest.mark.asyncio
async def test_collapse_autocomplete_click_with_side_effects_does_not_refire() -> None:
    """A click that changed the value (but did not match) must not be re-fired via the LLM fallback."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Wrong city",
        option_identity={"index": 1, "label": "Oakland, California"},
        click_closes_listbox=False,
    )
    inc_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc_scrape),
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()

        result = await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

    option_locator.click.assert_awaited_once()
    skyvern_el.press_fill.assert_awaited_once()
    mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_not_called()
    assert isinstance(result.action_result, ActionFailure)
    assert "clicked_but_not_committed" in (result.action_result.exception_message or "")


@pytest.mark.asyncio
async def test_collapse_autocomplete_detached_option_resets_before_llm() -> None:
    """A deterministic candidate that detaches before the click must refresh before the LLM chooser."""
    _frame, option_locator, skyvern_el, skyvern_frame_mock = _mock_autocomplete_input(
        selected_value="Oakland, California",
        option_identity={"index": 0, "label": "Oakland, California"},
    )
    option_locator.count = AsyncMock(return_value=0)
    events: list[str] = []
    skyvern_el.input_clear = AsyncMock(side_effect=lambda: events.append("clear"))
    skyvern_el.press_fill = AsyncMock(side_effect=lambda value: events.append(f"fill:{value}"))
    skyvern_el.press_key = AsyncMock()
    initial_scrape = _mock_incremental_scrape(DETERMINISTIC_ELEMENTS)
    initial_scrape.build_html_tree.return_value = '<div data-stale-id="AAAB">stale options</div>'
    fallback_scrape = _mock_incremental_scrape(
        [{"id": "FRESH", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland, California"}]
    )
    fallback_scrape.build_html_tree.return_value = '<div data-fresh-id="FRESH">fresh options</div>'

    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "Oakland, California",
        "reasoning": "Option detached",
    }

    with (
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=skyvern_frame_mock),
        ),
        patch(
            "skyvern.webeye.actions.handler.IncrementalScrapePage",
            side_effect=[initial_scrape, fallback_scrape],
        ) as scrape_factory,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
    ):

        async def _llm_handler(**_: object) -> dict[str, object]:
            events.append("llm")
            return llm_response

        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(side_effect=_llm_handler)
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)

        await choose_auto_completion_dropdown(
            context=_make_non_location_context(field="Current location"),
            page=MagicMock(),
            scraped_page=MagicMock(),
            dom=MagicMock(),
            text="Oakland, California",
            skyvern_element=skyvern_el,
            step=_STEP,
            task=_TASK,
            collapse_autocomplete_fanout_enabled=True,
        )

        option_locator.click.assert_not_called()
        skyvern_frame_mock.read_autocomplete_option_identity.assert_not_called()
        assert scrape_factory.call_count == 2
        skyvern_el.input_clear.assert_awaited_once()
        assert skyvern_el.press_fill.await_count == 2
        assert events == ["fill:Oakland, California", "clear", "fill:Oakland, California", "llm"]
        assert mock_prompt.load_prompt.call_args.kwargs["elements"] == '<div data-fresh-id="FRESH">fresh options</div>'
        assert "stale" not in mock_prompt.load_prompt.call_args.kwargs["elements"]
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER.assert_awaited_once()
        skyvern_el.press_key.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_reset_autocomplete_empty_incremental_rescrapes_page_elements() -> None:
    """Empty reset increments fall back to a full page scrape for fresh interactable options."""
    current_scrape = MagicMock(stop_listen_dom_increment=AsyncMock())
    fallback_scrape = _mock_incremental_scrape([])
    skyvern_el = _mock_skyvern_element()
    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())
    fresh_element = {"id": "FRESH", "tagName": "li", "attributes": {"role": "option"}, "text": "Oakland"}

    scraped_page = MagicMock()
    scraped_page.id_to_css_dict = {"OLD": "[data-skyvern-id='OLD']"}
    scraped_after_open = MagicMock()
    scraped_after_open.id_to_css_dict = {
        "OLD": "[data-skyvern-id='OLD']",
        "FRESH": "[data-skyvern-id='FRESH']",
    }
    scraped_after_open.id_to_element_dict = {"FRESH": fresh_element}
    scraped_after_open.build_element_tree.return_value = "<div>fresh page scrape</div>"
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=scraped_after_open)

    interactable = MagicMock()
    interactable.is_interactable.return_value = True
    dom_after_open = MagicMock()
    dom_after_open.get_skyvern_element_by_id = AsyncMock(return_value=interactable)

    with (
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=fallback_scrape) as scrape_factory,
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_after_open),
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock()),
    ):
        (
            returned_scrape,
            fallback_elements,
            cleaned_elements,
            html,
            new_element_ids,
        ) = await _reset_autocomplete_for_llm_fallback(
            current_incremental_scraped=current_scrape,
            skyvern_frame=skyvern_frame,
            skyvern_element=skyvern_el,
            page=MagicMock(),
            scraped_page=scraped_page,
            dom=MagicMock(),
            text="Oakland",
            task=_TASK,
            step=_STEP,
        )

    current_scrape.stop_listen_dom_increment.assert_awaited_once()
    skyvern_el.input_clear.assert_awaited_once()
    skyvern_el.press_fill.assert_awaited_once_with("Oakland")
    scrape_factory.assert_called_once_with(skyvern_frame=skyvern_frame)
    fallback_scrape.get_incremental_elements_num.assert_awaited()
    fallback_scrape.get_incremental_element_tree.assert_not_awaited()
    scraped_page.generate_scraped_page_without_screenshots.assert_awaited_once()
    dom_after_open.get_skyvern_element_by_id.assert_awaited_once_with("FRESH")
    assert returned_scrape is fallback_scrape
    assert fallback_elements == [fresh_element]
    assert cleaned_elements == [fresh_element]
    assert html == "<div>fresh page scrape</div>"
    assert new_element_ids == ["FRESH"]


@pytest.mark.asyncio
async def test_reset_autocomplete_empty_rescrape_without_interactable_elements_raises() -> None:
    """The reset helper raises when neither incremental nor page-rescrape options are interactable."""
    current_scrape = MagicMock(stop_listen_dom_increment=AsyncMock())
    fallback_scrape = _mock_incremental_scrape([])
    skyvern_el = _mock_skyvern_element()
    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())

    scraped_page = MagicMock()
    scraped_page.id_to_css_dict = {"OLD": "[data-skyvern-id='OLD']"}
    scraped_after_open = MagicMock()
    scraped_after_open.id_to_css_dict = {
        "OLD": "[data-skyvern-id='OLD']",
        "FRESH": "[data-skyvern-id='FRESH']",
    }
    scraped_after_open.id_to_element_dict = {"FRESH": {"id": "FRESH", "text": "Oakland"}}
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=scraped_after_open)

    non_interactable = MagicMock()
    non_interactable.is_interactable.return_value = False
    dom_after_open = MagicMock()
    dom_after_open.get_skyvern_element_by_id = AsyncMock(return_value=non_interactable)

    with (
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=fallback_scrape),
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_after_open),
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock()),
        pytest.raises(AutoCompletionCommitFailure),
    ):
        await _reset_autocomplete_for_llm_fallback(
            current_incremental_scraped=current_scrape,
            skyvern_frame=skyvern_frame,
            skyvern_element=skyvern_el,
            page=MagicMock(),
            scraped_page=scraped_page,
            dom=MagicMock(),
            text="Oakland",
            task=_TASK,
            step=_STEP,
        )

    current_scrape.stop_listen_dom_increment.assert_awaited_once()
    skyvern_el.input_clear.assert_awaited_once()
    skyvern_el.press_fill.assert_awaited_once_with("Oakland")
    scraped_page.generate_scraped_page_without_screenshots.assert_awaited_once()
    dom_after_open.get_skyvern_element_by_id.assert_awaited_once_with("FRESH")
