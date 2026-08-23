from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import BrowserSessionAlreadyOccupiedError
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from tests.unit.conftest import MockAsyncSessionCtx


def _repository(session: AsyncMock) -> BrowserSessionsRepository:
    return BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(session))


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
