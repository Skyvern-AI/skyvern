from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient

from skyvern.forge.sdk.routes import internal_auth


def _request(client_host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "headers": [],
            "client": (client_host, 1234),
            "method": "GET",
            "path": "/internal/auth/status",
            "scheme": "http",
        }
    )


def test_is_local_request_returns_false_for_public_ip() -> None:
    assert internal_auth._is_local_request(_request(client_host="8.8.8.8")) is False


def test_is_local_request_accepts_loopback() -> None:
    assert internal_auth._is_local_request(_request()) is True


def test_is_local_request_rejects_private_non_loopback_address() -> None:
    assert internal_auth._is_local_request(_request(client_host="192.168.1.20")) is False


def test_is_local_request_handles_missing_client() -> None:
    request = _request()
    request.scope["client"] = None
    assert internal_auth._is_local_request(request) is False


def test_repair_route_is_not_registered() -> None:
    app = FastAPI()
    app.include_router(internal_auth.router)

    with TestClient(app) as client:
        response = client.post("/internal/auth/repair")

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_auth_status_rejects_private_non_loopback_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(internal_auth, "settings", SimpleNamespace(ENV="local"))
    evaluate_local_api_key = AsyncMock()
    monkeypatch.setattr(internal_auth, "_evaluate_local_api_key", evaluate_local_api_key)

    with pytest.raises(HTTPException) as exc_info:
        await internal_auth.auth_status(_request(client_host="192.168.1.20"))

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    evaluate_local_api_key.assert_not_awaited()


def test_diagnostics_body_discloses_only_the_status() -> None:
    result = internal_auth.DiagnosticsResult(
        status=internal_auth.AuthStatus.ok,
        detail=None,
        validation=SimpleNamespace(
            organization=SimpleNamespace(organization_id="org-secret"),
            payload=SimpleNamespace(exp=4102444800),
        ),
        token="sk-abcdefghijklmnopqrstuvwxyz",
    )

    payload = internal_auth._emit_diagnostics(result)

    assert payload == {"status": "ok"}
