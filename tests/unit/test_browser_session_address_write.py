"""Tests for BrowserSessionsRepository routing-column behavior: address writes, vendor-held
session creation, and customer-facing visibility filtering."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import Base, PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session

UPSTREAM = "ws://10.0.0.7:9222/devtools/browser/b1"
PROXIED = "wss://proxy.example/pbs_123/token/devtools/browser/b1"
VENDOR_UPSTREAM = "wss://connect.vendor.example?sessionId=deadbeef-1234"
ORG_ID = "org_test"


async def _write_address(mock_session: MagicMock) -> None:
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))
    await repo.set_persistent_browser_session_browser_address(
        browser_session_id="pbs_123",
        browser_address=PROXIED,
        ip_address="10.0.0.7",
        ecs_task_arn=None,
        organization_id="org_123",
        upstream_cdp_url=UPSTREAM,
        browser_vendor="websocket",
    )


@pytest.mark.asyncio
async def test_address_write_persists_the_routing_fields() -> None:
    mock_pbs = MagicMock()
    await _write_address(make_mock_session(mock_pbs))

    assert mock_pbs.browser_address == PROXIED
    assert mock_pbs.upstream_cdp_url == UPSTREAM
    assert mock_pbs.browser_vendor == "websocket"


@pytest.mark.asyncio
async def test_failed_address_write_never_renders_the_upstream_in_the_error() -> None:
    """A failed commit renders its bound parameters, and callers log the error text."""
    mock_session = make_mock_session(MagicMock())
    mock_session.commit.side_effect = IntegrityError(
        "UPDATE persistent_browser_sessions SET upstream_cdp_url=%(upstream_cdp_url)s",
        {"upstream_cdp_url": UPSTREAM},
        Exception("duplicate key value violates unique constraint"),
    )

    with pytest.raises(IntegrityError) as excinfo:
        await _write_address(mock_session)

    assert UPSTREAM not in str(excinfo.value)


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
