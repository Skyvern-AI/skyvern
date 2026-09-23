import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, null, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.exceptions import BrowserSessionAlreadyEndedError
from skyvern.forge.sdk.db.datetime_utils import to_naive_utc
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

ORG_ID = "org_test"
SESSION_ID = "pbs_test"
STARTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
COMPLETED_AT = datetime(2026, 1, 2)


async def _repo(sqlite_engine: AsyncEngine, **fields):
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            PersistentBrowserSessionModel(
                persistent_browser_session_id=SESSION_ID,
                organization_id=ORG_ID,
                **fields,
            )
        )
        await session.commit()
    return BrowserSessionsRepository(session_factory=factory), factory


async def _snapshot(factory: async_sessionmaker) -> dict[str, object]:
    async with factory() as session:
        result = await session.execute(select(PersistentBrowserSessionModel.__table__))
        return dict(result.mappings().one())


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["created", None])
async def test_update_persists_startup_and_preserves_omitted_started_at(
    sqlite_engine: AsyncEngine, initial_status: str | None
) -> None:
    repo, factory = await _repo(sqlite_engine, status=initial_status if initial_status else null())
    result = await repo.update_persistent_browser_session(
        SESSION_ID,
        organization_id=ORG_ID,
        status="running",
        started_at=STARTED_AT,
        browser_address="wss://proxy.example/client",
        upstream_cdp_url="wss://upstream.example/browser",
    )
    assert result.status == "running"
    assert result.started_at == to_naive_utc(STARTED_AT)
    await repo.update_persistent_browser_session(SESSION_ID, organization_id=ORG_ID, status="retry")
    row = await _snapshot(factory)
    assert row["status"] == "retry"
    assert row["started_at"] == to_naive_utc(STARTED_AT)
    assert row["browser_address"] == result.browser_address
    assert row["upstream_cdp_url"] == result.upstream_cdp_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_status", "completed_at"),
    [("failed", None), ("running", COMPLETED_AT), (None, COMPLETED_AT)],
)
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "running"},
        {"started_at": STARTED_AT},
        {"browser_address": "wss://proxy.example/late"},
        {"upstream_cdp_url": "wss://upstream.example/late"},
    ],
)
async def test_terminal_indicators_independently_reject_general_liveness(
    sqlite_engine: AsyncEngine, stored_status: str | None, completed_at: datetime | None, payload: dict
) -> None:
    repo, factory = await _repo(
        sqlite_engine, status=stored_status if stored_status else null(), completed_at=completed_at
    )
    before = await _snapshot(factory)
    with pytest.raises(BrowserSessionAlreadyEndedError):
        await repo.update_persistent_browser_session(SESSION_ID, organization_id=ORG_ID, **payload)
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["general", "address"])
@pytest.mark.parametrize("scope", ["missing", "wrong_org", "deleted"])
async def test_liveness_rejection_does_not_disclose_out_of_scope_rows(
    sqlite_engine: AsyncEngine, method: str, scope: str
) -> None:
    repo, factory = await _repo(sqlite_engine, status="failed", deleted_at=COMPLETED_AT if scope == "deleted" else None)
    before = await _snapshot(factory)
    session_id = "pbs_missing" if scope == "missing" else SESSION_ID
    org_id = "org_other" if scope == "wrong_org" else ORG_ID
    with pytest.raises(NotFoundError):
        if method == "general":
            await repo.update_persistent_browser_session(session_id, organization_id=org_id, status="running")
        else:
            await repo.set_persistent_browser_session_browser_address(
                session_id, "wss://proxy.example/late", None, None, organization_id=org_id, mark_started=True
            )
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_terminal_reconciliation_and_metadata_remain_writable(sqlite_engine: AsyncEngine) -> None:
    repo, factory = await _repo(sqlite_engine, status="timeout", completed_at=COMPLETED_AT, download_run_id="wr_1")
    result = await repo.update_persistent_browser_session(
        SESSION_ID,
        organization_id=ORG_ID,
        status="completed",
        started_at=STARTED_AT,
        browser_address="wss://proxy.example/archived",
        upstream_cdp_url="wss://upstream.example/archived",
    )
    assert result.status == "completed"
    assert result.completed_at == COMPLETED_AT
    assert result.started_at == to_naive_utc(STARTED_AT)
    result = await repo.update_persistent_browser_session(
        SESSION_ID,
        organization_id=ORG_ID,
        timeout_minutes=30,
        generate_browser_profile=True,
        browser_profile_loaded=True,
    )
    row = await _snapshot(factory)
    assert row["timeout_minutes"] == result.timeout_minutes == 30
    assert row["generate_browser_profile"] is row["browser_profile_loaded"] is True
    assert row["download_run_id"] is None
    assert row["browser_address"] == "wss://proxy.example/archived"
    assert row["upstream_cdp_url"] == "wss://upstream.example/archived"
    assert row["completed_at"] == COMPLETED_AT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", None])
async def test_sqlite_close_committed_before_liveness_mutation_wins(
    sqlite_engine: AsyncEngine, status: str | None
) -> None:
    repo, factory = await _repo(sqlite_engine, status="created", download_run_id="wr_1")
    terminal_snapshot = None
    interleaved = False

    async def close_in_another_session(_driver_connection) -> None:
        nonlocal terminal_snapshot
        await repo.update_persistent_browser_session(
            SESSION_ID, organization_id=ORG_ID, status="failed", completed_at=COMPLETED_AT
        )
        terminal_snapshot = await _snapshot(factory)

    def before_mutation(connection, cursor, statement, parameters, context, executemany) -> None:
        nonlocal interleaved
        if not interleaved and context.isupdate:
            interleaved = True
            # The startup connection has not executed its UPDATE yet. A second real session
            # commits the close here, after any startup read and before the mutation itself.
            connection.connection.dbapi_connection.run_async(close_in_another_session)

    event.listen(sqlite_engine.sync_engine, "before_cursor_execute", before_mutation)
    try:
        rejection = None
        try:
            await asyncio.wait_for(
                repo.update_persistent_browser_session(
                    SESSION_ID,
                    organization_id=ORG_ID,
                    status=status,
                    started_at=STARTED_AT,
                    browser_address="wss://proxy.example/late",
                    upstream_cdp_url="wss://upstream.example/late",
                    timeout_minutes=90,
                    generate_browser_profile=True,
                    browser_profile_loaded=True,
                ),
                timeout=5,
            )
        except BrowserSessionAlreadyEndedError as exc:
            rejection = exc
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", before_mutation)

    assert interleaved and terminal_snapshot is not None
    assert await _snapshot(factory) == terminal_snapshot
    assert isinstance(rejection, BrowserSessionAlreadyEndedError)
