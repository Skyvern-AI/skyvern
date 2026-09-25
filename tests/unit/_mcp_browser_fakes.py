from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import browser_ops
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import tabs as mcp_tabs
from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.exceptions import StaleFrameSelectionError
from skyvern.webeye import action_deadline

HANGING_PAGE_URL = "https://fixture.test/metrics"
HANGING_BOUND_MS = 200
HANGING_HEADROOM_MS = 50


class StaleScopePage:
    """Page whose selected frame belongs to another tab, so reading the locator scope refuses.

    Everything else forwards to the raw page, so a caller that skips the ownership read still
    reaches a working page-space action and the test fails.
    """

    def __init__(self, raw: MagicMock) -> None:
        self.page = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self.page, name)

    @property
    def locator_scope(self) -> Any:
        raise StaleFrameSelectionError("payment", "https://frame.example.com/")


def make_real_wait_for_timeout() -> AsyncMock:
    """A `page.wait_for_timeout` fake that actually sleeps, for tests driving real deadline loops."""

    async def _wait(delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)

    return AsyncMock(side_effect=_wait)


def make_session_state(**overrides: Any) -> SessionState:
    state = SessionState(tab_state_persists=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
        raw.on = MagicMock()
    return SimpleNamespace(page=raw)


def make_mock_page(
    url: str = "https://example.com",
    *,
    with_context: bool = True,
    with_evaluate: bool = True,
    with_locator: bool = True,
    with_self_page: bool = True,
) -> MagicMock:
    page = MagicMock()
    page.url = url
    if with_self_page:
        page.page = page
    if with_evaluate:
        page.evaluate = AsyncMock(return_value={})
    if with_context:
        page.context = MagicMock()
        page.context.grant_permissions = AsyncMock()
    if with_locator:
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="<span>hello</span>")
        locator.input_value = AsyncMock(return_value="test-value")
        page.locator = MagicMock(return_value=locator)
    return page


def make_probe_locator(
    *,
    count: int = 1,
    visible: bool = True,
    enabled: bool = True,
    is_password: bool = False,
    side_effect: Exception | None = None,
) -> MagicMock:
    locator = MagicMock()
    locator.first = locator
    locator.evaluate = AsyncMock(side_effect=side_effect, return_value=is_password)
    locator.count = AsyncMock(side_effect=side_effect, return_value=count)
    locator.is_visible = AsyncMock(side_effect=side_effect, return_value=visible)
    locator.is_enabled = AsyncMock(side_effect=side_effect, return_value=enabled)
    return locator


def make_skyvern_page(page: MagicMock) -> MagicMock:
    wrapper = MagicMock(spec=SkyvernPage)
    wrapper.page = page
    wrapper.url = page.url
    if "evaluate" in page.__dict__:
        wrapper.evaluate = page.evaluate
    if "locator" in page.__dict__:
        wrapper.locator = page.locator
    if "context" in page.__dict__:
        wrapper.context = page.context
    wrapper.locator_scope = wrapper
    return wrapper


def make_select_option_page(*, locator_scope: Any | None = None) -> tuple[SimpleNamespace, AsyncMock]:
    native_select_option = AsyncMock(return_value="selected")
    raw_page = MagicMock()
    # The credential-safety boundary probe reads whether the target is a password input.
    probe = MagicMock()
    probe.first = probe
    probe.evaluate = AsyncMock(return_value=False)
    probe.select_option = AsyncMock(return_value="selected")
    raw_page.locator = MagicMock(return_value=probe)
    page = SimpleNamespace(page=raw_page, select_option=native_select_option, locator_scope=raw_page)
    if locator_scope is not None:
        if isinstance(locator_scope, MagicMock):
            locator_scope.locator = MagicMock(return_value=probe)
        page.locator_scope = locator_scope
    return page, native_select_option


def make_select_like_page(target: dict[str, Any]) -> tuple[MagicMock, MagicMock]:
    control = MagicMock()
    control.first = control
    control.evaluate = AsyncMock(return_value=target)
    control.click = AsyncMock()
    control.fill = AsyncMock()
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=[])
    page.locator = MagicMock(return_value=control)
    return page, control


def patch_get_page(monkeypatch: pytest.MonkeyPatch, module: Any, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    mock = AsyncMock(return_value=(make_skyvern_page(page), ctx))
    monkeypatch.setattr(module, "get_page", mock)
    return mock


class HangingDriverCall:
    """Driver entry point that answers only once the caller releases it."""

    def __init__(self, *, hangs: bool = True) -> None:
        self.hangs = hangs
        self.calls = 0
        self.released = asyncio.Event()

    async def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        if self.hangs:
            await self.released.wait()


def make_hanging_page(driver_call: Callable[..., Awaitable[object]], *, url: str = HANGING_PAGE_URL) -> SimpleNamespace:
    """A page whose every driver entry point routes to ``driver_call``."""
    locator = MagicMock()
    locator.first = locator
    locator.count = AsyncMock(return_value=1)
    locator.evaluate = driver_call
    locator.press = driver_call
    locator.select_option = driver_call
    locator.scroll_into_view_if_needed = driver_call
    raw_page = MagicMock()
    raw_page.url = url
    raw_page.locator = MagicMock(return_value=locator)
    raw_page.screenshot = driver_call
    raw_page.evaluate = driver_call
    return SimpleNamespace(
        page=raw_page,
        url=url,
        locator_scope=raw_page,
        locator=MagicMock(return_value=locator),
        is_closed=lambda: False,
        _working_frame=None,
        goto=driver_call,
        wait_for_load_state=AsyncMock(),
        title=AsyncMock(return_value="Metrics"),
        click=driver_call,
        fill=driver_call,
        type=driver_call,
        select_option=driver_call,
        screenshot=driver_call,
        evaluate=driver_call,
        keyboard=SimpleNamespace(press=driver_call),
    )


def patch_hanging_driver(
    monkeypatch: pytest.MonkeyPatch,
    page: SimpleNamespace,
    driver_call: Callable[..., Awaitable[object]],
    *,
    action_timeout_ms: int = HANGING_BOUND_MS,
    headroom_ms: int = HANGING_HEADROOM_MS,
) -> None:
    """Route the MCP browser tools at ``page`` under a budget small enough for real loop time."""
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, BrowserContext(mode="local"))))
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None, context=None))
    monkeypatch.setattr(mcp_browser, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(browser_ops, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(mcp_browser, "select_native_option_if_targeted", AsyncMock(return_value=None))
    for name in ("do_screenshot", "do_frame_list", "do_frame_switch", "do_select_option", "strategy_aware_input"):
        monkeypatch.setattr(mcp_browser, name, driver_call)
    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", action_timeout_ms)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", headroom_ms)


def make_tab_page(
    *,
    bring_to_front: Callable[..., Awaitable[object]] | None = None,
    close: Callable[..., Awaitable[object]] | None = None,
    url: str = HANGING_PAGE_URL,
) -> SimpleNamespace:
    """A raw page shaped for the tab tools; an unnamed driver entry point answers immediately."""
    return SimpleNamespace(
        url=url,
        title=AsyncMock(return_value="Metrics"),
        is_closed=lambda: False,
        bring_to_front=bring_to_front or AsyncMock(),
        close=close or AsyncMock(),
    )


def patch_tab_session(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[SimpleNamespace],
    *,
    new_page: Callable[..., Awaitable[object]] | None = None,
    action_timeout_ms: int = HANGING_BOUND_MS,
    headroom_ms: int = HANGING_HEADROOM_MS,
) -> SimpleNamespace:
    """Route the tab tools at ``pages`` under a budget small enough for real loop time."""
    browser = SimpleNamespace(
        _browser_context=SimpleNamespace(pages=pages, new_page=new_page or AsyncMock(return_value=pages[0]))
    )
    state = SimpleNamespace(
        browser=browser,
        _active_page=None,
        _implicit_page=None,
        _working_frame=None,
        selection_lost=False,
        tab_state_persists=True,
        _page_events=deque(maxlen=8),
        _hooked_page_ids=set(),
        _hooked_handlers_map={},
    )
    monkeypatch.setattr(mcp_tabs, "get_current_session", lambda: state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, BrowserContext(mode="local"))))
    monkeypatch.setattr(mcp_tabs, "ensure_browser_hooks", lambda _browser: None)
    monkeypatch.setattr(mcp_tabs, "clear_session_ref_map", lambda **_kwargs: None)
    monkeypatch.setattr(mcp_tabs, "DEFAULT_ACTION_TIMEOUT_MS", action_timeout_ms)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", headroom_ms)
    return state
