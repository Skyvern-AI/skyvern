from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import ElementHandle
from structlog.testing import capture_logs

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3 import input_dispatch
from skyvern.forge.taskv3.run_arms import LOGIN_PACE_FLAG
from tests.unit.scoped_asyncio import ScopedAsyncio

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


class _FakePage(_Device):
    def __init__(self) -> None:
        self.calls: list[Call] = []
        super().__init__(self.calls, "page")
        self.keyboard = _Device(self.calls, "keyboard")
        self.mouse = _Device(self.calls, "mouse")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    # As a frame realm: the main frame of its own page, never detached, with no navigation events.
    parent_frame = None

    def is_detached(self) -> bool:
        return False

    def on(self, event: str, handler: Callable[..., None]) -> None:
        pass

    def remove_listener(self, event: str, handler: Callable[..., None]) -> None:
        pass


@pytest.fixture
def page() -> _FakePage:
    return _FakePage()


@pytest.mark.asyncio
async def test_each_gesture_is_the_exact_playwright_call_it_replaced(page: _FakePage) -> None:
    locator = page.locator("#seg")
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
async def test_an_element_handle_is_clicked_with_only_the_arguments_given() -> None:
    handle = MagicMock(spec=ElementHandle)
    handle.click = AsyncMock()

    await input_dispatch.click_handle(_FakePage(), handle)

    handle.click.assert_awaited_once_with()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(input_dispatch, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(input_dispatch, "asyncio", ScopedAsyncio(sleep=clock.sleep))
    return clock


@pytest.fixture
def login_page() -> _FakePage:
    page = _FakePage()
    page.url = "https://login.test/password"
    page.page = page
    return page


@pytest.fixture
def arm(request: pytest.FixtureRequest) -> Iterator[str]:
    arm = getattr(request, "param", "treatment")
    skyvern_context.set(SkyvernContext(run_arms={LOGIN_PACE_FLAG: ("wr_login", arm)}))
    yield arm
    input_dispatch.end_login_pace()
    skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["treatment", "control", "unrandomized"], indirect=True)
async def test_only_the_treatment_holds_the_submit_after_a_password_fill(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    input_dispatch.start_login_pace()
    clock.now += 5
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    await input_dispatch.click(login_page, "#sign-in")

    assert sum(clock.sleeps) == (40.0 if arm == "treatment" else 0)
    assert login_page.calls[-1][0] == "page.click"


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed, waited", [(30.0, 15.0), (60.0, 0.0)])
async def test_the_dwell_is_measured_from_the_block_start_across_both_login_pages(
    arm: str, clock: _Clock, login_page: _FakePage, elapsed: float, waited: float
) -> None:
    input_dispatch.start_login_pace()
    login_page.url = "https://login.test/username"
    clock.now += elapsed / 2
    await input_dispatch.click(login_page, "#continue")
    login_page.url = "https://login.test/password"
    clock.now += elapsed / 2
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    with capture_logs() as logs:
        await input_dispatch.press(login_page, "#pw", "Enter")

    # Continue came before the password fill, so only the submit waits, and only for what the floor still needs.
    assert sum(clock.sleeps) == waited
    dwell = [log for log in logs if log["event"] == "Task V3 login pace dwell"]
    assert [(log["waited_s"], log["nav_to_submit_s"]) for log in dwell] == [(waited, max(elapsed, 45.0))]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gesture",
    [
        lambda page: input_dispatch.click(page, "#sign-in"),
        lambda page: input_dispatch.click(page, page.locator("#sign-in")),
        lambda page: input_dispatch.click_at(page, 10.0, 20.0),
        lambda page: input_dispatch.press(page, "#pw", "Enter"),
        lambda page: input_dispatch.press(page, None, "Enter"),
        lambda page: input_dispatch.js_click(page, "document", {"sel": "#sign-in"}),
    ],
)
async def test_every_click_and_enter_after_a_password_fill_is_held_once(
    arm: str, clock: _Clock, login_page: _FakePage, gesture: Callable[[_FakePage], Awaitable[Any]]
) -> None:
    input_dispatch.start_login_pace()
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    await input_dispatch.press(login_page, "#pw", "Tab")
    assert clock.sleeps == []

    await gesture(login_page)
    # A retyped password and a second submit on the same login wait no more.
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    await gesture(login_page)

    assert sum(clock.sleeps) == 45.0


@pytest.mark.asyncio
async def test_no_dwell_and_no_mechanism_log_without_a_password_fill(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    input_dispatch.start_login_pace()
    with capture_logs() as logs:
        await input_dispatch.click(login_page, "#search")
        await input_dispatch.press(login_page, "#q", "Enter")
        input_dispatch.end_login_pace()

    assert clock.sleeps == []
    assert logs == []


@pytest.mark.asyncio
async def test_a_submit_the_hook_never_saw_is_logged_as_not_held(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    input_dispatch.start_login_pace()
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    clock.now += 8
    # The page submitted on its own: the next click is already on another page and must not take the dwell.
    login_page.url = "https://login.test/challenge"
    with capture_logs() as logs:
        await input_dispatch.click(login_page, "#verify")
        input_dispatch.start_login_pace()
        input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
        input_dispatch.end_login_pace()

    assert clock.sleeps == []
    assert [(log["event"], log["reason"], log["nav_s"]) for log in logs] == [
        ("Task V3 login pace submit not held", "page_moved_before_submit", 8.0),
        ("Task V3 login pace submit not held", "no_click_or_enter_after_password", 0.0),
    ]


@pytest.mark.asyncio
async def test_a_hold_cancelled_by_the_callers_timeout_still_holds_the_submit(
    arm: str, clock: _Clock, login_page: _FakePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dispatch.start_login_pace()
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    real_sleep = clock.sleep

    async def _cancelled_once(seconds: float) -> None:
        monkeypatch.setattr(input_dispatch, "asyncio", ScopedAsyncio(sleep=real_sleep))
        clock.now += 5
        raise asyncio.CancelledError

    monkeypatch.setattr(input_dispatch, "asyncio", ScopedAsyncio(sleep=_cancelled_once))
    with pytest.raises(asyncio.CancelledError):
        await input_dispatch.click(login_page, "#captcha-checkbox")
    await input_dispatch.click(login_page, "#sign-in")

    assert sum(clock.sleeps) == 40.0
    assert login_page.calls == [("page.click", ("#sign-in",), {})]


@pytest.mark.asyncio
async def test_a_task_canceled_during_the_wait_never_sends_any_held_submit(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    polls: list[float] = []

    async def _should_cancel() -> bool:
        polls.append(clock.now)
        return len(polls) == 3

    input_dispatch.start_login_pace(_should_cancel)
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    with capture_logs() as logs:
        with pytest.raises(input_dispatch.LoginPaceRefused):
            await input_dispatch.click(login_page, "#sign-in")
        # A caller that swallows the refusal and tries another submit is refused too.
        with pytest.raises(input_dispatch.LoginPaceRefused):
            await input_dispatch.press(login_page, "#pw", "Enter")
        input_dispatch.end_login_pace()

    assert login_page.calls == []
    assert sum(clock.sleeps) == 6.0
    assert [log["reason"] for log in logs] == ["canceled_during_wait"]


@pytest.mark.asyncio
async def test_a_page_that_moves_during_the_wait_does_not_get_the_held_click(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    async def _page_moves_on_second_poll() -> bool:
        if clock.now >= 1004:
            login_page.url = "https://login.test/verify"
        return False

    input_dispatch.start_login_pace(_page_moves_on_second_poll)
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    with capture_logs() as logs:
        with pytest.raises(input_dispatch.LoginPaceRefused):
            await input_dispatch.click(login_page, "#sign-in")
        await input_dispatch.click(login_page, "#continue-on-new-page")

    assert login_page.calls == [("page.click", ("#continue-on-new-page",), {})]
    assert [log["reason"] for log in logs] == ["page_moved_during_wait"]


@pytest.mark.asyncio
async def test_a_failed_cancel_read_still_sends_the_held_submit(arm: str, clock: _Clock, login_page: _FakePage) -> None:
    async def _status_read_fails() -> bool:
        raise ConnectionError("db blip")

    input_dispatch.start_login_pace(_status_read_fails)
    input_dispatch.note_secret_fill(login_page, input_dispatch.secret_fill_anchor(login_page))
    await input_dispatch.click(login_page, "#sign-in")

    assert sum(clock.sleeps) == 45.0
    assert login_page.calls == [("page.click", ("#sign-in",), {})]


@pytest.mark.asyncio
async def test_a_frame_that_moved_during_the_password_fill_does_not_hold_the_next_click(
    arm: str, clock: _Clock, login_page: _FakePage
) -> None:
    input_dispatch.start_login_pace()
    anchor = input_dispatch.secret_fill_anchor(login_page)
    # The frame's own URL moved while the password went in; the top URL did not.
    input_dispatch.note_secret_fill(login_page, replace(anchor, frame_url="https://login.test/frame-before"))
    with capture_logs() as logs:
        await input_dispatch.click(login_page, "#next")

    assert clock.sleeps == []
    assert [(log["reason"], log["moved_by"]) for log in logs] == [("page_moved_before_submit", "frame_url")]
