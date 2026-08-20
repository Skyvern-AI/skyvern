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

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Page, async_playwright

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction, ClickContext
from skyvern.webeye.actions.responses import ActionAbort, ActionResult, ActionSuccess
from skyvern.webeye.utils.dom import SkyvernElement


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


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


_GRID_SNAPSHOT_DEFAULT = object()


def _non_grid_row_snapshot() -> dict[str, object]:
    """A well-formed ``_GRID_ROW_SNAPSHOT_JS`` result for a checkbox outside any grid row-selection
    chain, so ``_classify_grid_row_selection`` returns NOT_GRID_ROW and the resolver reads native
    state. Lets an ordinary checkbox fake exercise the new grid probe without being a grid row."""
    return {
        "inHeader": False,
        "hasGrid": False,
        "hasRow": False,
        "hasCell": False,
        "sameGridChain": False,
        "isCheckbox": True,
        "candidateExactGridcell": False,
        "candidateDirectRowChild": False,
        "gridCellCount": 0,
        "rowAriaSelected": None,
        "rowClasses": None,
    }


class FakeElementLocator:
    def __init__(
        self,
        multiselectable_ancestor: str | None,
        grid_snapshot: object = _GRID_SNAPSHOT_DEFAULT,
        grid_snapshot_error: Exception | None = None,
    ) -> None:
        self._multiselectable_ancestor = multiselectable_ancestor
        self._grid_snapshot = grid_snapshot
        self._grid_snapshot_error = grid_snapshot_error

    def locator(self, selector: str) -> FakeAncestorLocator:
        assert selector == "xpath=ancestor-or-self::*[@aria-multiselectable][1]"
        return FakeAncestorLocator(self._multiselectable_ancestor)

    async def evaluate(self, expression: str, *args: object) -> object:
        if self._grid_snapshot_error is not None:
            raise self._grid_snapshot_error
        if self._grid_snapshot is _GRID_SNAPSHOT_DEFAULT:
            return _non_grid_row_snapshot()
        return self._grid_snapshot


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
        grid_snapshot: object = _GRID_SNAPSHOT_DEFAULT,
        grid_snapshot_error: Exception | None = None,
    ) -> None:
        self._tag_name = tag_name
        self._attributes = attributes or {}
        self._checked = checked
        self._attr_error = attr_error
        self._locator = FakeElementLocator(multiselectable_ancestor, grid_snapshot, grid_snapshot_error)

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
    # The checkbox guard now probes grid row-selection first; a plain retarget child is not a grid row.
    child.get_locator.return_value.evaluate = AsyncMock(return_value=_non_grid_row_snapshot())
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


# ---------------------------------------------------------------------------
# Grid row-selection — a selectable ARIA grid tracks row selection as app state
# on the closest row, independent of a selection checkbox's native ``checked``.
# The desired_state guard must read (and drive) the ROW's selection, or a
# checked-but-unselected row reads as already satisfied and the corrective click
# is suppressed. Detection is purely structural (a checkbox in a grid -> row ->
# cell chain); no framework class token is used.
# ---------------------------------------------------------------------------


def _grid_snapshot(**overrides: object) -> dict[str, object]:
    """A well-formed ``_GRID_ROW_SNAPSHOT_JS`` result for a checkbox that is the unique selection cell of
    a grid -> row -> cell chain whose row is unselected: exactly one direct role=gridcell cell, and it is
    the checkbox's closest cell. Override any field to model other DOM shapes."""
    snapshot: dict[str, object] = {
        "inHeader": False,
        "hasGrid": True,
        "hasRow": True,
        "hasCell": True,
        "sameGridChain": True,
        "isCheckbox": True,
        "candidateExactGridcell": True,
        "candidateDirectRowChild": True,
        "gridCellCount": 1,
        "rowAriaSelected": None,
        "rowClasses": [],
        "otherRowSelected": False,
    }
    snapshot.update(overrides)
    return snapshot


# --- _classify_grid_row_selection — pure classifier, no browser --------------


@pytest.mark.parametrize("snapshot", [None, "grid", 5, 3.2, [], ("a",)])
def test_classify_non_dict_is_unreadable(snapshot: object) -> None:
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNREADABLE


@pytest.mark.parametrize("missing_key", ["inHeader", "hasGrid", "hasRow", "hasCell", "sameGridChain", "isCheckbox"])
def test_classify_missing_bool_key_is_unreadable(missing_key: str) -> None:
    snapshot = _grid_snapshot()
    del snapshot[missing_key]
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNREADABLE


@pytest.mark.parametrize("bad_value", ["true", 1, None])
def test_classify_wrong_typed_bool_key_is_unreadable(bad_value: object) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(isCheckbox=bad_value)) is (
        handler._GridRowSelection.UNREADABLE
    )


@pytest.mark.parametrize("missing_key", ["rowClasses", "rowAriaSelected"])
def test_classify_missing_state_key_is_unreadable(missing_key: str) -> None:
    snapshot = _grid_snapshot()
    del snapshot[missing_key]
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNREADABLE


@pytest.mark.parametrize("bad_row_classes", ["is-selected", [1, 2], [None], 5])
def test_classify_bad_rowclasses_type_is_unreadable(bad_row_classes: object) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowClasses=bad_row_classes)) is (
        handler._GridRowSelection.UNREADABLE
    )


@pytest.mark.parametrize("bad_aria", [5, True, ["true"]])
def test_classify_bad_aria_type_is_unreadable(bad_aria: object) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowAriaSelected=bad_aria)) is (
        handler._GridRowSelection.UNREADABLE
    )


def test_classify_header_checkbox_is_not_grid_row() -> None:
    # A header select-all checkbox sits in the same grid but never represents a data row's selection.
    assert handler._classify_grid_row_selection(_grid_snapshot(inHeader=True)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


def test_classify_not_a_checkbox_is_not_grid_row() -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(isCheckbox=False)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


@pytest.mark.parametrize("missing", ["hasGrid", "hasRow", "hasCell"])
def test_classify_missing_structure_is_not_grid_row(missing: str) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(**{missing: False})) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


def test_classify_broken_grid_chain_is_not_grid_row() -> None:
    # Row and cell exist but not in the checkbox's own closest-grid chain (e.g. a nested grid).
    assert handler._classify_grid_row_selection(_grid_snapshot(sameGridChain=False)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


@pytest.mark.parametrize("grid_cell_count", [0, 2, 3, 5])
def test_classify_row_without_unique_selection_cell_is_not_grid_row(grid_cell_count: int) -> None:
    # An ordinary data-column checkbox lives in a standards-complete ARIA grid where several data cells
    # carry role=gridcell, so the row has no single selection cell. Only a row with EXACTLY one direct
    # role=gridcell cell (the incident's selection-cell signature) is a row-selection control; anything
    # else preserves native checkbox behavior.
    assert handler._classify_grid_row_selection(_grid_snapshot(gridCellCount=grid_cell_count)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


def test_classify_candidate_bare_td_not_exact_gridcell_is_not_grid_row() -> None:
    # The checkbox's closest cell is a bare <td> (no role=gridcell); an arbitrary td is not accepted as a
    # selection cell even when the row otherwise looks grid-shaped.
    assert handler._classify_grid_row_selection(_grid_snapshot(candidateExactGridcell=False)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


def test_classify_candidate_not_direct_row_child_is_not_grid_row() -> None:
    # The unique role=gridcell is not the checkbox's closest cell (e.g. a gridcell nested inside another
    # gridcell), so the checkbox does not own the selection cell.
    assert handler._classify_grid_row_selection(_grid_snapshot(candidateDirectRowChild=False)) is (
        handler._GridRowSelection.NOT_GRID_ROW
    )


def test_classify_unique_selection_cell_signature_reads_row_state() -> None:
    # The positive signature: exactly one direct role=gridcell cell and it is the checkbox's closest
    # cell -> the classifier proceeds to read the row's selection state.
    assert (
        handler._classify_grid_row_selection(
            _grid_snapshot(
                candidateExactGridcell=True, candidateDirectRowChild=True, gridCellCount=1, rowAriaSelected="true"
            )
        )
        is handler._GridRowSelection.SELECTED
    )


@pytest.mark.parametrize("missing_key", ["candidateExactGridcell", "candidateDirectRowChild"])
def test_classify_missing_signature_bool_key_is_unreadable(missing_key: str) -> None:
    snapshot = _grid_snapshot()
    del snapshot[missing_key]
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNREADABLE


@pytest.mark.parametrize("bad_value", ["true", 1, None])
def test_classify_wrong_typed_signature_bool_is_unreadable(bad_value: object) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(candidateExactGridcell=bad_value)) is (
        handler._GridRowSelection.UNREADABLE
    )


@pytest.mark.parametrize("bad_count", ["1", None, True, 1.5])
def test_classify_malformed_gridcellcount_is_unreadable(bad_count: object) -> None:
    # A missing/non-integer grid-cell count means the extractor did not return its promised shape; fall
    # open rather than guess whether the row has a unique selection cell.
    assert handler._classify_grid_row_selection(_grid_snapshot(gridCellCount=bad_count)) is (
        handler._GridRowSelection.UNREADABLE
    )


def test_classify_missing_gridcellcount_is_unreadable() -> None:
    snapshot = _grid_snapshot()
    del snapshot["gridCellCount"]
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNREADABLE


@pytest.mark.parametrize("aria_value", ["true", " TRUE ", "True"])
def test_classify_aria_selected_true_is_selected(aria_value: str) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowAriaSelected=aria_value)) is (
        handler._GridRowSelection.SELECTED
    )


@pytest.mark.parametrize("aria_value", ["false", " False ", "FALSE"])
def test_classify_aria_selected_false_is_unselected(aria_value: str) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowAriaSelected=aria_value)) is (
        handler._GridRowSelection.UNSELECTED
    )


@pytest.mark.parametrize("aria_value", ["yes", "", "1", "selected", "mixed"])
def test_classify_aria_selected_malformed_is_unreadable(aria_value: str) -> None:
    # A present but non-boolean aria-selected is ambiguous; fall open rather than guess a state.
    assert handler._classify_grid_row_selection(_grid_snapshot(rowAriaSelected=aria_value)) is (
        handler._GridRowSelection.UNREADABLE
    )


@pytest.mark.parametrize("token", ["selected", "is-selected", "k-selected", "K-Selected", " is-selected "])
def test_classify_selected_row_class_token_is_selected(token: str) -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowClasses=["data-row", token])) is (
        handler._GridRowSelection.SELECTED
    )


def test_classify_no_selected_class_is_unmarked() -> None:
    # No aria-selected and no known selected class token is absence, not a positive UNSELECTED: UNMARKED.
    assert handler._classify_grid_row_selection(_grid_snapshot(rowClasses=["data-row", "unread"])) is (
        handler._GridRowSelection.UNMARKED
    )


def test_classify_empty_and_none_row_classes_are_unmarked() -> None:
    assert handler._classify_grid_row_selection(_grid_snapshot(rowClasses=[])) is (handler._GridRowSelection.UNMARKED)
    assert handler._classify_grid_row_selection(_grid_snapshot(rowClasses=None)) is (handler._GridRowSelection.UNMARKED)


def test_classify_aria_selected_takes_precedence_over_class() -> None:
    # An explicit aria-selected="false" is authoritative even if a stale selected class lingers.
    snapshot = _grid_snapshot(rowAriaSelected="false", rowClasses=["is-selected"])
    assert handler._classify_grid_row_selection(snapshot) is handler._GridRowSelection.UNSELECTED


# --- _read_grid_row_selection — snapshot read + classify ---------------------


@pytest.mark.asyncio
async def test_read_grid_row_selection_classifies_snapshot() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot=_grid_snapshot(rowAriaSelected="true"))
    assert (await handler._read_grid_row_selection(element)).state is handler._GridRowSelection.SELECTED


@pytest.mark.asyncio
async def test_read_grid_row_selection_snapshot_error_is_unreadable() -> None:
    # A failed snapshot read must fall open (UNREADABLE), never quietly become NOT_GRID_ROW, or a
    # checked-but-unselected row could still read as satisfied through the native fallback.
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot_error=RuntimeError("detached"))
    read = await handler._read_grid_row_selection(element)
    assert read.state is handler._GridRowSelection.UNREADABLE
    assert read.other_row_selected is False


# --- _resolve_live_selected_state — grid checkbox reads the ROW state --------


@pytest.mark.asyncio
async def test_resolve_grid_row_selected_reads_true_over_native() -> None:
    element = FakeToggleElement(
        "input", {"type": "checkbox"}, checked=False, grid_snapshot=_grid_snapshot(rowAriaSelected="true")
    )
    assert await handler._resolve_live_selected_state(element) is True


@pytest.mark.asyncio
async def test_resolve_grid_row_unmarked_returns_none_despite_native_checked() -> None:
    # The incident shape (native checked=True, no positive selected signal) is UNMARKED, not a positive
    # UNSELECTED: the shared resolver returns None so it never reports the row as unselected from absence.
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=True, grid_snapshot=_grid_snapshot())
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.asyncio
async def test_resolve_grid_row_unreadable_returns_none() -> None:
    element = FakeToggleElement(
        "input", {"type": "checkbox"}, checked=True, grid_snapshot=_grid_snapshot(rowAriaSelected="maybe")
    )
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.asyncio
async def test_resolve_grid_snapshot_error_returns_none() -> None:
    element = FakeToggleElement(
        "input", {"type": "checkbox"}, checked=True, grid_snapshot_error=RuntimeError("detached")
    )
    assert await handler._resolve_live_selected_state(element) is None


@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.asyncio
async def test_resolve_non_grid_checkbox_reads_native(checked: bool) -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=checked)
    assert await handler._resolve_live_selected_state(element) is checked


# --- _apply_desired_click_state — routing for grid row-selection checkboxes ---


@pytest.mark.asyncio
async def test_apply_grid_row_already_selected_suppresses_without_driver() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot=_grid_snapshot(rowAriaSelected="true"))
    drive = AsyncMock()
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)
    drive.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_grid_row_unmarked_desired_false_box_off_suppresses() -> None:
    # An UNMARKED row (no positive selected signal) with the native box positively off already matches
    # desired=unselected, so the click is suppressed without driving the cell.
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=False, grid_snapshot=_grid_snapshot())
    drive = AsyncMock()
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, False, MagicMock())
    assert result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)
    drive.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_grid_row_unmarked_desired_false_box_checked_falls_open() -> None:
    # S2 at the unit level: an UNMARKED row whose native box is checked cannot be proven unselected, so
    # desired=False must NOT be suppressed as already-satisfied -- fall open, never claim success.
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=True, grid_snapshot=_grid_snapshot())
    drive = AsyncMock()
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, False, MagicMock())
    assert result is None
    drive.assert_not_awaited()


@pytest.mark.parametrize(
    "snapshot,desired",
    [
        # A positively-readable mismatch: UNSELECTED -> select, or SELECTED -> deselect. Only these
        # readable starts are driven; an UNMARKED row is never routed to the cell (see below).
        (_grid_snapshot(rowAriaSelected="false"), True),
        (_grid_snapshot(rowAriaSelected="true"), False),
    ],
)
@pytest.mark.asyncio
async def test_apply_grid_row_mismatch_routes_to_driver(snapshot: dict[str, object], desired: bool) -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot=snapshot)
    sentinel: list[ActionResult] = [ActionAbort()]
    drive = AsyncMock(return_value=sentinel)
    set_state = AsyncMock(return_value=True)
    with (
        patch.object(handler, "_drive_grid_row_selection", drive),
        patch.object(handler, "_set_native_checkbox_state", set_state),
    ):
        result = await handler._apply_desired_click_state(_click_action(), element, desired, MagicMock())
    assert result is sentinel
    drive.assert_awaited_once()
    assert drive.await_args.args[2] is desired
    # A grid row-selection mismatch is driven through the cell, never a native check()/uncheck().
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_grid_row_driver_failure_falls_open() -> None:
    # A positively-UNSELECTED row is driven; if the drive cannot prove the selection it returns None and
    # the caller falls open to the ordinary click.
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot=_grid_snapshot(rowAriaSelected="false"))
    drive = AsyncMock(return_value=None)
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is None
    drive.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_grid_row_unmarked_desired_true_falls_open_without_driver() -> None:
    # v3.1 invariant: an UNMARKED row (no positive selected signal) with desired=True is DOM-
    # indistinguishable from a foreign-vocabulary already-selected row and has no readable post-state, so
    # it is NEVER cell-driven -- it falls open to the single ordinary click.
    element = FakeToggleElement("input", {"type": "checkbox"}, checked=True, grid_snapshot=_grid_snapshot())
    drive = AsyncMock()
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is None
    drive.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_grid_row_unreadable_falls_open_without_driver() -> None:
    element = FakeToggleElement("input", {"type": "checkbox"}, grid_snapshot=_grid_snapshot(rowAriaSelected="maybe"))
    drive = AsyncMock()
    with patch.object(handler, "_drive_grid_row_selection", drive):
        result = await handler._apply_desired_click_state(_click_action(), element, True, MagicMock())
    assert result is None
    drive.assert_not_awaited()


# --- _coerce_click_position — validate the hit-tested cell point --------------


def test_coerce_click_position_valid_point() -> None:
    assert handler._coerce_click_position({"x": 1, "y": 2.5}) == {"x": 1.0, "y": 2.5}


@pytest.mark.parametrize("point", [None, "x", 5, [1, 2], {"x": 1.0}, {"y": 2.0}, {}])
def test_coerce_click_position_bad_shape_is_none(point: object) -> None:
    assert handler._coerce_click_position(point) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_coerce_click_position_non_finite_is_none(bad: float) -> None:
    assert handler._coerce_click_position({"x": bad, "y": 1.0}) is None
    assert handler._coerce_click_position({"x": 1.0, "y": bad}) is None


@pytest.mark.parametrize("value", [True, "1", None])
def test_coerce_click_position_non_number_is_none(value: object) -> None:
    assert handler._coerce_click_position({"x": value, "y": 1.0}) is None


# ---------------------------------------------------------------------------
# Real-Chromium arms — high-fidelity evidence against a live DOM. A selectable
# grid is modeled after a document grid whose row selection is app state on the
# closest row (aria-selected / a selected row class), driven by a click on the
# selection cell rather than the bare checkbox. Structure is pure ARIA
# (role=grid > role=row > role=gridcell > native checkbox); no framework token
# is used for detection. Skipped when Playwright's Chromium is not installed.
# ---------------------------------------------------------------------------


_GRID_FIXTURE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  body { margin: 0; }
  [role="grid"] { display: block; margin: 6px; }
  table { border-collapse: collapse; }
  td, th { padding: 0; border: 0; }
  .sel-cell { position: relative; width: 140px; height: 30px; }
  input[type="checkbox"] { margin: 0; }
  .cover-center { position: absolute; left: 0; top: 0; width: 60%; height: 100%; }
  .cover-mid { position: absolute; left: 30%; top: 0; width: 40%; height: 100%; }
  .fill-cell { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
  .clip-window { width: 60px; overflow: hidden; }
  .wide-cell { width: 400px; }
</style></head><body>

<div role="grid" id="grid-cell-selects" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell"><input type="checkbox" id="cb-cell-selects"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-no-select" data-arm="no-select">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell"><input type="checkbox" id="cb-no-select"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-reset" data-arm="reset">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell"><input type="checkbox" id="cb-reset"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-center-covered" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell">
      <input type="checkbox" class="cover-center" id="cb-center-covered"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-span-covered" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-span-covered"><span class="cover-mid" tabindex="0">detail</span></td>
      <td>Document row</td></tr></tbody></table></div>

<div class="clip-window">
  <div role="grid" id="grid-clipped" data-arm="cell-selects">
    <table role="presentation"><tbody role="rowgroup">
      <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell wide-cell">
        <input type="checkbox" id="cb-clipped"></td>
        <td>Document row</td></tr></tbody></table></div></div>

<div role="grid" id="grid-nested" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-nested"><button type="button">Open</button></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-no-safe-point" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell">
      <input type="checkbox" class="fill-cell" id="cb-no-safe-point"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-deselect" data-arm="deselect">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-deselect" data-preselected="true"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-read-aria">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" aria-selected="true"><td role="gridcell"><input type="checkbox" id="cb-read-aria" checked></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-read-unselected">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row"><td role="gridcell"><input type="checkbox" id="cb-read-unselected"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-read-kselected">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row k-selected"><td role="gridcell"><input type="checkbox" id="cb-read-kselected"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-read-isselected">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row is-selected"><td role="gridcell"><input type="checkbox" id="cb-read-isselected"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-header">
  <table role="presentation">
    <thead><tr role="row"><th role="columnheader"><input type="checkbox" id="cb-header-all" checked></th>
      <th role="columnheader">Title</th></tr></thead>
    <tbody role="rowgroup"><tr role="row"><td role="gridcell"><input type="checkbox" id="cb-header-data"></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-data-multi" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row"><td role="gridcell">Name</td>
      <td role="gridcell" class="sel-cell"><input type="checkbox" id="cb-data-multi"></td>
      <td role="gridcell">Active</td></tr></tbody></table></div>

<div role="grid" id="grid-data-multi-checked" data-arm="cell-selects">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row"><td role="gridcell">Name</td>
      <td role="gridcell" class="sel-cell"><input type="checkbox" id="cb-data-multi-checked" checked></td>
      <td role="gridcell">Active</td></tr></tbody></table></div>

<div role="grid" id="grid-incident-checked" data-arm="native-onchange">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-incident-checked" checked></td>
      <td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-replace" data-arm="replace">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-replace-a" data-preselected="true"></td><td>Row A</td></tr>
    <tr role="row" class="data-row" aria-selected="false"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-replace-b"></td><td>Row B</td></tr></tbody></table></div>

<div role="grid" id="grid-unknown-selected">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row app-picked"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-unknown-selected" checked></td><td>Row</td></tr></tbody></table></div>

<div role="grid" id="grid-unknown-with-neighbor" data-arm="replace">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" aria-selected="true" class="is-selected"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-neighbor-selected" checked></td><td>Neighbor</td></tr>
    <tr role="row" class="data-row app-picked"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-unknown-target" checked></td><td>Target</td></tr></tbody></table></div>

<div role="grid" id="grid-foreign-selected" data-arm="native-onchange">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row app-picked"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-foreign-selected" checked></td><td>Document row</td></tr></tbody></table></div>

<div role="grid" id="grid-foreign-replace" data-arm="foreign-replace">
  <table role="presentation"><tbody role="rowgroup">
    <tr role="row" class="data-row app-picked"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-foreign-a" checked></td><td>Row A</td></tr>
    <tr role="row" class="data-row"><td role="gridcell" class="sel-cell">
      <input type="checkbox" id="cb-foreign-b"></td><td>Row B</td></tr></tbody></table></div>

<script>
(function () {
  var model = {};
  function render(input) {
    var row = input.closest('[role="row"]');
    var selected = !!model[input.id];
    if (selected) { row.setAttribute('aria-selected', 'true'); row.classList.add('is-selected'); }
    else { row.removeAttribute('aria-selected'); row.classList.remove('is-selected'); }
    input.checked = selected;
  }
  function select(input, on) { model[input.id] = on; render(input); }
  // Foreign-vocabulary selection: published ONLY through a row class the handler does not recognise and
  // with NO aria-selected anywhere -- the real incident grid's shape, whose selected rows are not
  // positively readable. It is driven by the checkbox's OWN change event, never by a cell-space click.
  function foreignSelected(input) { return input.closest('[role="row"]').classList.contains('app-picked'); }
  function selectForeign(input, on) {
    var row = input.closest('[role="row"]');
    if (on) { row.classList.add('app-picked'); } else { row.classList.remove('app-picked'); }
    input.checked = on;
  }
  Array.prototype.slice.call(document.querySelectorAll('[role="grid"][data-arm]')).forEach(function (g) {
    var arm = g.dataset.arm;
    Array.prototype.slice.call(g.querySelectorAll('tbody input[type=checkbox]')).forEach(function (input) {
      if (input.dataset.preselected === 'true') { model[input.id] = true; render(input); }
      var cell = input.closest('[role="gridcell"]');
      // Tally every cell-space click (anything but a click on the input itself) in the capture phase, so
      // a test can prove the recovery never clicked the selection cell -- the forbidden probe on an
      // ambiguous row. Capture runs before any stopPropagation.
      cell.addEventListener('click', function (e) {
        if (e.target === input) { return; }
        cell.dataset.cellClicks = String(parseInt(cell.dataset.cellClicks || '0', 10) + 1);
      }, true);
      // A bare toggle of the input never enters selection state and stops propagation, so a pointer
      // that lands on the input can never reach the cell's selection handler (checked-only divergence).
      input.addEventListener('click', function (e) { e.stopPropagation(); });
      var mid = cell.querySelector('.cover-mid');
      if (mid) { mid.addEventListener('click', function (e) { e.stopPropagation(); }); }
      if (arm === 'native-onchange') {
        // Controlled native checkbox: the app's row selection follows the input's OWN change event and is
        // published only as a foreign row class. A cell-space click never selects; only the checkbox does.
        input.addEventListener('change', function () { selectForeign(input, !foreignSelected(input)); });
        return;
      }
      if (arm === 'no-select') { return; }
      cell.addEventListener('click', function (e) {
        if (e.target === input) { return; }
        if (arm === 'reset') {
          select(input, true);
          requestAnimationFrame(function () { select(input, false); });
        } else if (arm === 'deselect') {
          select(input, !model[input.id]);
        } else if (arm === 'replace') {
          // Replace-selection semantics: clicking a row's cell clears every row's selection in
          // this grid (including any statically-marked neighbour), then selects the clicked row.
          Array.prototype.slice.call(g.querySelectorAll('tr[role="row"]')).forEach(function (r) {
            r.removeAttribute('aria-selected');
            r.classList.remove('is-selected');
            var other = r.querySelector('input[type=checkbox]');
            if (other) { other.checked = false; model[other.id] = false; }
          });
          select(input, true);
        } else if (arm === 'foreign-replace') {
          // Replace-selection published ONLY through the foreign class (no aria-selected): a cell click
          // clears every row's foreign selection, then selects the clicked row.
          Array.prototype.slice.call(g.querySelectorAll('tr[role="row"]')).forEach(function (r) {
            r.classList.remove('app-picked');
            var other = r.querySelector('input[type=checkbox]');
            if (other) { other.checked = false; }
          });
          selectForeign(input, true);
        } else {
          select(input, true);
        }
      });
    });
  });
})();
</script>
</body></html>"""


def _real_checkbox_element(page: Page, css_id: str) -> SkyvernElement:
    static = {"id": css_id.upper(), "tagName": "input", "attributes": {"type": "checkbox", "id": css_id}}
    return SkyvernElement(page.locator(f"#{css_id}"), page, static)


@contextlib.asynccontextmanager
async def _grid_fixture_page() -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # A tall viewport keeps every stacked grid on-screen so the cell hit-test probes a
            # visible region; per-element clip tests rely on their own `.clip-window`, not the viewport.
            context = await browser.new_context(viewport={"width": 1280, "height": 2400})
            page = await context.new_page()
            await page.set_content(_GRID_FIXTURE_HTML)
            yield page
        finally:
            await browser.close()


async def _row_selected(page: Page, css_id: str) -> bool:
    return await page.evaluate(
        "(id) => { const el = document.getElementById(id); const row = el.closest('[role=\"row\"]');"
        " return !!(row && row.getAttribute('aria-selected') === 'true'); }",
        css_id,
    )


async def _native_checked(page: Page, css_id: str) -> bool:
    return await page.evaluate("(id) => !!document.getElementById(id).checked", css_id)


async def _foreign_selected(page: Page, css_id: str) -> bool:
    # Row selection published only through the app's own class (no aria-selected) -- the incident's shape.
    return await page.evaluate(
        "(id) => { const el = document.getElementById(id); const row = el.closest('[role=\"row\"]');"
        " return !!(row && row.classList.contains('app-picked')); }",
        css_id,
    )


async def _cell_clicks(page: Page, css_id: str) -> int:
    # How many cell-space clicks (not clicks on the input) the checkbox's selection cell received.
    return await page.evaluate(
        "(id) => { const el = document.getElementById(id); const cell = el.closest('[role=\"gridcell\"], td');"
        " return cell ? parseInt(cell.dataset.cellClicks || '0', 10) : 0; }",
        css_id,
    )


async def _apply_grid(page: Page, css_id: str, desired_state: bool) -> list[ActionResult] | None:
    element = _real_checkbox_element(page, css_id)
    return await handler._apply_desired_click_state(_click_action(), element, desired_state, MagicMock())


def _is_abort(result: list[ActionResult] | None) -> bool:
    return result is not None and len(result) == 1 and isinstance(result[0], ActionAbort)


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_readable_unselected_desired_true_drives_selection() -> None:
    # A readable grid (explicit aria-selected): a positively-UNSELECTED row with desired_state=True is
    # driven through the cell to a positively-readable SELECTED post-state, then the duplicate click is
    # suppressed -- the retained cell drive, legitimate because both the start and the proof are readable.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-cell-selects", desired_state=True)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-cell-selects") is True
        assert await _native_checked(page, "cb-cell-selects") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_row_already_selected_suppresses_without_clicking() -> None:
    # A selected row with desired_state=True suppresses the click; the cell (which would toggle the row
    # off) is never clicked, proving suppression rather than a redundant drive.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-deselect", desired_state=True)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-deselect") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_no_selection_affordance_falls_open() -> None:
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-no-select", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-no-select") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_selection_reset_after_settle_falls_open() -> None:
    # The cell click briefly selects, then the app clears it within the settle window. Re-reading after
    # the settle must see the row unselected and fall open, never report a transient selection.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-reset", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-reset") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_checkbox_covering_center_hit_tests_side_point() -> None:
    # The checkbox covers the cell's center; a naive center click lands on the input (no selection).
    # Hit-testing must find clear cell space to the side and drive the row selection there.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-center-covered", desired_state=True)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-center-covered") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_unexamined_span_cover_hit_tests_side_point() -> None:
    # A focusable span (matched by no control allow-list) covers the cell center; only hit-testing
    # finds the real cell space beside it. Proves detection is by actual hit target, not a selector list.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-span-covered", desired_state=True)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-span-covered") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_clipped_cell_hit_tests_visible_region() -> None:
    # The selection cell is wider than its clipping ancestor, so the padding-box center is scrolled out
    # of view. Probing must clip to the visible region and find real cell space there.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-clipped", desired_state=True)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-clipped") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_no_safe_cell_point_falls_open() -> None:
    # The checkbox covers the entire cell, so no point hit-tests to clear cell space. Recovery must
    # fail closed (no click) and fall open rather than click the input.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-no-safe-point", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-no-safe-point") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_cell_with_nested_control_falls_open() -> None:
    # The selection cell nests a <button>; a recovery click could activate it. Detection must fail
    # closed and never click, even though clear cell space exists and would otherwise select the row.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-nested", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-nested") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_desired_false_deselects_selected_row() -> None:
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-deselect", desired_state=False)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-deselect") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_desired_false_already_unselected_suppresses() -> None:
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-no-select", desired_state=False)
        assert _is_abort(result)
        assert await _row_selected(page, "cb-no-select") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_resolve_reads_grid_row_aria_selected_from_real_dom() -> None:
    async with _grid_fixture_page() as page:
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-read-aria")) is True
        # No aria-selected and no known class is absence (UNMARKED) -> None, not a positive False.
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-read-unselected")) is None


@_skip_no_browser
@pytest.mark.asyncio
async def test_resolve_reads_selected_row_class_from_real_dom() -> None:
    async with _grid_fixture_page() as page:
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-read-kselected")) is True
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-read-isselected")) is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_resolve_grid_header_select_all_reads_native_not_row() -> None:
    # A header select-all checkbox is not a data-row selection control: it reads native `checked` (here
    # True). The unselected data row exposes no positive selected signal (UNMARKED) -> None, not a
    # positive False read from absence.
    async with _grid_fixture_page() as page:
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-header-all")) is True
        assert await handler._resolve_live_selected_state(_real_checkbox_element(page, "cb-header-data")) is None


@_skip_no_browser
@pytest.mark.asyncio
async def test_read_grid_row_selection_from_real_dom() -> None:
    async with _grid_fixture_page() as page:
        # A row with no positive signal at all (no aria-selected, no known class) is UNMARKED (absence);
        # a row with aria-selected="false" is a positive UNSELECTED; an aria-selected row is SELECTED.
        unmarked = await handler._read_grid_row_selection(_real_checkbox_element(page, "cb-read-unselected"))
        assert unmarked.state is handler._GridRowSelection.UNMARKED
        positively_unselected = await handler._read_grid_row_selection(_real_checkbox_element(page, "cb-cell-selects"))
        assert positively_unselected.state is handler._GridRowSelection.UNSELECTED
        selected = await handler._read_grid_row_selection(_real_checkbox_element(page, "cb-read-aria"))
        assert selected.state is handler._GridRowSelection.SELECTED


# ---------------------------------------------------------------------------
# Ordinary data-column checkbox vs. selection cell — the unique-selection-cell
# signature (exactly one direct role=gridcell cell, and it is the checkbox's
# closest cell) separates a row-selection control from a boolean data checkbox in
# a standards-complete ARIA grid. A data checkbox is set natively and never drives
# row selection. The selection-cell row is driven only from a positively-readable
# start (UNSELECTED/SELECTED); when its selection is unreadable (UNMARKED, the real
# incident's shape) it is handled by the ordinary click, never the cell drive.
# ---------------------------------------------------------------------------


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_data_checkbox_multi_gridcell_desired_false_unchecks_natively() -> None:
    # Ordinary data checkbox in a grid where every data cell has role=gridcell: checked + desired=False
    # must uncheck the native input and must not touch row selection. Before the fix the row reads
    # unselected==False, the click is suppressed, and the box stays checked (silent false success).
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-data-multi-checked", desired_state=False)
        assert _is_abort(result)
        assert await _native_checked(page, "cb-data-multi-checked") is False
        assert await _row_selected(page, "cb-data-multi-checked") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_data_checkbox_multi_gridcell_desired_true_checks_natively_without_selecting_row() -> None:
    # Same standards-complete grid, unchecked + desired=True: check the native input and never drive the
    # cell. Before the fix a cell click selects the row (spurious side effect) and leaves the box
    # unchecked.
    async with _grid_fixture_page() as page:
        result = await _apply_grid(page, "cb-data-multi", desired_state=True)
        assert _is_abort(result)
        assert await _native_checked(page, "cb-data-multi") is True
        assert await _row_selected(page, "cb-data-multi") is False


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_incident_unmarked_native_checked_falls_open_to_ordinary_click() -> None:
    # The real incident, vocabulary-faithful: selection is published ONLY through an app-specific row
    # class (no aria-selected anywhere) driven by the checkbox's own onChange, and the native box is
    # already `checked` while the row is unselected (the divergence). The row reads UNMARKED, so there is
    # NO positive selected post-state a cell drive could ever verify. The guard must NOT probe the cell --
    # it must fall open so the caller's single ordinary click fires onChange and enters app selection.
    async with _grid_fixture_page() as page:
        assert await _native_checked(page, "cb-incident-checked") is True
        assert await _foreign_selected(page, "cb-incident-checked") is False
        result = await _apply_grid(page, "cb-incident-checked", desired_state=True)
        assert result is None
        assert await _cell_clicks(page, "cb-incident-checked") == 0
        # The one ordinary click the caller now performs is what actually enters app selection.
        await page.locator("#cb-incident-checked").click()
        assert await _foreign_selected(page, "cb-incident-checked") is True
        assert await _native_checked(page, "cb-incident-checked") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_unknown_class_selected_desired_true_not_speculatively_cell_clicked() -> None:
    # A single row already selected through an unrecognised app class (no aria-selected), native box
    # checked, desired=True. It reads UNMARKED and is DOM-indistinguishable from the incident, so the
    # guard cannot prove it is already selected -- but it must NEVER speculatively drive the cell (a
    # state-changing probe on a toggle grid could clear the very selection it should keep). It falls open
    # without touching the cell, leaving the row's selection intact. FAILS before the fix (the cell is
    # driven). The subsequent ordinary click's irreducible toggle is the documented residual, not tested.
    async with _grid_fixture_page() as page:
        assert await _foreign_selected(page, "cb-foreign-selected") is True
        result = await _apply_grid(page, "cb-foreign-selected", desired_state=True)
        assert result is None
        assert await _cell_clicks(page, "cb-foreign-selected") == 0
        assert await _foreign_selected(page, "cb-foreign-selected") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_unknown_class_other_row_not_cleared_by_cell_drive() -> None:
    # Row A is selected through an unrecognised app class, invisible to the other-row-selected gate (which
    # only reads aria-selected/known tokens). Acting on the UNMARKED row B with desired=True must NOT
    # cell-drive, because a replace-semantics cell click would silently clear A. The guard falls open
    # without touching B's cell, so A's selection survives. FAILS before the fix (B is cell-driven, the
    # blind gate lets the replace clear A).
    async with _grid_fixture_page() as page:
        assert await _foreign_selected(page, "cb-foreign-a") is True
        result = await _apply_grid(page, "cb-foreign-b", desired_state=True)
        assert result is None
        assert await _cell_clicks(page, "cb-foreign-b") == 0
        assert await _foreign_selected(page, "cb-foreign-a") is True


# ---------------------------------------------------------------------------
# Formal-review corrections (SKY-13695): a cell drive must never clear another
# row's selection, absence of a selected signal is not a positive UNSELECTED,
# and the type dispatch must fail open rather than propagate.
# ---------------------------------------------------------------------------


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_desired_true_preserves_other_selected_row() -> None:
    # S5: row A is already selected; selecting row B on a grid whose cell click REPLACES the selection
    # must not silently clear A. The drive is only safe when no other row holds a readable selection, so
    # here it must fall open (never ActionAbort) and leave A selected. FAILS before the fix (the cell
    # drive replaces {A} with {B} and reports itself satisfied).
    async with _grid_fixture_page() as page:
        assert await _row_selected(page, "cb-replace-a") is True
        result = await _apply_grid(page, "cb-replace-b", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-replace-a") is True


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_desired_false_unknown_class_native_checked_not_suppressed() -> None:
    # S2: a row selected via a class outside the known vocabulary, native checkbox checked, desired=False.
    # Absence of a recognised selected signal is UNREADABLE, not a positive UNSELECTED, so the click must
    # not be suppressed as already-satisfied. FAILS before the fix (classified UNSELECTED -> suppressed ->
    # nothing happens and the run reports success).
    async with _grid_fixture_page() as page:
        assert await _native_checked(page, "cb-unknown-selected") is True
        result = await _apply_grid(page, "cb-unknown-selected", desired_state=False)
        assert result is None


@_skip_no_browser
@pytest.mark.asyncio
async def test_grid_desired_true_unknown_class_preserves_readable_neighbor() -> None:
    # S3: acting on a row whose selection is carried by an unrecognised class (desired=True) must not
    # speculatively drive the cell and clear a neighbouring row that IS readably selected. The target
    # reads UNMARKED (unrecognised class, no aria-selected), and an UNMARKED row is never cell-driven, so
    # it falls open to the ordinary click and the readable neighbour survives untouched.
    async with _grid_fixture_page() as page:
        assert await _row_selected(page, "cb-neighbor-selected") is True
        result = await _apply_grid(page, "cb-unknown-target", desired_state=True)
        assert result is None
        assert await _row_selected(page, "cb-neighbor-selected") is True


class _RaisingTypeElement:
    """A checkbox-tagged element whose ``get_attr`` raises (a detached/unreadable input)."""

    def get_tag_name(self) -> str:
        return "input"

    def get_id(self) -> str:
        return "el"

    async def get_attr(self, attr_name: str, mode: str = "auto", timeout: float | None = None) -> str | None:
        raise RuntimeError("element detached")

    def get_locator(self) -> object:
        raise AssertionError("must fall open before touching the locator")


@pytest.mark.asyncio
async def test_apply_get_attr_type_raises_falls_open() -> None:
    # A raising get_attr("type") on the input dispatch must fall open to the ordinary click, matching the
    # merge base, rather than propagating out of _apply_desired_click_state. FAILS before the fix.
    result = await handler._apply_desired_click_state(_click_action(), _RaisingTypeElement(), True, MagicMock())
    assert result is None
