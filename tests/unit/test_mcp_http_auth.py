from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from skyvern.cli.core import client as client_mod
from skyvern.cli.core import mcp_http_auth


@pytest.fixture(autouse=True)
def _reset_auth_context() -> None:
    client_mod._api_key_override.set(None)
    mcp_http_auth._auth_db = None
    mcp_http_auth._api_key_validation_cache.clear()
    mcp_http_auth._API_KEY_CACHE_TTL_SECONDS = 30.0
    mcp_http_auth._API_KEY_CACHE_MAX_SIZE = 1024
    mcp_http_auth._MAX_VALIDATION_RETRIES = 2
    mcp_http_auth._RETRY_DELAY_SECONDS = 0.0  # no delay in tests


async def _echo_request_context(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "api_key": client_mod.get_active_api_key(),
            "organization_id": getattr(request.state, "organization_id", None),
        }
    )


def _build_validation(
    organization_id: str,
) -> mcp_http_auth.MCPAPIKeyValidation:
    return mcp_http_auth.MCPAPIKeyValidation(
        organization_id=organization_id,
        token_type=mcp_http_auth.OrganizationAuthTokenType.api,
    )


def _build_resolved_validation(
    organization_id: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        organization=SimpleNamespace(organization_id=organization_id),
        token=SimpleNamespace(token_type=mcp_http_auth.OrganizationAuthTokenType.api),
    )


def _build_test_app() -> Starlette:
    return Starlette(
        routes=[Route("/mcp", endpoint=_echo_request_context, methods=["POST"])],
        middleware=[Middleware(mcp_http_auth.MCPAPIKeyMiddleware)],
    )


@pytest.mark.asyncio
async def test_mcp_http_auth_rejects_missing_api_key() -> None:
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert "x-api-key" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_http_auth_allows_health_checks_without_api_key() -> None:
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_mcp_http_auth_rejects_invalid_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=HTTPException(status_code=403, detail="Invalid credentials")),
    )
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "bad-key"}, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Invalid API key"


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_500_on_non_auth_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=HTTPException(status_code=500, detail="db down")),
    )
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_503_on_transient_validation_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=HTTPException(status_code=503, detail="API key validation temporarily unavailable")),
    )
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["message"] == "API key validation temporarily unavailable"


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_500_on_unexpected_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_mcp_http_auth_sets_request_scoped_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(return_value=_build_validation("org_123")),
    )
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

    assert response.status_code == 200
    assert response.json() == {
        "api_key": "sk_live_abc",
        "organization_id": "org_123",
    }
    assert client_mod.get_active_api_key() != "sk_live_abc"


@pytest.mark.asyncio
async def test_validate_mcp_api_key_uses_ttl_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        return _build_resolved_validation("org_cached")

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    first = await mcp_http_auth.validate_mcp_api_key("sk_test_cache")
    second = await mcp_http_auth.validate_mcp_api_key("sk_test_cache")

    assert first.organization_id == "org_cached"
    assert second.organization_id == "org_cached"
    assert calls == 1


@pytest.mark.asyncio
async def test_validate_mcp_api_key_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        return _build_resolved_validation(f"org_{calls}")

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    first = await mcp_http_auth.validate_mcp_api_key("sk_test_cache_expire")
    cache_key = mcp_http_auth.cache_key("sk_test_cache_expire")
    mcp_http_auth._api_key_validation_cache[cache_key] = (first, 0.0)
    second = await mcp_http_auth.validate_mcp_api_key("sk_test_cache_expire")

    assert first.organization_id == "org_1"
    assert second.organization_id == "org_2"
    assert calls == 2


@pytest.mark.asyncio
async def test_validate_mcp_api_key_negative_caches_auth_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        raise HTTPException(status_code=401, detail="Invalid credentials")

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    with pytest.raises(HTTPException, match="Invalid credentials"):
        await mcp_http_auth.validate_mcp_api_key("sk_test_auth_failure")

    with pytest.raises(HTTPException, match="Invalid API key"):
        await mcp_http_auth.validate_mcp_api_key("sk_test_auth_failure")

    assert calls == 1


@pytest.mark.asyncio
async def test_validate_mcp_api_key_retries_transient_failure_without_negative_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        return _build_resolved_validation("org_recovered")

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    recovered_org = await mcp_http_auth.validate_mcp_api_key("sk_test_transient")

    cache_key = mcp_http_auth.cache_key("sk_test_transient")
    assert mcp_http_auth._api_key_validation_cache[cache_key][0].organization_id == "org_recovered"

    assert recovered_org.organization_id == "org_recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_validate_mcp_api_key_concurrent_callers_all_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple concurrent callers for the same key all succeed; the cache
    collapses subsequent calls after the first one populates it."""
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        return _build_resolved_validation("org_concurrent")

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    results = await asyncio.gather(*[mcp_http_auth.validate_mcp_api_key("sk_test_concurrent") for _ in range(5)])
    assert all(r.organization_id == "org_concurrent" for r in results)
    # First call populates cache; remaining may or may not hit DB depending on
    # scheduling, but all must succeed.
    assert calls >= 1


@pytest.mark.asyncio
async def test_validate_mcp_api_key_returns_503_after_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("persistent db outage")

    monkeypatch.setattr(mcp_http_auth, "_MAX_VALIDATION_RETRIES", 2)
    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_get_auth_db", lambda: object())

    with pytest.raises(HTTPException, match="temporarily unavailable") as exc_info:
        await mcp_http_auth.validate_mcp_api_key("sk_test_transient_exhausted")

    assert exc_info.value.status_code == 503
    assert calls == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_close_auth_db_disposes_engine() -> None:
    dispose = AsyncMock()
    mcp_http_auth._auth_db = SimpleNamespace(engine=SimpleNamespace(dispose=dispose))
    mcp_http_auth._api_key_validation_cache["k"] = ("org", 123.0)

    await mcp_http_auth.close_auth_db()

    dispose.assert_awaited_once()
    assert mcp_http_auth._auth_db is None
    assert mcp_http_auth._api_key_validation_cache == {}


@pytest.mark.asyncio
async def test_close_auth_db_noop_when_uninitialized() -> None:
    mcp_http_auth._auth_db = None
    await mcp_http_auth.close_auth_db()
    assert mcp_http_auth._auth_db is None
