"""Anti-clickjacking headers stamped on every API response."""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from skyvern.config import settings
from skyvern.forge.api_app import SECURITY_HEADERS, SecurityHeadersMiddleware, security_headers

HSTS = "max-age=63072000"


@pytest.fixture
def hsts_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "STRICT_TRANSPORT_SECURITY", HSTS)


def _build_client() -> TestClient:
    app = FastAPI()

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    # Mirror api_app: the base-Exception (500) handler runs inside Starlette's
    # ServerErrorMiddleware, above SecurityHeadersMiddleware, so it must stamp the
    # framing headers itself or genuine 500s ship bare.
    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": "boom"}, headers=security_headers())

    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_security_header_values() -> None:
    # Exact-string assertions are intentional: these values are security-critical.
    assert SECURITY_HEADERS == {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
    }


@pytest.mark.parametrize(("path", "status_code"), [("/ok", 200), ("/missing", 404), ("/boom", 500)])
def test_security_headers_on_every_response(path: str, status_code: int, hsts_enabled: None) -> None:
    response = _build_client().get(path)

    assert response.status_code == status_code
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Strict-Transport-Security"] == HSTS


def test_hsts_is_off_unless_configured() -> None:
    response = _build_client().get("/ok")

    assert "Strict-Transport-Security" not in response.headers
