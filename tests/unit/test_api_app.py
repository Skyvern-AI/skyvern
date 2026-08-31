"""Connection-level database failures answer 503; server-raised ones stay on the 500 path."""

import sqlite3

import httpx
import psycopg
import psycopg.errors
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from skyvern.forge.api_app import db_unavailable_handler


def _app(dbapi_error: BaseException) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(OperationalError, db_unavailable_handler)

    @app.get("/read")
    @app.post("/write")
    async def failing() -> None:
        raise OperationalError("SELECT 1", {}, dbapi_error)

    return app


async def _call(dbapi_error: BaseException, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(dbapi_error))
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
    response = await _call(dbapi_error, "GET", "/read")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "FATAL" not in response.text


@pytest.mark.asyncio
async def test_connection_failure_on_a_write_is_503_without_a_retry_hint() -> None:
    response = await _call(
        psycopg.errors.ConnectionFailure("server closed the connection unexpectedly"), "POST", "/write"
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
        await _call(dbapi_error, "GET", "/read")
