from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import typing
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial

import structlog
from starlette.concurrency import iterate_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.log_redaction import (
    REDACTED,
    SENSITIVE_HEADERS,
    redact_sensitive_fields,
    strip_artifact_url_query,
)
from skyvern.forge.request_privacy import private_logging_scope, private_request_audit, private_request_path
from skyvern.forge.sdk.forge_log import exception_log_fields

if typing.TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from typing import Awaitable, Callable

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

    from skyvern.forge.sdk.services.request_principal import RequestPrincipal

LOG = structlog.get_logger()

_SENSITIVE_ENDPOINTS = {
    # Agent reports can contain credentials before route redaction.
    "POST /v1/agent_feedback",
    "GET /api/v1/users/me/onboarding",
    "POST /api/v1/users/me/onboarding",
    "POST /api/v1/credentials",
    "POST /v1/credentials",
    "POST /v1/credentials/onepassword/create",
    "POST /v1/credentials/azure_credential/create",
    "POST /v1/credentials/totp",
    "POST /api/v1/totp",
    "GET /v1/credentials/totp",
    "PUT /v1/google/oauth/config",
    "PUT /api/v1/google/oauth/config",
    "POST /v1/google/oauth/callback",
    "POST /api/v1/google/oauth/callback",
    "POST /v1/recipes/jobs/apply",
    "POST /v1/browser_sessions/external",
    # Copilot messages may contain credentials before the route's semantic
    # safety screen runs. The request audit keeps endpoint metadata while the
    # body stays opaque; the route persists only its canonical redacted form.
    "POST /v1/workflow/copilot/chat-post",
    "POST /v1/workflow/copilot/question-response",
    "POST /v1/workflow/copilot/steer",
    "POST /v1/workflow/copilot/credential-response",
    "POST /v1/workflow/copilot/demonstration/control",
    "POST /v1/workflow/copilot/convert-yaml-to-blocks",
    # Payment-provider events carry customer billing details (name, email, address, card metadata).
    "POST /api/v1/stripe_webhook",
}
_SIGNED_TRIGGER_RECEIVER_ENDPOINT = re.compile(r"^POST /(?:api/)?v1/triggers/[^/]+/events$")
_AD_ATTRIBUTION_WITHDRAWAL_ENDPOINT = re.compile(r"^POST /(?:api/)?v1/ad-attribution/withdraw$")
_SENSITIVE_ENDPOINT_PATTERNS = (
    re.compile(r"^POST /(?:api/)?v1/ad-attribution(?:/withdraw)?$"),
    re.compile(r"^(?:POST|PUT) /(?:api/)?v1/credentials(?:/.*)?$"),
    re.compile(r"^(?:GET|POST|PUT|DELETE) /(?:api/)?v1/email-forwarding(?:/.*)?$"),
    re.compile(r"^POST /(?:api/)?v1/email/inbound/[^/]+$"),
    # Twilio credentials and inbound SMS codes must stay opaque even with malformed bodies.
    re.compile(r"^(?:POST|PUT|PATCH|DELETE) /(?:api/)?v1/(?:integrations/twilio|sms)(?:/.*)?$"),
    # MCP arguments and results can contain arbitrary secrets, including malformed JSON.
    re.compile(r"^[^ ]+ /mcp(?:/.*)?$", re.DOTALL),
    # OAuth grants carry form-encoded client secrets and codes, and their responses carry tokens.
    re.compile(r"^POST /oauth/(?:token|consent|callback)$"),
    # Marketplace service routes return freshly minted client secrets.
    re.compile(r"^POST /(?:api/)?v1/marketplace(?:/.*)?$"),
    # Signed trigger events contain arbitrary caller-provided payloads.
    _SIGNED_TRIGGER_RECEIVER_ENDPOINT,
    # Skyplane authority calls carry runtime capabilities, tool arguments, agent feedback text and delivered files.
    re.compile(r"^[^ ]+ /api/v1/skyplane(?:/.*)?$"),
)
_STREAMING_REQUEST_ENDPOINT_PATTERNS = (re.compile(r"^POST /(?:api/)?v1/email/inbound/[^/]+$"),)
_MAX_BODY_LENGTH = 1000
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 404/405 are dominated by internet scanners and MCP clients probing GET for an SSE stream;
# there is nothing to act on, so they stay out of the warn tier.
_ROUTINE_CLIENT_ERROR_STATUSES = frozenset({404, 405})
_MAX_RESPONSE_READ_BYTES = 1024 * 1024  # 1 MB — skip logging bodies larger than this
_BINARY_PLACEHOLDER = "<binary>"
_LOGGABLE_CONTENT_TYPES = {"text/", "application/json"}
_STREAMING_CONTENT_TYPES = ("text/event-stream", "application/x-ndjson")
_ACTION_LOG_ENDPOINT_RE = re.compile(r"^/v1/browser_sessions/[^/]+/action_logs/?$")
_raw_request_exception_logger: ContextVar[typing.Callable[[int], None] | None] = ContextVar(
    "raw_request_exception_logger", default=None
)
_raw_request_stream_success_logger: ContextVar[typing.Callable[[int, str], None] | None] = ContextVar(
    "raw_request_stream_success_logger", default=None
)


@dataclass
class _RequestIdentity:
    organization_id: str | None = None
    organization_name: str | None = None
    org_age: int | None = None
    integration_platform: str | None = None
    principal: RequestPrincipal | None = None
    principal_resolution_key: tuple[str, bytes | None] | None = None
    principal_resolution_conflict: bool = False
    bearer_identity_status: str | None = None
    principal_resolution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    load_shed: bool = False


_request_identity: ContextVar[_RequestIdentity | None] = ContextVar("raw_request_identity", default=None)


def get_request_principal_state() -> _RequestIdentity | None:
    return _request_identity.get()


def set_request_organization(
    organization_id: str | None,
    organization_name: str | None = None,
    org_age: int | None = None,
) -> None:
    """Attribute the in-flight ``api.raw_request`` record to the authenticated organization.

    Auth resolves in a child task of this middleware, and a ContextVar rebound there never
    reaches the middleware that emits the record. Mutating the holder bound before
    ``call_next`` does, because the child inherits the same object.
    """
    identity = _request_identity.get()
    if identity is None:
        return
    if organization_id:
        if identity.principal is not None and organization_id != identity.principal.organization_id:
            identity.principal_resolution_conflict = True
            return
        identity.organization_id = organization_id
    if organization_name:
        identity.organization_name = organization_name
    if org_age is not None:
        identity.org_age = org_age


def mark_request_load_shed() -> None:
    """Log this request's 503 at warning level: the server shed it on purpose under load, before doing any work."""
    identity = _request_identity.get()
    if identity is not None:
        identity.load_shed = True


def set_request_principal(
    principal: RequestPrincipal,
    resolution_key: tuple[str, bytes | None],
    bearer_identity_status: str,
) -> None:
    identity = _request_identity.get()
    if identity is None:
        return
    # The bound member key takes precedence over any bearer principal observed earlier in this request.
    if principal.auth_kind == "member_api_key":
        identity.principal = principal
        identity.principal_resolution_key = resolution_key
        identity.bearer_identity_status = bearer_identity_status
    elif identity.principal is None:
        identity.principal = principal
        identity.principal_resolution_key = resolution_key
        identity.bearer_identity_status = bearer_identity_status
    elif identity.principal_resolution_key != resolution_key:
        identity.principal_resolution_conflict = True


def _identity_log_fields() -> dict[str, str | int | bool | None]:
    identity = _request_identity.get()
    if identity is None:
        return {}
    fields: dict[str, str | int | bool | None] = {}
    if identity.organization_id:
        fields["organization_id"] = identity.organization_id
    if identity.organization_name:
        fields["organization_name"] = identity.organization_name
    if identity.org_age is not None:
        fields["org_age"] = identity.org_age
    if identity.integration_platform is not None:
        fields["integration_platform"] = identity.integration_platform
    if identity.principal is not None:
        # Explicit nulls: "resolved, no user" must stay distinguishable from an unauthenticated request.
        fields["auth_kind"] = identity.principal.auth_kind.value
        fields["user_id"] = identity.principal.user_id
        fields["org_role"] = identity.principal.org_role
        fields["org_role_claim"] = identity.principal.org_role_claim
        fields["token_has_organization_claim"] = identity.principal.token_has_organization_claim
        fields["bearer_identity_status"] = identity.bearer_identity_status
        fields["principal_resolution_conflict"] = identity.principal_resolution_conflict
    return fields


def _should_log_request_bodies() -> bool:
    identity = _request_identity.get()
    if identity is None or identity.organization_id is None:
        return True
    return app.AGENT_FUNCTION.should_log_request_bodies(identity.organization_id)


def _schedule_validation_error_code(status_code: int, response_body: str) -> str | None:
    if status_code != 400:
        return None
    try:
        response = json.loads(response_body)
    except json.JSONDecodeError:
        return None
    detail = response.get("detail") if isinstance(response, dict) else None
    if isinstance(detail, str) and detail.startswith("Missing schedule parameters for workflow "):
        return "missing_schedule_parameters"
    return None


def integration_platform_from_user_agent(user_agent: str | None) -> str | None:
    if not isinstance(user_agent, str) or not user_agent.strip():
        return None

    value = user_agent.casefold()
    if re.search(r"(?<![\w-])zapier(?![\w-])", value):
        return "zapier"
    if re.search(r"(?<![\w-])n8n(?![\w-])", value):
        return "n8n"
    if value.startswith("make/") or re.search(r"\bmake\.com\b", value) or "integromat" in value:
        return "make"
    if re.search(r"(?<![\w-])workato(?![\w-])", value):
        return "workato"
    return "other"


def _sanitize_headers(headers: typing.Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            continue
        sanitized[key] = value
    return sanitized


def _client_ip_from_headers(headers: typing.Mapping[str, str]) -> str | None:
    # First hop may be client-supplied (spoofable); acceptable for Datadog alert grouping.
    value = headers.get("x-forwarded-for")
    if not value:
        return None
    first_hop = value.split(",")[0].strip()
    return first_hop or None


def _is_fixture_endpoint(request: Request) -> bool:
    return request.url.path.rstrip("/") == "/api/v1/eval-fixtures" or request.url.path.startswith(
        "/api/v1/eval-fixtures/"
    )


def _is_sensitive_endpoint(request: Request) -> bool:
    endpoint = f"{request.method.upper()} {request.url.path.rstrip('/')}"
    return (
        private_request_path(request.scope) is not None
        or _is_fixture_endpoint(request)
        or endpoint in _SENSITIVE_ENDPOINTS
        or any(pattern.fullmatch(endpoint) for pattern in _SENSITIVE_ENDPOINT_PATTERNS)
        or (request.method.upper() == "POST" and _ACTION_LOG_ENDPOINT_RE.fullmatch(request.url.path) is not None)
    )


def _uses_streaming_request_body(request: Request) -> bool:
    endpoint = f"{request.method.upper()} {request.url.path.rstrip('/')}"
    return any(pattern.fullmatch(endpoint) for pattern in _STREAMING_REQUEST_ENDPOINT_PATTERNS)


def _sanitize_body(request: Request, body: bytes, content_type: str | None) -> str:
    if _is_sensitive_endpoint(request):
        return REDACTED
    if not body:
        return ""
    if content_type and not (content_type.startswith("text/") or content_type.startswith("application/json")):
        return _BINARY_PLACEHOLDER
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return _BINARY_PLACEHOLDER
    text = _redact_loggable_body(text)
    if len(text) > _MAX_BODY_LENGTH:
        return text[:_MAX_BODY_LENGTH] + "...[truncated]"
    return text


def _redact_loggable_body(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return strip_artifact_url_query(text)
    redacted = redact_sensitive_fields(parsed)
    return json.dumps(redacted) if redacted != parsed else text


def _is_loggable_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True  # assume text when header is missing
    return any(content_type.startswith(prefix) for prefix in _LOGGABLE_CONTENT_TYPES)


def _sanitize_response_body(request: Request, body_str: str | None, content_type: str | None) -> str:
    if _is_sensitive_endpoint(request):
        return REDACTED
    if body_str is None:
        return _BINARY_PLACEHOLDER
    if not body_str:
        return ""
    if not _is_loggable_content_type(content_type):
        return _BINARY_PLACEHOLDER
    text = _redact_loggable_body(body_str)
    if len(text) > _MAX_BODY_LENGTH:
        return text[:_MAX_BODY_LENGTH] + "...[truncated]"
    return text


async def _get_response_body_str(response: Response) -> str | None:
    """Read and reconstitute the response body for logging.

    Returns ``None`` when the body is binary or exceeds
    ``_MAX_RESPONSE_READ_BYTES`` to avoid buffering large payloads
    solely for logging purposes.
    """
    response_body = b""
    async for chunk in response.body_iterator:
        response_body += chunk
    response.body_iterator = iterate_in_threadpool(iter([response_body]))

    if len(response_body) > _MAX_RESPONSE_READ_BYTES:
        return None

    try:
        return response_body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _log_unhandled_request(
    status_code: int,
    *,
    method: str,
    path: str,
    client_ip: str | None,
    body: str,
    headers: dict[str, str],
    start_time: float,
) -> None:
    """Emit the raw-request row after the server error handler selects a response."""
    exc = sys.exc_info()[1]
    try:
        body_fields = {"body": body} if _should_log_request_bodies() else {}
        # No traceback: the server error handler already logs it, and with one this row can pass the
        # container log driver's 16 KiB line limit and be split into fragments that lose every field.
        LOG.error(
            "api.raw_request",
            method=method,
            path=path,
            status_code=status_code,
            client_ip=client_ip,
            headers=headers,
            duration_seconds=time.monotonic() - start_time,
            **body_fields,
            **(exception_log_fields(exc) if exc is not None else {}),
            **_identity_log_fields(),
        )
    except Exception:
        pass


def _log_request(
    status_code: int,
    response_body: str,
    *,
    method: str,
    path: str,
    client_ip: str | None,
    body: str,
    headers: dict[str, str],
    start_time: float,
) -> None:
    identity = _request_identity.get()
    if status_code == 503 and identity is not None and identity.load_shed:
        log_method = LOG.warning
    elif status_code >= 500:
        log_method = LOG.error
    elif status_code >= 400 and status_code not in _ROUTINE_CLIENT_ERROR_STATUSES:
        log_method = LOG.warning
    else:
        log_method = LOG.info

    try:
        body_fields = (
            {
                "body": body,
                "response_body": response_body,
                # backwards-compat: keep error_body for existing Datadog queries
                "error_body": response_body if status_code >= 400 else None,
            }
            if _should_log_request_bodies()
            else {}
        )
        log_method(
            "api.raw_request",
            method=method,
            path=path,
            status_code=status_code,
            client_ip=client_ip,
            headers=headers,
            duration_seconds=time.monotonic() - start_time,
            error_code=_schedule_validation_error_code(status_code, response_body),
            **body_fields,
            **_identity_log_fields(),
        )
    except Exception:
        pass


def log_raw_request_exception(status_code: int) -> None:
    """Log an unhandled request once its outer error handler has chosen the status."""
    logger = _raw_request_exception_logger.get()
    _raw_request_exception_logger.set(None)
    if logger is not None:
        logger(status_code)


async def log_raw_request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    integration_platform = integration_platform_from_user_agent(request.headers.get("user-agent"))
    _request_identity.set(_RequestIdentity(integration_platform=integration_platform))
    if not settings.LOG_RAW_API_REQUESTS:
        return await call_next(request)

    start_time = time.monotonic()
    body_buffered = True
    try:
        if _is_fixture_endpoint(request):
            bounded = bytearray()
            async with asyncio.timeout(2):
                async for chunk in request.stream():
                    if len(bounded) + len(chunk) > 4096:
                        return Response(status_code=413)
                    bounded.extend(chunk)
            body_bytes = bytes(bounded)
        elif _SIGNED_TRIGGER_RECEIVER_ENDPOINT.fullmatch(
            endpoint := f"{request.method.upper()} {request.url.path.rstrip('/')}"
        ) or _AD_ATTRIBUTION_WITHDRAWAL_ENDPOINT.fullmatch(endpoint):
            # Route-level body limits must see the unconsumed request stream.
            body_bytes = b""
            body_buffered = False
        elif _uses_streaming_request_body(request):
            body_bytes = b""
            body_buffered = False
        else:
            body_bytes = await request.body()
    except TimeoutError:
        return Response(status_code=408)
    except ClientDisconnect:
        # The client closed the connection before the body finished streaming, so no
        # response will reach it. Short-circuit with a benign 499 instead of letting
        # the disconnect escape this BaseHTTPMiddleware (which wraps it in an
        # ExceptionGroup) and surface as an unhandled error in tracking.
        LOG.info("api.client_disconnect", method=request.method, path=request.url.path)
        return Response(status_code=499)
    # ensure downstream handlers can access body again
    if body_buffered:
        try:
            request._body = body_bytes  # type: ignore[attr-defined]
        except Exception:
            pass

    url_path = request.url.path
    http_method = request.method
    request_headers = dict(request.headers)
    sanitized_headers = _sanitize_headers(request_headers)
    client_ip = _client_ip_from_headers(request_headers)
    body_text = _sanitize_body(request, body_bytes, request.headers.get("content-type"))
    request_logger = partial(
        _log_unhandled_request,
        method=http_method,
        path=url_path,
        client_ip=client_ip,
        body=body_text,
        headers=sanitized_headers,
        start_time=start_time,
    )
    _raw_request_exception_logger.set(request_logger)

    response = await call_next(request)
    resp_content_type = response.headers.get("content-type", "")
    is_streaming_response = any(content_type in resp_content_type for content_type in _STREAMING_CONTENT_TYPES)

    # Skip successful reads before buffering the response body; 4xx/5xx and
    # mutating paths keep logging, and sensitive endpoints always keep
    # their redacted audit line.
    log_success = not (
        response.status_code < 400
        and http_method in _READ_METHODS
        and not settings.LOG_RAW_API_REQUESTS_SUCCESSFUL_READS
        and not _is_sensitive_endpoint(request)
    )
    if is_streaming_response:
        _raw_request_stream_success_logger.set(
            partial(
                _log_request,
                method=http_method,
                path=url_path,
                client_ip=client_ip,
                body=body_text,
                headers=sanitized_headers,
                start_time=start_time,
            )
            if log_success
            else None
        )
        return response

    if not log_success:
        _raw_request_exception_logger.set(None)
        return response

    raw_response_body = await _get_response_body_str(response)
    _log_request(
        response.status_code,
        _sanitize_response_body(request, raw_response_body, resp_content_type),
        method=http_method,
        path=url_path,
        client_ip=client_ip,
        body=body_text,
        headers=sanitized_headers,
        start_time=start_time,
    )
    _raw_request_exception_logger.set(None)
    return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await log_raw_request_middleware(request, call_next)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        if audit_path := private_request_path(scope):
            with private_logging_scope(audit_path):
                identity = _request_identity.set(_RequestIdentity())
                try:
                    await self._private_request(scope, receive, send, audit_path=audit_path)
                finally:
                    _request_identity.reset(identity)
            return

        # BaseHTTPMiddleware raises a started stream's error only after dispatch returns.
        # Keep the client-visible status here so that error is still countable.
        response_status: int | None = None

        async def send_with_response_status(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        try:
            await super().__call__(scope, receive, send_with_response_status)
        except Exception:
            if response_status is not None:
                log_raw_request_exception(response_status)
            raise
        else:
            stream_success_logger = _raw_request_stream_success_logger.get()
            if response_status is not None and stream_success_logger is not None:
                stream_success_logger(response_status, "<streaming>")
            _raw_request_exception_logger.set(None)
        finally:
            _raw_request_stream_success_logger.set(None)

    async def _private_request(self, scope: Scope, receive: Receive, send: Send, *, audit_path: str) -> None:
        start = time.monotonic()
        status: int | None = None
        complete = False
        outcome = "complete"

        async def opaque_send(message: Message) -> None:
            nonlocal status, complete
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            try:
                # Passing receive/send directly keeps payload bytes out of the logger's buffers.
                await self.app(scope, receive, opaque_send)
            except ClientDisconnect:
                outcome = "disconnected"
                if status is None:
                    try:
                        await Response(status_code=499)(scope, receive, opaque_send)
                    except Exception:
                        pass
            except Exception:
                outcome = "stream_failed" if status is not None else "failed"
                if status is None:
                    try:
                        await Response(
                            b'{"detail":"private_request_failed"}', status_code=500, media_type="application/json"
                        )(scope, receive, opaque_send)
                    except Exception:
                        pass
                elif not complete:
                    try:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    except Exception:
                        pass
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if settings.LOG_RAW_API_REQUESTS:
                code = status
                log = (
                    LOG.error
                    if outcome != "complete" or (code is not None and code >= 500)
                    else LOG.warning
                    if code is not None and code >= 400
                    else LOG.info
                )
                try:
                    with private_request_audit():
                        log(
                            "api.raw_request",
                            method=scope["method"]
                            if scope["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                            else "OTHER",
                            path=audit_path,
                            status_code=code,
                            duration_seconds=time.monotonic() - start,
                            outcome=outcome,
                        )
                except Exception:
                    pass
