import functools
import importlib.util
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Engine, create_engine, event, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from skyvern.config import settings
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import DebugSessionModel

INDEX_NAME = "ix_debug_sessions_org_wpid_user_created_at"


@functools.cache
def _migration() -> ModuleType:
    # Matched by suffix: the open-source mirror regenerates this migration under its own date and revision id, and
    # names it after the slug of the docstring's first line, so that line must stay "index debug_sessions lookup".
    versions = Path(__file__).resolve().parents[2] / "alembic/versions"
    matches = sorted(versions.glob("*_index_debug_sessions_lookup.py"))
    if not matches:
        raise AssertionError(f"no index_debug_sessions_lookup migration found in {versions}")
    spec = importlib.util.spec_from_file_location("index_debug_sessions_lookup_migration", matches[-1])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Schema(NamedTuple):
    engine: Engine
    connect_args: dict[str, str]


@pytest.fixture
def scratch_schema() -> Iterator[_Schema]:
    url = make_url(str(settings.DATABASE_STRING))
    if url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL")
    schema = f"debug_sessions_index_{uuid.uuid4().hex}"
    connect_args = {"options": f"-csearch_path={schema}"}
    engine = create_engine(url.set(drivername="postgresql+psycopg"), connect_args=connect_args)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.execute(CreateTable(DebugSessionModel.__table__))
    try:
        yield _Schema(engine, connect_args)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


def _upgrade(engine: Engine) -> None:
    with engine.connect() as connection, Operations.context(MigrationContext.configure(connection)):
        _migration().upgrade()


def _index_state(engine: Engine) -> tuple[bool, str] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT indisvalid, pg_get_indexdef(indexrelid) FROM pg_catalog.pg_index "
                "WHERE indexrelid = to_regclass(:name)"
            ),
            {"name": INDEX_NAME},
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def _session_row(organization_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    return {
        "debug_session_id": f"ds_{name}",
        "organization_id": organization_id,
        "browser_session_id": f"pbs_{name}",
        "workflow_permanent_id": "wpid_target",
        "user_id": "user_target",
        "status": "created",
        "deleted_at": None,
        **overrides,
    }


def _generic_plan(engine: Engine, statement: str, parameters: dict[str, Any]) -> str:
    names: list[str] = []

    def number(match: re.Match[str]) -> str:
        if match.group(1) not in names:
            names.append(match.group(1))
        return f"${names.index(match.group(1)) + 1}"

    prepared = re.sub(r"%\((\w+)\)s", number, statement)
    arguments = ", ".join(
        f"'{value}'" if isinstance(value, str) else str(value) for value in (parameters[name] for name in names)
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
        # A seq scan always wins on a seven-row table, so disable it to ask only whether the index can serve the query.
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        connection.exec_driver_sql(f"PREPARE debug_session_lookup AS {prepared}")
        try:
            plan = connection.exec_driver_sql(f"EXPLAIN EXECUTE debug_session_lookup({arguments})").scalars().all()
        finally:
            connection.exec_driver_sql("DEALLOCATE debug_session_lookup")
            connection.rollback()
    return "\n".join(plan)


@pytest.mark.asyncio
async def test_lookup_returns_the_newest_open_session_through_the_index_under_a_generic_plan(
    scratch_schema: _Schema,
) -> None:
    schema_engine = scratch_schema.engine
    _upgrade(schema_engine)
    organization_id = "o_debug_lookup"
    started = datetime(2026, 9, 1)
    with schema_engine.begin() as connection:
        connection.execute(
            insert(DebugSessionModel),
            [
                _session_row(organization_id, "older_open", created_at=started),
                _session_row(organization_id, "newest_open", created_at=started + timedelta(minutes=1)),
                _session_row(
                    organization_id, "completed", created_at=started + timedelta(minutes=2), status="completed"
                ),
                _session_row(organization_id, "deleted", created_at=started + timedelta(minutes=3), deleted_at=started),
                _session_row(organization_id, "other_user", created_at=started + timedelta(minutes=4), user_id="u2"),
                _session_row(
                    organization_id,
                    "other_workflow",
                    created_at=started + timedelta(minutes=5),
                    workflow_permanent_id="w2",
                ),
                _session_row("o_other", "other_org", created_at=started + timedelta(minutes=6)),
            ],
        )

    async_engine = create_async_engine(schema_engine.url, connect_args=scratch_schema.connect_args)
    statements: list[tuple[str, dict[str, Any]]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any) -> None:
        if "FROM debug_sessions" in statement:
            statements.append((statement, parameters))

    event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
    try:
        found = await AgentDB(str(async_engine.url), db_engine=async_engine).debug.get_debug_session(
            organization_id=organization_id,
            user_id="user_target",
            workflow_permanent_id="wpid_target",
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", capture)
        await async_engine.dispose()

    assert found is not None and found.debug_session_id == "ds_newest_open"
    [(statement, parameters)] = statements
    # psycopg prepares a statement after five runs on a connection, and Postgres may then plan it generically,
    # with status as an unknown parameter.
    plan = _generic_plan(schema_engine, statement, parameters)
    assert f"Index Scan Backward using {INDEX_NAME}" in plan, plan
    assert "Sort" not in plan, plan


def test_upgrade_rebuilds_an_invalid_index_left_by_a_failed_concurrent_build(scratch_schema: _Schema) -> None:
    schema_engine = scratch_schema.engine
    with schema_engine.begin() as connection:
        connection.execute(insert(DebugSessionModel), [_session_row("o_debug_lookup", "only")])
    with schema_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        with pytest.raises(DBAPIError, match="division by zero"):
            connection.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON debug_sessions ((length(user_id) / 0))"
            )
    leftover = _index_state(schema_engine)
    assert leftover is not None and leftover[0] is False

    _upgrade(schema_engine)

    state = _index_state(schema_engine)
    assert state is not None
    valid, definition = state
    assert valid is True
    assert "(organization_id, workflow_permanent_id, user_id, created_at)" in definition
