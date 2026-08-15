"""Deterministic ``ClickContext.desired_state`` execution inside
``handle_click_action`` (SKY-13916).

The level-triggered toggle guard lives in the shared OSS click handler: it reads
one live selected-state observable after the element is resolved and before the
physical click, then suppresses the click when the control already holds the
desired state, drives a native checkbox/radio to the desired state, or falls
through to a single ordinary click for a custom-control mismatch or an unreadable
state. Role never implies intent — it only chooses which observable to read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction, ClickContext
from skyvern.webeye.actions.responses import ActionAbort, ActionSuccess


class FakeLabelLocator:
    def __init__(
        self,
        checkbox_locator: FakeCheckboxLocator,
        visible: bool = True,
        exists: bool = True,
        present_descendants: list[str] | None = None,
        descendant_inspection_error: Exception | None = None,
    ) -> None:
        self.checkbox_locator = checkbox_locator
        self.visible = visible
        self.exists = exists
        self.present_descendants = present_descendants or []
        self.descendant_inspection_error = descendant_inspection_error
        self.click_count = 0

    @property
    def first(self) -> FakeLabelLocator:
        return self

    async def count(self) -> int:
        return int(self.exists)

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self, timeout: int | None = None, position: dict[str, int] | None = None) -> None:
        self.click_count += 1
        self.checkbox_locator.checked = not self.checkbox_locator.checked

    def locator(self, selector: str) -> FakeInteractiveDescendantLocator:
        return FakeInteractiveDescendantLocator(
            selector=selector,
            present_descendants=self.present_descendants,
            inspection_error=self.descendant_inspection_error,
        )


class FakeInteractiveDescendantLocator:
    def __init__(self, selector: str, present_descendants: list[str], inspection_error: Exception | None) -> None:
        self.selector = selector
        self.present_descendants = present_descendants
        self.inspection_error = inspection_error

    async def count(self) -> int:
        if self.inspection_error is not None:
            raise self.inspection_error
        queried = {part.strip() for part in self.selector.split(",")}
        return sum(1 for descendant in self.present_descendants if descendant in queried)


class FakeCheckboxLocator:
    def __init__(self, checked: bool) -> None:
        self.checked = checked
        self.label_locator = FakeLabelLocator(self)

    async def is_checked(self, timeout: int | None = None) -> bool:
        return self.checked

    def locator(self, selector: str) -> FakeLabelLocator:
        assert selector == "xpath=ancestor::label[1]"
        return self.label_locator


class FakeCheckboxElement:
    """Minimal SkyvernElement stand-in for driving ``_set_native_checkbox_state``."""

    def __init__(self, checked: bool, input_toggle_fails: bool = False) -> None:
        self.locator = FakeCheckboxLocator(checked)
        self.explicit_label_locator = FakeLabelLocator(self.locator)
        self.input_toggle_fails = input_toggle_fails
        self.check_count = 0
        self.uncheck_count = 0

    def get_locator(self) -> FakeCheckboxLocator:
        return self.locator

    def get_id(self) -> str:
        return "checkbox-id"

    async def get_attr(self, attr_name: str, mode: str | None = None) -> str | None:
        assert attr_name == "id"
        return "checkbox-id"

    def get_frame(self) -> FakeFrame:
        return FakeFrame(self.explicit_label_locator)

    async def check(self) -> None:
        self.check_count += 1
        if self.input_toggle_fails:
            raise RuntimeError("input is not visible")
        self.locator.checked = True

    async def uncheck(self) -> None:
        self.uncheck_count += 1
        if self.input_toggle_fails:
            raise RuntimeError("input is not visible")
        self.locator.checked = False


class FakeFrame:
    def __init__(self, explicit_label_locator: FakeLabelLocator) -> None:
        self.explicit_label_locator = explicit_label_locator

    def locator(self, selector: str) -> FakeLabelLocator:
        assert selector == 'label[for="checkbox-id"]'
        return self.explicit_label_locator


class FakeAncestorLocator:
    """Resolves the nearest ancestor-or-self ``[aria-multiselectable]`` for
    ``_is_single_select_option_highlight``: ``value`` is that attribute's value, or ``None`` when no
    such ancestor exists (count 0)."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    async def count(self) -> int:
        return 0 if self._value is None else 1

    async def get_attribute(self, name: str) -> str | None:
        assert name == "aria-multiselectable"
        return self._value


class FakeElementLocator:
    def __init__(self, multiselectable_ancestor: str | None) -> None:
        self._multiselectable_ancestor = multiselectable_ancestor

    def locator(self, selector: str) -> FakeAncestorLocator:
        assert selector == "xpath=ancestor-or-self::*[@aria-multiselectable][1]"
        return FakeAncestorLocator(self._multiselectable_ancestor)


class FakeToggleElement:
    """Attribute reads are served from a static dict; ``checked`` is the live value
    returned by ``is_checked()`` for native inputs (``None`` models an unreadable
    input); ``attr_error`` simulates a detached element when a live ARIA attribute
    is queried; ``multiselectable_ancestor`` is the value of the nearest ancestor-or-self
    ``[aria-multiselectable]`` (``None`` models no such ancestor)."""

    def __init__(
        self,
        tag_name: str,
        attributes: dict[str, str] | None = None,
        *,
        checked: bool | None = None,
        attr_error: Exception | None = None,
        multiselectable_ancestor: str | None = None,
    ) -> None:
        self._tag_name = tag_name
        self._attributes = attributes or {}
        self._checked = checked
        self._attr_error = attr_error
        self._locator = FakeElementLocator(multiselectable_ancestor)

    def get_tag_name(self) -> str:
        return self._tag_name

    def get_id(self) -> str:
        return "el"

    def get_locator(self) -> FakeElementLocator:
        return self._locator

    async def get_attr(self, attr_name: str, mode: str = "auto", timeout: float | None = None) -> str | None:
        if self._attr_error is not None and attr_name in ("aria-selected", "aria-checked", "aria-pressed"):
            raise self._attr_error
        return self._attributes.get(attr_name)

    async def is_checked(self, timeout: float | None = None) -> bool | None:
        return self._checked


def _click_action() -> ClickAction:
    return ClickAction(element_id="el", reasoning="toggle")


# ---------------------------------------------------------------------------
# _resolve_live_selected_state — one generic, live observable of selectedness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("input_type", ["checkbox", "radio"])
@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.asyncio
async def test_resolve_reads_native_input_is_checked(input_type: str, checked: bool) -> None:
    element = FakeToggleElement("input", {"type": input_type}, checked=checked)
    assert await handler._resolve_live_selected_state(element) is checked


@pytest.mark.asyncio
async def test_resolve_non_toggle_input_returns_none() -> None:
    element = FakeToggleElement("input", {"type": "text"}, checked=True)
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.parametrize("aria_attr", ["aria-checked", "aria-pressed", "aria-selected"])
@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
@pytest.mark.asyncio
async def test_resolve_reads_exact_aria_boolean(aria_attr: str, value: str, expected: bool) -> None:
    # A multiselectable container keeps a bare aria-selected readable as committed state; without it
    # a single-select option's aria-selected="true" is treated as an unreadable highlight (below).
    element = FakeToggleElement("div", {"role": "option", aria_attr: value}, multiselectable_ancestor="true")
    assert await handler._resolve_live_selected_state(element) is expected


@pytest.mark.asyncio
async def test_resolve_single_select_option_aria_selected_true_is_unreadable() -> None:
    # A single-select option's aria-selected="true" is the pre-commit keyboard highlight, not
    # committed state, so it must read as unreadable (None) and let the click commit the selection.
    element = FakeToggleElement("div", {"role": "option", "aria-selected": "true"})
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.asyncio
async def test_resolve_multiselect_option_aria_selected_true_reads_true() -> None:
    element = FakeToggleElement("div", {"role": "option", "aria-selected": "true"}, multiselectable_ancestor="true")
    assert await handler._resolve_live_selected_state(element) is True


@pytest.mark.asyncio
async def test_resolve_tab_aria_selected_true_reads_true() -> None:
    # aria-selected on a tab is committed state; only role=option treats it as a highlight.
    element = FakeToggleElement("div", {"role": "tab", "aria-selected": "true"})
    assert await handler._resolve_live_selected_state(element) is True


@pytest.mark.asyncio
async def test_resolve_prefers_aria_checked_over_pressed_and_selected() -> None:
    element = FakeToggleElement("div", {"aria-checked": "false", "aria-pressed": "true", "aria-selected": "true"})
    assert await handler._resolve_live_selected_state(element) is False


@pytest.mark.parametrize("aria_value", ["yes", "TRUE?", "mixed", ""])
@pytest.mark.asyncio
async def test_resolve_malformed_aria_returns_none(aria_value: str) -> None:
    element = FakeToggleElement("div", {"role": "option", "aria-selected": aria_value})
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.asyncio
async def test_resolve_read_exception_returns_none() -> None:
    element = FakeToggleElement("div", {"role": "option"}, attr_error=RuntimeError("detached"))
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.parametrize("attributes", [{}, {"role": "button"}, {"href": "https://example.test/next"}])
@pytest.mark.asyncio
async def test_resolve_control_without_boolean_observable_returns_none(attributes: dict[str, str]) -> None:
    element = FakeToggleElement("button", attributes)
    assert await handler._resolve_live_selected_state(element) is None


# ---------------------------------------------------------------------------
# _apply_desired_click_state — [ActionAbort()] suppresses; None falls through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aria_attr", ["aria-selected", "aria-checked", "aria-pressed"])
@pytest.mark.asyncio
async def test_apply_custom_control_matching_true_suppresses(aria_attr: str) -> None:
    # Multiselectable so the aria-selected param is committed state, not a single-select highlight.
    element = FakeToggleElement("div", {"role": "option", aria_attr: "true"}, multiselectable_ancestor="true")
    result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_custom_control_matching_false_suppresses() -> None:
    element = FakeToggleElement("div", {"role": "switch", "aria-checked": "false"})
    result = await handler._apply_desired_click_state(_click_action(), element, False, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_custom_control_mismatch_true_falls_through_one_click() -> None:
    element = FakeToggleElement("div", {"role": "option", "aria-selected": "false"})
    assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_single_select_option_aria_selected_false_desired_false_suppresses() -> None:
    # The highlight ambiguity only poisons aria-selected="true"; a single-select option's
    # aria-selected="false" stays readable, so desired_state=False suppresses (clicking it would
    # select the option) instead of falling open to a click.
    element = FakeToggleElement("div", {"role": "option", "aria-selected": "false"})
    result = await handler._apply_desired_click_state(_click_action(), element, False, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_custom_control_mismatch_false_falls_through_without_checking() -> None:
    element = FakeToggleElement("button", {"aria-pressed": "true"})
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        assert await handler._apply_desired_click_state(_click_action(), element, False, MagicMock()) is None
    set_state.assert_not_awaited()


@pytest.mark.parametrize("aria_value", ["yes", "TRUE?", "mixed", ""])
@pytest.mark.asyncio
async def test_apply_malformed_custom_state_falls_open(aria_value: str) -> None:
    element = FakeToggleElement("div", {"role": "option", "aria-selected": aria_value})
    assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_unreadable_custom_state_falls_open() -> None:
    element = FakeToggleElement("div", {"role": "option"}, attr_error=RuntimeError("detached"))
    assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None


@pytest.mark.parametrize(
    "tag_name,attributes",
    [("button", {}), ("button", {"role": "button"}), ("a", {"href": "https://example.test/next"})],
)
@pytest.mark.asyncio
async def test_apply_desired_state_on_non_selectable_control_falls_open(
    tag_name: str, attributes: dict[str, str]
) -> None:
    element = FakeToggleElement(tag_name, attributes)
    assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None


@pytest.mark.parametrize("input_type", ["checkbox", "radio"])
@pytest.mark.asyncio
async def test_apply_native_input_desired_true_when_unchecked_sets_and_suppresses(input_type: str) -> None:
    element = FakeToggleElement("input", {"type": input_type}, checked=False)
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)
    set_state.assert_awaited_once()
    assert set_state.await_args.kwargs["should_check"] is True


@pytest.mark.asyncio
async def test_apply_native_checkbox_desired_false_when_checked_unsets_and_suppresses() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=True)
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        result = await handler._apply_desired_click_state(_click_action(), element, False, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)
    set_state.assert_awaited_once()
    assert set_state.await_args.kwargs["should_check"] is False


@pytest.mark.asyncio
async def test_apply_native_radio_desired_false_when_checked_falls_through_without_setter() -> None:
    # A radio can't be turned off by clicking it (only selecting another radio clears it), so an
    # explicit desired_state=False on a checked radio must skip the doomed uncheck() and fall
    # through to a single ordinary click rather than a wasted state-setter attempt.
    element = FakeToggleElement("input", {"type": "radio"}, checked=True)
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        assert await handler._apply_desired_click_state(_click_action(), element, False, MagicMock()) is None
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_native_checkbox_matches_live_suppresses_without_setter() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=True)
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_native_checkbox_unreadable_live_state_falls_open() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=None)
    set_state = AsyncMock(return_value=True)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_native_checkbox_setter_failure_falls_through() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=False)
    set_state = AsyncMock(return_value=False)
    with patch.object(handler, "_set_native_checkbox_state", set_state):
        assert await handler._apply_desired_click_state(_click_action(), element, True, MagicMock()) is None
    set_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# _set_native_checkbox_state — native input set with associated-label fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_set_uses_input_when_actionable() -> None:
    element = FakeCheckboxElement(checked=False)
    assert await handler._set_native_checkbox_state(element, should_check=True)
    assert element.locator.checked is True
    assert element.check_count == 1
    assert element.locator.label_locator.click_count == 0


@pytest.mark.asyncio
async def test_native_set_falls_back_to_visible_label_for_hidden_input() -> None:
    element = FakeCheckboxElement(checked=False, input_toggle_fails=True)
    assert await handler._set_native_checkbox_state(element, should_check=True)
    assert element.locator.checked is True
    assert element.locator.label_locator.click_count == 1


@pytest.mark.asyncio
async def test_native_set_skips_when_already_matching() -> None:
    element = FakeCheckboxElement(checked=True)
    assert await handler._set_native_checkbox_state(element, should_check=True)
    assert element.check_count == 0
    assert element.uncheck_count == 0
    assert element.locator.label_locator.click_count == 0


# ---------------------------------------------------------------------------
# handle_click_action — the guard runs after element resolution, before the click
# ---------------------------------------------------------------------------


async def _run_handle_click(*, click_context: ClickContext | None, element: MagicMock) -> tuple[list, AsyncMock]:
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    page = MagicMock(url="https://example.test/page")
    page.evaluate = AsyncMock(return_value=False)
    chain = AsyncMock(return_value=[ActionSuccess()])
    incremental = MagicMock()
    incremental.start_listen_dom_increment = AsyncMock()
    incremental.stop_listen_dom_increment = AsyncMock()

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler, "get_or_create_wait_config", new=AsyncMock(return_value=None)),
        patch.object(handler.asyncio, "sleep", new=AsyncMock()),
        patch.object(handler, "chain_click", new=chain),
        patch.object(handler, "resolve_engine_selection_for_task", MagicMock(return_value=None)),
        patch.object(handler.SkyvernFrame, "create_instance", new=AsyncMock(return_value=MagicMock())),
        patch.object(handler, "IncrementalScrapePage", return_value=incremental),
        patch.object(handler, "handle_sequential_click_for_dropdown", new=AsyncMock(return_value=None)),
    ):
        action = ClickAction(element_id="control", click_context=click_context)
        results = await handler.handle_click_action(action, page, MagicMock(), MagicMock(), MagicMock())
    return results, chain


def _fall_through_element(tag_name: str, attr_value: str | None) -> MagicMock:
    element = MagicMock()
    element.get_id.return_value = "control"
    element.get_tag_name.return_value = tag_name
    element.get_attr = AsyncMock(return_value=attr_value)
    element.is_disabled = AsyncMock(return_value=False)
    element.scroll_into_view = AsyncMock()
    element.get_element_handler = AsyncMock(return_value=MagicMock())
    element.has_attr = AsyncMock(return_value=False)
    element.get_frame.return_value = MagicMock()
    return element


def _single_select_option_element() -> MagicMock:
    """A role=option reporting aria-selected="true" with no multiselectable ancestor — the guard
    must read this as an unreadable highlight and fall open to the physical click."""
    attrs = {"role": "option", "aria-selected": "true"}
    element = MagicMock()
    element.get_id.return_value = "control"
    element.get_tag_name.return_value = "div"
    element.get_attr = AsyncMock(side_effect=lambda name, *args, **kwargs: attrs.get(name))
    element.get_locator.return_value = FakeElementLocator(None)
    element.is_disabled = AsyncMock(return_value=False)
    element.scroll_into_view = AsyncMock()
    element.get_element_handler = AsyncMock(return_value=MagicMock())
    element.has_attr = AsyncMock(return_value=False)
    element.get_frame.return_value = MagicMock()
    return element


@pytest.mark.asyncio
async def test_handle_click_suppresses_already_satisfied_state() -> None:
    element = _fall_through_element("div", "true")
    results, chain = await _run_handle_click(click_context=ClickContext(desired_state=True), element=element)

    assert len(results) == 1 and isinstance(results[0], ActionAbort)
    chain.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_click_without_desired_state_runs_physical_click() -> None:
    element = _fall_through_element("button", None)
    results, chain = await _run_handle_click(click_context=None, element=element)

    chain.assert_awaited_once()
    assert results and isinstance(results[-1], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_click_custom_mismatch_falls_through_to_physical_click() -> None:
    element = _fall_through_element("div", "false")
    _results, chain = await _run_handle_click(click_context=ClickContext(desired_state=True), element=element)

    chain.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_single_select_option_highlight_falls_open_one_click() -> None:
    # A single-select option's aria-selected="true" is only a highlight, so through the full handler
    # the guard falls open and the physical click still commits the selection exactly once.
    element = _single_select_option_element()
    _results, chain = await _run_handle_click(click_context=ClickContext(desired_state=True), element=element)

    chain.assert_awaited_once()


# ---------------------------------------------------------------------------
# <label> targets — observe the spec-bound control (for= / wrapped input), not the
# label. Equal suppresses; mismatch / unresolved / unreadable falls through to one
# ordinary label click (whose forwarding performs the single toggle). Never drives
# the control's state through the label.
# ---------------------------------------------------------------------------


class FakeLabelElement:
    def __init__(
        self,
        *,
        label_for: FakeToggleElement | None = None,
        has_for_attr: bool = False,
        wrapped_state: bool | None = None,
        for_control_state: bool | None = None,
        wrapped_error: Exception | None = None,
    ) -> None:
        self._label_for = label_for
        self.has_for_attr = has_for_attr
        self.wrapped_state = wrapped_state
        self.for_control_state = for_control_state
        self.wrapped_error = wrapped_error

    def get_tag_name(self) -> str:
        return "label"

    def get_id(self) -> str:
        return "label-el"

    async def find_label_for(self, dom: object) -> FakeToggleElement | None:
        return self._label_for


async def _fake_evaluate_element_scoped(element: FakeLabelElement, expression: str, arg: object = None) -> bool | None:
    """Stand-in for the in-page label-control read (``_evaluate_element_scoped`` + JS). Models the JS
    outcome from the label's spec so assertions target behavior, not JS text: a configured error
    models a probe failure; an explicit ``for=`` label returns ``el.control``'s checked state
    (``for_control_state`` -- a bool for a resolved hidden toggle, None for a dangling / non-labelable
    / non-toggle control, with no descendant fallback); an implicit label returns ``wrapped_state``
    (the resolved single labelable descendant, None for zero / multiple / non-toggle). Routed through
    the real ``_read_label_control_state``, so its exception -> None and non-bool -> None handling is
    exercised too."""
    if element.wrapped_error is not None:
        raise element.wrapped_error
    if element.has_for_attr:
        return element.for_control_state
    return element.wrapped_state


@pytest.mark.asyncio
async def test_apply_label_target_reads_bound_control_state_and_suppresses() -> None:
    control = FakeToggleElement("input", {"type": "checkbox"}, checked=True)
    label = FakeLabelElement(label_for=control)
    result = await handler._apply_desired_click_state(_click_action(), label, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_label_target_mismatch_falls_through_one_click() -> None:
    control = FakeToggleElement("input", {"type": "checkbox"}, checked=False)
    label = FakeLabelElement(label_for=control)
    assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_bound_control_unreadable_state_falls_open() -> None:
    control = FakeToggleElement("input", {"type": "checkbox"}, checked=None)
    label = FakeLabelElement(label_for=control)
    assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_wrapped_control_live_state_matches_suppresses() -> None:
    # No for= and no scraped child resolves, so a deep-nested or display:none wrapped checkbox is
    # read live in-page. Already checked + desired_state=True -> suppress the redundant click that
    # would otherwise uncheck it (the double-toggle this guard exists to prevent).
    label = FakeLabelElement(wrapped_state=True)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        result = await handler._apply_desired_click_state(_click_action(), label, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_label_wrapped_control_live_state_mismatch_falls_through_one_click() -> None:
    # Live state differs from the desired state: fall through to one ordinary label click (whose
    # forwarding performs the single toggle) and never drive the control's state through the label.
    label = FakeLabelElement(wrapped_state=False)
    set_state = AsyncMock(return_value=True)
    with (
        patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped),
        patch.object(handler, "_set_native_checkbox_state", set_state),
    ):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_label_wrapped_control_unresolvable_falls_open() -> None:
    # evaluate -> None models zero / multiple / non-checkbox-radio labelable descendants (or a
    # browser/candidate disagreement): the state is unreadable, so fall open to an ordinary click.
    label = FakeLabelElement(wrapped_state=None)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_wrapped_control_probe_failure_falls_open() -> None:
    label = FakeLabelElement(wrapped_error=RuntimeError("detached"))
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_with_for_attr_never_reads_descendants() -> None:
    # Per the HTML labeled-control algorithm a for= label never falls back to descendants. When
    # el.control does not resolve to a native toggle (for_control_state=None), even a resolvable
    # checked wrapped descendant (wrapped_state=True) must not suppress: the JS gate -- mirrored by
    # the fake, which never consults wrapped_state once has_for_attr is set -- skips the descendant
    # scan for for= labels, so fall open to a single ordinary click.
    label = FakeLabelElement(has_for_attr=True, for_control_state=None, wrapped_state=True)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_wrapped_input_child_resolves_and_suppresses() -> None:
    # Migrated off the removed static-tree child stub: the wrapped control's state is now read
    # in-page. Already unchecked + desired_state=False -> suppress (never toggle an already-satisfied
    # control on), exercising the guard's other suppression direction.
    label = FakeLabelElement(wrapped_state=False)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        result = await handler._apply_desired_click_state(_click_action(), label, False, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_label_explicit_for_hidden_control_matches_suppresses() -> None:
    # <label for="x"> pointing at a display:none checked checkbox: the hidden input is never scraped
    # (no unique_id), so find_label_for cannot map it and the control's checked state is read in-page
    # via el.control. Already checked + desired_state=True -> suppress the click that HTML label
    # activation would otherwise forward to the hidden input, unchecking it.
    label = FakeLabelElement(has_for_attr=True, label_for=None, for_control_state=True)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        result = await handler._apply_desired_click_state(_click_action(), label, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@pytest.mark.asyncio
async def test_apply_label_explicit_for_hidden_control_mismatch_falls_through_one_click() -> None:
    # Explicit hidden control reads the opposite of the desired state: fall through to one ordinary
    # label click (whose forwarding performs the single toggle) and never drive state through the label.
    label = FakeLabelElement(has_for_attr=True, label_for=None, for_control_state=False)
    set_state = AsyncMock(return_value=True)
    with (
        patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped),
        patch.object(handler, "_set_native_checkbox_state", set_state),
    ):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_label_explicit_for_unresolvable_falls_open() -> None:
    # el.control resolves to nothing usable (dangling id / non-labelable / non-toggle) -> None ->
    # fall open to an ordinary click.
    label = FakeLabelElement(has_for_attr=True, label_for=None, for_control_state=None)
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        assert await handler._apply_desired_click_state(_click_action(), label, True, MagicMock()) is None


@pytest.mark.asyncio
async def test_apply_label_explicit_mapped_control_takes_precedence() -> None:
    # find_label_for stays first: when the explicit control is scraped and mapped, its state is read
    # through the mapped SkyvernElement (ARIA-capable) and the in-page el.control read is never
    # consulted. Mapped control checked + desired True -> suppress; for_control_state=False (which
    # would mismatch) plus a probe that raises if consulted both prove the mapped read wins.
    control = FakeToggleElement("input", {"type": "checkbox"}, checked=True)
    label = FakeLabelElement(
        label_for=control,
        has_for_attr=True,
        for_control_state=False,
        wrapped_error=RuntimeError("in-page read must not run when the control is mapped"),
    )
    with patch.object(handler, "_evaluate_element_scoped", _fake_evaluate_element_scoped):
        result = await handler._apply_desired_click_state(_click_action(), label, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


# ---------------------------------------------------------------------------
# Disabled-wrapper retarget — the pre-retarget guard falls open on an unreadable
# wrapper, the click is retargeted to its enabled single-chain descendant, and the
# same guard is reapplied to that child so an already-satisfied control is not
# re-toggled. A child that the guard can't suppress still gets one ordinary click.
# ---------------------------------------------------------------------------


async def _run_handle_click_retarget(
    *, wrapper: MagicMock, child: MagicMock, desired_state: bool
) -> tuple[list, AsyncMock]:
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=wrapper)
    dom.safe_get_skyvern_element_by_id = AsyncMock(return_value=child)
    page = MagicMock(url="https://example.test/page")
    page.evaluate = AsyncMock(return_value=False)
    chain = AsyncMock(return_value=[ActionSuccess()])
    incremental = MagicMock()
    incremental.start_listen_dom_increment = AsyncMock()
    incremental.stop_listen_dom_increment = AsyncMock()

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler, "get_or_create_wait_config", new=AsyncMock(return_value=None)),
        patch.object(handler.asyncio, "sleep", new=AsyncMock()),
        patch.object(handler, "chain_click", new=chain),
        patch.object(handler, "resolve_engine_selection_for_task", MagicMock(return_value=None)),
        patch.object(handler.SkyvernFrame, "create_instance", new=AsyncMock(return_value=MagicMock())),
        patch.object(handler, "IncrementalScrapePage", return_value=incremental),
        patch.object(handler, "handle_sequential_click_for_dropdown", new=AsyncMock(return_value=None)),
    ):
        action = ClickAction(element_id="wrapper", click_context=ClickContext(desired_state=desired_state))
        results = await handler.handle_click_action(action, page, MagicMock(), MagicMock(), MagicMock())
    return results, chain


def _disabled_wrapper() -> MagicMock:
    wrapper = MagicMock()
    wrapper.get_id.return_value = "wrapper"
    wrapper.get_tag_name.return_value = "div"
    wrapper.get_attr = AsyncMock(return_value=None)
    wrapper.is_disabled = AsyncMock(return_value=True)
    wrapper.find_deepest_interactable_descendant_in_single_chain = MagicMock(return_value="child")
    return wrapper


def _retarget_child(*, tag_name: str, attr_value: str | None, checked: bool | None) -> MagicMock:
    child = MagicMock()
    child.get_id.return_value = "child"
    child.get_tag_name.return_value = tag_name
    child.get_attr = AsyncMock(return_value=attr_value)
    child.is_checked = AsyncMock(return_value=checked)
    child.is_disabled = AsyncMock(return_value=False)
    child.scroll_into_view = AsyncMock()
    child.get_element_handler = AsyncMock(return_value=MagicMock())
    child.has_attr = AsyncMock(return_value=False)
    child.get_frame.return_value = MagicMock()
    return child


@pytest.mark.asyncio
async def test_handle_click_reapplies_guard_on_retargeted_child() -> None:
    wrapper = _disabled_wrapper()
    child = _retarget_child(tag_name="input", attr_value="checkbox", checked=True)
    results, chain = await _run_handle_click_retarget(wrapper=wrapper, child=child, desired_state=True)

    assert len(results) == 1 and isinstance(results[0], ActionAbort)
    chain.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_click_retargeted_child_mismatch_clicks_once() -> None:
    wrapper = _disabled_wrapper()
    child = _retarget_child(tag_name="div", attr_value="false", checked=None)
    _results, chain = await _run_handle_click_retarget(wrapper=wrapper, child=child, desired_state=True)

    chain.assert_awaited_once()
