import importlib.util
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, make_url

from skyvern.config import settings
from skyvern.forge.sdk.workflow.sequential_key import resolve_reuse_bound_key

def _resolve_migration_path() -> Path:
    # Matched by suffix, not by full filename: the open-source mirror regenerates this
    # migration under its own date and revision id, so a hardcoded name resolves in only
    # one of the two trees.
    versions = Path(__file__).resolve().parents[2] / "alembic/versions"
    matches = sorted(versions.glob("*_add_persistent_session_workflow_binding.py"))
    if not matches:
        raise AssertionError(f"no persistent_session_workflow_binding migration found in {versions}")
    return matches[-1]


_MIGRATION_PATH = _resolve_migration_path()


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("reuse_browser_session_binding_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        self.events.append("autocommit-enter")
        try:
            yield
        finally:
            self.events.append("autocommit-exit")


class _FakeScalarResult:
    def __init__(self, value: bool | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> bool | None:
        return self.value


class _FakeBind:
    def __init__(self, validity: list[bool | None]) -> None:
        self.validity = iter(validity)

    def execute(self, _statement: object) -> _FakeScalarResult:
        return _FakeScalarResult(next(self.validity))


class _FakeOp:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.bind = _FakeBind([None, True])

    def execute(self, statement: str) -> None:
        self.events.append(" ".join(statement.split()))

    def get_context(self) -> _FakeContext:
        return _FakeContext(self.events)

    def get_bind(self) -> _FakeBind:
        return self.bind


def test_binding_migration_builds_unique_index_concurrently_after_idempotent_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    assert fake_op.events[:2] == [
        "ALTER TABLE persistent_browser_sessions ADD COLUMN IF NOT EXISTS bound_workflow_permanent_id VARCHAR",
        "ALTER TABLE persistent_browser_sessions ADD COLUMN IF NOT EXISTS bound_key VARCHAR",
    ]
    assert fake_op.events[2] == "autocommit-enter"
    assert any(
        event.startswith("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_pbs_live_workflow_binding ")
        for event in fake_op.events
    )
    assert fake_op.events[-1] == "autocommit-exit"
    assert not any(event.startswith("CREATE UNIQUE INDEX uq_pbs_live_workflow_binding") for event in fake_op.events)


class _ConnectionOp:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def execute(self, statement: str) -> None:
        self.connection.exec_driver_sql(statement)

    def get_bind(self) -> Connection:
        return self.connection

    def get_context(self) -> _FakeContext:
        return _FakeContext([])


def test_binding_migration_replaces_invalid_concurrent_index_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = make_url(str(settings.DATABASE_STRING))
    if database_url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL")
    database_url = database_url.set(drivername="postgresql+psycopg")
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    schema = f"reuse_migration_{uuid.uuid4().hex}"
    migration = _load_migration_module()

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET search_path TO "{schema}"')
            connection.exec_driver_sql(
                "CREATE TABLE persistent_browser_sessions ("
                "id INTEGER PRIMARY KEY, "
                "organization_id VARCHAR NOT NULL, "
                "bound_workflow_permanent_id VARCHAR, "
                "bound_key VARCHAR, "
                "deleted_at TIMESTAMP, "
                "status VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO persistent_browser_sessions "
                "(id, organization_id, bound_workflow_permanent_id, bound_key, status) VALUES "
                "(1, 'org', 'wpid', NULL, 'running'), "
                "(2, 'org', 'wpid', NULL, 'running')"
            )
            with pytest.raises(Exception):
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX CONCURRENTLY uq_pbs_live_workflow_binding "
                    "ON persistent_browser_sessions "
                    "(organization_id, bound_workflow_permanent_id, COALESCE(bound_key, '')) "
                    "WHERE bound_workflow_permanent_id IS NOT NULL "
                    "AND deleted_at IS NULL "
                    "AND status IN ('created', 'running', 'retry')"
                )
            connection.rollback()
            invalid = connection.exec_driver_sql(
                "SELECT indisvalid FROM pg_catalog.pg_index "
                "WHERE indexrelid = to_regclass('uq_pbs_live_workflow_binding')"
            ).scalar_one()
            assert invalid is False
            connection.exec_driver_sql("DELETE FROM persistent_browser_sessions WHERE id = 2")

            monkeypatch.setattr(migration, "op", _ConnectionOp(connection))
            migration.upgrade()

            valid, unique = connection.exec_driver_sql(
                "SELECT indisvalid, indisunique FROM pg_catalog.pg_index "
                "WHERE indexrelid = to_regclass('uq_pbs_live_workflow_binding')"
            ).one()
            assert valid is True
            assert unique is True
            oversized_key, _ = resolve_reuse_bound_key(
                SimpleNamespace(
                    workflow_definition=SimpleNamespace(blocks=[]),
                    browser_profile_key="{{ payload }}",
                    sequential_key=None,
                    workflow_permanent_id="wpid",
                ),
                {"payload": "x" * 4096},
                {},
            )
            assert oversized_key.startswith("profile:sha256:")
            connection.exec_driver_sql(
                "INSERT INTO persistent_browser_sessions "
                "(id, organization_id, bound_workflow_permanent_id, bound_key, status) "
                "VALUES (3, 'org', 'wpid', %s, 'running')",
                (oversized_key,),
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT bound_key FROM persistent_browser_sessions WHERE id = 3"
                ).scalar_one()
                == oversized_key
            )
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        engine.dispose()
