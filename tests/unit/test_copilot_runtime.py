"""Tests for copilot runtime helpers.

Covers `mcp_to_copilot`, the pure dict adapter that normalizes MCP results
into the copilot `{ok, data, error}` envelope, plus the error-sanitization
contract on `ensure_browser_session`. Full coverage of the async context
managers lives in `tests/unit/test_copilot_session_injection.py`
alongside the tools and enforcement helpers they exercise end-to-end.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.exc import TimeoutError as SQLATimeoutError
from structlog.testing import capture_logs

from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot import mcp_adapter, runtime
from skyvern.forge.sdk.copilot.build_test_connect_failure import SUPERSEDED_BY_NEWER_TEST_REASON
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.runtime import AgentContext, ensure_browser_session, mcp_browser_context, mcp_to_copilot
from skyvern.forge.sdk.copilot.unrecoverable_tool_error import _is_unrecoverable_browser_session_error
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.webeye.browser_errors import BrowserCdpConnectionError, BrowserTargetClosedError
from skyvern.webeye.persistent_sessions_manager import BrowserOperation, BrowserRetirement
from tests.unit.test_copilot_secret_scrub import _make_server


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


def _admit_mock_browser_operations(manager: MagicMock) -> None:
    @asynccontextmanager
    async def _operation(_session_id: str, browser_state: Any) -> AsyncIterator[BrowserOperation]:
        yield BrowserOperation(browser_state, BrowserRetirement())

    manager.browser_operation = _operation


def _manager_reporting_fixed_deadline(remaining_seconds: float | None) -> MagicMock:
    manager = MagicMock()
    manager.seconds_until_fixed_deadline = AsyncMock(return_value=remaining_seconds)
    manager.close_session = AsyncMock()
    manager.create_session = AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_fresh"))
    manager.get_browser_state = AsyncMock(return_value=SimpleNamespace(browser_context=_FakeBrowserContext()))
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining_seconds", "expected_session_id"),
    [(30.0, "pbs_fresh"), (900.0, "pbs_held"), (None, "pbs_held")],
    ids=["inside_final_minute", "time_left", "deadline_is_not_fixed"],
)
async def test_a_session_at_its_fixed_deadline_is_replaced_before_it_is_used(
    monkeypatch: pytest.MonkeyPatch, remaining_seconds: float | None, expected_session_id: str
) -> None:
    """SKY-15044: on infrastructure that pins the deadline at provisioning, a session in its final
    minute still attaches and then dies mid-call, so the attach cannot catch it. Time left, or a
    deadline that is not fixed at all, must leave the held session and its page state alone."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    mock_manager = _manager_reporting_fixed_deadline(remaining_seconds)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "pbs_held"

    assert await ensure_browser_session(ctx) is None
    assert ctx.browser_session_id == expected_session_id


@pytest.mark.asyncio
async def test_a_run_dispatch_does_not_hand_out_a_session_at_its_fixed_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held id reaches a workflow run through verify_browser_session_by_attaching, which attaches
    directly instead of going through ensure_browser_session. A run outlives the check by far more
    than a tool call does, so this is the path where handing over an expiring session hurts most."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    mock_manager = _manager_reporting_fixed_deadline(30.0)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "pbs_held"

    assert await runtime.verify_browser_session_by_attaching(ctx) is None
    assert ctx.browser_session_id == "pbs_fresh"
    mock_manager.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deadline_read_that_failed_keeps_the_held_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that could not answer is not an answer about the session. Discarding the id here
    would throw away a live browser and its page state on a transient failure; the attach that
    follows is the oracle for whether it is really gone."""
    import skyvern.forge.sdk.copilot.runtime as runtime

    mock_manager = MagicMock()
    mock_manager.seconds_until_fixed_deadline = AsyncMock(side_effect=RuntimeError("temporal unreachable"))
    mock_manager.create_session = AsyncMock(side_effect=AssertionError("must not replace a live session"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "pbs_live"

    assert await ensure_browser_session(ctx) is None
    assert ctx.browser_session_id == "pbs_live"


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
    mock_manager.close_session.assert_awaited_once_with("org_1", "bs_2", reason=BrowserSessionCloseReason.aborted)


@pytest.mark.asyncio
async def test_a_duplicate_session_from_a_creation_race_closes_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # SKY-15022: the loser of a create race closes a healthy browser it never used; that is not an abort.
    import skyvern.forge.sdk.copilot.runtime as runtime

    ctx = _make_ctx()
    loser = MagicMock()
    loser.persistent_browser_session_id = "bs_loser"

    async def _create(**_kwargs: object) -> MagicMock:
        ctx.browser_session_id = "bs_winner"
        return loser

    ready_state = MagicMock()
    ready_state.browser_context = _FakeBrowserContext()
    mock_manager = MagicMock()
    mock_manager.create_session = AsyncMock(side_effect=_create)
    mock_manager.get_browser_state = AsyncMock(return_value=ready_state)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    result = await ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "bs_winner"
    mock_manager.close_session.assert_awaited_once_with(
        "org_1", "bs_loser", reason=BrowserSessionCloseReason.user_requested
    )


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
    mock_manager.close_session.assert_awaited_once_with(
        "org_1", "bs_cancelled_boot", reason=BrowserSessionCloseReason.aborted
    )


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
async def test_attach_retires_session_id_when_context_is_not_attachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attach is the only caller that learns a session is dead, so it must retire the id.
    Without this the id survives and every later tool call repeats the same failure."""
    dead_state = MagicMock()
    dead_state.browser_context = _FakeBrowserContext(connected=False)

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=dead_state)
    _admit_mock_browser_operations(mock_manager)
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

    mock_manager.get_browser_state = AsyncMock(side_effect=_replace_then_report_dead)
    _admit_mock_browser_operations(mock_manager)
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
async def test_attach_keeps_session_when_health_signal_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MagicMock()
    state.browser_context = _UnreachableSignalContext()

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=state)
    _admit_mock_browser_operations(mock_manager)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    with pytest.raises(RuntimeError, match="could not be determined") as exc_info:
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id == "bs_live"
    assert not isinstance(exc_info.value, runtime.CopilotBrowserSessionUnavailable)


@pytest.mark.asyncio
async def test_an_undetermined_attach_is_not_read_as_session_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_manager = MagicMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    undetermined_state = MagicMock()
    undetermined_state.browser_context = _UnreachableSignalContext()
    mock_manager.get_browser_state = AsyncMock(return_value=undetermined_state)
    _admit_mock_browser_operations(mock_manager)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"
    with pytest.raises(RuntimeError) as undetermined:
        async with mcp_browser_context(ctx):
            pass

    mock_manager.get_browser_state = AsyncMock(return_value=None)
    ctx.browser_session_id = "bs_gone"
    with pytest.raises(runtime.CopilotBrowserSessionUnavailable) as retired:
        async with mcp_browser_context(ctx):
            pass

    def _tool_output(exc: BaseException) -> dict[str, Any]:
        return {"ok": False, "error": f"evaluate failed: {exc}"}

    assert not _is_unrecoverable_browser_session_error("evaluate", _tool_output(undetermined.value))
    assert _is_unrecoverable_browser_session_error("evaluate", _tool_output(retired.value))


@pytest.mark.asyncio
async def test_create_closes_its_session_when_a_sibling_installed_one_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls can both find no session and both mint. Assigning over the winner would
    leave a live browser referenced by nobody until its 30-minute timeout."""
    ready = MagicMock()
    ready.browser_context = _FakeBrowserContext()
    mine = MagicMock()
    mine.persistent_browser_session_id = "bs_loser"

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(return_value=ready)

    async def _sibling_wins(*_args: object, **_kwargs: object) -> object:
        ctx.browser_session_id = "bs_sibling"
        return mine

    mock_manager.create_session = AsyncMock(side_effect=_sibling_wins)
    mock_manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    ctx = _make_ctx()

    assert await ensure_browser_session(ctx) is None

    assert ctx.browser_session_id == "bs_sibling"
    mock_manager.close_session.assert_awaited_once()
    assert mock_manager.close_session.await_args.args[1] == "bs_loser"


@pytest.mark.asyncio
async def test_self_heal_browser_state_adoption_does_not_enter_persistent_resolve_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx()
    ctx.turn_origin = runtime.TurnOrigin.runtime_self_heal
    ctx.browser_session_id = "self-heal:wr_test"
    browser_state = MagicMock()
    resolve_self_heal = AsyncMock(return_value=(ctx.browser_session_id, browser_state, MagicMock()))
    manager_resolve = AsyncMock()
    monkeypatch.setattr(runtime, "_resolve_self_heal_browser_state", resolve_self_heal)
    monkeypatch.setattr(runtime.app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", manager_resolve)

    result = await runtime.resolve_browser_state_for_context(ctx)

    assert result is browser_state
    resolve_self_heal.assert_awaited_once_with(ctx)
    manager_resolve.assert_not_awaited()


_TIMING_EVENT = "MCP tool timing"


def _attachable_state() -> MagicMock:
    state = MagicMock()
    state.browser_context = _FakeBrowserContext()
    return state


def _dead_state() -> MagicMock:
    state = MagicMock()
    state.browser_context = _FakeBrowserContext(connected=False)
    return state


class _SharedSessionLookup:
    """The probe and the attach read the same session-manager lookup, so a fault meant to hit only
    the probe has to be scoped to its call."""

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.calls = 0

    async def __call__(self, **_kwargs: Any) -> Any:
        self.calls += 1
        step = self._script[self.calls - 1] if self.calls <= len(self._script) else _attachable_state()
        if isinstance(step, Exception):
            raise step
        return step


def _install_dispatch_stack(
    monkeypatch: pytest.MonkeyPatch,
    lookup: _SharedSessionLookup,
    *,
    created_session_id: str = "bs_created",
) -> MagicMock:
    created = MagicMock()
    created.persistent_browser_session_id = created_session_id

    mock_manager = MagicMock()
    mock_manager.get_browser_state = lookup
    mock_manager.create_session = AsyncMock(return_value=created)
    _admit_mock_browser_operations(mock_manager)
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = MagicMock(return_value=None)
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *_a, **_kw: MagicMock(workflow_run_id=None))
    monkeypatch.setattr(runtime, "get_active_api_key", lambda: "sk-test-key")
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: object())
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    monkeypatch.setattr(runtime, "register_copilot_session", MagicMock())
    monkeypatch.setattr(runtime, "unregister_copilot_session", MagicMock())

    async def _close(_organization_id: str, _session_id: str, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runtime, "close_browser_session_quietly", _close)
    monkeypatch.setattr(mcp_adapter, "close_browser_session_quietly", _close)
    monkeypatch.setattr(mcp_adapter.app, "CACHE", LocalCache())
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)
    mcp_adapter._LOCAL_CONTINUITY_OUTCOMES.clear()
    mcp_adapter._LOCAL_CONTINUITY_ROOTS.clear()
    return mock_manager


async def _dispatch_browser_tool(ctx: AgentContext) -> tuple[Any, list[dict[str, Any]]]:
    server = _make_server(
        ctx,
        {"ok": True, "data": {"result": 7}, "timing_ms": {"total": 1234}},
        SchemaOverlay(requires_browser=True),
    )
    with capture_logs() as captured:
        result = await server.call_tool("evaluate", {"expression": "scan()"})
    return result, [record for record in captured if record.get("event") == _TIMING_EVENT]


def _count_session_loss_handling(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    handled: list[str] = []
    original = mcp_adapter._handle_browser_session_loss

    async def _counted(ctx: AgentContext, **kwargs: Any) -> Any:
        handled.append(str(kwargs["lost_session_id"]))
        return await original(ctx, **kwargs)

    monkeypatch.setattr(mcp_adapter, "_handle_browser_session_loss", _counted)
    return handled


def _count_stored_continuity_outcomes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    stored: list[tuple[str, str]] = []
    original = mcp_adapter._store_continuity_outcome

    async def _counted(organization_id: str, outcome: mcp_adapter._BrowserSessionContinuityOutcome) -> None:
        stored.append((outcome.lost_session_id, outcome.disposition))
        await original(organization_id, outcome)

    monkeypatch.setattr(mcp_adapter, "_store_continuity_outcome", _counted)
    return stored


class TestAttachDispatch:
    @pytest.mark.asyncio
    async def test_a_dead_session_is_replaced_once_by_the_loss_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _install_dispatch_stack(monkeypatch, _SharedSessionLookup(_dead_state()))
        handled = _count_session_loss_handling(monkeypatch)
        stored = _count_stored_continuity_outcomes(monkeypatch)

        ctx = _make_ctx()
        ctx.browser_session_id = "bs_stale"

        result, _ = await _dispatch_browser_tool(ctx)

        manager.create_session.assert_awaited_once()
        assert handled == ["bs_stale"]
        assert stored == [("bs_stale", "reestablished")]
        assert ctx.browser_session_id == "bs_created"
        assert "browser session was lost" in result.content[0].text

    @pytest.mark.asyncio
    async def test_no_session_held_creates_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_dispatch_stack(monkeypatch, _SharedSessionLookup())

        ctx = _make_ctx()
        ctx.browser_session_id = None

        error, continuity, disposition = await mcp_adapter._prepare_browser_session_for_dispatch(
            ctx, tool_name="evaluate", call_path="model", observed_generation=0
        )

        assert (error, continuity, disposition) == (None, None, None)
        assert ctx.browser_session_id == "bs_created"


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_determination_running_and_never_inherits_a_stuck_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that goes away must not take the manager's determination with it.

    Going away must not CANCEL the work: the manager serializes per session, so a teardown killed
    mid-flight is simply repeated by the next call and the session is never judged. The
    determination must also not be INHERITED: a lookup that is merely stuck must not poison the
    later attach, which is the oracle and has to be free to answer on its own (a stuck probe with
    a live browser is exactly the case the escalation path exists for).
    """
    stuck = asyncio.Event()
    cancelled: list[bool] = []
    fresh_state = MagicMock()
    fresh_state.browser_context = _FakeBrowserContext()
    calls = {"n": 0}

    async def _get_browser_state(**_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            return fresh_state
        try:
            await stuck.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return fresh_state

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=_get_browser_state)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_ABANDONED_BROWSER_STATE_RESOLVES", set())

    waiter = asyncio.ensure_future(runtime.resolve_persistent_browser_state(session_id="bs_1", organization_id="org_1"))
    for _ in range(10):
        if calls["n"] == 1:
            break
        await asyncio.sleep(0)
    assert calls["n"] == 1, "the determination never started"

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert cancelled == [], "a caller going away must not cancel the manager's work"
    assert len(runtime._ABANDONED_BROWSER_STATE_RESOLVES) == 1

    # The next caller issues its own lookup rather than inheriting the stuck one.
    assert await runtime.resolve_persistent_browser_state(session_id="bs_1", organization_id="org_1") is fresh_state
    assert mock_manager.get_browser_state.await_count == 2

    stuck.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime._ABANDONED_BROWSER_STATE_RESOLVES == set(), "a finished determination must be released"


_COPILOT_PACKAGE = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "copilot"
_MANAGER_ENTRY_POINTS = frozenset(
    {
        "resolve_persistent_browser_state",
        "resolve_browser_state_for_context",
        "_resolve_self_heal_browser_state",
        "ensure_browser_session",
        "get_browser_state",
        "create_session",
        "close_session",
    }
)


# Copilot legitimately bounds two LIFECYCLE calls: the create-and-boot poll (the OSS default
# manager returns before Chrome exists) and the quiet close (the backend is usually why we are
# closing). Both are recorded in cloud_docs/persistent-browser-sessions/BOUNDS.md. Every other
# clock around a manager call is the shape decision 0032 forbids, so adding one means adding its
# constant here - a visible line in the diff, not a silent exemption.
_DOCUMENTED_LIFECYCLE_BOUNDS = frozenset({"_BROWSER_BOOT_WAIT_SECONDS", "_SESSION_CLEANUP_TIMEOUT_SECONDS"})


def _asyncio_timeout_bound(node: ast.expr) -> str | None:
    """The constant bounding an `asyncio.timeout(...)` context, or None if this is not one."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"timeout", "timeout_at"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ):
        return None
    argument = node.args[0] if node.args else None
    return argument.id if isinstance(argument, ast.Name) else ""


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _wait_for_bound(node: ast.Call) -> str:
    """The constant bounding an `asyncio.wait_for(...)` call, as its keyword or trailing argument."""
    argument = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
    if argument is None and len(node.args) > 1:
        argument = node.args[1]
    return argument.id if isinstance(argument, ast.Name) else ""


def _timeout_wrapped_manager_calls(fn: ast.AsyncFunctionDef | ast.FunctionDef, path: Path) -> list[str]:
    offenders: list[str] = []

    class _Scope(ast.NodeVisitor):
        depth = 0

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            bounds = [_asyncio_timeout_bound(item.context_expr) for item in node.items]
            bounded = any(bound is not None and bound not in _DOCUMENTED_LIFECYCLE_BOUNDS for bound in bounds)
            _Scope.depth += bounded
            self.generic_visit(node)
            _Scope.depth -= bounded

        # A closure is bounded on purpose (a page-evidence read); the rule is about direct wraps.
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return None

        def visit_Call(self, node: ast.Call) -> None:
            name = _called_name(node)
            waited = _called_name(node.args[0]) if name == "wait_for" and node.args else None
            if waited in _MANAGER_ENTRY_POINTS and _wait_for_bound(node) not in _DOCUMENTED_LIFECYCLE_BOUNDS:
                offenders.append(f"{path.name}:{node.lineno} asyncio.wait_for({waited})")
            elif _Scope.depth and name in _MANAGER_ENTRY_POINTS:
                offenders.append(f"{path.name}:{node.lineno} asyncio.timeout around {name}")
            self.generic_visit(node)

    _Scope().visit(ast.Module(body=fn.body, type_ignores=[]))
    return offenders


def test_no_copilot_timeout_directly_wraps_a_manager_call() -> None:
    """Decision 0032: the manager owns bounded resolution. A Copilot clock in front of a manager
    entry point turns a slow success into a reported failure and cannot tell slow from dead."""
    offenders: list[str] = []
    for path in sorted(_COPILOT_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        functions: list[ast.AsyncFunctionDef | ast.FunctionDef] = []
        for node in tree.body:
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                functions.append(node)
            elif isinstance(node, ast.ClassDef):
                functions.extend(n for n in node.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)))
        for fn in functions:
            offenders.extend(_timeout_wrapped_manager_calls(fn, path))
    assert offenders == [], "Copilot-owned clocks around session-manager calls:\n" + "\n".join(offenders)


@pytest.mark.asyncio
async def test_ensure_does_not_probe_an_existing_session_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attach is the oracle. A pre-dispatch lookup re-derives what the attach reveals and
    doubled the manager traffic per tool call."""
    manager = MagicMock()
    manager.get_browser_state = AsyncMock(side_effect=AssertionError("no lookup before the attach"))
    manager.create_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    assert await ensure_browser_session(ctx) is None
    assert ctx.browser_session_id == "bs_live"
    manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attach_effect", "expected_session", "expected_error_type"),
    [
        pytest.param(None, "bs_live", None, id="attachable"),
        pytest.param(runtime.CopilotBrowserSessionUnavailable("bs_live"), "bs_fresh", None, id="gone-replaced"),
        pytest.param(
            runtime.CopilotBrowserLivenessUndetermined(),
            "bs_live",
            "CopilotBrowserLivenessUndetermined",
            id="undetermined-facts",
        ),
        pytest.param(ConnectionError("pool exhausted"), "bs_live", "ConnectionError", id="manager-raised-facts"),
    ],
)
async def test_the_verified_caller_attaches_once_and_reports_what_the_attach_said(
    monkeypatch: pytest.MonkeyPatch,
    attach_effect: BaseException | None,
    expected_session: str,
    expected_error_type: str | None,
) -> None:
    created = MagicMock()
    created.persistent_browser_session_id = "bs_fresh"
    manager = MagicMock()
    manager.create_session = AsyncMock(return_value=created)
    manager.get_browser_state = AsyncMock(return_value=_attachable_state())
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.0)

    @asynccontextmanager
    async def _attach(ctx: AgentContext) -> AsyncIterator[None]:
        if isinstance(attach_effect, runtime.CopilotBrowserSessionUnavailable):
            runtime.retire_browser_session_id(ctx, ctx.browser_session_id)
        if attach_effect is not None:
            raise attach_effect
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await runtime.verify_browser_session_by_attaching(ctx)

    assert ctx.browser_session_id == expected_session
    if expected_error_type is None:
        assert result is None
    else:
        assert result is not None and result["ok"] is False
        assert result["probe_error_type"] == expected_error_type
        assert "not evidence the browser is dead" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verify",
    [
        pytest.param(runtime.verify_browser_session_by_attaching, id="run"),
        pytest.param(runtime.verify_build_test_browser_session_by_attaching, id="build-test"),
    ],
)
async def test_attach_verification_follows_one_concurrent_generation_replacement(
    monkeypatch: pytest.MonkeyPatch,
    verify: Any,
) -> None:
    attach_attempts = 0

    @asynccontextmanager
    async def _attach(_ctx: AgentContext) -> AsyncIterator[None]:
        nonlocal attach_attempts
        attach_attempts += 1
        if attach_attempts == 1:
            raise runtime.CopilotBrowserGenerationRetired("bs_live")
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    monkeypatch.setattr(runtime, "_drop_browser_session_id_at_its_fixed_deadline", AsyncMock())
    _install_occupancy_app(monkeypatch, sessions=[_session_row(runnable_id=None)])
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    assert await verify(ctx) is None
    assert attach_attempts == 2
    assert ctx.browser_session_id == "bs_live"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attach_effect", "expected_state"),
    [
        pytest.param(runtime.CopilotBrowserSessionUnavailable("bs_live"), "already_closed", id="closed"),
        pytest.param(BrowserTargetClosedError("browser closed"), "already_closed", id="target-closed"),
        pytest.param(BrowserCdpConnectionError("connect failed"), "cdp_connect_failed", id="cdp"),
    ],
)
async def test_build_test_attach_records_typed_failure_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
    attach_effect: BaseException,
    expected_state: str,
) -> None:
    manager = MagicMock()
    manager.create_session = AsyncMock(side_effect=AssertionError("must not replace automatically"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)

    @asynccontextmanager
    async def _attach(ctx: AgentContext) -> AsyncIterator[None]:
        if isinstance(attach_effect, runtime.CopilotBrowserSessionUnavailable):
            runtime.retire_browser_session_id(ctx, ctx.browser_session_id)
        raise attach_effect
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await runtime.verify_build_test_browser_session_by_attaching(ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"] == {
        "state": expected_state,
        "browser_session_id": "bs_live",
        "retry_action": "test_end_to_end",
    }
    manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attach_effect",
    [
        pytest.param(ConnectionError("manager returned an untyped failure"), id="connection-error"),
        pytest.param(ConnectionResetError("transport reset outside a normalized CDP boundary"), id="connection-reset"),
        pytest.param(PlaywrightError("browser closed"), id="native-playwright-base"),
        pytest.param(PlaywrightTimeoutError("attach timed out"), id="native-timeout"),
    ],
)
async def test_build_test_attach_does_not_invent_a_typed_state_for_unknown_failure(
    monkeypatch: pytest.MonkeyPatch,
    attach_effect: Exception,
) -> None:
    manager = MagicMock()
    manager.create_session = AsyncMock(side_effect=AssertionError("must not replace automatically"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)

    @asynccontextmanager
    async def _attach(_ctx: AgentContext) -> AsyncIterator[None]:
        raise attach_effect
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    result = await runtime.verify_build_test_browser_session_by_attaching(ctx)

    assert result is not None
    assert result["ok"] is False
    assert result["probe_error_type"] == type(attach_effect).__name__
    assert "data" not in result
    assert ctx.browser_session_id == "bs_live"
    manager.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_test_provisioning_failure_retains_created_session_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(persistent_browser_session_id="bs_created")
    manager = MagicMock()
    manager.create_session = AsyncMock(return_value=session)
    manager.get_browser_state = AsyncMock(side_effect=RuntimeError("boot unavailable"))
    manager.close_session = AsyncMock()
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)

    result = await runtime.ensure_build_test_browser_session(_make_ctx())

    assert result is not None
    assert result["data"]["build_test_connect_failure"] == {
        "state": "provisioning_unavailable",
        "browser_session_id": "bs_created",
        "retry_action": "test_end_to_end",
    }
    manager.create_session.assert_awaited_once()


def _session_row(*, runnable_id: str | None, runnable_type: str | None = "workflow_run") -> PersistentBrowserSession:
    now = datetime.now(UTC)
    return PersistentBrowserSession(
        persistent_browser_session_id="bs_live",
        organization_id="org_1",
        runnable_id=runnable_id,
        runnable_type=runnable_type,
        status="running",
        created_at=now,
        modified_at=now,
    )


def _owner_run(
    *,
    copilot_session_id: str | None,
    status: WorkflowRunStatus = WorkflowRunStatus.running,
    parent_workflow_run_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_prior",
        copilot_session_id=copilot_session_id,
        status=status,
        parent_workflow_run_id=parent_workflow_run_id,
    )


def _install_occupancy_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: list[PersistentBrowserSession],
    owner: SimpleNamespace | None = None,
) -> MagicMock:
    remaining = list(sessions)

    async def _get_session(**_kwargs: Any) -> PersistentBrowserSession:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    manager = MagicMock()
    manager.get_session = AsyncMock(side_effect=_get_session)
    manager.release_browser_session = AsyncMock()
    manager.create_session = AsyncMock(side_effect=AssertionError("must not replace automatically"))
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=owner)
    # A row back means the conditional cancel actually claimed the transition; ``None`` is the
    # no-op the holder's own terminal write already won.
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final = AsyncMock(
        return_value=SimpleNamespace(workflow_run_id="wr_prior")
    )
    mock_app.DATABASE.workflow_params.record_superseded_build_test_run = AsyncMock()
    monkeypatch.setattr(runtime, "app", mock_app)
    monkeypatch.setattr(runtime, "_SUPERSEDE_RELEASE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS", 0.0)
    return mock_app


@pytest.fixture
def attached_build_test_ctx(monkeypatch: pytest.MonkeyPatch) -> AgentContext:
    @asynccontextmanager
    async def _attach(_ctx: AgentContext) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(runtime, "mcp_browser_context", _attach)
    monkeypatch.setattr(runtime, "_drop_browser_session_id_at_its_fixed_deadline", AsyncMock())
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"
    return ctx


async def _acquire(ctx: AgentContext, *, chat_id: str | None = "wcc_1") -> dict[str, Any] | None:
    return await runtime.verify_build_test_browser_session_by_attaching(ctx, copilot_chat_id=chat_id)


@pytest.mark.asyncio
async def test_an_unheld_session_dispatches_without_resolving_any_run(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    mock_app = _install_occupancy_app(monkeypatch, sessions=[_session_row(runnable_id=None)])

    assert await _acquire(attached_build_test_ctx) is None
    assert attached_build_test_ctx.browser_session_id == "bs_live"
    mock_app.DATABASE.workflow_runs.get_workflow_run.assert_not_awaited()
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_not_awaited()


@pytest.mark.asyncio
async def test_this_chats_older_test_run_is_superseded_and_the_same_session_is_reused(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior"), _session_row(runnable_id=None)],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )

    assert await _acquire(attached_build_test_ctx) is None
    assert attached_build_test_ctx.browser_session_id == "bs_live"
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_awaited_once_with(
        "wr_prior", failure_reason=SUPERSEDED_BY_NEWER_TEST_REASON
    )
    mock_app.PERSISTENT_SESSIONS_MANAGER.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_supersede_whose_owner_never_releases_the_lease_reports_occupied(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior")],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )

    result = await _acquire(attached_build_test_ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "owner", "build_test_calls_in_response", "expected_occupier_run_id"),
    [
        pytest.param(
            _session_row(runnable_id="wr_prior"),
            _owner_run(copilot_session_id="wcc_other"),
            1,
            "wr_prior",
            id="foreign-chat",
        ),
        pytest.param(
            _session_row(runnable_id="wr_prior"),
            _owner_run(copilot_session_id=None),
            1,
            "wr_prior",
            id="no-chat-identity",
        ),
        pytest.param(
            _session_row(runnable_id="wr_prior"),
            _owner_run(copilot_session_id="wcc_1", status=WorkflowRunStatus.completed),
            1,
            "wr_prior",
            id="terminal-owner",
        ),
        pytest.param(
            _session_row(runnable_id="wr_prior"),
            _owner_run(copilot_session_id="wcc_1", status=WorkflowRunStatus.paused),
            1,
            "wr_prior",
            id="paused-owner",
        ),
        pytest.param(
            _session_row(runnable_id="wr_prior"),
            _owner_run(copilot_session_id="wcc_1"),
            2,
            "wr_prior",
            id="sibling-dispatched-this-response",
        ),
        pytest.param(_session_row(runnable_id="tsk_1", runnable_type="task"), None, 1, None, id="task-owner"),
    ],
)
async def test_an_occupier_this_turn_may_not_supersede_is_reported_without_a_cancel(
    monkeypatch: pytest.MonkeyPatch,
    attached_build_test_ctx: AgentContext,
    session: PersistentBrowserSession,
    owner: SimpleNamespace | None,
    build_test_calls_in_response: int,
    expected_occupier_run_id: str | None,
) -> None:
    attached_build_test_ctx.build_test_tool_calls_in_model_response = build_test_calls_in_response
    mock_app = _install_occupancy_app(monkeypatch, sessions=[session], owner=owner)

    result = await _acquire(attached_build_test_ctx)

    assert result is not None
    expected_failure = {
        "state": "occupied",
        "browser_session_id": "bs_live",
        "retry_action": "test_end_to_end",
    }
    if expected_occupier_run_id is not None:
        expected_failure["occupier_run_id"] = expected_occupier_run_id
    assert result["data"]["build_test_connect_failure"] == expected_failure
    if expected_occupier_run_id is None:
        assert "tsk_1" not in result["error"]
    else:
        assert expected_occupier_run_id in result["error"]
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_not_awaited()
    mock_app.PERSISTENT_SESSIONS_MANAGER.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_supersede_cancel_write_reports_occupied_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior")],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final = AsyncMock(
        side_effect=SQLATimeoutError("cancel write failed")
    )

    result = await _acquire(attached_build_test_ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"


@pytest.mark.asyncio
async def test_the_release_wait_reports_occupied_before_the_turn_deadline_fires(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior")],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )
    monkeypatch.setattr(runtime, "_SUPERSEDE_RELEASE_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(runtime, "_SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS", 1.0)
    monkeypatch.setattr(runtime, "_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS", 0.05)

    async with asyncio.timeout(1.5) as deadline:
        attached_build_test_ctx.model_stream_deadline = deadline
        result = await _acquire(attached_build_test_ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_holder_that_ended_on_its_own_is_not_recorded_as_superseded(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    """The conditional cancel returns nothing when the holder already reached terminal, so this turn
    superseded nothing and must not leave a record saying it did."""
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior"), _session_row(runnable_id=None)],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final = AsyncMock(return_value=None)

    assert await _acquire(attached_build_test_ctx) is None
    mock_app.DATABASE.workflow_params.record_superseded_build_test_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_child_run_of_a_live_parent_is_never_superseded(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    """Descendant runs inherit the parent's chat id, so a child clears every other guard. Ending it
    would leave the parent running and reporting a failed child block it did not cause."""
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior")],
        owner=_owner_run(copilot_session_id="wcc_1", parent_workflow_run_id="wr_parent"),
    )

    result = await _acquire(attached_build_test_ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_spent_release_budget_leaves_the_older_test_running(
    monkeypatch: pytest.MonkeyPatch, attached_build_test_ctx: AgentContext
) -> None:
    """Cancelling with no time left to watch the lease drop would end the older test and start
    nothing in its place, so the older test keeps the browser and this dispatch reports occupied."""
    mock_app = _install_occupancy_app(
        monkeypatch,
        sessions=[_session_row(runnable_id="wr_prior")],
        owner=_owner_run(copilot_session_id="wcc_1"),
    )
    monkeypatch.setattr(runtime, "_SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS", 30.0)

    async with asyncio.timeout(0.5) as deadline:
        attached_build_test_ctx.model_stream_deadline = deadline
        result = await _acquire(attached_build_test_ctx)

    assert result is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "occupied"
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_suspended_turn_deadline_leaves_the_release_wait_unclamped(
    attached_build_test_ctx: AgentContext,
) -> None:
    async with asyncio.timeout(5.0) as deadline:
        deadline.reschedule(None)
        attached_build_test_ctx.model_stream_deadline = deadline
        budget = runtime._supersede_release_budget_seconds(attached_build_test_ctx)

    assert budget == runtime._SUPERSEDE_RELEASE_WAIT_SECONDS
