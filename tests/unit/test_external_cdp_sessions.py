import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import (
    BlockedHost,
    BrowserSessionAlreadyOccupiedError,
    BrowserSessionClosed,
    BrowserSessionExpired,
    BrowserSessionNotFound,
    BrowserSessionNotRenewable,
    BrowserSessionOwnershipConflict,
    ExternalBrowserEndpointNotEncrypted,
    ExternalBrowserUnavailable,
)
from skyvern.forge import agent as agent_mod
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.routes import agent_protocol as agent_protocol_mod
from skyvern.forge.sdk.routes.streaming import verify as verify_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    lost_external_browser_error,
)
from skyvern.forge.sdk.schemas.tasks import TaskRequest
from skyvern.forge.sdk.workflow import service as service_mod
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye import external_cdp_sessions as external_mod
from skyvern.webeye import real_browser_manager as real_browser_manager_mod
from skyvern.webeye import real_browser_state as real_browser_state_mod
from skyvern.webeye.actions.handler import _adopted_session_download_binding
from skyvern.webeye.browser_factory import BrowserContextFactory, rebind_download_dir
from skyvern.webeye.cdp_discovery import CdpDiscoveryError, resolve_websocket_url
from skyvern.webeye.external_cdp_sessions import close_expired_external_cdp_sessions
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit._external_cdp_fakes import (
    CDP_URL,
    ORG_A,
    ORG_B,
    TOKEN,
    acquire_lease,
    backdate_registration,
    begin_lease,
    external_browser_state,
    external_cdp_stack,
    install_deadline_clock,
    register_external,
    release_lease,
    release_lease_by,
    stall_external_detach,
)
from tests.unit._task_block_fakes import mock_block_execute_deps, output_parameter
from tests.unit.scoped_asyncio import ScopedAsyncio


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[SimpleNamespace]:
    async with external_cdp_stack() as built:
        yield built


@pytest.mark.asyncio
async def test_registration_is_org_scoped_detach_only_and_never_reveals_the_address(stack: SimpleNamespace) -> None:
    client, manager = stack.client, stack.manager
    attached, context, pw = external_browser_state()
    connect = AsyncMock(return_value=attached)
    with capture_logs() as logs, patch.object(manager_mod, "attach_registered_cdp_browser", connect):
        registered = await register_external(client)
        assert registered.status_code == 200
        session_id = registered.json()["browser_session_id"]
        assert session_id.startswith("pbs_")
        assert registered.json()["status"] == "running"
        assert registered.json()["browser_address"] is None

        read_a = await client.get(f"/v1/browser_sessions/{session_id}")
        assert read_a.status_code == 200
        stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
        assert stored is not None and stored.upstream_cdp_url and stored.upstream_cdp_url not in repr(stored)

        stack.caller.organization_id = ORG_B
        assert (await client.get(f"/v1/browser_sessions/{session_id}")).status_code == 404
        assert await manager.get_browser_state(session_id, ORG_B) is None
        connect.assert_not_awaited()

        assert await manager.get_browser_state(session_id, ORG_A) is attached
        assert await manager.get_browser_state(session_id, ORG_A) is attached
        connect.assert_awaited_once()
        assert await manager.get_browser_state(session_id, ORG_B) is None
        assert (await client.post(f"/v1/browser_sessions/{session_id}/close")).status_code == 404
        assert session_id in manager._browser_sessions

        stack.caller.organization_id = ORG_A
        assert (await client.post(f"/v1/browser_sessions/{session_id}/close")).status_code == 200

    assert session_id not in manager._browser_sessions
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()
    context.browser.close.assert_not_awaited()
    closed = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert closed is not None and closed.completed_at is not None
    assert closed.upstream_cdp_url is None
    for body in (registered.text, read_a.text):
        assert TOKEN not in body and ":9333" not in body
    assert any(log.get("event") == "api.raw_request" for log in logs)
    assert TOKEN not in repr(logs) and "devtools/browser/b1" not in repr(logs)


@pytest.mark.asyncio
async def test_closing_every_session_leaves_a_registration_open(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]

    await stack.manager.close_all_sessions(ORG_A)

    kept = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert kept is not None and kept.completed_at is None and kept.upstream_cdp_url is not None


@pytest.mark.asyncio
async def test_attach_rechecks_the_host_policy_at_connect_time(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    with patch.object(settings, "ENV", "production"), pytest.raises(BlockedHost):
        await stack.manager.get_browser_state(session_id, ORG_A)
    assert session_id not in stack.manager._browser_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False], ids=["warm_cache", "cold_cache"])
async def test_an_expired_registration_is_refused_and_released(warm: bool, stack: SimpleNamespace) -> None:
    manager = stack.manager
    attached, context, pw = external_browser_state()
    connect = AsyncMock(return_value=attached)
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    with patch.object(manager_mod, "attach_registered_cdp_browser", connect):
        if warm:
            assert await manager.get_browser_state(session_id, ORG_A) is attached
        await backdate_registration(stack, session_id, minutes=6)
        with pytest.raises(BrowserSessionExpired):
            await begin_lease(stack.manager, session_id, "tsk_before_close")
        with pytest.raises(BrowserSessionExpired):
            await manager.get_browser_state(session_id, ORG_A)
        with pytest.raises(BrowserSessionExpired):
            await begin_lease(stack.manager, session_id, "tsk_after_close")

    assert connect.await_count == (1 if warm else 0)
    assert session_id not in manager._browser_sessions
    assert pw.stop.await_count == (1 if warm else 0)
    context.close.assert_not_awaited()
    closed = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert closed is not None and closed.completed_at is not None and closed.upstream_cdp_url is None


@pytest.mark.asyncio
async def test_renewal_never_moves_a_registration_deadline(stack: SimpleNamespace) -> None:
    manager = stack.manager
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    await backdate_registration(stack, session_id, minutes=4)

    renewed = await manager.renew_or_close_session(session_id, ORG_A)

    assert renewed.timeout_minutes == 5 and renewed.completed_at is None
    stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert stored is not None and stored.timeout_minutes == 5 and stored.completed_at is None
    remaining = await manager.remaining_lifetime_seconds(session_id, ORG_A)
    assert remaining is not None and 0 < remaining <= 60
    fixed = await manager.seconds_until_fixed_deadline(session_id, ORG_A)
    assert fixed is not None and 0 < fixed <= 60

    await backdate_registration(stack, session_id, minutes=6)
    with pytest.raises(BrowserSessionNotRenewable):
        await manager.renew_or_close_session(session_id, ORG_A, close_on_failure=False)
    stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert stored is not None and stored.completed_at is None


@pytest.mark.asyncio
async def test_a_close_during_an_in_flight_connect_publishes_nothing(stack: SimpleNamespace) -> None:
    manager = stack.manager
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state()
    connect_started, release_connect = asyncio.Event(), asyncio.Event()

    async def slow_connect(_: str) -> RealBrowserState:
        connect_started.set()
        await release_connect.wait()
        return attached

    with patch.object(manager_mod, "attach_registered_cdp_browser", slow_connect):
        attach = asyncio.create_task(manager.get_browser_state(session_id, ORG_A))
        await connect_started.wait()
        await manager.close_session(ORG_A, session_id)
        release_connect.set()
        assert await attach is None

    assert session_id not in manager._browser_sessions
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_closes_a_registration_no_process_holds(stack: SimpleNamespace) -> None:
    expired_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    live_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    await backdate_registration(stack, expired_id, minutes=10)

    with patch.object(manager_mod, "active_copilot_session_ids", lambda: {expired_id}):
        await stack.manager.reap_expired_sessions()
    reaped = await stack.repo.get_persistent_browser_session(expired_id, ORG_A)
    assert reaped is not None and reaped.completed_at is not None and reaped.upstream_cdp_url is None

    await backdate_registration(stack, live_id, minutes=10)
    assert await close_expired_external_cdp_sessions(stack.repo) == 1
    swept = await stack.repo.get_persistent_browser_session(live_id, ORG_A)
    assert swept is not None and swept.completed_at is not None and swept.upstream_cdp_url is None
    assert swept.close_reason == "expired"


@pytest.mark.asyncio
async def test_a_restart_leaves_registrations_attachable(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    with patch.object(settings, "BROWSER_STREAMING_MODE", "cdp"):
        await stack.manager.cleanup_stale_sessions()
    kept = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert kept is not None and kept.completed_at is None and kept.upstream_cdp_url is not None

    attached, context, pw = external_browser_state()
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)):
        assert await stack.manager.get_browser_state(session_id, ORG_A) is attached
    await manager_mod.DefaultPersistentSessionsManager.close()

    kept = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert kept is not None and kept.completed_at is None and kept.upstream_cdp_url is not None
    assert session_id not in stack.manager._browser_sessions
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_host_dns_cannot_resolve_is_refused_before_any_dial() -> None:
    start = AsyncMock()
    with (
        patch.object(settings, "ENV", "production"),
        patch.object(external_mod, "async_playwright", lambda: SimpleNamespace(start=start)),
        pytest.raises(BlockedHost),
    ):
        await external_mod.connect_external_cdp_browser("https://cdp.invalid:9222/?token=T")
    start.assert_not_awaited()


async def _serve_discovery(reply: bytes | Callable[[int], bytes], requests: list[bytes]) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(reply(writer.get_extra_info("sockname")[1]) if callable(reply) else reply)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def _json_reply(payload: object) -> bytes:
    body = json.dumps(payload).encode()
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)


def _driver() -> tuple[SimpleNamespace, AsyncMock]:
    """A Playwright driver whose connect records the websocket URL it was asked to dial."""
    connect = AsyncMock(return_value=SimpleNamespace(contexts=[external_browser_state()[1]]))
    pw = SimpleNamespace(stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=connect))
    return SimpleNamespace(start=AsyncMock(return_value=pw)), connect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "reported", "request_line", "auth", "dialed"),
    [
        (
            "http://127.0.0.1:{port}/?token=T",
            "ws://localhost/devtools/browser/abc",
            b"GET /json/version?token=T ",
            None,
            "ws://127.0.0.1:{port}/devtools/browser/abc?token=T",
        ),
        (
            "http://user:p%40ss@127.0.0.1:{port}",
            "ws://127.0.0.1:{port}/devtools/browser/abc",
            b"GET /json/version ",
            b"Authorization: Basic dXNlcjpwQHNz",
            "ws://user:p%40ss@127.0.0.1:{port}/devtools/browser/abc",
        ),
        (
            "http://127.0.0.1:{port}/pbs_x?token=T",
            "ws://127.0.0.1:{port}/pbs_x/devtools/browser/abc?token=R",
            b"GET /pbs_x/json/version?token=T ",
            None,
            "ws://127.0.0.1:{port}/pbs_x/devtools/browser/abc?token=R",
        ),
        (
            "http://127.0.0.1:{port}",
            "wss://93.184.216.34:8443/devtools/browser/abc?session=S",
            b"GET /json/version ",
            None,
            "wss://93.184.216.34:8443/devtools/browser/abc?session=S",
        ),
    ],
    ids=["loopback_answer_keeps_query_token", "same_authority_keeps_userinfo", "reported_query_wins", "separate_host"],
)
async def test_the_dial_keeps_the_callers_credential_or_follows_a_separate_websocket_host(
    endpoint: str, reported: str, request_line: bytes, auth: bytes | None, dialed: str
) -> None:
    requests: list[bytes] = []
    server = await _serve_discovery(
        lambda port: _json_reply({"webSocketDebuggerUrl": reported.format(port=port)}), requests
    )
    port = server.sockets[0].getsockname()[1]
    driver, connect = _driver()
    try:
        with patch.object(settings, "ENV", "local"), patch.object(external_mod, "async_playwright", lambda: driver):
            await external_mod.connect_external_cdp_browser(endpoint.format(port=port))
    finally:
        server.close()

    assert requests[0].startswith(request_line)
    if auth is not None:
        assert auth in requests[0]
    assert connect.await_args is not None and connect.await_args.args[0] == dialed.format(port=port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        _json_reply({"webSocketDebuggerUrl": "ws://93.184.216.34:9222/devtools/browser/abc"}),
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/json/version\r\nContent-Length: 0\r\n\r\n",
        _json_reply({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/abc", "pad": "x" * 70_000}),
    ],
    ids=["plaintext_websocket_elsewhere", "redirect", "oversized"],
)
async def test_a_discovery_answer_that_breaks_policy_starts_no_driver(reply: bytes) -> None:
    requests: list[bytes] = []
    server = await _serve_discovery(reply, requests)
    driver, connect = _driver()
    try:
        with (
            patch.object(settings, "ENV", "local"),
            patch.object(external_mod, "async_playwright", lambda: driver),
            pytest.raises((ExternalBrowserEndpointNotEncrypted, ExternalBrowserUnavailable)),
        ):
            await external_mod.connect_external_cdp_browser(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    finally:
        server.close()

    assert len(requests) == 1
    driver.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_trickling_discovery_reply_is_cut_off_at_the_deadline() -> None:
    handlers: list[asyncio.Task[Any]] = []

    async def trickle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(cast("asyncio.Task[Any]", asyncio.current_task()))
        await reader.readuntil(b"\r\n\r\n")
        try:
            writer.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(50):
                writer.write(b"X-Slow: 1\r\n")
                await writer.drain()
                await asyncio.sleep(0.2)
        except ConnectionError:
            pass
        writer.close()

    server = await asyncio.start_server(trickle, "127.0.0.1", 0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(CdpDiscoveryError, match="TimeoutError"):
            await resolve_websocket_url(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", timeout_ms=1_000)
    finally:
        server.close()
        for handler in handlers:
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    assert loop.time() - started < 3


@pytest.mark.asyncio
async def test_the_stored_address_is_ciphertext_that_attach_decrypts(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert stored is not None and stored.upstream_cdp_url
    assert TOKEN not in stored.upstream_cdp_url and ":9333" not in stored.upstream_cdp_url

    attached, _, _ = external_browser_state()
    connect = AsyncMock(return_value=attached)
    with patch.object(external_mod, "connect_external_cdp_browser", connect):
        assert await stack.manager.get_browser_state(session_id, ORG_A) is attached
    connect.assert_awaited_once_with(CDP_URL)


@pytest.mark.asyncio
async def test_registration_is_refused_without_encryption_configured(stack: SimpleNamespace) -> None:
    with patch.dict(encryptor._methods, clear=True):
        response = await register_external(stack.client)
    assert response.status_code == 503
    assert await stack.repo.get_uncompleted_persistent_browser_sessions() == []


@pytest.mark.asyncio
async def test_an_attach_cancelled_before_publishing_releases_its_driver(stack: SimpleNamespace) -> None:
    manager = stack.manager
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state()
    read_row = stack.repo.get_persistent_browser_session
    attach_done, reading_after_attach = asyncio.Event(), asyncio.Event()

    async def attach(_: str) -> RealBrowserState:
        attach_done.set()
        return attached

    async def read(session_id: str, organization_id: str | None = None) -> PersistentBrowserSession | None:
        if attach_done.is_set():
            reading_after_attach.set()
            await asyncio.Event().wait()
        return await read_row(session_id, organization_id)

    with (
        patch.object(manager_mod, "attach_registered_cdp_browser", attach),
        patch.object(stack.repo, "get_persistent_browser_session", read),
    ):
        attach_task = asyncio.create_task(manager.get_browser_state(session_id, ORG_A))
        await reading_after_attach.wait()
        attach_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attach_task

    assert session_id not in manager._browser_sessions
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_connect_that_lands_mid_close_publishes_nothing(stack: SimpleNamespace) -> None:
    manager = stack.manager
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state()
    connect_started, release_connect = asyncio.Event(), asyncio.Event()
    closing, finish_close = asyncio.Event(), asyncio.Event()
    record_close_reason = stack.repo.record_persistent_browser_session_close_reason

    async def slow_connect(_: str) -> RealBrowserState:
        connect_started.set()
        await release_connect.wait()
        return attached

    async def held_record(*args: object) -> None:
        closing.set()
        await finish_close.wait()
        await record_close_reason(*args)

    with (
        patch.object(manager_mod, "attach_registered_cdp_browser", slow_connect),
        patch.object(stack.repo, "record_persistent_browser_session_close_reason", held_record),
    ):
        attach = asyncio.create_task(manager.get_browser_state(session_id, ORG_A))
        await connect_started.wait()
        close = asyncio.create_task(manager.close_session(ORG_A, session_id))
        await closing.wait()
        release_connect.set()
        assert await attach is None
        finish_close.set()
        await close

    assert session_id not in manager._browser_sessions
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_connect_failure_is_bounded_and_reads_the_same() -> None:
    async def hang(*_: object, **__: object) -> None:
        await asyncio.Event().wait()

    failures = [
        hang,
        AsyncMock(side_effect=RuntimeError("Unexpected status 401 when connecting")),
        AsyncMock(side_effect=ConnectionRefusedError("connect ECONNREFUSED")),
    ]
    messages = set()
    for connect in failures:
        pw = SimpleNamespace(stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=connect))
        with (
            patch.object(settings, "ENV", "local"),
            patch.object(external_mod, "EXTERNAL_CDP_CONNECT_TIMEOUT_MS", 100),
            patch.object(external_mod, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=pw))),
            pytest.raises(ExternalBrowserUnavailable) as raised,
        ):
            async with asyncio.timeout(5):
                await external_mod.connect_external_cdp_browser(CDP_URL)
        messages.add(str(raised.value))
        pw.stop.assert_awaited_once()
    assert len(messages) == 1


async def _lease(stack: SimpleNamespace, session_id: str) -> tuple[str | None, str | None]:
    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None
    return row.runnable_id, row.runnable_generation_id


@pytest.mark.asyncio
async def test_a_run_leases_the_named_registration_and_its_release_only_detaches_its_driver(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state(pages=2)
    next_run, _, next_pw = external_browser_state()
    connect = AsyncMock(side_effect=[attached, next_run])

    with capture_logs() as logs, patch.object(manager_mod, "attach_registered_cdp_browser", connect):
        generation = await begin_lease(stack.manager, session_id, "tsk_a")
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is attached
        with pytest.raises(BrowserSessionAlreadyOccupiedError):
            await begin_lease(stack.manager, session_id, "tsk_b")
        assert await _lease(stack, session_id) == ("tsk_a", generation)
        assert await release_lease(stack.manager, session_id, "tsk_a", generation)
        assert await _lease(stack, session_id) == (None, None)
        pw.stop.assert_awaited_once()

        generation_b = await begin_lease(stack.manager, session_id, "tsk_b")
        assert await acquire_lease(stack.manager, session_id, "tsk_b", generation_b) is next_run
        assert not await release_lease(stack.manager, session_id, "tsk_a", generation)
        assert await acquire_lease(stack.manager, session_id, "tsk_b", generation_b) is next_run

    assert await _lease(stack, session_id) == ("tsk_b", generation_b)
    next_pw.stop.assert_not_awaited()
    assert next_run.is_connected()
    released = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert released is not None and released.completed_at is None and released.upstream_cdp_url is not None
    context.close.assert_not_awaited()
    context.browser.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()
    assert TOKEN not in repr(logs) and ":9333" not in repr(logs)
    with patch.object(verify_mod, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=stack.manager)):
        assert await verify_mod.verify_browser_session(session_id, ORG_A) is None


@pytest.mark.asyncio
async def test_a_run_naming_no_session_launches_a_managed_browser_and_leaves_registrations_alone(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    managed = MagicMock(get_or_create_page=AsyncMock(), browser_context=None)
    browser_manager = RealBrowserManager()
    launch = AsyncMock(return_value=managed)
    attach = AsyncMock()
    task = MagicMock(
        task_id="tsk_1",
        workflow_run_id=None,
        organization_id=ORG_A,
        browser_session_id=None,
        browser_address=None,
        url="https://example.test/",
    )

    with (
        patch.object(real_browser_manager_mod, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)),
        patch.object(manager_mod, "attach_registered_cdp_browser", attach),
        patch.object(browser_manager, "_create_browser_state", launch),
    ):
        assert await browser_manager.get_or_create_for_task(task) is managed

    assert launch.await_args is not None and launch.await_args.kwargs.get("browser_session_id") is None
    attach.assert_not_awaited()
    assert await _lease(stack, session_id) == (None, None)


@pytest.mark.asyncio
async def test_a_reused_session_claim_leases_a_registration_without_waiting_for_an_address(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None

    skyvern_context.set(SkyvernContext())
    try:
        with patch.object(service_mod, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=stack.manager)):
            claimed = await WorkflowService()._claim_reused_session(
                organization_id=ORG_A, workflow_run_id="wr_1", browser_session=row
            )
    finally:
        skyvern_context.reset()

    assert claimed == session_id
    assert (await _lease(stack, session_id))[0] == "wr_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["task", "workflow_run"])
@pytest.mark.parametrize("state", ["open", "expired", "closed", "foreign"])
async def test_submission_admits_only_an_open_registration_of_the_callers_org(
    stack: SimpleNamespace, state: str, run_type: str
) -> None:
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    if state == "expired":
        await backdate_registration(stack, session_id, minutes=6)
    if state == "closed":
        await stack.client.post(f"/v1/browser_sessions/{session_id}/close")
    admitted = AsyncMock(side_effect=asyncio.CancelledError)
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(browser_sessions=stack.repo, tasks=SimpleNamespace(create_task=admitted))
    )
    expected: dict[str, tuple[type[BaseException], str | None]] = {
        "open": (asyncio.CancelledError, None),
        "expired": (
            BrowserSessionExpired,
            r"expired after .* register it again \(POST /v1/browser_sessions/external\)",
        ),
        "closed": (BrowserSessionClosed, r"no longer usable .* register it again"),
        "foreign": (BrowserSessionNotFound, None),
    }
    error_type, message = expected[state]
    organization_id = ORG_B if state == "foreign" else ORG_A
    service = WorkflowService()

    with (
        patch.object(agent_mod, "app", fake_app),
        patch.object(service_mod, "app", fake_app),
        patch.object(service, "get_workflow", admitted),
        pytest.raises(error_type, match=message),
    ):
        if run_type == "task":
            await ForgeAgent().create_task(
                TaskRequest(url="https://example.test/", browser_session_id=session_id), organization_id
            )
        else:
            await service.create_workflow_run(
                WorkflowRequestBody(browser_session_id=session_id), "wpid_1", "w_1", organization_id
            )

    assert admitted.await_count == (1 if state == "open" else 0)


@pytest.mark.asyncio
async def test_an_attach_that_lands_after_its_lease_moved_publishes_nothing(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state()
    connect_started, release_connect = asyncio.Event(), asyncio.Event()

    async def slow_connect(_: str) -> RealBrowserState:
        connect_started.set()
        await release_connect.wait()
        return attached

    with patch.object(manager_mod, "attach_registered_cdp_browser", slow_connect):
        generation_a = await begin_lease(stack.manager, session_id, "tsk_a")
        attach_a = asyncio.create_task(acquire_lease(stack.manager, session_id, "tsk_a", generation_a))
        await connect_started.wait()
        assert await release_lease(stack.manager, session_id, "tsk_a", generation_a)
        generation_b = await begin_lease(stack.manager, session_id, "tsk_b")
        release_connect.set()
        with pytest.raises(BrowserSessionOwnershipConflict):
            await attach_a

    assert session_id not in stack.manager._browser_sessions
    assert await _lease(stack, session_id) == ("tsk_b", generation_b)
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_that_waited_behind_another_attach_while_its_lease_moved_gets_no_browser(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, _ = external_browser_state()
    connect_started, release_connect = asyncio.Event(), asyncio.Event()

    async def slow_connect(_: str) -> RealBrowserState:
        connect_started.set()
        await release_connect.wait()
        return attached

    with patch.object(manager_mod, "attach_registered_cdp_browser", slow_connect):
        generation_a = await begin_lease(stack.manager, session_id, "tsk_a")
        observer = asyncio.create_task(stack.manager.get_browser_state(session_id, ORG_A))
        await connect_started.wait()
        waiter = asyncio.create_task(acquire_lease(stack.manager, session_id, "tsk_a", generation_a))
        async with asyncio.timeout(5):
            while not stack.manager._external_connect_locks[session_id]._waiters:
                await asyncio.sleep(0)
        assert await release_lease(stack.manager, session_id, "tsk_a", generation_a)
        generation_b = await begin_lease(stack.manager, session_id, "tsk_b")
        release_connect.set()
        assert await observer is attached
        with pytest.raises(BrowserSessionOwnershipConflict):
            await waiter

    assert await _lease(stack, session_id) == ("tsk_b", generation_b)
    context.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["deadline", "closed_elsewhere"])
async def test_the_lease_holder_detaches_when_its_registration_expires_or_another_process_closes_it(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    clock = install_deadline_clock(monkeypatch)
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    attached, context, pw = external_browser_state(pages=2)

    if end == "deadline":
        await backdate_registration(stack, session_id, minutes=5 - 0.5 / 60, clock=clock)
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)):
        generation = await begin_lease(stack.manager, session_id, "tsk_a")
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is attached
        if end == "closed_elsewhere":
            await stack.repo.close_persistent_browser_session(session_id, ORG_A)
        clock.ticking.set()
        await asyncio.wait_for(stack.manager._external_deadlines[session_id], timeout=5)
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is None

    closed = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert closed is not None and closed.completed_at is not None and closed.upstream_cdp_url is None
    if end == "deadline":
        assert closed.close_reason == "expired" and closed.started_at is not None
        assert clock.now(UTC) == closed.started_at.replace(tzinfo=UTC) + timedelta(minutes=5)
    else:
        assert clock.polls == [external_mod.EXTERNAL_CDP_CLOSE_POLL_SECONDS]
    assert session_id not in stack.manager._browser_sessions and not stack.manager._external_deadlines
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_a_registration_whose_driver_hangs_on_detach_still_closes_the_row(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state(pages=2)
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)):
        generation = await begin_lease(stack.manager, session_id, "tsk_a")
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is attached
    driver_process = stall_external_detach(context, pw)
    monkeypatch.setattr(real_browser_state_mod, "BROWSER_CLOSE_TIMEOUT", 0.05)

    with capture_logs() as logs:
        await asyncio.wait_for(stack.manager.close_session(ORG_A, session_id), timeout=5)

    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and row.completed_at is not None and row.upstream_cdp_url is None
    assert any(log["log_level"] == "warning" and log.get("session_id") == session_id for log in logs)
    driver_process.kill.assert_called_once_with()
    context.close.assert_not_awaited()
    context.browser.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True], ids=["owner_release", "stale_release"])
async def test_only_the_owners_release_cancels_the_deadline(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    clock = install_deadline_clock(monkeypatch)
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    await backdate_registration(stack, session_id, minutes=5 - 0.5 / 60, clock=clock)

    generation = await begin_lease(stack.manager, session_id, "tsk_a")
    deadline = stack.manager._external_deadlines[session_id]
    released = await release_lease(stack.manager, session_id, "tsk_a", "gen_stale" if stale else generation)
    assert released is not stale
    clock.ticking.set()
    if stale:
        await asyncio.wait_for(deadline, timeout=5)
    else:
        with pytest.raises(asyncio.CancelledError):
            await deadline

    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and (row.completed_at is not None) is stale
    assert not stack.manager._external_deadlines


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True], ids=["owner_release", "stale_release"])
async def test_a_driver_that_fails_to_detach_keeps_the_lease_and_deadline_until_a_retry_detaches_it(
    stack: SimpleNamespace, stale: bool
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state()
    pw.stop = AsyncMock(side_effect=[RuntimeError("driver stop failed"), None])
    no_live_runs = SimpleNamespace(BROWSER_MANAGER=SimpleNamespace(live_session_runnable_ids=set))

    with (
        patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)),
        patch.object(manager_mod, "app", no_live_runs),
    ):
        generation = await begin_lease(stack.manager, session_id, "tsk_a")
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is attached
        deadline = stack.manager._external_deadlines[session_id]

        assert not await release_lease_by(stack, stack.manager, session_id, "tsk_a", generation, attached, stale=stale)
        assert await _lease(stack, session_id) == ("tsk_a", generation)
        assert stack.manager._external_deadlines[session_id] is deadline and not deadline.done()
        assert stack.manager._browser_sessions[session_id].browser_state is attached

        assert await release_lease_by(stack, stack.manager, session_id, "tsk_a", generation, attached, stale=stale)

    assert await _lease(stack, session_id) == (None, None)
    assert session_id not in stack.manager._browser_sessions
    assert pw.stop.await_count == 2
    context.close.assert_not_awaited()
    context.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_release_racing_the_next_owners_begin_leaves_the_new_deadline_armed(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    generation_a = await begin_lease(stack.manager, session_id, "tsk_a")
    deadline_a = stack.manager._external_deadlines[session_id]
    real_release = stack.repo.release_persistent_browser_session
    generations_b: list[str] = []

    async def release_then_reacquire(
        session_id: str,
        organization_id: str,
        *,
        expected_runnable_id: str | None,
        expected_runnable_generation_id: str | None,
    ) -> PersistentBrowserSession | None:
        released = await real_release(
            session_id,
            organization_id,
            expected_runnable_id=expected_runnable_id,
            expected_runnable_generation_id=expected_runnable_generation_id,
        )
        generations_b.append(await begin_lease(stack.manager, session_id, "tsk_b"))
        return released

    with patch.object(stack.repo, "release_persistent_browser_session", release_then_reacquire):
        assert await release_lease(stack.manager, session_id, "tsk_a", generation_a)

    deadline_b = stack.manager._external_deadlines[session_id]
    assert deadline_b is not deadline_a and not deadline_b.done()
    with pytest.raises(asyncio.CancelledError):
        await deadline_a
    assert await _lease(stack, session_id) == ("tsk_b", generations_b[0])


@pytest.mark.asyncio
async def test_a_legacy_cancel_leaves_the_registration_leased_until_the_worker_releases(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    generation = await begin_lease(stack.manager, session_id, "wr_a")
    run = SimpleNamespace(workflow_run_id="wr_a", browser_session_id=session_id)
    route_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
            browser_sessions=stack.repo,
        ),
        PERSISTENT_SESSIONS_MANAGER=stack.manager,
    )
    cancel = AsyncMock()

    with (
        patch.object(agent_protocol_mod, "app", route_app),
        patch.object(agent_protocol_mod.run_service, "cancel_workflow_run", cancel),
    ):
        await agent_protocol_mod._cancel_workflow_run("wr_a", ORG_A)

    assert cancel.await_args is not None and cancel.await_args.args == ("wr_a",)
    assert await _lease(stack, session_id) == ("wr_a", generation)
    with pytest.raises(BrowserSessionAlreadyOccupiedError):
        await begin_lease(stack.manager, session_id, "wr_b")
    assert not stack.manager._external_deadlines[session_id].done()


@pytest.mark.asyncio
async def test_the_deadline_watcher_keeps_its_own_lease_and_detaches_one_another_process_took(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    attached, context, pw = external_browser_state(pages=2)
    polled = asyncio.Event()
    next_poll = asyncio.Event()
    polls: list[float] = []

    async def poll_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) > 1:
            polled.set()
        await next_poll.wait()
        next_poll.clear()

    monkeypatch.setattr(external_mod, "asyncio", ScopedAsyncio(sleep=poll_sleep))
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)):
        generation = await begin_lease(stack.manager, session_id, "tsk_a")
        watcher = stack.manager._external_deadlines[session_id]
        assert await acquire_lease(stack.manager, session_id, "tsk_a", generation) is attached
        next_poll.set()
        await asyncio.wait_for(polled.wait(), timeout=5)
        assert stack.manager._external_deadlines[session_id] is watcher and not watcher.done()
        assert session_id in stack.manager._browser_sessions
        pw.stop.assert_not_awaited()

        async with stack.session_factory() as session:
            await session.execute(
                update(PersistentBrowserSessionModel)
                .where(PersistentBrowserSessionModel.persistent_browser_session_id == session_id)
                .values(runnable_id="wr_other_process", runnable_generation_id="gen_other")
            )
            await session.commit()
        next_poll.set()
        await asyncio.wait_for(watcher, timeout=5)

    assert session_id not in stack.manager._browser_sessions and not stack.manager._external_deadlines
    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()
    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and row.completed_at is None
    assert (row.runnable_id, row.runnable_generation_id) == ("wr_other_process", "gen_other")


@pytest.mark.asyncio
async def test_a_failed_close_at_the_deadline_is_retried_until_the_row_is_final(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = install_deadline_clock(monkeypatch)
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    await backdate_registration(stack, session_id, minutes=5 - 0.5 / 60, clock=clock)
    real_close = stack.repo.close_persistent_browser_session
    close_attempts: list[str] = []

    async def close_fails_once(session_id: str, organization_id: str) -> PersistentBrowserSession:
        close_attempts.append(session_id)
        if len(close_attempts) == 1:
            raise RuntimeError("transient database error")
        return await real_close(session_id, organization_id)

    with patch.object(stack.repo, "close_persistent_browser_session", close_fails_once):
        await begin_lease(stack.manager, session_id, "tsk_a")
        clock.ticking.set()
        await asyncio.wait_for(stack.manager._external_deadlines[session_id], timeout=5)

    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and row.completed_at is not None and row.close_reason == "expired"
    assert not stack.manager._external_deadlines


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loss", "run_type"),
    [
        ("expired", "workflow_run"),
        ("killed", "task"),
        ("killed", "workflow_run"),
        ("closed", "task"),
        ("closed", "workflow_run"),
        ("vanished", "task"),
        ("vanished", "workflow_run"),
    ],
)
async def test_the_next_step_after_losing_a_registration_fails_with_that_fact_and_launches_nothing(
    stack: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, run_type: str, loss: str
) -> None:
    clock = install_deadline_clock(monkeypatch)
    session_id = (await register_external(stack.client, timeout=5)).json()["browser_session_id"]
    if loss == "expired":
        await backdate_registration(stack, session_id, minutes=5 - 0.5 / 60, clock=clock)
    run_id = "tsk_1" if run_type == "task" else "wr_1"
    generation = await stack.manager.begin_session(
        browser_session_id=session_id, runnable_type=run_type, runnable_id=run_id, organization_id=ORG_A
    )
    lost, context, _ = external_browser_state()
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=lost)):
        assert await acquire_lease(stack.manager, session_id, run_id, generation) is lost
    if loss == "expired":
        clock.ticking.set()
        await asyncio.wait_for(stack.manager._external_deadlines[session_id], timeout=5)
    if loss in ("closed", "vanished"):
        await stack.repo.close_persistent_browser_session(session_id, ORG_A)
    context.browser.is_connected.return_value = False
    browser_manager = RealBrowserManager()
    browser_manager.pages[run_id] = lost
    launch = AsyncMock()
    rbm_app = MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)
    rbm_app.AGENT_FUNCTION.enforce_workflow_run_credential_use = AsyncMock()
    unreachable = AsyncMock(side_effect=ExternalBrowserUnavailable("connection failed"))

    with (
        patch.object(real_browser_manager_mod, "app", rbm_app),
        patch.object(manager_mod, "attach_registered_cdp_browser", unreachable),
        patch.object(browser_manager, "_create_browser_state", launch),
        patch.object(
            stack.manager,
            "get_session",
            AsyncMock(return_value=None) if loss == "vanished" else stack.manager.get_session,
        ),
        pytest.raises(
            {
                "expired": BrowserSessionExpired,
                "killed": ExternalBrowserUnavailable,
                "closed": BrowserSessionClosed,
                "vanished": ExternalBrowserUnavailable,
            }[loss]
        ),
    ):
        if run_type == "task":
            # A task inside a workflow reuses the workflow's lease, so it reaches the launch lookup without a begin.
            inside_workflow = run_id if loss == "vanished" else None
            task = MagicMock(task_id=run_id, workflow_run_id=inside_workflow, organization_id=ORG_A)
            await browser_manager.get_or_create_for_task(task, browser_session_id=session_id)
        else:
            workflow_run = MagicMock(workflow_run_id=run_id, parent_workflow_run_id=None, organization_id=ORG_A)
            await browser_manager.get_or_create_for_workflow_run(
                workflow_run,
                url="https://example.test/",
                browser_session_id=session_id,
                browser_session_runnable_id=run_id,
                browser_session_runnable_generation_id=generation,
            )

    launch.assert_not_awaited()
    assert run_id not in browser_manager.pages
    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and (row.completed_at is not None) is (loss != "killed")
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_task_whose_registration_drops_between_steps_still_gives_its_lease_back(
    stack: SimpleNamespace,
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    generation = await begin_lease(stack.manager, session_id, "tsk_1")
    lost, context, _ = external_browser_state()
    with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=lost)):
        assert await acquire_lease(stack.manager, session_id, "tsk_1", generation) is lost
    browser_manager = RealBrowserManager()
    browser_manager.pages["tsk_1"] = lost
    browser_manager._persistent_session_leases["tsk_1"] = real_browser_manager_mod._PersistentSessionLease(
        session_id=session_id,
        organization_id=ORG_A,
        runnable_id="tsk_1",
        browser_state=lost,
        runnable_generation_id=generation,
    )
    context.browser.is_connected.return_value = False
    task = MagicMock(task_id="tsk_1", workflow_run_id=None, organization_id=ORG_A)
    unreachable = AsyncMock(side_effect=ExternalBrowserUnavailable("connection failed"))

    with (
        patch.object(real_browser_manager_mod, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)),
        patch.object(manager_mod, "attach_registered_cdp_browser", unreachable),
    ):
        with pytest.raises(ExternalBrowserUnavailable):
            await browser_manager.get_or_create_for_task(task, browser_session_id=session_id)
        await browser_manager.cleanup_for_task(
            "tsk_1", close_browser_on_completion=False, browser_session_id=session_id, organization_id=ORG_A
        )

    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None and (row.runnable_id, row.runnable_generation_id) == (None, None)
    assert session_id not in stack.manager._external_deadlines
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_attached_registration_hands_a_runs_download_to_the_runs_scope_without_gating_the_callers_tabs(
    tmp_path: Path,
) -> None:
    driver, connect = _driver()
    context = connect.return_value.contexts[0]
    context.browser.contexts = [context]
    context.browser.new_browser_cdp_session = AsyncMock(return_value=MagicMock(send=AsyncMock()))
    page = MagicMock(context=context)

    with (
        patch.object(settings, "ENV", "local"),
        patch.object(settings, "DOWNLOAD_PATH", str(tmp_path)),
        patch.object(external_mod, "async_playwright", lambda: driver),
        patch.object(external_mod, "resolve_websocket_url", AsyncMock(return_value="ws://127.0.0.1:9222/devtools")),
    ):
        state = await external_mod.connect_external_cdp_browser("http://127.0.0.1:9222")
        assert state.browser_context is context
        await rebind_download_dir(context.browser, run_id="wr_download")
        async with _adopted_session_download_binding(SimpleNamespace(page=page), page) as (interceptor, _, scope):
            assert interceptor is context._skyvern_cdp_download_interceptor
            assert scope == "wr_download"

    context.new_cdp_session.assert_not_called()
    # Browser.setDownloadBehavior is browser-wide: it would move the caller's own downloads to a worker path.
    context.browser.new_browser_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["closed", "deleted"])
async def test_a_later_task_block_on_a_lost_registration_fails_with_its_fact_and_launches_nothing(
    stack: SimpleNamespace, end: str
) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    lost, context, _ = external_browser_state()
    context.browser.is_connected.return_value = False
    if end == "closed":
        await stack.client.post(f"/v1/browser_sessions/{session_id}/close")
    else:
        async with stack.session_factory() as session:
            await session.execute(
                update(PersistentBrowserSessionModel)
                .where(PersistentBrowserSessionModel.persistent_browser_session_id == session_id)
                .values(deleted_at=naive_utc_now())
            )
            await session.commit()
    block = TaskBlock(label="second", output_parameter=output_parameter(), title="second", url="https://example.test/")
    browser_manager = RealBrowserManager()
    launch = AsyncMock()

    browser_app = MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)
    browser_app.AGENT_FUNCTION.enforce_workflow_run_credential_use = AsyncMock()
    with (
        mock_block_execute_deps(working_page_url="https://example.test/") as deps,
        patch.object(real_browser_manager_mod, "app", browser_app),
        patch.object(browser_manager, "_create_browser_state", launch),
        pytest.raises(BrowserSessionClosed if end == "closed" else ExternalBrowserUnavailable),
    ):
        workflow_run = deps.workflow_run
        workflow_run.__dict__.update(
            organization_id=ORG_A, parent_workflow_run_id=None, proxy_location=None, extra_http_headers=None
        )
        browser_manager.pages[workflow_run.workflow_run_id] = lost
        deps.tasks_db.get_last_task_for_workflow_run.return_value = SimpleNamespace(order=0, retry=0)
        deps.browser_manager.get_for_workflow_run = browser_manager.get_for_workflow_run
        deps.browser_manager.get_or_create_for_workflow_run = browser_manager.get_or_create_for_workflow_run
        await block.execute(
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_run_block_id="wrb_test",
            organization_id=ORG_A,
            browser_session_id=session_id,
        )

    launch.assert_not_awaited()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_row_read_on_a_cache_miss_is_a_miss(stack: SimpleNamespace) -> None:
    with patch.object(stack.repo, "get_persistent_browser_session", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await stack.manager.get_browser_state("pbs_uncached", ORG_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_browser_on_completion", "release_driver", "detaches"),
    [(True, None, True), (False, None, True), (False, False, False), (True, False, False)],
)
async def test_closing_an_external_state_only_ever_detaches(
    close_browser_on_completion: bool, release_driver: bool | None, detaches: bool
) -> None:
    state, context, pw = external_browser_state(pages=2)
    on_close = AsyncMock()
    state.add_on_close(on_close)

    await state.close(close_browser_on_completion, release_driver=release_driver)

    assert pw.stop.await_count == (1 if detaches else 0)
    assert on_close.await_count == (1 if close_browser_on_completion else 0)
    context.close.assert_not_awaited()
    context.browser.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_external_state_never_closes_the_callers_tabs_or_rebuilds_the_browser() -> None:
    state, context, _ = external_browser_state(pages=3)
    with patch.object(BrowserContextFactory, "create_browser_context", AsyncMock()) as rebuild:
        await state.check_and_fix_state()
        assert await state.list_valid_pages(max_pages=1) == context.pages
        await state._close_all_other_pages()
        await state.close_pages_opened_after(set())
        assert await state.close_current_open_page() is False

        crashed = context.pages[0]
        crashed.context = context
        state._on_page_crashed(crashed)
        assert await state._reopen_lost_working_page() is None
        await asyncio.sleep(0.05)

        state.browser_context = None
        with pytest.raises(ExternalBrowserUnavailable) as raised:
            await state.check_and_fix_state()
        assert str(raised.value) == str(lost_external_browser_error(None))

    rebuild.assert_not_awaited()
    context.new_page.assert_not_awaited()
    context.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()
