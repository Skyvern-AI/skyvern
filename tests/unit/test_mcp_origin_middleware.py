"""Unit tests for the MCP Origin-validation middleware."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from skyvern.cli.mcp_tools.origin_middleware import (
    OriginValidationMiddleware,
    _sanitize_origin_for_log,
    is_allowed_origin,
)


async def _ok_handler(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@pytest.fixture()
def client() -> TestClient:
    # Starlette app with OriginValidationMiddleware wrapped around it.
    async def app_factory(scope, receive, send):  # type: ignore[no-untyped-def]
        inner = Starlette(routes=[Route("/mcp/", _ok_handler, methods=["GET", "POST"])])
        middleware = OriginValidationMiddleware(inner)
        await middleware(scope, receive, send)

    return TestClient(app_factory)


# -- is_allowed_origin ------------------------------------------------------


def test_is_allowed_origin_absent_is_allowed() -> None:
    assert is_allowed_origin(None) is True
    assert is_allowed_origin("") is True


def test_is_allowed_origin_claude_ai_allowed() -> None:
    assert is_allowed_origin("https://claude.ai") is True
    assert is_allowed_origin("https://www.claude.ai") is True


def test_is_allowed_origin_claude_com_allowed() -> None:
    # The Connectors Directory is served from claude.com; submissions must not
    # 403 out of the gate when the user installs the connector from there.
    assert is_allowed_origin("https://claude.com") is True
    assert is_allowed_origin("https://www.claude.com") is True


def test_is_allowed_origin_anthropic_rejected() -> None:
    # anthropic.com is marketing / docs, not an MCP client surface. Admitting
    # it would widen CSRF surface without adding a legitimate flow.
    assert is_allowed_origin("https://anthropic.com") is False
    assert is_allowed_origin("https://www.anthropic.com") is False
    assert is_allowed_origin("https://api.anthropic.com") is False


def test_is_allowed_origin_loopback_allowed() -> None:
    assert is_allowed_origin("http://localhost:5173") is True
    assert is_allowed_origin("http://127.0.0.1:9000") is True
    assert is_allowed_origin("http://[::1]:3000") is True


def test_is_allowed_origin_random_is_rejected() -> None:
    assert is_allowed_origin("https://evil.example") is False
    assert is_allowed_origin("https://attacker.com") is False


def test_is_allowed_origin_prefix_spoofing_rejected() -> None:
    # Subdomain claiming claude.ai must not pass (only hostname exact matches).
    assert is_allowed_origin("https://claude.ai.attacker.com") is False
    assert is_allowed_origin("https://not-claude.ai") is False


def test_is_allowed_origin_missing_host_rejected() -> None:
    # Malformed origin with no host.
    assert is_allowed_origin("https://") is False


def test_sanitize_origin_for_log_escapes_control_chars() -> None:
    assert _sanitize_origin_for_log("https://evil.example\r\nforged") == "https://evil.example\\r\\nforged"


def test_sanitize_origin_for_log_truncates_long_values() -> None:
    value = "https://evil.example/" + ("x" * 500)
    sanitized = _sanitize_origin_for_log(value)
    assert sanitized is not None
    assert sanitized.endswith("... [truncated]")
    assert len(sanitized) < len(value)


# -- middleware integration -------------------------------------------------


def test_middleware_allows_missing_origin(client: TestClient) -> None:
    response = client.get("/mcp/")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_middleware_allows_claude_ai(client: TestClient) -> None:
    response = client.post("/mcp/", headers={"origin": "https://claude.ai"})
    assert response.status_code == 200


def test_middleware_allows_loopback(client: TestClient) -> None:
    response = client.post("/mcp/", headers={"origin": "http://127.0.0.1:12345"})
    assert response.status_code == 200


def test_middleware_rejects_unknown_origin(client: TestClient) -> None:
    response = client.post("/mcp/", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "forbidden_origin"
    # Static message — rejected origin is in the structured log, not reflected
    # back in the response body.
    assert body["detail"] == "Origin not allowed"
    assert "evil.example" not in body["detail"]


def test_middleware_rejects_subdomain_spoof(client: TestClient) -> None:
    response = client.post(
        "/mcp/",
        headers={"origin": "https://claude.ai.attacker.com"},
    )
    assert response.status_code == 403


# -- websocket scope handling ----------------------------------------------


@pytest.mark.asyncio
async def test_middleware_rejects_websocket_with_unknown_origin() -> None:
    # FastMCP currently only mounts streamable-HTTP, but the middleware must
    # also gate WebSocket handshakes in case that transport is ever enabled
    # at `/mcp`. Rejection happens via `websocket.close` with code 1008
    # (policy violation) before the app sees `websocket.accept`.
    sent: list[dict] = []

    async def _should_not_be_called(scope, receive, send):  # type: ignore[no-untyped-def]
        raise AssertionError("inner app must not receive rejected websocket scope")

    middleware = OriginValidationMiddleware(_should_not_be_called)

    scope = {
        "type": "websocket",
        "path": "/mcp/",
        "headers": [(b"origin", b"https://evil.example")],
    }

    async def _receive() -> dict:
        return {"type": "websocket.connect"}

    async def _send(message: dict) -> None:
        sent.append(message)

    await middleware(scope, _receive, _send)

    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_middleware_allows_websocket_with_claude_ai_origin() -> None:
    # An allowlisted Origin on a WebSocket scope passes through to the inner
    # app so it can run the `websocket.accept` handshake itself.
    called = False

    async def _inner(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    middleware = OriginValidationMiddleware(_inner)

    scope = {
        "type": "websocket",
        "path": "/mcp/",
        "headers": [(b"origin", b"https://claude.ai")],
    }

    async def _receive() -> dict:
        return {"type": "websocket.connect"}

    async def _send(message: dict) -> None:  # pragma: no cover — inner no-op
        raise AssertionError("inner send should not fire in this fake")

    await middleware(scope, _receive, _send)

    assert called is True


@pytest.mark.asyncio
async def test_middleware_passes_lifespan_scope_through() -> None:
    # Lifespan scopes have no Origin header; the middleware must not try to
    # 403 them or it breaks app startup/shutdown.
    reached = False

    async def _inner(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal reached
        reached = True

    middleware = OriginValidationMiddleware(_inner)

    await middleware({"type": "lifespan"}, lambda: None, lambda _m: None)  # type: ignore[arg-type]

    assert reached is True
