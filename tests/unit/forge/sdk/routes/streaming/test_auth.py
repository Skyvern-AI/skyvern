"""The streaming websocket rejects every failed auth the same way, but the log level has to tell
expected credential failures apart from backend faults: a bad token is a 4xx-equivalent, while an
org-lookup outage is the signal an operator is paged on."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from structlog.testing import capture_logs

from skyvern.forge.sdk.routes.streaming import auth as auth_module


class _FakeWebSocket:
    def __init__(self, subprotocols: str | None = None) -> None:
        self.accepted = False
        self.close_codes: list[int] = []
        self.headers = {"sec-websocket-protocol": subprotocols} if subprotocols is not None else {}

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
    ("header", "expected"),
    [
        ("skyvern.v1, bearer.jwt.a-b_c", (None, "Bearer jwt.a-b_c", "skyvern.v1")),
        ("skyvern.v1, apikey.key.1", ("key.1", None, "skyvern.v1")),
        ("skyvern.v1, apikey.key, bearer.jwt", ("key", "Bearer jwt", "skyvern.v1")),
        ("skyvern.v1, bearer., apikey., garbage", (None, None, "skyvern.v1")),
        ("bearer.jwt", (None, "Bearer jwt", None)),
        (None, (None, None, None)),
    ],
    ids=[
        "bearer",
        "apikey",
        "both",
        "garbage_and_empty_values",
        "no_stream_subprotocol_offered",
        "no_header",
    ],
)
def test_credentials_from_websocket(
    header: str | None,
    expected: tuple[str | None, str | None, str | None],
) -> None:
    websocket = _FakeWebSocket(header)

    credentials = auth_module.credentials_from_websocket(websocket)  # type: ignore[arg-type]

    assert tuple(credentials) == expected


def _auth_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/stream/test")
    async def _stream(websocket: WebSocket) -> None:
        organization_id = await auth_module.auth(
            websocket=websocket,
            folder_action="workflow_run.read",
            folder_resource={},
        )
        if organization_id:
            await websocket.send_text(organization_id)
            await websocket.close()

    return app


def test_stream_handshake_authenticates_and_echoes_subprotocol(monkeypatch: pytest.MonkeyPatch) -> None:
    get_current_org = AsyncMock(return_value=SimpleNamespace(organization_id="o_test"))
    monkeypatch.setattr(auth_module, "get_current_org", get_current_org)

    with (
        TestClient(_auth_app()) as client,
        client.websocket_connect("/stream/test", subprotocols=["skyvern.v1", "bearer.header-jwt"]) as websocket,
    ):
        assert websocket.accepted_subprotocol == "skyvern.v1"
        assert websocket.receive_text() == "o_test"

    get_current_org.assert_awaited_once_with(x_api_key=None, authorization="Bearer header-jwt")


@pytest.mark.parametrize(
    "url",
    ["/stream/test?apikey=query-key", "/stream/test?token=Bearer%20query-jwt"],
    ids=["apikey", "token"],
)
def test_query_string_credential_is_rejected(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Load balancer access logs record full URLs, so a credential in the query string would persist
    an org API key in them. Only the handshake subprotocol is honored."""
    get_current_org = AsyncMock(return_value=SimpleNamespace(organization_id="o_test"))
    monkeypatch.setattr(auth_module, "get_current_org", get_current_org)

    with TestClient(_auth_app()) as client, client.websocket_connect(url) as websocket:
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 1002
    get_current_org.assert_not_awaited()
