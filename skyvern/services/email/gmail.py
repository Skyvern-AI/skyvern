from dataclasses import dataclass
from datetime import timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from skyvern.schemas.emails import GmailSendErrorCode, GmailSendOutcome
from skyvern.services.email.gmail_client import (
    GMAIL_API_BASE,
    GmailAPIError,
    decode,
    get_json,
    payload_text,
)
from skyvern.services.email.types import EmailAttachment, EmailMessage

GMAIL_SEND_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
GMAIL_SEND_TIMEOUT_SECONDS = 120.0
# Gmail's documented ceiling for an uploaded message.
GMAIL_MAX_MESSAGE_BYTES = 35 * 1024 * 1024
# google.rpc.Code names: the only provider text kept from a rejected send.
_GOOGLE_RPC_CODES = frozenset(
    {
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
    }
)
# The only statuses read as "not sent". A send recorded as failed may be attempted again, so any other
# status, including 408 and 409, stays unknown.
_REJECTED_STATUS_ERROR_CODES = {
    400: GmailSendErrorCode.PROVIDER_REJECTED,
    401: GmailSendErrorCode.RECONNECT,
    403: GmailSendErrorCode.PROVIDER_REJECTED,
    404: GmailSendErrorCode.PROVIDER_REJECTED,
    413: GmailSendErrorCode.ATTACHMENT_TOO_LARGE,
    429: GmailSendErrorCode.RATE_LIMITED,
}
_SYSTEM_LABELS = {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "STARRED", "IMPORTANT", "UNREAD"}


def _is_system_label(label: str) -> bool:
    normalized = label.upper()
    return normalized in _SYSTEM_LABELS or normalized.startswith("CATEGORY_")


async def _resolve_label_id(client: httpx.AsyncClient, access_token: str, label: str) -> str:
    normalized = label.strip() or "INBOX"
    if _is_system_label(normalized):
        return normalized.upper()
    payload = await get_json(client, f"{GMAIL_API_BASE}/users/me/labels", access_token=access_token)
    for item in payload.get("labels") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        label_id = item.get("id")
        if isinstance(name, str) and isinstance(label_id, str) and name.casefold() == normalized.casefold():
            return label_id
    raise GmailAPIError(status=404, code="folder_not_found", message=f"Gmail folder not found: {label}")


def _quote_gmail_value(value: str) -> str | None:
    sanitized = value.replace("\\", "").replace('"', "").strip()
    if not sanitized:
        return None
    return f'"{sanitized}"'


def _build_folder_query(sender: str | None, subject: str | None, newer_than_days: int | None) -> str:
    query_parts: list[str] = []
    if sender:
        quoted_sender = _quote_gmail_value(sender)
        if quoted_sender:
            query_parts.append(f"from:{quoted_sender}")
    if subject:
        quoted_subject = _quote_gmail_value(subject)
        if quoted_subject:
            query_parts.append(f"subject:{quoted_subject}")
    if newer_than_days is not None:
        query_parts.append(f"newer_than:{max(1, newer_than_days)}d")
    return " ".join(query_parts)


def _payload_text_by_mime(payload: dict[str, Any], mime_types: set[str]) -> list[str]:
    texts: list[str] = []
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    decoded = decode(body.get("data"))
    mime_type = str(payload.get("mimeType") or "").lower()
    if decoded and mime_type in mime_types:
        texts.append(decoded)
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            texts.extend(_payload_text_by_mime(part, mime_types))
    return texts


def _attachments(payload: dict[str, Any]) -> list[EmailAttachment]:
    attachments: list[EmailAttachment] = []
    filename = payload.get("filename")
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    attachment_id = body.get("attachmentId")
    if isinstance(filename, str) and filename and isinstance(attachment_id, str):
        size = body.get("size")
        mime_type = payload.get("mimeType")
        attachments.append(
            EmailAttachment(
                name=filename,
                mime_type=mime_type if isinstance(mime_type, str) else None,
                size=size if isinstance(size, int) else None,
                attachment_id=attachment_id,
            )
        )
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            attachments.extend(_attachments(part))
    return attachments


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in payload.get("headers") or []
        if isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("value"), str)
    }


def _date_header_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _message_from_gmail(message: dict[str, Any], include_body: bool) -> EmailMessage | None:
    message_id = message.get("id")
    if not isinstance(message_id, str):
        return None
    raw_payload = message.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    headers = _headers(payload)
    from_name, from_email = parseaddr(headers.get("from") or "")
    body_text = ""
    body_html = None
    if include_body:
        text_parts = _payload_text_by_mime(payload, {"text/plain"})
        html_parts = _payload_text_by_mime(payload, {"text/html"})
        body_text = "\n".join(text_parts) if text_parts else "\n".join(payload_text(payload))
        body_html = "\n".join(html_parts) if html_parts else None
    attachments = _attachments(payload) if include_body else []
    raw_label_ids = message.get("labelIds")
    label_ids = raw_label_ids if isinstance(raw_label_ids, list) else []
    thread_id = message.get("threadId") if isinstance(message.get("threadId"), str) else None
    link_id = thread_id or message_id
    return EmailMessage(
        id=message_id,
        thread_id=thread_id,
        subject=headers.get("subject") or "",
        from_email=from_email,
        from_name=from_name or None,
        to=[address for _, address in getaddresses([headers.get("to") or ""]) if address],
        cc=[address for _, address in getaddresses([headers.get("cc") or ""]) if address],
        date=_date_header_to_iso(headers.get("date")),
        snippet=message.get("snippet") if isinstance(message.get("snippet"), str) else "",
        body_text=body_text,
        body_html=body_html,
        has_attachments=bool(attachments) if include_body else None,
        attachments=attachments,
        is_read="UNREAD" not in label_ids,
        web_link=f"https://mail.google.com/mail/u/0/#all/{link_id}",
    )


def _clamp_max_results(max_results: int) -> int:
    return max(1, min(max_results, 100))


async def list_folder_messages(
    *,
    access_token: str,
    label: str = "INBOX",
    sender: str | None = None,
    subject: str | None = None,
    newer_than_days: int | None = None,
    max_results: int = 25,
    include_body: bool = True,
    client: httpx.AsyncClient | None = None,
) -> list[EmailMessage]:
    async def _list(client_: httpx.AsyncClient) -> list[EmailMessage]:
        label_id = await _resolve_label_id(client_, access_token, label)
        max_results_clamped = _clamp_max_results(max_results)
        params: dict[str, Any] = {
            "labelIds": label_id,
            "maxResults": max_results_clamped,
            "includeSpamTrash": "true" if label_id in {"SPAM", "TRASH"} else "false",
        }
        query = _build_folder_query(sender, subject, newer_than_days)
        if query:
            params["q"] = query
        payload = await get_json(
            client_,
            f"{GMAIL_API_BASE}/users/me/messages",
            access_token=access_token,
            params=params,
        )
        messages: list[EmailMessage] = []
        for ref in (payload.get("messages") or [])[:max_results_clamped]:
            message_id = ref.get("id") if isinstance(ref, dict) else None
            if not isinstance(message_id, str):
                continue
            message = await get_json(
                client_,
                f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}",
                access_token=access_token,
                params={"format": "full" if include_body else "metadata"},
            )
            normalized = _message_from_gmail(message, include_body)
            if normalized:
                messages.append(normalized)
        return messages

    if client is None:
        async with httpx.AsyncClient(timeout=20.0) as owned_client:
            return await _list(owned_client)
    return await _list(client)


@dataclass(frozen=True)
class GmailSendResult:
    outcome: GmailSendOutcome
    provider_message_id: str | None = None
    error_code: GmailSendErrorCode | None = None
    provider_status: int | None = None
    provider_reason: str | None = None


def _response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_error(response: httpx.Response) -> dict[str, Any]:
    error = _response_object(response).get("error")
    return error if isinstance(error, dict) else {}


def _accepted_message_id(response: httpx.Response) -> str | None:
    message_id = _response_object(response).get("id")
    return message_id if isinstance(message_id, str) and message_id else None


async def send_raw_message(
    access_token: str, mime_bytes: bytes, client: httpx.AsyncClient | None = None
) -> GmailSendResult:
    """Send one RFC 822 message with exactly one HTTP attempt."""

    async def _post(client_: httpx.AsyncClient) -> httpx.Response:
        return await client_.post(
            GMAIL_SEND_URL,
            params={"uploadType": "media"},
            content=mime_bytes,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "message/rfc822"},
            timeout=GMAIL_SEND_TIMEOUT_SECONDS,
        )

    # Never retried: after a timeout, a dropped connection or a 5xx, Gmail may already have accepted the message.
    try:
        if client is not None:
            response = await _post(client)
        else:
            async with httpx.AsyncClient() as owned_client:
                response = await _post(owned_client)
    except Exception:
        return GmailSendResult(GmailSendOutcome.UNKNOWN, error_code=GmailSendErrorCode.OUTCOME_UNKNOWN)

    status = response.status_code
    if 200 <= status < 300:
        message_id = _accepted_message_id(response)
        if message_id is not None:
            return GmailSendResult(GmailSendOutcome.ACCEPTED, provider_message_id=message_id)
        return GmailSendResult(
            GmailSendOutcome.UNKNOWN, error_code=GmailSendErrorCode.OUTCOME_UNKNOWN, provider_status=status
        )
    error_code = _REJECTED_STATUS_ERROR_CODES.get(status)
    if error_code is not None:
        rpc_code = _response_error(response).get("status")
        return GmailSendResult(
            GmailSendOutcome.FAILED,
            error_code=error_code,
            provider_status=status,
            provider_reason=rpc_code if rpc_code in _GOOGLE_RPC_CODES else None,
        )
    return GmailSendResult(
        GmailSendOutcome.UNKNOWN, error_code=GmailSendErrorCode.OUTCOME_UNKNOWN, provider_status=status
    )
