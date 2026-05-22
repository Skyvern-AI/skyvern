import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic/versions/2026_05_14_1500-6cd1d6f3f734_add_cdp_connect_headers.py"
)


class _OrigLockError(Exception):
    sqlstate = "55P03"


class _OrigPgcodeLockError(Exception):
    pgcode = "55P03"


class _FakeOp:
    def __init__(
        self, lock_failures: int = 0, fail_rollback: bool = False, lock_error: Exception | None = None
    ) -> None:
        self.lock_failures = lock_failures
        self.fail_rollback = fail_rollback
        self.lock_error = lock_error or _OrigLockError()
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if statement == "ROLLBACK" and self.fail_rollback:
            raise RuntimeError("rollback failed")
        if statement.startswith("LOCK TABLE") and self.lock_failures:
            self.lock_failures -= 1
            raise DBAPIError(statement, None, self.lock_error)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("cdp_connect_headers_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retry_uses_nowait_lock_and_rolls_back_failed_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=2)
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)

    migration._execute_with_retry(
        "tasks",
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        deadline=100.0,
    )

    assert sleeps == [5, 5]
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        "COMMIT",
    ]


def test_retry_stops_when_deadline_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=1)
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 101.0)

    with pytest.raises(DBAPIError):
        migration._execute_with_retry(
            "observer_cruises",
            'ALTER TABLE "observer_cruises" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
            deadline=100.0,
        )

    assert sleeps == []
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "observer_cruises" IN ACCESS EXCLUSIVE MODE NOWAIT',
        "ROLLBACK",
    ]


def test_retry_recognizes_pgcode_lock_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=1, lock_error=_OrigPgcodeLockError())
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)

    migration._execute_with_retry(
        "tasks",
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        deadline=100.0,
    )

    assert sleeps == [5]
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        "COMMIT",
    ]


def test_retry_aborts_when_rollback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=1, fail_rollback=True)
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)

    with pytest.raises(RuntimeError, match="rollback failed"):
        migration._execute_with_retry(
            "tasks",
            'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
            deadline=100.0,
        )

    assert sleeps == []
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE NOWAIT',
        "ROLLBACK",
    ]


def test_retry_budget_stays_under_ten_minutes() -> None:
    migration = _load_migration_module()

    assert migration._MIGRATION_RETRY_SECONDS == 10 * 60
