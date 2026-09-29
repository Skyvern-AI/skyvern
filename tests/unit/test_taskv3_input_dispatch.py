from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import ElementHandle, Locator, Page

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import RunArm, SkyvernContext
from skyvern.forge.sdk.event.base import CursorEventStrategy, InputEventStrategy, ScrollEventStrategy
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.forge.taskv3 import input_dispatch
from skyvern.forge.taskv3.run_arms import HUMANIZED_INPUT_FLAG, resolve_run_arm

Call = tuple[str, tuple[Any, ...], dict[str, Any]]


class _Device:
    def __init__(self, log: list[Call], name: str) -> None:
        self._log = log
        self._name = name

    def __getattr__(self, attr: str) -> Callable[..., Awaitable[None]]:
        async def record(*args: Any, **kwargs: Any) -> None:
            self._log.append((f"{self._name}.{attr}", args, kwargs))

        return record


class _FakeLocator(_Device):
    def __init__(self, page: _FakePage, selector: str) -> None:
        super().__init__(page.calls, f"locator({selector})")
        self.page = page
        self.first = self


class _FakeHandle(_Device):
    def __init__(self, page: _FakePage) -> None:
        super().__init__(page.calls, "handle")

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 10.0, "y": 20.0, "width": 4.0, "height": 6.0}


class _FakePage(_Device):
    def __init__(self) -> None:
        self.calls: list[Call] = []
        super().__init__(self.calls, "page")
        self.keyboard = _Device(self.calls, "keyboard")
        self.mouse = _Device(self.calls, "mouse")

    def __getattr__(self, attr: str) -> Callable[..., Awaitable[None]]:
        # A top-level page has neither, which is how the dispatcher tells it from a frame.
        if attr in ("page", "parent_frame"):
            raise AttributeError(attr)
        return super().__getattr__(attr)

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


class _RecordingCursor(CursorEventStrategy):
    def __init__(self, log: list[Call]) -> None:
        self._log = log

    async def move_to(self, page: Page, x: float, y: float) -> None:
        self._log.append(("cursor.move_to", (x, y), {}))

    async def move_to_element(self, page: Page, locator: Locator) -> tuple[float, float]:
        self._log.append(("cursor.move_to_element", (locator,), {}))
        return 0.0, 0.0

    async def click(self, page: Page, locator: Locator, *, timeout: float | None = None) -> None:
        self._log.append(("cursor.click", (locator,), {"timeout": timeout}))


class _RecordingInput(InputEventStrategy):
    def __init__(self, log: list[Call]) -> None:
        self._log = log

    async def type_text(
        self,
        page: Page,
        locator: Locator | None,
        text: str,
        *,
        timeout: float | None,
        delay: float | None = None,
        no_wait_after: bool | None = None,
        allow_batched_playwright: bool = False,
    ) -> None:
        self._log.append(("input.type_text", (locator, text), {"delay": delay}))

    async def clear_field(
        self,
        page: Page,
        locator: Locator,
        char_count: int,
        *,
        timeout: float | None,
        force: bool | None = None,
        no_wait_after: bool | None = None,
    ) -> None:
        self._log.append(("input.clear_field", (locator,), {"timeout": timeout}))


class _RecordingScroll(ScrollEventStrategy):
    def __init__(self, log: list[Call]) -> None:
        self._log = log

    async def scroll_to_element(self, page: Page, locator: Locator) -> None:
        self._log.append(("scroll.scroll_to_element", (locator,), {}))

    async def scroll_by(self, page: Page, delta_y: float) -> None:
        self._log.append(("scroll.scroll_by", (delta_y,), {}))


@pytest.fixture
def page() -> _FakePage:
    return _FakePage()


@pytest.fixture
def humanized_strategies(page: _FakePage) -> Iterator[list[Call]]:
    """A humanized registration, as USE_EVENT_STRATEGIES makes it, logging into the page's own call list."""
    EventStrategyFactory.set_cursor_strategy(_RecordingCursor(page.calls))
    EventStrategyFactory.set_input_strategy(_RecordingInput(page.calls))
    EventStrategyFactory.set_scroll_strategy(_RecordingScroll(page.calls))
    EventStrategyFactory.record_profile("default")
    try:
        yield page.calls
    finally:
        EventStrategyFactory.reset()


def _pin(arm: RunArm) -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    context.run_arms = {HUMANIZED_INPUT_FLAG: ("wr_1", arm)}
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


@pytest.fixture
def control() -> Iterator[SkyvernContext]:
    yield from _pin("control")


@pytest.fixture
def treatment() -> Iterator[SkyvernContext]:
    yield from _pin("treatment")


def _names(calls: list[Call]) -> list[str]:
    return [name for name, _, _ in calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "humanized"),
    [
        (AsyncMock(return_value="treatment"), True),
        (AsyncMock(return_value="control"), False),
        (AsyncMock(return_value=None), False),
        (AsyncMock(return_value="true"), False),
        (AsyncMock(side_effect=RuntimeError("flag service down")), False),
    ],
    ids=["treatment", "control", "no_variant", "unknown_variant", "flag_error"],
)
async def test_only_a_treatment_run_sends_its_clicks_through_the_registered_strategy(
    monkeypatch: pytest.MonkeyPatch,
    page: _FakePage,
    humanized_strategies: list[Call],
    reader: AsyncMock,
    humanized: bool,
) -> None:
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", reader)
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        await resolve_run_arm(context, HUMANIZED_INPUT_FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)
        await input_dispatch.click(page, "#go", timeout=1234)
    finally:
        skyvern_context.reset()

    assert _names(page.calls) == (["cursor.click"] if humanized else ["page.click"])


@pytest.mark.asyncio
async def test_input_outside_a_resolved_run_is_plain_playwright(
    page: _FakePage, humanized_strategies: list[Call]
) -> None:
    skyvern_context.reset()

    await input_dispatch.click(page, "#go", timeout=1234)
    await input_dispatch.type_keys(page, "#q", "abc", delay=15, timeout=900)
    await input_dispatch.wheel(page, 0, 800)

    assert page.calls == [
        ("page.click", ("#go",), {"timeout": 1234}),
        ("page.type", ("#q", "abc"), {"delay": 15, "timeout": 900}),
        ("mouse.wheel", (0, 800), {}),
    ]


@pytest.mark.asyncio
async def test_control_sends_each_gesture_as_the_exact_playwright_call_it_replaced(
    control: SkyvernContext, page: _FakePage, humanized_strategies: list[Call]
) -> None:
    locator = page.locator("#seg")
    await input_dispatch.approach(page, "#go")
    await input_dispatch.click(page, "#go", timeout=5000, force=True)
    await input_dispatch.click(page, locator, timeout=2000)
    await input_dispatch.click_at(page, 10.0, 20.0)
    await input_dispatch.hover(page, "#menu", timeout=2000)
    await input_dispatch.focus(page, "#f", timeout=2000)
    await input_dispatch.fill(page, "#f", "v", timeout=100)
    await input_dispatch.clear(page, "#f", timeout=100)
    await input_dispatch.type_keys(page, "#f", "ab", delay=15, timeout=900)
    await input_dispatch.type_keys(page, "#f", "ab", timeout=900)
    await input_dispatch.type_keys(page, None, "7", delay=40)
    await input_dispatch.type_keys(page, locator, "7", delay=40)
    await input_dispatch.press(page, "#f", "Enter")
    await input_dispatch.press(page, None, "Escape")
    await input_dispatch.press(page, locator, "Backspace")
    await input_dispatch.select_option(page, "#s", value=["a"], timeout=100, force=False)
    await input_dispatch.wheel(page, 0, -800)

    assert page.calls == [
        ("page.click", ("#go",), {"timeout": 5000, "force": True}),
        ("locator(#seg).click", (), {"timeout": 2000}),
        ("mouse.click", (10.0, 20.0), {}),
        ("page.hover", ("#menu",), {"timeout": 2000}),
        ("page.focus", ("#f",), {"timeout": 2000}),
        ("page.fill", ("#f", "v"), {"timeout": 100}),
        ("page.fill", ("#f", ""), {"timeout": 100}),
        ("page.type", ("#f", "ab"), {"delay": 15, "timeout": 900}),
        ("page.type", ("#f", "ab"), {"timeout": 900}),
        ("keyboard.type", ("7",), {"delay": 40}),
        ("locator(#seg).press_sequentially", ("7",), {"delay": 40}),
        ("page.press", ("#f", "Enter"), {}),
        ("keyboard.press", ("Escape",), {}),
        ("locator(#seg).press", ("Backspace",), {}),
        ("page.select_option", ("#s",), {"value": ["a"], "timeout": 100, "force": False}),
        ("mouse.wheel", (0, -800), {}),
    ]


@pytest.mark.asyncio
async def test_control_clicks_an_element_handle_with_only_the_arguments_given(control: SkyvernContext) -> None:
    handle = MagicMock(spec=ElementHandle)
    handle.click = AsyncMock()

    await input_dispatch.click_handle(_FakePage(), handle)

    handle.click.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_treatment_routes_pointer_typing_and_scroll_through_the_registered_strategies(
    treatment: SkyvernContext, page: _FakePage, humanized_strategies: list[Call]
) -> None:
    await input_dispatch.click(page, "#go", timeout=1234)
    await input_dispatch.click(page, "#go", timeout=5000, force=True)
    await input_dispatch.click_at(page, 10.0, 20.0)
    await input_dispatch.hover(page, "#menu", timeout=2000)
    await input_dispatch.clear(page, "#f", timeout=100)
    await input_dispatch.wheel(page, 0, 800)

    assert _names(page.calls) == [
        "cursor.click",
        # The strategy has no forced click: it moves there, then Playwright clicks exactly as before.
        "cursor.move_to_element",
        "page.click",
        "cursor.move_to",
        "mouse.click",
        "cursor.move_to_element",
        "page.hover",
        "input.clear_field",
        "scroll.scroll_by",
    ]
    assert page.calls[2] == ("page.click", ("#go",), {"timeout": 5000, "force": True})


@pytest.mark.asyncio
async def test_treatment_clicks_a_locator_through_the_strategy_and_a_handle_after_a_cursor_move(
    treatment: SkyvernContext, page: _FakePage, humanized_strategies: list[Call]
) -> None:
    await input_dispatch.click(page, page.locator("#seg"), timeout=2000)
    await input_dispatch.click_handle(page, _FakeHandle(page), position={"x": 1.0, "y": 2.0}, timeout=1500)

    assert _names(page.calls) == ["cursor.click", "cursor.move_to", "handle.click"]
    assert page.calls[1:] == [
        ("cursor.move_to", (12.0, 23.0), {}),
        ("handle.click", (), {"position": {"x": 1.0, "y": 2.0}, "timeout": 1500}),
    ]


@pytest.mark.asyncio
async def test_treatment_types_through_the_keyboard_after_one_focus(
    treatment: SkyvernContext, page: _FakePage, humanized_strategies: list[Call]
) -> None:
    await input_dispatch.type_keys(page, "#f", "abc", delay=15, timeout=900)

    # The strategy gets no locator (given one, a humanized keyboard re-focuses the field before every key) and no
    # delay: as in v1, the registered strategy owns the cadence.
    assert page.calls == [
        ("page.focus", ("#f",), {"timeout": 900}),
        ("input.type_text", (None, "abc"), {"delay": None}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replace", "head_write"),
    [
        (True, ("page.fill", ("#f", "Hello, "), {"timeout": 900})),
        (False, ("keyboard.insert_text", ("Hello, ",), {})),
    ],
    ids=["emptied_field_fills_the_head", "append_inserts_the_head_at_the_caret"],
)
async def test_treatment_humanizes_only_the_last_ten_characters_as_v1_does(
    treatment: SkyvernContext,
    page: _FakePage,
    humanized_strategies: list[Call],
    replace: bool,
    head_write: Call,
) -> None:
    await input_dispatch.type_keys(page, "#f", "Hello, 0123456789", delay=15, timeout=900, replace=replace)

    assert page.calls[-2:] == [head_write, ("input.type_text", (None, "0123456789"), {"delay": None})]


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["control", "treatment"])
async def test_a_secret_is_typed_key_by_key_exactly_as_before_in_both_arms(
    page: _FakePage, humanized_strategies: list[Call], arm: RunArm
) -> None:
    # Split one-time-code boxes advance on each keystroke, so neither a fill nor a humanized keyboard may take over.
    for _ in _pin(arm):
        await input_dispatch.type_keys(page, "#code", "s3cret", delay=15, timeout=900, secret=True, replace=True)
        await input_dispatch.type_keys(page, page.locator("#box"), "7", timeout=1000, secret=True)

    assert page.calls == [
        ("page.type", ("#code", "s3cret"), {"delay": 15, "timeout": 900}),
        ("locator(#box).press_sequentially", ("7",), {"timeout": 1000}),
    ]


@pytest.mark.asyncio
async def test_treatment_approach_moves_then_waits_for_the_page_to_settle(
    treatment: SkyvernContext, page: _FakePage, humanized_strategies: list[Call]
) -> None:
    await input_dispatch.approach(page, "#go")

    assert _names(page.calls) == ["locator(#go).scroll_into_view_if_needed", "cursor.move_to_element", "page.evaluate"]


def test_arm_fields_name_the_arm_whether_it_is_in_effect_and_the_registered_profile(
    treatment: SkyvernContext, humanized_strategies: list[Call]
) -> None:
    assert input_dispatch.arm_fields() == {
        "humanized_input_arm": "treatment",
        "humanized_input_in_effect": True,
        "global_event_strategy_profile": "default",
    }


def test_registered_profile_tells_no_registration_from_an_unnamed_one() -> None:
    EventStrategyFactory.reset()
    try:
        assert EventStrategyFactory.registered_profile() == "none"
        EventStrategyFactory.set_cursor_strategy(_RecordingCursor([]))
        assert EventStrategyFactory.registered_profile() == "unrecorded"
        EventStrategyFactory.record_profile("kernel")
        assert EventStrategyFactory.registered_profile() == "kernel"
    finally:
        EventStrategyFactory.reset()
    assert EventStrategyFactory.registered_profile() == "none"
