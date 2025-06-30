from __future__ import annotations

import typing

import structlog

from skyvern.config import settings

if typing.TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from typing import Awaitable, Callable

    from fastapi import Response
    from starlette.requests import Request

LOG = structlog.get_logger()

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}
_MAX_BODY_LENGTH = 1000
_BINARY_PLACEHOLDER = "<binary>"


def _sanitize_headers(headers: typing.Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            continue
        sanitized[key] = value
    return sanitized


def _sanitize_body(body: bytes, content_type: str | None) -> str:
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


async def log_raw_request_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if not settings.LOG_RAW_API_REQUESTS:
        return await call_next(request)

    body_bytes = await request.body()
    # ensure downstream handlers can access body again
    try:
        request._body = body_bytes  # type: ignore[attr-defined]
    except Exception:
        pass

    sanitized_headers = _sanitize_headers(dict(request.headers))
    body_text = _sanitize_body(body_bytes, request.headers.get("content-type"))

    LOG.info(
        "api.raw_request",
        method=request.method,
        path=request.url.path,
        headers=sanitized_headers,
        body=body_text,
    )
    return await call_next(request)
