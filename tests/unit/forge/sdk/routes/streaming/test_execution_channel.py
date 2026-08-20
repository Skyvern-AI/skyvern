"""Regression tests for the ExecutionChannel driver lifecycle (SKY-12524)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError

from skyvern.forge.sdk.routes.streaming.channels.execution import ExecutionChannel, execution_channel
from tests.unit.forge.sdk.routes.streaming.test_exfiltration_channel import (
    _FakePw,
    _make_vnc_channel,
    _patch_pw_stack,
)


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

    async with execution_channel(_make_vnc_channel()):
        pass
    await _drain_loop()

    assert state.start_calls == 1
    assert state.pws[0].stopped is True


@pytest.mark.asyncio
async def test_stop_releases_driver_when_browser_close_raises() -> None:
    channel = ExecutionChannel(vnc_channel=_make_vnc_channel())
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
