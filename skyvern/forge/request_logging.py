from __future__ import annotations

import json
import re
import time
import typing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial

import structlog
from starlette.concurrency import iterate_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from skyvern.config import settings
from skyvern.forge.log_redaction import (
    REDACTED,
    SENSITIVE_HEADERS,
    redact_sensitive_fields,
    strip_artifact_url_query,
)

if typing.TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from typing import Awaitable, Callable

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

LOG = structlog.get_logger()

_SENSITIVE_ENDPOINTS = {
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
    # Copilot messages may contain credentials before the route's semantic
    # safety screen runs. The request audit keeps endpoint metadata while the
    # body stays opaque; the route persists only its canonical redacted form.
    "POST /v1/workflow/copilot/chat-post",
    "POST /v1/workflow/copilot/question-response",
    "POST /v1/workflow/copilot/credential-response",
    "POST /v1/workflow/copilot/convert-yaml-to-blocks",
}
_SENSITIVE_ENDPOINT_PATTERNS = (re.compile(r"^(?:POST|PUT) /(?:api/)?v1/credentials(?:/.*)?$"),)
_MAX_BODY_LENGTH = 1000
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 404/405 are dominated by internet scanners and MCP clients probing GET for an SSE stream;
# there is nothing to act on, so they stay out of the warn tier.
_ROUTINE_CLIENT_ERROR_STATUSES = frozenset({404, 405})
_MAX_RESPONSE_READ_BYTES = 1024 * 1024  # 1 MB — skip logging bodies larger than this
_BINARY_PLACEHOLDER = "<binary>"
_LOGGABLE_CONTENT_TYPES = {"text/", "application/json"}
_STREAMING_CONTENT_TYPE = "text/event-stream"
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


_request_identity: ContextVar[_RequestIdentity | None] = ContextVar("raw_request_identity", default=None)


def set_request_organization(organization_id: str | None, organization_name: str | None = None) -> None:
    """Attribute the in-flight ``api.raw_request`` record to the authenticated organization.

    Auth resolves in a child task of this middleware, and a ContextVar rebound there never
    reaches the middleware that emits the record. Mutating the holder bound before
    ``call_next`` does, because the child inherits the same object.
    """
    identity = _request_identity.get()
    if identity is None:
        return
    if organization_id:
        identity.organization_id = organization_id
    if organization_name:
        identity.organization_name = organization_name


def _organization_log_fields() -> dict[str, str]:
    identity = _request_identity.get()
    if identity is None:
        return {}
    fields: dict[str, str] = {}
    if identity.organization_id:
        fields["organization_id"] = identity.organization_id
    if identity.organization_name:
        fields["organization_name"] = identity.organization_name
    return fields


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


def _is_sensitive_endpoint(request: Request) -> bool:
    endpoint = f"{request.method.upper()} {request.url.path.rstrip('/')}"
    return (
        endpoint in _SENSITIVE_ENDPOINTS
        or any(pattern.fullmatch(endpoint) for pattern in _SENSITIVE_ENDPOINT_PATTERNS)
        or (request.method.upper() == "POST" and _ACTION_LOG_ENDPOINT_RE.fullmatch(request.url.path) is not None)
    )


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
    try:
        LOG.error(
            "api.raw_request",
            method=method,
            path=path,
            status_code=status_code,
            client_ip=client_ip,
            body=body,
            headers=headers,
            exc_info=True,
            duration_seconds=time.monotonic() - start_time,
            **_organization_log_fields(),
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
    if status_code >= 500:
        log_method = LOG.error
    elif status_code >= 400 and status_code not in _ROUTINE_CLIENT_ERROR_STATUSES:
        log_method = LOG.warning
    else:
        log_method = LOG.info

    try:
        log_method(
            "api.raw_request",
            method=method,
            path=path,
            status_code=status_code,
            client_ip=client_ip,
            body=body,
            headers=headers,
            response_body=response_body,
            # backwards-compat: keep error_body for existing Datadog queries
            error_body=response_body if status_code >= 400 else None,
            duration_seconds=time.monotonic() - start_time,
            **_organization_log_fields(),
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
    if not settings.LOG_RAW_API_REQUESTS:
        return await call_next(request)

    start_time = time.monotonic()
    _request_identity.set(_RequestIdentity())
    try:
        body_bytes = await request.body()
    except ClientDisconnect:
        # The client closed the connection before the body finished streaming, so no
        # response will reach it. Short-circuit with a benign 499 instead of letting
        # the disconnect escape this BaseHTTPMiddleware (which wraps it in an
        # ExceptionGroup) and surface as an unhandled error in tracking.
        LOG.info("api.client_disconnect", method=request.method, path=request.url.path)
        return Response(status_code=499)
    # ensure downstream handlers can access body again
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
    is_streaming_response = _STREAMING_CONTENT_TYPE in resp_content_type

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
