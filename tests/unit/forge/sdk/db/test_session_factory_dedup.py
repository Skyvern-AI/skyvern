import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB


@pytest.mark.asyncio
async def test_create_task_child_does_not_reuse_session_inherited_before_parent_exit() -> None:
    db = BaseAlchemyDB(create_async_engine("sqlite+aiosqlite:///:memory:"))
    start_child_db_work = asyncio.Event()
    captured = {}

    async def child() -> None:
        await start_child_db_work.wait()
        async with db.Session() as session:
            await session.execute(text("SELECT 1"))
            captured["child"] = session

    async with db.Session() as outer:
        child_task = asyncio.create_task(child())

    start_child_db_work.set()
    await child_task
    await db.engine.dispose()

    assert captured["child"] is not outer


@pytest.mark.asyncio
async def test_none_current_task_does_not_reuse_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    db = BaseAlchemyDB(create_async_engine("sqlite+aiosqlite:///:memory:"))
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    async with db.Session() as outer:
        async with db.Session() as inner:
            assert inner is not outer

    await db.engine.dispose()


@pytest.mark.asyncio
async def test_pinned_session_commits_durably_on_one_connection_and_stays_out_of_child_tasks(tmp_path: Path) -> None:
    db = BaseAlchemyDB(create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pinned.db'}"))
    checkouts: list[Any] = []
    event.listen(db.engine.sync_engine.pool, "checkout", lambda *args: checkouts.append(args))
    async with db.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE runs (id INTEGER)"))
    checkouts.clear()
    captured: dict[str, AsyncSession] = {}

    async def child() -> None:
        async with db.Session() as session:
            captured["child"] = session

    async with db.Session.pinned() as pinned:
        for run_id in (1, 2):
            async with db.Session() as session:
                assert session is pinned
                await session.execute(text("INSERT INTO runs VALUES (:id)"), {"id": run_id})
                await session.commit()
        await asyncio.create_task(child())
        pinned_checkouts = len(checkouts)

    async with db.engine.connect() as reader:
        rows = (await reader.execute(text("SELECT id FROM runs ORDER BY id"))).scalars().all()
    await db.engine.dispose()

    assert rows == [1, 2]
    # Both committed transactions ran on the pinned session's single checkout.
    assert pinned_checkouts == 1
    assert captured["child"] is not pinned


@pytest.mark.asyncio
async def test_released_block_holds_no_connection_and_the_session_keeps_committing(tmp_path: Path) -> None:
    db = BaseAlchemyDB(create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'released.db'}"))
    pool = db.engine.sync_engine.pool
    async with db.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE runs (id INTEGER)"))

    async with db.Session.pinned() as session:
        await session.execute(text("INSERT INTO runs VALUES (1)"))
        await session.commit()
        async with db.Session.released():
            checked_out_while_released = pool.checkedout()
        await session.execute(text("INSERT INTO runs VALUES (2)"))
        await session.commit()
        async with db.Session.released():
            # A read here opens a transaction, as a flag-data freshness check does; it keeps its own connection.
            await session.execute(text("SELECT count(*) FROM runs"))
        await session.execute(text("INSERT INTO runs VALUES (3)"))
        await session.commit()

    async with db.engine.connect() as reader:
        rows = (await reader.execute(text("SELECT id FROM runs ORDER BY id"))).scalars().all()
    checked_out_after_scope = pool.checkedout()
    await db.engine.dispose()

    assert checked_out_while_released == 0
    assert rows == [1, 2, 3]
    assert checked_out_after_scope == 0
