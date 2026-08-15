from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from skyvern.services.email import gmail_client
from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN

GMAIL_API_BASE = gmail_client.GMAIL_API_BASE
GmailAPIError = gmail_client.GmailAPIError
LOG = structlog.get_logger()

_OTP_QUERY_TERMS = (
    '(verification OR verify OR code OR passcode OR otp OR 2fa OR one-time OR password OR "sign in" OR "sign-in" '
    'OR signin OR "log in" OR "log-in" OR login OR "magic link" OR "access link")'
)
_SAFE_EMAIL_QUERY_IDENTIFIER = SAFE_EMAIL_ADDRESS_PATTERN
_MAX_EXTERNALIZED_TEXT_PART_FETCHES = 4
_MAX_EXTERNALIZED_TEXT_PART_BYTES = 256 * 1024
_decode = gmail_client.decode
_get_json = gmail_client.get_json
_payload_text = gmail_client.payload_text


@dataclass(frozen=True)
class GmailMessageCandidate:
    message_id: str
    content: str
    internal_date: datetime | None = None
    hydration_failed: bool = False


@dataclass(frozen=True)
class _BodyExtraction:
    texts: tuple[str, ...]
    externalized_parts_fetched: int
    externalized_parts_failed: int
    externalized_parts_skipped: int


async def fetch_profile_email(
    *,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    async def _fetch(client_: httpx.AsyncClient) -> str | None:
        payload = await _get_json(
            client_,
            f"{GMAIL_API_BASE}/users/me/profile",
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            return None
        value = payload.get("emailAddress")
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        return candidate if _SAFE_EMAIL_QUERY_IDENTIFIER.fullmatch(candidate) else None

    try:
        if client is not None:
            return await _fetch(client)
        async with httpx.AsyncClient(timeout=20.0) as owned_client:
            return await _fetch(owned_client)
    except (GmailAPIError, ValueError) as exc:
        log_fields: dict[str, str | int | None] = {"exception_type": type(exc).__name__}
        if isinstance(exc, GmailAPIError):
            log_fields.update(status=exc.status, code=exc.code)
        LOG.warning("Failed to fetch Gmail profile email", **log_fields)
        return None


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


def _internal_date(message: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _externalized_text_parts(payload: dict[str, Any]) -> list[tuple[str, int | None]]:
    externalized_parts: list[tuple[str, int | None]] = []
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    mime_type = str(payload.get("mimeType") or "").lower()
    attachment_id = body.get("attachmentId")
    if (
        mime_type in {"text/plain", "text/html"}
        and not payload.get("filename")
        and not body.get("data")
        and isinstance(attachment_id, str)
        and attachment_id
    ):
        size = body.get("size")
        externalized_parts.append((attachment_id, size if isinstance(size, int) else None))
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            externalized_parts.extend(_externalized_text_parts(part))
    return externalized_parts


async def _extract_otp_body_texts(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    message_id: str,
    payload: dict[str, Any],
) -> _BodyExtraction:
    texts = list(_payload_text(payload))
    fetched = 0
    failed = 0
    skipped = 0
    attempts = 0
    # Shared per-message budget so the assembled content stays bounded for the parse prompt.
    remaining = _MAX_EXTERNALIZED_TEXT_PART_BYTES
    for attachment_id, size in _externalized_text_parts(payload):
        if size is not None and size > remaining:
            skipped += 1
            continue
        if attempts >= _MAX_EXTERNALIZED_TEXT_PART_FETCHES:
            skipped += 1
            continue
        attempts += 1
        try:
            body = await _get_json(
                client,
                (
                    f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}/attachments/"
                    f"{quote(attachment_id, safe='')}"
                ),
                access_token=access_token,
            )
        except (GmailAPIError, ValueError) as exc:
            if isinstance(exc, GmailAPIError) and exc.code == "reconnect_required":
                raise
            log_fields: dict[str, str | int | None] = {
                "message_id": message_id,
                "exception_type": type(exc).__name__,
            }
            if isinstance(exc, GmailAPIError):
                log_fields.update(status=exc.status, code=exc.code)
            LOG.warning("Failed to fetch externalized Gmail OTP body part", **log_fields)
            failed += 1
            continue
        if not isinstance(body, dict):
            LOG.warning("Malformed externalized Gmail OTP body part", message_id=message_id)
            failed += 1
            continue
        data = body.get("data")
        response_size = body.get("size")
        if isinstance(response_size, int) and response_size > remaining:
            LOG.warning("Rejected oversized externalized Gmail OTP body part", message_id=message_id)
            failed += 1
            continue
        if not isinstance(data, str) or not data:
            LOG.warning("Malformed externalized Gmail OTP body part", message_id=message_id)
            failed += 1
            continue
        if len(data) > 4 * ((remaining + 2) // 3):
            LOG.warning("Rejected oversized externalized Gmail OTP body data", message_id=message_id)
            failed += 1
            continue
        decoded = _decode(data)
        if not decoded:
            failed += 1
            continue
        texts.append(decoded)
        fetched += 1
        remaining -= len(decoded)
    return _BodyExtraction(
        texts=tuple(texts),
        externalized_parts_fetched=fetched,
        externalized_parts_failed=failed,
        externalized_parts_skipped=skipped,
    )


def _candidate(
    message: dict[str, Any], body_texts: tuple[str, ...], *, hydration_failed: bool = False
) -> GmailMessageCandidate | None:
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
            "\n".join(body_texts),
        ]
        if part
    ).strip()
    return (
        GmailMessageCandidate(
            message_id=message_id,
            content=content,
            internal_date=_internal_date(message),
            hydration_failed=hydration_failed,
        )
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
    excluded_message_ids: Collection[str] | None = None,
) -> list[GmailMessageCandidate]:
    query = _build_query(totp_identifier, created_after=created_after)
    if query is None:
        return []
    cutoff = _as_utc(created_after) if created_after else None
    excluded_ids = set(excluded_message_ids or ())

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
            if message_id in excluded_ids:
                continue
            message = await _get_json(
                client_,
                f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}",
                access_token=access_token,
                params={"format": "full"},
            )
            internal_date = _internal_date(message)
            if not internal_date:
                LOG.debug("Skipping Gmail OTP candidate without internalDate", message_id=message_id)
                continue
            if cutoff and internal_date < cutoff:
                continue
            raw_message_payload = message.get("payload")
            message_payload = raw_message_payload if isinstance(raw_message_payload, dict) else {}
            extraction = await _extract_otp_body_texts(
                client_,
                access_token=access_token,
                message_id=message_id,
                payload=message_payload,
            )
            candidate = _candidate(
                message,
                extraction.texts,
                hydration_failed=(
                    extraction.externalized_parts_failed > 0 and extraction.externalized_parts_fetched == 0
                ),
            )
            LOG.info(
                "Fetched Gmail OTP candidate",
                message_id=message_id,
                content_length=len(candidate.content) if candidate else 0,
                body_parts_extracted=len(extraction.texts),
                externalized_parts_fetched=extraction.externalized_parts_fetched,
                externalized_parts_failed=extraction.externalized_parts_failed,
                externalized_parts_skipped=extraction.externalized_parts_skipped,
            )
            if not candidate:
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
