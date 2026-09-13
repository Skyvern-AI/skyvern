"""Liveness validation + reconnect of a reused browser state whose driver was already stopped."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from playwright.async_api import async_playwright

from skyvern.exceptions import (
    BrowserStateDiagnostic,
    MissingBrowserState,
    MissingBrowserStatePage,
    get_user_facing_exception_message,
)
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye import real_browser_state as real_browser_state_module
from skyvern.webeye.browser_artifacts import BrowserArtifacts
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
    workflow_run = _FakeWorkflowRun()
    recovered_state = MagicMock()
    recovered_state.reconnect = AsyncMock()
    attach = AsyncMock(return_value=recovered_state)

    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", attach)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=workflow_run))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is recovered_state
    attach.assert_awaited_once_with(
        workflow_run=workflow_run,
        url=None,
        browser_session_id="pbs_1",
        browser_profile_id=None,
        browser_session_runnable_id=None,
        browser_session_runnable_generation_id=None,
    )
    # Recovery belongs to the browser manager. The block must not run a second reconnect.
    recovered_state.reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnected_session_without_resolvable_address_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()
    workflow_run = _FakeWorkflowRun()
    attach = AsyncMock(return_value=None)

    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", attach)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=workflow_run))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is None
    attach.assert_awaited_once_with(
        workflow_run=workflow_run,
        url=None,
        browser_session_id="pbs_1",
        browser_profile_id=None,
        browser_session_runnable_id=None,
        browser_session_runnable_generation_id=None,
    )


@pytest.mark.asyncio
async def test_connected_reused_session_is_not_reconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()
    workflow_run = _FakeWorkflowRun()
    connected_state = MagicMock()
    connected_state.reconnect = AsyncMock()
    attach = AsyncMock(return_value=connected_state)
    get_run = AsyncMock(return_value=workflow_run)

    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", attach)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", get_run)

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is connected_state
    connected_state.reconnect.assert_not_awaited()
    get_run.assert_awaited_once_with(workflow_run_id="wr_test", organization_id="o_test")
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()
    workflow_run = _FakeWorkflowRun()
    attach = AsyncMock(return_value=None)

    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", attach)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=workflow_run))

    result = await block.get_or_create_browser_state(
        workflow_run_id="wr_test", organization_id="o_test", browser_session_id="pbs_1"
    )

    assert result is None
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_reconnect_preserves_initial_route_policy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()
    workflow_run = _FakeWorkflowRun()
    browser_state = MagicMock()
    browser_state.is_connected.return_value = False
    browser_state.browser_context_route_policy_url = "https://accounts.example.test/login"
    browser_state.reconnect = AsyncMock()

    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=browser_state))
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_run", AsyncMock(return_value=workflow_run))

    result = await block.get_or_create_browser_state(workflow_run_id="wr_test", organization_id="o_test")

    assert result is browser_state
    browser_state.reconnect.assert_awaited_once_with(
        proxy_location=None,
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        browser_context_route_policy_url="https://accounts.example.test/login",
        organization_id="o_test",
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address="ws://remote-browser",
        browser_profile_id=None,
        browser_session_id=None,
    )


@pytest.mark.asyncio
async def test_code_block_preserves_missing_browser_state_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    exception = MissingBrowserState(workflow_run_id="wr_test", failure_reason="reconnect_failed:RuntimeError")
    monkeypatch.setattr(CodeBlock, "_execute", AsyncMock(side_effect=exception))

    with pytest.raises(MissingBrowserState) as exc_info:
        await _make_code_block().execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test")

    assert "reconnect_failed:RuntimeError" in str(exc_info.value)


def test_missing_browser_state_user_message_hides_disconnect_diagnostic() -> None:
    detected_at = datetime.now(timezone.utc)
    exception = MissingBrowserState(
        workflow_run_id="wr_test",
        diagnostic=BrowserStateDiagnostic(
            reason="browser_context_disconnected",
            disconnect_observed_at=detected_at - timedelta(seconds=2),
            browser_session_id="pbs_test",
        ),
        detected_at=detected_at,
    )

    assert "browser_session_id=pbs_test" in str(exception)
    assert "browser_session_id=pbs_test" not in get_user_facing_exception_message(exception)


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


def test_disconnected_browser_state_latches_diagnostic() -> None:
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=False)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    state = RealBrowserState(
        pw=MagicMock(),
        browser_context=context,
        browser_artifacts=BrowserArtifacts(remote_browser_session_id="pbs_test"),
    )

    assert state.is_connected() is False
    diagnostic = state.get_browser_state_diagnostic()
    assert diagnostic is not None
    assert diagnostic.reason == "browser_context_disconnected"
    assert diagnostic.browser_session_id == "pbs_test"
    assert diagnostic.observation_source == "liveness_probe"

    assert state.is_connected() is False
    assert state.get_browser_state_diagnostic() is diagnostic


@pytest.mark.asyncio
async def test_missing_page_includes_disconnect_timestamp_and_gap() -> None:
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=False)
    context = MagicMock()
    context.browser = browser
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    state = RealBrowserState(
        pw=MagicMock(),
        browser_context=context,
        browser_artifacts=BrowserArtifacts(remote_browser_session_id="pbs_test"),
    )
    await state.set_working_page(MagicMock())
    state.list_valid_pages = AsyncMock(return_value=[])

    with pytest.raises(MissingBrowserStatePage) as exc_info:
        await state.must_get_working_page()

    message = str(exc_info.value)
    assert "browser_context_disconnected" in message
    assert "browser_session_id=pbs_test" in message
    assert "disconnect_observed_at=" in message
    assert "detected_at=" in message
    assert "observation_gap_seconds=" in message
    assert "observation_source=liveness_probe" in message
    assert "browser_session_id=pbs_test" not in get_user_facing_exception_message(exc_info.value)


def test_stale_browser_disconnect_event_does_not_latch_replacement_state() -> None:
    old_browser = MagicMock()
    old_context = MagicMock(browser=old_browser)
    new_browser = MagicMock()
    new_context = MagicMock(browser=new_browser)
    state = RealBrowserState(pw=MagicMock(), browser_context=old_context)

    state.browser_context = new_context
    state._register_disconnect_listeners(new_context)
    state._on_browser_context_closed(old_context)
    state._on_browser_disconnected(old_browser)

    assert state.get_browser_state_diagnostic() is None


def test_browser_disconnect_event_latches_event_observation() -> None:
    browser = MagicMock()
    context = MagicMock()
    context.browser = browser
    state = RealBrowserState(
        pw=MagicMock(),
        browser_context=context,
        browser_artifacts=BrowserArtifacts(remote_browser_session_id="pbs_test"),
    )

    context_close_handler = context.on.call_args_list[0].args[1]
    browser_disconnect_handler = browser.on.call_args_list[0].args[1]
    assert context.on.call_args_list[0].args[0] == "close"
    assert browser.on.call_args_list[0].args[0] == "disconnected"
    assert context_close_handler == state._on_browser_context_closed
    browser_disconnect_handler(browser)

    diagnostic = state.get_browser_state_diagnostic()
    assert diagnostic is not None
    assert diagnostic.reason == "browser_disconnected_event"
    assert diagnostic.event == "browser_disconnected"
    assert diagnostic.observation_source == "browser_event"


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


def test_is_connected_true_when_context_browser_is_none() -> None:
    # A CDP-connected context can expose ``browser is None``; is_connected() then reports True from
    # cached impl flags alone, with no transport round-trip. This passive True is exactly why the
    # page-less inheritance seam actively probes the transport before same-context recovery
    # (RealBrowserManager._inherited_browser_transport_alive) rather than trusting is_connected()
    # (SKY-13389).
    context = MagicMock()
    context.browser = None
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    assert _state_with_context(context).is_connected() is True


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
async def test_reconnect_restores_replacement_before_stopping_stale_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    fresh_pw = MagicMock()

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            events.append("start fresh driver")
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild and replay scripts"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")

    assert state.pw is fresh_pw
    assert events == ["start fresh driver", "rebuild and replay scripts", "stop stale driver"]
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
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock(side_effect=RuntimeError("cdp handshake failed")))

    with pytest.raises(RuntimeError, match="cdp handshake failed"):
        await state.reconnect(browser_address="ws://remote-browser")

    # The failed replacement is stopped while the still-guarded stale driver remains attached.
    fresh_pw.stop.assert_awaited_once()
    stale_pw.stop.assert_not_awaited()
    assert state.pw is stale_pw


@pytest.mark.asyncio
async def test_reconnect_cancellation_restores_still_guarded_stale_state(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(return_value=None)
    fresh_pw = MagicMock()
    fresh_pw.stop = AsyncMock(return_value=None)

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    stale_context = MagicMock()
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    stale_listener_browser = MagicMock()
    state._disconnect_listener_browser = stale_listener_browser
    registration = object()
    state.add_sessionless_init_script_registration(registration)

    async def cancel_after_replacement_context_was_published(**_kwargs: object) -> None:
        replacement_context = MagicMock()
        state.browser_context = replacement_context
        state._disconnect_listener_browser = MagicMock()
        state._on_browser_context_closed(replacement_context)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        state,
        "check_and_fix_state",
        AsyncMock(side_effect=cancel_after_replacement_context_was_published),
    )

    with pytest.raises(asyncio.CancelledError):
        await state.reconnect(browser_address="ws://remote-browser")

    assert state.pw is stale_pw
    assert state.browser_context is stale_context
    assert state._disconnect_listener_browser is stale_listener_browser
    assert state.sessionless_init_script_registrations == (registration,)
    fresh_pw.stop.assert_awaited_once()
    stale_pw.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_pre_handoff_reconnect_preserves_sessionless_registrations_for_next_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_pw = MagicMock()
    first_replacement_pw = MagicMock()
    second_replacement_pw = MagicMock()
    start_fresh = AsyncMock(side_effect=[first_replacement_pw, second_replacement_pw])

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    initial_context = MagicMock()
    replacement_contexts = [MagicMock(), MagicMock()]
    state = RealBrowserState(pw=initial_pw, browser_context=initial_context)
    state._connection_status = MagicMock(return_value=(True, None))
    registration = object()
    state.add_sessionless_init_script_registration(registration)
    replayed_registrations: list[tuple[object, ...]] = []

    async def stop_current_driver() -> None:
        current_context = state.browser_context
        assert current_context is not None
        state._on_browser_context_closed(current_context)

    initial_pw.stop = AsyncMock(side_effect=stop_current_driver)
    first_replacement_pw.stop = AsyncMock(side_effect=stop_current_driver)

    async def rebuild(**kwargs: object) -> None:
        replayed_registrations.append(kwargs["sessionless_init_script_registrations"])  # type: ignore[arg-type]
        state.browser_context = replacement_contexts.pop(0)

    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock(side_effect=rebuild))

    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)

    assert replayed_registrations == [(registration,), (registration,)]
    assert state.sessionless_init_script_registrations == (registration,)


@pytest.mark.asyncio
async def test_reconnect_restores_stale_state_before_cancelled_replacement_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(return_value=None)
    fresh_pw = MagicMock()
    shutdown_started = asyncio.Event()

    async def wait_during_replacement_shutdown() -> None:
        shutdown_started.set()
        await asyncio.Event().wait()

    fresh_pw.stop = AsyncMock(side_effect=wait_during_replacement_shutdown)

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    stale_context = MagicMock()
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    replacement_context = MagicMock()

    async def fail_after_replacement_context_was_published(**_kwargs: object) -> None:
        state.browser_context = replacement_context
        raise RuntimeError("replacement setup failed")

    monkeypatch.setattr(
        state,
        "check_and_fix_state",
        AsyncMock(side_effect=fail_after_replacement_context_was_published),
    )

    reconnect = asyncio.create_task(state.reconnect(browser_address="ws://remote-browser"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)
    reconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reconnect

    assert state.pw is stale_pw
    assert state.browser_context is stale_context
    fresh_pw.stop.assert_awaited_once()
    stale_pw.stop.assert_not_awaited()
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_bounds_fresh_driver_shutdown_when_state_rebuild_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_started = asyncio.Event()

    async def hang_during_stop() -> None:
        stop_started.set()
        await asyncio.Event().wait()

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(return_value=None)
    fresh_pw = MagicMock()
    fresh_pw.stop = AsyncMock(side_effect=hang_during_stop)

    class _FakeAsyncPlaywright:
        async def start(self) -> object:
            return fresh_pw

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0.01)

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock(side_effect=RuntimeError("cdp handshake failed")))

    with pytest.raises(RuntimeError, match="cdp handshake failed"):
        await asyncio.wait_for(state.reconnect(browser_address="ws://remote-browser"), timeout=0.1)

    assert stop_started.is_set()
    fresh_pw.stop.assert_awaited_once()
    stale_pw.stop.assert_not_awaited()
    assert state.pw is stale_pw


@pytest.mark.asyncio
async def test_reconnect_recovers_when_stale_driver_shutdown_has_already_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=RuntimeError("stale driver still attached"))
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    check_and_fix = AsyncMock()
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")
    await asyncio.sleep(0)
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
    await asyncio.sleep(0)

    assert stale_pw.stop.await_count == 2
    start_fresh.assert_awaited_once()
    check_and_fix.assert_awaited_once()
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_refuses_to_overlap_driver_when_stale_connection_may_still_be_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock()
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    check_and_fix = AsyncMock()
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    with pytest.raises(RuntimeError, match="may still be live"):
        await state.reconnect(browser_address="ws://remote-browser")

    stale_pw.stop.assert_not_awaited()
    start_fresh.assert_not_awaited()
    check_and_fix.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_reason", ["browser_context_missing", "browser_context_closed"])
async def test_reconnect_stops_known_unusable_context_before_starting_replacement(
    monkeypatch: pytest.MonkeyPatch,
    disconnect_reason: str,
) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    start_fresh = AsyncMock(side_effect=lambda: events.append("start fresh driver"))

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, disconnect_reason))
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")

    assert events == ["stop stale driver", "start fresh driver", "rebuild"]


@pytest.mark.asyncio
async def test_reconnect_stops_driver_after_connection_probe_failure_before_starting_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    start_fresh = AsyncMock(side_effect=lambda: events.append("start fresh driver"))

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    stale_context = MagicMock()
    stale_context._impl_obj = None
    stale_context.browser.is_connected.side_effect = RuntimeError("connection probe failed")
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")

    assert events == ["stop stale driver", "start fresh driver", "rebuild"]


@pytest.mark.asyncio
async def test_reconnect_keeps_guard_through_connection_probe_failure_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    start_fresh = AsyncMock(side_effect=lambda: events.append("start fresh driver"))

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))

    stale_context = MagicMock()
    stale_context._impl_obj = None
    stale_context.browser.is_connected.side_effect = RuntimeError("connection probe failed")
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild and restore guard"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser")

    assert events == ["start fresh driver", "rebuild and restore guard", "stop stale driver"]


@pytest.mark.asyncio
async def test_reconnect_stops_explicitly_unusable_live_context_before_starting_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    start_fresh = AsyncMock(side_effect=lambda: events.append("start fresh driver"))

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)

    assert events == ["stop stale driver", "start fresh driver", "rebuild"]


@pytest.mark.asyncio
async def test_reconnect_replaces_guarded_live_context_before_stopping_stale_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=lambda: events.append("stop stale driver"))
    start_fresh = AsyncMock(side_effect=lambda: events.append("start fresh driver"))

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    check_and_fix = AsyncMock(side_effect=lambda **_: events.append("rebuild and replay scripts"))
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)

    assert events == ["start fresh driver", "rebuild and replay scripts", "stop stale driver"]


@pytest.mark.asyncio
async def test_reconnect_recovers_when_unusable_live_driver_stop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=[RuntimeError("driver still attached"), None])
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    check_and_fix = AsyncMock()
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)

    assert stale_pw.stop.await_count == 2
    start_fresh.assert_awaited_once()
    check_and_fix.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_aborts_when_unguarded_unusable_driver_shutdown_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang_during_stop() -> None:
        state._on_browser_context_closed(stale_context)
        await asyncio.Event().wait()

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=hang_during_stop)
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    stale_context = MagicMock()
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    state._connection_status = MagicMock(return_value=(True, None))
    registration = object()
    state.add_sessionless_init_script_registration(registration)
    check_and_fix = AsyncMock()
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    with pytest.raises(RuntimeError, match="Failed to stop unusable stale Playwright driver"):
        await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)

    start_fresh.assert_not_awaited()
    check_and_fix.assert_not_awaited()
    assert state.sessionless_init_script_registrations == (registration,)


@pytest.mark.asyncio
async def test_reconnect_cancellation_during_pre_handoff_shutdown_restores_sessionless_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def stop_after_context_close() -> None:
        stop_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_stop.wait()
            state._on_browser_context_closed(stale_context)

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=stop_after_context_close)
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))

    stale_context = MagicMock()
    state = RealBrowserState(pw=stale_pw, browser_context=stale_context)
    state._connection_status = MagicMock(return_value=(True, None))
    registration = object()
    state.add_sessionless_init_script_registration(registration)
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())

    reconnect = asyncio.create_task(
        state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    )
    await asyncio.wait_for(stop_started.wait(), timeout=1)
    reconnect.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reconnect

    start_fresh.assert_not_awaited()
    assert state.sessionless_init_script_registrations == (registration,)

    release_stop.set()
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
    assert state.sessionless_init_script_registrations == (registration,)


@pytest.mark.asyncio
async def test_reconnect_bounds_stale_driver_shutdown_after_guarded_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_started = asyncio.Event()

    async def hang_during_stop() -> None:
        stop_started.set()
        await asyncio.Event().wait()

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=hang_during_stop)
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0.01)

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    check_and_fix = AsyncMock()
    monkeypatch.setattr(state, "check_and_fix_state", check_and_fix)

    await asyncio.wait_for(state.reconnect(browser_address="ws://remote-browser"), timeout=0.1)
    await asyncio.sleep(0)
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)

    assert stop_started.is_set()
    assert stale_pw.stop.await_count == 2
    start_fresh.assert_awaited_once()
    check_and_fix.assert_awaited_once()
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_does_not_retry_until_cancellation_resistant_stale_shutdown_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_stop = asyncio.Event()

    async def cancellation_resistant_stop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_stop.wait()

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=cancellation_resistant_stop)
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0.01)

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())

    await asyncio.wait_for(state.reconnect(browser_address="ws://remote-browser"), timeout=0.1)
    await asyncio.sleep(0.02)

    # The first stop remains owned without racing a second stop.
    assert stale_pw.stop.await_count == 1
    assert len(state._detached_teardown_tasks) == 1

    release_stop.set()
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_retries_after_cancellation_resistant_stale_shutdown_eventually_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_stop = asyncio.Event()
    stop_attempts = 0

    async def cancellation_resistant_stop() -> None:
        nonlocal stop_attempts
        stop_attempts += 1
        if stop_attempts != 1:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_stop.wait()
            raise RuntimeError("late shutdown failure")

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=cancellation_resistant_stop)
    start_fresh = AsyncMock()

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0.01)

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(False, "playwright_driver_connection_closed"))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())

    await asyncio.wait_for(state.reconnect(browser_address="ws://remote-browser"), timeout=0.1)
    await asyncio.sleep(0.02)
    assert stale_pw.stop.await_count == 1

    release_stop.set()

    async def wait_for_retry() -> None:
        while stale_pw.stop.await_count < 2:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_retry(), timeout=0.1)
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
    await asyncio.sleep(0)

    assert stale_pw.stop.await_count == 2
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_retries_post_replacement_stale_driver_stop_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=[RuntimeError("driver still attached"), None])
    fresh_pw = MagicMock()
    start_fresh = AsyncMock(return_value=fresh_pw)

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())

    await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    await asyncio.gather(*list(state._detached_teardown_tasks))
    await asyncio.sleep(0)

    assert state.pw is fresh_pw
    assert stale_pw.stop.await_count == 2
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_cancellation_during_post_replacement_shutdown_retains_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_stop_started = asyncio.Event()
    stop_attempts = 0

    async def cancellation_sensitive_stop() -> None:
        nonlocal stop_attempts
        stop_attempts += 1
        if stop_attempts == 1:
            first_stop_started.set()
            await asyncio.Event().wait()

    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock(side_effect=cancellation_sensitive_stop)
    fresh_pw = MagicMock()
    start_fresh = AsyncMock(return_value=fresh_pw)

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())

    reconnect = asyncio.create_task(
        state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    )
    await asyncio.wait_for(first_stop_started.wait(), timeout=1)
    reconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reconnect
    await asyncio.sleep(0)
    await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
    await asyncio.sleep(0)

    assert state.pw is fresh_pw
    assert stale_pw.stop.await_count == 2
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_reconnect_cancellation_before_stale_shutdown_starts_wakes_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pw = MagicMock()
    stale_pw.stop = AsyncMock()
    fresh_pw = MagicMock()
    start_fresh = AsyncMock(return_value=fresh_pw)

    class _FakeAsyncPlaywright:
        start = start_fresh

    monkeypatch.setattr("skyvern.webeye.real_browser_state.async_playwright", lambda: _FakeAsyncPlaywright())
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))

    state = RealBrowserState(pw=stale_pw, browser_context=MagicMock())
    state._connection_status = MagicMock(return_value=(True, None))
    monkeypatch.setattr(state, "check_and_fix_state", AsyncMock())
    bounded_calls = 0

    async def cancel_first_phase_before_start(
        phase: object,
        _timeout: float,
        _description: str,
        *,
        accept_failure_as_completion: bool = False,
    ) -> bool:
        nonlocal bounded_calls
        del accept_failure_as_completion
        bounded_calls += 1
        if bounded_calls == 1:
            phase_task = asyncio.ensure_future(phase)  # type: ignore[arg-type]
            phase_task.cancel()
            await asyncio.gather(phase_task, return_exceptions=True)
            raise asyncio.CancelledError
        await phase  # type: ignore[misc]
        return True

    monkeypatch.setattr(state, "_run_bounded_detachable", cancel_first_phase_before_start)

    with pytest.raises(asyncio.CancelledError):
        await state.reconnect(browser_address="ws://remote-browser", stale_context_is_unusable=True)
    detached = list(state._detached_teardown_tasks)
    done, pending = await asyncio.wait(detached, timeout=0.1)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)

    assert state.pw is fresh_pw
    assert len(done) == len(detached)
    stale_pw.stop.assert_awaited_once()
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
async def test_requested_close_logs_disconnect_at_info_not_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # The end-of-run teardown closes the context on purpose; observing that close is diagnostic
    # context, not a warning. A close nobody requested still warns.
    log = MagicMock()
    monkeypatch.setattr(real_browser_state_module, "LOG", log)
    monkeypatch.setattr(real_browser_state_module, "disable_download_interceptor_for_context", AsyncMock())

    async def _skip_phase(coro, timeout, description):  # type: ignore[no-untyped-def]
        coro.close()
        return True

    context = MagicMock(browser=MagicMock())
    state = RealBrowserState(pw=MagicMock(), browser_context=context)
    monkeypatch.setattr(state, "_run_bounded_detachable", _skip_phase)
    monkeypatch.setattr(state, "_run_browser_cleanup_bounded", AsyncMock())
    monkeypatch.setattr(state, "_stop_driver_bounded", AsyncMock())

    await state.close()
    state._on_browser_context_closed(context)

    log.warning.assert_not_called()
    assert [c for c in log.info.call_args_list if c.args[0] == "Browser state disconnected"]
    assert state.get_browser_state_diagnostic() is not None

    unrequested = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(browser=MagicMock()))
    unrequested._on_browser_context_closed(unrequested.browser_context)

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "Browser state disconnected"


@pytest.mark.asyncio
async def test_keep_alive_close_does_not_downgrade_a_later_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    # close(close_browser_on_completion=False) keeps the browser for reuse, so a disconnect that
    # follows it is nobody's request and must still warn.
    log = MagicMock()
    monkeypatch.setattr(real_browser_state_module, "LOG", log)

    context = MagicMock(browser=MagicMock())
    state = RealBrowserState(pw=MagicMock(), browser_context=context)
    monkeypatch.setattr(state, "_stop_driver_bounded", AsyncMock())

    await state.close(close_browser_on_completion=False, release_driver=False)
    state._on_browser_context_closed(context)

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "Browser state disconnected"


def _crashable_page() -> MagicMock:
    page = MagicMock()
    page.close = AsyncMock()
    return page


async def _never_returns() -> None:
    await asyncio.sleep(3600)


def test_crash_listener_registers_once_for_pre_existing_and_later_pages() -> None:
    existing = _crashable_page()
    context = MagicMock(pages=[existing])
    state = RealBrowserState(pw=MagicMock(), browser_context=context)

    assert existing.on.call_args_list == [call("crash", state._on_page_crashed)]
    assert call("page", state._watch_page_for_crash) in context.on.call_args_list

    later = _crashable_page()
    state._watch_page_for_crash(later)
    state._watch_page_for_crash(later)
    state._register_disconnect_listeners(context)

    assert existing.on.call_count == 1
    assert later.on.call_count == 1


def test_a_context_attached_from_outside_the_class_is_armed_for_crash_reaping() -> None:
    existing = _crashable_page()
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(pages=[]))
    replacement = MagicMock(pages=[existing])

    state.browser_context = replacement

    assert existing.on.call_args_list == [call("crash", state._on_page_crashed)]


@pytest.mark.asyncio
async def test_a_crashed_page_is_excluded_before_its_close_lands() -> None:
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/crashed"
    crashed.close = AsyncMock(side_effect=_never_returns)
    survivor = _crashable_page()
    survivor.url = "https://example.invalid/alive"
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(pages=[crashed, survivor]))

    state._on_page_crashed(crashed)

    try:
        assert await state.list_valid_pages() == [survivor]
        crashed.close.assert_not_awaited()
    finally:
        for task in list(state._detached_teardown_tasks):
            task.cancel()


@pytest.mark.asyncio
async def test_a_close_that_swallows_cancellation_still_exhausts_its_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation-resistant close must not read as success: asyncio.timeout only cancels, so a
    close that swallows the cancel would leave the dead target blocking every later attach."""
    monkeypatch.setattr(real_browser_state_module, "BROWSER_PAGE_CLOSE_TIMEOUT", 0.05)
    page = _crashable_page()

    async def _swallow_cancellation() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    page.close = AsyncMock(side_effect=_swallow_cancellation)
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(pages=[page]))

    await state._close_crashed_page(page)

    assert page.close.await_count == real_browser_state_module.CRASHED_PAGE_CLOSE_ATTEMPTS


@pytest.mark.asyncio
async def test_a_timed_out_crashed_page_close_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(real_browser_state_module, "BROWSER_PAGE_CLOSE_TIMEOUT", 0.05)
    page = _crashable_page()
    attempts: list[int] = []

    async def _hang_once_then_succeed() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            await _never_returns()

    page.close = AsyncMock(side_effect=_hang_once_then_succeed)
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(pages=[page]))

    await state._close_crashed_page(page)

    assert page.close.await_count == 2


@pytest.mark.asyncio
async def test_crashed_page_close_is_bounded_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(real_browser_state_module, "BROWSER_PAGE_CLOSE_TIMEOUT", 0.05)
    crashed = _crashable_page()
    stubborn = _crashable_page()
    stubborn.close = AsyncMock(side_effect=RuntimeError("close refused"))
    hanging = _crashable_page()
    hanging.close = AsyncMock(side_effect=_never_returns)
    context = MagicMock(pages=[crashed, stubborn, hanging])
    state = RealBrowserState(pw=MagicMock(), browser_context=context)

    state._on_page_crashed(crashed)
    state._on_page_crashed(stubborn)
    state._on_page_crashed(hanging)
    async with asyncio.timeout(10):
        await asyncio.gather(*list(state._detached_teardown_tasks))

    crashed.close.assert_awaited_once()
    stubborn.close.assert_awaited_once()
    assert state._detached_teardown_tasks == set()
