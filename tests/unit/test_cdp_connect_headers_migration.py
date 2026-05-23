import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/2026_05_22_2059-0b0bd1875c6e_add_cdp_connect_headers.py"
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


def test_retry_uses_bounded_lock_wait_and_rolls_back_failed_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=2)
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(migration.random, "random", lambda: 0.5)

    migration._execute_with_retry(
        "tasks",
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        deadline=100.0,
    )

    assert sleeps == [0.625, 0.625]
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
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
    monkeypatch.setattr(migration.random, "random", lambda: 0.5)

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
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "observer_cruises" IN ACCESS EXCLUSIVE MODE',
        "ROLLBACK",
    ]


def test_retry_recognizes_pgcode_lock_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=1, lock_error=_OrigPgcodeLockError())
    sleeps: list[int] = []

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(migration.random, "random", lambda: 1.0)

    migration._execute_with_retry(
        "tasks",
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        deadline=100.0,
    )

    assert sleeps == [1.0]
    assert fake_op.statements == [
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
        "ROLLBACK",
        "BEGIN",
        "SET LOCAL statement_timeout = '5s'",
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
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
    monkeypatch.setattr(migration.random, "random", lambda: 0.5)

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
        "SET LOCAL lock_timeout = '250ms'",
        'LOCK TABLE "tasks" IN ACCESS EXCLUSIVE MODE',
        "ROLLBACK",
    ]


def test_retry_budget_is_twenty_minutes() -> None:
    migration = _load_migration_module()

    assert migration._MIGRATION_RETRY_SECONDS == 20 * 60


def test_lock_wait_budget_is_bounded_to_250ms() -> None:
    migration = _load_migration_module()

    assert migration._LOCK_TIMEOUT == "250ms"


def test_retry_backoff_samples_sub_second_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration_module()
    fake_op = _FakeOp(lock_failures=2)
    sleeps: list[float] = []
    draws = iter([0.0, 1.0])

    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)
    monkeypatch.setattr(migration.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(migration.random, "random", lambda: next(draws))

    migration._execute_with_retry(
        "tasks",
        'ALTER TABLE "tasks" ADD COLUMN IF NOT EXISTS cdp_connect_headers JSON',
        deadline=100.0,
    )

    assert sleeps == [0.25, 1.0]
