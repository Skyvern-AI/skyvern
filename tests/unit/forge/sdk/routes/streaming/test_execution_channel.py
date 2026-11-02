"""Regression tests for the ExecutionChannel driver lifecycle (SKY-12524)."""

from __future__ import annotations

import asyncio
import typing as t
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError

from skyvern.forge.sdk.routes.streaming.channels.execution import ExecutionChannel, execution_channel


def _make_context() -> MagicMock:
    browser_session = MagicMock()
    browser_session.browser_address = "http://localhost:9222"
    browser_session.persistent_browser_session_id = "pbs_123"

    context = MagicMock()
    context.organization_id = "org_123"
    context.x_api_key = "api-key-123"
    context.browser_session = browser_session
    context.identity = {
        "client_id": "client-1",
        "browser_session_id": browser_session.persistent_browser_session_id,
    }

    return context


class _FakeCdpSession:
    def __init__(self) -> None:
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> None:
        return None

    async def detach(self) -> None:
        self.detached = True


class _FakePwPage:
    def __init__(self, context: _FakeContext) -> None:
        self.url = "https://example.com"
        self.context = context

    async def add_init_script(self, *args: object, **kwargs: object) -> None:
        return None

    async def evaluate(self, *args: object, **kwargs: object) -> None:
        return None

    def on(self, *args: object, **kwargs: object) -> None:
        return None

    def remove_listener(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePwPage] = [_FakePwPage(self)]

    async def new_cdp_session(self, page: object) -> _FakeCdpSession:
        return _FakeCdpSession()


class _FakePwBrowser:
    """A stand-in for a Playwright Browser obtained via connect_over_cdp."""

    def __init__(self, *, fire_disconnect_on_close: bool) -> None:
        self._connected = True
        self._fire_disconnect_on_close = fire_disconnect_on_close
        self.handlers: dict[str, t.Callable[[], None]] = {}
        self.context = _FakeContext()
        self.contexts = [self.context]
        self.close_calls = 0

    def is_connected(self) -> bool:
        return self._connected

    def on(self, event: str, callback: t.Callable[[], None]) -> None:
        self.handlers[event] = callback

    async def new_browser_cdp_session(self) -> _FakeCdpSession:
        return _FakeCdpSession()

    async def new_context(self) -> _FakeContext:
        return self.context

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False
        # Real playwright fires "disconnected" when a connected browser is closed;
        # this reproduces that so the reconnect-guard can be exercised end-to-end.
        if self._fire_disconnect_on_close:
            handler = self.handlers.get("disconnected")
            if handler is not None:
                handler()


class _FakePw:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakePwManager:
    def __init__(self, on_start: t.Callable[[], _FakePw]) -> None:
        self._on_start = on_start

    async def start(self) -> _FakePw:
        return self._on_start()


def _patch_pw_stack(monkeypatch: pytest.MonkeyPatch, *, fire_disconnect_on_close: bool = False) -> SimpleNamespace:
    """Replace the Playwright driver + CDP connect helper with counting fakes.

    `start_calls` counts local Playwright driver spawns, so a reconnect (which starts
    a fresh driver) is observable without launching a real Node subprocess.
    """
    import skyvern.forge.sdk.routes.streaming.channels.cdp as cdp_mod

    state = SimpleNamespace(start_calls=0, pws=[], browsers=[], connections=[])

    def _make_pw() -> _FakePw:
        state.start_calls += 1
        pw = _FakePw()
        state.pws.append(pw)
        return pw

    def _fake_async_playwright() -> _FakePwManager:
        return _FakePwManager(_make_pw)

    async def _fake_connect(pw: object, url: str, headers: dict | None = None, **kwargs: object) -> _FakePwBrowser:
        browser = _FakePwBrowser(fire_disconnect_on_close=fire_disconnect_on_close)
        state.browsers.append(browser)
        state.connections.append((url, headers))
        return browser

    monkeypatch.setattr(cdp_mod, "async_playwright", _fake_async_playwright)
    monkeypatch.setattr(cdp_mod, "connect_over_cdp_with_diagnostics", _fake_connect)
    return state


async def _drain_loop() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class _RaisingBrowser:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        raise TargetClosedError("target already dead")


@pytest.mark.asyncio
async def test_execution_channel_cm_does_not_resurrect_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    # Playwright fires "disconnected" during an intentional browser.close(); the
    # on_close handler in CdpChannel.connect must not chain a fresh driver spawn.
    state = _patch_pw_stack(monkeypatch, fire_disconnect_on_close=True)

    async with execution_channel(_make_context()):
        pass
    await _drain_loop()

    assert state.start_calls == 1
    assert state.pws[0].stopped is True


@pytest.mark.asyncio
async def test_stop_releases_driver_when_browser_close_raises() -> None:
    channel = ExecutionChannel(context=_make_context())
    browser = _RaisingBrowser()
    pw = _FakePw()
    channel.browser = browser  # type: ignore[assignment]
    channel.pw = pw  # type: ignore[assignment]

    await channel.stop()

    assert browser.close_calls == 1
    assert pw.stopped is True
    assert channel.browser is None
    assert channel.pw is None
    assert channel._closing is True


@pytest.mark.asyncio
async def test_local_execution_channel_stop_is_safe() -> None:
    from skyvern.forge.sdk.routes.streaming.channels.execution import LocalExecutionChannel

    page = MagicMock()
    page.context = MagicMock()
    channel = LocalExecutionChannel(page=page)

    await channel.stop()

    assert channel._closing is True


def _fake_message_channel(browser_session_id: str, client_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        client_id=client_id,
        organization_id="org_exec",
        browser_session=SimpleNamespace(persistent_browser_session_id=browser_session_id),
    )


@pytest.mark.asyncio
async def test_execution_for_message_channel_releases_adopted_browser_state_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-dev path (no VncChannel registered for the client) adopts a
    browser_session-scoped browser_state via wait_for_browser_state, whose docstring hands
    ownership to the caller. execution_for_message_channel must give it back on every exit or
    the adopted Playwright driver + CDP websocket leaks once per JS-exec/message operation,
    not just once per connection."""
    from skyvern.forge import app as forge_app
    from skyvern.forge.sdk.routes.streaming import screencast
    from skyvern.forge.sdk.routes.streaming.channels import execution as execution_module

    browser_session_id = "pbs_exec_leak_check"
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=MagicMock()))
    message_channel = _fake_message_channel(browser_session_id, "client_exec_leak_check")

    monkeypatch.setattr(execution_module, "get_vnc_channel", lambda client_id: None)
    monkeypatch.setattr(
        forge_app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(status="running"))),
    )
    monkeypatch.setattr(screencast, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    release_browser_state = AsyncMock()
    monkeypatch.setattr(screencast, "release_browser_state", release_browser_state)

    async with execution_module.execution_for_message_channel(message_channel):
        pass

    release_browser_state.assert_awaited_once_with(browser_state, "browser_session", browser_session_id)


@pytest.mark.asyncio
async def test_execution_for_message_channel_releases_on_no_working_page_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership must also be given back when the context manager body never runs at all -
    e.g. the adopted browser has no working page yet, which raises before the caller's
    `async with` block gets control."""
    from skyvern.forge import app as forge_app
    from skyvern.forge.sdk.routes.streaming import screencast
    from skyvern.forge.sdk.routes.streaming.channels import execution as execution_module

    browser_session_id = "pbs_exec_leak_check_exc"
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    message_channel = _fake_message_channel(browser_session_id, "client_exec_leak_check_exc")

    monkeypatch.setattr(execution_module, "get_vnc_channel", lambda client_id: None)
    monkeypatch.setattr(
        forge_app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(status="running"))),
    )
    monkeypatch.setattr(screencast, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    release_browser_state = AsyncMock()
    monkeypatch.setattr(screencast, "release_browser_state", release_browser_state)

    with pytest.raises(RuntimeError, match="no working page"):
        async with execution_module.execution_for_message_channel(message_channel):
            pass

    release_browser_state.assert_awaited_once_with(browser_state, "browser_session", browser_session_id)
