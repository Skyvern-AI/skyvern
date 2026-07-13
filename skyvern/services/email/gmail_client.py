import asyncio
import base64
import binascii
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 5.0


class GmailAPIError(RuntimeError):
    def __init__(self, *, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        value = retry_after.strip()
        try:
            return min(max(0.0, float(value)), _MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            delta = (target - datetime.now(UTC)).total_seconds()
            return min(max(0.0, delta), _MAX_BACKOFF_SECONDS)
    return min(0.5 * (3 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


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
        if response.is_success or response.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS:
            break
        await asyncio.sleep(_compute_backoff(attempt, response.headers.get("Retry-After")))

    if response is None:
        raise GmailAPIError(status=503, code="upstream_unavailable", message="Gmail transport failure")
    if response.is_success:
        return response.json() or {}

    code = None
    message = response.text[:500] or "Gmail API error"
    try:
        err = (response.json() or {}).get("error")
        if isinstance(err, dict):
            message = err.get("message") or message
            details = err.get("errors")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                code = details[0].get("reason")
    except ValueError:
        pass
    if response.status_code == 403 and code in {"insufficientPermissions", "insufficientScopes"}:
        code = "reconnect_required"
    raise GmailAPIError(status=response.status_code, code=code, message=message)


def decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(f"{data}{'=' * (-len(data) % 4)}").decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def payload_text(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    decoded = decode(body.get("data"))
    mime_type = str(payload.get("mimeType") or "").lower()
    if decoded and mime_type in {"text/plain", "text/html"}:
        texts.append(decoded)
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            texts.extend(payload_text(part))
    return texts
