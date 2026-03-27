"""Tests for the @db_operation decorator."""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.exceptions import NotFoundError


class FakeDB:
    """Fake DB class to test the decorator on methods."""

    @db_operation("get_item")
    async def get_item(self, item_id: str) -> str:
        return f"item-{item_id}"

    @db_operation("create_item")
    async def create_item(self, name: str, value: int) -> dict:
        return {"name": name, "value": value}

    @db_operation("raise_not_found")
    async def raise_not_found(self) -> None:
        raise NotFoundError("item not found")

    @db_operation("raise_sqlalchemy")
    async def raise_sqlalchemy(self) -> None:
        raise SQLAlchemyError("db connection failed")

    @db_operation("raise_generic")
    async def raise_generic(self) -> None:
        raise RuntimeError("something unexpected")


@pytest.mark.asyncio
async def test_db_operation_returns_value() -> None:
    """Decorator should pass through the return value on success."""
    db = FakeDB()
    result = await db.get_item("123")
    assert result == "item-123"


@pytest.mark.asyncio
async def test_db_operation_passes_args() -> None:
    """Decorator should forward all args and kwargs correctly."""
    db = FakeDB()
    result = await db.create_item("test", value=42)
    assert result == {"name": "test", "value": 42}


@pytest.mark.asyncio
async def test_db_operation_logs_and_reraises_not_found() -> None:
    """NotFoundError should be logged at debug level and re-raised."""
    db = FakeDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(NotFoundError, match="item not found"):
            await db.raise_not_found()
        mock_log.warning.assert_called_once_with("BusinessLogicError", operation="raise_not_found", exc_info=True)


@pytest.mark.asyncio
async def test_db_operation_logs_and_reraises_sqlalchemy_error() -> None:
    """SQLAlchemyError should be logged and re-raised."""
    db = FakeDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(SQLAlchemyError, match="db connection failed"):
            await db.raise_sqlalchemy()
        mock_log.exception.assert_called_once_with("SQLAlchemyError", operation="raise_sqlalchemy")


@pytest.mark.asyncio
async def test_db_operation_logs_and_reraises_generic_exception() -> None:
    """Generic exceptions should be logged and re-raised."""
    db = FakeDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(RuntimeError, match="something unexpected"):
            await db.raise_generic()
        mock_log.exception.assert_called_once_with("UnexpectedError", operation="raise_generic")


@pytest.mark.asyncio
async def test_db_operation_preserves_function_name() -> None:
    """Decorator should preserve __name__ via functools.wraps."""
    db = FakeDB()
    assert db.get_item.__name__ == "get_item"
    assert db.create_item.__name__ == "create_item"


@pytest.mark.asyncio
async def test_db_operation_preserves_docstring() -> None:
    """Decorator should preserve __doc__ via functools.wraps."""

    class DocDB:
        @db_operation("documented_method")
        async def documented_method(self) -> str:
            """This method has a docstring."""
            return "ok"

    db = DocDB()
    assert db.documented_method.__doc__ == "This method has a docstring."


@pytest.mark.asyncio
async def test_db_operation_on_standalone_function() -> None:
    """Decorator should also work on standalone async functions, not just methods."""

    @db_operation("standalone")
    async def standalone(x: int, y: int) -> int:
        return x + y

    result = await standalone(3, 4)
    assert result == 7


@pytest.mark.asyncio
async def test_db_operation_not_found_subclass_reraises() -> None:
    """Subclasses of NotFoundError should also be logged and re-raised."""

    class SpecificNotFound(NotFoundError):
        pass

    class SubDB:
        @db_operation("subclass_not_found")
        async def subclass_not_found(self) -> None:
            raise SpecificNotFound("specific not found")

    db = SubDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(SpecificNotFound, match="specific not found"):
            await db.subclass_not_found()
        mock_log.warning.assert_called_once_with("BusinessLogicError", operation="subclass_not_found", exc_info=True)


def test_db_operation_rejects_sync_function() -> None:
    """Decorator should raise TypeError when applied to a sync function."""
    with pytest.raises(TypeError, match="requires an async function"):

        @db_operation("sync_op")
        def sync_func() -> str:
            return "not async"


@pytest.mark.asyncio
async def test_db_operation_sqlalchemy_subclass_logged_and_reraised() -> None:
    """Subclasses of SQLAlchemyError should be caught, logged, and re-raised."""
    from sqlalchemy.exc import IntegrityError

    class IntegrityDB:
        @db_operation("integrity_error")
        async def integrity_error(self) -> None:
            raise IntegrityError("duplicate key", params=None, orig=Exception("dup"))

    db = IntegrityDB()
    with pytest.raises(IntegrityError):
        await db.integrity_error()


@pytest.mark.asyncio
async def test_db_operation_log_errors_false_suppresses_logging() -> None:
    """When log_errors=False, errors should still be re-raised but not logged."""

    class SilentDB:
        @db_operation("silent_op", log_errors=False)
        async def raise_sqlalchemy(self) -> None:
            raise SQLAlchemyError("silent error")

    db = SilentDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(SQLAlchemyError, match="silent error"):
            await db.raise_sqlalchemy()
        mock_log.warning.assert_not_called()
        mock_log.error.assert_not_called()
        mock_log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_db_operation_schedule_limit_exceeded_is_passthrough() -> None:
    """ScheduleLimitExceededError should be treated as business logic, not unexpected."""
    from skyvern.forge.sdk.db.mixins.schedules import ScheduleLimitExceededError

    class ScheduleDB:
        @db_operation("create_schedule")
        async def create_schedule(self) -> None:
            raise ScheduleLimitExceededError("org1", "wpid1", 5, 5)

    db = ScheduleDB()
    with patch("skyvern.forge.sdk.db._error_handling.LOG") as mock_log:
        with pytest.raises(ScheduleLimitExceededError):
            await db.create_schedule()
        mock_log.warning.assert_called_once_with("BusinessLogicError", operation="create_schedule", exc_info=True)
        mock_log.exception.assert_not_called()
