"""Regression tests for SKY-11343.

When INPUT_TEXT into a search-bar/combobox commits a selection in-action, a queued
page-level Enter (or a same-element action) would re-open the widget and reset the
selection. The fix stops the batch (skip_remaining_actions) only in that case, gated by
``action.stop_batch_after_dropdown_select`` (set by the agent loop). Plain search inputs
and non-clobbering follow-ups (Tab/Escape/Arrow, different-element actions) are unaffected.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import AutoCompletionCommitFailure
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction, KeypressAction
from skyvern.webeye.actions.handler import AUTOCOMPLETE_COMMIT_REQUIRED_FLAG, handle_input_text_action
from skyvern.webeye.actions.handler_utils import keys_include_enter, should_stop_batch_after_dropdown_select
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Select the account")
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)


# --------------------------------------------------------------------------- #
# keys_include_enter — Enter/Return alias (mirrors keypress() normalization)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "keys,expected",
    [
        (["Enter"], True),
        (["Return"], True),
        (["reTURN"], True),
        (["RETURN"], True),
        (["a", "Enter"], True),
        (["Tab"], False),
        (["Escape"], False),
        (["ArrowDown"], False),
        ([], False),
    ],
)
def test_keys_include_enter(keys: list[str], expected: bool) -> None:
    assert keys_include_enter(keys) is expected


# --------------------------------------------------------------------------- #
# should_stop_batch_after_dropdown_select — the gate the agent loop computes
# --------------------------------------------------------------------------- #
def _input(element_id: str = "AADC") -> InputTextAction:
    return InputTextAction(element_id=element_id, text="123456", reasoning="type the account number")


@pytest.mark.parametrize(
    "next_action,expected",
    [
        (KeypressAction(keys=["Enter"], reasoning="submit"), True),
        (KeypressAction(keys=["Return"], reasoning="submit"), True),
        (KeypressAction(keys=["Tab"], reasoning="next field"), False),
        (KeypressAction(keys=["Escape"], reasoning="close"), False),
        (InputTextAction(element_id="AADC", text="x", reasoning="same element no longer triggers"), False),
        (InputTextAction(element_id="OTHER", text="x", reasoning="different element"), False),
        (None, False),
    ],
)
def test_should_stop_batch_after_dropdown_select(next_action: object, expected: bool) -> None:
    assert should_stop_batch_after_dropdown_select(next_action) is expected


def test_flag_is_transient_not_serialized() -> None:
    action = _input()
    action.stop_batch_after_dropdown_select = True
    assert "stop_batch_after_dropdown_select" not in action.model_dump()


# --------------------------------------------------------------------------- #
# handle_input_text_action — gated batch-stop behavior
# --------------------------------------------------------------------------- #
async def _run_search_bar_input(stop_flag: bool, incremental: list[dict]) -> list:
    skyvern_el = make_input_element_mock(element_id="AADC")
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=incremental)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}

    context = InputOrSelectContext(field="Account", is_search_bar=True, is_location_input=False)
    select_result = MagicMock()
    select_result.action_result = ActionSuccess()

    action = _input()
    action.stop_batch_after_dropdown_select = stop_flag

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="123456",
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._incremental_tree_contains_target_value", return_value=True),
        patch(
            "skyvern.webeye.actions.handler.sequentially_select_from_dropdown",
            new=AsyncMock(return_value=select_result),
        ),
        patch(
            "skyvern.webeye.actions.handler._is_input_text_commit_verification_enabled",
            new=AsyncMock(return_value=False),
        ),
    ):
        return await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )


def _fake_experimentation_provider(enabled_flags: set[str]) -> MagicMock:
    provider = MagicMock()

    async def _is_enabled(flag: str, distinct_id: str, properties: dict | None = None) -> bool:
        return flag in enabled_flags

    provider.is_feature_enabled_cached = AsyncMock(side_effect=_is_enabled)
    provider.resolve_feature_enabled_unrecorded = AsyncMock(return_value=False)
    return provider


async def _run_autocomplete_input_without_suggestions(
    *,
    is_search_bar: bool,
    is_location_input: bool,
    commit_flag_enabled: bool,
) -> tuple[list, MagicMock]:
    """Drive handle_input_text_action through the REAL autocomplete chain with a widget
    that never renders suggestions."""
    skyvern_el = make_input_element_mock(element_id="AADC")
    skyvern_el.is_auto_completion_input = AsyncMock(return_value=True)
    skyvern_el.press_fill = AsyncMock()
    skyvern_el.has_attr = AsyncMock(return_value=False)
    skyvern_el.get_locator.return_value.click = AsyncMock()

    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_elements_num = AsyncMock(return_value=0)
    inc.get_incremental_element_tree = AsyncMock(return_value=[])
    inc.build_html_tree.return_value = "<div></div>"
    inc.id_to_element_dict = {}

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}
    scraped_page.id_to_css_dict = {"AADC": "#aadc"}
    scraped_after = MagicMock()
    scraped_after.id_to_css_dict = {"AADC": "#aadc"}
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=scraped_after)

    context = InputOrSelectContext(field="Account", is_search_bar=is_search_bar, is_location_input=is_location_input)
    enabled_flags = {AUTOCOMPLETE_COMMIT_REQUIRED_FLAG} if commit_flag_enabled else set()

    action = _input()

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="123456",
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock()),
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.EXPERIMENTATION_PROVIDER = _fake_experimentation_provider(enabled_flags)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value={})
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )
    return results, skyvern_el


@pytest.mark.asyncio
async def test_selection_with_clobbering_next_stops_batch() -> None:
    """Selection committed + a clobbering next action (flag set) -> skip_remaining_actions=True."""
    results = await _run_search_bar_input(stop_flag=True, incremental=[{"id": "OPT", "text": "Target 123456"}])
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert results[0].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_selection_without_clobbering_next_preserves_batch() -> None:
    """Selection committed but next action is benign (flag unset) -> batch NOT stopped."""
    results = await _run_search_bar_input(stop_flag=False, incremental=[{"id": "OPT", "text": "Target 123456"}])
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert not results[0].skip_remaining_actions


@pytest.mark.asyncio
async def test_plain_search_input_does_not_stop_batch() -> None:
    """No dropdown surfaced (plain search box) -> ActionSuccess without skip, so a trailing Enter still fires."""
    results = await _run_search_bar_input(stop_flag=True, incremental=[])
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert not results[0].skip_remaining_actions


@pytest.mark.asyncio
async def test_search_bar_without_suggestions_falls_through_to_plain_typing() -> None:
    """A search bar whose autocomplete never renders suggestions must still be typed into (flag off)."""
    results, skyvern_el = await _run_autocomplete_input_without_suggestions(
        is_search_bar=True,
        is_location_input=False,
        commit_flag_enabled=False,
    )
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    skyvern_el.input_sequentially.assert_awaited_once()


@pytest.mark.asyncio
async def test_location_field_without_suggestions_falls_through_when_flag_off() -> None:
    results, skyvern_el = await _run_autocomplete_input_without_suggestions(
        is_search_bar=False,
        is_location_input=True,
        commit_flag_enabled=False,
    )
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    skyvern_el.input_sequentially.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_bar_direct_search_enter_navigation_is_implicit_commit() -> None:
    """direct_searching presses Enter, the page navigates, and every post-Enter read-back hits a stale
    element; the action must succeed as an implicit commit without falling through to plain typing."""
    skyvern_el = make_input_element_mock(element_id="AADC")
    skyvern_el.is_auto_completion_input = AsyncMock(return_value=True)
    skyvern_el.press_fill = AsyncMock()
    skyvern_el.has_attr = AsyncMock(return_value=False)

    enter_pressed = False

    async def _press_key(key: str, **_: object) -> None:
        nonlocal enter_pressed
        if key == "Enter":
            enter_pressed = True

    skyvern_el.press_key = AsyncMock(side_effect=_press_key)

    async def _read_input_value(tag_name: str, locator: object) -> str:
        if enter_pressed:
            raise PlaywrightError("Element is not attached to the DOM")
        return ""

    suggestions = [
        {"id": "OPT1", "tagName": "li", "attributes": {"role": "option"}, "text": "Target 123456"},
        {"id": "OPT2", "tagName": "li", "attributes": {"role": "option"}, "text": "Target 654321"},
    ]

    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_elements_num = AsyncMock(return_value=len(suggestions))
    inc.get_incremental_element_tree = AsyncMock(return_value=copy.deepcopy(suggestions))
    inc.build_html_tree.return_value = "<div>options</div>"
    inc.id_to_element_dict = {}

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}
    scraped_page.id_to_css_dict = {"AADC": "#aadc"}

    context = InputOrSelectContext(field="Search", is_search_bar=True, is_location_input=False)
    llm_response = {
        "auto_completion_attempt": False,
        "relevance_float": 0.0,
        "id": "",
        "direct_searching": True,
        "value": "123456",
        "reasoning": "Search directly",
    }

    action = _input()

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch(
            "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
            new=AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
        ),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=_read_input_value)),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="123456",
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new=AsyncMock()),
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch("skyvern.webeye.actions.handler.app") as mock_app,
    ):
        mock_app.EXPERIMENTATION_PROVIDER = _fake_experimentation_provider(set())
        mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
        mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"potential_values": []})
        mock_app.AGENT_FUNCTION = MagicMock()
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    skyvern_el.press_key.assert_awaited_once_with("Enter")
    skyvern_el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_location_field_without_commit_fails_when_flag_on() -> None:
    """With the commit-required flag on, a location field never falls through to plain typing."""
    results, skyvern_el = await _run_autocomplete_input_without_suggestions(
        is_search_bar=False,
        is_location_input=True,
        commit_flag_enabled=True,
    )
    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert results[0].exception_type == AutoCompletionCommitFailure.__name__
    assert "suggestions_never_rendered" in (results[0].exception_message or "")
    skyvern_el.input_sequentially.assert_not_awaited()
