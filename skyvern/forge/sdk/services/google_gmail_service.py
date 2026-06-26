import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Any
from urllib.parse import quote

import httpx
import structlog

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
LOG = structlog.get_logger()

_OTP_QUERY_TERMS = "(verification OR verify OR code OR passcode OR otp OR 2fa OR one-time OR password)"
_SAFE_EMAIL_QUERY_IDENTIFIER = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class GmailMessageCandidate:
    message_id: str
    content: str
    internal_date: datetime | None = None


class GmailAPIError(RuntimeError):
    def __init__(self, *, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _newer_than_query(created_after: datetime | None) -> str:
    if not created_after:
        return "newer_than:1d"
    seconds = max(1, (datetime.now(timezone.utc) - _as_utc(created_after)).total_seconds())
    days = max(1, ceil(seconds / 86_400))
    return f"newer_than:{days}d"


def _build_query(totp_identifier: str, *, created_after: datetime | None = None) -> str | None:
    identifier = totp_identifier.strip()
    if not _SAFE_EMAIL_QUERY_IDENTIFIER.fullmatch(identifier):
        return None
    quoted = '"' + identifier.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{_newer_than_query(created_after)} {_OTP_QUERY_TERMS} (to:{quoted} OR deliveredto:{quoted})"


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(f"{data}{'=' * (-len(data) % 4)}").decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def _payload_text(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    decoded = _decode(body.get("data") if isinstance(body, dict) else None)
    mime_type = str(payload.get("mimeType") or "").lower()
    if decoded and mime_type in {"text/plain", "text/html"}:
        texts.append(decoded)
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            texts.extend(_payload_text(part))
    return texts


def _internal_date(message: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _candidate(message: dict[str, Any]) -> GmailMessageCandidate | None:
    message_id = message.get("id")
    if not isinstance(message_id, str):
        return None
    raw_payload = message.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    headers = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers") or []
        if isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("value"), str)
    }
    snippet = message.get("snippet") if isinstance(message.get("snippet"), str) else None
    content = "\n".join(
        part
        for part in [
            f"Subject: {headers['subject']}" if headers.get("subject") else "",
            f"Snippet: {snippet}" if snippet else "",
            "\n".join(_payload_text(payload)),
        ]
        if part
    ).strip()
    return (
        GmailMessageCandidate(message_id=message_id, content=content, internal_date=_internal_date(message))
        if content
        else None
    )


async def search_recent_otp_messages(
    *,
    access_token: str,
    totp_identifier: str,
    created_after: datetime | None = None,
    max_results: int = 10,
    client: httpx.AsyncClient | None = None,
) -> list[GmailMessageCandidate]:
    query = _build_query(totp_identifier, created_after=created_after)
    if query is None:
        return []
    cutoff = _as_utc(created_after) if created_after else None

    async def _search(client_: httpx.AsyncClient) -> list[GmailMessageCandidate]:
        payload = await _get_json(
            client_,
            f"{GMAIL_API_BASE}/users/me/messages",
            access_token=access_token,
            params={"q": query, "maxResults": max(1, min(max_results, 20)), "includeSpamTrash": "false"},
        )
        candidates_: list[GmailMessageCandidate] = []
        for ref in (payload.get("messages") or [])[:max_results]:
            message_id = ref.get("id") if isinstance(ref, dict) else None
            if not isinstance(message_id, str):
                continue
            message = await _get_json(
                client_,
                f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}",
                access_token=access_token,
                params={"format": "full"},
            )
            candidate = _candidate(message)
            if not candidate:
                continue
            if not candidate.internal_date:
                LOG.debug("Skipping Gmail OTP candidate without internalDate", message_id=candidate.message_id)
                continue
            if cutoff and candidate.internal_date < cutoff:
                continue
            candidates_.append(candidate)
        return candidates_

    if client is None:
        async with httpx.AsyncClient(timeout=20.0) as owned_client:
            candidates = await _search(owned_client)
    else:
        candidates = await _search(client)
    return sorted(
        candidates, key=lambda item: item.internal_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
