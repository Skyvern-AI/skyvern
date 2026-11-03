from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.exceptions import BrowserSessionAlreadyOccupiedError, BrowserSessionOwnershipConflict
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.schemas.persistent_browser_sessions import SESSION_RETIREMENT_RUNNABLE_TYPE
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye import real_browser_manager as real_browser_manager_mod
from skyvern.webeye.real_browser_manager import RealBrowserManager, _PersistentSessionLease
from tests.unit._external_cdp_fakes import (
    ORG_A,
    acquire_lease,
    begin_lease,
    external_browser_state,
    external_cdp_stack,
    register_external,
    release_lease,
)
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


@pytest.mark.asyncio
async def test_a_stale_owner_cannot_attach_a_registration_a_newer_run_holds() -> None:
    async with external_cdp_stack() as stack:
        session_id = (await register_external(stack.client)).json()["browser_session_id"]
        attached, context, pw = external_browser_state()

        with patch.object(manager_mod, "attach_registered_cdp_browser", AsyncMock(return_value=attached)):
            generation_a = await begin_lease(stack.manager, session_id, "tsk_a")
            assert await release_lease(stack.manager, session_id, "tsk_a", generation_a)
            generation_b = await begin_lease(stack.manager, session_id, "tsk_b")
            assert await acquire_lease(stack.manager, session_id, "tsk_b", generation_b) is attached
            with pytest.raises(BrowserSessionOwnershipConflict):
                await acquire_lease(stack.manager, session_id, "tsk_a", generation_a)

        assert stack.manager._browser_sessions[session_id].browser_state is attached
        pw.stop.assert_not_awaited()
        context.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleaned_up_run", ["wr_competitor", "wr_holder"])
@pytest.mark.parametrize("external", [True, False], ids=["external", "managed"])
async def test_run_cleanup_releases_only_a_lease_that_run_holds(cleaned_up_run: str, external: bool) -> None:
    async with external_cdp_stack() as stack:
        if external:
            session_id = (await register_external(stack.client)).json()["browser_session_id"]
        else:
            managed = await stack.repo.create_persistent_browser_session(organization_id=ORG_A)
            session_id = managed.persistent_browser_session_id
        generation = await begin_lease(stack.manager, session_id, "wr_holder", runnable_type="workflow_run")
        holder_state, _, _ = external_browser_state()
        browser_manager = RealBrowserManager()
        browser_manager._persistent_session_leases["wr_holder"] = _PersistentSessionLease(
            session_id=session_id,
            organization_id=ORG_A,
            runnable_id="wr_holder",
            browser_state=holder_state,
            runnable_generation_id=generation,
        )
        teardown = MagicMock()

        with (
            patch.object(real_browser_manager_mod, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)),
            patch.object(real_browser_manager_mod, "complete_stream_teardown", teardown),
        ):
            await browser_manager.cleanup_for_workflow_run(
                cleaned_up_run,
                [],
                close_browser_on_completion=False,
                browser_session_id=session_id,
                organization_id=ORG_A,
            )

        row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
        assert row is not None
        held = (row.runnable_id, row.runnable_generation_id)
        assert held == (("wr_holder", generation) if cleaned_up_run == "wr_competitor" else (None, None))
        assert (session_id in browser_manager._renewable_session_ids()) is (cleaned_up_run == "wr_competitor")
        teardown.assert_called_once_with(cleaned_up_run)


@pytest.mark.asyncio
async def test_run_cleanup_whose_generation_lost_the_release_keeps_its_stream_teardown_pending() -> None:
    async with external_cdp_stack() as stack:
        session_id = (await register_external(stack.client)).json()["browser_session_id"]
        generation = await begin_lease(stack.manager, session_id, "wr_holder", runnable_type="workflow_run")
        teardown = MagicMock()

        with (
            skyvern_context.scoped(
                SkyvernContext(
                    browser_session_runnable_id="wr_holder", browser_session_runnable_generation_id="gen_stale"
                )
            ),
            patch.object(real_browser_manager_mod, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)),
            patch.object(real_browser_manager_mod, "complete_stream_teardown", teardown),
        ):
            await RealBrowserManager().cleanup_for_workflow_run(
                "wr_holder",
                [],
                close_browser_on_completion=False,
                browser_session_id=session_id,
                organization_id=ORG_A,
            )

        row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
        assert row is not None and (row.runnable_id, row.runnable_generation_id) == ("wr_holder", generation)
        teardown.assert_not_called()


@pytest.mark.asyncio
async def test_parent_cleanup_releases_a_lease_only_its_child_run_took() -> None:
    async with external_cdp_stack() as stack:
        managed = await stack.repo.create_persistent_browser_session(organization_id=ORG_A)
        session_id = managed.persistent_browser_session_id
        generation = await begin_lease(stack.manager, session_id, "wr_child", runnable_type="workflow_run")
        child_state, _, _ = external_browser_state()
        browser_manager = RealBrowserManager()
        browser_manager._persistent_session_leases["wr_child"] = _PersistentSessionLease(
            session_id=session_id,
            organization_id=ORG_A,
            runnable_id="wr_child",
            browser_state=child_state,
            runnable_generation_id=generation,
        )

        with (
            patch.object(real_browser_manager_mod, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=stack.manager)),
            patch.object(real_browser_manager_mod, "complete_stream_teardown", MagicMock()),
        ):
            await browser_manager.cleanup_for_workflow_run(
                "wr_parent",
                [],
                close_browser_on_completion=False,
                browser_session_id=session_id,
                organization_id=ORG_A,
                child_workflow_run_ids=["wr_child"],
            )

        row = await stack.repo.get_persistent_browser_session(session_id, ORG_A)
        assert row is not None and (row.runnable_id, row.runnable_generation_id) == (None, None)
        assert session_id not in browser_manager._renewable_session_ids()
