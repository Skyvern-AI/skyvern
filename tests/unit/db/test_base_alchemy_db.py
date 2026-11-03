"""Behavioral coverage for bounded recovery of a disconnected idempotent read."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import psycopg
import psycopg.errors
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from skyvern.forge.sdk.db.base_alchemy_db import read_with_disconnect_recovery
from skyvern.forge.sdk.db.exceptions import DatabaseConnectionUnavailableError


def _eof_error(*, invalidated: bool = True) -> OperationalError:
    """The exact production shape: psycopg EOF with no SQLSTATE, wrapped by SQLAlchemy."""
    error = OperationalError(
        "SELECT 1", {}, psycopg.OperationalError("consuming input failed: SSL SYSCALL error: EOF detected")
    )
    error.connection_invalidated = invalidated
    return error


class FakeSession:
    def __init__(self, owner: FakeSessionFactory, label: str) -> None:
        self._owner = owner
        self.label = label
        self.closed = False
        self.acquire_error: BaseException | None = None
        self.close_hangs = False

    async def connection(self) -> object:
        self._owner.acquisitions.append(self.label)
        if self.acquire_error is not None:
            raise self.acquire_error
        return object()

    async def close(self) -> None:
        if self.close_hangs:
            await asyncio.sleep(30)
        self.closed = True


class FakeSessionFactory:
    """Mimics _SessionFactory: ambient sessions are per-task, fresh() never is."""

    def __init__(self, ambient: FakeSession | None = None) -> None:
        self._ambient = ambient
        self.acquisitions: list[str] = []
        self.opened: list[str] = []
        self.sessions: list[FakeSession] = []
        self.prepare: Callable[[FakeSession], None] | None = None

    def current(self) -> FakeSession | None:
        return self._ambient

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeSession]:
        assert self._ambient is not None, "the helper must only enter this for a caller-owned session"
        self.opened.append(self._ambient.label)
        yield self._ambient

    @asynccontextmanager
    async def _detached_session(self) -> AsyncIterator[FakeSession]:
        session = FakeSession(self, f"detached-{len(self.opened)}")
        if self.prepare is not None:
            self.prepare(session)
        self.opened.append(session.label)
        self.sessions.append(session)
        try:
            yield session
        finally:
            try:
                async with asyncio.timeout(0.05):
                    await session.close()
            except TimeoutError:
                pass


@pytest.mark.asyncio
async def test_invalidated_read_retries_on_a_new_session() -> None:
    factory = FakeSessionFactory()
    seen: list[str] = []

    async def read(session: FakeSession) -> str:
        seen.append(session.label)
        if len(seen) == 1:
            raise _eof_error()
        return "rows"

    assert await read_with_disconnect_recovery(factory, read, operation="history") == "rows"
    assert seen[0] != seen[1]
    assert all(label.startswith("detached-") for label in seen)


@pytest.mark.asyncio
async def test_non_invalidated_operational_error_is_not_retried() -> None:
    factory = FakeSessionFactory()
    attempts = 0

    async def read(session: FakeSession) -> str:
        nonlocal attempts
        attempts += 1
        raise _eof_error(invalidated=False)

    with pytest.raises(OperationalError):
        await read_with_disconnect_recovery(factory, read, operation="history")
    assert attempts == 1


@pytest.mark.asyncio
async def test_unrelated_database_error_is_not_retried() -> None:
    factory = FakeSessionFactory()
    attempts = 0

    async def read(session: FakeSession) -> str:
        nonlocal attempts
        attempts += 1
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    with pytest.raises(IntegrityError):
        await read_with_disconnect_recovery(factory, read, operation="history")
    assert attempts == 1


@pytest.mark.asyncio
async def test_persistent_invalidation_raises_the_typed_cause() -> None:
    factory = FakeSessionFactory()
    attempts = 0

    async def read(session: FakeSession) -> str:
        nonlocal attempts
        attempts += 1
        raise _eof_error()

    with pytest.raises(DatabaseConnectionUnavailableError) as excinfo:
        await read_with_disconnect_recovery(factory, read, operation="history", attempts=3, backoff_seconds=0)
    assert attempts == 3
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.__cause__, OperationalError)


@pytest.mark.asyncio
async def test_caller_owned_transaction_is_surfaced_not_silently_replaced() -> None:
    factory = FakeSessionFactory()
    factory._ambient = FakeSession(factory, "caller-transaction")
    attempts = 0

    async def read(session: FakeSession) -> str:
        nonlocal attempts
        attempts += 1
        raise _eof_error()

    with pytest.raises(DatabaseConnectionUnavailableError) as excinfo:
        await read_with_disconnect_recovery(factory, read, operation="history")
    assert attempts == 1
    assert excinfo.value.attempts == 1, "the one attempt actually made must be reported"
    assert not any(label.startswith("detached-") for label in factory.opened)


@pytest.mark.asyncio
async def test_total_budget_bounds_every_attempt_and_its_backoff() -> None:
    factory = FakeSessionFactory()

    async def read(session: FakeSession) -> str:
        await asyncio.sleep(5)
        raise _eof_error()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DatabaseConnectionUnavailableError):
        await read_with_disconnect_recovery(factory, read, operation="history", budget_seconds=0.05)
    assert loop.time() - started < 1.0


@pytest.mark.asyncio
async def test_external_cancellation_propagates_during_the_query() -> None:
    factory = FakeSessionFactory()
    entered = asyncio.Event()

    async def read(session: FakeSession) -> str:
        entered.set()
        await asyncio.sleep(30)
        return "rows"

    task = asyncio.create_task(read_with_disconnect_recovery(factory, read, operation="history"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_external_cancellation_propagates_during_backoff() -> None:
    factory = FakeSessionFactory()
    first_failed = asyncio.Event()

    async def read(session: FakeSession) -> str:
        first_failed.set()
        raise _eof_error()

    task = asyncio.create_task(read_with_disconnect_recovery(factory, read, operation="history", backoff_seconds=30))
    await first_failed.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_healthy_read_uses_the_ambient_session() -> None:
    factory = FakeSessionFactory()
    ambient = FakeSession(factory, "caller-transaction")
    factory._ambient = ambient
    seen: list[str] = []

    async def read(session: FakeSession) -> str:
        seen.append(session.label)
        return "rows"

    assert await read_with_disconnect_recovery(factory, read, operation="history") == "rows"
    assert seen == ["caller-transaction"]


@pytest.mark.asyncio
async def test_a_retry_that_cannot_obtain_a_connection_exhausts_rather_than_escaping() -> None:
    """The driver is not always psycopg — on Windows it is asyncpg — so a failed reconnect is
    recognised by never reaching the query, not by the shape of its error."""
    factory = FakeSessionFactory()
    # asyncpg raises this bare, not wrapped in a SQLAlchemy DBAPIError — verified against a real
    # asyncpg engine refused a connect.
    raw_refusal = ConnectionRefusedError("[Errno 61] Connect call failed")

    def refuse_acquisition(session: FakeSession) -> None:
        if factory.sessions:
            session.acquire_error = raw_refusal

    factory.prepare = refuse_acquisition

    async def read(session: FakeSession) -> str:
        raise _eof_error()

    with pytest.raises(DatabaseConnectionUnavailableError) as excinfo:
        await read_with_disconnect_recovery(factory, read, operation="history", backoff_seconds=0)
    assert excinfo.value.attempts == 3
    assert len(factory.sessions) == 3


@pytest.mark.asyncio
async def test_a_stalled_session_close_does_not_outlive_the_read() -> None:
    factory = FakeSessionFactory()

    def stall_close(session: FakeSession) -> None:
        session.close_hangs = True

    factory.prepare = stall_close

    async def read(session: FakeSession) -> str:
        return "rows"

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await read_with_disconnect_recovery(factory, read, operation="history") == "rows"
    assert loop.time() - started < 1.0


@pytest.mark.asyncio
async def test_a_write_that_hit_the_same_outage_reads_as_a_dependency_failure() -> None:
    """A write has no recovery of its own, so during an outage it reaches the copilot's failure
    classifier as the raw driver error rather than the typed one."""
    from skyvern.forge.sdk.copilot.recoverable_failure import _failure_kind_for_exception

    dropped = OperationalError("INSERT", {}, psycopg.errors.ConnectionFailure("server closed the connection"))
    server_raised = OperationalError("SELECT", {}, psycopg.errors.QueryCanceled("statement timeout"))

    # asyncpg carries no psycopg SQLSTATE, so only SQLAlchemy's own flag identifies it.
    asyncpg_shaped = OperationalError("INSERT", {}, Exception("connection was closed"))
    asyncpg_shaped.connection_invalidated = True

    assert _failure_kind_for_exception(dropped) == "external_dep"
    assert _failure_kind_for_exception(asyncpg_shaped) == "external_dep"
    # asyncpg surfaces a refused checkout without wrapping it at all.
    assert _failure_kind_for_exception(ConnectionRefusedError("[Errno 61] Connect call failed")) == "external_dep"
    assert _failure_kind_for_exception(server_raised) == "unknown"
    assert _failure_kind_for_exception(FileNotFoundError("missing artifact")) == "unknown"


@pytest.mark.asyncio
async def test_a_timeout_from_inside_the_read_is_not_reported_as_the_budget_expiring() -> None:
    """``asyncio.TimeoutError`` is the builtin ``TimeoutError``, so a socket timeout raised by the
    read would otherwise be translated into a typed exhaustion the route acts on."""
    factory = FakeSessionFactory()

    async def read(session: FakeSession) -> str:
        raise TimeoutError("socket timed out")

    with pytest.raises(TimeoutError) as excinfo:
        await read_with_disconnect_recovery(factory, read, operation="history")
    assert not isinstance(excinfo.value, DatabaseConnectionUnavailableError)
