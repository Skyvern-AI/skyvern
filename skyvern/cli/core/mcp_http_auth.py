from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

import structlog
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from skyvern.config import settings
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

from .api_key_hash import hash_api_key_for_cache
from .client import reset_api_key_override, set_api_key_override

LOG = structlog.get_logger(__name__)
API_KEY_HEADER = "x-api-key"
HEALTH_PATHS = {"/health", "/healthz"}
_MCP_ALLOWED_TOKEN_TYPES = (OrganizationAuthTokenType.api,)
_auth_db: AgentDB | None = None
_auth_db_lock = RLock()
_api_key_cache_lock = RLock()
_api_key_validation_cache: OrderedDict[str, tuple[MCPAPIKeyValidation | None, float]] = OrderedDict()
_NEGATIVE_CACHE_TTL_SECONDS = 5.0
_VALIDATION_RETRY_EXHAUSTED_MESSAGE = "API key validation temporarily unavailable"
_MAX_VALIDATION_RETRIES = 2
_RETRY_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class MCPAPIKeyValidation:
    organization_id: str
    token_type: OrganizationAuthTokenType


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


def _get_auth_db() -> AgentDB:
    global _auth_db
    # Guard singleton init in case HTTP transport is served with threaded workers.
    with _auth_db_lock:
        if _auth_db is None:
            # Prefer CloudAgentDB when available (cloud deploys) because the base
            # AgentDB.validate_org_auth_token does not handle encrypted tokens,
            # causing all API key validation to fail when ENABLE_ENCRYPTION is on.
            try:
                from cloud.db.cloud_agent_db import CloudAgentDB  # noqa: PLC0415

                _auth_db = CloudAgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)
                LOG.info("MCP auth DB initialized", db_class="CloudAgentDB")
            except ImportError:
                _auth_db = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)
                LOG.info("MCP auth DB initialized", db_class="AgentDB")
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
                return cached_validation
            _api_key_validation_cache.pop(key, None)

    # Cache miss — do the DB lookup with simple retry on transient errors.
    last_exc: Exception | None = None
    for attempt in range(_MAX_VALIDATION_RETRIES + 1):
        if attempt > 0:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
        try:
            validation = await resolve_org_from_api_key(
                api_key,
                _get_auth_db(),
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


def _unauthorized_response(message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "UNAUTHORIZED", "message": message}}, status_code=401)


def _internal_error_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        status_code=500,
    )


def _service_unavailable_response(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "SERVICE_UNAVAILABLE", "message": message}},
        status_code=503,
    )


class MCPAPIKeyMiddleware:
    """Require x-api-key for MCP HTTP transport and scope requests to that key."""

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

        api_key = request.headers.get(API_KEY_HEADER)
        if not api_key:
            response = _unauthorized_response("Missing x-api-key header")
            await response(scope, receive, send)
            return

        try:
            validation = await validate_mcp_api_key(api_key)
            scope.setdefault("state", {})
            scope["state"]["organization_id"] = validation.organization_id
        except HTTPException as e:
            if e.status_code in {401, 403}:
                response = _unauthorized_response("Invalid API key")
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
