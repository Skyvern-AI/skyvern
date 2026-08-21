"""Tests for copilot runtime helpers.

Covers `mcp_to_copilot`, the pure dict adapter that normalizes MCP results
into the copilot `{ok, data, error}` envelope, plus the error-sanitization
contract on `ensure_browser_session`. Full coverage of the async context
managers lives in `tests/unit/test_copilot_session_injection.py`
alongside the tools and enforcement helpers they exercise end-to-end.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import TimeoutError as SQLATimeoutError

from skyvern.forge.sdk.copilot import runtime
from skyvern.forge.sdk.copilot.runtime import AgentContext, ensure_browser_session, mcp_browser_context, mcp_to_copilot


class _FakeBrowser:
    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _FakeBrowserContext:
    def __init__(self, *, connected: bool = True, closed: bool = False) -> None:
        self.browser = _FakeBrowser(connected=connected)
        self._impl_obj = SimpleNamespace(_close_was_called=closed, _closed=closed)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"ok": True, "data": {"count": 3}}, {"ok": True, "data": {"count": 3}}, id="ok_passthrough"),
        pytest.param({"data": "x"}, {"ok": True, "data": "x"}, id="defaults_ok_true_when_missing"),
        # Upstream MCP tool returning an error-shaped dict without an explicit
        # ok field must not produce {"ok": True, "error": "..."}.
        pytest.param(
            {"error": "tool exploded"},
            {"ok": False, "error": "tool exploded"},
            id="defaults_ok_false_when_error_present_without_ok",
        ),
        pytest.param(
            {"ok": False, "error": {"code": "E1", "message": "boom", "hint": "retry later"}},
            {"ok": False, "error": "boom. retry later", "error_code": "E1"},
            id="error_with_hint_joins_message_and_hint",
        ),
        pytest.param(
            {"ok": False, "error": {"code": "E1", "message": "boom"}},
            {"ok": False, "error": "boom", "error_code": "E1"},
            id="error_without_hint_uses_message_only",
        ),
        pytest.param(
            {"ok": False, "error": {"message": "boom", "hint": ""}},
            {"ok": False, "error": "boom"},
            id="error_with_empty_hint_uses_message_only",
        ),
        pytest.param(
            {"ok": False, "error": {"code": "E1"}},
            {"ok": False, "error": "Unknown error", "error_code": "E1"},
            id="error_dict_without_message_uses_default",
        ),
        pytest.param(
            {"ok": False, "error": ValueError("boom")},
            {"ok": False, "error": "boom"},
            id="non_dict_error_coerced_with_str",
        ),
        pytest.param({"ok": False, "error": "boom"}, {"ok": False, "error": "boom"}, id="string_error_passthrough"),
        pytest.param({"ok": True, "data": None}, {"ok": True}, id="data_none_omitted"),
        pytest.param(
            {"ok": True, "warnings": ["slow response"]},
            {"ok": True, "warnings": ["slow response"]},
            id="warnings_passthrough",
        ),
        pytest.param({"ok": True, "warnings": []}, {"ok": True}, id="empty_warnings_omitted"),
    ],
)
def test_mcp_to_copilot(payload: dict[str, Any], expected: dict[str, Any]) -> None:
    assert mcp_to_copilot(payload) == expected


def _make_ctx(*, api_key: str | None = "test-api-key") -> AgentContext:
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    return AgentContext(
        organization_id="org_1",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
    )


@pytest.mark.asyncio
async def test_ensure_browser_session_error_dict_omits_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # The returned error envelope flows back through the tool/agent path and
    # can surface in LLM-visible or user-visible output. The raw exception
    # may carry internal URLs, file paths, or backend identifiers -- it must
    # stay in the logs, not the return value.
    import skyvern.forge.sdk.copilot.runtime as runtime

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(
        side_effect=RuntimeError("internal: http://persistent-sessions.internal.svc:8080/ failed"),
    )
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result is not None
    assert isinstance(result, dict)
    assert result["ok"] is False
    error_text: Any = result["error"]
    assert isinstance(error_text, str)
    assert "persistent-sessions.internal.svc" not in error_text
    assert "http://" not in error_text
    assert "internal:" not in error_text


@pytest.mark.asyncio
async def test_ensure_browser_session_waits_for_browser_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # DefaultPersistentSessionsManager.create_session returns before chromium
    # has finished booting; ensure_browser_session must poll until
    # browser_context is set so the next mcp_browser_context lookup succeeds.
    import skyvern.forge.sdk.copilot.runtime as runtime

    session = MagicMock()
    session.persistent_browser_session_id = "bs_1"

    pending_state = MagicMock()
    pending_state.browser_context = None
    ready_state = MagicMock()
    ready_state.browser_context = _FakeBrowserContext()

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_manager.get_browser_state = AsyncMock(side_effect=[None, pending_state, ready_state])
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result is None
    assert ctx.browser_session_id == "bs_1"
    assert mock_manager.get_browser_state.await_count == 3


@pytest.mark.asyncio
async def test_ensure_browser_session_recreates_disconnected_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # A persistent-session DB row can still point at a Playwright context whose
    # backing browser has been closed. Treat it as stale so Copilot does not
    # keep reusing a dead session after a target page/browser shutdown.
    import skyvern.forge.sdk.copilot.runtime as runtime

    stale_state = MagicMock()
    stale_state.browser_context = _FakeBrowserContext(connected=False)
    fresh_state = MagicMock()
    fresh_state.browser_context = _FakeBrowserContext()
    session = MagicMock()
    session.persistent_browser_session_id = "bs_fresh"

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=[stale_state, fresh_state])
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_stale"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_fresh"
    mock_manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_browser_session_recreates_closed_persistent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # A completed persistent-session row is not begin-able even when an old
    # Playwright context is still around in memory. Recreate before handing
    # the id to workflow execution.
    import skyvern.forge.sdk.copilot.runtime as runtime

    fresh_state = MagicMock()
    fresh_state.browser_context = _FakeBrowserContext()
    session = MagicMock()
    session.persistent_browser_session_id = "bs_fresh"

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=fresh_state)
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_browser_sessions = MagicMock()
    mock_browser_sessions.get_persistent_browser_session = AsyncMock(return_value=SimpleNamespace(status="completed"))
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions = mock_browser_sessions
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_closed"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_fresh"
    mock_browser_sessions.get_persistent_browser_session.assert_awaited_once_with("bs_closed", "org_1")
    mock_manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_browser_session_recreates_sync_closed_persistent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skyvern.forge.sdk.copilot.runtime as runtime

    fresh_state = MagicMock()
    fresh_state.browser_context = _FakeBrowserContext()
    session = MagicMock()
    session.persistent_browser_session_id = "bs_fresh"

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=fresh_state)
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_browser_sessions = MagicMock()
    mock_browser_sessions.get_persistent_browser_session = MagicMock(return_value=SimpleNamespace(status="failed"))
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions = mock_browser_sessions
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_closed"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_fresh"
    mock_browser_sessions.get_persistent_browser_session.assert_called_once_with("bs_closed", "org_1")
    mock_manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_browser_session_times_out_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # If chromium never boots within _BROWSER_BOOT_WAIT_SECONDS, fall into the
    # cleanup branch so the agent does not keep building on a phantom session.
    import skyvern.forge.sdk.copilot.runtime as runtime

    session = MagicMock()
    session.persistent_browser_session_id = "bs_2"

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_manager.get_browser_state = AsyncMock(return_value=None)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    result = await ensure_browser_session(ctx)
    assert result == {"ok": False, "error": "Failed to create browser session"}
    assert ctx.browser_session_id is None
    mock_manager.close_session.assert_awaited_once_with("org_1", "bs_2")


@pytest.mark.asyncio
async def test_cancelled_browser_boot_closes_the_partially_created_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.persistent_browser_session_id = "bs_cancelled_boot"
    boot_polled = asyncio.Event()

    async def _never_boots(*_args: Any, **_kwargs: Any) -> None:
        boot_polled.set()
        await asyncio.Event().wait()

    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(return_value=session)
    mock_manager.get_browser_state = AsyncMock(side_effect=_never_boots)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    task = asyncio.create_task(ensure_browser_session(ctx))
    await boot_polled.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.browser_session_id is None
    mock_manager.close_session.assert_awaited_once_with("org_1", "bs_cancelled_boot")


@pytest.mark.asyncio
async def test_mcp_browser_context_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silently skipping set_api_key_override when ctx.api_key is None would
    let get_active_api_key() fall back to settings.SKYVERN_API_KEY — the
    exact coarse-grained-auth hole the override exists to close. The CM
    must refuse to enter without an api_key, before touching any backend."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    # If the guard is in the right place (pre-backend), we should never see
    # PERSISTENT_SESSIONS_MANAGER touched. Install a tripwire.
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(
        side_effect=AssertionError("backend accessed before api_key guard"),
    )
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx(api_key=None)
    ctx.browser_session_id = "bs_1"

    with pytest.raises(RuntimeError, match="missing api_key"):
        async with mcp_browser_context(ctx):
            pass

    # Tripwire must not have fired: the backend call should not have happened.
    mock_manager.get_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_browser_session_retains_session_when_lookup_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=SQLATimeoutError("QueuePool limit of size 5 overflow 10"))
    mock_manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    mock_log = MagicMock()
    monkeypatch.setattr(runtime, "LOG", mock_log)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_live"
    mock_manager.create_session.assert_not_awaited()
    warned = [call.args[0] for call in mock_log.warning.call_args_list]
    assert "Browser state probe failed; liveness undetermined" in warned
    assert "Supplied browser_session_id is no longer attachable; auto-creating" not in warned
    assert mock_log.warning.call_args_list[0].kwargs["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_ensure_browser_session_retains_session_on_arbitrary_probe_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=ZeroDivisionError("unexpected"))
    mock_manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_live"
    mock_manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_classifies_failed_lookup_as_undetermined(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=SQLATimeoutError("pool exhausted"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    # A raising sentinel cannot guard this: the probe's own except would swallow it and return the
    # expected value anyway. Spy on the call instead and assert it outside that except.
    health_check = MagicMock(name="_browser_context_attachability")
    monkeypatch.setattr(runtime, "_browser_context_attachability", health_check)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    outcome = await runtime._probe_browser_session(ctx, "bs_live")

    assert outcome == runtime.BrowserProbeOutcome.could_not_determine
    assert outcome != runtime.BrowserProbeOutcome.positively_unreachable
    health_check.assert_not_called()


@pytest.mark.asyncio
async def test_probe_deadline_is_uncertainty_not_session_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_answers(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=_never_answers)
    mock_manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_PROBE_WAIT_SECONDS", 0.01)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_slow_but_unknown"

    result = await ensure_browser_session(ctx, require_verified_session=True)

    assert result == {"ok": False, "error": "Could not verify the browser session; please retry"}
    assert ctx.browser_session_id == "bs_slow_but_unknown"
    mock_manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_browser_session_retains_across_repeated_undetermined_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No budget: an undetermined probe is never evidence against the session, however often it repeats."""
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=SQLATimeoutError("pool exhausted"))
    mock_manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    for _ in range(6):
        assert await ensure_browser_session(ctx) is None
        assert ctx.browser_session_id == "bs_live"
    mock_manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_browser_session_retains_session_when_record_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_state = MagicMock()
    live_state.browser_context = _FakeBrowserContext()

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=live_state)
    mock_manager.create_session = AsyncMock()
    mock_browser_sessions = MagicMock()
    mock_browser_sessions.get_persistent_browser_session = MagicMock(
        side_effect=SQLATimeoutError("QueuePool limit of size 5 overflow 10"),
    )
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions = mock_browser_sessions
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_live"
    mock_manager.get_browser_state.assert_awaited_once()
    mock_manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_retires_session_id_when_context_is_not_attachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attach is the only caller that learns a session is dead, so it must retire the id.
    Without this the id survives and every later tool call repeats the same failure."""
    dead_state = MagicMock()
    dead_state.browser_context = _FakeBrowserContext(connected=False)

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=dead_state)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_dead"

    with pytest.raises(RuntimeError, match="No browser context"):
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id is None


@pytest.mark.asyncio
async def test_attach_keeps_session_id_when_state_lookup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup that could not complete is not evidence the browser is gone, so the id survives."""
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=SQLATimeoutError("pool exhausted"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    with pytest.raises(SQLATimeoutError):
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id == "bs_live"


@pytest.mark.asyncio
async def test_attach_does_not_retire_a_session_replaced_underneath_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent tool call can mint a replacement while this attach is in flight. Retiring the
    id unconditionally would discard that live replacement, so the clear is compare-and-swap."""
    mock_manager = MagicMock()

    async def _replace_then_report_dead(*_args: object, **_kwargs: object) -> None:
        ctx.browser_session_id = "bs_replacement"
        return None

    mock_manager.get_browser_state = AsyncMock(side_effect=_replace_then_report_dead)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_stale"

    with pytest.raises(RuntimeError, match="No browser context"):
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id == "bs_replacement"


class _UnreachableSignalBrowser:
    """The connectivity signal itself is unavailable — not an answer about the browser."""

    def is_connected(self) -> bool:
        raise ConnectionError("cdp endpoint unreachable")


class _UnreachableSignalContext:
    def __init__(self) -> None:
        self.browser = _UnreachableSignalBrowser()
        self._impl_obj = SimpleNamespace(_close_was_called=False, _closed=False)


@pytest.mark.asyncio
async def test_probe_treats_failed_health_signal_as_undetermined(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup can complete while the health signal does not. Reading that as a verdict is the
    same mistake as reading a failed lookup as one."""
    state = MagicMock()
    state.browser_context = _UnreachableSignalContext()

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=state)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    outcome = await runtime._probe_browser_session(ctx, "bs_live")

    assert outcome == runtime.BrowserProbeOutcome.could_not_determine
    assert outcome != runtime.BrowserProbeOutcome.positively_unreachable


@pytest.mark.asyncio
async def test_attach_keeps_session_when_health_signal_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MagicMock()
    state.browser_context = _UnreachableSignalContext()

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=state)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    with pytest.raises(RuntimeError, match="No browser context") as exc_info:
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id == "bs_live"
    assert not isinstance(exc_info.value, runtime.CopilotBrowserSessionUnavailable)


@pytest.mark.asyncio
async def test_ensure_does_not_retire_a_session_replaced_during_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling tool can install a live replacement while this probe is in flight."""
    dead_state = MagicMock()
    dead_state.browser_context = _FakeBrowserContext(connected=False)
    fresh = MagicMock()
    fresh.persistent_browser_session_id = "bs_created"

    async def _replace_then_report_dead(*_args: object, **_kwargs: object) -> object:
        ctx.browser_session_id = "bs_replacement"
        return dead_state

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=_replace_then_report_dead)
    mock_manager.create_session = AsyncMock(return_value=fresh)
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_stale"

    await ensure_browser_session(ctx)

    assert ctx.browser_session_id == "bs_replacement"
    mock_manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_surfaces_infra_error_when_caller_requires_a_verified_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that dispatch the id without attaching cannot discover a dead session later."""
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=SQLATimeoutError("pool exhausted"))
    mock_manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await ensure_browser_session(ctx, require_verified_session=True)

    assert result is not None
    assert result["ok"] is False
    assert ctx.browser_session_id == "bs_live"
    mock_manager.create_session.assert_not_awaited()

    # The same probe result is success for a caller that will attach and find out for itself.
    assert await ensure_browser_session(ctx) is None


@pytest.mark.asyncio
async def test_create_closes_its_session_when_a_sibling_installed_one_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls can both find the session dead and both mint. Assigning over the winner would
    leave a live browser referenced by nobody until its 30-minute timeout."""
    ready = MagicMock()
    ready.browser_context = _FakeBrowserContext()
    dead = MagicMock()
    dead.browser_context = _FakeBrowserContext(connected=False)
    mine = MagicMock()
    mine.persistent_browser_session_id = "bs_loser"

    mock_manager = MagicMock()
    # Probe says dead, then the boot wait for whichever session survives.
    mock_manager.get_browser_state = AsyncMock(side_effect=[dead] + [ready] * 5)

    async def _sibling_wins(*_args: object, **_kwargs: object) -> object:
        ctx.browser_session_id = "bs_sibling"
        return mine

    mock_manager.create_session = AsyncMock(side_effect=_sibling_wins)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_dead"

    assert await ensure_browser_session(ctx) is None

    assert ctx.browser_session_id == "bs_sibling"
    mock_manager.close_session.assert_awaited_once()
    assert mock_manager.close_session.await_args.args[1] == "bs_loser"
