import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import (
    BlockedHost,
    BrowserSessionClosed,
    BrowserSessionNotRenewable,
    ExternalBrowserEndpointNotEncrypted,
    ExternalBrowserSessionNotRunnable,
    ExternalBrowserUnavailable,
)
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.routes.streaming import verify as verify_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye import external_cdp_sessions as external_mod
from skyvern.webeye.browser_factory import BrowserContextFactory
from skyvern.webeye.cdp_discovery import CdpDiscoveryError, resolve_websocket_url
from skyvern.webeye.external_cdp_sessions import close_expired_external_cdp_sessions
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit._external_cdp_fakes import (
    CDP_URL,
    ORG_A,
    ORG_B,
    TOKEN,
    external_browser_state,
    external_cdp_stack,
    register_external,
)


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[SimpleNamespace]:
    async with external_cdp_stack() as built:
        yield built


async def _backdate(stack: SimpleNamespace, session_id: str, minutes: int) -> None:
    async with stack.session_factory() as session:
        await session.execute(
            update(PersistentBrowserSessionModel)
            .where(PersistentBrowserSessionModel.persistent_browser_session_id == session_id)
            .values(started_at=naive_utc_now() - timedelta(minutes=minutes))
        )
        await session.commit()


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
        assert TOKEN not in body and "9333" not in body
    assert any(log.get("event") == "api.raw_request" for log in logs)
    assert TOKEN not in repr(logs) and "devtools/browser/b1" not in repr(logs)


@pytest.mark.asyncio
async def test_registrations_stay_off_session_listings_open_and_closed(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    await stack.repo.update_persistent_browser_session(
        session_id, status="failed", organization_id=ORG_A, completed_at=naive_utc_now()
    )

    assert await stack.repo.get_active_persistent_browser_sessions(ORG_A) == []
    assert await stack.repo.get_persistent_browser_sessions_history(ORG_A) == []
    assert await stack.repo.get_persistent_browser_sessions_history_count(ORG_A) == 0
    ended = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert ended is not None and ended.upstream_cdp_url is None


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
        await _backdate(stack, session_id, minutes=6)
        with pytest.raises(BrowserSessionClosed, match="expired"):
            await manager.get_browser_state(session_id, ORG_A)

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
    await _backdate(stack, session_id, minutes=4)

    renewed = await manager.renew_or_close_session(session_id, ORG_A)

    assert renewed.timeout_minutes == 5 and renewed.completed_at is None
    stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert stored is not None and stored.timeout_minutes == 5 and stored.completed_at is None
    remaining = await manager.remaining_lifetime_seconds(session_id, ORG_A)
    assert remaining is not None and 0 < remaining <= 60
    fixed = await manager.seconds_until_fixed_deadline(session_id, ORG_A)
    assert fixed is not None and 0 < fixed <= 60

    await _backdate(stack, session_id, minutes=6)
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
    await _backdate(stack, expired_id, minutes=10)

    with patch.object(manager_mod, "active_copilot_session_ids", lambda: {expired_id}):
        await stack.manager.reap_expired_sessions()
    reaped = await stack.repo.get_persistent_browser_session(expired_id, ORG_A)
    assert reaped is not None and reaped.completed_at is not None and reaped.upstream_cdp_url is None

    await _backdate(stack, live_id, minutes=10)
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
    assert TOKEN not in stored.upstream_cdp_url and "9333" not in stored.upstream_cdp_url

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


@pytest.mark.asyncio
async def test_runs_and_live_view_refuse_a_registration(stack: SimpleNamespace) -> None:
    session_id = (await register_external(stack.client)).json()["browser_session_id"]
    row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert row is not None

    with pytest.raises(ExternalBrowserSessionNotRunnable):
        await stack.manager.begin_session(
            browser_session_id=session_id, runnable_type="workflow_run", runnable_id="wr_1", organization_id=ORG_A
        )
    with pytest.raises(ExternalBrowserSessionNotRunnable):
        await WorkflowService()._claim_reused_session(
            organization_id=ORG_A, workflow_run_id="wr_1", browser_session=row
        )
    with patch.object(verify_mod, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=stack.manager)):
        assert await verify_mod.verify_browser_session(session_id, ORG_A) is None
    stored = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
    assert stored is not None and stored.runnable_id is None


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
        assert await state.close_current_open_page() is False

        crashed = context.pages[0]
        crashed.context = context
        state._on_page_crashed(crashed)
        assert await state._reopen_lost_working_page() is None
        await asyncio.sleep(0.05)

        state.browser_context = None
        with pytest.raises(ExternalBrowserUnavailable):
            await state.check_and_fix_state()

    rebuild.assert_not_awaited()
    context.new_page.assert_not_awaited()
    context.close.assert_not_awaited()
    for page in context.pages:
        page.close.assert_not_awaited()
