"""Tests for BrowserSessionsRepository routing-column behavior: address writes, vendor-held
session creation, and customer-facing visibility filtering."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy import null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from structlog.testing import LogCapture

from skyvern.exceptions import BrowserSessionAlreadyEndedError
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import Base, PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.forge_log import CustomConsoleRenderer
from tests.unit.conftest import MockAsyncSessionCtx

UPSTREAM = "ws://10.0.0.7:9222/devtools/browser/b1"
PROXIED = "wss://proxy.example/pbs_123/token/devtools/browser/b1"
VENDOR_UPSTREAM = "wss://connect.vendor.example?sessionId=deadbeef-1234"
ORG_ID = "org_test"


async def _repo_with_open_rows(
    *session_ids: str, engine: AsyncEngine | None = None
) -> tuple[BrowserSessionsRepository, async_sessionmaker]:
    """A real (in-memory) engine, not a mock — the address write is a conditional UPDATE whose
    predicate and RETURNING a mocked session cannot exercise."""
    engine = engine or create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PersistentBrowserSessionModel.__table__])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                PersistentBrowserSessionModel(
                    persistent_browser_session_id=session_id,
                    organization_id=ORG_ID,
                    status="created",
                    created_at=naive_utc_now(),
                    started_at=None,
                    completed_at=None,
                )
                for session_id in session_ids
            ]
        )
        await session.commit()
    return BrowserSessionsRepository(session_factory=session_factory), session_factory


async def _read_model(session_factory: async_sessionmaker, session_id: str) -> PersistentBrowserSessionModel:
    async with session_factory() as session:
        row = (
            await session.scalars(
                select(PersistentBrowserSessionModel).filter_by(persistent_browser_session_id=session_id)
            )
        ).first()
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_address_write_persists_the_routing_fields() -> None:
    repo, session_factory = await _repo_with_open_rows("pbs_open")

    await repo.set_persistent_browser_session_browser_address(
        browser_session_id="pbs_open",
        browser_address=PROXIED,
        ip_address="10.0.0.7",
        ecs_task_arn=None,
        organization_id=ORG_ID,
        upstream_cdp_url=UPSTREAM,
        browser_vendor="websocket",
    )

    row = await _read_model(session_factory, "pbs_open")
    assert row.browser_address == PROXIED
    assert row.upstream_cdp_url == UPSTREAM
    assert row.browser_vendor == "websocket"


@pytest.mark.asyncio
async def test_the_session_clock_starts_only_when_the_caller_asks_for_it() -> None:
    """An address that names the session rather than the browser can be published before anything
    is provisioned, and starting the timeout clock there would expire a session that has no
    browser yet — so writing an address no longer implies the session started."""
    repo, session_factory = await _repo_with_open_rows("pbs_unstarted", "pbs_started")

    await repo.set_persistent_browser_session_browser_address(
        browser_session_id="pbs_unstarted",
        browser_address="wss://proxy.example/pbs_unstarted/token/devtools/browser/b1",
        ip_address="10.0.0.7",
        ecs_task_arn=None,
        organization_id=ORG_ID,
        upstream_cdp_url="ws://10.0.0.7:9222/devtools/browser/b1",
        browser_vendor="websocket",
        mark_started=False,
    )
    await repo.set_persistent_browser_session_browser_address(
        browser_session_id="pbs_started",
        browser_address="wss://proxy.example/pbs_started/token/devtools/browser/b2",
        ip_address="10.0.0.8",
        ecs_task_arn=None,
        organization_id=ORG_ID,
        upstream_cdp_url="ws://10.0.0.8:9222/devtools/browser/b2",
        browser_vendor="websocket",
        mark_started=True,
    )

    assert (await _read_model(session_factory, "pbs_unstarted")).started_at is None
    assert (await _read_model(session_factory, "pbs_started")).started_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["general", "address"])
async def test_failed_address_write_never_renders_the_upstream_in_the_error(
    sqlite_engine: AsyncEngine, method: str
) -> None:
    repo, _ = await _repo_with_open_rows("pbs_owner", "pbs_duplicate", engine=sqlite_engine)
    await repo.set_persistent_browser_session_browser_address("pbs_owner", PROXIED, None, None, organization_id=ORG_ID)
    # Already-bound loggers can retain a processor chain that bypasses capture_logs().
    capture = LogCapture()
    logger = structlog.wrap_logger(
        structlog.ReturnLogger(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        processors=[capture],
    )
    with (
        patch("skyvern.forge.sdk.db._error_handling.LOG", logger),
        pytest.raises(IntegrityError) as excinfo,
    ):
        if method == "general":
            await repo.update_persistent_browser_session(
                "pbs_duplicate", organization_id=ORG_ID, browser_address=PROXIED, upstream_cdp_url=UPSTREAM
            )
        else:
            await repo.set_persistent_browser_session_browser_address(
                "pbs_duplicate", PROXIED, None, None, organization_id=ORG_ID, upstream_cdp_url=UPSTREAM
            )
    assert excinfo.value.hide_parameters is True
    assert UPSTREAM not in str(excinfo.value)
    assert PROXIED not in str(excinfo.value)
    assert any(
        entry["event"] == "SQLAlchemyError" and entry["log_level"] == "error" and entry.get("exc_info")
        for entry in capture.entries
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "completed_at"),
    [("failed", None), ("running", datetime(2026, 1, 2)), (None, datetime(2026, 1, 2))],
)
async def test_address_guard_checks_each_terminal_indicator_without_mutating_the_row(
    sqlite_engine: AsyncEngine, status: str | None, completed_at: datetime | None
) -> None:
    repo, factory = await _repo_with_open_rows("pbs_closed", engine=sqlite_engine)
    async with factory() as session:
        row = await session.get(PersistentBrowserSessionModel, "pbs_closed")
        row.status = status if status else null()
        row.completed_at = completed_at
        await session.commit()
        before = dict((await session.execute(select(PersistentBrowserSessionModel.__table__))).mappings().one())
    with pytest.raises(BrowserSessionAlreadyEndedError):
        await repo.set_persistent_browser_session_browser_address(
            "pbs_closed",
            PROXIED,
            "10.0.0.7",
            "synthetic-task-arn",
            organization_id=ORG_ID,
            upstream_cdp_url=UPSTREAM,
            browser_vendor="websocket",
            mark_started=True,
        )
    async with factory() as session:
        after = dict((await session.execute(select(PersistentBrowserSessionModel.__table__))).mappings().one())
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["general", "address"])
async def test_expected_liveness_rejection_never_logs_traceback_locals(sqlite_engine: AsyncEngine, method: str) -> None:
    repo, _ = await _repo_with_open_rows("pbs_closed", engine=sqlite_engine)
    completed_at = datetime(2026, 1, 2)
    await repo.update_persistent_browser_session(
        "pbs_closed", organization_id=ORG_ID, status="failed", completed_at=completed_at
    )
    browser_address = "wss://proxy.example/client-address-canary"
    upstream_cdp_url = "wss://upstream.example/?token=upstream-token-canary"
    rendered: list[str] = []
    renderer = CustomConsoleRenderer()

    def render_event(logger, name, event_dict):
        rendered.append(renderer(logger, name, event_dict.copy()))
        return event_dict

    capture = LogCapture()
    logger = structlog.wrap_logger(
        structlog.ReturnLogger(),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        processors=[render_event, capture],
    )
    with (
        patch("skyvern.forge.sdk.db._error_handling.LOG", logger),
        pytest.raises(BrowserSessionAlreadyEndedError) as caught,
    ):
        if method == "general":
            await repo.update_persistent_browser_session(
                "pbs_closed",
                organization_id=ORG_ID,
                status="running",
                browser_address=browser_address,
                upstream_cdp_url=upstream_cdp_url,
            )
        else:
            await repo.set_persistent_browser_session_browser_address(
                "pbs_closed",
                browser_address,
                None,
                None,
                organization_id=ORG_ID,
                upstream_cdp_url=upstream_cdp_url,
                mark_started=True,
            )

    assert caught.value.browser_session_id == "pbs_closed"
    assert caught.value.status == "failed"
    assert caught.value.completed_at == completed_at
    events = [entry for entry in capture.entries if "operation" in entry]
    assert events and all(entry["event"] == "ExpectedError" for entry in events)
    assert all(not entry.get("exc_info") and "exception" not in entry for entry in events)
    output = str(capture.entries) + "\n".join(rendered)
    assert "UnexpectedError" not in output
    assert "Traceback" not in output
    assert "client-address-canary" not in output
    assert "upstream-token-canary" not in output


@pytest.mark.asyncio
async def test_failed_vendor_insert_never_renders_the_upstream_in_the_error() -> None:
    """Same leak as the address write, on the INSERT path: this upstream is a bearer credential and
    the routing caller logs the failure with exc_info."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock(
        side_effect=IntegrityError(
            "INSERT INTO persistent_browser_sessions (upstream_cdp_url) VALUES (%(upstream_cdp_url)s)",
            {"upstream_cdp_url": VENDOR_UPSTREAM},
            Exception("duplicate key value violates unique constraint"),
        )
    )
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    with pytest.raises(IntegrityError) as excinfo:
        await repo.create_vendor_cdp_browser_session(
            organization_id=ORG_ID,
            upstream_cdp_url=VENDOR_UPSTREAM,
            browser_vendor="websocket",
            browser_id="vendor-sess-1",
            timeout_minutes=240,
        )

    assert VENDOR_UPSTREAM not in str(excinfo.value)


@pytest.mark.asyncio
async def test_create_vendor_cdp_browser_session_insert_shape() -> None:
    """The vendor-held row is a single INSERT: running, timed, upstream-addressed, and left with
    no client-facing address or runnable binding."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    def _validate(model: object) -> MagicMock:
        validated = MagicMock()
        validated.status = model.status
        validated.upstream_cdp_url = model.upstream_cdp_url
        return validated

    with patch(
        "skyvern.forge.sdk.schemas.persistent_browser_sessions.PersistentBrowserSession.model_validate",
        side_effect=_validate,
    ):
        result = await repo.create_vendor_cdp_browser_session(
            organization_id=ORG_ID,
            upstream_cdp_url=VENDOR_UPSTREAM,
            browser_vendor="websocket",
            browser_id="vendor-sess-1",
            timeout_minutes=240,
        )

    inserted = mock_session.add.call_args.args[0]
    assert inserted.organization_id == ORG_ID
    assert inserted.status == "running"
    assert inserted.started_at is not None
    assert inserted.timeout_minutes == 240
    assert inserted.upstream_cdp_url == VENDOR_UPSTREAM
    assert inserted.browser_vendor == "websocket"
    assert inserted.browser_id == "vendor-sess-1"
    assert inserted.browser_address is None
    assert inserted.runnable_type is None
    assert inserted.runnable_id is None
    assert result.status == "running"
    assert result.upstream_cdp_url == VENDOR_UPSTREAM


def _session_row(
    session_id: str,
    *,
    upstream_cdp_url: str | None,
    browser_address: str | None,
    status: str = "running",
    completed_at: datetime | None = None,
) -> PersistentBrowserSessionModel:
    now = naive_utc_now()
    return PersistentBrowserSessionModel(
        persistent_browser_session_id=session_id,
        organization_id=ORG_ID,
        status=status,
        created_at=now,
        started_at=now,
        completed_at=completed_at,
        upstream_cdp_url=upstream_cdp_url,
        browser_address=browser_address,
    )


async def _repo_with_visibility_rows() -> BrowserSessionsRepository:
    """A real (in-memory) engine, not a mock — the exclusion predicate is a SQL WHERE clause, and
    a mocked session can't tell us whether it actually filters rows."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PersistentBrowserSessionModel.__table__])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                # Vendor-held: upstream set, no client-facing address yet — must be hidden.
                _session_row("pbs_vendor", upstream_cdp_url=VENDOR_UPSTREAM, browser_address=None),
                # Self-hosted routed: both set — still visible.
                _session_row("pbs_self_hosted", upstream_cdp_url=UPSTREAM, browser_address=PROXIED),
                # Pre-routing / legacy: neither set — still visible.
                _session_row("pbs_pending", upstream_cdp_url=None, browser_address=None),
            ]
        )
        await session.commit()
    return BrowserSessionsRepository(session_factory=session_factory)


@pytest.mark.asyncio
async def test_get_active_sessions_hides_vendor_held_rows() -> None:
    repo = await _repo_with_visibility_rows()

    sessions = await repo.get_active_persistent_browser_sessions(ORG_ID)

    ids = {session.persistent_browser_session_id for session in sessions}
    assert ids == {"pbs_self_hosted", "pbs_pending"}


@pytest.mark.asyncio
async def test_get_history_hides_vendor_held_rows() -> None:
    repo = await _repo_with_visibility_rows()

    sessions = await repo.get_persistent_browser_sessions_history(ORG_ID)

    ids = {session.persistent_browser_session_id for session in sessions}
    assert ids == {"pbs_self_hosted", "pbs_pending"}


@pytest.mark.asyncio
async def test_get_history_count_hides_vendor_held_rows() -> None:
    repo = await _repo_with_visibility_rows()

    count = await repo.get_persistent_browser_sessions_history_count(ORG_ID)

    assert count == 2
