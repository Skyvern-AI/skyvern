"""Anti-clickjacking headers stamped on every API response."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from skyvern.forge.api_app import SECURITY_HEADERS, SecurityHeadersMiddleware


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
        return JSONResponse(status_code=500, content={"error": "boom"}, headers=SECURITY_HEADERS)

    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_security_header_values() -> None:
    # Exact-string assertions are intentional: these values are security-critical.
    assert SECURITY_HEADERS == {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
    }


def test_security_headers_on_success_response() -> None:
    response = _build_client().get("/ok")

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_security_headers_on_error_response() -> None:
    response = _build_client().get("/missing")

    assert response.status_code == 404
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_security_headers_on_unhandled_exception() -> None:
    response = _build_client().get("/boom")

    assert response.status_code == 500
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
