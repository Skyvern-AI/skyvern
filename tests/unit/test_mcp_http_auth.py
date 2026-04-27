from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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

_TEST_BASE_URL = "http://testserver"


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
        routes=[Route("/mcp", endpoint=_echo_request_context, methods=["GET", "HEAD", "POST"])],
        middleware=[Middleware(mcp_http_auth.MCPAPIKeyMiddleware)],
    )


def _jwtish_token(
    *,
    header: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
) -> str:
    def _encode(segment: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(segment, separators=(",", ":")).encode()).rstrip(b"=").decode()

    encoded_header = _encode(header or {"alg": "RS256", "typ": "JWT"})
    encoded_payload = _encode(payload or {"sub": "user_123"})
    return f"{encoded_header}.{encoded_payload}.signature"


async def _request(
    app: Starlette,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL) as client:
        return await client.request(method, path, **kwargs)


def _stub_auth_db(monkeypatch: pytest.MonkeyPatch, db: object) -> None:
    monkeypatch.setattr(mcp_http_auth, "get_auth_db", lambda: db)


def _expected_oauth_challenge(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(mcp_http_auth.settings, "SKYVERN_BASE_URL", "https://api.skyvern.com")
    return mcp_http_auth._oauth_challenge_header()


@pytest.mark.asyncio
async def test_mcp_http_auth_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app()
    expected_challenge = _expected_oauth_challenge(monkeypatch)

    response = await _request(app, "POST", "/mcp", json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert "x-api-key" in response.json()["error"]["message"]
    assert response.headers["www-authenticate"] == expected_challenge
    assert response.headers["access-control-expose-headers"] == "WWW-Authenticate"


@pytest.mark.asyncio
async def test_mcp_http_auth_head_request_exposes_oauth_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app()
    expected_challenge = _expected_oauth_challenge(monkeypatch)

    response = await _request(app, "HEAD", "/mcp")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == expected_challenge
    assert response.headers["access-control-expose-headers"] == "WWW-Authenticate"


@pytest.mark.asyncio
async def test_mcp_http_auth_allows_health_checks_without_api_key() -> None:
    app = _build_test_app()

    response = await _request(app, "GET", "/healthz")

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

    response = await _request(app, "POST", "/mcp", headers={"x-api-key": "bad-key"}, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["message"] == "Invalid API key"
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_500_on_non_auth_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=HTTPException(status_code=500, detail="db down")),
    )
    app = _build_test_app()

    response = await _request(app, "POST", "/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

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

    response = await _request(app, "POST", "/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

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

    response = await _request(app, "POST", "/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

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

    response = await _request(app, "POST", "/mcp", headers={"x-api-key": "sk_live_abc"}, json={})

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
    _stub_auth_db(monkeypatch, object())

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
    _stub_auth_db(monkeypatch, object())

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
    _stub_auth_db(monkeypatch, object())

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
    _stub_auth_db(monkeypatch, object())

    recovered_org = await mcp_http_auth.validate_mcp_api_key("sk_test_transient")

    cache_key = mcp_http_auth.cache_key("sk_test_transient")
    assert mcp_http_auth._api_key_validation_cache[cache_key][0].organization_id == "org_recovered"

    assert recovered_org.organization_id == "org_recovered"
    assert calls == 2


def test_profile_to_mcp_url_normalizes_base_variants() -> None:
    # Canonical form has no trailing slash so the advertised MCP resource URI
    # matches what clients send during RFC 8707 audience / RFC 9728
    # protected-resource comparison.
    assert mcp_http_auth._canonical_mcp_url("https://api.skyvern.com") == "https://api.skyvern.com/mcp"
    assert mcp_http_auth._canonical_mcp_url("https://api.skyvern.com/") == "https://api.skyvern.com/mcp"
    assert mcp_http_auth._canonical_mcp_url("https://api.skyvern.com/mcp") == "https://api.skyvern.com/mcp"
    assert mcp_http_auth._canonical_mcp_url("https://api.skyvern.com/mcp/") == "https://api.skyvern.com/mcp"


def test_resource_metadata_url_normalizes_base_variants() -> None:
    assert (
        mcp_http_auth._canonical_resource_metadata_url("https://api.skyvern.com")
        == "https://api.skyvern.com/.well-known/oauth-protected-resource/mcp"
    )
    assert (
        mcp_http_auth._canonical_resource_metadata_url("https://api.skyvern.com/mcp/")
        == "https://api.skyvern.com/.well-known/oauth-protected-resource/mcp"
    )


def test_validate_token_audience_rejects_wrong_resource() -> None:
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience(
            {"aud": ["https://some-other-resource.example.com/mcp/"]},
            "https://api.skyvern.com/mcp/",
        )


def test_validate_token_audience_accepts_matching_url() -> None:
    mcp_http_auth._validate_token_audience(
        {"aud": ["https://api.skyvern.com/mcp/"]},
        "https://api.skyvern.com/mcp/",
    )


def test_validate_token_audience_tolerates_trailing_slash_mismatch() -> None:
    # Token audience minted against the slashed form must still validate when
    # the canonical (slashless) expected_resource is used, and vice versa.
    mcp_http_auth._validate_token_audience(
        {"aud": ["https://api.skyvern.com/mcp/"]},
        "https://api.skyvern.com/mcp",
    )
    mcp_http_auth._validate_token_audience(
        {"aud": ["https://api.skyvern.com/mcp"]},
        "https://api.skyvern.com/mcp/",
    )


def test_validate_token_resource_claim_tolerates_trailing_slash_mismatch() -> None:
    # Same normalization applies to the RFC 8707 `resource` claim.
    mcp_http_auth._validate_token_resource_claims(
        {"resource": "https://api.skyvern.com/mcp/"},
        "https://api.skyvern.com/mcp",
    )
    mcp_http_auth._validate_token_resource_claims(
        {"resource": "https://api.skyvern.com/mcp"},
        "https://api.skyvern.com/mcp/",
    )


def test_validate_token_audience_rejects_missing_aud() -> None:
    # Payload without any `aud` key at all must reject — the `any(...)` check
    # on an empty audience list cannot match the expected resource.
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience({}, "https://api.skyvern.com/mcp")


def test_validate_token_audience_rejects_none_aud() -> None:
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience({"aud": None}, "https://api.skyvern.com/mcp")


def test_validate_token_audience_rejects_empty_list_aud() -> None:
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience({"aud": []}, "https://api.skyvern.com/mcp")


def test_validate_token_audience_filters_non_string_list_items() -> None:
    # Non-string items inside the `aud` array are silently dropped (per the
    # asymmetry documented in _validate_token_audience); with only garbage in
    # the list, there is nothing to match against the expected resource.
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience({"aud": [42, None, {}]}, "https://api.skyvern.com/mcp")


def test_validate_token_audience_rejects_different_path_despite_normalization() -> None:
    # Guards against a future refactor broadening rstrip normalization into a
    # prefix / startswith check. `/mcp-other/` is not a slash-variant of
    # `/mcp` and must be rejected.
    with pytest.raises(HTTPException, match="Token audience is not valid for this MCP resource"):
        mcp_http_auth._validate_token_audience(
            {"aud": ["https://api.skyvern.com/mcp-other/"]},
            "https://api.skyvern.com/mcp",
        )


def test_validate_token_resource_claim_rejects_different_path_despite_normalization() -> None:
    # Same boundary guard for the `resource` claim.
    with pytest.raises(HTTPException, match="Token resource is not valid for this MCP resource"):
        mcp_http_auth._validate_token_resource_claims(
            {"resource": "https://api.skyvern.com/mcp-other/"},
            "https://api.skyvern.com/mcp",
        )


def test_validate_token_resource_claim_rejects_non_string_claim() -> None:
    # Explicit type guard: a non-string `resource` claim is a malformed token,
    # not a slash-variant of the expected value, and gets its own error detail
    # so the cause is obvious in logs.
    with pytest.raises(HTTPException, match="Token resource claim must be a string"):
        mcp_http_auth._validate_token_resource_claims(
            {"resource": 42},
            "https://api.skyvern.com/mcp",
        )


def test_looks_like_jwt_rejects_dotted_opaque_token() -> None:
    assert mcp_http_auth._looks_like_jwt("opaque.with.dots") is False


def test_looks_like_jwt_accepts_jwt_header() -> None:
    assert mcp_http_auth._looks_like_jwt(_jwtish_token()) is True


def test_validate_oauth_token_contract_rejects_invalid_issuer() -> None:
    with pytest.raises(HTTPException, match="Token issuer is not valid for this MCP resource"):
        mcp_http_auth._validate_oauth_token_contract(
            {
                "iss": "https://wrong-issuer.example.com",
                "aud": ["https://api.skyvern.com/mcp/"],
            },
            expected_resource="https://api.skyvern.com/mcp/",
            expected_issuer="https://clerk.example.com",
        )


def test_validate_oauth_token_contract_rejects_mismatched_resource_claim() -> None:
    with pytest.raises(HTTPException, match="Token resource is not valid for this MCP resource"):
        mcp_http_auth._validate_oauth_token_contract(
            {
                "iss": "https://clerk.example.com",
                "aud": ["https://api.skyvern.com/mcp/"],
                "resource": "https://api.skyvern.com/other/",
            },
            expected_resource="https://api.skyvern.com/mcp/",
            expected_issuer="https://clerk.example.com",
        )


def test_validate_oauth_token_contract_accepts_valid_jwt_claims() -> None:
    mcp_http_auth._validate_oauth_token_contract(
        {
            "iss": "https://clerk.example.com/",
            "aud": ["https://api.skyvern.com/mcp/"],
            "resource": "https://api.skyvern.com/mcp/",
        },
        expected_resource="https://api.skyvern.com/mcp/",
        expected_issuer="https://clerk.example.com",
    )


@pytest.mark.asyncio
async def test_fetch_oauth_userinfo_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "aiohttp_request",
        AsyncMock(return_value=(200, {}, {"sub": "user_123", "email": "user@example.com"})),
    )

    payload = await mcp_http_auth._fetch_oauth_userinfo("opaque-token", "https://clerk.example.com")

    assert payload == {"sub": "user_123", "email": "user@example.com"}
    mcp_http_auth.aiohttp_request.assert_awaited_once_with(
        "GET",
        "https://clerk.example.com/oauth/userinfo",
        headers={"Authorization": "Bearer opaque-token"},
    )


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_rejects_opaque_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opaque (non-JWT) bearer tokens cannot be audience-validated; we reject with 401."""
    fetch_userinfo = AsyncMock(return_value={"sub": "user_123"})
    monkeypatch.setattr(mcp_http_auth, "_fetch_oauth_userinfo", fetch_userinfo)
    monkeypatch.setattr(
        mcp_http_auth,
        "_get_oauth_issuer_url",
        lambda: "https://clerk.example.com",
    )
    monkeypatch.setattr(mcp_http_auth.settings, "SKYVERN_BASE_URL", "https://api.skyvern.com")

    with pytest.raises(HTTPException) as exc_info:
        await mcp_http_auth.validate_mcp_oauth_token("opaque-token")

    assert exc_info.value.status_code == 401
    assert "Opaque Bearer tokens" in exc_info.value.detail
    # The reject path must never call userinfo — we decide purely on shape.
    fetch_userinfo.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_rejects_dotted_opaque_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token with dots but not a valid JWT header is still opaque and must be rejected."""
    fetch_userinfo = AsyncMock(return_value={"sub": "user_123"})
    monkeypatch.setattr(mcp_http_auth, "_fetch_oauth_userinfo", fetch_userinfo)
    monkeypatch.setattr(
        mcp_http_auth,
        "_get_oauth_issuer_url",
        lambda: "https://clerk.example.com",
    )
    monkeypatch.setattr(mcp_http_auth.settings, "SKYVERN_BASE_URL", "https://api.skyvern.com")

    with pytest.raises(HTTPException) as exc_info:
        await mcp_http_auth.validate_mcp_oauth_token("opaque.with.dots")

    assert exc_info.value.status_code == 401
    fetch_userinfo.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_does_not_negative_cache_503_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 from Clerk JWKS fetch must not be cached — the next call should retry."""
    monkeypatch.setattr(
        mcp_http_auth,
        "_get_oauth_issuer_url",
        lambda: "https://clerk.example.com",
    )
    _stub_auth_db(monkeypatch, object())
    monkeypatch.setattr(mcp_http_auth, "_looks_like_jwt", lambda _token: True)
    monkeypatch.setattr(
        mcp_http_auth,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                get_mcp_oauth_jwt_key=AsyncMock(side_effect=RuntimeError("clerk down")),
            )
        ),
    )

    jwt_token = _jwtish_token()
    with pytest.raises(HTTPException) as exc_info:
        await mcp_http_auth.validate_mcp_oauth_token(jwt_token)

    assert exc_info.value.status_code == 503
    assert mcp_http_auth._oauth_cache_key(jwt_token) not in mcp_http_auth._api_key_validation_cache


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_negative_caches_401_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid-signature 401 should be negative-cached so repeated bad tokens are cheap."""
    import jwt
    from jwt.exceptions import InvalidSignatureError

    def _fake_decode(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise InvalidSignatureError("bad signature")

    monkeypatch.setattr(jwt, "PyJWK", lambda key: key)
    monkeypatch.setattr(jwt, "decode", _fake_decode)
    monkeypatch.setattr(
        mcp_http_auth,
        "_get_oauth_issuer_url",
        lambda: "https://clerk.example.com",
    )
    _stub_auth_db(monkeypatch, object())
    monkeypatch.setattr(mcp_http_auth, "_looks_like_jwt", lambda _token: True)
    monkeypatch.setattr(
        mcp_http_auth,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                get_mcp_oauth_jwt_key=AsyncMock(return_value="jwk"),
            )
        ),
    )

    jwt_token = _jwtish_token()
    with pytest.raises(HTTPException) as exc_info:
        await mcp_http_auth.validate_mcp_oauth_token(jwt_token)

    assert exc_info.value.status_code == 401
    assert mcp_http_auth._oauth_cache_key(jwt_token) in mcp_http_auth._api_key_validation_cache


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_returns_401_when_cloud_jwk_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "_get_oauth_issuer_url",
        lambda: "https://clerk.example.com",
    )
    _stub_auth_db(monkeypatch, object())
    monkeypatch.setattr(mcp_http_auth, "_looks_like_jwt", lambda _token: True)
    monkeypatch.setattr(
        mcp_http_auth,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                get_mcp_oauth_jwt_key=AsyncMock(return_value=None),
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await mcp_http_auth.validate_mcp_oauth_token("header.payload.signature")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "OAuth authentication requires cloud deployment"


@pytest.mark.asyncio
async def test_validate_mcp_oauth_token_passes_clock_skew_leeway_to_pyjwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt

    captured: dict[str, object] = {}

    def _fake_jwk(key: object) -> object:
        captured["jwk_key"] = key
        return key

    def _fake_decode(token: str, signing_key: object, **kwargs: object) -> dict[str, object]:
        captured["token"] = token
        captured["signing_key"] = signing_key
        captured["kwargs"] = kwargs
        return {
            "iss": "https://clerk.example.com",
            "aud": ["https://api.skyvern.com/mcp/"],
            "resource": "https://api.skyvern.com/mcp/",
            "sub": "user_123",
        }

    fake_db = SimpleNamespace(
        get_organization_entities=AsyncMock(return_value=[SimpleNamespace(organization_id="org_jwt")]),
        get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="sk_live_from_jwt")),
    )
    monkeypatch.setattr(jwt, "PyJWK", _fake_jwk)
    monkeypatch.setattr(jwt, "decode", _fake_decode)
    _stub_auth_db(monkeypatch, fake_db)
    monkeypatch.setattr(mcp_http_auth, "_looks_like_jwt", lambda _token: True)
    monkeypatch.setattr(mcp_http_auth, "_get_oauth_issuer_url", lambda: "https://clerk.example.com")
    monkeypatch.setattr(mcp_http_auth.settings, "SKYVERN_BASE_URL", "https://api.skyvern.com")
    monkeypatch.setattr(
        mcp_http_auth,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                get_mcp_oauth_jwt_key=AsyncMock(return_value="jwk"),
            )
        ),
    )

    resolution = await mcp_http_auth.validate_mcp_oauth_token(_jwtish_token())

    assert resolution.api_key == "sk_live_from_jwt"
    assert captured["kwargs"]["leeway"] == mcp_http_auth._TOKEN_CLOCK_SKEW_SECONDS
    assert "options" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_mcp_http_auth_accepts_opaque_bearer_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_oauth_token",
        AsyncMock(
            return_value=mcp_http_auth._OAuthResolution(
                api_key="sk_live_opaque",
                validation=_build_validation("org_opaque"),
            )
        ),
    )
    app = _build_test_app()

    response = await _request(app, "POST", "/mcp", headers={"authorization": "Bearer opaque-token"}, json={})

    assert response.status_code == 200
    assert response.json() == {
        "api_key": "sk_live_opaque",
        "organization_id": "org_opaque",
    }


@pytest.mark.asyncio
async def test_mcp_http_auth_falls_back_to_api_key_after_invalid_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_oauth_token",
        AsyncMock(side_effect=HTTPException(status_code=401, detail="Invalid Bearer token")),
    )
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(return_value=_build_validation("org_from_api_key")),
    )
    app = _build_test_app()

    response = await _request(
        app,
        "POST",
        "/mcp",
        headers={"authorization": "Bearer sk_live_proxy_token"},
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {
        "api_key": "sk_live_proxy_token",
        "organization_id": "org_from_api_key",
    }


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_503_when_clerk_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_oauth_token",
        AsyncMock(side_effect=HTTPException(status_code=503, detail="Authentication service temporarily unavailable")),
    )
    # API-key fallback also rejects the token; we must still surface 503 because
    # the OAuth path was the authoritative validator for a JWT-shaped token.
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(side_effect=HTTPException(status_code=401, detail="Invalid API key")),
    )
    app = _build_test_app()

    response = await _request(app, "POST", "/mcp", headers={"authorization": "Bearer a.b.c"}, json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["message"] == "Authentication service temporarily unavailable"


@pytest.mark.asyncio
async def test_mcp_http_auth_falls_back_to_api_key_after_oauth_service_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw API key in the Bearer slot must still authenticate when Clerk is degraded."""
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_oauth_token",
        AsyncMock(side_effect=HTTPException(status_code=503, detail="Authentication service temporarily unavailable")),
    )
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_api_key",
        AsyncMock(return_value=_build_validation("org_recovered")),
    )
    app = _build_test_app()

    response = await _request(
        app,
        "POST",
        "/mcp",
        headers={"authorization": "Bearer sk_live_proxy_token"},
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {
        "api_key": "sk_live_proxy_token",
        "organization_id": "org_recovered",
    }


@pytest.mark.asyncio
async def test_mcp_http_auth_returns_500_when_oauth_validation_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_http_auth,
        "validate_mcp_oauth_token",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    validate_mcp_api_key = AsyncMock(return_value=_build_validation("org_should_not_run"))
    monkeypatch.setattr(mcp_http_auth, "validate_mcp_api_key", validate_mcp_api_key)
    app = _build_test_app()

    response = await _request(app, "POST", "/mcp", headers={"authorization": "Bearer a.b.c"}, json={})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    validate_mcp_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_oauth_subject_to_org_logs_missing_db_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    debug_log = Mock()
    monkeypatch.setattr(mcp_http_auth.LOG, "debug", debug_log)

    with pytest.raises(HTTPException, match="OAuth authentication requires cloud deployment"):
        await mcp_http_auth._resolve_oauth_subject_to_org({"sub": "user_123"}, object())

    debug_log.assert_called_once()


def test_get_auth_db_uses_agent_function_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    built_db = object()
    builder = Mock(return_value=built_db)

    monkeypatch.setattr(
        mcp_http_auth,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(build_mcp_auth_db=builder),
        ),
    )
    monkeypatch.setattr(mcp_http_auth, "_auth_db", None)

    db = mcp_http_auth.get_auth_db()

    assert db is built_db
    builder.assert_called_once_with(
        mcp_http_auth.settings.DATABASE_STRING,
        debug_enabled=mcp_http_auth.settings.DEBUG_MODE,
    )


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
    _stub_auth_db(monkeypatch, object())

    results = await asyncio.gather(*[mcp_http_auth.validate_mcp_api_key("test-key-concurrent") for _ in range(5)])
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
    _stub_auth_db(monkeypatch, object())

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
