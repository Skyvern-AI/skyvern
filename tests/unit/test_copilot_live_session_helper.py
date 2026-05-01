from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent import _resolve_live_browser_session_id
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


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


def _running_session(browser_address: str = "wss://example/cdp") -> SimpleNamespace:
    return SimpleNamespace(status="running", browser_address=browser_address)


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
        SimpleNamespace(get_session=AsyncMock(return_value=None)),
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
                return_value=SimpleNamespace(status="completed", browser_address="wss://example/cdp"),
            ),
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
                return_value=SimpleNamespace(status="running", browser_address=None),
            ),
        ),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_booting", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result is None


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
        SimpleNamespace(get_session=AsyncMock(return_value=_running_session())),
    )

    result = await _resolve_live_browser_session_id(
        _request(browser_session_id="pbs_live", wpid="wpid-1"),
        organization_id="org-1",
    )

    assert result == "pbs_live"


@pytest.mark.asyncio
async def test_db_exception_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
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
    """If `_resolve_live_browser_session_id` returns an id whose DB row is
    healthy but whose chromium has died, `ensure_browser_session` must
    auto-create a fresh session rather than short-circuit and let
    `mcp_browser_context` raise on the first browser tool call."""
    from skyvern.forge.sdk.copilot import runtime as runtime_module
    from skyvern.forge.sdk.copilot.context import CopilotContext

    monkeypatch.setattr(runtime_module, "_BROWSER_BOOT_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(runtime_module, "_BROWSER_BOOT_POLL_INTERVAL_SECONDS", 0.02)

    # First get_browser_state returns a stale row (no browser_context).
    # After auto-create, the second call returns a healthy state so the
    # post-create boot wait can complete.
    fresh_state = SimpleNamespace(browser_context=object())
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

    result = await runtime_module.ensure_browser_session(ctx)

    assert result is None
    assert ctx.browser_session_id == "pbs_fresh"
    create_session_mock.assert_awaited_once()
