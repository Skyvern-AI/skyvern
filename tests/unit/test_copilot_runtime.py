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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.exc import TimeoutError as SQLATimeoutError
from structlog.testing import capture_logs

from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot import mcp_adapter, runtime
from skyvern.forge.sdk.copilot.build_test_connect_failure import SUPERSEDED_BY_NEWER_TEST_REASON
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.runtime import (
    BROWSER_TOOLS_UNAVAILABLE_ERROR,
    AgentContext,
    ensure_browser_session,
    mcp_browser_context,
    mcp_to_copilot,
)
from skyvern.forge.sdk.copilot.tools import run_execution
from skyvern.forge.sdk.copilot.unrecoverable_tool_error import _is_unrecoverable_browser_session_error
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.webeye.browser_errors import (
    BrowserCdpConnectionError,
    BrowserRetryableCdpError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
)
from skyvern.webeye.persistent_sessions_manager import (
    BrowserOperation,
    BrowserRetirement,
    BrowserSessionCreditAdmissionRefusal,
)
from tests.unit.copilot_test_helpers import (
    TURN_EXIT_PATHS,
    make_copilot_ctx,
    run_concurrent_turns_on_one_session,
    run_turn_attaching_during_release,
    run_turn_cancelled_during_cleanup,
    run_turn_to_exit,
)
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


@pytest.mark.asyncio
async def test_a_turn_without_browser_authority_is_told_so_not_that_creation_failed() -> None:
    ctx = _make_ctx()
    ctx.copilot_config = CopilotConfig(browser_tools_available=False)

    assert await ensure_browser_session(ctx) == {"ok": False, "error": BROWSER_TOOLS_UNAVAILABLE_ERROR}


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
@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (
            runtime.ensure_browser_session,
            {
                "ok": False,
                "error": ("Browser session did not start because credits are exhausted. Upgrade your plan in Billing."),
                "data": {
                    "browser_session_acquisition_failure": {
                        "state": "billing_credit_admission_refusal",
                        "retry_action": None,
                    }
                },
            },
        ),
        (
            runtime.ensure_build_test_browser_session,
            {
                "ok": False,
                "error": (
                    "Build test did not start because credits are exhausted. "
                    "No browser or run started. Upgrade your plan in Billing."
                ),
                "data": {
                    "overall_status": "setup_failed",
                    "failure_reason": (
                        "Build test did not start because credits are exhausted. "
                        "No browser or run started. Upgrade your plan in Billing."
                    ),
                    "browser_session_id": None,
                    "build_test_connect_failure": {
                        "state": "billing_credit_admission_refusal",
                        "workflow_run_id": None,
                        "workflow_run_block_id": None,
                        "task_id": None,
                        "browser_session_id": None,
                        "occupier_run_id": None,
                        "diagnostic": None,
                        "retry_action": None,
                    },
                    "blocks": [],
                },
            },
        ),
    ],
)
async def test_billing_credit_refusal_survives_both_acquisition_callers(
    monkeypatch: pytest.MonkeyPatch,
    caller: Callable[[AgentContext], Awaitable[object]],
    expected: object,
) -> None:
    manager = MagicMock()
    manager.create_session = AsyncMock(side_effect=BrowserSessionCreditAdmissionRefusal())
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    result = await caller(ctx)

    assert result == expected
    assert ctx.browser_session_id is None
    manager.create_session.assert_awaited_once()


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
    assert ctx.attached_browser_drivers == {"bs_1": runtime.AttachedBrowserDriver("bs_1", ready_state)}


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
async def test_attach_loss_waits_for_source_promotion_before_retiring_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attach-loss oracle is a browser-session mutation spine, so it must share the
    promotion lock instead of invalidating source provenance while persistence is in flight."""
    lookup_started = asyncio.Event()

    async def _report_dead(*_args: object, **_kwargs: object) -> None:
        lookup_started.set()
        return None

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=_report_dead)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_validated_source"
    await ctx.browser_session_recovery_lock.acquire()

    async def _attach() -> None:
        async with mcp_browser_context(ctx):
            pass

    attach = asyncio.create_task(_attach())
    await asyncio.wait_for(lookup_started.wait(), timeout=1)
    completed_while_promotion_held, _ = await asyncio.wait({attach}, timeout=0.05)
    session_while_promotion_held = ctx.browser_session_id
    attach_waited_for_promotion = not completed_while_promotion_held
    ctx.browser_session_recovery_lock.release()

    with pytest.raises(runtime.CopilotBrowserSessionUnavailable):
        await attach
    assert session_while_promotion_held == "bs_validated_source"
    assert attach_waited_for_promotion
    assert ctx.browser_session_id is None


def _closed_report(cause: BaseException | None) -> BrowserTargetClosedError:
    report = BrowserTargetClosedError("Browser session disconnected during the run and could not reconnect.")
    report.__cause__ = cause
    return report


def _install_closed_report(monkeypatch: pytest.MonkeyPatch, report: BrowserTargetClosedError) -> None:
    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=report)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(None, id="probe-dead"),
        pytest.param(
            PlaywrightTargetClosedError("Target page, context or browser has been closed"), id="native-target-closed"
        ),
    ],
)
async def test_attach_retires_a_session_the_manager_reports_closed(
    monkeypatch: pytest.MonkeyPatch, cause: BaseException | None
) -> None:
    _install_closed_report(monkeypatch, _closed_report(cause))
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_dead"

    with pytest.raises(runtime.CopilotBrowserSessionUnavailable):
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(PlaywrightTimeoutError("connect_over_cdp: Timeout 30000ms exceeded"), id="playwright-timeout"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(BrowserTimeoutError("deadline"), id="browser-timeout"),
        pytest.param(BrowserRetryableCdpError("transient disconnect"), id="retryable-cdp"),
        pytest.param(BrowserCdpConnectionError("CDP connection failed"), id="cdp-connection"),
        pytest.param(PlaywrightError("connect ECONNREFUSED 127.0.0.1:9222"), id="refused-reconnect"),
        pytest.param(OSError("[Errno 8] nodename nor servname provided"), id="name-resolution"),
        pytest.param(PlaywrightError("WebSocket error: socket hang up"), id="websocket-dropped"),
    ],
)
async def test_attach_keeps_a_session_whose_closed_report_came_from_a_timeout_or_transport_failure(
    monkeypatch: pytest.MonkeyPatch, cause: BaseException
) -> None:
    _install_closed_report(monkeypatch, _closed_report(cause))
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_live"

    with pytest.raises(BrowserTargetClosedError):
        async with mcp_browser_context(ctx):
            pass

    assert ctx.browser_session_id == "bs_live"


@pytest.mark.asyncio
@pytest.mark.parametrize("next_lookup_queued_behind", [False, True])
@pytest.mark.parametrize(
    ("cause", "retires"),
    [
        pytest.param(None, True, id="positive-closed"),
        pytest.param(TimeoutError(), False, id="timeout-closed"),
        pytest.param(BrowserCdpConnectionError("CDP connection failed"), False, id="transport-closed"),
    ],
)
async def test_a_closed_report_landing_after_its_caller_gave_up_reaches_the_next_attach_once(
    monkeypatch: pytest.MonkeyPatch, cause: BaseException | None, retires: bool, next_lookup_queued_behind: bool
) -> None:
    """The manager drops its disconnected handle once it has answered closed, so every later lookup
    of that session only sees an ambiguous connect error."""
    monkeypatch.setattr(runtime, "_ABANDONED_CLOSED_FACTS", {})
    monkeypatch.setattr(runtime, "_ABANDONED_BROWSER_STATE_RESOLVES", set())
    determination_started, chrome_answered = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    async def _get_browser_state(**_kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            determination_started.set()
            await chrome_answered.wait()
            raise _closed_report(cause)
        await chrome_answered.wait()
        raise BrowserCdpConnectionError("CDP connection failed")

    mock_manager = MagicMock()
    mock_manager.get_browser_state = AsyncMock(side_effect=_get_browser_state)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = mock_manager
    monkeypatch.setattr(runtime, "app", mock_app)
    ctx = _make_ctx()
    ctx.browser_session_id = "bs_chat"

    async def _attach() -> None:
        async with mcp_browser_context(ctx):
            pass

    abandoned = asyncio.ensure_future(_attach())
    await determination_started.wait()
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    if next_lookup_queued_behind:
        next_attach = asyncio.ensure_future(_attach())
        while calls["n"] < 2:
            await asyncio.sleep(0)
        chrome_answered.set()
    else:
        chrome_answered.set()
        while runtime._ABANDONED_BROWSER_STATE_RESOLVES:
            await asyncio.sleep(0)
        next_attach = asyncio.ensure_future(_attach())
    expected_error = runtime.CopilotBrowserSessionUnavailable if retires else BrowserCdpConnectionError
    with pytest.raises(expected_error):
        await next_attach
    assert ctx.browser_session_id == (None if retires else "bs_chat")

    ctx.browser_session_id = "bs_chat"
    with pytest.raises(BrowserCdpConnectionError):
        await _attach()
    assert ctx.browser_session_id == "bs_chat", "the kept fact answers one lookup, not every later one"


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
    mock_manager.supports_evict_and_reconnect = MagicMock(return_value=False)
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
        "evict_cached_browser_state",
        "resolve_persistent_browser_state",
        "resolve_browser_state_for_context",
        "_resolve_self_heal_browser_state",
        "ensure_browser_session",
        "get_browser_state",
        "create_session",
        "close_session",
    }
)


# Three LIFECYCLE calls are legitimately bounded and recorded in BOUNDS.md: the create-and-boot
# poll, the quiet close, and the turn-exit driver release. Every other clock around a manager call
# is the shape decision 0032 forbids, so adding one means adding its constant here.
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
    # A shield is transparent to this rule: the bound still lands on the manager call inside it.
    if isinstance(node.func, ast.Attribute) and node.func.attr == "shield" and node.args:
        return _called_name(node.args[0])
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _calls_in_own_scope(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[ast.Call]:
    """Calls this function makes itself; a nested function owns its own calls."""
    calls: list[ast.Call] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


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
            await runtime.retire_browser_session_id(ctx, ctx.browser_session_id)
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
        pytest.param(
            _closed_report(PlaywrightTimeoutError("attach timed out")), "cdp_connect_failed", id="timed-out-closed"
        ),
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
            await runtime.retire_browser_session_id(ctx, ctx.browser_session_id)
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


@pytest.mark.asyncio
async def test_restored_session_is_consumed_and_the_authored_navigation_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner hands back a session it proved answers, so the caller attaches to it rather than
    minting a replacement, and the work already authored against the old one is still there."""
    authored_yaml = "blocks:\n  - block_type: goto\n    url: https://example.com/search\n"
    lookup = _SharedSessionLookup()
    manager = _install_dispatch_stack(monkeypatch, lookup)
    manager.get_session = AsyncMock(return_value=_session_row(runnable_id=None))
    monkeypatch.setattr(runtime, "_drop_browser_session_id_at_its_fixed_deadline", AsyncMock())

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_restored"
    ctx.workflow_yaml = authored_yaml

    assert await runtime.verify_build_test_browser_session_by_attaching(ctx) is None

    result, _ = await _dispatch_browser_tool(ctx)

    assert ctx.browser_session_id == "bs_restored"
    assert ctx.workflow_yaml == authored_yaml
    assert '"result": 7' in result.content[0].text
    assert "browser session was lost" not in result.content[0].text
    manager.create_session.assert_not_awaited()
    assert lookup.calls >= 2


@pytest.mark.asyncio
async def test_owner_refusing_the_replacement_retires_the_session_and_leaves_a_route_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proven-defunct upstream has to reach the user explicitly AND release the held id, or the
    same authoring request re-attaches to the same corpse for the rest of the chat."""
    authored_yaml = "blocks:\n  - block_type: goto\n    url: https://example.com/search\n"
    manager = MagicMock()
    manager.get_browser_state = AsyncMock(
        side_effect=BrowserTargetClosedError("Replacement browser state did not answer a CDP roundtrip.")
    )
    _admit_mock_browser_operations(manager)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(runtime, "app", mock_app)

    ctx = _make_ctx()
    ctx.browser_session_id = "bs_unusable"
    ctx.workflow_yaml = authored_yaml

    result = await runtime.verify_build_test_browser_session_by_attaching(ctx)

    assert result is not None
    assert result["ok"] is False
    assert result["data"]["build_test_connect_failure"] == {
        "state": "already_closed",
        "browser_session_id": "bs_unusable",
        "retry_action": "test_end_to_end",
    }
    assert "extracted" not in result["data"]
    assert result["data"].get("overall_status") != "completed"
    assert ctx.workflow_yaml == authored_yaml
    assert not ctx.browser_session_id

    provision = AsyncMock(return_value=None)
    monkeypatch.setattr(runtime, "ensure_build_test_browser_session", provision)
    assert await runtime.verify_build_test_browser_session_by_attaching(ctx) is None
    provision.assert_awaited_once()


_TURN_EXIT_TERMINAL_REASONS = {
    "normal": None,
    "model_error": "unexpected_error",
    "deadline": "timeout",
    "cancel": "cancel",
}


def _release_manager(*, evict: AsyncMock | None = None, supported: bool = True) -> MagicMock:
    manager = MagicMock()
    manager.supports_evict_and_reconnect = MagicMock(return_value=supported)
    manager.evict_cached_browser_state = evict or AsyncMock(return_value=True)
    manager.close_session = AsyncMock()
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", TURN_EXIT_PATHS)
async def test_every_turn_exit_releases_the_driver_it_attached(monkeypatch: pytest.MonkeyPatch, exit_path: str) -> None:
    attached = MagicMock()
    manager = _release_manager()

    with capture_logs() as logs:
        result, _ = await run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path=exit_path,
            session_id="pbs_turn",
            browser_state=attached,
        )

    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_turn",
        "org-1",
        expected=attached,
        detach_remote_driver=True,
        only_if_unleased=True,
    )
    manager.close_session.assert_not_awaited()
    released = [log for log in logs if log["event"] == "copilot_browser_driver_released"]
    assert [(log["session_id"], log["had_cached_driver"]) for log in released] == [("pbs_turn", True)]
    assert result is not None
    assert result.turn_outcome.terminal_reason == _TURN_EXIT_TERMINAL_REASONS[exit_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", TURN_EXIT_PATHS)
async def test_every_turn_exit_releases_a_click_listener_its_post_hook_never_reached(
    monkeypatch: pytest.MonkeyPatch, exit_path: str
) -> None:
    """A click cancelled between its pre-hook and post-hook never reaches the post-hook's release, so
    the turn's own exit is the last place its listener on the persistent page can be removed."""
    detached: list[str] = []

    async def _armed_click_that_never_reported(ctx: AgentContext) -> None:
        ctx.pending_scout_challenge_detachers.append(lambda: detached.append("framenavigated"))

    await run_turn_to_exit(
        monkeypatch,
        manager=_release_manager(),
        exit_path=exit_path,
        browser_state=MagicMock(),
        on_attached=_armed_click_that_never_reported,
    )

    assert detached == ["framenavigated"]


@pytest.mark.asyncio
async def test_a_cancel_during_prior_run_hydration_still_releases_the_driver_it_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the origin run attaches to that run's browser before the model loop starts; a
    cancel landing there must still reach the finalizer with the attach on record."""
    attached = MagicMock()
    manager = _release_manager()

    with capture_logs() as logs:
        result, raised = await run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="cancel",
            session_id="pbs_origin_run",
            browser_state=attached,
            attach_through="prior_run_hydration",
        )

    assert result is None
    assert isinstance(raised, asyncio.CancelledError)
    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_origin_run",
        "org-1",
        expected=attached,
        detach_remote_driver=True,
        only_if_unleased=True,
    )
    manager.close_session.assert_not_awaited()
    released = [log for log in logs if log["event"] == "copilot_browser_driver_released"]
    assert [(log["session_id"], log["had_cached_driver"]) for log in released] == [("pbs_origin_run", True)]
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "pbs_turn"])
async def test_a_turn_that_attached_nothing_releases_nothing(
    monkeypatch: pytest.MonkeyPatch, session_id: str | None
) -> None:
    """A turn that never attached must not retire a generation a concurrent turn is driving."""
    manager = _release_manager()

    await run_turn_to_exit(monkeypatch, manager=manager, exit_path="normal", session_id=session_id, browser_state=None)

    manager.evict_cached_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_manager_that_cannot_reconnect_after_eviction_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OSS default manager drives the browser in-process: detaching its driver would strand
    the browser and leave the session uncacheable for the rest of its life."""
    manager = _release_manager(supported=False)

    await run_turn_to_exit(monkeypatch, manager=manager, exit_path="normal", browser_state=MagicMock())

    manager.evict_cached_browser_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "hangs", "cancelled"])
@pytest.mark.parametrize("exit_path", TURN_EXIT_PATHS)
async def test_a_failed_release_does_not_replace_the_turns_own_terminal(
    monkeypatch: pytest.MonkeyPatch, failure: str, exit_path: str
) -> None:
    detached = asyncio.Event()

    async def _hang(*args: object, **kwargs: object) -> bool:
        await asyncio.sleep(runtime._SESSION_CLEANUP_TIMEOUT_SECONDS * 4)
        detached.set()
        return True

    evicts = {
        "raises": AsyncMock(side_effect=RuntimeError("evict exploded")),
        "hangs": AsyncMock(side_effect=_hang),
        "cancelled": AsyncMock(side_effect=asyncio.CancelledError()),
    }
    monkeypatch.setattr(runtime, "_SESSION_CLEANUP_TIMEOUT_SECONDS", 0.01)

    with capture_logs() as logs:
        result, raised = await run_turn_to_exit(
            monkeypatch,
            manager=_release_manager(evict=evicts[failure]),
            exit_path=exit_path,
            browser_state=MagicMock(),
        )

    assert [log for log in logs if log["event"] == "copilot_browser_driver_released"] == []
    assert raised is None
    assert result is not None
    assert result.turn_outcome.terminal_reason == _TURN_EXIT_TERMINAL_REASONS[exit_path]
    if failure == "hangs":
        # The detach outliving the caller's bound is the shield: without it the timed-out wait
        # cancels the evict and strands the popped cache entry this release exists to drop.
        await asyncio.wait_for(detached.wait(), timeout=2)


@pytest.mark.asyncio
async def test_a_resolve_for_another_session_is_released_by_the_turn_that_attached_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool resolving a run's own session caches that driver on this pod exactly like the chat
    session's, so the turn that attached it is the only thing that will ever hand it back."""
    attached = MagicMock()
    manager = _release_manager()

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        session_id="pbs_turn",
        resolve_session_id="pbs_other",
        browser_state=attached,
    )

    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_other", "org-1", expected=attached, detach_remote_driver=True, only_if_unleased=True
    )


@pytest.mark.asyncio
async def test_a_self_heal_turn_leaves_the_healers_injected_driver_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healer's injected browser state never entered this pod's cache, so evicting it could
    only retire a generation this turn does not own."""
    manager = _release_manager()

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        browser_state=MagicMock(),
        turn_origin=runtime.TurnOrigin.runtime_self_heal,
    )

    manager.evict_cached_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_last_concurrent_turn_on_a_session_releases_its_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring the cached generation cancels its admitted operations, so a turn that exits while
    a sibling turn is still attached to the same session must leave the driver where it is."""
    state = MagicMock()
    manager = _release_manager()

    def _after_first_exit() -> None:
        manager.evict_cached_browser_state.assert_not_awaited()

    await run_concurrent_turns_on_one_session(
        monkeypatch, manager=manager, browser_states=(state, state), after_first_exit=_after_first_exit
    )

    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_turn", "org-1", expected=state, detach_remote_driver=True, only_if_unleased=True
    )


@pytest.mark.asyncio
async def test_a_cancel_delivered_while_the_release_waits_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn cancelled mid-cleanup must still report as cancelled, and the detach it already
    started must finish rather than stranding the popped cache entry."""
    evicting = asyncio.Event()
    detached = asyncio.Event()

    async def _slow_evict(*args: object, **kwargs: object) -> bool:
        evicting.set()
        await asyncio.sleep(0.05)
        detached.set()
        return True

    result, raised = await run_turn_cancelled_during_cleanup(
        monkeypatch,
        manager=_release_manager(evict=AsyncMock(side_effect=_slow_evict)),
        browser_state=MagicMock(),
        evicting=evicting,
    )

    assert result is None
    assert isinstance(raised, asyncio.CancelledError)
    await asyncio.wait_for(detached.wait(), timeout=2)


@pytest.mark.asyncio
async def test_a_turn_that_only_probed_the_session_still_releases_the_driver_it_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The liveness probe attaches through the same cache as every other lookup, so a turn that
    never touched the browser afterwards still owns the driver the probe left behind."""
    attached = MagicMock()
    manager = _release_manager()

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        browser_state=attached,
        attach_through="liveness_probe",
    )

    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_turn", "org-1", expected=attached, detach_remote_driver=True, only_if_unleased=True
    )


@pytest.mark.asyncio
async def test_a_build_test_run_on_the_turns_own_session_records_the_driver_it_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build test that reuses the chat's own browser resolves it outside the tool funnel, so
    without its own recording the turn exits owing a driver nothing released."""
    ctx = make_copilot_ctx(browser_session_id="pbs_turn")
    attached = MagicMock()
    monkeypatch.setattr(run_execution, "resolve_persistent_browser_state", AsyncMock(return_value=attached))

    await run_execution._observe_authored_locators(
        ctx,
        run_session_id="pbs_turn",
        failed_block_code="page.locator('#pay').click()",
    )

    assert list(ctx.attached_browser_drivers) == ["pbs_turn"]
    assert ctx.attached_browser_drivers["pbs_turn"].browser_state is attached


@pytest.mark.asyncio
async def test_a_build_test_run_on_a_separate_session_records_that_session_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_turn")
    attached = MagicMock()
    monkeypatch.setattr(run_execution, "resolve_persistent_browser_state", AsyncMock(return_value=attached))

    await run_execution._observe_authored_locators(
        ctx,
        run_session_id="pbs_minted",
        failed_block_code="page.locator('#pay').click()",
    )

    assert ctx.attached_browser_drivers["pbs_minted"].browser_state is attached


def test_every_copilot_resolve_of_a_cached_browser_state_records_what_it_attached() -> None:
    """The turn-exit release can only retire generations the turn recorded, so a resolve that
    reaches the pod's cache without a recording beside it strands its driver until the session dies."""
    offenders: list[str] = []
    for path in sorted(_COPILOT_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            names = [_called_name(call) for call in _calls_in_own_scope(node)]
            if "resolve_persistent_browser_state" in names and "record_attached_browser_driver" not in names:
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert offenders == [], "Copilot resolves that never record the driver they attached:\n" + "\n".join(offenders)


@pytest.mark.asyncio
async def test_a_turn_that_attached_two_sessions_releases_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording only the newest attach would leave the first session's driver cached for the rest
    of its lifetime — the exact pileup this release exists to stop."""
    first, second = MagicMock(), MagicMock()
    manager = _release_manager()
    states = {"pbs_first": first, "pbs_second": second}
    manager.get_browser_state = AsyncMock(side_effect=lambda session_id, *a, **k: states[session_id])

    async def _attach_a_second_session(ctx: runtime.AgentContext) -> None:
        ctx.browser_session_id = "pbs_second"
        await runtime.resolve_browser_state_for_context(ctx, session_id="pbs_second")

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        session_id="pbs_first",
        browser_state=first,
        on_attached=_attach_a_second_session,
    )

    released = [call.args[0] for call in manager.evict_cached_browser_state.await_args_list]
    assert sorted(released) == ["pbs_first", "pbs_second"]


@pytest.mark.asyncio
async def test_a_second_session_does_not_strand_the_first_sessions_attach_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count left behind by an unreleased session reads as a live sibling turn forever, so every
    later turn on that session would skip its own release."""
    first, second = MagicMock(), MagicMock()
    manager = _release_manager()
    states = {"pbs_first": first, "pbs_second": second}
    manager.get_browser_state = AsyncMock(side_effect=lambda session_id, *a, **k: states[session_id])

    async def _attach_a_second_session(ctx: runtime.AgentContext) -> None:
        ctx.browser_session_id = "pbs_second"
        await runtime.resolve_browser_state_for_context(ctx, session_id="pbs_second")

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        session_id="pbs_first",
        browser_state=first,
        on_attached=_attach_a_second_session,
    )
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}

    later = _release_manager()
    later.get_browser_state = AsyncMock(return_value=first)
    await run_turn_to_exit(monkeypatch, manager=later, exit_path="normal", session_id="pbs_first", browser_state=first)

    later.evict_cached_browser_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_last_turn_out_retires_the_generation_the_pod_now_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling turn that attached a replacement generation and exited first leaves this turn
    holding a superseded state, and evicting that one would cache the live driver for good."""
    superseded, replacement = MagicMock(), MagicMock()
    manager = _release_manager()

    await run_concurrent_turns_on_one_session(
        monkeypatch, manager=manager, browser_states=(superseded, replacement), exit_order=(1, 0)
    )

    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_turn", "org-1", expected=replacement, detach_remote_driver=True, only_if_unleased=True
    )


@pytest.mark.asyncio
async def test_a_resolve_abandoned_by_a_cancel_releases_the_driver_it_landed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup is shielded, so a cancelled turn leaves it running and it caches a driver the
    turn's finalizer never saw recorded."""
    attached = MagicMock()
    resolving, evicted = asyncio.Event(), asyncio.Event()

    async def _slow_resolve(**_kwargs: object) -> MagicMock:
        resolving.set()
        await asyncio.sleep(0.05)
        return attached

    async def _evict(*_args: object, **_kwargs: object) -> bool:
        evicted.set()
        return True

    manager = _release_manager(evict=AsyncMock(side_effect=_evict))
    turn = asyncio.ensure_future(
        run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="normal",
            browser_state=attached,
            get_browser_state=AsyncMock(side_effect=_slow_resolve),
        )
    )
    await resolving.wait()
    turn.cancel()
    await turn

    await asyncio.wait_for(evicted.wait(), timeout=2)
    manager.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_turn", "org-1", expected=attached, detach_remote_driver=True, only_if_unleased=True
    )
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_already_in_flight", [False, True])
async def test_a_turn_attaching_while_the_last_release_is_in_flight_ends_on_a_live_generation(
    monkeypatch: pytest.MonkeyPatch, lookup_already_in_flight: bool
) -> None:
    """A lookup that answers with the generation an evict is retiring hands the new turn a driver
    whose next operation is refused as session_ending, which closes the user's session."""
    retiring, reconnected = MagicMock(name="retiring"), MagicMock(name="reconnected")
    manager = _release_manager()

    await run_turn_attaching_during_release(
        monkeypatch,
        manager=manager,
        browser_states=(retiring, reconnected),
        lookup_already_in_flight=lookup_already_in_flight,
    )

    evicted = [call.kwargs["expected"] for call in manager.evict_cached_browser_state.await_args_list]
    assert evicted == [retiring, reconnected]
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}
    assert runtime._DRIVER_RELEASES_IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_a_cancel_mid_cleanup_still_hands_back_every_session_the_turn_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing one session at a time lets the cancel that lands on the first one skip the rest,
    stranding their drivers for the life of the pod."""
    evicting, both_detached = asyncio.Event(), asyncio.Event()
    detached: list[str] = []

    async def _slow_evict(session_id: str, *_args: object, **_kwargs: object) -> bool:
        evicting.set()
        await asyncio.sleep(0.05)
        detached.append(session_id)
        if len(detached) == 2:
            both_detached.set()
        return True

    async def _attach_a_second_session(ctx: runtime.AgentContext) -> None:
        await runtime.resolve_browser_state_for_context(ctx, session_id="pbs_second")

    _result, raised = await run_turn_cancelled_during_cleanup(
        monkeypatch,
        manager=_release_manager(evict=AsyncMock(side_effect=_slow_evict)),
        browser_state=MagicMock(),
        evicting=evicting,
        on_attached=_attach_a_second_session,
    )

    assert isinstance(raised, asyncio.CancelledError)
    await asyncio.wait_for(both_detached.wait(), timeout=2)
    assert sorted(detached) == ["pbs_second", "pbs_turn"]
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}


@pytest.mark.asyncio
async def test_a_release_that_outlives_its_wait_still_holds_off_the_next_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manager's own detach bound is longer than the turn's wait, so a lookup admitted after the
    wait gives up but before the evict pops the entry would be handed the retiring generation."""
    retiring, reconnected = MagicMock(name="retiring"), MagicMock(name="reconnected")
    evict_may_finish, evicted = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(runtime, "_SESSION_CLEANUP_TIMEOUT_SECONDS", 0.05)

    async def _slow_evict(*_args: object, **_kwargs: object) -> bool:
        if not evicted.is_set():
            await evict_may_finish.wait()
            evicted.set()
        return True

    async def _current_generation(**_kwargs: object) -> MagicMock:
        return reconnected if evicted.is_set() else retiring

    manager = _release_manager(evict=AsyncMock(side_effect=_slow_evict))
    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        browser_state=retiring,
        get_browser_state=AsyncMock(side_effect=_current_generation),
    )
    assert "pbs_turn" in runtime._DRIVER_RELEASES_IN_FLIGHT

    second = asyncio.ensure_future(
        run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="normal",
            browser_state=reconnected,
            get_browser_state=AsyncMock(side_effect=_current_generation),
        )
    )
    await asyncio.sleep(0.05)
    assert not second.done()
    evict_may_finish.set()
    await asyncio.wait_for(second, 5)

    evicted_generations = [call.kwargs["expected"] for call in manager.evict_cached_browser_state.await_args_list]
    assert evicted_generations == [retiring, reconnected]
    assert runtime._DRIVER_RELEASES_IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_a_manager_that_rejects_the_release_call_outright_neither_masks_the_turn_nor_blocks_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous raise (a manager without the newer keyword) happens before any evict task
    exists; it must be logged like any other failed release and must never leave a marker that
    every later resolve of the session would wait on forever."""
    manager = _release_manager()
    manager.evict_cached_browser_state = MagicMock(side_effect=TypeError("unexpected keyword argument"))

    with capture_logs() as logs:
        result, _ = await run_turn_to_exit(monkeypatch, manager=manager, exit_path="normal", browser_state=MagicMock())

    assert result is not None
    assert result.turn_outcome.terminal_reason == _TURN_EXIT_TERMINAL_REASONS["normal"]
    assert [log["error_type"] for log in logs if log["event"] == "Failed to release browser driver"] == ["TypeError"]
    assert runtime._DRIVER_RELEASES_IN_FLIGHT == {}

    later = _release_manager()
    await asyncio.wait_for(
        run_turn_to_exit(monkeypatch, manager=later, exit_path="normal", browser_state=MagicMock()), 5
    )
    later.evict_cached_browser_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cancel_landing_on_the_finalizers_own_await_still_hands_back_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release created inside the awaited gather is cancelled before its first line when the
    cancel lands there, so the attach count never comes down and every later turn on the session
    skips its own release."""
    from skyvern.forge.sdk.copilot import agent as copilot_agent

    manager = _release_manager()
    real_finalize = copilot_agent.finalize_outcome_verification_trace

    def _finalize_then_cancel(*args: object, **kwargs: object) -> None:
        real_finalize(*args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    monkeypatch.setattr(copilot_agent, "finalize_outcome_verification_trace", _finalize_then_cancel)

    _, raised = await run_turn_to_exit(monkeypatch, manager=manager, exit_path="normal", browser_state=MagicMock())
    for _ in range(3):
        await asyncio.sleep(0)

    assert isinstance(raised, asyncio.CancelledError)
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}
    manager.evict_cached_browser_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_release_on_another_session_does_not_reissue_an_overlapping_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a busy pod, steady turn exits on other sessions would otherwise re-issue every slow
    attach in flight and leave it without a bound."""
    other_released = asyncio.Event()
    lookups = 0

    async def _slow_lookup(**_kwargs: object) -> MagicMock:
        nonlocal lookups
        lookups += 1
        await asyncio.wait_for(other_released.wait(), 5)
        return MagicMock(name="pbs_slow generation")

    slow_manager = _release_manager()
    slow_manager.get_browser_state = AsyncMock(side_effect=_slow_lookup)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = slow_manager
    monkeypatch.setattr(runtime, "app", mock_app)

    slow = asyncio.ensure_future(
        runtime.resolve_persistent_browser_state(session_id="pbs_slow", organization_id="org-1")
    )
    await asyncio.sleep(0)
    other = runtime.AttachedBrowserDriver("pbs_other", MagicMock())
    runtime._hold_attached_browser_driver("pbs_other", other.browser_state, new_holder=True)
    await runtime.release_browser_driver_quietly("org-1", other)
    other_released.set()

    assert await asyncio.wait_for(slow, 5) is not None
    assert lookups == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unwind_seconds", [0.02, 2.0], ids=["unwinds_within_the_bound", "outlives_the_bound"])
async def test_a_release_refused_for_the_turns_own_unwinding_operation_waits_it_out(
    monkeypatch: pytest.MonkeyPatch, unwind_seconds: float
) -> None:
    """A cancelled turn's tool task can still be inside its admitted operation when the finalizer
    runs. Taking the manager's refusal as done would leave the driver cached for the session's life,
    and an unwind slower than the turn's cleanup bound must delay the release, not the turn."""
    monkeypatch.setattr(runtime, "_SESSION_CLEANUP_TIMEOUT_SECONDS", 0.05)
    admitted: set[asyncio.Task[object]] = set()
    let_tool_finish = asyncio.Event()
    tool_entered = asyncio.Event()
    attached = MagicMock()
    attached.browser_context = _FakeBrowserContext()

    @asynccontextmanager
    async def _operation(_session_id: str, browser_state: Any) -> AsyncIterator[BrowserOperation]:
        task = asyncio.current_task()
        assert task is not None
        admitted.add(task)
        try:
            yield BrowserOperation(browser_state, BrowserRetirement())
        finally:
            admitted.discard(task)

    async def _evict_only_if_unleased(*_args: object, **_kwargs: object) -> bool:
        return not admitted

    manager = _release_manager(evict=AsyncMock(side_effect=_evict_only_if_unleased))
    manager.browser_operation = _operation
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *_a, **_kw: MagicMock(workflow_run_id=None))
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: object())
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    monkeypatch.setattr(runtime, "register_copilot_session", MagicMock())
    monkeypatch.setattr(runtime, "unregister_copilot_session", MagicMock())

    async def _tool_call(ctx: runtime.AgentContext) -> None:
        async with mcp_browser_context(ctx):
            tool_entered.set()
            await let_tool_finish.wait()

    tool: asyncio.Task[None] | None = None

    async def _start_tool(ctx: runtime.AgentContext) -> None:
        nonlocal tool
        tool = asyncio.ensure_future(_tool_call(ctx))
        await asyncio.wait_for(tool_entered.wait(), 5)
        asyncio.get_running_loop().call_later(unwind_seconds, let_tool_finish.set)

    refused_while_busy: list[bool] = []
    real_evict = manager.evict_cached_browser_state

    async def _evict_recording_busy(*args: object, **kwargs: object) -> bool:
        refused_while_busy.append(bool(admitted))
        return await real_evict(*args, **kwargs)

    manager.evict_cached_browser_state = AsyncMock(side_effect=_evict_recording_busy)

    with capture_logs() as logs:
        result, _ = await run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="cancel",
            browser_state=attached,
            on_attached=_start_tool,
        )
        assert tool is not None
        turn_exited_first = not tool.done()
        await tool
        for _ in range(5):
            await asyncio.sleep(0)

    assert refused_while_busy == [False], "the release evicted while the tool was still admitted"
    if unwind_seconds > 0.05:
        assert turn_exited_first, "the turn's exit waited on the tool past the cleanup bound"

    assert result is not None and result.turn_outcome.terminal_reason == "cancel"
    assert runtime._ATTACHED_TURNS_PER_SESSION == {}
    released = [log["had_cached_driver"] for log in logs if log["event"] == "copilot_browser_driver_released"]
    assert released == [True]


@pytest.mark.asyncio
async def test_a_wedged_operation_on_one_session_does_not_hold_another_sessions_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting on a turn-wide operation set would let a stuck tool on session A strand session B's
    driver for the pod's life."""
    monkeypatch.setattr(runtime, "_SESSION_CLEANUP_TIMEOUT_SECONDS", 0.05)
    wedged = asyncio.Event()
    tool_entered = asyncio.Event()
    first, second = MagicMock(name="first"), MagicMock(name="second")
    for state in (first, second):
        state.browser_context = _FakeBrowserContext()
    states = {"pbs_first": first, "pbs_second": second}

    @asynccontextmanager
    async def _operation(_session_id: str, browser_state: Any) -> AsyncIterator[BrowserOperation]:
        yield BrowserOperation(browser_state, BrowserRetirement())

    manager = _release_manager()
    manager.browser_operation = _operation
    manager.get_browser_state = AsyncMock(side_effect=lambda session_id, *a, **k: states[session_id])
    monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
    monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *_a, **_kw: MagicMock(workflow_run_id=None))
    monkeypatch.setattr(runtime, "set_api_key_override", lambda _key: object())
    monkeypatch.setattr(runtime, "reset_api_key_override", lambda _token: None)
    monkeypatch.setattr(runtime, "register_copilot_session", MagicMock())
    monkeypatch.setattr(runtime, "unregister_copilot_session", MagicMock())

    async def _wedged_tool_on_first(ctx: runtime.AgentContext) -> None:
        async with mcp_browser_context(ctx):
            tool_entered.set()
            await wedged.wait()

    tool: asyncio.Task[None] | None = None

    async def _attach_second_and_wedge_first(ctx: runtime.AgentContext) -> None:
        nonlocal tool
        tool = asyncio.ensure_future(_wedged_tool_on_first(ctx))
        await asyncio.wait_for(tool_entered.wait(), 5)
        ctx.browser_session_id = "pbs_second"
        await runtime.resolve_browser_state_for_context(ctx, session_id="pbs_second")

    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="cancel",
        session_id="pbs_first",
        browser_state=first,
        on_attached=_attach_second_and_wedge_first,
    )
    for _ in range(5):
        await asyncio.sleep(0)

    released = [call.args[0] for call in manager.evict_cached_browser_state.await_args_list]
    assert released == ["pbs_second"], "the idle session waited on the other session's stuck tool"

    wedged.set()
    assert tool is not None
    await tool
    for _ in range(5):
        await asyncio.sleep(0)
    released = [call.args[0] for call in manager.evict_cached_browser_state.await_args_list]
    assert sorted(released) == ["pbs_first", "pbs_second"]
