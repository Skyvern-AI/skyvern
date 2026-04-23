"""Origin-header validation for the remote MCP HTTP transport.

Claude's Connectors Directory submission requires that remote MCP servers
validate the `Origin` request header to protect against CSRF-style attacks
where a malicious page in the user's browser tries to invoke MCP tools on
behalf of an authenticated user.

Policy:
- Absent `Origin` header → ALLOW. Non-browser clients (MCP Inspector, curl,
  SDKs, Claude.ai backend using python-httpx) do not send Origin. Blocking
  them would break valid flows.
- Loopback origins (localhost, 127.0.0.1, ::1) → ALLOW. For local dev and
  Claude Code ephemeral callback flows.
- `claude.ai` and `claude.com` (with `www.` variants) → ALLOW. These are the
  only Anthropic-operated front-ends that issue MCP tool calls from a
  browser context. `anthropic.com` is deliberately NOT on the list: it is a
  marketing / docs site, not an MCP client surface, so admitting it would
  only widen CSRF surface without adding a legitimate flow.
- Anything else → 403.

The allowlist is intentionally restrictive. Extend it as new first-party
client surfaces emerge; do not expand it to third-party integrators.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOG = structlog.get_logger(__name__)
_MAX_LOGGED_ORIGIN_CHARS = 200


_ALLOWED_MCP_ORIGIN_HOSTS = frozenset(
    {
        "claude.ai",
        "www.claude.ai",
        "claude.com",
        "www.claude.com",
    }
)

# `urlsplit("http://[::1]:3000").hostname` returns bare `::1` (no brackets).
_ALLOWED_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_allowed_origin(origin: str | None) -> bool:
    """Return True if `origin` is permitted to invoke the MCP endpoint."""
    if origin is None or origin == "":
        return True
    try:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
    except Exception:
        return False

    if not host:
        return False
    if host in _ALLOWED_LOOPBACK_HOSTS:
        return True
    return host in _ALLOWED_MCP_ORIGIN_HOSTS


def _sanitize_origin_for_log(origin: str | None) -> str | None:
    """Bound attacker-controlled Origin values before writing them to logs."""
    if origin is None:
        return None
    # Bound before escaping so a huge attacker-controlled header does not
    # allocate an arbitrarily large intermediate string during `.replace()`.
    sanitized = origin[: _MAX_LOGGED_ORIGIN_CHARS * 2].replace("\r", "\\r").replace("\n", "\\n")
    if len(sanitized) <= _MAX_LOGGED_ORIGIN_CHARS:
        return sanitized
    return f"{sanitized[:_MAX_LOGGED_ORIGIN_CHARS]}... [truncated]"


class OriginValidationMiddleware:
    """Reject MCP requests whose `Origin` header is not on the allowlist.

    Placed outermost in the MCP middleware stack so unknown origins are rejected
    before any API key or OAuth validation work is performed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # FastMCP currently mounts streamable-HTTP only, but WebSocket scopes
        # carry an `Origin` header in the ASGI handshake and must be gated
        # with the same policy if that transport is ever adopted at `/mcp`.
        # `lifespan` and any other scope type have no Origin and pass through.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        origin: str | None = None
        for key, value in scope.get("headers", ()):
            if key.lower() == b"origin":
                origin = value.decode("latin-1")
                break

        if not is_allowed_origin(origin):
            # The offending origin is captured in the structured log; do not
            # echo it back in the response body to avoid reflecting
            # attacker-controlled input.
            LOG.warning(
                "mcp_origin_rejected",
                origin=_sanitize_origin_for_log(origin),
                scope_type=scope["type"],
                path=scope.get("path", ""),
            )
            if scope["type"] == "http":
                response = JSONResponse(
                    {"error": "forbidden_origin", "detail": "Origin not allowed"},
                    status_code=403,
                )
                await response(scope, receive, send)
            else:
                # Refuse the handshake before `websocket.accept`. ASGI lets us
                # send a bare `websocket.close` in response to `websocket.connect`
                # — RFC 6455 close code 1008 = "policy violation".
                await send({"type": "websocket.close", "code": 1008})
            return

        await self.app(scope, receive, send)


__all__ = ["OriginValidationMiddleware", "is_allowed_origin"]
