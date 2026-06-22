import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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
