"""The standalone-run listing, the partial tasks index and its migration must agree; plan choice needs real Postgres."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.config import settings
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

INDEX_NAME = "ix_tasks_org_standalone_status_created"
POLLER_ORG = "o_standalone_poller"
OTHER_ORG = "o_standalone_other"


def _load_migration() -> ModuleType:
    # Found by content: the open-source mirror renames the file with its own date, revision id and docstring slug.
    versions = Path(__file__).resolve().parents[2] / "alembic/versions"
    marker = f'INDEX_NAME = "{INDEX_NAME}"'
    matches = [path for path in sorted(versions.glob("*.py")) if marker in path.read_text()]
    assert len(matches) == 1, f"expected one migration defining {marker} in {versions}, found {matches}"
    spec = importlib.util.spec_from_file_location("tasks_standalone_index_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class IsolatedSchema:
    url: URL
    connect_args: dict[str, str]
    engine: Engine


@pytest.fixture
def isolated_schema() -> Iterator[IsolatedSchema]:
    url = make_url(str(settings.DATABASE_STRING))
    if url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL")
    url = url.set(drivername="postgresql+psycopg")
    schema = f"tasks_standalone_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    connect_args = {"options": f"-csearch_path={schema}"}
    engine = create_engine(url, isolation_level="AUTOCOMMIT", connect_args=connect_args)
    try:
        with engine.connect() as connection:
            # Only what the listing reads and its foreign keys: other tables need pg_trgm, which lives in `public`.
            Base.metadata.create_all(
                connection,
                tables=[
                    Base.metadata.tables[name]
                    for name in ("organizations", "folders", "workflows", "workflow_runs", "tasks")
                ],
            )
            connection.exec_driver_sql(
                "INSERT INTO organizations (organization_id, organization_name, created_at, modified_at) VALUES "
                f"('{POLLER_ORG}', 'poller', now(), now()), ('{OTHER_ORG}', 'other', now(), now())"
            )
        yield IsolatedSchema(url=url, connect_args=connect_args, engine=engine)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _index_names(plan_node: dict[str, Any]) -> Iterator[str]:
    if "Index Name" in plan_node:
        yield plan_node["Index Name"]
    for child in plan_node.get("Plans", []):
        yield from _index_names(child)


@pytest.mark.asyncio
async def test_standalone_run_listing_is_answered_from_the_partial_index(isolated_schema: IsolatedSchema) -> None:
    with isolated_schema.engine.connect() as connection:
        # The poller's org has a long standalone history but almost nothing running; another org runs a lot.
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_id, organization_id, status, url, errors, created_at, modified_at) "
            f"SELECT 'tsk_done_' || g, '{POLLER_ORG}', 'completed', 'https://example.com', '[]', "
            "now() - g * interval '1 second', now() FROM generate_series(1, 20000) g"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_id, organization_id, status, url, errors, created_at, modified_at) "
            f"SELECT 'tsk_other_' || g, '{OTHER_ORG}', 'running', 'https://example.com', '[]', "
            "now() - g * interval '1 second', now() FROM generate_series(1, 5000) g"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_id, organization_id, status, url, errors, created_at, modified_at) "
            f"SELECT 'tsk_running_' || g, '{POLLER_ORG}', 'running', 'https://example.com', '[]', "
            "now() - g * interval '1 minute', now() FROM generate_series(1, 3) g"
        )
        connection.exec_driver_sql("ANALYZE tasks")

    async_engine = create_async_engine(isolated_schema.url, connect_args=isolated_schema.connect_args)
    task_statements: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool) -> None:
        if "FROM tasks" in statement:
            task_statements.append((statement, parameters))

    event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
    try:
        database = AgentDB(
            isolated_schema.url.render_as_string(hide_password=False), debug_enabled=False, db_engine=async_engine
        )
        runs = await database.workflow_runs.get_all_runs(
            POLLER_ORG, page=1, page_size=100, status=[WorkflowRunStatus.running]
        )
    finally:
        await async_engine.dispose()

    assert [run.task_id for run in runs] == ["tsk_running_1", "tsk_running_2", "tsk_running_3"]
    assert len(task_statements) == 1
    statement, parameters = task_statements[0]
    with isolated_schema.engine.connect() as connection:
        plan = connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters).scalar_one()
    assert INDEX_NAME in set(_index_names(plan[0]["Plan"])), plan


class _RecordingOp:
    """Stands in for alembic's `op` on a real AUTOCOMMIT connection, recording each statement it executes."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.executed: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed.append(statement)
        self.connection.exec_driver_sql(statement)

    def get_bind(self) -> Connection:
        return self.connection

    def get_context(self) -> _RecordingOp:
        return self

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        yield


_INDEX_STATE_SQL = text(
    "SELECT i.indisvalid, pg_get_indexdef(c.oid) FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND i.indrelid = 'tasks'::regclass"
)


def _replace_with_invalid_leftover(connection: Connection) -> None:
    connection.exec_driver_sql(f"DROP INDEX {INDEX_NAME}")
    # A failed CONCURRENTLY build of the same name leaves an INVALID index behind, as a cancelled build would.
    connection.exec_driver_sql(
        "INSERT INTO tasks (task_id, organization_id, status, errors, created_at, modified_at) VALUES "
        f"('tsk_dup_1', '{POLLER_ORG}', 'running', '[]', '2026-10-01', now()), "
        f"('tsk_dup_2', '{POLLER_ORG}', 'running', '[]', '2026-10-01', now())"
    )
    with pytest.raises(IntegrityError):
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX_NAME} "
            "ON tasks (organization_id, status, created_at) WHERE workflow_run_id IS NULL"
        )
    connection.rollback()
    assert connection.execute(_INDEX_STATE_SQL, {"name": INDEX_NAME}).one()[0] is False


def test_upgrade_rebuilds_an_invalid_leftover_and_issues_no_ddl_over_a_valid_index(
    isolated_schema: IsolatedSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = _load_migration()
    with isolated_schema.engine.connect() as connection:
        declared_valid, declared_definition = connection.execute(_INDEX_STATE_SQL, {"name": INDEX_NAME}).one()
        assert declared_valid is True
        _replace_with_invalid_leftover(connection)

        recording_op = _RecordingOp(connection)
        monkeypatch.setattr(migration, "op", recording_op)
        migration.upgrade()
        assert connection.execute(_INDEX_STATE_SQL, {"name": INDEX_NAME}).one() == (True, declared_definition)

        # Production builds the index out of band; any DDL here would queue behind a tasks autovacuum on deploy.
        recording_op.executed.clear()
        migration.upgrade()
        assert recording_op.executed == []


def test_upgrade_neither_drops_a_build_in_progress_nor_accepts_a_different_index(
    isolated_schema: IsolatedSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = _load_migration()
    monkeypatch.setattr(migration, "LEFTOVER_DROP_LOCK_TIMEOUT", "100ms")
    with isolated_schema.engine.connect() as connection, isolated_schema.engine.connect() as builder:
        _replace_with_invalid_leftover(connection)
        recording_op = _RecordingOp(connection)
        monkeypatch.setattr(migration, "op", recording_op)

        # An out-of-band CREATE INDEX CONCURRENTLY holds this lock while its index is still INVALID. Dropping the index
        # once that build finishes would discard it and start a second full build inside the deploy.
        builder.execution_options(isolation_level="READ COMMITTED")
        with builder.begin():
            # Ends the session, and its lock, if the migration waits for the lock instead of giving up.
            builder.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            builder.exec_driver_sql("LOCK TABLE tasks IN SHARE UPDATE EXCLUSIVE MODE")
            with pytest.raises(OperationalError, match="lock timeout"):
                migration.upgrade()
        connection.rollback()
        assert connection.execute(_INDEX_STATE_SQL, {"name": INDEX_NAME}).one()[0] is False

        # A valid index with other columns or another predicate must stop the deploy, not stamp the revision over it.
        connection.exec_driver_sql(f"DROP INDEX {INDEX_NAME}")
        connection.exec_driver_sql(
            f"CREATE INDEX {INDEX_NAME} ON tasks (organization_id, created_at) WHERE workflow_run_id IS NULL"
        )
        recording_op.executed.clear()
        with pytest.raises(RuntimeError, match="definition"):
            migration.upgrade()
        assert recording_op.executed == []
