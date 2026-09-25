"""Liveness validation + reconnect of a reused browser state whose driver was already stopped."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import urllib.request
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call

import pytest
from playwright.async_api import async_playwright
from structlog.testing import capture_logs

from skyvern.exceptions import (
    BrowserStateDiagnostic,
    MissingBrowserState,
    MissingBrowserStatePage,
    get_user_facing_exception_message,
)
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye import real_browser_state as real_browser_state_module
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_runtime_events import BrowserRuntimeLogContext
from skyvern.webeye.driver_connection import close_driver_connection_on_transport_loss
from skyvern.webeye.real_browser_state import RealBrowserState, expect_process_driver_teardown
from tests.unit.forge_log_capture import capture_runtime_logs


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
@pytest.mark.parametrize("operation", ["bare_stop", "detach", "transport_loss", "released_then_killed"])
async def test_is_connected_false_after_real_driver_stop(tmp_path: Path, operation: str) -> None:
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
            "--use-mock-keychain",
            "--password-store=basic",
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
        close_driver_connection_on_transport_loss(pw)
        browser = await pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        state = RealBrowserState(pw=pw, browser_context=context)
        state.bind_runtime_event_context(
            BrowserRuntimeLogContext(workflow_run_id=f"wr_{operation}", browser_session_id="pbs_smoke")
        )

        assert state.is_connected() is True

        try:
            with capture_logs() as logs:
                if operation in {"transport_loss", "released_then_killed"}:
                    if operation == "released_then_killed":
                        # A persistent session's run released it, then the worker kills every driver.
                        state.mark_run_released()
                        expect_process_driver_teardown()
                    transport = pw._impl_obj._connection._transport
                    transport._proc.kill()
                    with pytest.raises(Exception, match="Connection closed while reading from the driver"):
                        await asyncio.wait_for(asyncio.shield(transport.on_error_future), timeout=5)
                    await asyncio.sleep(0)
                elif operation == "detach":
                    await state.detach_remote_driver()
                else:
                    await pw.stop()
                assert state.is_connected() is False
            events = [
                entry
                for entry in logs
                if entry.get("browser_runtime_event") == "runtime_ended"
                and entry["workflow_run_id"] == f"wr_{operation}"
            ]
            assert len(events) == 1
            assert (
                events[0]["disconnect_kind"]
                == {
                    "bare_stop": "connection_unusable",
                    "detach": "intentional_teardown",
                    "transport_loss": "driver_transport_loss",
                    "released_then_killed": "intentional_teardown",
                }[operation]
            )
            assert events[0]["expected"] is (operation in {"detach", "released_then_killed"})
            assert events[0]["run_phase"] == ("after_release" if operation == "released_then_killed" else "active")
            if operation == "released_then_killed":
                assert events[0]["reason"] == "driver_release"
                assert events[0]["observation_source"] == "driver_event"
        finally:
            await asyncio.wait_for(pw.stop(), timeout=5)
    finally:
        proc.kill()
        proc.wait(timeout=5)


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
    retry_started = asyncio.Event()

    async def hang_during_stop() -> None:
        if stop_started.is_set():
            retry_started.set()
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
    await asyncio.wait_for(retry_started.wait(), timeout=0.1)
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

    context = _crashable_context()
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


def _crashable_context(*pages: MagicMock) -> MagicMock:
    """A connected context: a renderer crash leaves the browser itself alive."""
    context = MagicMock(pages=list(pages))
    context.browser.is_connected = MagicMock(return_value=True)
    context._impl_obj = MagicMock(_close_was_called=False, _closed=False, _connection=MagicMock(_closed_error=None))
    context.new_page = AsyncMock()
    return context


def _replacement_page_opener(context: MagicMock, replacement: MagicMock, order: list[str]) -> AsyncMock:
    """new_page as Playwright behaves: the new tab joins context.pages before the call returns."""

    async def _open() -> MagicMock:
        order.append("new_page")
        context.pages.append(replacement)
        return replacement

    return AsyncMock(side_effect=_open)


def _transport_observed_state() -> tuple[RealBrowserState, MagicMock, asyncio.Future[None]]:
    context = _crashable_context()
    future = asyncio.get_running_loop().create_future()
    connection = SimpleNamespace(_closed_error=None, _transport=SimpleNamespace(on_error_future=future))
    context._impl_obj._connection = connection
    driver = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection), stop=AsyncMock())
    return RealBrowserState(pw=driver, browser_context=context), context, future


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("transport_already_lost", [False, True])
async def test_registered_disconnect_survives_raising_browser_property(
    monkeypatch: pytest.MonkeyPatch, deferred: bool, transport_already_lost: bool
) -> None:
    owner = SkyvernContext(workflow_run_id="workflow-owner", browser_session_id="session-owner")
    with skyvern_context.scoped(owner):
        state, context, future = _transport_observed_state()
    state._runtime_events_deferred = deferred
    browser = context.browser
    callback = next(item.args[1] for item in browser.on.call_args_list if item.args[0] == "disconnected")
    monkeypatch.setattr(
        type(context),
        "browser",
        PropertyMock(side_effect=RuntimeError("https://private.invalid/?token=private")),
        raising=False,
    )
    delivered = asyncio.Event()
    future.add_done_callback(lambda _: delivered.set())
    if transport_already_lost:
        future.set_exception(RuntimeError("private transport detail"))
    unrelated = SkyvernContext(workflow_run_id="unrelated-owner")
    with skyvern_context.scoped(unrelated), capture_runtime_logs() as logs:
        callback(browser)
        callback(browser)
        state._on_browser_context_closed(context)
        if not transport_already_lost:
            future.set_exception(RuntimeError("later transport failure"))
        await delivered.wait()
        if deferred:
            assert state.get_browser_state_diagnostic() is None
            state.record_browser_acquisition("attach")
        diagnostic = state.get_browser_state_diagnostic()
        assert diagnostic is not None
        assert diagnostic.reason == "browser_disconnected_event"
        callback(browser)
        state._on_browser_context_closed(context)
        assert state.get_browser_state_diagnostic() is diagnostic
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    event = ended[0]
    assert event["disconnect_kind"] == ("driver_transport_loss" if transport_already_lost else "browser_disconnected")
    assert event["disconnect_evidence"] == ("transport_error_future" if transport_already_lost else "browser_event")
    assert event["observation_source"] == "browser_event"
    assert event["workflow_run_id"] == "workflow-owner" and event["browser_session_id"] == "session-owner"
    assert event["expected"] is False
    assert "private" not in str(event) and "unrelated" not in str(event)
    persisted = [entry for entry in owner.log if entry.get("browser_runtime_event") == "runtime_ended"]
    assert persisted == [{key: value for key, value in event.items() if key != "log_level"}]
    assert unrelated.log == []


@pytest.mark.parametrize("registered_replacement", [False, True])
def test_raising_browser_property_cannot_attribute_stale_callback_to_replacement(
    monkeypatch: pytest.MonkeyPatch, registered_replacement: bool
) -> None:
    old = _crashable_context()
    state = RealBrowserState(pw=MagicMock(), browser_context=old)
    old_browser = old.browser
    old_callback = next(item.args[1] for item in old_browser.on.call_args_list if item.args[0] == "disconnected")
    new = _crashable_context()
    browser = new.browser
    state.browser_context = new
    state.bind_runtime_event_context(BrowserRuntimeLogContext(task_id="new-owner"))
    if registered_replacement:
        state._register_disconnect_listeners(new)
    with capture_runtime_logs() as logs:
        with monkeypatch.context() as patcher:
            patcher.setattr(type(new), "browser", PropertyMock(side_effect=RuntimeError("unavailable")), raising=False)
            old_callback(old_browser)
            state._on_browser_context_closed(old)
            assert state.get_browser_state_diagnostic() is None
            assert logs == []
        state._register_disconnect_listeners(new)
        callback = next(item.args[1] for item in browser.on.call_args_list if item.args[0] == "disconnected")
        monkeypatch.setattr(type(new), "browser", PropertyMock(side_effect=RuntimeError("unavailable")), raising=False)
        callback(browser)
        old_callback(old_browser)
        callback(browser)
    assert len(logs) == 1
    assert logs[0]["browser_runtime_event"] == "runtime_ended"
    assert logs[0]["disconnect_kind"] == "browser_disconnected"
    assert logs[0]["task_id"] == "new-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_signal", ["context", "browser"])
@pytest.mark.parametrize("later_transport_loss", [True, False])
async def test_runtime_disconnect_deferred_first_signal_keeps_original_evidence(
    monkeypatch: pytest.MonkeyPatch, first_signal: str, later_transport_loss: bool
) -> None:
    state, context, future = _transport_observed_state()
    state._runtime_events_deferred = True
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)
    with capture_logs() as logs:
        if first_signal == "context":
            state._on_browser_context_closed(context)
        else:
            state._on_browser_disconnected(context.browser)
        clock.now.return_value = first_observed_at + timedelta(seconds=1)
        state._on_browser_context_closed(context)
        state._on_browser_disconnected(context.browser)
        if later_transport_loss:
            future.set_exception(RuntimeError("later transport loss"))
        assert state.get_browser_state_diagnostic() is None
        assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        state.record_browser_acquisition("attach")
        diagnostic = state.get_browser_state_diagnostic()
        assert diagnostic is not None
        state._on_browser_context_closed(context)
        state._on_browser_disconnected(context.browser)
        assert state.get_browser_state_diagnostic() is diagnostic
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["disconnect_kind"] == ("context_closed" if first_signal == "context" else "browser_disconnected")
    assert ended[0]["disconnect_evidence"] == ("context_event" if first_signal == "context" else "browser_event")
    assert ended[0]["observation_source"] == "browser_event"
    assert ended[0]["disconnect_observed_at"] == first_observed_at.isoformat()
    assert diagnostic.disconnect_observed_at == first_observed_at


@pytest.mark.asyncio
async def test_runtime_disconnect_deferred_signal_cannot_retire_replacement() -> None:
    state, old_context, _ = _transport_observed_state()
    replacement, context, _ = _transport_observed_state()
    state._runtime_events_deferred = True
    with capture_logs() as logs:
        state._on_browser_context_closed(old_context)
        state.pw = replacement.pw
        state.browser_context = context
        state._register_disconnect_listeners(context)
        state.record_browser_acquisition("attach")
        assert state.get_browser_state_diagnostic() is None
        assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        state._on_browser_disconnected(context.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["disconnect_kind"] == "browser_disconnected"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["workflow", "task", "session"])
@pytest.mark.parametrize(
    ("first", "kind", "evidence", "source"),
    [
        ("driver", "driver_transport_loss", "transport_error_future", "driver_event"),
        ("browser", "browser_disconnected", "browser_event", "browser_event"),
        ("context", "context_closed", "context_event", "browser_event"),
        ("probe", "connection_unusable", "liveness_state", "liveness_probe"),
    ],
)
async def test_runtime_disconnect_first_observation_and_attribution(
    owner: str, first: str, kind: str, evidence: str, source: str
) -> None:
    state, context, future = _transport_observed_state()
    state.bind_runtime_event_context(
        BrowserRuntimeLogContext(
            workflow_run_id="workflow-owner" if owner == "workflow" else None,
            task_id="task-owner" if owner == "task" else None,
            browser_session_id="session-owner",
        )
    )
    state.browser_artifacts = BrowserArtifacts(remote_browser_session_id="vendor-only")
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="unrelated-owner")), capture_logs() as logs:
        if first == "browser":
            state._on_browser_disconnected(context.browser)
        elif first == "context":
            state._on_browser_context_closed(context)
        elif first == "probe":
            context._impl_obj._connection._closed_error = RuntimeError("closed by stop")
            assert not state.is_connected()
        future.set_exception(RuntimeError("ws://secret.invalid/?token=private-value"))
        await asyncio.sleep(0)
        original = state.get_browser_state_diagnostic()
        state._on_browser_disconnected(context.browser)
        state._on_browser_context_closed(context)
        context.browser.is_connected.return_value = False
        assert not state.is_connected()
        assert state.get_browser_state_diagnostic() is original
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    event = events[0]
    assert (event["disconnect_kind"], event["disconnect_evidence"], event["observation_source"]) == (
        kind,
        evidence,
        source,
    )
    assert event["expected"] is False
    assert event["workflow_run_id"] == ("workflow-owner" if owner == "workflow" else None)
    assert event["task_id"] == ("task-owner" if owner == "task" else None)
    assert event["browser_session_id"] == "session-owner"
    assert not event.get("run_id")
    assert event["remote_browser_session_id"] == "vendor-only"
    assert "private-value" not in str(event)
    assert all("vendor-only" not in str(value) for key, value in event.items() if key != "remote_browser_session_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["closed", "close_called", "disconnected", "transport", "cancelled", "result"])
async def test_runtime_disconnect_liveness_requires_transport_evidence(signal: str) -> None:
    state, context, future = _transport_observed_state()
    kind = "connection_unusable"
    evidence = "liveness_state"
    if signal in {"closed", "cancelled", "result", "transport"}:
        context._impl_obj._connection._closed_error = RuntimeError("connection closed")
    elif signal == "close_called":
        context._impl_obj._close_was_called = True
        kind = "context_closed"
    else:
        context.browser.is_connected.return_value = False
        kind = "browser_disconnected"
    if signal == "transport":
        future.set_exception(RuntimeError("driver read failed"))
        kind, evidence = "driver_transport_loss", "transport_error_future"
    elif signal == "cancelled":
        future.cancel()
    elif signal == "result":
        future.set_result(None)
    with capture_logs() as logs:
        assert not state.is_connected()
        await asyncio.sleep(0)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert (events[0]["disconnect_kind"], events[0]["disconnect_evidence"]) == (kind, evidence)
    assert events[0]["observation_source"] == "liveness_probe"


@pytest.mark.asyncio
async def test_runtime_disconnect_driver_without_browser_and_stale_context() -> None:
    state, old_context, old_future = _transport_observed_state()
    replacement, context, future = _transport_observed_state()
    replacement.browser_context = None
    context.browser = None
    state.pw = replacement.pw
    state.browser_context = context
    state._register_disconnect_listeners(context)
    state._register_disconnect_listeners(context)
    with capture_logs() as logs:
        old_future.set_exception(RuntimeError("old driver"))
        state._on_browser_context_closed(old_context)
        state._on_browser_disconnected(old_context.browser)
        await asyncio.sleep(0)
        assert state.get_browser_state_diagnostic() is None
        future.set_exception(RuntimeError("current driver"))
        await asyncio.sleep(0)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert state.get_browser_state_diagnostic().reason == "playwright_driver_transport_lost"
    assert all(entry["disconnect_kind"] == "driver_transport_loss" for entry in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_setup", [False, True])
async def test_runtime_disconnect_successful_reconnect_resets_driver_observer(
    monkeypatch: pytest.MonkeyPatch, during_setup: bool
) -> None:
    state, old_context, old_future = _transport_observed_state()
    replacement, new_context, new_future = _transport_observed_state()
    replacement.browser_context = None
    page = _crashable_page()
    page.url = "about:blank"
    page.context = new_context
    new_context.pages = [page]
    old_context._impl_obj._connection._closed_error = RuntimeError("old closed connection")
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new_context, BrowserArtifacts(), None)),
    )
    if during_setup:

        async def working_page() -> MagicMock:
            if not new_future.done():
                new_future.set_exception(RuntimeError("driver lost while setting up"))
            await asyncio.sleep(0)
            return page

        state.get_working_page = working_page
    with capture_logs() as logs:
        assert not state.is_connected()
        await state.reconnect(browser_address="ws://example.invalid")
        old_future.set_exception(RuntimeError("stale driver callback"))
        state._on_browser_context_closed(old_context)
        state._on_browser_disconnected(old_context.browser)
        await asyncio.sleep(0)
        if not during_setup:
            assert state.get_browser_state_diagnostic() is None
            new_future.set_exception(RuntimeError("replacement driver callback"))
            await asyncio.sleep(0)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert [entry["disconnect_kind"] for entry in events] == ["connection_unusable", "driver_transport_loss"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("late_signal", ["driver", "browser"])
async def test_runtime_disconnect_failed_reconnect_restores_old_observer(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool, late_signal: str
) -> None:
    state, old_context, old_future = _transport_observed_state()
    replacement, new_context, new_future = _transport_observed_state()
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    error = asyncio.CancelledError("setup cancelled") if cancelled else RuntimeError("setup failed")

    async def fail_setup() -> None:
        new_future.set_exception(RuntimeError("unpublished driver failed"))
        await asyncio.sleep(0)
        raise error

    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new_context, BrowserArtifacts(), None)),
    )
    state.get_working_page = fail_setup
    # Only the state being reconnected should observe the replacement under construction.
    replacement.browser_context = None
    with capture_logs() as logs:
        with pytest.raises(type(error)) as raised:
            await state.reconnect(browser_address="ws://example.invalid", stale_context_is_unusable=True)
        assert raised.value is error
        assert state.browser_context is old_context
        assert state.get_browser_state_diagnostic() is None
        assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        if late_signal == "browser":
            browser = old_context.browser
            callback = next(item.args[1] for item in browser.on.call_args_list if item.args[0] == "disconnected")
            monkeypatch.setattr(
                type(old_context), "browser", PropertyMock(side_effect=RuntimeError("unavailable")), raising=False
            )
            callback(browser)
            callback(browser)
        old_future.set_exception(RuntimeError("old driver failed after rollback"))
        await asyncio.sleep(0)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert events[0]["disconnect_kind"] == (
        "driver_transport_loss" if late_signal == "driver" else "browser_disconnected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["driver_start", "context_setup"])
async def test_runtime_disconnect_failed_reconnect_keeps_pre_shutdown_observation(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    state, context, future = _transport_observed_state()
    observed = []

    async def stop_old_driver() -> None:
        state._on_browser_context_closed(context)
        observed.append(state.get_browser_state_diagnostic())

    state.pw.stop.side_effect = stop_old_driver
    failure = RuntimeError("replacement setup failed")
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(
            start=AsyncMock(
                return_value=MagicMock(stop=AsyncMock()),
                side_effect=failure if failure_stage == "driver_start" else None,
            )
        ),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory, "create_browser_context", AsyncMock(side_effect=failure)
    )
    with capture_logs() as logs:
        with pytest.raises(RuntimeError) as raised:
            await state.reconnect(browser_address="ws://example.invalid", stale_context_is_unusable=True)
        assert raised.value is failure
        future.set_exception(RuntimeError("old transport callback"))
        await asyncio.sleep(0)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert state.get_browser_state_diagnostic() is observed[0]
    assert events[0]["reason"] == "driver_replacement"


async def _reap_crashed_pages(state: RealBrowserState) -> None:
    async with asyncio.timeout(10):
        await asyncio.gather(*list(state._detached_teardown_tasks))


async def _never_returns() -> None:
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_browser_runtime_event_page_crash_preserves_recovery_and_identity() -> None:
    order: list[str] = []
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    crashed.close = AsyncMock(side_effect=lambda: order.append("close"))
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    crashed.context = context
    context.new_page = _replacement_page_opener(context, replacement, order)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="workflow-owner", browser_session_id="session-owner")):
        state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock(side_effect=lambda page, url: order.append("navigate"))
    handler = next(item.args[1] for item in crashed.on.call_args_list if item.args[0] == "crash")
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="other-workflow")), capture_logs() as logs:
        handler(crashed)
        handler(crashed)
        await _reap_crashed_pages(state)
    assert order == ["new_page", "close", "navigate"]
    assert await state.must_get_working_page() is replacement
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "page_crash"]
    assert len(events) == 1
    assert events[0]["workflow_run_id"] == "workflow-owner"
    assert events[0]["browser_session_id"] == "session-owner"
    assert events[0]["expected"] is False
    assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "recreate"])
@pytest.mark.parametrize("failure", ["raise", "timeout"])
@pytest.mark.parametrize("first_signal", ["context_close", "browser_disconnected"])
async def test_browser_runtime_event_failed_close_keeps_later_loss_unexpected(
    monkeypatch: pytest.MonkeyPatch, operation: str, failure: str, first_signal: str
) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    context.cookies = AsyncMock(return_value=[])
    close_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def fail_close() -> None:
        close_started.set()
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("context close failed")

    async def cleanup() -> None:
        cleanup_finished.set()

    context.close = fail_close
    state = RealBrowserState(pw=MagicMock(), browser_context=context, browser_cleanup=cleanup)
    if failure == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if operation == "recreate":
            assert await state.close_current_open_page() is False
        else:
            assert await state.close(release_driver=False) is False
            assert cleanup_finished.is_set()
        assert close_started.is_set()
        await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
        assert state.browser_context is context
        assert state.is_connected()
        if first_signal == "context_close":
            state._on_browser_context_closed(context)
        else:
            state._on_browser_disconnected(context.browser)
        state._on_browser_context_closed(context)
        state._on_browser_disconnected(context.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["expected"] is False
    assert ended[0]["reason"] == ("context_closed" if first_signal == "context_close" else "browser_disconnected")
    assert ended[0]["close_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result", ["raise", "timeout", "success"])
@pytest.mark.parametrize("first_signal", ["context_close", "browser_disconnected"])
@pytest.mark.parametrize("signal_timing", ["during_stop", "after_stop"])
async def test_browser_runtime_event_detach_intent_tracks_stop_completion(
    monkeypatch: pytest.MonkeyPatch, stop_result: str, first_signal: str, signal_timing: str
) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    pw = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=pw, browser_context=context)
    close_handler = next(item.args[1] for item in context.on.call_args_list if item.args[0] == "close")
    disconnect_handler = next(
        item.args[1] for item in context.browser.on.call_args_list if item.args[0] == "disconnected"
    )

    def signal_loss() -> None:
        context.browser.is_connected.return_value = False
        if first_signal == "context_close":
            close_handler(context)
        else:
            disconnect_handler(context.browser)

    async def stop() -> None:
        if signal_timing == "during_stop":
            signal_loss()
        if stop_result == "timeout":
            await asyncio.Event().wait()
        if stop_result == "raise":
            raise RuntimeError("driver stop failed")

    pw.stop.side_effect = stop
    if stop_result == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if stop_result == "success":
            await state.detach_remote_driver()
        else:
            with pytest.raises(TimeoutError if stop_result == "timeout" else RuntimeError):
                await state.detach_remote_driver()
        assert state._remote_driver_detached is (stop_result == "success")
        if signal_timing == "after_stop":
            assert state.is_connected()
            assert state.get_browser_state_diagnostic() is None
            signal_loss()
        close_handler(context)
        disconnect_handler(context.browser)
        assert state.is_connected() is False
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    expected = stop_result == "success" or signal_timing == "during_stop"
    assert ended[0]["expected"] is expected
    assert ended[0]["reason"] == (
        "deliberate_detach"
        if expected
        else "context_closed"
        if first_signal == "context_close"
        else "browser_disconnected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result", ["raise", "timeout", "cancel", "success"])
@pytest.mark.parametrize("first_signal", ["context_close", "browser_disconnected"])
@pytest.mark.parametrize("signal_timing", ["during_stop", "after_stop"])
async def test_browser_runtime_event_driver_release_intent_tracks_stop_completion(
    monkeypatch: pytest.MonkeyPatch, stop_result: str, first_signal: str, signal_timing: str
) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    pw = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=pw, browser_context=context)
    close_handler = next(item.args[1] for item in context.on.call_args_list if item.args[0] == "close")
    disconnect_handler = next(
        item.args[1] for item in context.browser.on.call_args_list if item.args[0] == "disconnected"
    )

    def signal_loss() -> None:
        context.browser.is_connected.return_value = False
        if first_signal == "context_close":
            close_handler(context)
        else:
            disconnect_handler(context.browser)

    async def stop() -> None:
        if signal_timing == "during_stop":
            signal_loss()
        if stop_result == "timeout":
            await asyncio.Event().wait()
        if stop_result == "raise":
            raise RuntimeError("driver stop failed")
        if stop_result == "cancel":
            raise asyncio.CancelledError

    pw.stop.side_effect = stop
    if stop_result == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if stop_result == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await state.close(False, True)
        else:
            assert await state.close(False, True) is False
        assert state.browser_context is context
        assert state.pw is pw
        if signal_timing == "after_stop":
            assert state.is_connected()
            assert state.get_browser_state_diagnostic() is None
            signal_loss()
        close_handler(context)
        disconnect_handler(context.browser)
        assert state.is_connected() is False
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    expected = stop_result == "success" or signal_timing == "during_stop"
    assert ended[0]["expected"] is expected
    assert ended[0]["reason"] == (
        "driver_release"
        if expected
        else "context_closed"
        if first_signal == "context_close"
        else "browser_disconnected"
    )
    assert ended[0]["close_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result", ["raise", "timeout", "cancel"])
async def test_browser_runtime_event_failed_driver_release_preserves_replacement_intent(
    monkeypatch: pytest.MonkeyPatch, stop_result: str
) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    replacement = _crashable_context()
    pw = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=pw, browser_context=context)

    async def stop() -> None:
        state.browser_context = replacement
        state._register_disconnect_listeners(replacement)
        state._expect_runtime_end("deliberate_detach", replacement)
        if stop_result == "timeout":
            await asyncio.Event().wait()
        if stop_result == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("old driver stop failed")

    pw.stop.side_effect = stop
    if stop_result == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if stop_result == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await state.close(False, True)
        else:
            assert await state.close(False, True) is False
        assert state._expected_runtime_ends[context] == "driver_release"
        state._on_browser_context_closed(context)
        state._on_browser_disconnected(context.browser)
        assert state.get_browser_state_diagnostic() is None
        state._on_browser_context_closed(replacement)
        state._on_browser_disconnected(replacement.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["expected"] is True
    assert ended[0]["reason"] == "deliberate_detach"


@pytest.mark.parametrize("session_id", [None, "session-owner"])
def test_browser_runtime_event_terminal_log_preserves_remote_session_correlator(session_id: str | None) -> None:
    context = _crashable_context()
    with skyvern_context.scoped(SkyvernContext(task_id="task-owner", browser_session_id=session_id)):
        state = RealBrowserState(
            pw=MagicMock(),
            browser_context=context,
            browser_artifacts=BrowserArtifacts(remote_browser_session_id="provider-session"),
        )
    with capture_logs() as logs:
        state._on_browser_disconnected(context.browser)
    ended = next(entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended")
    assert ended["remote_browser_session_id"] == "provider-session"
    assert ended["browser_session_id"] == session_id
    assert ended["task_id"] == "task-owner"
    assert not {"url", "browser_address", "cdp_url", "headers"}.intersection(ended)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "recreate"])
@pytest.mark.parametrize("failure", ["raise", "timeout"])
async def test_browser_runtime_event_failed_close_intent_is_not_proof_of_context_end(
    monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    context.cookies = AsyncMock(return_value=[])

    async def fail_after_close_intent() -> None:
        context._impl_obj._close_was_called = True
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("context close failed before completion")

    context.close = fail_after_close_intent
    state = RealBrowserState(pw=MagicMock(), browser_context=context)
    if failure == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if operation == "recreate":
            assert await state.close_current_open_page() is False
        else:
            assert await state.close(release_driver=False) is False
        await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
        assert state.browser_context is context
        assert context._impl_obj._closed is False
        assert context.browser.is_connected()
        state._on_browser_context_closed(context)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["expected"] is False
    assert ended[0]["reason"] == "context_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected", "reason"),
    [
        ("close", True, "normal_close"),
        ("close_failure_cleanup", True, "normal_close"),
        ("detach", True, "deliberate_detach"),
        ("release", True, "driver_release"),
        ("recreate", True, "context_recreation"),
        ("keep_open", False, "context_closed"),
        ("loss", False, "context_closed"),
    ],
)
async def test_browser_runtime_event_termination_intent_and_dedupe(operation: str, expected: bool, reason: str) -> None:
    context = _crashable_context()
    context._skyvern_cdp_download_interceptor = None
    context.cookies = AsyncMock(return_value=[])
    pw = MagicMock(stop=AsyncMock())
    with skyvern_context.scoped(SkyvernContext(task_id="task-owner")):
        state = RealBrowserState(pw=pw, browser_context=context, browser_artifacts=BrowserArtifacts())
    close_handler = next(item.args[1] for item in context.on.call_args_list if item.args[0] == "close")
    disconnect_handler = next(
        item.args[1] for item in context.browser.on.call_args_list if item.args[0] == "disconnected"
    )

    async def lose_connection() -> None:
        close_handler(context)
        disconnect_handler(context.browser)

    context.close = AsyncMock(side_effect=lose_connection)
    pw.stop.side_effect = lose_connection
    with capture_logs() as logs:
        if operation == "close_failure_cleanup":
            context.close.side_effect = RuntimeError("context close failed")
            state.browser_cleanup = lose_connection
            await state.close()
        elif operation == "close":
            await state.close()
        elif operation == "detach":
            await state.detach_remote_driver()
        elif operation == "release":
            await state.close(False, True)
        elif operation == "recreate":
            assert await state.close_current_open_page()
        elif operation == "keep_open":
            await state.close(False, False)
        await lose_connection()
        context.browser.is_connected.return_value = False
        state.is_connected()
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert events[0]["expected"] is expected
    assert events[0]["reason"] == reason
    assert events[0]["disconnect_kind"] == ("intentional_teardown" if expected else "context_closed")
    assert events[0]["disconnect_evidence"] == ("teardown_intent" if expected else "context_event")
    assert events[0]["task_id"] == "task-owner"
    assert state.get_browser_state_diagnostic().reason == "browser_context_close_event"
    if operation in {"detach", "release", "keep_open", "loss"}:
        context.close.assert_not_awaited()


def test_browser_runtime_event_replacement_rejects_stale_signals() -> None:
    old_context = _crashable_context()
    state = RealBrowserState(pw=MagicMock(), browser_context=old_context)
    new_context = _crashable_context()
    with capture_logs() as logs:
        state._on_browser_disconnected(old_context.browser)
        original = state.get_browser_state_diagnostic()
        state._on_browser_context_closed(old_context)
        assert state.get_browser_state_diagnostic() is original
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(events) == 1
    assert events[0]["reason"] == "browser_disconnected"
    state.browser_context = new_context
    state._register_disconnect_listeners(new_context)
    with capture_logs() as logs:
        state._on_browser_disconnected(old_context.browser)
        state._on_browser_context_closed(old_context)
    assert not [entry for entry in logs if entry.get("browser_runtime_event")]


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["browser_context_disconnected", "playwright_driver_connection_closed"])
@pytest.mark.parametrize("replacement", ["success", "driver_start_failure", "context_setup_failure"])
async def test_reconnect_observes_known_disconnect_before_replacement_without_callback(
    monkeypatch: pytest.MonkeyPatch, loss: str, replacement: str
) -> None:
    old = _crashable_context()
    page = _crashable_page()
    page.url = "about:blank"
    page.is_closed.return_value = False
    new = _crashable_context(page)
    old_pw = MagicMock(stop=AsyncMock())
    fresh_pw = MagicMock(stop=AsyncMock())
    state = RealBrowserState(
        pw=old_pw,
        browser_context=old,
        browser_artifacts=BrowserArtifacts(remote_browser_session_id="old-session"),
        runtime_event_context=BrowserRuntimeLogContext(task_id="old-owner", browser_session_id="old-session"),
    )
    if loss == "browser_context_disconnected":
        old.browser.is_connected.return_value = False
    else:
        old._impl_obj._connection._closed_error = RuntimeError("connection closed")
    assert state.get_browser_state_diagnostic() is None
    before_handoff: list[BrowserStateDiagnostic | None] = []

    async def start_driver() -> MagicMock:
        before_handoff.append(state.get_browser_state_diagnostic())
        if replacement == "driver_start_failure":
            raise RuntimeError("replacement failed")
        return fresh_pw

    monkeypatch.setattr(real_browser_state_module, "async_playwright", lambda: SimpleNamespace(start=start_driver))
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(
            return_value=(new, BrowserArtifacts(remote_browser_session_id="new-session"), None),
            side_effect=RuntimeError("replacement failed") if replacement == "context_setup_failure" else None,
        ),
    )
    with skyvern_context.scoped(SkyvernContext(task_id="unrelated-caller")), capture_logs() as logs:
        if replacement == "success":
            await state.reconnect(stale_context_is_unusable=True)
            assert state.pw is fresh_pw
            assert state.browser_context is new
            assert await state.get_working_page() is page
            assert state.get_browser_state_diagnostic() is None
        else:
            with pytest.raises(RuntimeError, match="replacement failed"):
                await state.reconnect(stale_context_is_unusable=True)
            assert state.pw is old_pw
            assert state.browser_context is old
            assert not state.is_connected()
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
        ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        assert len(ended) == 1
        assert ended[0]["task_id"] == "old-owner"
        assert ended[0]["browser_session_id"] == "old-session"
        assert ended[0]["remote_browser_session_id"] == "old-session"
        assert ended[0]["expected"] is False
        assert ended[0]["disconnect_reason"] == loss
        assert ended[0]["observation_source"] == "liveness_probe"
        diagnostic = before_handoff[0]
        assert diagnostic is not None
        assert ended[0]["disconnect_observed_at"] == diagnostic.disconnect_observed_at.isoformat()
        if replacement != "success":
            assert state.get_browser_state_diagnostic() is diagnostic
            return
        state.bind_runtime_event_context(
            BrowserRuntimeLogContext(task_id="new-owner", browser_session_id="new-session")
        )
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
        assert state.get_browser_state_diagnostic() is None
        new.browser.is_connected.return_value = False
        assert not state.is_connected()
        state._on_browser_context_closed(new)
        state._on_browser_disconnected(new.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert [entry["task_id"] for entry in ended] == ["old-owner", "new-owner"]
    assert [entry["remote_browser_session_id"] for entry in ended] == ["old-session", "new-session"]
    assert all(entry["expected"] is False for entry in ended)


@pytest.mark.asyncio
async def test_browser_runtime_event_deliberate_reconnect_resets_expected_end(monkeypatch: pytest.MonkeyPatch) -> None:
    old_context = _crashable_context()
    new_page = _crashable_page()
    new_page.url = "about:blank"
    new_context = _crashable_context(new_page)
    pw = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=pw, browser_context=old_context)
    pw.stop.side_effect = lambda: state._on_browser_context_closed(old_context)
    monkeypatch.setattr(state, "_connection_status", MagicMock(return_value=(True, None)))
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))
    monkeypatch.setattr(real_browser_state_module, "async_playwright", lambda: MagicMock(start=AsyncMock()))
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new_context, BrowserArtifacts(), None)),
    )
    with capture_logs() as logs:
        await state.reconnect(browser_address="ws://example.invalid", stale_context_is_unusable=True)
        assert state.browser_context is new_context
        assert state.get_browser_state_diagnostic() is None
        state._on_browser_context_closed(old_context)
        state._on_browser_disconnected(old_context.browser)
        state._on_browser_context_closed(new_context)
        state._on_browser_disconnected(new_context.browser)
    events = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert [(entry["expected"], entry["reason"]) for entry in events] == [
        (True, "driver_replacement"),
        (False, "context_closed"),
    ]
    assert [entry["disconnect_kind"] for entry in events] == ["intentional_teardown", "context_closed"]


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
async def test_closing_the_crashed_only_tab_opens_its_replacement_first() -> None:
    """A headed Linux Chromium exits with its last tab and takes the CDP endpoint with it, so the
    replacement must exist before the crashed only-tab closes, and it must be the existing recovery
    so the next caller resumes on the URL the crashed tab was on (SKY-16016)."""
    order: list[str] = []
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    crashed.close = AsyncMock(side_effect=lambda: order.append("close"))
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, order)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock(side_effect=lambda page, url: order.append(f"navigate:{url}"))

    state._on_page_crashed(crashed)
    await _reap_crashed_pages(state)

    assert order == ["new_page", "close", "navigate:https://example.invalid/form"]
    assert await state.must_get_working_page() is replacement


@pytest.mark.asyncio
async def test_a_stalled_url_restore_does_not_hold_the_crashed_target_open() -> None:
    """The restore navigation can retry for minutes against an unreachable URL; the dead target
    must already be closed by then, or every later CDP attach still lands on it."""
    order: list[str] = []
    closed = asyncio.Event()
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"

    async def _close() -> None:
        order.append("close")
        closed.set()

    crashed.close = AsyncMock(side_effect=_close)
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, order)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)

    async def _restore_stalls(**_: object) -> None:
        order.append("navigate")
        await _never_returns()

    state.navigate_to_url = AsyncMock(side_effect=_restore_stalls)

    state._on_page_crashed(crashed)

    try:
        await asyncio.wait_for(closed.wait(), timeout=1)
        # The event fires inside the close; the stalled navigate may not have started yet.
        assert order[:2] == ["new_page", "close"]
    finally:
        for task in list(state._detached_teardown_tasks):
            task.cancel()


@pytest.mark.asyncio
async def test_a_caller_that_wins_the_replacement_closes_the_crashed_target_before_its_restore() -> None:
    """The caller's own recovery can hold the reopen lock through a restore that retries for minutes;
    the crashed target must close as soon as that caller's replacement exists, not after the reaper
    finally gets the lock."""
    order: list[str] = []
    gate = asyncio.Event()
    closed = asyncio.Event()
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"

    async def _close() -> None:
        order.append("close")
        closed.set()

    crashed.close = AsyncMock(side_effect=_close)
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)

    async def _open_when_released() -> MagicMock:
        order.append("new_page")
        await gate.wait()
        context.pages.append(replacement)
        return replacement

    context.new_page = AsyncMock(side_effect=_open_when_released)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)

    async def _restore_stalls(**_: object) -> None:
        order.append("navigate")
        await _never_returns()

    state.navigate_to_url = AsyncMock(side_effect=_restore_stalls)

    # The crash mark lands synchronously at crash time; here the caller reacts before the reaper task
    # runs, so its recovery holds the lock inside new_page() when the reaper queues behind it.
    state._crashed_pages.add(crashed)
    caller = asyncio.create_task(state.must_get_working_page())
    await asyncio.sleep(0)
    state._on_page_crashed(crashed)
    gate.set()

    try:
        await asyncio.wait_for(closed.wait(), timeout=1)
        assert order[:2] == ["new_page", "close"]
        assert context.new_page.await_count == 1
    finally:
        caller.cancel()
        for task in list(state._detached_teardown_tasks):
            task.cancel()


@pytest.mark.asyncio
async def test_consumers_wait_for_the_recovered_page_instead_of_the_blank_replacement() -> None:
    """While a recovery is restoring the replacement's URL, every consumer waits for the finished
    page: none is handed about:blank to drive or to navigate away from, and get_or_create_page does
    not open a tab of its own and close the one being recovered."""
    order: list[str] = []
    restore_started = asyncio.Event()
    restore_gate = asyncio.Event()
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, order)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)

    async def _restore_when_released(**_: object) -> None:
        restore_started.set()
        await restore_gate.wait()

    state.navigate_to_url = AsyncMock(side_effect=_restore_when_released)

    state._on_page_crashed(crashed)
    await asyncio.wait_for(restore_started.wait(), timeout=1)
    consumers = [
        asyncio.create_task(state.get_working_page()),
        asyncio.create_task(state.must_get_working_page()),
        asyncio.create_task(state.get_or_create_page()),
    ]
    await asyncio.sleep(0)
    assert not any(consumer.done() for consumer in consumers)

    restore_gate.set()
    await _reap_crashed_pages(state)

    assert [await consumer for consumer in consumers] == [replacement, replacement, replacement]
    assert context.new_page.await_count == 1
    state.navigate_to_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_slow_crashed_target_close_does_not_expose_the_blank_replacement() -> None:
    """The gate closes before the replacement appears, so the crashed-target close that runs ahead of
    the restore cannot leak the blank tab to a consumer either."""
    close_started = asyncio.Event()
    close_gate = asyncio.Event()
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"

    async def _close_when_released() -> None:
        close_started.set()
        await close_gate.wait()

    crashed.close = AsyncMock(side_effect=_close_when_released)
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, [])
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock()

    state._on_page_crashed(crashed)
    await asyncio.wait_for(close_started.wait(), timeout=1)
    consumer = asyncio.create_task(state.get_working_page())
    await asyncio.sleep(0)
    assert not consumer.done()

    close_gate.set()
    await _reap_crashed_pages(state)

    assert await consumer is replacement


@pytest.mark.asyncio
async def test_a_cancelled_recovery_owner_leaves_the_crashed_target_for_the_reaper_to_close() -> None:
    """A caller whose recovery wins can be cancelled while it awaits a slow close; the target must not
    stay marked as closing, or the queued reaper skips it and nothing ever retries."""
    closed = asyncio.Event()
    attempts: list[int] = []

    async def _hang_then_close() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            await _never_returns()
        closed.set()

    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    crashed.close = AsyncMock(side_effect=_hang_then_close)
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, [])
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock()

    state._crashed_pages.add(crashed)
    owner = asyncio.create_task(state.must_get_working_page())
    for _ in range(3):
        await asyncio.sleep(0)
    assert attempts == [1]
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    state._on_page_crashed(crashed)
    try:
        await asyncio.wait_for(closed.wait(), timeout=1)
        assert crashed.close.await_count == 2
    finally:
        for task in list(state._detached_teardown_tasks):
            task.cancel()


@pytest.mark.asyncio
async def test_a_hung_new_page_fails_the_recovery_within_the_bound_and_releases_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every consumer waits on the recovery, so a CDP new_page() that never answers must fail it
    within the bound; the crashed tab is then left open rather than the browser losing its last tab."""
    monkeypatch.setattr(real_browser_state_module, "BROWSER_PAGE_CLOSE_TIMEOUT", 0.05)
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    context = _crashable_context(crashed)
    context.new_page = AsyncMock(side_effect=_never_returns)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)

    state._on_page_crashed(crashed)
    await asyncio.sleep(0)
    consumer = asyncio.create_task(state.get_working_page())

    try:
        assert await asyncio.wait_for(consumer, timeout=1) is None
        await _reap_crashed_pages(state)
        crashed.close.assert_not_awaited()
    finally:
        for task in list(state._detached_teardown_tasks):
            task.cancel()


@pytest.mark.asyncio
async def test_a_caller_that_recovered_first_still_gets_the_crashed_target_closed() -> None:
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)
    context.new_page = _replacement_page_opener(context, replacement, [])
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock()

    state._crashed_pages.add(crashed)
    assert await state.must_get_working_page() is replacement
    state._on_page_crashed(crashed)
    await _reap_crashed_pages(state)

    crashed.close.assert_awaited_once()
    assert context.new_page.await_count == 1
    state.navigate_to_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_caller_recovering_during_the_reap_gets_the_reaper_replacement() -> None:
    """The caller whose action just failed reaches for a page while the reaper is still opening
    one; it must wait for that page rather than open a second tab."""
    order: list[str] = []
    gate = asyncio.Event()
    crashed = _crashable_page()
    crashed.url = "https://example.invalid/form"
    replacement = _crashable_page()
    replacement.url = "about:blank"
    context = _crashable_context(crashed)

    async def _open_when_released() -> MagicMock:
        order.append("new_page")
        await gate.wait()
        context.pages.append(replacement)
        return replacement

    context.new_page = AsyncMock(side_effect=_open_when_released)
    state = RealBrowserState(pw=MagicMock(), browser_context=context, page=crashed)
    state.navigate_to_url = AsyncMock()

    state._on_page_crashed(crashed)
    caller = asyncio.create_task(state.must_get_working_page())
    await asyncio.sleep(0)
    gate.set()
    await _reap_crashed_pages(state)

    assert await caller is replacement
    assert context.new_page.await_count == 1


@pytest.mark.asyncio
async def test_a_crashed_tab_with_a_live_sibling_closes_without_opening_another() -> None:
    crashed = _crashable_page()
    survivor = _crashable_page()
    context = _crashable_context(crashed, survivor)
    state = RealBrowserState(pw=MagicMock(), browser_context=context)

    state._on_page_crashed(crashed)
    await _reap_crashed_pages(state)

    context.new_page.assert_not_awaited()
    crashed.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_crashed_only_tab_stays_open_when_no_replacement_can_be_opened() -> None:
    crashed = _crashable_page()
    context = _crashable_context(crashed)
    context.new_page = AsyncMock(side_effect=RuntimeError("target could not be created"))
    state = RealBrowserState(pw=MagicMock(), browser_context=context)

    state._on_page_crashed(crashed)
    await _reap_crashed_pages(state)

    crashed.close.assert_not_awaited()
    assert await state.list_valid_pages() == []


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
    state = RealBrowserState(pw=MagicMock(), browser_context=_crashable_context(page))

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
    state = RealBrowserState(pw=MagicMock(), browser_context=_crashable_context(page))

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
    context = _crashable_context(crashed, stubborn, hanging)
    state = RealBrowserState(pw=MagicMock(), browser_context=context)

    state._on_page_crashed(crashed)
    state._on_page_crashed(stubborn)
    state._on_page_crashed(hanging)
    await _reap_crashed_pages(state)

    crashed.close.assert_awaited_once()
    stubborn.close.assert_awaited_once()
    assert state._detached_teardown_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stop_timeout", "stop_cancel", "start", "setup", "page_reset"])
@pytest.mark.parametrize("stop_raises", [False, True])
async def test_lifecycle_v4_reconnect_rollback_clears_replacement_intent(
    monkeypatch: pytest.MonkeyPatch, phase: str, stop_raises: bool
) -> None:
    old = _crashable_context()
    replacement = _crashable_context()
    pw = MagicMock(stop=AsyncMock())
    fresh = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=pw, browser_context=old)
    started = asyncio.Event()

    async def stop() -> None:
        started.set()
        if phase in {"stop_timeout", "stop_cancel"}:
            await asyncio.Event().wait()
        if stop_raises:
            raise RuntimeError("stop failed")

    async def setup(**_: object) -> None:
        state.browser_context = replacement
        state._register_disconnect_listeners(replacement)
        raise RuntimeError("setup failed")

    pw.stop.side_effect = stop
    start = AsyncMock(return_value=fresh, side_effect=RuntimeError("start failed") if phase == "start" else None)
    monkeypatch.setattr(real_browser_state_module, "async_playwright", lambda: SimpleNamespace(start=start))
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=False))
    monkeypatch.setattr(state, "check_and_fix_state", setup)
    if phase == "page_reset":
        monkeypatch.setattr(state, "set_working_page", AsyncMock(side_effect=RuntimeError("page reset failed")))
    if phase == "stop_timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if phase == "stop_cancel":
            reconnect = asyncio.create_task(state.reconnect(stale_context_is_unusable=True))
            await started.wait()
            reconnect.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reconnect
        else:
            with pytest.raises(RuntimeError):
                await state.reconnect(stale_context_is_unusable=True)
        await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
        assert state.pw is pw
        assert state.browser_context is old
        assert old not in state._expected_runtime_ends
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert [(entry["expected"], entry["reason"]) for entry in ended] == [(False, "context_closed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_end", [False, True])
@pytest.mark.parametrize("stop_outcome", ["success", "raise_once", "late_success", "late_failure"])
async def test_lifecycle_v4_guarded_stale_end_is_published_once(
    monkeypatch: pytest.MonkeyPatch, prior_end: bool, stop_outcome: str
) -> None:
    old = _crashable_context()
    page = _crashable_page()
    page.url = "about:blank"
    page.is_closed.return_value = False
    new = _crashable_context(page)
    old_pw = MagicMock(stop=AsyncMock())
    fresh = MagicMock(stop=AsyncMock())
    state = RealBrowserState(pw=old_pw, browser_context=old)
    state._sessionless_init_script_registrations = ["registration"]
    released = asyncio.Event()
    cancelled = asyncio.Event()
    completed = asyncio.Event()
    attempts = 0

    async def stop() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if stop_outcome.startswith("late"):
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await released.wait()
            if stop_outcome in {"raise_once", "late_failure"}:
                raise RuntimeError("stop failed")
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
        completed.set()

    old_pw.stop.side_effect = stop
    monkeypatch.setattr(
        real_browser_state_module, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=fresh))
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(), None)),
    )
    if stop_outcome.startswith("late"):
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if prior_end:
            state._on_browser_context_closed(old)
        await state.reconnect(stale_context_is_unusable=True)
        if stop_outcome.startswith("late"):
            await cancelled.wait()
            released.set()
        await completed.wait()
        await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
        assert state.pw is fresh
        assert state.browser_context is new
        assert state.get_browser_state_diagnostic() is None
        assert state.sessionless_init_script_registrations == (("registration",) if not prior_end else ())
        stale_ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        assert len(stale_ended) == 1
        assert stale_ended[0]["expected"] is (not prior_end)
        assert stale_ended[0]["reason"] == ("context_closed" if prior_end else "driver_replacement")
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
        state._on_browser_context_closed(new)
        state._on_browser_disconnected(new.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 2
    assert ended[-1]["expected"] is False
    assert ended[-1]["reason"] == "context_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["close", "failure", "no_close", "cancel"])
@pytest.mark.parametrize("detach", ["timeout", "caller_cancel", "immediate"])
async def test_lifecycle_v4_close_intent_follows_owned_teardown(
    monkeypatch: pytest.MonkeyPatch, completion: str, detach: str
) -> None:
    context = _crashable_context()
    state = RealBrowserState(pw=MagicMock(), browser_context=context)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    finish = asyncio.Event()
    monkeypatch.setattr(real_browser_state_module, "disable_download_interceptor_for_context", AsyncMock())
    monkeypatch.setattr(real_browser_state_module, "persist_session_cookies", AsyncMock())

    async def close_context() -> None:
        entered.set()
        if detach != "immediate":
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await finish.wait()
        if completion == "close":
            context._impl_obj._closed = True
            state._on_browser_context_closed(context)
        elif completion == "failure":
            raise RuntimeError("close failed")
        elif completion == "cancel":
            raise asyncio.CancelledError

    context.close = close_context
    if detach == "timeout":
        monkeypatch.setattr(real_browser_state_module, "BROWSER_CLOSE_TIMEOUT", 0)
    with capture_logs() as logs:
        if detach == "caller_cancel":
            closing = asyncio.create_task(state.close(release_driver=False))
            await entered.wait()
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await state.close(release_driver=False)
        if detach != "immediate":
            await cancelled.wait()
            pending_close_requested = state._close_requested
            pending_reason = state._expected_runtime_ends.get(context)
            finish.set()
            await asyncio.gather(*list(state._detached_teardown_tasks), return_exceptions=True)
            assert pending_close_requested
            assert pending_reason == "normal_close"
        if completion != "close":
            assert not state._close_requested
            assert context not in state._expected_runtime_ends
        state._on_browser_context_closed(context)
        state._on_browser_disconnected(context.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["expected"] is (completion == "close")
    assert ended[0]["reason"] == ("normal_close" if completion == "close" else "context_closed")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_signal", ["context", "driver", "browser"])
@pytest.mark.parametrize("failure", ["raise", "cancel"])
@pytest.mark.parametrize("stage", ["page_reset", "setup"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_reconnect_rollback_immediately_recovers_stale_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch, first_signal: str, failure: str, stage: str, deferred: bool
) -> None:
    state, old, old_future = _transport_observed_state()
    old_browser = old.browser
    old_callback = next(item.args[1] for item in old_browser.on.call_args_list if item.args[0] == "disconnected")
    replacement, new, _ = _transport_observed_state()
    old_pw = state.pw
    replacement.browser_context = None
    state._runtime_events_deferred = deferred
    error = asyncio.CancelledError("setup cancelled") if failure == "cancel" else RuntimeError("setup failed")
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)

    async def fail_setup(*args: object, **kwargs: object) -> None:
        assert state.pw is not old_pw
        if stage == "setup":
            assert state.browser_context is new
        old._impl_obj._closed = True
        if first_signal == "context":
            state._on_browser_context_closed(old)
            clock.now.return_value = first_observed_at + timedelta(seconds=1)
        elif first_signal == "browser":
            if stage == "setup":
                monkeypatch.setattr(
                    type(new), "browser", PropertyMock(side_effect=RuntimeError("unavailable")), raising=False
                )
            old_callback(old_browser)
            clock.now.return_value = first_observed_at + timedelta(seconds=1)
        delivered = asyncio.Event()
        old_future.add_done_callback(lambda _: delivered.set())
        old_future.set_exception(RuntimeError("stale driver lost during setup"))
        await delivered.wait()
        clock.now.return_value = first_observed_at + timedelta(seconds=2)
        state._on_browser_context_closed(old)
        state._on_browser_disconnected(old.browser)
        raise error

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(), None)),
    )
    if stage == "page_reset":
        monkeypatch.setattr(state, "set_working_page", fail_setup)
    else:
        monkeypatch.setattr(state, "get_working_page", fail_setup)
    with capture_logs() as logs:
        with pytest.raises(type(error)) as raised:
            await state.reconnect(stale_context_is_unusable=True)
        assert raised.value is error
        assert state.pw is old_pw
        assert state.browser_context is old
        if deferred:
            assert state._deferred_runtime_end is not None
            assert state._deferred_runtime_end.diagnostic.disconnect_observed_at == first_observed_at
            assert state.get_browser_state_diagnostic() is None
            state.record_browser_acquisition("attach")
        diagnostic = state.get_browser_state_diagnostic()
        assert diagnostic is not None
        assert diagnostic.disconnect_observed_at == first_observed_at
        state._on_browser_context_closed(old)
        state._on_driver_transport_lost(old_pw, old)
        assert state.get_browser_state_diagnostic() is diagnostic
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["expected"] is False
    assert (
        ended[0]["disconnect_kind"]
        == {"context": "context_closed", "driver": "driver_transport_loss", "browser": "browser_disconnected"}[
            first_signal
        ]
    )
    assert ended[0]["observation_source"] == ("driver_event" if first_signal == "driver" else "browser_event")


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["context", "driver"])
async def test_reconnect_rollback_reobserves_loss_without_delivered_callback(
    monkeypatch: pytest.MonkeyPatch, loss: str
) -> None:
    state, old, future = _transport_observed_state()
    fresh = MagicMock(stop=AsyncMock())

    async def setup(**_: object) -> None:
        if loss == "context":
            old._impl_obj._closed = True
        else:
            future.set_exception(RuntimeError("transport closed"))
        raise RuntimeError("setup failed")

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=fresh))
    )
    monkeypatch.setattr(state, "check_and_fix_state", setup)

    async def cleanup() -> None:
        assert state.get_browser_state_diagnostic() is not None

    fresh.stop.side_effect = cleanup
    with capture_logs() as logs, pytest.raises(RuntimeError, match="setup failed"):
        await state.reconnect(stale_context_is_unusable=True)
    assert state.get_browser_state_diagnostic() is not None
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    assert ended[0]["disconnect_kind"] == ("context_closed" if loss == "context" else "driver_transport_loss")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_signal", ["context", "driver"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_reconnect_success_keeps_stale_setup_loss_separate_from_replacement(
    monkeypatch: pytest.MonkeyPatch, first_signal: str, deferred: bool
) -> None:
    state, old, future = _transport_observed_state()
    state._runtime_events_deferred = deferred
    replacement, new, _ = _transport_observed_state()
    replacement.browser_context = None

    async def working_page() -> MagicMock:
        if first_signal == "context":
            state._on_browser_context_closed(old)
        delivered = asyncio.Event()
        future.add_done_callback(lambda _: delivered.set())
        future.set_exception(RuntimeError("stale transport failed during handoff"))
        await delivered.wait()
        state._on_browser_context_closed(old)
        return MagicMock()

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(), None)),
    )
    monkeypatch.setattr(state, "get_working_page", working_page)
    with capture_logs() as logs:
        await state.reconnect(stale_context_is_unusable=True)
        assert state.browser_context is new
        assert state.pw is replacement.pw
        assert state.get_browser_state_diagnostic() is None
        if deferred:
            assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
            state.record_browser_acquisition("attach")
            state.publish_runtime_events()
            assert state.get_browser_state_diagnostic() is None
        state._on_browser_context_closed(old)
        state._on_browser_context_closed(new)
        state._on_browser_disconnected(new.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 2
    assert [entry["expected"] for entry in ended] == [False, False]
    assert ended[0]["disconnect_kind"] == ("context_closed" if first_signal == "context" else "driver_transport_loss")
    assert ended[1]["disconnect_kind"] == "context_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signal", "moment"),
    [
        ("context", "pending"),
        ("driver", "pending"),
        ("context", "handoff"),
        ("browser", "handoff"),
        ("driver", "handoff"),
        ("closed_flag", "handoff"),
        ("context_then_driver", "handoff"),
        ("driver", "stop"),
        ("closed_flag", "stop"),
        ("normal", "handoff"),
    ],
)
async def test_unpublished_reconnect_replays_retired_generation_with_its_owner(
    monkeypatch: pytest.MonkeyPatch, signal: str, moment: str
) -> None:
    owner = SkyvernContext(workflow_run_id="old-run", browser_session_id="old-session")
    new_owner = SkyvernContext(task_id="new-task", browser_session_id="new-session")
    unrelated = SkyvernContext(workflow_run_id="ambient-run")
    with skyvern_context.scoped(owner):
        state, old, future = _transport_observed_state()
    state._runtime_events_deferred = True
    state.browser_artifacts = BrowserArtifacts(remote_browser_session_id="old-remote-session")
    old_pw = state.pw
    replacement, new, _ = _transport_observed_state()
    replacement.browser_context = None
    context_closed = next(call.args[1] for call in old.on.call_args_list if call.args[0] == "close")
    browser_disconnected = next(
        call.args[1] for call in old.browser.on.call_args_list if call.args[0] == "disconnected"
    )
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)

    def lose_stale_generation() -> None:
        if signal in {"context", "context_then_driver"}:
            context_closed(old)
        elif signal == "browser":
            browser_disconnected(old.browser)
        elif signal == "closed_flag":
            old._impl_obj._closed = True
        if signal in {"driver", "context_then_driver"}:
            future.set_exception(RuntimeError("private transport wss://example.invalid/?token=hidden"))

    check_and_fix = state.check_and_fix_state

    async def finish_setup(**kwargs: object) -> None:
        await check_and_fix(**kwargs)
        if moment == "handoff":
            lose_stale_generation()
        elif moment == "stop":
            asyncio.get_running_loop().call_soon(lose_stale_generation)

    async def stop() -> None:
        clock.now.return_value = first_observed_at + timedelta(seconds=1)
        context_closed(old)
        browser_disconnected(old.browser)

    old_pw.stop.side_effect = stop
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(remote_browser_session_id="new-remote-session"), None)),
    )
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(state, "check_and_fix_state", finish_setup)
    with skyvern_context.scoped(unrelated), capture_runtime_logs() as logs:
        if moment == "pending":
            lose_stale_generation()
        await state.reconnect(stale_context_is_unusable=True)
        assert state.browser_context is new and state.pw is replacement.pw
        assert state.get_browser_state_diagnostic() is None
        assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        with skyvern_context.scoped(new_owner):
            state.bind_runtime_event_context(
                BrowserRuntimeLogContext.for_run(task_id="new-task", browser_session_id="new-session")
            )
        state.record_browser_acquisition("attach")
        state.publish_runtime_events()
        state.record_browser_acquisition("attach")
        assert state.get_browser_state_diagnostic() is None
        context_closed(old)
        browser_disconnected(old.browser)
        state._on_driver_transport_lost(old_pw, old)
        retired = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        assert len(retired) == 1
        state._on_browser_context_closed(new)
        state.publish_runtime_events()
        assert skyvern_context.current() is unrelated
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 2
    assert [(entry["workflow_run_id"], entry["task_id"], entry["browser_session_id"]) for entry in ended] == [
        ("old-run", None, "old-session"),
        (None, "new-task", "new-session"),
    ]
    assert [entry["remote_browser_session_id"] for entry in ended] == ["old-remote-session", "new-remote-session"]
    assert ended[0]["expected"] is (signal == "normal")
    assert ended[0]["disconnect_kind"] == (
        "intentional_teardown"
        if signal == "normal"
        else "driver_transport_loss"
        if signal == "driver"
        else "browser_disconnected"
        if signal == "browser"
        else "context_closed"
    )
    if signal != "normal":
        assert ended[0]["disconnect_observed_at"] == first_observed_at.isoformat()
    assert len([entry for entry in owner.log if entry.get("browser_runtime_event") == "runtime_ended"]) == 1
    assert len([entry for entry in new_owner.log if entry.get("browser_runtime_event") == "runtime_ended"]) == 1
    assert not [entry for entry in unrelated.log if entry.get("browser_runtime_event")]
    assert "private transport" not in str(ended) and "example.invalid" not in str(ended)
    acquired = next(entry for entry in logs if entry.get("browser_runtime_event") == "acquire_result")
    assert logs.index(acquired) < logs.index(ended[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_lost", [False, True])
async def test_unpublished_reconnect_keeps_each_generation_until_acquisition(
    monkeypatch: pytest.MonkeyPatch, replacement_lost: bool
) -> None:
    owner = SkyvernContext(workflow_run_id="owner-run", browser_session_id="owner-session")
    with skyvern_context.scoped(owner):
        state, old, _ = _transport_observed_state()
    state._runtime_events_deferred = True
    state.browser_artifacts = BrowserArtifacts(remote_browser_session_id="generation-0")
    replacements = [_transport_observed_state(), _transport_observed_state()]
    middle, new = (item[1] for item in replacements)
    for replacement, _, _ in replacements:
        replacement.browser_context = None

    async def working_page() -> MagicMock:
        state._on_browser_context_closed(old if state.browser_context is middle else middle)
        if state.browser_context is new and replacement_lost:
            state._on_browser_context_closed(new)
        return MagicMock()

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    starter = AsyncMock(side_effect=[item[0].pw for item in replacements])
    monkeypatch.setattr(real_browser_state_module, "async_playwright", lambda: SimpleNamespace(start=starter))
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(
            side_effect=[
                (middle, BrowserArtifacts(remote_browser_session_id="generation-1"), None),
                (new, BrowserArtifacts(remote_browser_session_id="generation-2"), None),
            ]
        ),
    )
    monkeypatch.setattr(state, "get_working_page", working_page)
    with capture_runtime_logs() as logs:
        await state.reconnect(stale_context_is_unusable=True)
        await state.reconnect(stale_context_is_unusable=True)
        assert state.browser_context is new
        assert state.get_browser_state_diagnostic() is None
        assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
        state.record_browser_acquisition("attach")
        diagnostic = state.get_browser_state_diagnostic()
        if replacement_lost:
            assert diagnostic is not None and diagnostic.browser_session_id == "generation-2"
        else:
            assert diagnostic is None
        state.publish_runtime_events()
        state._on_browser_context_closed(old)
        state._on_browser_context_closed(middle)
        assert state.get_browser_state_diagnostic() is diagnostic
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert [entry["remote_browser_session_id"] for entry in ended] == [
        f"generation-{index}" for index in range(3 if replacement_lost else 2)
    ]
    assert all(entry["workflow_run_id"] == "owner-run" and entry["expected"] is False for entry in ended)
    assert len([entry for entry in owner.log if entry.get("browser_runtime_event") == "runtime_ended"]) == len(ended)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal", ["driver", "context", "context_then_driver", "cancelled_future", "successful_future"]
)
async def test_reconnect_success_rechecks_stale_loss_before_queued_callback(
    monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    state, old, future = _transport_observed_state()
    replacement, new, _ = _transport_observed_state()
    old_pw = state.pw
    replacement.browser_context = None
    state.bind_runtime_event_context(
        BrowserRuntimeLogContext(workflow_run_id="workflow-old", browser_session_id="session-old")
    )
    delivered = asyncio.Event()
    future.add_done_callback(lambda _: delivered.set())
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)
    check_and_fix = state.check_and_fix_state

    async def finish_setup(**kwargs: object) -> None:
        await check_and_fix(**kwargs)
        if signal == "context_then_driver":
            state._on_browser_context_closed(old)
            clock.now.return_value = first_observed_at + timedelta(seconds=1)
        if signal in {"driver", "context_then_driver"}:
            future.set_exception(RuntimeError("private stale transport failure"))
        elif signal == "context":
            old._impl_obj._closed = True

            def deliver_context_close() -> None:
                state._on_browser_context_closed(old)
                delivered.set()

            asyncio.get_running_loop().call_soon(deliver_context_close)
        elif signal == "cancelled_future":
            future.cancel()
        else:
            future.set_result(None)
        assert not delivered.is_set()

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(), None)),
    )
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(state, "check_and_fix_state", finish_setup)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="ambient-run")), capture_logs() as logs:
        await state.reconnect(stale_context_is_unusable=True)
        await delivered.wait()
        assert state.browser_context is new
        assert state.pw is replacement.pw
        assert state.get_browser_state_diagnostic() is None
        state._on_browser_context_closed(old)
        if signal in {"driver", "context_then_driver"}:
            state._on_driver_transport_lost(old_pw, old)
        state.bind_runtime_event_context(
            BrowserRuntimeLogContext(workflow_run_id="workflow-new", browser_session_id="session-new")
        )
        state._on_browser_context_closed(new)
        state._on_browser_disconnected(new.browser)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 2
    assert [entry["workflow_run_id"] for entry in ended] == ["workflow-old", "workflow-new"]
    assert [entry["browser_session_id"] for entry in ended] == ["session-old", "session-new"]
    assert ended[0]["disconnect_observed_at"] == first_observed_at.isoformat()
    if signal in {"cancelled_future", "successful_future"}:
        assert ended[0]["expected"] is True
        assert ended[0]["disconnect_kind"] == "intentional_teardown"
    else:
        assert ended[0]["expected"] is False
        assert ended[0]["disconnect_kind"] == ("driver_transport_loss" if signal == "driver" else "context_closed")
        assert ended[0]["disconnect_evidence"] == (
            "transport_error_future"
            if signal == "driver"
            else "context_event"
            if signal == "context_then_driver"
            else "liveness_state"
        )
    assert ended[1]["expected"] is False
    assert ended[1]["disconnect_kind"] == "context_closed"
    assert "private stale transport failure" not in str(ended)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("teardown", "deferred"),
    [
        ("detach", False),
        ("detach", True),
        ("release", False),
        ("release", True),
        ("close", False),
        ("close", True),
        ("recreate", False),
    ],
)
@pytest.mark.parametrize(
    "signal", ["driver", "context", "context_then_driver", "cancelled_future", "successful_future"]
)
async def test_teardown_observes_completed_loss_before_installing_intent(
    monkeypatch: pytest.MonkeyPatch, teardown: str, deferred: bool, signal: str
) -> None:
    state, context, future = _transport_observed_state()
    context._skyvern_cdp_download_interceptor = None
    state.bind_runtime_event_context(
        BrowserRuntimeLogContext(workflow_run_id="workflow-owner", browser_session_id="session-owner")
    )
    state._runtime_events_deferred = deferred
    delivered = asyncio.Event()
    future.add_done_callback(lambda _: delivered.set())
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)

    def queue_loss() -> None:
        if signal == "context_then_driver":
            state._on_browser_context_closed(context)
            clock.now.return_value = first_observed_at + timedelta(seconds=1)
        if signal in {"driver", "context_then_driver"}:
            future.set_exception(RuntimeError("private transport failure"))
        elif signal == "context":
            context._impl_obj._closed = True

            def deliver_close() -> None:
                state._on_browser_context_closed(context)
                delivered.set()

            asyncio.get_running_loop().call_soon(deliver_close)
        elif signal == "cancelled_future":
            future.cancel()
        else:
            future.set_result(None)
        assert not delivered.is_set()

    async def close_context() -> None:
        clock.now.return_value = first_observed_at + timedelta(seconds=2)
        context._impl_obj._closed = True
        state._on_browser_context_closed(context)

    context.close = AsyncMock(side_effect=close_context)
    state.pw.stop.side_effect = close_context
    monkeypatch.setattr(real_browser_state_module, "persist_session_cookies", AsyncMock())
    monkeypatch.setattr(real_browser_state_module, "disable_download_interceptor_for_context", AsyncMock())
    run_bounded = state._run_bounded_detachable

    async def finish_phase(awaitable: Awaitable[None], timeout: float, description: str, **kwargs: object) -> bool:
        result = await run_bounded(awaitable, timeout, description, **kwargs)
        if description == "download interceptor disable":
            queue_loss()
        return result

    monkeypatch.setattr(state, "_run_bounded_detachable", finish_phase)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="unrelated-run")), capture_logs() as logs:
        if teardown == "detach":
            context._skyvern_cdp_download_interceptor = SimpleNamespace(disable=AsyncMock(side_effect=queue_loss))
            await state.detach_remote_driver()
        elif teardown == "recreate":
            monkeypatch.setattr(state, "_close_all_other_pages", AsyncMock(side_effect=queue_loss))
            assert await state.close_current_open_page()
        else:
            await state.close(close_browser_on_completion=teardown == "close", release_driver=True)
        await delivered.wait()
        if deferred:
            assert not [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
            state.publish_runtime_events()
        diagnostic = state.get_browser_state_diagnostic()
        assert diagnostic is not None
        state._on_driver_transport_lost(state.pw, context)
        state._on_browser_context_closed(context)
        assert state.get_browser_state_diagnostic() is diagnostic

    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 1
    event = ended[0]
    expected = signal in {"cancelled_future", "successful_future"}
    assert event["expected"] is expected
    assert event["workflow_run_id"] == "workflow-owner"
    assert event["browser_session_id"] == "session-owner"
    if expected:
        assert event["disconnect_kind"] == "intentional_teardown"
        assert event["disconnect_evidence"] == "teardown_intent"
    else:
        assert event["disconnect_observed_at"] == first_observed_at.isoformat()
        assert event["close_requested"] is False
        assert event["disconnect_kind"] == ("driver_transport_loss" if signal == "driver" else "context_closed")
        assert event["disconnect_evidence"] == (
            "transport_error_future"
            if signal == "driver"
            else "context_event"
            if signal == "context_then_driver"
            else "liveness_state"
        )
    assert "private transport failure" not in str(ended)


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize(
    "signal", ["driver", "context", "context_then_driver", "cancelled_future", "successful_future"]
)
async def test_reconnect_scheduled_stop_observes_loss_before_installing_intent(
    monkeypatch: pytest.MonkeyPatch, guarded: bool, signal: str
) -> None:
    state, old, future = _transport_observed_state()
    replacement, new, _ = _transport_observed_state()
    old_pw = state.pw
    replacement.browser_context = None
    state.bind_runtime_event_context(
        BrowserRuntimeLogContext(workflow_run_id="workflow-old", browser_session_id="session-old")
    )
    delivered = asyncio.Event()
    future.add_done_callback(lambda _: delivered.set())
    first_observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = first_observed_at
    monkeypatch.setattr(real_browser_state_module, "datetime", clock)

    def queue_loss() -> None:
        if signal in {"driver", "context_then_driver"}:
            future.set_exception(RuntimeError("private retired transport failure"))
        elif signal == "context":
            old._impl_obj._closed = True

            def deliver_close() -> None:
                state._on_browser_context_closed(old)
                delivered.set()

            asyncio.get_running_loop().call_soon(deliver_close)
        elif signal == "cancelled_future":
            future.cancel()
        else:
            future.set_result(None)
        assert not delivered.is_set()

    def schedule_loss() -> None:
        if signal == "context_then_driver":
            state._on_browser_context_closed(old)
            clock.now.return_value = first_observed_at + timedelta(seconds=1)
        asyncio.get_running_loop().call_soon(queue_loss)

    async def has_guard(_: object) -> bool:
        if not guarded:
            schedule_loss()
        return guarded

    async def stop_old() -> None:
        assert not delivered.is_set()
        old._impl_obj._closed = True
        state._on_browser_context_closed(old)

    old_pw.stop.side_effect = stop_old
    check_and_fix = state.check_and_fix_state

    async def finish_setup(**kwargs: object) -> None:
        await check_and_fix(**kwargs)
        if guarded:
            schedule_loss()

    monkeypatch.setattr(app.AGENT_FUNCTION, "has_retained_browser_egress_guard", has_guard)
    monkeypatch.setattr(
        real_browser_state_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=replacement.pw)),
    )
    monkeypatch.setattr(
        real_browser_state_module.BrowserContextFactory,
        "create_browser_context",
        AsyncMock(return_value=(new, BrowserArtifacts(), None)),
    )
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(state, "check_and_fix_state", finish_setup)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="ambient-run")), capture_logs() as logs:
        await state.reconnect(stale_context_is_unusable=True)
        await delivered.wait()
        assert state.browser_context is new
        assert state.pw is replacement.pw
        assert state.get_browser_state_diagnostic() is None
        state._on_driver_transport_lost(old_pw, old)
        state._on_browser_context_closed(old)
        state.bind_runtime_event_context(
            BrowserRuntimeLogContext(workflow_run_id="workflow-new", browser_session_id="session-new")
        )
        state._on_browser_context_closed(new)
    ended = [entry for entry in logs if entry.get("browser_runtime_event") == "runtime_ended"]
    assert len(ended) == 2
    assert [entry["workflow_run_id"] for entry in ended] == ["workflow-old", "workflow-new"]
    assert [entry["browser_session_id"] for entry in ended] == ["session-old", "session-new"]
    assert ended[0]["disconnect_observed_at"] == first_observed_at.isoformat()
    expected = signal in {"cancelled_future", "successful_future"}
    assert ended[0]["expected"] is expected
    assert ended[0]["disconnect_kind"] == (
        "intentional_teardown" if expected else "driver_transport_loss" if signal == "driver" else "context_closed"
    )
    assert ended[1]["expected"] is False
    assert ended[1]["disconnect_kind"] == "context_closed"
    assert "private retired transport failure" not in str(ended)
