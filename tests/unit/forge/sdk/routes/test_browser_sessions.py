import asyncio
import json
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.responses import ORJSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import (
    BrowserSessionExtensionUnconfirmed,
    BrowserSessionNotExtendable,
    ExternalBrowserUnavailable,
)
from skyvern.forge import app as forge_app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import API_BROWSER_SESSION_CREATED_BY
from skyvern.schemas.browser_session_timeouts import max_lifetime_exceeded_warning
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from skyvern.webeye.persistent_sessions_manager import (
    BrowserSessionCreditAdmissionRefusal,
    BrowserSessionExtension,
)
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.schemas import BrowserSessionResponse
from tests.unit._external_cdp_fakes import CDP_URL as EXTERNAL_CDP_URL
from tests.unit._external_cdp_fakes import TOKEN as EXTERNAL_TOKEN
from tests.unit._external_cdp_fakes import external_browser_state, external_cdp_stack, register_external


async def _extend(app_mock: MagicMock, additional_minutes: int) -> Any:
    built_response = BrowserSessionResponse(
        browser_session_id="pbs_1",
        organization_id="org_1",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )
    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(
            browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock(return_value=built_response)
        ),
    ):
        return await browser_sessions_mod.extend_browser_session(
            browser_sessions_mod.ExtendBrowserSessionRequest(additional_minutes=additional_minutes),
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )


async def _close(app_mock: MagicMock) -> ORJSONResponse:
    with patch.object(browser_sessions_mod, "app", app_mock):
        return await browser_sessions_mod.close_browser_session(
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )


@pytest.mark.asyncio
async def test_extend_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _extend(app_mock, 30)

    assert exc_info.value.status_code == 404
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_awaited_once_with("pbs_1", "org_1")
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_extend_browser_session_reports_a_clamped_grant_as_a_warning() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=20)
    )

    clamped = await _extend(app_mock, 90)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_awaited_once_with("pbs_1", "org_1", 90)
    assert clamped.warning == max_lifetime_exceeded_warning(90, 20)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=90)
    )
    assert (await _extend(app_mock, 90)).warning is None


@pytest.mark.asyncio
async def test_extend_browser_session_maps_refusals_to_conflict() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="completed"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as ended:
        await _extend(app_mock, 30)
    assert ended.value.status_code == 409
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()

    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionNotExtendable("browser session has expired", "pbs_1")
    )
    with pytest.raises(HTTPException) as refused:
        await _extend(app_mock, 30)
    assert refused.value.status_code == 409
    assert "expired" in refused.value.detail


@pytest.mark.asyncio
async def test_an_unconfirmed_extension_is_accepted_not_failed() -> None:
    """The signal landed, so a retry adds again. The SDK retries 409 and every 5xx on its own, so the
    only honest answer is a 2xx that says the effect is not confirmed and points at GET."""
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionExtensionUnconfirmed("pbs_1")
    )

    accepted = await _extend(app_mock, 30)

    assert accepted.status_code == 202
    body = json.loads(accepted.body)
    assert "Do not retry" in body["warning"]


@pytest.mark.asyncio
async def test_close_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await browser_sessions_mod.close_browser_session(
            "pbs_foreign",
            current_org=SimpleNamespace(organization_id="org_requester"),
        )

    assert exc_info.value.status_code == 404
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_foreign", "org_requester"
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_browser_session_skips_close_for_a_completed_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status="completed", completed_at=datetime(2026, 1, 1))
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    response = await _close(app_mock)

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Browser session closed"}
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "failed"])
async def test_close_browser_session_closes_any_session_without_completed_at(status: str) -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status=status, completed_at=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    response = await _close(app_mock)

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Browser session closed"}
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_1", "pbs_1")


@pytest.mark.asyncio
async def test_close_browser_session_propagates_close_failure_for_a_live_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status="running", completed_at=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(RuntimeError, match="close failed"):
        await _close(app_mock)


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_live_view", [True, False])
async def test_create_browser_session_forwards_whether_the_session_will_be_watched(
    needs_live_view: bool,
) -> None:
    """Dropped here, the session is still created and still connects — it only fails later, when
    someone opens the live view. Defaulting it False is what keeps unattended automation routable."""
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(needs_live_view=needs_live_view),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["needs_live_view"] is needs_live_view


@pytest.mark.asyncio
async def test_create_browser_session_preserves_typed_credit_refusal_http_contract() -> None:
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=BrowserSessionCreditAdmissionRefusal())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == "Credits exhausted. Upgrade your plan in Billing."


@pytest.mark.asyncio
async def test_create_browser_session_observes_before_creating_the_session() -> None:
    calls: list[str] = []
    app_mock = MagicMock()

    async def observe(**_: object) -> None:
        calls.append("observe")

    async def create_session(**_: object) -> MagicMock:
        calls.append("create")
        return MagicMock()

    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock(side_effect=observe)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=create_session)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(),
            current_org=SimpleNamespace(organization_id="org_1"),
            user_id="user_1",
        )

    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access.assert_awaited_once_with(
        organization_id="org_1",
        feature_names={"standalone_browser_sessions"},
    )
    assert calls == ["observe", "create"]


def test_a_session_request_is_unwatched_unless_it_says_otherwise() -> None:
    """The public default. True would route every API-created session to first-party and make the
    provider rollout unreachable."""
    assert browser_sessions_mod.CreateBrowserSessionRequest().needs_live_view is False


@pytest.mark.asyncio
async def test_get_browser_session_enables_strict_download_lookup() -> None:
    browser_session = MagicMock()
    response = MagicMock()
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=browser_session)
    from_browser_session = AsyncMock(return_value=response)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        result = await browser_sessions_mod.get_browser_session(
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert result is response
    from_browser_session.assert_awaited_once_with(
        browser_session,
        app_mock.STORAGE,
        fail_download_lookup=True,
        include_stream_transport=True,
        concurrent_listings=True,
    )


@pytest.mark.parametrize("endpoint", ["history", "active"])
@pytest.mark.asyncio
async def test_fan_out_listings_keep_each_sessions_storage_listings_sequential(endpoint: str) -> None:
    """These endpoints already gather across sessions, so overlapping a session's two listings would
    only raise the request's peak pool checkouts from N to 2N."""
    sessions = [MagicMock(), MagicMock()]
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_sessions_history = AsyncMock(return_value=sessions)
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_active_sessions = AsyncMock(return_value=sessions)
    from_browser_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        if endpoint == "history":
            await browser_sessions_mod.get_browser_sessions_all(
                current_org=SimpleNamespace(organization_id="org_1"), page=1, page_size=100
            )
        else:
            await browser_sessions_mod.get_browser_sessions(current_org=SimpleNamespace(organization_id="org_1"))

    assert from_browser_session.await_count == len(sessions)
    for call in from_browser_session.await_args_list:
        assert call.kwargs.get("concurrent_listings", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "expected_stored_created_by", "expected_response_created_by"),
    [
        ("user_creator", "user_creator", "user_creator"),
        (None, API_BROWSER_SESSION_CREATED_BY, None),
    ],
)
async def test_create_and_history_hide_api_origin_but_preserve_real_user_id(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: AsyncEngine,
    user_id: str | None,
    expected_stored_created_by: str,
    expected_response_created_by: str | None,
) -> None:
    db = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    org = await db.organizations.create_organization(organization_name="Creator Org")
    monkeypatch.setattr(forge_app.DATABASE, "browser_sessions", db.browser_sessions)
    monkeypatch.setattr(forge_app, "PERSISTENT_SESSIONS_MANAGER", DefaultPersistentSessionsManager(database=db))
    monkeypatch.setattr(forge_app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(forge_app, "STORAGE", None)

    created = await browser_sessions_mod.create_browser_session(
        browser_sessions_mod.CreateBrowserSessionRequest(),
        current_org=org,
        user_id=user_id,
    )
    [listed] = await browser_sessions_mod.get_browser_sessions_all(current_org=org, page=1, page_size=10)
    [stored] = await db.browser_sessions.get_persistent_browser_sessions_history(
        org.organization_id, page=1, page_size=10
    )

    assert created.model_dump()["created_by"] == expected_response_created_by
    assert listed.model_dump()["created_by"] == expected_response_created_by
    assert stored.created_by == expected_stored_created_by


@pytest_asyncio.fixture
async def external_stack() -> AsyncIterator[SimpleNamespace]:
    async with external_cdp_stack() as built:
        yield built


async def _external_row_count(stack: SimpleNamespace) -> int:
    async with stack.session_factory() as session:
        return len((await session.execute(PersistentBrowserSessionModel.__table__.select())).all())


@pytest.mark.asyncio
async def test_external_registration_refuses_a_private_host_outside_local_development(
    external_stack: SimpleNamespace,
) -> None:
    probe = AsyncMock()
    with (
        patch.object(settings, "ENV", "production"),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", probe),
    ):
        for address in ("ws://10.0.0.5:9222/devtools/browser/b1", "http://169.254.169.254:9222"):
            response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": address})
            assert response.status_code == 400
        plaintext = await external_stack.client.post(
            "/v1/browser_sessions/external", json={"cdp_url": "ws://93.184.216.34:9222/devtools/browser/b1"}
        )
        assert plaintext.status_code == 422 and "wss://" in plaintext.text
    probe.assert_not_awaited()
    assert await _external_row_count(external_stack) == 0


@pytest.mark.asyncio
async def test_an_invalid_external_registration_never_echoes_the_submitted_address(
    external_stack: SimpleNamespace,
) -> None:
    with capture_logs() as logs:
        for body in ({"cdp_url": f"not a url {EXTERNAL_TOKEN}"}, {"cdp_url": EXTERNAL_CDP_URL, "timeout": 100_000}):
            response = await external_stack.client.post("/v1/browser_sessions/external", json=body)
            assert response.status_code == 422
            assert EXTERNAL_TOKEN not in response.text
    assert EXTERNAL_TOKEN not in repr(logs)


async def _reject_upgrade(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    await writer.drain()
    writer.close()


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["refused", "rejected"])
async def test_an_unreachable_external_endpoint_reports_its_cause_and_leaves_no_session(
    endpoint: str, external_stack: SimpleNamespace
) -> None:
    server = await asyncio.start_server(_reject_upgrade, "127.0.0.1", 0) if endpoint == "rejected" else None
    port = server.sockets[0].getsockname()[1] if server else _unused_port()
    dead_address = f"ws://127.0.0.1:{port}/devtools/browser/b1?token={EXTERNAL_TOKEN}"
    try:
        with capture_logs() as logs:
            response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": dead_address})
    finally:
        if server:
            server.close()

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "unavailable" in detail and "connection failed" in detail
    for leaked in (EXTERNAL_TOKEN, "devtools/browser/b1"):
        assert leaked not in response.text
        assert leaked not in repr(logs)
    assert await external_stack.repo.get_uncompleted_persistent_browser_sessions() == []
    async with external_stack.session_factory() as session:
        attempts = (await session.execute(select(PersistentBrowserSessionModel))).scalars().all()
    assert [(row.status, row.upstream_cdp_url) for row in attempts] == [("failed", None)]


@pytest.mark.asyncio
async def test_failed_registration_attempts_are_limited_per_minute(external_stack: SimpleNamespace) -> None:
    probe = AsyncMock(side_effect=ExternalBrowserUnavailable("connection failed"))
    limit = browser_sessions_mod.MAX_EXTERNAL_REGISTRATION_ATTEMPTS_PER_MINUTE
    with patch.object(browser_sessions_mod, "connect_external_cdp_browser", probe):
        statuses = [
            (
                await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": EXTERNAL_CDP_URL})
            ).status_code
            for _ in range(limit + 1)
        ]

    assert statuses == [503] * limit + [429]
    assert probe.await_count == limit


@pytest.mark.asyncio
async def test_reservations_abandoned_mid_probe_do_not_lock_the_org_out(external_stack: SimpleNamespace) -> None:
    for _ in range(browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS):
        assert await external_stack.repo.reserve_external_cdp_session(
            "org_a", 60, max_open=100, max_attempts_per_minute=100
        )
    async with external_stack.session_factory() as session:
        await session.execute(update(PersistentBrowserSessionModel).values(created_at=datetime(2000, 1, 1)))
        await session.commit()

    assert (await register_external(external_stack.client)).status_code == 200
    open_rows = await external_stack.repo.get_uncompleted_persistent_browser_sessions()
    assert len(open_rows) == 1 and open_rows[0].upstream_cdp_url is not None


@pytest.mark.asyncio
async def test_a_probe_that_cannot_detach_fails_the_registration(external_stack: SimpleNamespace) -> None:
    probe_state, _, _ = external_browser_state()
    with (
        patch.object(probe_state, "close", AsyncMock(side_effect=TimeoutError())),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", AsyncMock(return_value=probe_state)),
    ):
        response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": EXTERNAL_CDP_URL})

    assert response.status_code == 503 and "connection failed" in response.json()["detail"]
    assert await external_stack.repo.get_uncompleted_persistent_browser_sessions() == []


@pytest.mark.asyncio
async def test_registration_probes_beyond_the_process_bound_are_refused_at_once(
    external_stack: SimpleNamespace,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    probe_state, _, _ = external_browser_state()

    async def slow_probe(_: str) -> RealBrowserState:
        started.set()
        await release.wait()
        return probe_state

    body = {"cdp_url": EXTERNAL_CDP_URL}
    with (
        patch.object(browser_sessions_mod, "_REGISTRATION_PROBES", asyncio.Semaphore(1)),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", slow_probe),
    ):
        first = asyncio.create_task(external_stack.client.post("/v1/browser_sessions/external", json=body))
        await started.wait()
        try:
            busy = await asyncio.wait_for(
                external_stack.client.post("/v1/browser_sessions/external", json=body), timeout=5
            )
        finally:
            release.set()
        assert (await first).status_code == 200

    assert busy.status_code == 429
    assert len(await external_stack.repo.get_uncompleted_persistent_browser_sessions()) == 1


@pytest.mark.asyncio
async def test_an_external_registration_cannot_be_extended(external_stack: SimpleNamespace) -> None:
    session_id = (await register_external(external_stack.client)).json()["browser_session_id"]
    response = await external_stack.client.post(
        f"/v1/browser_sessions/{session_id}/extend", json={"additional_minutes": 5}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_external_registrations_are_capped_per_organization(external_stack: SimpleNamespace) -> None:
    for _ in range(browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS):
        assert (await register_external(external_stack.client)).status_code == 200

    refused = await register_external(external_stack.client)

    assert refused.status_code == 429
    assert await _external_row_count(external_stack) == browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS

    async with external_stack.session_factory() as session:
        await session.execute(update(PersistentBrowserSessionModel).values(started_at=datetime(2000, 1, 1)))
        await session.commit()
    assert (await register_external(external_stack.client)).status_code == 200
    open_rows = await external_stack.repo.get_uncompleted_persistent_browser_sessions()
    assert len(open_rows) == 1 and open_rows[0].upstream_cdp_url is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_status,run_status", [("running", "running"), ("retry", "paused")])
async def test_metadata_reads_orphaned_session_without_any_lifecycle_or_transport_calls(browser_status, run_status):
    session = SimpleNamespace(
        persistent_browser_session_id="pbs_1",
        organization_id="org_1",
        status=browser_status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=None,
        runnable_id="wr_1",
        timeout_minutes=30,
        browser_address="PRIVATE_TRANSPORT",
        browser_settings={"secret": "PRIVATE_SETTING"},
    )
    run = {
        "workflow_run_id": "wr_1",
        "organization_id": "org_1",
        "browser_session_id": "pbs_1",
        "association_browser_session_id": "pbs_1",
        "workflow_permanent_id": "wpid_1",
        "copilot_session_id": "wcc_1",
        "status": run_status,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "finished_at": None,
        "credits_used": 3,
        "cached_credits_used": 1,
        "prompt": "PRIVATE_PROMPT",
        "parameters": {"password": "PRIVATE_PASSWORD"},
    }

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("Metadata must not access lifecycle, transport, provider or write functions")

    class BrowserRows:
        async def get_persistent_browser_session(self, session_id, organization_id):
            assert (session_id, organization_id) == ("pbs_1", "org_1")
            return session

        def __getattr__(self, name):
            raise AssertionError("Browser metadata cannot write")

    class WorkflowRows:
        async def get_workflow_metadata_for_browser_session(self, session_id, organization_id):
            assert (session_id, organization_id) == ("pbs_1", "org_1")
            return [run]

        def __getattr__(self, name):
            raise AssertionError("Workflow metadata cannot write")

    app_readonly = SimpleNamespace(
        DATABASE=SimpleNamespace(browser_sessions=BrowserRows(), workflow_runs=WorkflowRows()),
        PERSISTENT_SESSIONS_MANAGER=Forbidden(),
        STORAGE=Forbidden(),
        WORKFLOW_SERVICE=Forbidden(),
        AGENT_FUNCTION=Forbidden(),
    )
    with patch.object(browser_sessions_mod, "app", app_readonly):
        response = await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert response.status == browser_status and response.completed_at is None
    assert response.associated_workflow_runs[0].status == run_status
    assert response.associated_workflow_runs[0].copilot_session_id == "wcc_1"
    assert response.association_index_complete is True
    assert "PRIVATE_" not in response.model_dump_json()
    assert session.status == browser_status and session.timeout_minutes == 30 and session.completed_at is None


@pytest.mark.asyncio
async def test_metadata_wrong_org_or_missing_session_returns_404_before_associations():
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock()
    with patch.object(browser_sessions_mod, "app", app_mock), pytest.raises(HTTPException) as error:
        await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_2")
        )
    assert error.value.status_code == 404
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_awaited_once_with("pbs_1", "org_2")
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_truncation_is_reported_without_pagination_or_reconciliation():
    session = SimpleNamespace(
        persistent_browser_session_id="pbs_1",
        organization_id="org_1",
        status="completed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        runnable_id=None,
    )
    run = {
        "workflow_run_id": "wr_1",
        "organization_id": "org_1",
        "browser_session_id": "pbs_1",
        "association_browser_session_id": "pbs_1",
        "workflow_permanent_id": "wpid_1",
        "copilot_session_id": None,
        "status": "completed",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 1, 2, tzinfo=UTC),
        "credits_used": None,
        "cached_credits_used": None,
    }
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=session)
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock(return_value=[run] * 101)
    with patch.object(browser_sessions_mod, "app", app_mock):
        response = await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert len(response.associated_workflow_runs) == 100 and response.association_index_complete is False
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_missing_stored_status_fails_without_association_or_lifecycle_access():
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status=None)
    )
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock()
    with patch.object(browser_sessions_mod, "app", app_mock), pytest.raises(HTTPException) as error:
        await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert error.value.status_code == 503 and error.value.detail == {"code": "browser_session_metadata_unavailable"}
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_not_called()
