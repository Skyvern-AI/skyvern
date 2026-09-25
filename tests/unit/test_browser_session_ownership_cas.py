from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.exceptions import BrowserSessionAlreadyOccupiedError
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.schemas.persistent_browser_sessions import SESSION_RETIREMENT_RUNNABLE_TYPE
from tests.unit.conftest import MockAsyncSessionCtx


def _repository(session: AsyncMock) -> BrowserSessionsRepository:
    return BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(session))


@pytest.mark.asyncio
@pytest.mark.parametrize("same_owner", [False, True], ids=["unleased", "same-owner"])
@pytest.mark.parametrize(
    "expected_generation", [None, "gen_old", "gen_obsolete"], ids=["unguarded", "current", "obsolete"]
)
@pytest.mark.parametrize(
    "state,allowed",
    [
        ("created", True),
        ("running", True),
        ("retry", True),
        ("legacy-null", True),
        ("completed", False),
        ("failed", False),
        ("timeout", False),
        ("deleted_at", False),
        ("completed_at", False),
        ("close_requested_at", False),
        ("retired", False),
        ("other-owner", False),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_occupy_persists_only_live_lease_admission(
    sqlite_engine: AsyncEngine, state: str, allowed: bool, same_owner: bool, expected_generation: str | None
) -> None:
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repository = BrowserSessionsRepository(session_factory=session_factory)
    row = PersistentBrowserSessionModel(
        persistent_browser_session_id="pbs_1",
        organization_id="org_1",
        status="running",
        runnable_type="workflow_run" if same_owner else None,
        runnable_id="wr_owner" if same_owner else None,
        runnable_generation_id="gen_old" if same_owner else None,
        download_run_id="wr_download_old" if same_owner else None,
    )
    if state.endswith("_at"):
        setattr(row, state, datetime(2026, 1, 1))
    elif state == "retired":
        row.runnable_type = SESSION_RETIREMENT_RUNNABLE_TYPE
        row.runnable_id = "wr_owner" if same_owner else "wr_retirement"
    elif state == "other-owner":
        row.runnable_id = "wr_other"
    else:
        row.status = state
    fields = ("runnable_type", "runnable_id", "runnable_generation_id", "download_run_id")
    previous_lease = tuple(getattr(row, field) for field in fields)
    allowed = allowed and (expected_generation is None or (same_owner and expected_generation == "gen_old"))
    async with session_factory() as session:
        session.add(row)
        if state == "legacy-null":
            await session.flush()
            row.status = None
        await session.commit()

    if allowed:
        await repository.occupy_persistent_browser_session(
            "pbs_1",
            "workflow_run",
            "wr_owner",
            "org_1",
            runnable_generation_id="gen_new",
            expected_runnable_generation_id=expected_generation,
        )
    else:
        error = NotFoundError if state == "deleted_at" else BrowserSessionAlreadyOccupiedError
        with pytest.raises(error):
            await repository.occupy_persistent_browser_session(
                "pbs_1",
                "workflow_run",
                "wr_owner",
                "org_1",
                runnable_generation_id="gen_new",
                expected_runnable_generation_id=expected_generation,
            )

    async with session_factory() as session:
        persisted = await session.get(PersistentBrowserSessionModel, "pbs_1")
        assert persisted is not None
        lease = tuple(getattr(persisted, field) for field in fields)
        assert lease == (("workflow_run", "wr_owner", "gen_new", "wr_owner") if allowed else previous_lease)


@pytest.mark.asyncio
async def test_occupy_uses_single_update_guarded_by_current_runnable() -> None:
    updated = MagicMock()
    update_result = MagicMock()
    update_result.first.return_value = updated
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=update_result)
    repository = _repository(session)

    await repository.occupy_persistent_browser_session(
        "pbs_1",
        "workflow_run",
        "wr_owner",
        "org_1",
        runnable_generation_id="gen_new",
    )

    statement = session.scalars.await_args.args[0]
    where_sql = str(statement.whereclause)
    assert "runnable_id IS NULL" in where_sql
    assert "runnable_id =" in where_sql
    assert "runnable_generation_id" in str(statement)
    session.commit.assert_awaited_once_with()
    session.refresh.assert_awaited_once_with(updated)


@pytest.mark.asyncio
async def test_occupy_cas_miss_reports_the_winning_owner_without_overwrite() -> None:
    update_result = MagicMock()
    update_result.first.return_value = None
    existing = MagicMock(runnable_id="wr_winner")
    lookup_result = MagicMock()
    lookup_result.first.return_value = existing
    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[update_result, lookup_result])
    repository = _repository(session)

    with pytest.raises(BrowserSessionAlreadyOccupiedError, match="wr_winner"):
        await repository.occupy_persistent_browser_session(
            "pbs_1",
            "workflow_run",
            "wr_loser",
            "org_1",
            runnable_generation_id="gen_loser",
        )

    session.commit.assert_not_awaited()
    assert session.scalars.await_count == 2


@pytest.mark.asyncio
async def test_release_update_is_guarded_by_exact_expected_runnable() -> None:
    update_result = MagicMock()
    update_result.first.return_value = None
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=update_result)
    repository = _repository(session)

    released = await repository.release_persistent_browser_session(
        "pbs_1",
        "org_1",
        expected_runnable_id="wr_owner",
        expected_runnable_generation_id="gen_old",
    )

    assert released is None
    statement = session.scalars.await_args.args[0]
    where_sql = str(statement.whereclause)
    assert "runnable_id =" in where_sql
    assert "runnable_generation_id =" in where_sql
    session.commit.assert_not_awaited()
