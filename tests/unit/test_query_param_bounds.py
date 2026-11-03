"""Malformed request input must be rejected at the API boundary, not by Postgres.

A 44-digit integer satisfies a bare ``int`` annotation (Python ints are
arbitrary-precision), so an unbounded ``page``/``page_size``/``version`` param reached
the database and raised ``NumericValueOutOfRange``; a NUL byte in a string reached it
and raised ``PostgreSQL text fields cannot contain NUL (0x00) bytes``. Both surfaced as
500s in a DAST scan (SKY-19497). These assert the 422 contract at the route, plus a
control that ordinary values still pass.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from skyvern.forge.api_app import RejectNulByteQueryMiddleware, register_invalid_data_handler
from skyvern.forge.sdk.routes.agent_protocol import get_workflow, run_workflow_legacy
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service

ORG_ID = "o_test"

# ZAP's integer-overflow rule (30003) sends 44-digit values.
HUGE_INT = "9" * 44


def _make_org() -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=ORG_ID,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture(scope="module")
def _route_app() -> FastAPI:
    """Mount the real routers once; ``include_router`` is expensive."""
    from skyvern.forge.sdk.routes.routers import base_router

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.dependency_overrides[org_auth_service.get_current_org] = _make_org
    test_app.add_middleware(RejectNulByteQueryMiddleware)
    return test_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, _route_app: FastAPI) -> TestClient:
    """Stub the DB so a request that clears validation returns 200 instead of
    erroring for an unrelated reason — otherwise the control test cannot tell a
    bounds rejection apart from a missing database."""
    from skyvern.forge.sdk.routes import agent_protocol, scripts

    app_mock = MagicMock()
    app_mock.DATABASE.folders.get_folders = AsyncMock(return_value=[])
    app_mock.DATABASE.scripts.get_scripts = AsyncMock(return_value=[])

    monkeypatch.setattr(agent_protocol, "app", app_mock)
    monkeypatch.setattr(scripts, "app", app_mock)
    return TestClient(_route_app)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/folders", {"page": HUGE_INT}),
        ("/v1/scripts", {"page": HUGE_INT}),
        ("/v1/scripts", {"page_size": HUGE_INT}),
        # version is compared against an int4 column, so it overflows too.
        ("/v1/workflows/wpid_123", {"version": HUGE_INT}),
    ],
)
def test_out_of_range_integer_params_are_rejected(client: TestClient, path: str, params: dict[str, str]) -> None:
    assert client.get(path, params=params).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        # The DAST payload that 500'd: ?search='"\x00<script>
        {"search": "'\"\x00<script>"},
        # Rejection is param-agnostic by design, so a param no route declares is
        # refused too — that is what a per-param validator would not cover.
        {"undeclared": "\x00"},
    ],
)
def test_nul_byte_in_query_param_is_rejected(client: TestClient, params: dict[str, str]) -> None:
    resp = client.get("/v1/folders", params=params)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "string_contains_nul"


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/folders", {"page": "1", "page_size": "10"}),
        ("/v1/folders", {"search": "quarterly report"}),
        ("/v1/scripts", {"page": "2", "page_size": "25"}),
    ],
)
def test_ordinary_pagination_and_search_still_accepted(client: TestClient, path: str, params: dict[str, str]) -> None:
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text


def test_version_defaults_stay_plain_values_for_direct_callers() -> None:
    """These handlers are imported and awaited directly, not only served as routes, so a
    `Query(None, ...)` default would become the runtime value and be bound into SQL as a
    `Query` object. Bounds must ride on Annotated metadata instead."""
    for handler in (get_workflow, run_workflow_legacy):
        default = inspect.signature(handler).parameters["version"].default
        assert default is None, f"{handler.__name__} version default is {default!r}, not None"


def test_data_error_from_a_request_body_is_422_without_leaking_sql() -> None:
    """The run_action 500 came from a NUL byte in a JSON body field, which no query-param
    guard can reach, so DataError itself has to answer 422."""
    sql = "SELECT persistent_browser_sessions.bound_key FROM persistent_browser_sessions"

    app = FastAPI()

    @app.post("/sdk/run_action")
    def run_action() -> None:
        raise DataError(sql, {}, Exception("PostgreSQL text fields cannot contain NUL (0x00) bytes"))

    register_invalid_data_handler(app)

    resp = TestClient(app, raise_server_exceptions=False).post("/sdk/run_action", json={"browser_session_id": "\x00"})

    assert resp.status_code == 422
    # The raw statement rides along on the exception; it must not reach the client.
    assert "persistent_browser_sessions" not in resp.text
