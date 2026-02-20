from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from contextvars import ContextVar, Token
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
TARGET_ORG_ID_HEADER = "x-target-org-id"
HEALTH_PATHS = {"/health", "/healthz"}
_MCP_ALLOWED_TOKEN_TYPES = (
    OrganizationAuthTokenType.api,
    OrganizationAuthTokenType.mcp_admin_impersonation,
)
_auth_db: AgentDB | None = None
_auth_db_lock = RLock()
_api_key_cache_lock = RLock()
_api_key_validation_cache: OrderedDict[str, tuple[MCPAPIKeyValidation | None, float]] = OrderedDict()
_NEGATIVE_CACHE_TTL_SECONDS = 5.0
_VALIDATION_RETRY_EXHAUSTED_MESSAGE = "API key validation temporarily unavailable"
_MAX_VALIDATION_RETRIES = 2
_RETRY_DELAY_SECONDS = 0.25

# ---------------------------------------------------------------------------
# Impersonation session state
# ---------------------------------------------------------------------------

_admin_api_key_hash: ContextVar[str | None] = ContextVar("admin_api_key_hash", default=None)


@dataclass(frozen=True)
class ImpersonationSession:
    admin_api_key_hash: str
    admin_org_id: str
    target_org_id: str
    # Stored in plaintext in process memory for up to TTL. Acceptable V1 trade-off
    # (in-process only, same as API key cache).
    target_api_key: str
    expires_at: float  # time.monotonic() deadline
    ttl_minutes: int


_impersonation_sessions: dict[str, ImpersonationSession] = {}
_impersonation_lock = RLock()

MAX_TTL_MINUTES = 120
DEFAULT_TTL_MINUTES = 30
_MAX_IMPERSONATION_SESSIONS = 100


def get_active_impersonation(admin_key_hash: str) -> ImpersonationSession | None:
    with _impersonation_lock:
        session = _impersonation_sessions.get(admin_key_hash)
        if session is None:
            return None
        if session.expires_at <= time.monotonic():
            _impersonation_sessions.pop(admin_key_hash, None)
            return None
        return session


def set_impersonation_session(session: ImpersonationSession) -> None:
    with _impersonation_lock:
        _impersonation_sessions[session.admin_api_key_hash] = session
        # Sweep expired sessions, then evict oldest if over capacity
        now = time.monotonic()
        expired_keys = [k for k, s in _impersonation_sessions.items() if s.expires_at <= now]
        for k in expired_keys:
            _impersonation_sessions.pop(k, None)
        if len(_impersonation_sessions) > _MAX_IMPERSONATION_SESSIONS:
            oldest_key = min(_impersonation_sessions, key=lambda k: _impersonation_sessions[k].expires_at)
            _impersonation_sessions.pop(oldest_key, None)


def clear_impersonation_session(admin_key_hash: str) -> ImpersonationSession | None:
    with _impersonation_lock:
        return _impersonation_sessions.pop(admin_key_hash, None)


def clear_all_impersonation_sessions() -> None:
    with _impersonation_lock:
        _impersonation_sessions.clear()


def get_admin_api_key_hash() -> str | None:
    return _admin_api_key_hash.get()


# ---------------------------------------------------------------------------


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
            _auth_db = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)
    return _auth_db


async def close_auth_db() -> None:
    """Dispose the auth DB engine used by HTTP middleware, if initialized."""
    global _auth_db
    with _auth_db_lock:
        db = _auth_db
        _auth_db = None
    with _api_key_cache_lock:
        _api_key_validation_cache.clear()
    clear_all_impersonation_sessions()
    if db is None:
        return

    try:
        await db.engine.dispose()
    except Exception:
        LOG.warning("Failed to dispose MCP auth DB engine", exc_info=True)


def cache_key(api_key: str) -> str:
    return hash_api_key_for_cache(api_key)


def _admin_organization_ids() -> set[str]:
    try:
        from cloud.config import settings as cloud_settings  # noqa: PLC0415

        admin_ids = getattr(cloud_settings, "ADMIN_ORGANIZATION_IDS", [])
    except ImportError:
        admin_ids = getattr(settings, "ADMIN_ORGANIZATION_IDS", [])
    return {org_id for org_id in admin_ids if org_id}


def _is_admin_impersonation_enabled() -> bool:
    try:
        from cloud.config import settings as cloud_settings  # noqa: PLC0415

        return bool(getattr(cloud_settings, "MCP_ADMIN_IMPERSONATION_ENABLED", False))
    except ImportError:
        return bool(getattr(settings, "MCP_ADMIN_IMPERSONATION_ENABLED", False))


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


def _deny_impersonation(
    *,
    reason: str,
    caller_organization_id: str | None,
    target_organization_id: str | None,
    token_type: OrganizationAuthTokenType | None = None,
) -> JSONResponse:
    log_kwargs: dict[str, object] = {
        "reason": reason,
        "caller_organization_id": caller_organization_id,
        "target_organization_id": target_organization_id,
    }
    if token_type is not None:
        log_kwargs["token_type"] = token_type.value
    LOG.warning("MCP admin impersonation denied", **log_kwargs)
    return _unauthorized_response("Impersonation not allowed")


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

        target_org_id_header = request.headers.get(TARGET_ORG_ID_HEADER)
        admin_hash_token: Token[str | None] | None = None

        try:
            validation = await validate_mcp_api_key(api_key)
            caller_organization_id = validation.organization_id
            admin_key_hash = cache_key(api_key)

            scope.setdefault("state", {})
            if target_org_id_header is not None:
                # Explicit header-based impersonation (takes priority over session)
                if not _is_admin_impersonation_enabled():
                    response = _deny_impersonation(
                        reason="feature_disabled",
                        caller_organization_id=caller_organization_id,
                        target_organization_id=target_org_id_header.strip() or target_org_id_header,
                    )
                    await response(scope, receive, send)
                    return

                target_organization_id = target_org_id_header.strip()
                if not target_organization_id:
                    response = _deny_impersonation(
                        reason="missing_target_organization_id",
                        caller_organization_id=caller_organization_id,
                        target_organization_id=target_org_id_header,
                        token_type=validation.token_type,
                    )
                    await response(scope, receive, send)
                    return

                # Delegate validation to the single source of truth in cloud/.
                # The import can't fail here — _is_admin_impersonation_enabled()
                # already returned True, which requires cloud.config to be importable.
                try:
                    from cloud.mcp_admin_tools import validate_impersonation_target  # noqa: PLC0415
                except ImportError:
                    response = _deny_impersonation(
                        reason="impersonation_not_available",
                        caller_organization_id=caller_organization_id,
                        target_organization_id=target_organization_id,
                    )
                    await response(scope, receive, send)
                    return

                result = await validate_impersonation_target(
                    caller_organization_id=caller_organization_id,
                    target_organization_id=target_organization_id,
                    token_type=validation.token_type,
                )
                if isinstance(result, str):
                    response = _deny_impersonation(
                        reason=result,
                        caller_organization_id=caller_organization_id,
                        target_organization_id=target_organization_id,
                        token_type=validation.token_type,
                    )
                    await response(scope, receive, send)
                    return

                resolved_org_id, target_api_key = result
                api_key = target_api_key

                scope["state"]["organization_id"] = resolved_org_id
                scope["state"]["admin_organization_id"] = caller_organization_id
                scope["state"]["impersonation_target_organization_id"] = resolved_org_id
                admin_hash_token = _admin_api_key_hash.set(admin_key_hash)
                LOG.info(
                    "MCP admin impersonation allowed",
                    caller_organization_id=caller_organization_id,
                    target_organization_id=resolved_org_id,
                    token_type=validation.token_type.value,
                )
            else:
                # No explicit header — check for session-based impersonation
                session = get_active_impersonation(admin_key_hash)
                if session is not None:
                    # Session data (target API key) is cached for TTL. If the target org's
                    # key is revoked mid-session, impersonation continues until expiry —
                    # acceptable trade-off vs re-validating every request.
                    api_key = session.target_api_key
                    scope["state"]["organization_id"] = session.target_org_id
                    scope["state"]["admin_organization_id"] = session.admin_org_id
                    scope["state"]["impersonation_target_organization_id"] = session.target_org_id
                    admin_hash_token = _admin_api_key_hash.set(admin_key_hash)
                    LOG.info(
                        "MCP session impersonation applied",
                        caller_organization_id=session.admin_org_id,
                        target_organization_id=session.target_org_id,
                        ttl_minutes=session.ttl_minutes,
                    )
                else:
                    scope["state"]["organization_id"] = caller_organization_id
        except HTTPException as e:
            if e.status_code in {401, 403}:
                response = _unauthorized_response("Invalid API key")
                await response(scope, receive, send)
                return
            if e.status_code == 503:
                response = _service_unavailable_response(e.detail or _VALIDATION_RETRY_EXHAUSTED_MESSAGE)
                await response(scope, receive, send)
                return
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
            if admin_hash_token is not None:
                _admin_api_key_hash.reset(admin_hash_token)
