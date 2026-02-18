from __future__ import annotations

import json
import time
import typing

import structlog
from starlette.concurrency import iterate_in_threadpool

from skyvern.config import settings

if typing.TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from typing import Awaitable, Callable

    from fastapi import Response
    from starlette.requests import Request

LOG = structlog.get_logger()

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}
_SENSITIVE_ENDPOINTS = {
    "POST /api/v1/credentials",
    "POST /v1/credentials",
    "POST /v1/credentials/onepassword/create",
    "POST /v1/credentials/azure_credential/create",
}
_MAX_BODY_LENGTH = 1000
_MAX_RESPONSE_READ_BYTES = 1024 * 1024  # 1 MB — skip logging bodies larger than this
_BINARY_PLACEHOLDER = "<binary>"
_REDACTED = "****"
_LOGGABLE_CONTENT_TYPES = {"text/", "application/json"}
_STREAMING_CONTENT_TYPE = "text/event-stream"

# Exact field names that are always redacted.  Use a set for O(1) lookup
# instead of regex substring matching to avoid false positives like
# credential_id, author, page_token, etc.
_SENSITIVE_FIELDS: set[str] = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "credential",
    "access_key",
    "private_key",
    "auth",
    "authorization",
    "secret_key",
}


def _sanitize_headers(headers: typing.Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            continue
        sanitized[key] = value
    return sanitized


def _sanitize_body(request: Request, body: bytes, content_type: str | None) -> str:
    if f"{request.method.upper()} {request.url.path.rstrip('/')}" in _SENSITIVE_ENDPOINTS:
        return _REDACTED
    if not body:
        return ""
    if content_type and not (content_type.startswith("text/") or content_type.startswith("application/json")):
        return _BINARY_PLACEHOLDER
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return _BINARY_PLACEHOLDER
    if len(text) > _MAX_BODY_LENGTH:
        return text[:_MAX_BODY_LENGTH] + "...[truncated]"
    return text


def _is_sensitive_key(key: str) -> bool:
    return key.lower() in _SENSITIVE_FIELDS


def _redact_sensitive_fields(obj: typing.Any, _depth: int = 0) -> typing.Any:
    """Redact dict values whose *key name* exactly matches a known sensitive field.

    Uses exact-match (case-insensitive) rather than substring/regex to avoid
    false positives on fields like ``credential_id``, ``author``, or
    ``page_token`` which contain sensitive substrings but are not secrets.
    """
    if _depth > 20:
        # Stop recursing but still redact sensitive keys at this level
        if isinstance(obj, dict):
            return {k: _REDACTED if _is_sensitive_key(k) else v for k, v in obj.items()}
        return obj
    if isinstance(obj, dict):
        return {
            k: _REDACTED if _is_sensitive_key(k) else _redact_sensitive_fields(v, _depth + 1) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive_fields(item, _depth + 1) for item in obj]
    return obj


def _is_loggable_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True  # assume text when header is missing
    return any(content_type.startswith(prefix) for prefix in _LOGGABLE_CONTENT_TYPES)


def _sanitize_response_body(request: Request, body_str: str | None, content_type: str | None) -> str:
    if f"{request.method.upper()} {request.url.path.rstrip('/')}" in _SENSITIVE_ENDPOINTS:
        return _REDACTED
    if body_str is None:
        return _BINARY_PLACEHOLDER
    if not body_str:
        return ""
    if not _is_loggable_content_type(content_type):
        return _BINARY_PLACEHOLDER
    try:
        parsed = json.loads(body_str)
        redacted = _redact_sensitive_fields(parsed)
        text = json.dumps(redacted)
    except (json.JSONDecodeError, TypeError):
        text = body_str
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


async def log_raw_request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if not settings.LOG_RAW_API_REQUESTS:
        return await call_next(request)

    start_time = time.monotonic()
    body_bytes = await request.body()
    # ensure downstream handlers can access body again
    try:
        request._body = body_bytes  # type: ignore[attr-defined]
    except Exception:
        pass

    url_path = request.url.path
    http_method = request.method
    sanitized_headers = _sanitize_headers(dict(request.headers))
    body_text = _sanitize_body(request, body_bytes, request.headers.get("content-type"))

    try:
        response = await call_next(request)

        if response.status_code >= 500:
            log_method = LOG.error
        elif response.status_code >= 400:
            log_method = LOG.warning
        else:
            log_method = LOG.info

        resp_content_type = response.headers.get("content-type", "")
        if _STREAMING_CONTENT_TYPE in resp_content_type:
            response_body = "<streaming>"
        else:
            raw_response_body = await _get_response_body_str(response)
            response_body = _sanitize_response_body(request, raw_response_body, resp_content_type)

        log_method(
            "api.raw_request",
            method=http_method,
            path=url_path,
            status_code=response.status_code,
            body=body_text,
            headers=sanitized_headers,
            response_body=response_body,
            # backwards-compat: keep error_body for existing Datadog queries
            error_body=response_body if response.status_code >= 400 else None,
            duration_seconds=time.monotonic() - start_time,
        )
        return response
    except Exception:
        LOG.error(
            "api.raw_request",
            method=http_method,
            path=url_path,
            body=body_text,
            headers=sanitized_headers,
            exc_info=True,
            duration_seconds=time.monotonic() - start_time,
        )
        raise
