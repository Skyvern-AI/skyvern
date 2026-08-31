from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import TimeoutError as SQLATimeoutError

import skyvern.forge.sdk.copilot.agent as agent_module
from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent import _resolve_live_browser_session_id
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

_UNSET_UPSTREAM = "<unset>"


class _FakeBrowser:
    def is_connected(self) -> bool:
        return True


class _FakeBrowserContext:
    browser = _FakeBrowser()
    _impl_obj = SimpleNamespace(_close_was_called=False, _closed=False)


def _request(browser_session_id: str | None = None, wpid: str = "wpid-1") -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id=wpid,
        workflow_id="wf-1",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        browser_session_id=browser_session_id,
        message="hi",
        workflow_yaml="title: x",
    )


def _session(
    *,
    status: str = "running",
    browser_address: str | None = "wss://example/cdp",
    upstream_cdp_url: str | None = _UNSET_UPSTREAM,
) -> PersistentBrowserSession:
    """The real model rather than a stand-in: whether a session is usable is decided by its
    upstream endpoint, which the session worker writes together with the address."""
    now = datetime.now(UTC)
    return PersistentBrowserSession(
        persistent_browser_session_id="pbs_test",
        organization_id="org-1",
        status=status,
        browser_address=browser_address,
        upstream_cdp_url=("ws://10.0.0.7:9223/devtools/browser/b-1" if browser_address else None)
        if upstream_cdp_url is _UNSET_UPSTREAM
        else upstream_cdp_url,
        created_at=now,
        modified_at=now,
    )


def _running_session(browser_address: str = "wss://example/cdp") -> PersistentBrowserSession:
    return _session(browser_address=browser_address)


@pytest.mark.asyncio
async def test_no_id_returns_none_and_does_not_call_db(monkeypatch: pytest.MonkeyPatch) -> None:
    debug_mock = AsyncMock(side_effect=AssertionError("DB must not be touched when no id is supplied"))
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(get_debug_session_by_browser_session_id=debug_mock),
    )

    result = await _resolve_live_browser_session_id(_request(browser_session_id=None), organization_id="org-1")

    assert result is None
    debug_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_id_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(get_debug_session_by_browser_session_id=AsyncMock(return_value=None)),
    )

    result = await _resolve_live_browser_session_id(_request(browser_session_id="pbs_unknown"), organization_id="org-1")

    assert result is None


@pytest.mark.asyncio
async def test_wrong_workflow_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-other"),
            ),
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_foreign", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_persistent_row_missing_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(
            get_session=AsyncMock(return_value=None),
            can_probe_registered_browser_state=lambda: False,
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_unknown_persistent", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_status_in_final_state_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(
            get_session=AsyncMock(
                return_value=_session(status="completed"),
            ),
            can_probe_registered_browser_state=lambda: False,
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_done", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_browser_address_unset_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(
            get_session=AsyncMock(
                return_value=_session(browser_address=None),
            ),
            can_probe_registered_browser_state=lambda: False,
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_booting", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_default_manager_registered_browser_state_allows_missing_browser_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    get_browser_state = AsyncMock(return_value=SimpleNamespace(browser_context=_FakeBrowserContext()))
    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=_session(browser_address=None)),
        get_browser_state=get_browser_state,
        can_probe_registered_browser_state=lambda: True,
    )
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_booted_local", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result == "pbs_booted_local"
    get_browser_state.assert_awaited_once_with(session_id="pbs_booted_local", organization_id="org-1")


@pytest.mark.asyncio
async def test_default_manager_unattachable_registered_browser_state_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    get_browser_state = AsyncMock(return_value=SimpleNamespace(browser_context=None))
    manager = SimpleNamespace(
        get_session=AsyncMock(return_value=_session(browser_address=None)),
        get_browser_state=get_browser_state,
        can_probe_registered_browser_state=lambda: True,
    )
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_not_ready", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None
    get_browser_state.assert_awaited_once_with(session_id="pbs_not_ready", organization_id="org-1")


@pytest.mark.asyncio
async def test_owned_and_running_returns_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(
            get_session=AsyncMock(return_value=_running_session()),
            can_probe_registered_browser_state=lambda: False,
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_live", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result == "pbs_live"


@pytest.mark.asyncio
async def test_db_exception_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ownership fails closed, unlike liveness: an org/workflow binding that could not be confirmed
    is never reused, while a liveness lookup that could not complete keeps the session."""
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(side_effect=RuntimeError("transient DB error")),
        ),
    )

    result = await _resolve_live_browser_session_id(_request(browser_session_id="pbs_x"), organization_id="org-1")

    assert result is None


@pytest.mark.asyncio
async def test_ensure_browser_session_recovers_from_stale_supplied_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supplied id whose chromium has died must be replaced before the caller hands it onward.
    The attach is what discovers that: it retires the dead id and the create path mints a fresh
    session, so `mcp_browser_context` never raises on the first browser tool call."""
    from skyvern.forge.sdk.copilot import runtime as runtime_module
    from skyvern.forge.sdk.copilot.context import CopilotContext

    monkeypatch.setattr(runtime_module, "_BROWSER_BOOT_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(runtime_module, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.02)

    # First get_browser_state returns a stale row (no browser_context).
    # After auto-create, the second call returns a healthy state so the
    # post-create boot wait can complete.
    fresh_state = SimpleNamespace(browser_context=_FakeBrowserContext())
    get_browser_state_mock = AsyncMock(side_effect=[SimpleNamespace(browser_context=None), fresh_state])
    create_session_mock = AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_fresh"))

    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        SimpleNamespace(
            get_browser_state=get_browser_state_mock,
            create_session=create_session_mock,
        ),
    )

    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wpid-1",
        workflow_yaml="",
        browser_session_id="pbs_stale",
        stream=SimpleNamespace(),
        api_key="sk-test",
        user_message="",
        workflow_copilot_chat_id="chat-1",
    )

    result = await runtime_module.verify_browser_session_by_attaching(ctx)

    assert result is None
    assert ctx.browser_session_id == "pbs_fresh"
    create_session_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_liveness_lookup_failure_keeps_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ownership is established, so a liveness lookup that could not complete is not evidence
    the browser is gone. Returning None here is what discarded a healthy session under pool
    exhaustion, before ensure_browser_session ever got to classify it."""
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "get_session",
        AsyncMock(side_effect=SQLATimeoutError("QueuePool limit of size 20 overflow 20 reached")),
    )

    result = await _resolve_live_browser_session_id(_request(browser_session_id="pbs_live"), organization_id="org-1")

    assert result == "pbs_live"


@pytest.mark.asyncio
async def test_unavailable_health_signal_keeps_the_session_at_the_first_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered-state check feeds this gate's liveness decision. Collapsing an unavailable
    connectivity signal to "not usable" discards an owned, running session before the probe runs."""

    class _RaisingBrowser:
        def is_connected(self) -> bool:
            raise ConnectionError("cdp endpoint unreachable")

    class _RaisingContext:
        def __init__(self) -> None:
            self.browser = _RaisingBrowser()
            self._impl_obj = SimpleNamespace(_close_was_called=False, _closed=False)

    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "get_session",
        AsyncMock(return_value=_session(status="running", browser_address=None)),
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "can_probe_registered_browser_state",
        lambda: True,
    )
    state = SimpleNamespace(browser_context=_RaisingContext())
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "get_browser_state", AsyncMock(return_value=state))

    result = await _resolve_live_browser_session_id(_request(browser_session_id="pbs_live"), organization_id="org-1")

    assert result == "pbs_live"


def test_a_session_row_carries_the_relays_unreachable_mark() -> None:
    now = datetime.now(UTC)
    row = PersistentBrowserSession.model_validate(
        SimpleNamespace(
            persistent_browser_session_id="pbs_x",
            organization_id="org-1",
            status="running",
            browser_address=None,
            upstream_cdp_url=None,
            cdp_unreachable_at=now,
            created_at=now,
            modified_at=now,
        )
    )
    assert row.cdp_unreachable_at == now
    assert (
        PersistentBrowserSession(
            persistent_browser_session_id="pbs_y", organization_id="org-1", created_at=now, modified_at=now
        ).cdp_unreachable_at
        is None
    )


@pytest.mark.asyncio
async def test_a_session_the_relay_declared_unreachable_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE,
        "debug",
        SimpleNamespace(
            get_debug_session_by_browser_session_id=AsyncMock(
                return_value=SimpleNamespace(workflow_permanent_id="wpid-1"),
            ),
        ),
    )
    dead = _session()
    dead.cdp_unreachable_at = datetime.now(UTC)
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "get_session", AsyncMock(return_value=dead))
    monkeypatch.setattr(agent_module, "_manager_can_probe_registered_browser_state", lambda: False)

    result = await _resolve_live_browser_session_id(_request(browser_session_id="pbs_test"), organization_id="org-1")

    assert result is None
