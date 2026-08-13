import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from skyvern.forge.sdk.routes.streaming import cdp_input
from skyvern.forge.sdk.streaming import registries
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


@pytest.mark.asyncio
async def test_public_workflow_stream_rejects_attach_after_closing_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_late_attach"
    websocket = SimpleNamespace(close=AsyncMock(), send_json=AsyncMock())
    workflow_run = SimpleNamespace(
        workflow_run_id=workflow_run_id,
        organization_id="org_stream",
        status=WorkflowRunStatus.running,
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run)))
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    wait_for_browser_state = AsyncMock(side_effect=AssertionError("late attach reached BrowserState acquisition"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", wait_for_browser_state)
    registries.mark_stream_closing(workflow_run_id)

    await cdp_input.cdp_input_stream(websocket, workflow_run_id, client_id="client_late")

    websocket.close.assert_awaited_once_with(code=4409, reason="workflow_run_closing")
    websocket.send_json.assert_not_awaited()
    wait_for_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_route_releases_adopted_browser_state_on_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait_for_browser_state hands browser_session-scoped ownership to its caller (see the
    docstring on screencast.py's wait_for_browser_state); the caller must give it back via
    release_browser_state on every exit path or the adopted Playwright driver + CDP websocket is
    never closed and leaks for the life of the process."""
    browser_session_id = "pbs_leak_check"
    browser_state = SimpleNamespace(name="adopted_browser_state")
    session = SimpleNamespace(status="running")

    class _FakeCdpSession:
        async def detach(self) -> None:
            return None

    class _FakeContext:
        async def new_cdp_session(self, page: object) -> "_FakeCdpSession":
            return _FakeCdpSession()

    fake_page = SimpleNamespace(context=_FakeContext(), url="https://example.test/")

    websocket = SimpleNamespace(
        close=AsyncMock(),
        send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect()),
    )
    fake_app = SimpleNamespace(
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(get_session=AsyncMock(return_value=session)),
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(cdp_input, "_resolve_working_page", AsyncMock(return_value=fake_page))
    release_browser_state = AsyncMock()
    # raising=False: pre-fix, cdp_input does not import release_browser_state at all.
    monkeypatch.setattr(cdp_input, "release_browser_state", release_browser_state, raising=False)

    await cdp_input.cdp_input_browser_session_stream(websocket, browser_session_id, client_id="client_leak_check")

    release_browser_state.assert_awaited_once_with(browser_state, "browser_session", browser_session_id)


@pytest.mark.asyncio
async def test_browser_session_route_releases_before_input_session_close_can_swallow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ActivePageCdpInputSession.close() catches `except Exception`, which does not catch
    CancelledError. release_browser_state must run before input_session.close() in the finally
    block, matching screenshot.py:416 - otherwise a cancellation during input_session teardown
    skips the release entirely."""
    browser_session_id = "pbs_leak_check_cancel"
    browser_state = SimpleNamespace(name="adopted_browser_state")
    session = SimpleNamespace(status="running")

    class _FakeCdpSession:
        async def detach(self) -> None:
            raise asyncio.CancelledError()

    class _FakeContext:
        async def new_cdp_session(self, page: object) -> "_FakeCdpSession":
            return _FakeCdpSession()

    fake_page = SimpleNamespace(context=_FakeContext(), url="https://example.test/")

    websocket = SimpleNamespace(
        close=AsyncMock(),
        send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect()),
    )
    fake_app = SimpleNamespace(
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(get_session=AsyncMock(return_value=session)),
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(cdp_input, "_resolve_working_page", AsyncMock(return_value=fake_page))
    release_browser_state = AsyncMock()
    monkeypatch.setattr(cdp_input, "release_browser_state", release_browser_state, raising=False)

    with pytest.raises(asyncio.CancelledError):
        await cdp_input.cdp_input_browser_session_stream(
            websocket, browser_session_id, client_id="client_leak_check_cancel"
        )

    release_browser_state.assert_awaited_once_with(browser_state, "browser_session", browser_session_id)
