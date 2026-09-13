"""Connection-level database failures answer 503; server-raised ones stay on the 500 path.

Handlers are registered through the app's own registrar so a type the real app covers
cannot be missing here, or vice versa.
"""

import sqlite3

import httpx
import psycopg
import psycopg.errors
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from skyvern.forge.api_app import register_db_unavailable_handlers
from skyvern.forge.sdk.db.exceptions import DatabaseConnectionUnavailableError


def _app(error: BaseException) -> FastAPI:
    app = FastAPI()
    register_db_unavailable_handlers(app)

    @app.get("/read")
    @app.post("/write")
    async def failing() -> None:
        raise error

    return app


async def _call(error: BaseException, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(error))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dbapi_error",
    [
        psycopg.OperationalError("connection failed: FATAL: Failed to connect to database"),
        psycopg.errors.CannotConnectNow("the database system is starting up"),
        psycopg.errors.ConnectionFailure("server closed the connection unexpectedly"),
        psycopg.errors.TooManyConnections("FATAL: too many connections for role"),
    ],
    ids=["refused", "57P03", "08006", "53300"],
)
async def test_connection_failure_on_a_read_is_503_with_retry_after(dbapi_error: BaseException) -> None:
    response = await _call(OperationalError("SELECT 1", {}, dbapi_error), "GET", "/read")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "FATAL" not in response.text


@pytest.mark.asyncio
async def test_connection_failure_on_a_write_is_503_without_a_retry_hint() -> None:
    response = await _call(
        OperationalError("INSERT 1", {}, psycopg.errors.ConnectionFailure("server closed the connection unexpectedly")),
        "POST",
        "/write",
    )

    assert response.status_code == 503
    assert "Retry-After" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dbapi_error",
    [
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
        psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"),
        sqlite3.OperationalError("no such table: tasks"),
    ],
    ids=["57014", "55P03", "sqlite"],
)
async def test_server_raised_and_non_postgres_errors_stay_on_the_500_path(dbapi_error: BaseException) -> None:
    with pytest.raises(OperationalError):
        await _call(OperationalError("SELECT 1", {}, dbapi_error), "GET", "/read")


@pytest.mark.asyncio
async def test_a_read_that_exhausted_its_reconnects_answers_like_the_driver_error_it_replaced() -> None:
    """Recovering the read inside the repository must not downgrade the endpoint's answer."""
    response = await _call(DatabaseConnectionUnavailableError("get_workflow_copilot_chat_messages", 3), "GET", "/read")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
