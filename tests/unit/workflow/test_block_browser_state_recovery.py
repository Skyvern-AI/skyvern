"""Liveness validation + reconnect of a reused browser state whose driver was already stopped."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.real_browser_state import RealBrowserState


def _has_playwright_browser() -> bool:
    """Check that Playwright's chromium binary exists for the current installed version."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


def _make_code_block() -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="code_output",
        description="test output",
        output_parameter_id="op_code",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="code_1", code="value = 'ok'", output_parameter=output_parameter)


class _FakeWorkflowRun:
    workflow_run_id = "wr_test"
    organization_id = "o_test"
    workflow_permanent_id = "wpid_test"
    proxy_location = None
    extra_http_headers: dict[str, str] | None = None
    cdp_connect_headers: dict[str, str] | None = None
    browser_address = "ws://remote-browser"
    browser_profile_id = None
    parent_workflow_run_id = None


@pytest.mark.asyncio
async def test_reused_persistent_session_is_reconnected_when_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()

    fake_state = MagicMock()
    fake_state.is_connected = MagicMock(return_value=False)
    fake_state.reconnect = AsyncMock(return_value=None)

    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", AsyncMock(return_value=fake_state), raising=False
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "get_browser_address_if_ready",
        AsyncMock(return_value="ws://session-browser"),
        raising=False,
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=_FakeWorkflowRun()))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is fake_state
    fake_state.reconnect.assert_awaited_once()
    # The session's own remote browser is the reconnect target, not the run's pooled address.
    assert fake_state.reconnect.await_args.kwargs["browser_address"] == "ws://session-browser"
    # CDP handshake headers must survive the rebuild or remote browsers needing them fail to reattach.
    assert "cdp_connect_headers" in fake_state.reconnect.await_args.kwargs


@pytest.mark.asyncio
async def test_disconnected_session_without_resolvable_address_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()

    fake_state = MagicMock()
    fake_state.is_connected = MagicMock(return_value=False)
    fake_state.reconnect = AsyncMock(return_value=None)

    # The run still carries a (pooled) address, but a session-backed block must never reconnect
    # to it — only the session's own browser is correct, so an unresolved session address is fatal.
    class _PooledAddressRun(_FakeWorkflowRun):
        browser_address = "ws://pooled-wrong-browser"

    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", AsyncMock(return_value=fake_state), raising=False
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "get_browser_address_if_ready",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=_PooledAddressRun()))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is None
    fake_state.reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_connected_reused_session_is_not_reconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()

    fake_state = MagicMock()
    fake_state.is_connected = MagicMock(return_value=True)
    fake_state.reconnect = AsyncMock(return_value=None)

    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", AsyncMock(return_value=fake_state), raising=False
    )
    get_run = AsyncMock(return_value=_FakeWorkflowRun())
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", get_run)

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is fake_state
    fake_state.reconnect.assert_not_awaited()
    # A healthy reused state never needs the workflow run looked up for a rebuild.
    get_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()

    fake_state = MagicMock()
    fake_state.is_connected = MagicMock(return_value=False)
    fake_state.reconnect = AsyncMock(side_effect=RuntimeError("driver gone"))

    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", AsyncMock(return_value=fake_state), raising=False
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "get_browser_address_if_ready",
        AsyncMock(return_value="ws://session-browser"),
        raising=False,
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=_FakeWorkflowRun()))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is None


def _state_with_context(context: object | None) -> RealBrowserState:
    return RealBrowserState(pw=MagicMock(), browser_context=context)  # type: ignore[arg-type]


def test_is_connected_false_when_no_context() -> None:
    assert _state_with_context(None).is_connected() is False


def test_is_connected_true_when_browser_connected() -> None:
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=True)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    assert _state_with_context(context).is_connected() is True


def test_is_connected_false_when_browser_disconnected() -> None:
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=False)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    assert _state_with_context(context).is_connected() is False


def test_is_connected_false_when_context_close_was_called() -> None:
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=True)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(_close_was_called=True, _closed=False)
    assert _state_with_context(context).is_connected() is False


def test_is_connected_false_when_driver_connection_closed() -> None:
    # A bare pw.stop() leaves browser.is_connected() True and _close_was_called False, but the
    # shared driver Connection records a closed-error — that is the only reliable dead-driver signal.
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=True)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(
        _close_was_called=False,
        _closed=False,
        _connection=MagicMock(_closed_error=RuntimeError("Target page, context or browser has been closed")),
    )
    assert _state_with_context(context).is_connected() is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@_skip_no_browser
@pytest.mark.asyncio
async def test_is_connected_false_after_real_driver_stop(tmp_path: Path) -> None:
    # The real reused-dead-session repro: connect_over_cdp, then a bare pw.stop() with no graceful
    # context.close(). browser.is_connected() stays True, so the probe must fall through to the
    # driver Connection's closed-error to report the dead state and trigger a reconnect.
    launcher = await async_playwright().start()
    chromium_path = launcher.chromium.executable_path
    await launcher.stop()

    port = _free_port()
    proc = subprocess.Popen(
        [
            chromium_path,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url: str | None = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version") as resp:
                    ws_url = json.loads(resp.read())["webSocketDebuggerUrl"]
                break
            except Exception:
                await asyncio.sleep(0.1)
        assert ws_url is not None, "chromium CDP endpoint never came up"

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        state = RealBrowserState(pw=pw, browser_context=context)

        assert state.is_connected() is True

        await pw.stop()

        assert state.is_connected() is False
    finally:
        proc.kill()


@pytest.mark.asyncio
async def test_reconnect_starts_fresh_driver_and_stops_stale_one(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(return_value=None)
    fresh_pw = MagicMock()

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    check_and_fix = AsyncMock(return_value=None)
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")

    assert state.pw is fresh_pw
    stale_pw.stop.assert_awaited_once()
    assert check_and_fix.await_args.kwargs["browser_address"] == "ws://remote-browser"


@pytest.mark.asyncio
async def test_reconnect_stops_fresh_driver_when_state_rebuild_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(return_value=None)
    fresh_pw = MagicMock()
    fresh_pw.stop = AsyncMock(return_value=None)

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock(side_effect=RuntimeError("cdp handshake failed")))

    with pytest.raises(RuntimeError, match="cdp handshake failed"):
        await state.reconnect(browser_address="ws://remote-browser")

    # A failed rebuild must stop both drivers so it never orphans the freshly started one.
    fresh_pw.stop.assert_awaited_once()
    stale_pw.stop.assert_awaited_once()
