"""Regression tests for the ExecutionChannel driver lifecycle (SKY-12524)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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
