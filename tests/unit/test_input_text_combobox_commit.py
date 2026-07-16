"""Tests for the post-input combobox-commit gate in ``handle_input_text_action``.

An ``aria-autocomplete`` combobox (e.g. ``role=combobox aria-autocomplete=both``) is not an
``is_auto_completion_input()`` (that predicate matches only ``list``), so typing into it never
runs the deterministic type-then-select flow. When such a field stays ``aria-invalid`` after
typing — because a value is only committed by picking a rendered option — the old code fell to
the blind Tab hack, which does not commit the option, so the planner looped clear/retype until
``REACH_MAX_STEPS``.

The gate here reuses the existing post-input incremental-DOM block: only when a genuine option
node exposes the typed value AND the source input is a still-invalid combobox does it force one
deterministic selection. It deliberately does NOT touch ``is_auto_completion_input()`` or the
speculative pre-input fanout, and it matches option-like nodes only (not arbitrary tree text) so
a "No results for <x>" banner cannot admit a selection attempt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill the job title")
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

_TARGET = "Backend Engineer"


def _listbox_with_option(label: str) -> list[dict]:
    return [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {"tagName": "li", "attributes": {"role": "option"}, "id": "OPT1", "text": label},
            ],
        }
    ]


# --------------------------------------------------------------------------- #
# _attr_indicates_aria_invalid — string-normalized truthiness (never bare bool)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("grammar", True),
        ("spelling", True),
        (True, True),
        ("false", False),
        ("False", False),
        (False, False),
        ("", False),
        (None, False),
    ],
)
def test_attr_indicates_aria_invalid(raw: object, expected: bool) -> None:
    assert handler._attr_indicates_aria_invalid(raw) is expected


# --------------------------------------------------------------------------- #
# _incremental_tree_contains_option_with_target_value — option-only matching
# --------------------------------------------------------------------------- #
def test_option_helper_matches_option_label() -> None:
    assert handler._incremental_tree_contains_option_with_target_value(_listbox_with_option(_TARGET), _TARGET) is True


def test_option_helper_ignores_no_results_banner() -> None:
    banner = [{"tagName": "div", "attributes": {"role": "status"}, "text": f"No results for {_TARGET}"}]
    assert handler._incremental_tree_contains_option_with_target_value(banner, _TARGET) is False


def test_option_helper_ignores_bare_li_outside_listbox() -> None:
    # A bare <li> with no listbox/menu ancestor is not an option candidate, so a
    # "No results" line rendered as a stray <li> must not false-trigger.
    stray = [{"tagName": "li", "id": "X", "text": f"No results for {_TARGET}"}]
    assert handler._incremental_tree_contains_option_with_target_value(stray, _TARGET) is False


def test_option_helper_no_match_when_options_lack_target() -> None:
    assert (
        handler._incremental_tree_contains_option_with_target_value(_listbox_with_option("Frontend Developer"), _TARGET)
        is False
    )


def test_option_helper_empty_target_is_false() -> None:
    assert handler._incremental_tree_contains_option_with_target_value(_listbox_with_option(_TARGET), "") is False


# --------------------------------------------------------------------------- #
# _is_commit_required_combobox — combobox (role/aria-autocomplete) AND aria-invalid
# --------------------------------------------------------------------------- #
def _element_with_attrs(attrs: dict[str, object]) -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = "CBX"

    def _get_attr(name: str, *args: object, **kwargs: object) -> object:
        return attrs.get(name)

    el.get_attr = AsyncMock(side_effect=_get_attr)
    return el


@pytest.mark.asyncio
async def test_commit_required_true_for_invalid_both_combobox() -> None:
    el = _element_with_attrs({"role": "combobox", "aria-autocomplete": "both", "aria-invalid": "true"})
    assert await handler._is_commit_required_combobox(el) is True


@pytest.mark.asyncio
async def test_commit_required_true_via_aria_autocomplete_list() -> None:
    el = _element_with_attrs({"role": None, "aria-autocomplete": "list", "aria-invalid": "true"})
    assert await handler._is_commit_required_combobox(el) is True


@pytest.mark.asyncio
async def test_commit_required_false_when_valid() -> None:
    el = _element_with_attrs({"role": "combobox", "aria-autocomplete": "both", "aria-invalid": "false"})
    assert await handler._is_commit_required_combobox(el) is False


@pytest.mark.asyncio
async def test_commit_required_false_when_aria_invalid_absent() -> None:
    el = _element_with_attrs({"role": "combobox", "aria-autocomplete": "both", "aria-invalid": None})
    assert await handler._is_commit_required_combobox(el) is False


@pytest.mark.asyncio
async def test_commit_required_false_for_non_combobox() -> None:
    el = _element_with_attrs({"role": "textbox", "aria-autocomplete": None, "aria-invalid": "true"})
    assert await handler._is_commit_required_combobox(el) is False


# --------------------------------------------------------------------------- #
# handle_input_text_action — end-to-end wiring of the combobox-commit branch
# --------------------------------------------------------------------------- #
def _pressed_keys(el: MagicMock) -> list[str]:
    return [call.args[0] for call in el.press_key.call_args_list if call.args]


async def _run_combobox_input(
    *,
    attrs: dict[str, object],
    options: list[dict],
    select_success: bool,
    stop_flag: bool,
    is_search_bar: bool = False,
    is_location_input: bool = False,
    is_secret: bool = False,
) -> tuple[list, MagicMock, MagicMock]:
    skyvern_el = make_input_element_mock(element_id="CBX", attrs=attrs)
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    if is_secret:
        # Secret-valued params skip Block A's ArrowDown probe (its guard excludes secrets),
        # so Block B is the first and only incremental read.
        inc.get_incremental_element_tree = AsyncMock(return_value=options)
    else:
        # Block A (ArrowDown probe on the empty field) surfaces nothing; Block B (after typing) surfaces options.
        inc.get_incremental_element_tree = AsyncMock(side_effect=[[], options])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}

    context = InputOrSelectContext(field="Title", is_search_bar=is_search_bar, is_location_input=is_location_input)

    select_result = MagicMock()
    select_result.action_result = ActionSuccess() if select_success else ActionFailure(Exception("not committed"))

    # A secret makes the resolved text differ from action.text, so is_secret_value becomes True.
    action_text = "{{secret_param}}" if is_secret else _TARGET
    action = InputTextAction(element_id="CBX", text=action_text, reasoning="type the job title")
    action.stop_batch_after_dropdown_select = stop_flag

    select_mock = AsyncMock(return_value=select_result)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=_TARGET,
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        results = await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )
    return results, skyvern_el, select_mock


_INVALID_BOTH = {"role": "combobox", "aria-autocomplete": "both", "aria-invalid": "true"}


@pytest.mark.asyncio
async def test_invalid_combobox_commits_and_suppresses_tab() -> None:
    """Invalid combobox + option matching the typed value -> one forced selection, no Tab hack."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH, options=_listbox_with_option(_TARGET), select_success=True, stop_flag=False
    )
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    select_mock.assert_awaited_once()
    assert select_mock.await_args.kwargs["force_select"] is True
    assert select_mock.await_args.kwargs["target_value"] == _TARGET
    assert "Tab" not in _pressed_keys(el)
    assert not results[0].skip_remaining_actions


@pytest.mark.asyncio
async def test_invalid_combobox_commit_stops_batch_when_flagged() -> None:
    """A trailing clobbering action (flag set) -> skip_remaining_actions=True, mirroring search-bar semantics."""
    results, _el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH, options=_listbox_with_option(_TARGET), select_success=True, stop_flag=True
    )
    select_mock.assert_awaited_once()
    assert len(results) == 1 and results[0].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_non_combobox_does_not_trigger_select() -> None:
    """A plain textbox (not a combobox) must never enter the deterministic selection path."""
    results, el, select_mock = await _run_combobox_input(
        attrs={"role": "textbox", "aria-autocomplete": None, "aria-invalid": "true"},
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=True,
    )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_valid_combobox_does_not_trigger_select() -> None:
    """A combobox already reporting aria-invalid=false has committed; no selection needed."""
    results, _el, select_mock = await _run_combobox_input(
        attrs={"role": "combobox", "aria-autocomplete": "both", "aria-invalid": "false"},
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=True,
    )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_no_matching_option_does_not_trigger_select() -> None:
    """Invalid combobox but the dropdown has no option matching the typed value -> no selection."""
    results, _el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH,
        options=_listbox_with_option("Frontend Developer"),
        select_success=True,
        stop_flag=True,
    )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_selection_failure_falls_back_to_tab() -> None:
    """When the forced selection fails to commit, behavior degrades to today's Tab hack + ActionSuccess."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH, options=_listbox_with_option(_TARGET), select_success=False, stop_flag=False
    )
    select_mock.assert_awaited_once()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert "Tab" in _pressed_keys(el)


@pytest.mark.asyncio
async def test_search_bar_does_not_use_combobox_branch() -> None:
    """Search bars keep their own path; the combobox branch (which force-selects) must not fire for them."""
    # A search bar whose surfaced tree has no target match: the search-bar branch is skipped, and the
    # combobox branch must not pick it up either (guardrail: search-bar behavior unchanged).
    results, _el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH,
        options=_listbox_with_option("Frontend Developer"),
        select_success=True,
        stop_flag=True,
        is_search_bar=True,
    )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_secret_valued_action_does_not_trigger_select() -> None:
    """A secret-valued parameter must never enter the selection path: its value would otherwise be
    logged (target_value=...) and sent into the custom-select LLM prompt via target_value=text."""
    results, _el, select_mock = await _run_combobox_input(
        attrs=_INVALID_BOTH,
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=True,
        is_secret=True,
    )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
