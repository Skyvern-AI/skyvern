from __future__ import annotations

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
_BINARY_PLACEHOLDER = "<binary>"


def _sanitize_headers(headers: typing.Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            continue
        sanitized[key] = value
    return sanitized


def _sanitize_body(request: Request, body: bytes, content_type: str | None) -> str:
    if f"{request.method.upper()} {request.url.path.rstrip('/')}" in _SENSITIVE_ENDPOINTS:
        return "****"
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


async def _get_response_body_str(response: Response) -> str:
    response_body = b""
    async for chunk in response.body_iterator:
        response_body += chunk
    response.body_iterator = iterate_in_threadpool(iter([response_body]))

    try:
        return response_body.decode("utf-8")
    except UnicodeDecodeError:
        return str(response_body)
    except Exception:
        return str(response_body)


async def log_raw_request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if not settings.LOG_RAW_API_REQUESTS:
        return await call_next(request)

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
            error_body = await _get_response_body_str(response)
        elif response.status_code >= 400:
            log_method = LOG.warning
            error_body = await _get_response_body_str(response)
        else:
            log_method = LOG.info
            error_body = None

        log_method(
            "api.raw_request",
            method=http_method,
            path=url_path,
            status_code=response.status_code,
            body=body_text,
            headers=sanitized_headers,
            error_body=error_body,
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
        )
        raise
