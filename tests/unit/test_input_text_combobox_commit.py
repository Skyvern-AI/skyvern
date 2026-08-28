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
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
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
# aria-disabled empty-state placeholder must not admit the force-select gate
# (an empty-state row rendered as role=option aria-disabled=true echoing
#  "No results for <target>" alongside a genuine unrelated enabled option)
# --------------------------------------------------------------------------- #
def _listbox_disabled_placeholder_plus_enabled(
    target: str, *, aria_disabled: str = "true", enabled_label: str = "Frontend Developer"
) -> list[dict]:
    return [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option", "aria-disabled": aria_disabled},
                    "id": "OPTD",
                    "text": f"No results for {target}",
                },
                {"tagName": "li", "attributes": {"role": "option"}, "id": "OPT9", "text": enabled_label},
            ],
        }
    ]


def _listbox_disabled_nested_placeholder(target: str) -> list[dict]:
    # The echoed target is split across nested spans (the disabled row has empty own text), so it is only
    # reachable through the subtree-text arm.
    return [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option", "aria-disabled": "true"},
                    "id": "OPTD",
                    "children": [
                        {"tagName": "span", "text": "No results for "},
                        {"tagName": "span", "text": target},
                    ],
                },
                {"tagName": "li", "attributes": {"role": "option"}, "id": "OPT9", "text": "Frontend Developer"},
            ],
        }
    ]


def test_option_helper_excludes_aria_disabled_placeholder_echo() -> None:
    # RED on the starting head: the disabled placeholder's label "No results for <target>" is admitted as an
    # option candidate, so the gate returns True and force_select would commit against a disabled placeholder.
    tree = _listbox_disabled_placeholder_plus_enabled(_TARGET)
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is False


def test_option_subtree_gate_excludes_aria_disabled_nested_echo() -> None:
    tree = _listbox_disabled_nested_placeholder(_TARGET)
    assert handler._incremental_tree_contains_option_subtree_with_target_value(tree, _TARGET) is False


# positive controls: enabled representations must stay eligible after the disabled exclusion
def test_option_subtree_gate_matches_enabled_deferred_nested_target() -> None:
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option"},
                    "id": "OPT1",
                    "children": [
                        {"tagName": "span", "text": _TARGET[:4]},
                        {"tagName": "span", "text": _TARGET[4:]},
                    ],
                }
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_subtree_with_target_value(tree, _TARGET) is True


def test_option_gate_matches_enabled_roleless_li_in_choice_surface() -> None:
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [{"tagName": "li", "id": "OPT1", "text": _TARGET}],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is True


def test_option_gate_aria_disabled_false_option_still_eligible() -> None:
    # aria-disabled="false" is not disabled, so an enabled option carrying the target stays eligible.
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option", "aria-disabled": "false"},
                    "id": "OPT1",
                    "text": _TARGET,
                }
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is True


# --------------------------------------------------------------------------- #
# INHERITED disabled state must exclude descendant options/choices from the
# custom-select candidate/gate seams (a disabled ancestor disables the subtree)
# --------------------------------------------------------------------------- #
def _disabled_listbox_with_target_option(target: str, *, container_disabled: str = "true") -> list[dict]:
    return [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox", "aria-disabled": container_disabled},
            "children": [{"tagName": "li", "attributes": {"role": "option"}, "id": "OPT1", "text": target}],
        }
    ]


def test_option_helper_excludes_option_under_aria_disabled_listbox() -> None:
    # RED on starting head: the option's own attrs are not disabled, so it is admitted even though its
    # listbox ancestor is aria-disabled=true.
    tree = _disabled_listbox_with_target_option(_TARGET)
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is False


def test_option_subtree_gate_excludes_option_under_aria_disabled_container() -> None:
    tree = [
        {
            "tagName": "div",
            "attributes": {"role": "listbox", "aria-disabled": "true"},
            "children": [
                {
                    "tagName": "div",
                    "attributes": {"role": "option"},
                    "id": "OPT1",
                    "children": [{"tagName": "span", "text": _TARGET[:4]}, {"tagName": "span", "text": _TARGET[4:]}],
                }
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_subtree_with_target_value(tree, _TARGET) is False


def test_option_helper_excludes_choice_input_under_disabled_fieldset() -> None:
    # A radio/checkbox descendant of an HTML-disabled <fieldset> (boolean attribute) is disabled by
    # inheritance. On the starting head the input's own attrs are enabled and its aria-label carries the
    # target, so it is admitted as a choice-input candidate; only ancestor inheritance excludes it.
    tree = [
        {
            "tagName": "fieldset",
            "attributes": {"disabled": ""},
            "children": [
                {"tagName": "input", "attributes": {"type": "radio", "aria-label": _TARGET}, "id": "INP1"},
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is False


def test_option_helper_excludes_nested_input_under_disabled_label_wrapper() -> None:
    # Skipped-wrapper / eligible-child leak: the label wrapper is aria-disabled=true, so the current head
    # skips the wrapper candidate but still admits the nested input as a separate eligible candidate.
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "label",
                    "attributes": {"aria-disabled": "true"},
                    "id": "LBL",
                    "children": [
                        {"tagName": "input", "attributes": {"type": "radio", "aria-label": _TARGET}, "id": "INP1"}
                    ],
                }
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is False


def test_candidates_excludes_inherited_disabled_option() -> None:
    # Candidate-production boundary: no candidate carrying the target may be produced under a disabled ancestor.
    cands = handler._custom_select_candidates_from_elements(_disabled_listbox_with_target_option(_TARGET))
    assert all(_TARGET.lower() not in str(c.get("label") or "").lower() for c in cands)


def test_option_helper_inherited_disabled_not_overridden_by_child_aria_disabled_false() -> None:
    # Monotonic inheritance: a descendant aria-disabled=false cannot re-enable an inherited-disabled ancestor.
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox", "aria-disabled": "true"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option", "aria-disabled": "false"},
                    "id": "OPT1",
                    "text": _TARGET,
                }
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is False


def test_option_helper_ancestor_aria_disabled_false_keeps_option_eligible() -> None:
    tree = _disabled_listbox_with_target_option(_TARGET, container_disabled="false")
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is True


def test_option_helper_disabled_sibling_does_not_suppress_enabled_sibling() -> None:
    # A disabled option sibling must not suppress an enabled sibling that carries the target.
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option", "aria-disabled": "true"},
                    "id": "OPTD",
                    "text": "Frontend Developer",
                },
                {"tagName": "li", "attributes": {"role": "option"}, "id": "OPT2", "text": _TARGET},
            ],
        }
    ]
    assert handler._incremental_tree_contains_option_with_target_value(tree, _TARGET) is True


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


def _typed_values(el: MagicMock) -> list[str]:
    return [call.args[0] if call.args else call.kwargs["text"] for call in el.input_sequentially.call_args_list]


def _written_values(el: MagicMock) -> list[str]:
    # Write-method-agnostic: after the fill-first flip (SKY-13821) a signal-less input writes its fall-through
    # value with one atomic fill, while a search-bar/combobox keeps per-character typing. Tests that only care
    # that the value was written on fall-through use this instead of asserting the write mechanism.
    calls = [*el.input_sequentially.call_args_list, *el.input_fill.call_args_list]
    return [call.args[0] if call.args else call.kwargs["text"] for call in calls]


async def _run_combobox_input(
    *,
    attrs: dict[str, object],
    options: list[dict],
    select_success: bool,
    stop_flag: bool,
    is_search_bar: bool = False,
    is_location_input: bool = False,
    is_date_related: bool = False,
    is_secret: bool = False,
    prefilter_typeahead: bool = False,
    prefilter_raises: bool = False,
    use_base_action: bool = False,
    first_block_incremental: list[dict] | None = None,
    terminal_failure: bool = False,
    nonterminal_skip: bool = False,
    nonterminal_failure_skip: bool = False,
) -> tuple[list, MagicMock, MagicMock]:
    skyvern_el = make_input_element_mock(element_id="CBX", attrs=attrs)
    if prefilter_raises:
        # Simulate the prefilter typing a prefix then raising mid-dispatch (field left dirty). Raise only on
        # the first call (the Block A prefilter); later calls succeed, so the rest of the flow runs normally.
        _raised = {"done": False}

        def _raise_first(*_args: object, **_kwargs: object) -> None:
            if not _raised["done"]:
                _raised["done"] = True
                raise RuntimeError("partial prefilter dispatch then raise")

        skyvern_el.input_sequentially = AsyncMock(side_effect=_raise_first)
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    if first_block_incremental is not None:
        # A typeahead that surfaces options in Block A itself (after the target is typed to filter).
        inc.get_incremental_element_tree = AsyncMock(return_value=first_block_incremental)
    elif is_secret:
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

    context = InputOrSelectContext(
        field="Title",
        is_search_bar=is_search_bar,
        is_location_input=is_location_input,
        is_date_related=is_date_related,
    )

    select_result = MagicMock()
    if terminal_failure:
        select_result.action_result, _ = handler._terminal_custom_select_failure(
            target_value=_TARGET,
            matched_label=_TARGET,
        )
    elif nonterminal_failure_skip:
        select_result.action_result = ActionFailure(Exception("not committed"))
        select_result.action_result.skip_remaining_actions = True
    elif nonterminal_skip:
        select_result.action_result = ActionResult(success=False, skip_remaining_actions=True)
    else:
        select_result.action_result = ActionSuccess() if select_success else ActionFailure(Exception("not committed"))

    # A secret makes the resolved text differ from action.text, so is_secret_value becomes True.
    action_text = "{{secret_param}}" if is_secret else _TARGET
    if use_base_action:
        # Production hydrates/replays INPUT_TEXT actions as base ``Action`` (see hydrate_action /
        # get_task_actions), which the dispatcher routes by action_type. Do NOT set the runtime hint here —
        # a base Action lacks it until the fix moves it onto the base model.
        action = Action(action_type=ActionType.INPUT_TEXT, element_id="CBX", text=action_text, reasoning="type it")
        action.stop_batch_after_dropdown_select = stop_flag
    else:
        action = InputTextAction(element_id="CBX", text=action_text, reasoning="type the job title")
        action.stop_batch_after_dropdown_select = stop_flag
        # Admission marks eligible actions; the handler consumes only this runtime flag.
        action.prefilter_typeahead = prefilter_typeahead

    select_mock = AsyncMock(return_value=select_result)
    input_value_mock = AsyncMock(side_effect=["", _TARGET]) if is_secret else AsyncMock(return_value="")

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=input_value_mock),
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
    assert select_mock.await_args.kwargs["entry_action_type"] == "input_text"
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
    results, _el, select_mock = await _run_combobox_input(
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
    with patch.object(
        handler.app.WORKFLOW_CONTEXT_MANAGER,
        "mask_secrets_enabled_for_run",
        MagicMock(return_value=True),
    ):
        results, el, select_mock = await _run_combobox_input(
            attrs=_INVALID_BOTH,
            options=_listbox_with_option(_TARGET),
            select_success=True,
            stop_flag=True,
            is_secret=True,
        )
    select_mock.assert_not_awaited()
    el.apply_secret_visual_mask.assert_awaited_once_with()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_secret_valued_search_bar_does_not_trigger_select() -> None:
    """A secret in a search bar must not enter the custom-select path either: it logs target_value=... and
    feeds it into the LLM prompt. Mirrors the secret-combobox guard so secret typed-widgets type sequentially
    and rely on the Tab hack instead of the logging select (SKY-13821)."""
    with patch.object(
        handler.app.WORKFLOW_CONTEXT_MANAGER,
        "mask_secrets_enabled_for_run",
        MagicMock(return_value=True),
    ):
        results, el, select_mock = await _run_combobox_input(
            attrs={"role": "textbox", "aria-autocomplete": None, "aria-invalid": "false"},
            options=_listbox_with_option(_TARGET),
            select_success=True,
            stop_flag=True,
            is_search_bar=True,
            is_secret=True,
        )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_maxlength_short_secret_does_not_trigger_select() -> None:
    """A maxlength-constrained secret routes to sequential auto-advance entry; even if a dropdown surfaces, a
    plain (non-typeahead) field must not enter the custom-select that logs target_value (SKY-13821)."""
    with patch.object(
        handler.app.WORKFLOW_CONTEXT_MANAGER,
        "mask_secrets_enabled_for_run",
        MagicMock(return_value=True),
    ):
        results, el, select_mock = await _run_combobox_input(
            attrs={"role": None, "aria-autocomplete": None, "aria-invalid": "false", "maxlength": "4"},
            options=_listbox_with_option(_TARGET),
            select_success=True,
            stop_flag=True,
            is_secret=True,
        )
    select_mock.assert_not_awaited()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


# --------------------------------------------------------------------------- #
# handle_input_text_action — runtime prefilter_typeahead flag drives type-before-match
#
# Field admission (which sites/fields qualify) is a Cloud Setup concern and is not tested here. The OSS
# handler consumes only the generic runtime-only InputTextAction.prefilter_typeahead flag, gated by the
# existing safety checks (non-empty resolved text, not date-related, plus the enclosing
# search/location/secret/TOTP/raw exclusions). No site/field strings appear in this OSS-synced file.
# --------------------------------------------------------------------------- #
def test_prefilter_typeahead_flag_excluded_from_serialization() -> None:
    # Runtime-only: set per-step by Cloud Setup, never persisted/serialized.
    action = InputTextAction(element_id="CBX", text=_TARGET)
    action.prefilter_typeahead = True
    assert "prefilter_typeahead" not in action.model_dump()
    assert action.prefilter_typeahead is True


_FLAG_TYPEAHEAD_ATTRS = {"role": None, "aria-autocomplete": None, "aria-invalid": "false"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attrs", "is_search_bar", "first_block_incremental", "expected_typed_values"),
    [
        pytest.param(
            _FLAG_TYPEAHEAD_ATTRS,
            False,
            _listbox_with_option(_TARGET),
            [],
            id="autocomplete-detect",
        ),
        pytest.param(
            {"role": "textbox", "aria-autocomplete": None, "aria-invalid": "false"},
            True,
            _listbox_with_option(_TARGET),
            [_TARGET],
            id="search-bar",
        ),
        pytest.param(_INVALID_BOTH, False, None, [_TARGET], id="invalid-combobox"),
    ],
)
async def test_terminal_custom_select_failure_stops_each_input_text_caller(
    attrs: dict[str, object],
    is_search_bar: bool,
    first_block_incremental: list[dict] | None,
    expected_typed_values: list[str],
) -> None:
    results, el, select_mock = await _run_combobox_input(
        attrs=attrs,
        options=_listbox_with_option(_TARGET),
        select_success=False,
        stop_flag=True,
        is_search_bar=is_search_bar,
        first_block_incremental=first_block_incremental,
        terminal_failure=True,
    )

    failure = select_mock.return_value.action_result
    assert results[0] is failure
    assert isinstance(failure, ActionFailure)
    assert not isinstance(failure, ActionSuccess)
    assert failure.skip_remaining_actions is True
    assert _typed_values(el) == expected_typed_values
    assert "Tab" not in _pressed_keys(el)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attrs", "is_search_bar", "first_block_incremental"),
    [
        pytest.param(_FLAG_TYPEAHEAD_ATTRS, False, _listbox_with_option(_TARGET), id="autocomplete-detect"),
        pytest.param(
            {"role": "textbox", "aria-autocomplete": None, "aria-invalid": "false"},
            True,
            _listbox_with_option(_TARGET),
            id="search-bar",
        ),
        pytest.param(_INVALID_BOTH, False, None, id="invalid-combobox"),
    ],
)
async def test_nonterminal_skip_carrier_falls_through_each_input_text_caller(
    attrs: dict[str, object],
    is_search_bar: bool,
    first_block_incremental: list[dict] | None,
) -> None:
    results, el, select_mock = await _run_combobox_input(
        attrs=attrs,
        options=_listbox_with_option(_TARGET),
        select_success=False,
        stop_flag=False,
        is_search_bar=is_search_bar,
        first_block_incremental=first_block_incremental,
        nonterminal_skip=True,
    )

    select_mock.assert_awaited_once()
    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
    assert results[0] is not select_mock.return_value.action_result
    assert _written_values(el) == [_TARGET]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attrs", "is_search_bar", "first_block_incremental"),
    [
        pytest.param(_FLAG_TYPEAHEAD_ATTRS, False, _listbox_with_option(_TARGET), id="autocomplete-detect"),
        pytest.param(
            {"role": "textbox", "aria-autocomplete": None, "aria-invalid": "false"},
            True,
            _listbox_with_option(_TARGET),
            id="search-bar",
        ),
        pytest.param(_INVALID_BOTH, False, None, id="invalid-combobox"),
    ],
)
async def test_date_related_failure_with_skip_falls_through_each_input_text_caller(
    attrs: dict[str, object],
    is_search_bar: bool,
    first_block_incremental: list[dict] | None,
) -> None:
    """A datepicker ActionFailure+skip lacks the custom-select terminal marker and falls through."""
    results, el, select_mock = await _run_combobox_input(
        attrs=attrs,
        options=_listbox_with_option(_TARGET),
        select_success=False,
        stop_flag=False,
        is_search_bar=is_search_bar,
        first_block_incremental=first_block_incremental,
        nonterminal_failure_skip=True,
        is_date_related=True,
    )

    select_mock.assert_awaited_once()
    assert len(results) == 1
    assert isinstance(results[0], ActionSuccess)
    assert results[0] is not select_mock.return_value.action_result
    assert _written_values(el) == [_TARGET]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attrs", "is_search_bar", "first_block_incremental", "stop_flag", "expected_stop"),
    [
        pytest.param(
            _FLAG_TYPEAHEAD_ATTRS,
            False,
            _listbox_with_option(_TARGET),
            False,
            False,
            id="autocomplete-detect",
        ),
        pytest.param(
            {"role": "textbox", "aria-autocomplete": None, "aria-invalid": "false"},
            True,
            _listbox_with_option(_TARGET),
            True,
            True,
            id="search-bar",
        ),
        pytest.param(_INVALID_BOTH, False, None, True, True, id="invalid-combobox"),
    ],
)
async def test_successful_custom_select_preserves_each_input_text_caller(
    attrs: dict[str, object],
    is_search_bar: bool,
    first_block_incremental: list[dict] | None,
    stop_flag: bool,
    expected_stop: bool,
) -> None:
    results, _el, select_mock = await _run_combobox_input(
        attrs=attrs,
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=stop_flag,
        is_search_bar=is_search_bar,
        first_block_incremental=first_block_incremental,
    )

    success = select_mock.return_value.action_result
    assert results[0] is success
    assert isinstance(success, ActionSuccess)
    assert bool(success.skip_remaining_actions) is expected_stop


@pytest.mark.asyncio
async def test_prefilter_flag_types_target_to_filter_before_match() -> None:
    """With prefilter_typeahead set (by Cloud Setup), Block A types the target to filter the listbox
    before candidate matching, instead of opening it unfiltered with ArrowDown."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_FLAG_TYPEAHEAD_ATTRS,
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=False,
        prefilter_typeahead=True,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    # entered the target to filter the listbox ...
    entered = [call.args[0] for call in el.input_sequentially.await_args_list if call.args]
    assert _TARGET in entered
    # ... and did NOT fall back to the unfiltered ArrowDown probe
    assert "ArrowDown" not in _pressed_keys(el)
    # the custom-select ran against the filtered listbox and committed the option
    select_mock.assert_awaited_once()
    assert select_mock.await_args.kwargs["target_value"] == _TARGET
    assert select_mock.await_args.kwargs["entry_action_type"] == "input_text"
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_prefilter_flag_failure_clears_before_terminal_fill() -> None:
    """When the flagged prefilter types the target but the block select does NOT commit, the terminal fill
    must clear first so the typed-but-uncommitted value is not doubled (e.g. 'BackendBackend') on the
    fall-through path."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_FLAG_TYPEAHEAD_ATTRS,
        options=_listbox_with_option(_TARGET),
        select_success=False,
        stop_flag=False,
        prefilter_typeahead=True,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    # prefilter entered the target (Block A) ...
    entered = [call.args[0] for call in el.input_sequentially.await_args_list if call.args]
    assert _TARGET in entered
    # ... the select was attempted but did not commit ...
    select_mock.assert_awaited()
    # ... so the field is cleared again before the terminal fill (Block A clear + terminal clear).
    assert el.input_clear.await_count >= 2
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_prefilter_partial_dispatch_failure_clears_before_terminal_fill() -> None:
    """If the flagged prefilter's input_sequentially dispatches a prefix then raises (field left dirty)
    with initially empty current_text, the terminal fill must still clear first — otherwise it appends the
    full target to the dirty prefix. It falls back to ArrowDown and clears before the final fill."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_FLAG_TYPEAHEAD_ATTRS,
        options=_listbox_with_option(_TARGET),
        select_success=False,
        stop_flag=False,
        prefilter_typeahead=True,
        prefilter_raises=True,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    # the prefilter was attempted (dispatched then raised) ...
    assert el.input_sequentially.call_count >= 1
    # ... so it fell back to the unfiltered ArrowDown probe ...
    assert "ArrowDown" in _pressed_keys(el)
    # ... and the terminal path cleared the dirty field before the final fill (Block A clear + terminal clear),
    # even though current_text was empty and prefilter_typeahead was reset to False on the exception.
    assert el.input_clear.await_count >= 2
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_flag_off_keeps_arrowdown_probe() -> None:
    """Control: with the flag off the input must NOT be pre-filtered — it keeps the ArrowDown probe and
    never types the target as a filter before the block select."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_FLAG_TYPEAHEAD_ATTRS,
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=False,
        prefilter_typeahead=False,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    assert "ArrowDown" in _pressed_keys(el)
    entered = [call.args[0] for call in el.input_sequentially.await_args_list if call.args]
    assert _TARGET not in entered
    select_mock.assert_awaited_once()
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_date_related_overrides_flag_and_keeps_arrowdown() -> None:
    """The is_date_related safety gate overrides the flag: even with prefilter_typeahead set, a date input
    performs no prefilter and retains the ArrowDown path (date pickers must keep the existing flow)."""
    results, el, select_mock = await _run_combobox_input(
        attrs=_FLAG_TYPEAHEAD_ATTRS,
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=False,
        is_date_related=True,
        prefilter_typeahead=True,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    assert "ArrowDown" in _pressed_keys(el)
    entered = [call.args[0] for call in el.input_sequentially.await_args_list if call.args]
    assert _TARGET not in entered
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


# --------------------------------------------------------------------------- #
# Regression: handle_input_text_action runs at runtime with base `Action` objects (hydrated/replayed),
# not only `InputTextAction`. The runtime hint must live on the base model so reading it never raises.
# --------------------------------------------------------------------------- #
def test_prefilter_typeahead_is_a_base_action_field_defaulting_false() -> None:
    # Reproduces the production runtime shape: base Action for INPUT_TEXT (what hydrate_action /
    # Action.model_validate produce). The hint must exist there, default False, and stay settable on the
    # subclass. Unmodified code raises AttributeError on `base.prefilter_typeahead`.
    base = Action(action_type=ActionType.INPUT_TEXT, element_id="X", text="hello")
    assert type(base) is Action
    assert base.prefilter_typeahead is False
    assert InputTextAction(element_id="X", text="hello").prefilter_typeahead is False
    assert InputTextAction(element_id="X", text="hello", prefilter_typeahead=True).prefilter_typeahead is True


@pytest.mark.asyncio
async def test_base_action_input_text_does_not_raise_and_keeps_default_probe() -> None:
    """A base `Action` (INPUT_TEXT) flowing through the handler must not raise AttributeError on the hint;
    with no hint it falls back to the default ArrowDown probe. Unmodified code raises at the hint read."""
    results, el, select_mock = await _run_combobox_input(
        attrs={"role": None, "aria-autocomplete": None, "aria-invalid": "false"},
        options=_listbox_with_option(_TARGET),
        select_success=True,
        stop_flag=False,
        use_base_action=True,
        first_block_incremental=_listbox_with_option(_TARGET),
    )
    assert "ArrowDown" in _pressed_keys(el)
    entered = [call.args[0] for call in el.input_sequentially.await_args_list if call.args]
    assert _TARGET not in entered
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


# --------------------------------------------------------------------------- #
# handle_input_text_action — search-bar deferred-render re-observation (SKY-14275)
#
# A search-bar combobox whose filtered option is absent from the first post-input snapshot must
# render-settle and re-read until it surfaces, then reach the custom-select owner seam. When the target
# is already present, or the field is not a search bar, no settle happens.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_search_bar", "attrs", "target_present_first", "expect_settle"),
    [
        pytest.param(True, {"role": "combobox", "aria-autocomplete": "list"}, False, True, id="search-bar-deferred"),
        pytest.param(True, {"role": "combobox", "aria-autocomplete": "list"}, True, False, id="search-bar-present"),
        pytest.param(False, {"role": "textbox"}, True, False, id="ordinary-no-settle"),
    ],
)
async def test_search_bar_render_settle_re_observation(
    is_search_bar: bool,
    attrs: dict[str, object],
    target_present_first: bool,
    expect_settle: bool,
) -> None:
    state = {"settled": False, "calls": 0}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1
        state["settled"] = True

    def _incremental(*_a: object, **_k: object) -> list[dict]:
        # The target surfaces on the first read only when present-first; otherwise only after a settle.
        if target_present_first or state["settled"]:
            return _listbox_with_option(_TARGET)
        return []

    skyvern_el = make_input_element_mock(element_id="CBX", attrs=attrs)
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(side_effect=_incremental)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}
    context = InputOrSelectContext(field="Account", is_search_bar=is_search_bar)

    select_result = MagicMock()
    select_result.action_result = ActionSuccess()
    select_mock = AsyncMock(return_value=select_result)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_TARGET),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._wait_custom_select_render_settle", new=_settle_spy),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        await handle_input_text_action(
            action=InputTextAction(element_id="CBX", text=_TARGET, reasoning="type it"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    assert (state["calls"] > 0) is expect_settle
    # The search-bar path reaches the owner seam with the target once it surfaces (immediately when
    # present, after the settle when deferred); the ordinary path here only asserts it adds no settle.
    if is_search_bar:
        select_mock.assert_awaited_once()
        assert select_mock.await_args.kwargs["target_value"] == _TARGET


@pytest.mark.asyncio
async def test_search_bar_banner_only_target_does_not_force_select() -> None:
    """Adversarial safety: the target text appears only in a non-option status banner while an unrelated
    selectable option is present. The forced search-bar custom-select must NOT be entered -- otherwise
    force_select=True would commit the unrelated option. The seam mock here would report a committed
    selection if awaited (it is not stubbed to a quiet no-match), so a wrong entry is caught."""
    banner_only_tree = [
        {"tagName": "div", "attributes": {"role": "status"}, "text": f"Showing results for {_TARGET}", "children": []},
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {"tagName": "li", "attributes": {"role": "option"}, "id": "OPT9", "text": "Frontend Developer"}
            ],
        },
    ]

    async def _settle_spy(_element: object) -> None:
        return None

    skyvern_el = make_input_element_mock(element_id="CBX", attrs={"role": "combobox", "aria-autocomplete": "list"})
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=banner_only_tree)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}
    context = InputOrSelectContext(field="Account", is_search_bar=True)

    committed = MagicMock()
    committed.action_result = ActionSuccess()  # a forced selection would look like a committed success
    select_mock = AsyncMock(return_value=committed)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_TARGET),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._wait_custom_select_render_settle", new=_settle_spy),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        await handle_input_text_action(
            action=InputTextAction(element_id="CBX", text=_TARGET, reasoning="type it"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    # No selectable option carries the target, so the forced custom-select sink must never be reached.
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_bar_aria_disabled_placeholder_does_not_force_select() -> None:
    """Adversarial safety: the target text appears only in an aria-disabled empty-state option
    ("No results for <target>") while an unrelated enabled option is present. The forced search-bar
    custom-select must NOT be entered -- otherwise force_select=True would commit against the disabled
    placeholder (or the unrelated enabled option). The seam mock reports a committed selection if awaited."""
    disabled_placeholder_tree = _listbox_disabled_placeholder_plus_enabled(_TARGET)

    async def _settle_spy(_element: object) -> None:
        return None

    skyvern_el = make_input_element_mock(element_id="CBX", attrs={"role": "combobox", "aria-autocomplete": "list"})
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=disabled_placeholder_tree)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}
    context = InputOrSelectContext(field="Account", is_search_bar=True)

    committed = MagicMock()
    committed.action_result = ActionSuccess()  # a forced selection would look like a committed success
    select_mock = AsyncMock(return_value=committed)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_TARGET),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._wait_custom_select_render_settle", new=_settle_spy),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        await handle_input_text_action(
            action=InputTextAction(element_id="CBX", text=_TARGET, reasoning="type it"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    # The only target-bearing option is aria-disabled, so the forced custom-select sink must never be reached.
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surfaced_tree",
    [
        pytest.param(
            # Real ui-select shape: the option's value renders in nested spans, the option node's own text is
            # empty. Reducing the match to node.get("text") (no subtree walk) would miss it and go RED.
            [
                {
                    "tagName": "div",
                    "attributes": {"role": "option"},
                    "text": "",
                    "children": [
                        {"tagName": "span", "text": "ACME", "children": []},
                        {"tagName": "b", "text": _TARGET, "children": []},
                    ],
                }
            ],
            id="nested-spans-option",
        ),
        pytest.param(
            # Role-less <li> option inside a role=listbox choice surface (jQuery-UI / select2 v3 / typeahead
            # family) -- eligible via the selector's `tag == "li" and in_choice_surface`, previously dropped.
            [
                {
                    "tagName": "ul",
                    "attributes": {"role": "listbox"},
                    "children": [
                        {"tagName": "li", "text": "", "children": [{"tagName": "a", "text": _TARGET, "children": []}]}
                    ],
                }
            ],
            id="roleless-li-in-choice-surface",
        ),
        pytest.param(
            # Target split across sibling text nodes in document order. A reverse-order subtree walk would
            # scramble it ("EngineerBackend") and miss the match, so this guards the document-order collector.
            [
                {
                    "tagName": "div",
                    "attributes": {"role": "option"},
                    "text": "",
                    "children": [
                        {"tagName": "b", "text": "Backend ", "children": []},
                        {"tagName": "b", "text": "Engineer", "children": []},
                    ],
                }
            ],
            id="split-across-siblings-document-order",
        ),
        pytest.param(
            # Multi-select filter combobox: a bare checkbox row labelled via aria-label (selector admits via
            # is_choice_input). Covered by the label-candidate arm, not the subtree pass.
            [{"tagName": "input", "id": "CHK", "attributes": {"type": "checkbox", "aria-label": _TARGET}}],
            id="checkbox-input-aria-label",
        ),
        pytest.param(
            # A <label> wrapping a checkbox, target in the label text (selector admits via is_label_choice).
            [
                {
                    "tagName": "label",
                    "id": "LBL",
                    "text": _TARGET,
                    "children": [{"tagName": "input", "id": "CHK2", "attributes": {"type": "checkbox"}}],
                }
            ],
            id="label-wrapped-checkbox",
        ),
        pytest.param(
            # An option whose label lives only in aria-label (empty text), so the subtree pass alone would
            # miss it -- the label-candidate arm covers it via _select_shadow_label_from_node.
            [{"tagName": "div", "id": "OPT", "attributes": {"role": "option", "aria-label": _TARGET}, "text": ""}],
            id="attribute-only-label-option",
        ),
    ],
)
async def test_search_bar_gate_admits_selector_eligible_option(surfaced_tree: list[dict]) -> None:
    """Eligibility mirrors the selector's candidate set: an option whose value renders in nested spans, and a
    role-less <li> in a choice surface, must reach the forced custom-select seam with the target."""

    async def _settle_spy(_element: object) -> None:
        return None

    skyvern_el = make_input_element_mock(element_id="CBX", attrs={"role": "combobox", "aria-autocomplete": "list"})
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=surfaced_tree)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}
    context = InputOrSelectContext(field="Account", is_search_bar=True)

    select_result = MagicMock()
    select_result.action_result = ActionSuccess()
    select_mock = AsyncMock(return_value=select_result)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_TARGET),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._wait_custom_select_render_settle", new=_settle_spy),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        await handle_input_text_action(
            action=InputTextAction(element_id="CBX", text=_TARGET, reasoning="type it"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    select_mock.assert_awaited_once()
    assert select_mock.await_args.kwargs["target_value"] == _TARGET


# --------------------------------------------------------------------------- #
# _incremental_tree_has_enabled_selectable_option — the deferred-settle entry gate
# reads "no enabled selectable option candidate at all" straight off the canonical
# candidate producer, so disabled-only snapshots stay settle-eligible.
# --------------------------------------------------------------------------- #
def test_enabled_selectable_gate_true_for_enabled_option() -> None:
    assert handler._incremental_tree_has_enabled_selectable_option(_listbox_with_option(_TARGET)) is True


def test_enabled_selectable_gate_false_for_disabled_only_snapshot() -> None:
    # A snapshot whose only options sit under an aria-disabled container yields no candidate, so it reads as
    # "no enabled selectable option" and the single deferred settle stays eligible.
    tree = _disabled_listbox_with_target_option("Frontend Developer")
    assert handler._incremental_tree_has_enabled_selectable_option(tree) is False


def test_enabled_selectable_gate_false_for_empty_snapshot() -> None:
    assert handler._incremental_tree_has_enabled_selectable_option([]) is False


# --------------------------------------------------------------------------- #
# handle_input_text_action — deferred settle is exactly ONE bounded attempt, gated
# on a live combobox/typeahead whose first snapshot has no enabled option (SKY-6657).
# --------------------------------------------------------------------------- #
async def _drive_search_bar_input(
    *,
    attrs: dict[str, object],
    incremental_side_effect,
    settle_spy,
    select_mock,
    is_search_bar: bool = True,
) -> MagicMock:
    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(side_effect=incremental_side_effect)

    skyvern_el = make_input_element_mock(element_id="CBX", attrs=attrs)
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"CBX": {"tagName": "input"}}
    context = InputOrSelectContext(field="Account", is_search_bar=is_search_bar)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=_TARGET),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
        patch("skyvern.webeye.actions.handler._wait_custom_select_render_settle", new=settle_spy),
        patch("skyvern.webeye.actions.handler.sequentially_select_from_dropdown", new=select_mock),
    ):
        await handle_input_text_action(
            action=InputTextAction(element_id="CBX", text=_TARGET, reasoning="type it"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )
    return inc


@pytest.mark.asyncio
async def test_search_bar_deferred_settles_exactly_once_when_target_never_surfaces() -> None:
    # Deferred-empty race where the target never surfaces: the first snapshot has no enabled option, so
    # exactly one settle + one re-read runs; the still-empty re-read yields no second settle. RED on the
    # pre-fix head, which looped up to three settles (four incremental reads).
    state = {"calls": 0}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1

    inc = await _drive_search_bar_input(
        attrs={"role": "combobox", "aria-autocomplete": "list"},
        incremental_side_effect=lambda *_a, **_k: [],
        settle_spy=_settle_spy,
        select_mock=AsyncMock(),
    )

    assert state["calls"] == 1
    assert inc.get_incremental_element_tree.await_count == 2  # initial read + exactly one re-read


@pytest.mark.asyncio
async def test_search_bar_no_settle_when_live_element_not_combobox() -> None:
    # A search-bar context whose live element is a plain textbox (not combobox/typeahead) must not settle,
    # even with an empty first snapshot. RED on the pre-fix head, which had no combobox/typeahead gate.
    state = {"calls": 0}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1

    inc = await _drive_search_bar_input(
        attrs={"role": "textbox"},
        incremental_side_effect=lambda *_a, **_k: [],
        settle_spy=_settle_spy,
        select_mock=AsyncMock(),
    )

    assert state["calls"] == 0
    assert inc.get_incremental_element_tree.await_count == 1  # initial read only, no re-read


@pytest.mark.asyncio
async def test_search_bar_no_settle_when_populated_no_match() -> None:
    # The first snapshot already carries an enabled (non-target) option: a populated no-match state, not the
    # deferred-empty race. No settle, and the forced custom-select sink is never reached. RED on the pre-fix
    # head, which settled whenever the target was merely absent.
    state = {"calls": 0}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1

    select_mock = AsyncMock()
    inc = await _drive_search_bar_input(
        attrs={"role": "combobox", "aria-autocomplete": "list"},
        incremental_side_effect=lambda *_a, **_k: _listbox_with_option("Frontend Developer"),
        settle_spy=_settle_spy,
        select_mock=select_mock,
    )

    assert state["calls"] == 0
    assert inc.get_incremental_element_tree.await_count == 1
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_bar_disabled_only_snapshot_settles_once_then_commits() -> None:
    # Disabled-only first snapshot (no enabled candidate) is the deferred-empty race: the single settle stays
    # eligible, and once the enabled target surfaces on the re-read the real owner seam commits it.
    state = {"calls": 0, "settled": False}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1
        state["settled"] = True

    def _incremental(*_a: object, **_k: object) -> list[dict]:
        if state["settled"]:
            return _listbox_with_option(_TARGET)
        return _disabled_listbox_with_target_option("Frontend Developer")

    select_result = MagicMock()
    select_result.action_result = ActionSuccess()
    select_mock = AsyncMock(return_value=select_result)

    await _drive_search_bar_input(
        attrs={"role": "combobox", "aria-autocomplete": "list"},
        incremental_side_effect=_incremental,
        settle_spy=_settle_spy,
        select_mock=select_mock,
    )

    assert state["calls"] == 1
    select_mock.assert_awaited_once()
    assert select_mock.await_args.kwargs["target_value"] == _TARGET


@pytest.mark.asyncio
async def test_search_bar_no_settle_when_enabled_nested_span_target_present() -> None:
    # Fast-path preservation: the first snapshot already carries an ENABLED target option whose value renders
    # only in nested spans (empty own text). The label-candidate producer yields no candidate for such a row,
    # so the enabled-selectable predicate reads False -- but the option-subtree gate recognizes the enabled
    # target, so this is a populated present-target state, not the deferred-empty race. No settle, only the
    # initial read, and the existing owner seam still commits. RED on the v9 head, which settled once here.
    tree = [
        {
            "tagName": "ul",
            "attributes": {"role": "listbox"},
            "children": [
                {
                    "tagName": "li",
                    "attributes": {"role": "option"},
                    "id": "OPT1",
                    "text": "",
                    "children": [
                        {"tagName": "span", "text": _TARGET[:4]},
                        {"tagName": "span", "text": _TARGET[4:]},
                    ],
                }
            ],
        }
    ]
    state = {"calls": 0}

    async def _settle_spy(_element: object) -> None:
        state["calls"] += 1

    select_result = MagicMock()
    select_result.action_result = ActionSuccess()
    select_mock = AsyncMock(return_value=select_result)

    inc = await _drive_search_bar_input(
        attrs={"role": "combobox", "aria-autocomplete": "list"},
        incremental_side_effect=lambda *_a, **_k: tree,
        settle_spy=_settle_spy,
        select_mock=select_mock,
    )

    assert state["calls"] == 0
    assert inc.get_incremental_element_tree.await_count == 1  # initial read only, no settle re-read
    select_mock.assert_awaited_once()
    assert select_mock.await_args.kwargs["target_value"] == _TARGET


# --------------------------------------------------------------------------- #
# _custom_select_committed_readback_confirms — the pre-ownership-recovery gate.
# A strict scope read can miss a commit the chosen option itself reflects; the gate honors a visibly
# committed pick before ownership-dependent recovery, but only on an exact-label + committed signal.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_committed_readback_confirms_selected_option_with_exact_label() -> None:
    element = MagicMock()
    state = {"label": "Yes", "role": "option", "selectedAttr": True}
    with patch.object(handler, "_read_custom_select_matched_state", AsyncMock(return_value=state)):
        assert await handler._custom_select_committed_readback_confirms(element, "Yes") is True


@pytest.mark.asyncio
async def test_committed_readback_rejects_label_mismatch() -> None:
    element = MagicMock()
    state = {"label": "No", "role": "option", "selectedAttr": True}
    with patch.object(handler, "_read_custom_select_matched_state", AsyncMock(return_value=state)):
        assert await handler._custom_select_committed_readback_confirms(element, "Yes") is False


@pytest.mark.asyncio
async def test_committed_readback_rejects_unreadable_state() -> None:
    # A None read-back (unreadable / errored seam) is never success — no fail-open.
    element = MagicMock()
    with patch.object(handler, "_read_custom_select_matched_state", AsyncMock(return_value=None)):
        assert await handler._custom_select_committed_readback_confirms(element, "Yes") is False


@pytest.mark.asyncio
async def test_committed_readback_rejects_single_select_highlight() -> None:
    # aria-selected on a single-select option is a keyboard highlight, not a commit; not success.
    element = MagicMock()
    state = {"label": "Yes", "role": "option", "ariaSelected": True, "inMultiselectable": False}
    with patch.object(handler, "_read_custom_select_matched_state", AsyncMock(return_value=state)):
        assert await handler._custom_select_committed_readback_confirms(element, "Yes") is False


@pytest.mark.asyncio
async def test_committed_readback_rejects_blank_requested_value() -> None:
    # An empty expected label cannot be exact-matched; short-circuit before reading, never success.
    element = MagicMock()
    read = AsyncMock(return_value={"label": "Yes", "selectedAttr": True})
    with patch.object(handler, "_read_custom_select_matched_state", read):
        assert await handler._custom_select_committed_readback_confirms(element, "   ") is False
    read.assert_not_awaited()
