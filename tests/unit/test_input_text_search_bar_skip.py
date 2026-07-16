"""Regression tests for SKY-11343.

When INPUT_TEXT into a search-bar/combobox commits a selection in-action, a queued
page-level Enter (or a same-element action) would re-open the widget and reset the
selection. The fix stops the batch (skip_remaining_actions) only in that case, gated by
``action.stop_batch_after_dropdown_select`` (set by the agent loop). Plain search inputs
and non-clobbering follow-ups (Tab/Escape/Arrow, different-element actions) are unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction, KeypressAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.handler_utils import keys_include_enter, should_stop_batch_after_dropdown_select
from skyvern.webeye.actions.responses import ActionSuccess
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
    ):
        return await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )


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
