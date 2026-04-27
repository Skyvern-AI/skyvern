from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

from .api_key_hash import hash_api_key_for_cache
from .client import reset_api_key_override, set_api_key_override

LOG = structlog.get_logger(__name__)
API_KEY_HEADER = "x-api-key"
AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "Bearer "
HEALTH_PATHS = {"/health", "/healthz"}
# Keep in sync with skyvern-frontend/cloud/mcp-auth-constants.ts::SKYVERN_AUTH_TEMPLATE_NAME.
# Names the JWT template the frontend uses to mint bearer tokens for
# /oauth/callback and the CLI signup flow. A divergence silently breaks auth.
# Canonical declaration only — nothing in this Python module currently reads
# the template name (JWT validation here inspects `iss`/`aud`/`sub` claims,
# not the template identifier). The constant exists so future Python callers
# (tests, token-minting helpers) have a single source of truth paired with
# the TypeScript side.
SKYVERN_AUTH_TEMPLATE_NAME = "skyvern-auth-template"
_MCP_ALLOWED_TOKEN_TYPES = (OrganizationAuthTokenType.api,)
_auth_db: AgentDB | None = None
_auth_db_lock = RLock()
_api_key_cache_lock = RLock()
# Cache entries are namespace-separated by key prefix:
# `cache_key(...)` stores MCPAPIKeyValidation, `_oauth_cache_key(...)` stores
# _OAuthResolution. Keep those prefixes distinct if this cache evolves.
_api_key_validation_cache: OrderedDict[str, tuple[MCPAPIKeyValidation | _OAuthResolution | None, float]] = OrderedDict()
_OAUTH_CACHE_TTL_SECONDS = 30.0
_NEGATIVE_CACHE_TTL_SECONDS = 5.0
_VALIDATION_RETRY_EXHAUSTED_MESSAGE = "API key validation temporarily unavailable"
_MAX_VALIDATION_RETRIES = 2
_RETRY_DELAY_SECONDS = 0.25
_OAUTH_UNAVAILABLE_MSG = "OAuth authentication requires cloud deployment"
_OAUTH_SERVICE_UNAVAILABLE_MSG = "Authentication service temporarily unavailable"
_DEFAULT_REMOTE_BASE_URL = "https://api.skyvern.com"
_MCP_REALM = "mcp"
_RESOURCE_CLAIM_KEYS = ("resource",)
_TOKEN_CLOCK_SKEW_SECONDS = 60.0


@dataclass(frozen=True)
class MCPAPIKeyValidation:
    organization_id: str
    token_type: OrganizationAuthTokenType


@dataclass(frozen=True)
class _OAuthResolution:
    """Resolved OAuth token: the org's internal API key + validation metadata."""

    api_key: str
    validation: MCPAPIKeyValidation


def _canonical_mcp_url(base_url: str | None = None) -> str:
    """Return the canonical MCP resource URI with no trailing slash.

    MCP clients canonicalize the resource URI without a trailing slash for
    RFC 8707 audience / RFC 9728 protected-resource comparison. Advertising
    ``.../mcp/`` caused Claude's MCP SDK to reject discovery with
    ``Protected resource .../mcp/ does not match expected .../mcp``. The
    audience / resource-claim validators below normalize both sides before
    comparing so tokens issued against either form still validate.
    """
    candidate = (base_url or settings.SKYVERN_BASE_URL or _DEFAULT_REMOTE_BASE_URL).strip()
    if not candidate:
        candidate = _DEFAULT_REMOTE_BASE_URL
    parts = urlsplit(candidate)
    path = (parts.path or "").rstrip("/")
    if path.endswith("/mcp"):
        canonical_path = path
    else:
        canonical_path = f"{path}/mcp" if path else "/mcp"
    return urlunsplit((parts.scheme, parts.netloc, canonical_path, "", ""))


def _canonical_resource_metadata_url(base_url: str | None = None) -> str:
    mcp_url = _canonical_mcp_url(base_url)
    parts = urlsplit(mcp_url)
    suffix = parts.path.rstrip("/").lstrip("/")
    metadata_path = f"/.well-known/oauth-protected-resource/{suffix}"
    return urlunsplit((parts.scheme, parts.netloc, metadata_path, "", ""))


def _oauth_challenge_header(base_url: str | None = None) -> str:
    return f'Bearer realm="{_MCP_REALM}", resource_metadata="{_canonical_resource_metadata_url(base_url)}"'


def _apply_oauth_exposed_headers(headers: dict[str, str]) -> dict[str, str]:
    headers["Access-Control-Expose-Headers"] = "WWW-Authenticate"
    return headers


def _looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return False

    header_segment = parts[0]
    padded_header = header_segment + "=" * (-len(header_segment) % 4)
    try:
        header_bytes = base64.urlsafe_b64decode(padded_header)
        header = json.loads(header_bytes)
    except Exception:
        return False

    if not isinstance(header, dict):
        return False

    alg = header.get("alg")
    if not isinstance(alg, str) or not alg:
        return False

    token_type = header.get("typ")
    if token_type is None:
        return True
    if not isinstance(token_type, str):
        return False
    return token_type.lower() in {"jwt", "at+jwt"}


def _normalize_issuer(value: str) -> str:
    return value.rstrip("/")


def _get_oauth_issuer_url() -> str:
    issuer = _normalize_issuer(app.AGENT_FUNCTION.get_mcp_oauth_issuer_url() or "")
    if not issuer:
        raise HTTPException(status_code=401, detail=_OAUTH_UNAVAILABLE_MSG)
    return issuer


def _validate_token_issuer(payload: dict[str, object], expected_issuer: str) -> None:
    issuer = payload.get("iss")
    if not isinstance(issuer, str) or _normalize_issuer(issuer) != _normalize_issuer(expected_issuer):
        raise HTTPException(status_code=401, detail="Token issuer is not valid for this MCP resource")


def _normalize_resource(resource: str) -> str:
    """Strip a trailing slash so audience / resource comparisons are slash-agnostic.

    Centralized here so ``_validate_token_audience``,
    ``_validate_token_resource_claims``, and any future validator apply the
    same normalization rule to both sides of the comparison.
    """
    return resource.rstrip("/")


def _validate_token_audience(payload: dict[str, object], expected_resource: str) -> None:
    audience = payload.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list):
        # RFC 7519 permits `aud` as string-or-array-of-strings; silently drop
        # any non-string items in the array rather than rejecting. This is
        # intentionally more permissive than `_validate_token_resource_claims`
        # (where the single `resource` claim must be a string outright):
        # a malformed array element here shouldn't poison an otherwise valid
        # audience list.
        audiences = [item for item in audience if isinstance(item, str)]
    else:
        audiences = []

    # Slash-agnostic compare: tokens whose `aud` was minted against either
    # `.../mcp` or `.../mcp/` validate against either expected form.
    expected_norm = _normalize_resource(expected_resource)
    if not any(_normalize_resource(a) == expected_norm for a in audiences):
        raise HTTPException(status_code=401, detail="Token audience is not valid for this MCP resource")


def _validate_token_resource_claims(payload: dict[str, object], expected_resource: str) -> None:
    expected_norm = _normalize_resource(expected_resource)
    for key in _RESOURCE_CLAIM_KEYS:
        claim_value = payload.get(key)
        if claim_value is None:
            continue
        # A non-string `resource` claim is a malformed token, not a value
        # mismatch — use a distinct error so the cause is obvious in logs.
        if not isinstance(claim_value, str):
            raise HTTPException(status_code=401, detail="Token resource claim must be a string")
        if _normalize_resource(claim_value) != expected_norm:
            raise HTTPException(status_code=401, detail="Token resource is not valid for this MCP resource")


def _validate_oauth_token_contract(payload: dict[str, object], expected_resource: str, expected_issuer: str) -> None:
    _validate_token_issuer(payload, expected_issuer)
    _validate_token_audience(payload, expected_resource)
    _validate_token_resource_claims(payload, expected_resource)


async def _fetch_oauth_userinfo(bearer_token: str, issuer_url: str) -> dict[str, object]:
    status, _headers, response_body = await aiohttp_request(
        "GET",
        f"{issuer_url}/oauth/userinfo",
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    if status in {401, 403}:
        raise HTTPException(status_code=401, detail="Invalid Bearer token")
    if status != 200:
        raise HTTPException(status_code=503, detail=_OAUTH_SERVICE_UNAVAILABLE_MSG)
    if not isinstance(response_body, dict):
        raise HTTPException(status_code=503, detail=_OAUTH_SERVICE_UNAVAILABLE_MSG)
    if not isinstance(response_body.get("sub"), str):
        raise HTTPException(status_code=401, detail="Bearer token missing subject claim")
    return response_body


async def _resolve_oauth_subject_to_org(
    payload: dict[str, object],
    db: object,
) -> _OAuthResolution:
    get_entities = getattr(db, "get_organization_entities", None)
    get_auth_token = getattr(db, "get_valid_org_auth_token", None)
    if not callable(get_entities) or not callable(get_auth_token):
        LOG.debug(
            "MCP OAuth DB object is missing required auth methods",
            db_class=type(db).__name__,
            has_get_organization_entities=callable(get_entities),
            has_get_valid_org_auth_token=callable(get_auth_token),
        )
        raise HTTPException(status_code=401, detail=_OAUTH_UNAVAILABLE_MSG)

    entity_id = payload.get("sub")
    if not isinstance(entity_id, str) or not entity_id:
        raise HTTPException(status_code=401, detail="Bearer token missing subject claim")

    entity_type = "user"
    org_id_claim = payload.get("org_id")
    if isinstance(org_id_claim, str) and org_id_claim:
        entity_type = "organization"
        entity_id = org_id_claim

    org_entities = await get_entities(entity_id, entity_type)
    if not org_entities:
        LOG.info("MCP OAuth: no org mapping for entity", entity_id=entity_id, entity_type=entity_type)
        raise HTTPException(status_code=401, detail="No organization found for this user")

    org_id = org_entities[0].organization_id
    api_token = await get_auth_token(org_id, OrganizationAuthTokenType.api)
    if not api_token:
        from skyvern.forge.sdk.services import org_auth_token_service  # noqa: PLC0415

        api_token = await org_auth_token_service.create_org_api_token(org_id)
        LOG.info("MCP OAuth: auto-created API key for org", organization_id=org_id)

    validation = MCPAPIKeyValidation(organization_id=org_id, token_type=OrganizationAuthTokenType.api)
    return _OAuthResolution(api_key=api_token.token, validation=validation)


def _resolve_api_key_cache_ttl_seconds() -> float:
    raw = os.environ.get("SKYVERN_MCP_API_KEY_CACHE_TTL_SECONDS", "30")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


def _resolve_api_key_cache_max_size() -> int:
    raw = os.environ.get("SKYVERN_MCP_API_KEY_CACHE_MAX_SIZE", "1024")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1024


_API_KEY_CACHE_TTL_SECONDS = _resolve_api_key_cache_ttl_seconds()
_API_KEY_CACHE_MAX_SIZE = _resolve_api_key_cache_max_size()


def get_auth_db() -> AgentDB:
    global _auth_db
    # Guard singleton init in case HTTP transport is served with threaded workers.
    with _auth_db_lock:
        if _auth_db is None:
            _auth_db = app.AGENT_FUNCTION.build_mcp_auth_db(
                settings.DATABASE_STRING,
                debug_enabled=settings.DEBUG_MODE,
            )
            LOG.info("MCP auth DB initialized", db_class=type(_auth_db).__name__)
    return _auth_db


async def close_auth_db() -> None:
    """Dispose the auth DB engine used by HTTP middleware, if initialized."""
    global _auth_db
    with _auth_db_lock:
        db = _auth_db
        _auth_db = None
    with _api_key_cache_lock:
        _api_key_validation_cache.clear()
    if db is None:
        return

    try:
        await db.engine.dispose()
    except Exception:
        LOG.warning("Failed to dispose MCP auth DB engine", exc_info=True)


def cache_key(api_key: str) -> str:
    return hash_api_key_for_cache(api_key)


async def validate_mcp_api_key(api_key: str) -> MCPAPIKeyValidation:
    """Validate API key and return caller organization + token type."""
    key = cache_key(api_key)

    # Check cache first.
    with _api_key_cache_lock:
        cached = _api_key_validation_cache.get(key)
        if cached is not None:
            cached_validation, expires_at = cached
            if expires_at > time.monotonic():
                _api_key_validation_cache.move_to_end(key)
                if cached_validation is None:
                    raise HTTPException(status_code=401, detail="Invalid API key")
                if isinstance(cached_validation, MCPAPIKeyValidation):
                    return cached_validation
                # The shared cache also stores OAuth resolutions under a different
                # key namespace; treat any unexpected value here as a cache miss.
                _api_key_validation_cache.pop(key, None)
            _api_key_validation_cache.pop(key, None)

    # Cache miss — do the DB lookup with simple retry on transient errors.
    last_exc: Exception | None = None
    for attempt in range(_MAX_VALIDATION_RETRIES + 1):
        if attempt > 0:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
        try:
            validation = await resolve_org_from_api_key(
                api_key,
                get_auth_db(),
                token_types=_MCP_ALLOWED_TOKEN_TYPES,
            )
            caller_validation = MCPAPIKeyValidation(
                organization_id=validation.organization.organization_id,
                token_type=validation.token.token_type,
            )
            with _api_key_cache_lock:
                _api_key_validation_cache[key] = (
                    caller_validation,
                    time.monotonic() + _API_KEY_CACHE_TTL_SECONDS,
                )
                _api_key_validation_cache.move_to_end(key)
                while len(_api_key_validation_cache) > _API_KEY_CACHE_MAX_SIZE:
                    _api_key_validation_cache.popitem(last=False)
            return caller_validation
        except HTTPException as e:
            if e.status_code in {401, 403}:
                with _api_key_cache_lock:
                    _api_key_validation_cache[key] = (None, time.monotonic() + _NEGATIVE_CACHE_TTL_SECONDS)
                    _api_key_validation_cache.move_to_end(key)
                    while len(_api_key_validation_cache) > _API_KEY_CACHE_MAX_SIZE:
                        _api_key_validation_cache.popitem(last=False)
                raise
            last_exc = e
        except Exception as e:
            last_exc = e

    LOG.warning("API key validation retries exhausted", attempts=_MAX_VALIDATION_RETRIES + 1, exc_info=last_exc)
    raise HTTPException(status_code=503, detail=_VALIDATION_RETRY_EXHAUSTED_MESSAGE)


def _oauth_cache_key(bearer_token: str) -> str:
    return "oauth:" + hashlib.sha256(bearer_token.encode()).hexdigest()


async def validate_mcp_oauth_token(bearer_token: str) -> _OAuthResolution:
    """Validate a Clerk-issued Bearer token and resolve the caller's org API key.

    Flow:
    1. Check cache (keyed on hashed bearer token, 30s TTL).
    2. Decode the JWT locally using Clerk's public JWKS (RS256).
    3. Extract user identity (``sub`` claim) and map to an organization via
       the same entity-lookup used by ``cloud/cloud_app.py:authentication_function``.
    4. Fetch (or auto-create) the org's internal API key.
    5. Cache and return.

    Raises:
        HTTPException(401) for invalid/expired tokens or missing org mapping.
        HTTPException(503) if Clerk JWKS fetch fails (network error).
    """
    key = _oauth_cache_key(bearer_token)
    expected_resource = _canonical_mcp_url()
    expected_issuer = _get_oauth_issuer_url()

    # 1. Cache hit?
    with _api_key_cache_lock:
        cached = _api_key_validation_cache.get(key)
        if cached is not None:
            cached_value, expires_at = cached
            if expires_at > time.monotonic():
                _api_key_validation_cache.move_to_end(key)
                if cached_value is None:
                    raise HTTPException(status_code=401, detail="Invalid Bearer token")
                # cached_value is an _OAuthResolution for oauth cache entries
                if isinstance(cached_value, _OAuthResolution):
                    return cached_value
            _api_key_validation_cache.pop(key, None)

    db = get_auth_db()
    try:
        if _looks_like_jwt(bearer_token):
            try:
                import jwt as pyjwt  # noqa: PLC0415
                from jwt import PyJWK  # noqa: PLC0415
                from jwt.exceptions import InvalidKeyError, PyJWTError  # noqa: PLC0415
            except ImportError as exc:
                # ``pyjwt`` missing is a server-side misconfiguration, not a
                # credential problem — surface 503 so the client retries rather
                # than invalidating otherwise-good tokens.
                LOG.error("pyjwt is not installed; MCP OAuth Bearer validation is unavailable")
                raise HTTPException(status_code=503, detail=_OAUTH_UNAVAILABLE_MSG) from exc

            try:
                jwt_key = await app.AGENT_FUNCTION.get_mcp_oauth_jwt_key()
            except Exception as exc:
                LOG.warning("Failed to fetch OAuth JWKS for MCP Bearer validation", exc_info=True)
                raise HTTPException(status_code=503, detail=_OAUTH_SERVICE_UNAVAILABLE_MSG) from exc
            if not jwt_key:
                raise HTTPException(status_code=401, detail=_OAUTH_UNAVAILABLE_MSG)

            try:
                signing_key = PyJWK(jwt_key)
                payload = pyjwt.decode(
                    bearer_token,
                    signing_key,
                    algorithms=["RS256"],
                    leeway=_TOKEN_CLOCK_SKEW_SECONDS,
                )
            except (InvalidKeyError, PyJWTError):
                LOG.info("MCP OAuth Bearer token failed JWT verification")
                raise HTTPException(status_code=401, detail="Invalid Bearer token")

            _validate_oauth_token_contract(
                payload,
                expected_resource=expected_resource,
                expected_issuer=expected_issuer,
            )
        else:
            # Opaque (non-JWT) Bearer tokens cannot be audience-validated via
            # ``/oauth/userinfo`` — that endpoint returns a user profile, not
            # OAuth claims, so we have no way to confirm the token was issued
            # for THIS MCP resource rather than another client under the same
            # Clerk tenant. Reject instead of authenticating blindly. Callers
            # holding a Clerk session should mint a JWT via a template (the
            # flow used by ``McpAuthPage``) and retry.
            LOG.info("Rejecting non-JWT Bearer token for MCP OAuth: audience cannot be validated")
            raise HTTPException(status_code=401, detail="Opaque Bearer tokens are not supported")

        resolution = await _resolve_oauth_subject_to_org(payload, db)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            _cache_negative_oauth(key)
        raise

    with _api_key_cache_lock:
        _api_key_validation_cache[key] = (resolution, time.monotonic() + _OAUTH_CACHE_TTL_SECONDS)
        _api_key_validation_cache.move_to_end(key)
        while len(_api_key_validation_cache) > _API_KEY_CACHE_MAX_SIZE:
            _api_key_validation_cache.popitem(last=False)
    return resolution


def _cache_negative_oauth(key: str) -> None:
    with _api_key_cache_lock:
        _api_key_validation_cache[key] = (None, time.monotonic() + _NEGATIVE_CACHE_TTL_SECONDS)
        _api_key_validation_cache.move_to_end(key)
        while len(_api_key_validation_cache) > _API_KEY_CACHE_MAX_SIZE:
            _api_key_validation_cache.popitem(last=False)


def _unauthorized_response(message: str, *, include_oauth_challenge: bool = True) -> JSONResponse:
    headers: dict[str, str] = {}
    if include_oauth_challenge:
        headers["WWW-Authenticate"] = _oauth_challenge_header()
        _apply_oauth_exposed_headers(headers)
    return JSONResponse(
        {"error": {"code": "UNAUTHORIZED", "message": message}},
        status_code=401,
        headers=headers,
    )


def _internal_error_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        status_code=500,
    )


def _service_unavailable_response(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "SERVICE_UNAVAILABLE", "message": message}},
        status_code=503,
        headers={"Retry-After": "30"},
    )


class MCPAPIKeyMiddleware:
    """Require x-api-key or Authorization: Bearer for MCP HTTP transport."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if request.url.path in HEALTH_PATHS:
            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Try Authorization: Bearer first, then fall back to x-api-key.
        auth_header = request.headers.get(AUTHORIZATION_HEADER, "")
        if auth_header.startswith(BEARER_PREFIX):
            bearer_token = auth_header[len(BEARER_PREFIX) :]
            if not bearer_token:
                response = _unauthorized_response("Empty Bearer token")
                await response(scope, receive, send)
                return
            await self._handle_bearer(scope, receive, send, bearer_token)
            return

        api_key = request.headers.get(API_KEY_HEADER)
        if not api_key:
            response = _unauthorized_response("Missing x-api-key or Authorization: Bearer header")
            await response(scope, receive, send)
            return

        await self._handle_api_key(scope, receive, send, api_key)

    async def _handle_bearer(self, scope: Scope, receive: Receive, send: Send, bearer_token: str) -> None:
        """Validate Bearer token, resolve org API key, and forward the request.

        Tries Clerk OAuth token validation first, then falls back to treating
        the Bearer value as a raw API key (for tokens issued by the proxy
        /oauth/token endpoint). Both auth failures (401/403) and Clerk service
        outages (503) fall through to the API-key fallback so a raw API key in
        the Bearer slot still authenticates when Clerk is degraded. If the
        API-key fallback also fails, the original 503 is preferred over 401
        because the OAuth path was the authoritative validator.
        """
        oauth_service_error: HTTPException | None = None

        try:
            resolution = await validate_mcp_oauth_token(bearer_token)
            scope.setdefault("state", {})
            scope["state"]["organization_id"] = resolution.validation.organization_id
            token = set_api_key_override(resolution.api_key)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_api_key_override(token)
            return
        except HTTPException as e:
            if e.status_code == 503:
                oauth_service_error = e
            elif e.status_code not in {401, 403}:
                LOG.warning("Unexpected HTTPException during MCP OAuth Bearer validation", status_code=e.status_code)
                response = _internal_error_response()
                await response(scope, receive, send)
                return
        except Exception:
            LOG.exception("Unexpected MCP OAuth Bearer validation failure")
            response = _internal_error_response()
            await response(scope, receive, send)
            return

        # Fall back: treat Bearer value as a raw API key
        try:
            validation = await validate_mcp_api_key(bearer_token)
            scope.setdefault("state", {})
            scope["state"]["organization_id"] = validation.organization_id
        except HTTPException as e:
            if e.status_code in {401, 403}:
                if oauth_service_error is not None:
                    response = _service_unavailable_response(
                        oauth_service_error.detail or _OAUTH_SERVICE_UNAVAILABLE_MSG
                    )
                else:
                    response = _unauthorized_response(e.detail or "Invalid Bearer token")
            elif e.status_code == 503:
                response = _service_unavailable_response(e.detail or _VALIDATION_RETRY_EXHAUSTED_MESSAGE)
            else:
                LOG.warning("Unexpected HTTPException during MCP Bearer validation", status_code=e.status_code)
                response = _internal_error_response()
            await response(scope, receive, send)
            return
        except Exception:
            LOG.exception("Unexpected MCP Bearer validation failure")
            response = _internal_error_response()
            await response(scope, receive, send)
            return

        token = set_api_key_override(bearer_token)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_api_key_override(token)

    async def _handle_api_key(self, scope: Scope, receive: Receive, send: Send, api_key: str) -> None:
        """Validate x-api-key header and forward the request (original flow)."""
        try:
            validation = await validate_mcp_api_key(api_key)
            scope.setdefault("state", {})
            scope["state"]["organization_id"] = validation.organization_id
        except HTTPException as e:
            if e.status_code in {401, 403}:
                response = _unauthorized_response("Invalid API key", include_oauth_challenge=False)
            elif e.status_code == 503:
                response = _service_unavailable_response(e.detail or _VALIDATION_RETRY_EXHAUSTED_MESSAGE)
            else:
                LOG.warning("Unexpected HTTPException during MCP API key validation", status_code=e.status_code)
                response = _internal_error_response()
            await response(scope, receive, send)
            return
        except Exception:
            LOG.exception("Unexpected MCP API key validation failure")
            response = _internal_error_response()
            await response(scope, receive, send)
            return

        token = set_api_key_override(api_key)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_api_key_override(token)
