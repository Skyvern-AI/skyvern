import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from skyvern.forge.sdk.routes.streaming import cdp_input
from skyvern.forge.sdk.streaming import registries
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


def _browser_session_stream_agent_functions() -> SimpleNamespace:
    return SimpleNamespace(
        authorize_browser_session_stream=AsyncMock(return_value=True),
        browser_session_stream_reauthorization_required=AsyncMock(return_value=False),
        reauthorize_browser_session_stream=AsyncMock(return_value=True),
    )


@pytest.mark.parametrize("failure", ["transient", "sustained", "denied"])
@pytest.mark.asyncio
async def test_cdp_reauthorization_retries_transient_errors_within_grace(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(cdp_input, "STREAM_AUTHORIZATION_RECHECK_SECONDS", 0.01)
    closed = asyncio.Event()
    close_calls: list[tuple[int, str]] = []
    calls = 0

    async def close(*, code: int, reason: str) -> None:
        close_calls.append((code, reason))
        closed.set()

    async def reauthorize() -> bool:
        nonlocal calls
        calls += 1
        if failure == "denied":
            raise PermissionError("credential not accessible")
        if failure == "sustained" or calls == 1:
            raise RuntimeError("transient authorization lookup failure")
        return True

    task = asyncio.create_task(
        cdp_input._reauthorize_stream_periodically(SimpleNamespace(close=close), reauthorize, {})
    )
    if failure != "transient":
        await asyncio.wait_for(closed.wait(), timeout=1)
        await task
        assert close_calls == [(4403, "browser-session-access-denied")]
        if failure == "denied":
            assert calls == 1
    else:
        while calls < 2:
            await asyncio.sleep(0.001)
        assert close_calls == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


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


@pytest.mark.parametrize("route", ["browser_session", "workflow_run"])
@pytest.mark.asyncio
async def test_cdp_connect_authorization_lookup_error_uses_retryable_close_code(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    websocket = SimpleNamespace(close=AsyncMock(), send_json=AsyncMock())
    agent_functions = SimpleNamespace(authorize_browser_session_stream=AsyncMock(side_effect=RuntimeError("lookup")))
    channel = SimpleNamespace(close=AsyncMock())
    fake_app = SimpleNamespace(
        AGENT_FUNCTION=agent_functions,
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(
                    return_value=SimpleNamespace(organization_id="org_stream", status=WorkflowRunStatus.running)
                )
            )
        ),
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(
            get_session_by_runnable_id=AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_run"))
        ),
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "CdpInputChannel", lambda **_kwargs: channel)
    monkeypatch.setattr(cdp_input, "get_request_principal", lambda: None)
    if route == "workflow_run":
        monkeypatch.setattr(cdp_input, "try_stream_ref_inc", lambda _workflow_run_id: True)
        monkeypatch.setattr(cdp_input, "stream_ref_dec", AsyncMock())
        monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=object()))
        monkeypatch.setattr(cdp_input, "release_browser_state", AsyncMock())
        await cdp_input.cdp_input_stream(websocket, "wr_test", client_id="client_test")
    else:
        await cdp_input.cdp_input_browser_session_stream(websocket, "pbs_test", client_id="client_test")

    websocket.close.assert_awaited_once_with(code=1011, reason="authorization-temporarily-unavailable")


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
    agent_functions = _browser_session_stream_agent_functions()

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
        AGENT_FUNCTION=agent_functions,
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(cdp_input, "_resolve_working_page", AsyncMock(return_value=fake_page))
    release_browser_state = AsyncMock()
    # Keep this test resilient if release ownership moves behind another streaming helper.
    monkeypatch.setattr(cdp_input, "release_browser_state", release_browser_state, raising=False)

    await cdp_input.cdp_input_browser_session_stream(websocket, browser_session_id, client_id="client_leak_check")

    agent_functions.authorize_browser_session_stream.assert_awaited_once_with(None, "org_stream", browser_session_id)
    release_browser_state.assert_awaited_once_with(browser_state, "browser_session", browser_session_id)


@pytest.mark.asyncio
async def test_browser_session_route_detaches_input_before_releasing_browser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child CDP session must be detached while the adopted driver is still connected.

    Releasing the Playwright driver first leaves its child Target session attached to the
    shared CDP connection. A subsequent frame reconnect can then receive a live socket with no
    usable screencast events.
    """
    browser_session_id = "pbs_teardown_order"
    browser_state = SimpleNamespace(name="adopted_browser_state")
    session = SimpleNamespace(status="running")
    agent_functions = _browser_session_stream_agent_functions()
    events: list[str] = []

    class _FakeCdpSession:
        async def detach(self) -> None:
            events.append("detach")

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
        AGENT_FUNCTION=agent_functions,
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(cdp_input, "_resolve_working_page", AsyncMock(return_value=fake_page))

    async def _release(*_args: object) -> None:
        events.append("release")

    monkeypatch.setattr(cdp_input, "release_browser_state", _release)

    await cdp_input.cdp_input_browser_session_stream(websocket, browser_session_id, client_id="client_order")

    assert events == ["detach", "release"]


@pytest.mark.asyncio
async def test_browser_session_route_releases_after_cancelled_input_session_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled child-session detach must not prevent adopted browser-state release."""
    browser_session_id = "pbs_leak_check_cancel"
    browser_state = SimpleNamespace(name="adopted_browser_state")
    session = SimpleNamespace(status="running")
    agent_functions = _browser_session_stream_agent_functions()

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
        AGENT_FUNCTION=agent_functions,
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


@pytest.mark.asyncio
async def test_browser_session_route_reauthorizes_and_closes_when_access_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session_id = "pbs_reauth"
    disconnected = asyncio.Event()
    session = SimpleNamespace(status="running")

    class _FakeCdpSession:
        async def detach(self) -> None:
            return None

    class _FakeContext:
        async def new_cdp_session(self, page: object) -> _FakeCdpSession:
            return _FakeCdpSession()

    fake_page = SimpleNamespace(context=_FakeContext(), url="https://example.test/")
    close_calls: list[tuple[int, str]] = []

    async def close(*, code: int = 1000, reason: str = "") -> None:
        close_calls.append((code, reason))
        disconnected.set()

    async def receive_text() -> str:
        await disconnected.wait()
        raise WebSocketDisconnect()

    websocket = SimpleNamespace(close=close, send_json=AsyncMock(), receive_text=receive_text)
    agent_functions = SimpleNamespace(
        authorize_browser_session_stream=AsyncMock(return_value=True),
        browser_session_stream_reauthorization_required=AsyncMock(return_value=True),
        reauthorize_browser_session_stream=AsyncMock(return_value=False),
    )
    fake_app = SimpleNamespace(
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(get_session=AsyncMock(return_value=session)),
        AGENT_FUNCTION=agent_functions,
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "get_request_principal", lambda: None)
    monkeypatch.setattr(cdp_input, "STREAM_AUTHORIZATION_RECHECK_SECONDS", 0.001)
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", AsyncMock(return_value=object()))
    monkeypatch.setattr(cdp_input, "_resolve_working_page", AsyncMock(return_value=fake_page))
    monkeypatch.setattr(cdp_input, "release_browser_state", AsyncMock())

    await asyncio.wait_for(
        cdp_input.cdp_input_browser_session_stream(websocket, browser_session_id, client_id="client_reauth"),
        timeout=2,
    )

    agent_functions.authorize_browser_session_stream.assert_awaited_once_with(None, "org_stream", browser_session_id)
    agent_functions.reauthorize_browser_session_stream.assert_awaited_once_with(None, "org_stream", browser_session_id)
    assert (4403, "browser-session-access-denied") in close_calls


@pytest.mark.parametrize("association", ["ephemeral", "initial", "late"])
@pytest.mark.asyncio
async def test_workflow_run_route_authorizes_only_resolved_persistent_sessions(
    monkeypatch: pytest.MonkeyPatch, association: str
) -> None:
    workflow_run_id = "wr_session_auth"
    browser_session_id = "pbs_run"
    has_initial_session = association == "initial"
    expects_reauthorization = True
    principal = object()
    disconnect = asyncio.Event()
    close_calls: list[tuple[int, str]] = []
    agent_functions = SimpleNamespace(
        authorize_browser_session_stream=AsyncMock(return_value=True),
        browser_session_stream_reauthorization_required=AsyncMock(return_value=True),
        reauthorize_browser_session_stream=AsyncMock(return_value=False),
        reauthorize_workflow_run_stream=AsyncMock(return_value=False),
    )

    async def close(*, code: int = 1000, reason: str = "") -> None:
        close_calls.append((code, reason))
        disconnect.set()

    websocket = SimpleNamespace(close=close, send_json=AsyncMock())
    workflow_run = SimpleNamespace(organization_id="org_stream", status=WorkflowRunStatus.running)
    lookup_count = 0

    async def get_session_by_runnable_id(*_args: object, **_kwargs: object) -> object | None:
        nonlocal lookup_count
        lookup_count += 1
        if association == "initial" or (association == "late" and lookup_count > 1):
            return SimpleNamespace(persistent_browser_session_id=browser_session_id)
        return None

    manager = SimpleNamespace(get_session_by_runnable_id=AsyncMock(side_effect=get_session_by_runnable_id))
    fake_app = SimpleNamespace(
        PERSISTENT_SESSIONS_MANAGER=manager,
        DATABASE=SimpleNamespace(workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run))),
        AGENT_FUNCTION=agent_functions,
    )

    class _FakeInputSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def get_session(self, *, force_refresh: bool = False) -> object:
            return object()

        async def close(self) -> None:
            return None

    channel = SimpleNamespace(client_id="client_run_auth", interactor="agent", close=AsyncMock())
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    monkeypatch.setattr(cdp_input, "get_request_principal", lambda: principal)
    monkeypatch.setattr(cdp_input, "CdpInputChannel", lambda **_kwargs: channel)
    monkeypatch.setattr(cdp_input, "ActivePageCdpInputSession", _FakeInputSession)
    monkeypatch.setattr(cdp_input, "try_stream_ref_inc", lambda _workflow_run_id: True)
    monkeypatch.setattr(cdp_input, "stream_ref_dec", AsyncMock())
    monkeypatch.setattr(cdp_input, "STREAM_AUTHORIZATION_RECHECK_SECONDS", 0.001)
    monkeypatch.setattr(cdp_input, "release_browser_state", AsyncMock())

    async def wait_for_browser_state(*_args: object, **_kwargs: object) -> object:
        assert manager.get_session_by_runnable_id.await_count == 0
        assert agent_functions.authorize_browser_session_stream.await_count == 0
        return object()

    async def run_input_loop(*_args: object, **_kwargs: object) -> None:
        assert agent_functions.authorize_browser_session_stream.await_count == int(has_initial_session)
        if expects_reauthorization:
            await disconnect.wait()
        raise WebSocketDisconnect()

    monkeypatch.setattr(cdp_input, "wait_for_browser_state", wait_for_browser_state)
    monkeypatch.setattr(cdp_input, "_run_input_loop", run_input_loop)

    await asyncio.wait_for(
        cdp_input.cdp_input_stream(websocket, workflow_run_id, client_id="client_run_auth"), timeout=2
    )

    assert lookup_count >= 1
    if has_initial_session:
        agent_functions.authorize_browser_session_stream.assert_awaited_once_with(principal, "org_stream", "pbs_run")
    else:
        agent_functions.authorize_browser_session_stream.assert_not_awaited()
    agent_functions.browser_session_stream_reauthorization_required.assert_awaited_once_with(principal, "org_stream")
    if association == "ephemeral":
        agent_functions.reauthorize_browser_session_stream.assert_not_awaited()
        agent_functions.reauthorize_workflow_run_stream.assert_awaited_once_with(
            principal, "org_stream", workflow_run_id, "workflow_run.execute"
        )
    else:
        agent_functions.reauthorize_browser_session_stream.assert_awaited_once_with(principal, "org_stream", "pbs_run")
    assert (4403, "browser-session-access-denied") in close_calls
    if association == "late":
        assert lookup_count == 2
    if association == "initial":
        assert lookup_count == 2
