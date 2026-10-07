"""Shared fixtures for the DB-repository unit tests: AgentDBs on SQLite and, where available, local PostgreSQL."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.config import settings
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import Base

_LOCAL_POSTGRES_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def agent_db(db_engine: Any) -> AsyncGenerator[AgentDB]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=db_engine)


@pytest.fixture(scope="session")
def postgres_scratch_schema_url() -> Iterator[URL]:
    """DATABASE_STRING aimed at a throwaway schema built from the models, so no test row lands in public."""
    url = make_url(settings.DATABASE_STRING)
    if url.get_backend_name() != "postgresql" or url.host not in _LOCAL_POSTGRES_HOSTS:
        pytest.skip("needs a local or CI PostgreSQL")
    schema = f"test_scratch_{uuid.uuid4().hex}"
    admin = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            # A migrated database has pg_trgm in public; an unmigrated one gets it here and loses it with the schema.
            conn.exec_driver_sql(f'CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA "{schema}"')
            # With public on the path, existence checks would find its tables, hence checkfirst=False.
            conn.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
            Base.metadata.create_all(conn, checkfirst=False)
        try:
            yield url.update_query_dict({"options": f"-c search_path={schema},public"})
        finally:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        admin.dispose()


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def org_scoped_db(
    request: pytest.FixtureRequest, sqlite_schema_template: Path, tmp_path: Path
) -> AsyncGenerator[tuple[AgentDB, AsyncEngine, str]]:
    """An AgentDB holding one fresh organization, on an engine built the way production builds it."""
    if request.param == "sqlite":
        db_path = tmp_path / "org_scoped.db"
        shutil.copyfile(sqlite_schema_template, db_path)
        database_string = f"sqlite+aiosqlite:///{db_path}"
    else:
        scratch_url: URL = request.getfixturevalue("postgres_scratch_schema_url")
        database_string = scratch_url.render_as_string(hide_password=False)
    engine = _build_engine(database_string)
    db = AgentDB(database_string, db_engine=engine)
    try:
        organization = await db.organizations.create_organization(f"org_scoped_db_{uuid.uuid4().hex}")
        yield db, engine, organization.organization_id
    finally:
        await engine.dispose()
