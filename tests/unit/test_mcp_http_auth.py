from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType


@pytest.fixture(autouse=True)
def _reset_auth_context() -> None:
    client_mod._api_key_override.set(None)
    mcp_http_auth._auth_db = None
    mcp_http_auth._api_key_validation_cache.clear()
    mcp_http_auth._API_KEY_CACHE_TTL_SECONDS = 30.0
    mcp_http_auth._API_KEY_CACHE_MAX_SIZE = 1024
    mcp_http_auth._MAX_VALIDATION_RETRIES = 2
    mcp_http_auth._RETRY_DELAY_SECONDS = 0.0  # no delay in tests
    mcp_http_auth.clear_all_impersonation_sessions()


async def _echo_request_context(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "api_key": client_mod.get_active_api_key(),
            "organization_id": getattr(request.state, "organization_id", None),
            "admin_organization_id": getattr(request.state, "admin_organization_id", None),
            "impersonation_target_organization_id": getattr(
                request.state, "impersonation_target_organization_id", None
            ),
        }
    )


def _build_validation(
    organization_id: str,
    token_type: OrganizationAuthTokenType = OrganizationAuthTokenType.api,
) -> mcp_http_auth.MCPAPIKeyValidation:
    return mcp_http_auth.MCPAPIKeyValidation(
        organization_id=organization_id,
        token_type=token_type,
    )


def _build_resolved_validation(
    organization_id: str,
    token_type: OrganizationAuthTokenType = OrganizationAuthTokenType.api,
) -> SimpleNamespace:
    return SimpleNamespace(
        organization=SimpleNamespace(organization_id=organization_id),
        token=SimpleNamespace(token_type=token_type),
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
        "admin_organization_id": None,
        "impersonation_target_organization_id": None,
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


@pytest.mark.asyncio
async def test_mcp_http_auth_denies_target_org_when_feature_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    validate_mock = AsyncMock(
        return_value=_build_validation(
            "org_admin",
            OrganizationAuthTokenType.mcp_admin_impersonation,
        )
    )
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        validate_mock,
    )
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: False)
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/mcp",
            headers={"x-api-key": "sk_live_admin", "x-target-org-id": "org_target"},
            json={},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Impersonation not allowed"
    validate_mock.assert_awaited_once_with("sk_live_admin")


@pytest.mark.asyncio
async def test_mcp_http_auth_validates_api_key_before_feature_flag_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    validate_mock = AsyncMock(side_effect=HTTPException(status_code=403, detail="Invalid credentials"))
    monkeypatch.setattr(mcp_http_auth, "validate_mcp_api_key", validate_mock)
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: False)
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/mcp",
            headers={"x-api-key": "bad-key", "x-target-org-id": "org_target"},
            json={},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Invalid API key"
    validate_mock.assert_awaited_once_with("bad-key")


@pytest.mark.parametrize("target_org_id", ["", "   \t  "])
@pytest.mark.asyncio
async def test_mcp_http_auth_denies_empty_or_whitespace_target_org_id_header(
    monkeypatch: pytest.MonkeyPatch,
    target_org_id: str,
) -> None:
    validate_mock = AsyncMock(
        return_value=_build_validation(
            "org_admin",
            OrganizationAuthTokenType.mcp_admin_impersonation,
        )
    )
    monkeypatch.setattr(mcp_http_auth, "validate_mcp_api_key", validate_mock)
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: True)
    app = _build_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/mcp",
            headers={"x-api-key": "sk_live_admin", "x-target-org-id": target_org_id},
            json={},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Impersonation not allowed"
    validate_mock.assert_awaited_once_with("sk_live_admin")


@pytest.mark.asyncio
async def test_mcp_http_auth_denies_when_validate_impersonation_target_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(
            return_value=_build_validation("org_admin", OrganizationAuthTokenType.mcp_admin_impersonation),
        ),
    )
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: True)
    app = _build_test_app()

    with patch(
        "cloud.mcp_admin_tools.validate_impersonation_target",
        new_callable=AsyncMock,
        return_value="caller_not_in_admin_organization_allowlist",
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/mcp",
                headers={"x-api-key": "sk_live_admin", "x-target-org-id": "org_target"},
                json={},
            )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Impersonation not allowed"


@pytest.mark.asyncio
async def test_mcp_http_auth_allows_admin_impersonation_and_applies_target_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(
            return_value=_build_validation("org_admin", OrganizationAuthTokenType.mcp_admin_impersonation),
        ),
    )
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: True)
    app = _build_test_app()

    with patch(
        "cloud.mcp_admin_tools.validate_impersonation_target",
        new_callable=AsyncMock,
        return_value=("org_target", "sk_live_target_org_key"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/mcp",
                headers={"x-api-key": "sk_live_admin", "x-target-org-id": "org_target"},
                json={},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["api_key"] == "sk_live_target_org_key"
    assert body["organization_id"] == "org_target"
    assert body["admin_organization_id"] == "org_admin"
    assert body["impersonation_target_organization_id"] == "org_target"


@pytest.mark.asyncio
async def test_mcp_http_auth_keeps_cache_safe_between_impersonated_and_non_impersonated_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _resolve(_api_key: str, _db: object, **_: object) -> object:
        nonlocal calls
        calls += 1
        return _build_resolved_validation(
            "org_admin",
            OrganizationAuthTokenType.mcp_admin_impersonation,
        )

    monkeypatch.setattr(mcp_http_auth, "resolve_org_from_api_key", _resolve)
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: True)
    app = _build_test_app()

    with patch(
        "cloud.mcp_admin_tools.validate_impersonation_target",
        new_callable=AsyncMock,
        return_value=("org_target", "sk_live_target_org_key"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            impersonated = await client.post(
                "/mcp",
                headers={"x-api-key": "sk_live_admin", "x-target-org-id": "org_target"},
                json={},
            )
            direct = await client.post(
                "/mcp",
                headers={"x-api-key": "sk_live_admin"},
                json={},
            )

    assert impersonated.status_code == 200
    assert direct.status_code == 200
    assert impersonated.json()["api_key"] == "sk_live_target_org_key"
    assert impersonated.json()["organization_id"] == "org_target"
    assert impersonated.json()["admin_organization_id"] == "org_admin"
    assert direct.json()["api_key"] == "sk_live_admin"
    assert direct.json()["organization_id"] == "org_admin"
    assert direct.json()["admin_organization_id"] is None
    assert calls == 1


# ---------------------------------------------------------------------------
# Session-based impersonation tests
# ---------------------------------------------------------------------------


def test_impersonation_session_lifecycle() -> None:
    """set → get → clear lifecycle."""
    admin_hash = mcp_http_auth.cache_key("sk_admin_key")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_target",
        target_api_key="sk_target_key",
        expires_at=time.monotonic() + 600,
        ttl_minutes=10,
    )
    mcp_http_auth.set_impersonation_session(session)

    retrieved = mcp_http_auth.get_active_impersonation(admin_hash)
    assert retrieved is not None
    assert retrieved.target_org_id == "org_target"

    cleared = mcp_http_auth.clear_impersonation_session(admin_hash)
    assert cleared is not None
    assert cleared.target_org_id == "org_target"

    assert mcp_http_auth.get_active_impersonation(admin_hash) is None


def test_impersonation_session_auto_expiry() -> None:
    """Expired sessions are lazily cleaned up on get."""
    admin_hash = mcp_http_auth.cache_key("sk_admin_key")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_target",
        target_api_key="sk_target_key",
        expires_at=time.monotonic() - 1,  # already expired
        ttl_minutes=1,
    )
    mcp_http_auth.set_impersonation_session(session)

    assert mcp_http_auth.get_active_impersonation(admin_hash) is None


@pytest.mark.asyncio
async def test_middleware_applies_session_impersonation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a session is active, middleware auto-applies impersonation without header."""
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(
            return_value=_build_validation(
                "org_admin",
                OrganizationAuthTokenType.mcp_admin_impersonation,
            )
        ),
    )

    admin_hash = mcp_http_auth.cache_key("sk_live_admin")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_target",
        target_api_key="sk_live_target_key",
        expires_at=time.monotonic() + 600,
        ttl_minutes=10,
    )
    mcp_http_auth.set_impersonation_session(session)

    app = _build_test_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_admin"}, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["api_key"] == "sk_live_target_key"
    assert body["organization_id"] == "org_target"
    assert body["admin_organization_id"] == "org_admin"
    assert body["impersonation_target_organization_id"] == "org_target"


@pytest.mark.asyncio
async def test_middleware_explicit_header_overrides_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit x-target-org-id header takes priority over session impersonation."""
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(
            return_value=_build_validation(
                "org_admin",
                OrganizationAuthTokenType.mcp_admin_impersonation,
            )
        ),
    )
    monkeypatch.setattr(mcp_http_auth, "_is_admin_impersonation_enabled", lambda: True)

    # Set up a session pointing to org_session_target
    admin_hash = mcp_http_auth.cache_key("sk_live_admin")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_session_target",
        target_api_key="sk_live_session_target_key",
        expires_at=time.monotonic() + 600,
        ttl_minutes=10,
    )
    mcp_http_auth.set_impersonation_session(session)

    app = _build_test_app()
    with patch(
        "cloud.mcp_admin_tools.validate_impersonation_target",
        new_callable=AsyncMock,
        return_value=("org_header_target", "sk_live_header_target_key"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/mcp",
                headers={"x-api-key": "sk_live_admin", "x-target-org-id": "org_header_target"},
                json={},
            )

    assert response.status_code == 200
    body = response.json()
    # Header target wins, not session target
    assert body["api_key"] == "sk_live_header_target_key"
    assert body["organization_id"] == "org_header_target"


@pytest.mark.asyncio
async def test_middleware_ignores_expired_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired session is ignored — middleware reverts to admin's own org."""
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(
            return_value=_build_validation(
                "org_admin",
                OrganizationAuthTokenType.mcp_admin_impersonation,
            )
        ),
    )

    admin_hash = mcp_http_auth.cache_key("sk_live_admin")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_target",
        target_api_key="sk_live_target_key",
        expires_at=time.monotonic() - 1,  # expired
        ttl_minutes=1,
    )
    mcp_http_auth.set_impersonation_session(session)

    app = _build_test_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"x-api-key": "sk_live_admin"}, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["api_key"] == "sk_live_admin"
    assert body["organization_id"] == "org_admin"
    assert body["admin_organization_id"] is None


@pytest.mark.asyncio
async def test_close_auth_db_clears_impersonation_sessions() -> None:
    admin_hash = mcp_http_auth.cache_key("sk_admin_key")
    session = mcp_http_auth.ImpersonationSession(
        admin_api_key_hash=admin_hash,
        admin_org_id="org_admin",
        target_org_id="org_target",
        target_api_key="sk_target_key",
        expires_at=time.monotonic() + 600,
        ttl_minutes=10,
    )
    mcp_http_auth.set_impersonation_session(session)

    dispose = AsyncMock()
    mcp_http_auth._auth_db = SimpleNamespace(engine=SimpleNamespace(dispose=dispose))

    await mcp_http_auth.close_auth_db()

    assert mcp_http_auth.get_active_impersonation(admin_hash) is None
