import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import structlog

from skyvern.services.email.types import EmailAttachment, EmailMessage

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
LOG = structlog.get_logger()
_MAX_SUBJECT_FILTER_PAGES = 10
_MAX_SUBJECT_FILTER_FETCHED = 250
_MAX_ATTACHMENT_PAGES = 4
_MAX_FOLDER_SEARCH_DEPTH = 5
_MAX_FOLDER_SEARCH_FOLDERS = 500
_PERMISSIVE_RECEIVED_DATETIME_LOWER_BOUND = "1900-01-01T00:00:00Z"
_RECONNECT_ERROR_CODES = {
    "invalidauthenticationtoken",
    "accessdenied",
    "erroraccessdenied",
    "authorization_requestdenied",
}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 5.0
_WELL_KNOWN_FOLDERS = {"inbox", "drafts", "sentitems", "deleteditems", "junkemail", "archive", "clutter", "outbox"}


class OutlookAPIError(RuntimeError):
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


async def _get_json(
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
                raise OutlookAPIError(
                    status=503,
                    code="upstream_unavailable",
                    message=f"Microsoft Graph transport failure: {exc}",
                ) from exc
            await asyncio.sleep(_compute_backoff(attempt, None))
            continue
        if response.is_success or response.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS:
            break
        await asyncio.sleep(_compute_backoff(attempt, response.headers.get("Retry-After")))

    if response is None:
        raise OutlookAPIError(status=503, code="upstream_unavailable", message="Microsoft Graph transport failure")
    if response.is_success:
        return response.json() or {}

    code = None
    message = response.text[:500] or "Microsoft Graph API error"
    try:
        err = (response.json() or {}).get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or message
    except ValueError:
        pass
    if response.status_code == 401 or (
        response.status_code == 403 and isinstance(code, str) and code.casefold() in _RECONNECT_ERROR_CODES
    ):
        code = "reconnect_required"
    raise OutlookAPIError(status=response.status_code, code=code, message=message)


def _clamp_max_results(max_results: int) -> int:
    return max(1, min(max_results, 100))


def _escape_odata_string(value: str) -> str:
    return value.replace("'", "''")


def _validated_next_link(raw_next_link: Any) -> str | None:
    if not isinstance(raw_next_link, str) or not raw_next_link:
        return None
    try:
        parsed = urlparse(raw_next_link)
        hostname = parsed.hostname
    except ValueError:
        LOG.warning("Ignoring malformed Outlook pagination URL", next_link_host=None)
        return None
    if parsed.scheme != "https" or hostname is None or hostname.casefold() != "graph.microsoft.com":
        LOG.warning(
            "Ignoring untrusted Outlook pagination URL",
            next_link_host=hostname,
        )
        return None
    return raw_next_link


async def _find_folder_id_by_display_name(
    client: httpx.AsyncClient,
    access_token: str,
    url: str,
    display_name: str,
) -> str | None:
    payload = await _get_json(
        client,
        url,
        access_token=access_token,
        params={
            "$filter": f"displayName eq '{_escape_odata_string(display_name)}'",
            "$select": "id,displayName",
        },
    )
    for item in payload.get("value") or []:
        folder_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(folder_id, str) and folder_id:
            return folder_id
    return None


async def _search_folder_tree_by_display_name(
    client: httpx.AsyncClient,
    access_token: str,
    display_name: str,
) -> str | None:
    queue: list[tuple[str, dict[str, Any] | None, int]] = [
        (f"{GRAPH_API_BASE}/me/mailFolders", {"$select": "id,displayName"}, 0)
    ]
    visited = 0
    target = display_name.casefold()
    while queue and visited < _MAX_FOLDER_SEARCH_FOLDERS:
        url, params, depth = queue.pop(0)
        payload = await _get_json(client, url, access_token=access_token, params=params)
        items = payload.get("value")
        for item in items if isinstance(items, list) else []:
            if visited >= _MAX_FOLDER_SEARCH_FOLDERS or not isinstance(item, dict):
                break
            visited += 1
            folder_id = item.get("id")
            folder_name = item.get("displayName")
            if isinstance(folder_id, str) and isinstance(folder_name, str):
                if folder_name.casefold() == target:
                    return folder_id
                if depth < _MAX_FOLDER_SEARCH_DEPTH:
                    queue.append(
                        (
                            f"{GRAPH_API_BASE}/me/mailFolders/{quote(folder_id, safe='')}/childFolders",
                            {"$select": "id,displayName"},
                            depth + 1,
                        )
                    )
        next_link = _validated_next_link(payload.get("@odata.nextLink"))
        if next_link and visited < _MAX_FOLDER_SEARCH_FOLDERS:
            queue.insert(0, (next_link, None, depth))
    return None


async def _resolve_folder_id(client: httpx.AsyncClient, access_token: str, folder: str) -> str:
    normalized = folder.strip() or "inbox"
    lower = normalized.lower()
    if lower in _WELL_KNOWN_FOLDERS:
        return lower
    if "/" in normalized:
        segments = [segment.strip() for segment in normalized.split("/") if segment.strip()]
        if segments:
            folder_id = await _find_folder_id_by_display_name(
                client,
                access_token,
                f"{GRAPH_API_BASE}/me/mailFolders",
                segments[0],
            )
            for segment in segments[1:]:
                if not folder_id:
                    break
                folder_id = await _find_folder_id_by_display_name(
                    client,
                    access_token,
                    f"{GRAPH_API_BASE}/me/mailFolders/{quote(folder_id, safe='')}/childFolders",
                    segment,
                )
            if folder_id:
                return folder_id
        raise OutlookAPIError(status=404, code="folder_not_found", message=f"Outlook folder not found: {folder}")
    folder_id = await _find_folder_id_by_display_name(
        client,
        access_token,
        f"{GRAPH_API_BASE}/me/mailFolders",
        normalized,
    )
    if folder_id:
        return folder_id
    folder_id = await _search_folder_tree_by_display_name(client, access_token, normalized)
    if folder_id:
        return folder_id
    raise OutlookAPIError(status=404, code="folder_not_found", message=f"Outlook folder not found: {folder}")


def _email_address(value: dict[str, Any] | None) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        return "", None
    email_address = value.get("emailAddress")
    if not isinstance(email_address, dict):
        return "", None
    address = email_address.get("address")
    name = email_address.get("name")
    return (address if isinstance(address, str) else "", name if isinstance(name, str) else None)


def _recipient_addresses(recipients: list[Any] | None) -> list[str]:
    addresses: list[str] = []
    for recipient in recipients or []:
        if not isinstance(recipient, dict):
            continue
        address, _ = _email_address(recipient)
        if address:
            addresses.append(address)
    return addresses


async def _attachments(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    message_id: str,
) -> list[EmailAttachment]:
    attachments: list[EmailAttachment] = []
    request_url: str | None = f"{GRAPH_API_BASE}/me/messages/{quote(message_id, safe='')}/attachments"
    request_params: dict[str, Any] | None = {"$select": "id,name,contentType,size"}
    page_count = 0
    try:
        while request_url and page_count < _MAX_ATTACHMENT_PAGES:
            payload = await _get_json(client, request_url, access_token=access_token, params=request_params)
            page_count += 1
            for item in payload.get("value") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                size = item.get("size")
                attachment_id = item.get("id")
                content_type = item.get("contentType")
                attachments.append(
                    EmailAttachment(
                        name=name,
                        mime_type=content_type if isinstance(content_type, str) else None,
                        size=size if isinstance(size, int) else None,
                        attachment_id=attachment_id if isinstance(attachment_id, str) else None,
                    )
                )
            request_url = _validated_next_link(payload.get("@odata.nextLink"))
            request_params = None
    except OutlookAPIError as exc:
        if exc.code == "reconnect_required":
            raise
        LOG.warning(
            "Failed to fetch Outlook message attachments",
            status=exc.status,
            code=exc.code,
        )
    return attachments


def _message_from_graph(
    message: dict[str, Any],
    attachments: list[EmailAttachment],
    *,
    include_body: bool,
) -> EmailMessage | None:
    message_id = message.get("id")
    if not isinstance(message_id, str):
        return None
    from_email, from_name = _email_address(message.get("from") if isinstance(message.get("from"), dict) else None)
    body_text = ""
    body_html = None
    raw_body = message.get("body")
    body = raw_body if include_body and isinstance(raw_body, dict) else {}
    body_content = body.get("content")
    if isinstance(body_content, str):
        if str(body.get("contentType") or "").lower() == "html":
            body_html = body_content
        else:
            body_text = body_content
    return EmailMessage(
        id=message_id,
        thread_id=message.get("conversationId") if isinstance(message.get("conversationId"), str) else None,
        subject=message.get("subject") if isinstance(message.get("subject"), str) else "",
        from_email=from_email,
        from_name=from_name,
        to=_recipient_addresses(message.get("toRecipients") if isinstance(message.get("toRecipients"), list) else []),
        cc=_recipient_addresses(message.get("ccRecipients") if isinstance(message.get("ccRecipients"), list) else []),
        date=message.get("receivedDateTime") if isinstance(message.get("receivedDateTime"), str) else None,
        snippet=message.get("bodyPreview") if isinstance(message.get("bodyPreview"), str) else "",
        body_text=body_text,
        body_html=body_html,
        has_attachments=bool(message.get("hasAttachments")),
        attachments=attachments,
        is_read=bool(message.get("isRead", True)),
        web_link=message.get("webLink") if isinstance(message.get("webLink"), str) else None,
    )


def _filter_expression(sender: str | None, newer_than_days: int | None) -> str | None:
    filters: list[str] = []
    if newer_than_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=max(0, newer_than_days))
        filters.append(f"receivedDateTime ge {cutoff.isoformat().replace('+00:00', 'Z')}")
    elif sender:
        filters.append(f"receivedDateTime ge {_PERMISSIVE_RECEIVED_DATETIME_LOWER_BOUND}")
    if sender:
        filters.append(f"from/emailAddress/address eq '{_escape_odata_string(sender)}'")
    return " and ".join(filters) if filters else None


def _subject_matches(message: dict[str, Any], subject_filter: str | None) -> bool:
    if not subject_filter:
        return True
    raw_subject = message.get("subject")
    item_subject = raw_subject if isinstance(raw_subject, str) else ""
    return subject_filter in item_subject.lower()


async def list_folder_messages(
    *,
    access_token: str,
    folder: str = "inbox",
    sender: str | None = None,
    subject: str | None = None,
    newer_than_days: int | None = None,
    max_results: int = 25,
    include_body: bool = True,
    client: httpx.AsyncClient | None = None,
) -> list[EmailMessage]:
    async def _list(client_: httpx.AsyncClient) -> list[EmailMessage]:
        folder_id = await _resolve_folder_id(client_, access_token, folder)
        select_fields = [
            "id",
            "conversationId",
            "subject",
            "from",
            "toRecipients",
            "ccRecipients",
            "receivedDateTime",
            "bodyPreview",
            "hasAttachments",
            "isRead",
            "webLink",
        ]
        if include_body:
            select_fields.append("body")
        max_results_clamped = _clamp_max_results(max_results)
        params: dict[str, Any] = {
            "$top": max_results_clamped,
            "$orderby": "receivedDateTime desc",
            "$select": ",".join(select_fields),
        }
        filter_expression = _filter_expression(sender, newer_than_days)
        if filter_expression:
            params["$filter"] = filter_expression
        subject_filter = subject.lower() if subject else None
        messages: list[EmailMessage] = []
        page_count = 0
        fetched_count = 0
        request_url: str | None = f"{GRAPH_API_BASE}/me/mailFolders/{quote(folder_id, safe='')}/messages"
        request_params: dict[str, Any] | None = params
        truncated = False

        while request_url and len(messages) < max_results_clamped:
            payload = await _get_json(client_, request_url, access_token=access_token, params=request_params)
            page_count += 1
            raw_items = payload.get("value")
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if fetched_count >= _MAX_SUBJECT_FILTER_FETCHED:
                    truncated = True
                    break
                fetched_count += 1
                if not isinstance(item, dict) or not _subject_matches(item, subject_filter):
                    continue
                message_id = item.get("id")
                item_attachments: list[EmailAttachment] = []
                if include_body and bool(item.get("hasAttachments")) and isinstance(message_id, str):
                    item_attachments = await _attachments(client_, access_token=access_token, message_id=message_id)
                message = _message_from_graph(item, item_attachments, include_body=include_body)
                if message:
                    messages.append(message)
                if len(messages) >= max_results_clamped:
                    break
            if not subject_filter or len(messages) >= max_results_clamped or truncated:
                break
            if page_count >= _MAX_SUBJECT_FILTER_PAGES:
                truncated = True
                break
            request_url = _validated_next_link(payload.get("@odata.nextLink"))
            request_params = None
        if truncated:
            LOG.debug(
                "Truncated Outlook subject pagination",
                pages=page_count,
                fetched=fetched_count,
                max_pages=_MAX_SUBJECT_FILTER_PAGES,
                max_fetched=_MAX_SUBJECT_FILTER_FETCHED,
            )
        return messages

    if client is None:
        async with httpx.AsyncClient(timeout=20.0) as owned_client:
            return await _list(owned_client)
    return await _list(client)
