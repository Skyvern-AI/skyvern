import asyncio
import base64
import binascii
from typing import Any

import httpx
import structlog

from skyvern.forge.sdk.integrations.google.client import (
    RATE_LIMIT_REASONS,
    RETRYABLE_STATUSES,
    GoogleAPIError,
    error_from_response,
    is_rate_limit_refusal,
)
from skyvern.forge.sdk.integrations.http import compute_backoff

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GMAIL_RATE_LIMIT_REASONS = RATE_LIMIT_REASONS
_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 5.0
LOG = structlog.get_logger()


class GmailAPIError(GoogleAPIError):
    pass


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    return compute_backoff(attempt, retry_after, base_seconds=0.5, factor=3, max_seconds=_MAX_BACKOFF_SECONDS)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: httpx.Response | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt == _MAX_ATTEMPTS:
                raise GmailAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Gmail transport failure: {exc}",
                ) from exc
            await asyncio.sleep(_compute_backoff(attempt, None))
            continue
        if response.is_success:
            break
        retryable = response.status_code in RETRYABLE_STATUSES or is_rate_limit_refusal(
            response.status_code, response.text
        )
        if not retryable or attempt == _MAX_ATTEMPTS:
            break
        await asyncio.sleep(_compute_backoff(attempt, response.headers.get("Retry-After")))

    if response is None:
        raise GmailAPIError(status=503, code="upstream_unavailable", message="Gmail transport failure")
    if response.is_success:
        return response.json() or {}

    raise error_from_response(
        response, GmailAPIError, default_message="Gmail API error", body_text_when_message_missing=True
    )


def decode(data: str | None) -> str:
    if not data:
        return ""
    exception_type: str | None = None
    try:
        decoded = base64.urlsafe_b64decode(f"{data}{'=' * (-len(data) % 4)}").decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        decoded = ""
        exception_type = type(exc).__name__
    if not decoded:
        log_fields: dict[str, int | str] = {"encoded_length": len(data)}
        if exception_type:
            log_fields["exception_type"] = exception_type
        LOG.warning("Gmail body data decoded to empty content", **log_fields)
    return decoded


def body_text(decoded: str, mime_type: str) -> str:
    if mime_type != "text/html":
        return decoded

    from bs4 import BeautifulSoup

    from skyvern.forge.sdk.api.email import _plain_text

    return _plain_text(BeautifulSoup(decoded, "html.parser")).strip()


def payload_text(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    decoded = decode(body.get("data"))
    mime_type = str(payload.get("mimeType") or "").lower()
    if decoded and mime_type in {"text/plain", "text/html"}:
        texts.append(body_text(decoded, mime_type))
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            texts.extend(payload_text(part))
    return texts
