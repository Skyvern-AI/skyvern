"""Cached-script emission of ``ClickContext.desired_state`` (SKY-14051).

A recorded click that carries level-triggered toggle intent must keep it in the
generated script, so a cached replay can suppress a click that would toggle an
already-satisfied control back off.  A recording without that intent has to keep
producing byte-identical legacy output.
"""

from __future__ import annotations

from typing import Any

import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import _action_to_stmt
from skyvern.webeye.actions.actions import ActionType

_SEMANTIC_ELEMENT_DATA = {
    "tagName": "input",
    "attributes": {"aria-label": "Opt in to marketing email", "type": "checkbox"},
}

# Exact bytes this emitter produced before it learned the desired_state keyword, captured from the
# merge base. Every already-cached recording shape has to keep regenerating to these bytes.
_LEGACY_EMISSIONS: dict[tuple[str, bool], str] = {
    (
        "absent",
        False,
    ): "await page.click(\n    selector = 'xpath=//input[@id=\"opt-in\"]', \n    ai = 'proactive', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "absent",
        True,
    ): "await page.click(\n    selector = 'input[aria-label=\"Opt in to marketing email\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "None",
        False,
    ): "await page.click(\n    selector = 'xpath=//input[@id=\"opt-in\"]', \n    ai = 'proactive', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "None",
        True,
    ): "await page.click(\n    selector = 'input[aria-label=\"Opt in to marketing email\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{}",
        False,
    ): "await page.click(\n    selector = 'xpath=//input[@id=\"opt-in\"]', \n    ai = 'proactive', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{}",
        True,
    ): "await page.click(\n    selector = 'input[aria-label=\"Opt in to marketing email\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{'single_option_click': True}",
        False,
    ): "await page.click(\n    selector = 'xpath=//input[@id=\"opt-in\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{'single_option_click': True}",
        True,
    ): "await page.click(\n    selector = 'input[aria-label=\"Opt in to marketing email\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{'single_option_click': False}",
        False,
    ): "await page.click(\n    selector = 'xpath=//input[@id=\"opt-in\"]', \n    ai = 'proactive', \n    prompt = 'opt in to marketing email',\n)\n",
    (
        "{'single_option_click': False}",
        True,
    ): "await page.click(\n    selector = 'input[aria-label=\"Opt in to marketing email\"]', \n    ai = 'fallback', \n    prompt = 'opt in to marketing email',\n)\n",
}


def _click_action(click_context: Any = ..., **overrides: Any) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_type": ActionType.CLICK,
        "xpath": '//input[@id="opt-in"]',
        "intention": "opt in to marketing email",
        "skyvern_element_data": _SEMANTIC_ELEMENT_DATA,
    }
    if click_context is not ...:
        action["click_context"] = click_context
    action.update(overrides)
    return action


def _emit(action: dict[str, Any], *, use_semantic_selectors: bool = False) -> str:
    stmt = _action_to_stmt(action, {}, use_semantic_selectors=use_semantic_selectors)
    return cst.Module(body=[stmt]).code


@pytest.mark.parametrize("use_semantic_selectors", [False, True])
@pytest.mark.parametrize("desired_state", [True, False])
def test_recorded_desired_state_is_emitted_as_a_keyword(desired_state: bool, use_semantic_selectors: bool) -> None:
    action = _click_action({"single_option_click": False, "desired_state": desired_state})

    code = _emit(action, use_semantic_selectors=use_semantic_selectors)

    assert f"desired_state = {desired_state}" in code


def test_desired_state_is_emitted_after_the_ai_mode() -> None:
    action = _click_action({"desired_state": True})

    code = _emit(action)

    assert code.index("ai = ") < code.index("desired_state = ") < code.index("prompt = ")


@pytest.mark.parametrize(
    "click_context",
    [
        pytest.param(..., id="no-click-context"),
        pytest.param(None, id="null-click-context"),
        pytest.param({}, id="empty-click-context"),
        pytest.param({"single_option_click": True}, id="single-option-click-on"),
        pytest.param({"single_option_click": False}, id="single-option-click-off"),
    ],
)
@pytest.mark.parametrize("use_semantic_selectors", [False, True])
def test_recording_without_desired_state_keeps_legacy_output_byte_identical(
    click_context: Any, use_semantic_selectors: bool
) -> None:
    key = "absent" if click_context is ... else repr(click_context)

    code = _emit(_click_action(click_context), use_semantic_selectors=use_semantic_selectors)

    assert "desired_state" not in code
    assert code == _LEGACY_EMISSIONS[(key, use_semantic_selectors)]


@pytest.mark.parametrize("use_semantic_selectors", [False, True])
@pytest.mark.parametrize("single_option_click", [True, False])
def test_explicit_null_desired_state_matches_the_same_recording_without_the_key(
    single_option_click: bool, use_semantic_selectors: bool
) -> None:
    # An agent recording that ran the guard but found no level-triggered intent stores
    # desired_state=None; it must not diverge by a byte from a recording made before the field existed.
    code = _emit(
        _click_action({"single_option_click": single_option_click, "desired_state": None}),
        use_semantic_selectors=use_semantic_selectors,
    )

    assert code == _LEGACY_EMISSIONS[(repr({"single_option_click": single_option_click}), use_semantic_selectors)]


@pytest.mark.parametrize("click_context", ["true", 1, ["desired_state"]])
def test_non_dict_click_context_emits_no_desired_state(click_context: Any) -> None:
    code = _emit(_click_action(click_context))

    assert "desired_state" not in code


@pytest.mark.parametrize("desired_state", ["true", 1, 0, {}])
def test_non_boolean_desired_state_is_not_emitted(desired_state: Any) -> None:
    # Only a real boolean is a level-triggered intent; anything else would emit an argument the
    # runtime would have to re-interpret, so it falls back to the legacy edge-triggered click.
    code = _emit(_click_action({"desired_state": desired_state}))

    assert "desired_state" not in code


@pytest.mark.parametrize("desired_state", [True, False])
def test_emitted_desired_state_is_a_python_boolean_literal(desired_state: bool) -> None:
    code = _emit(_click_action({"desired_state": desired_state}))

    call = cst.parse_expression(code.strip().removeprefix("await "))
    assert isinstance(call, cst.Call)
    keyword = next(arg for arg in call.args if arg.keyword is not None and arg.keyword.value == "desired_state")
    assert isinstance(keyword.value, cst.Name)
    assert keyword.value.value == str(desired_state)
