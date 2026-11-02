"""The streaming websocket rejects every failed auth the same way, but the log level has to tell
expected credential failures apart from backend faults: a bad token is a 4xx-equivalent, while an
org-lookup outage is the signal an operator is paged on."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from skyvern.forge.sdk.routes.streaming import auth as auth_module


class _FakeWebSocket:
    def __init__(self, subprotocols: str | None = None) -> None:
        self.accepted = False
        self.close_codes: list[int] = []
        self.headers = {"sec-websocket-protocol": subprotocols} if subprotocols is not None else {}
        self.url = SimpleNamespace(path="/v1/stream/test")

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.close_codes.append(code)


def _raise(exc: BaseException) -> Any:
    async def _get_current_org(**kwargs: Any) -> Any:
        raise exc

    return _get_current_org


@pytest.mark.parametrize(
    ("exc", "expected_level"),
    [
        (HTTPException(status_code=403, detail="Auth token is expired"), "warning"),
        (HTTPException(status_code=401, detail="Invalid credentials"), "warning"),
        (HTTPException(status_code=404, detail="Organization not found"), "error"),
        (TimeoutError("QueuePool limit reached"), "error"),
    ],
)
@pytest.mark.asyncio
async def test_auth_failure_log_level_separates_bad_credentials_from_backend_faults(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_level: str,
) -> None:
    monkeypatch.setattr(auth_module, "get_current_org", _raise(exc))
    websocket = _FakeWebSocket("skyvern.v1, apikey.bad")

    with capture_logs() as logs:
        organization_id = await auth_module.auth(  # type: ignore[arg-type]
            apikey=None,
            token=None,
            websocket=websocket,
            folder_action="workflow_run.read",
            folder_resource={"workflow_run_id": "wr_1"},
        )

    # Rejection is unconditional; only the level moves.
    assert organization_id is None
    assert websocket.close_codes == [1002]

    levels = [entry["log_level"] for entry in logs]
    assert expected_level in levels
    # Security floor: an auth failure is never silently dropped.
    assert "debug" not in levels and "info" not in levels


@pytest.mark.parametrize(
    ("header", "query_apikey", "query_token", "expected"),
    [
        ("skyvern.v1, bearer.jwt.a-b_c", None, None, (None, "Bearer jwt.a-b_c", "skyvern.v1")),
        ("skyvern.v1, apikey.key.1", None, None, ("key.1", None, "skyvern.v1")),
        ("skyvern.v1, apikey.key, bearer.jwt", None, None, ("key", "Bearer jwt", "skyvern.v1")),
        ("skyvern.v1, bearer.header-jwt", "query-key", "Bearer query-jwt", (None, "Bearer header-jwt", "skyvern.v1")),
        ("skyvern.v1, bearer., apikey., garbage", None, None, (None, None, "skyvern.v1")),
        ("skyvern.v1", "query-key", "Bearer query-jwt", ("query-key", "Bearer query-jwt", "skyvern.v1")),
        ("bearer.jwt", None, None, (None, "Bearer jwt", None)),
        (None, "query-key", None, ("query-key", None, None)),
    ],
    ids=[
        "bearer",
        "apikey",
        "both",
        "header_beats_query",
        "garbage_and_empty_values",
        "query_fallback",
        "no_stream_subprotocol_offered",
        "no_header",
    ],
)
def test_credentials_from_websocket(
    header: str | None,
    query_apikey: str | None,
    query_token: str | None,
    expected: tuple[str | None, str | None, str | None],
) -> None:
    websocket = _FakeWebSocket(header)

    credentials = auth_module.credentials_from_websocket(websocket, query_apikey, query_token)  # type: ignore[arg-type]

    assert tuple(credentials) == expected


def test_query_credential_logs_deprecation_but_header_credential_does_not() -> None:
    with capture_logs() as logs:
        auth_module.credentials_from_websocket(_FakeWebSocket("skyvern.v1, bearer.jwt"), "key", None)  # type: ignore[arg-type]
    assert not [entry for entry in logs if "deprecated" in entry["event"]]

    with capture_logs() as logs:
        auth_module.credentials_from_websocket(_FakeWebSocket(), "key", None)  # type: ignore[arg-type]
    assert [entry["event"] for entry in logs] == ["Streaming credential supplied in query string (deprecated)"]


def _auth_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/stream/test")
    async def _stream(websocket: WebSocket, apikey: str | None = None, token: str | None = None) -> None:
        organization_id = await auth_module.auth(
            apikey=apikey,
            token=token,
            websocket=websocket,
            folder_action="workflow_run.read",
            folder_resource={},
        )
        if organization_id:
            await websocket.send_text(organization_id)
            await websocket.close()

    return app


@pytest.mark.parametrize(
    ("url", "subprotocols", "expected_subprotocol", "expected_kwargs"),
    [
        (
            "/stream/test",
            ["skyvern.v1", "bearer.header-jwt"],
            "skyvern.v1",
            {"x_api_key": None, "authorization": "Bearer header-jwt"},
        ),
        (
            "/stream/test?token=Bearer%20query-jwt",
            None,
            None,
            {"x_api_key": None, "authorization": "Bearer query-jwt"},
        ),
    ],
    ids=["subprotocol_header", "deprecated_query"],
)
def test_stream_handshake_authenticates_and_echoes_subprotocol(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    subprotocols: list[str] | None,
    expected_subprotocol: str | None,
    expected_kwargs: dict[str, str | None],
) -> None:
    get_current_org = AsyncMock(return_value=SimpleNamespace(organization_id="o_test"))
    monkeypatch.setattr(auth_module, "get_current_org", get_current_org)

    with TestClient(_auth_app()) as client, client.websocket_connect(url, subprotocols=subprotocols) as websocket:
        assert websocket.accepted_subprotocol == expected_subprotocol
        assert websocket.receive_text() == "o_test"

    get_current_org.assert_awaited_once_with(**expected_kwargs)
