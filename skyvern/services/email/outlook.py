import asyncio
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import structlog

from skyvern.services.email.types import EmailAttachment, EmailMessage
from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN

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
_MAX_OTP_SEARCH_PAGES = 4
_MAX_OTP_SEARCH_FETCHED = 100
_OTP_EXCLUDED_FOLDER_IDS_STATE_KEY = "otp_excluded_folder_ids"
_OTP_MAILBOX_IDENTITIES_STATE_KEY = "otp_mailbox_identities"
_SAFE_EMAIL_IDENTIFIER = SAFE_EMAIL_ADDRESS_PATTERN
_OTP_KEYWORD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:verification|verify|code|passcode|otp|2fa|one(?:[\s-]+)time|password|"
    r"sign(?:[\s-]+)in|signin|log(?:[\s-]+)in|login|(?:magic|access)(?:[\s-]+)link)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Only EOP-stripped headers are trusted for recipient matching.
_DELIVERY_HEADER_NAMES = {
    "x-ms-exchange-organization-originalenveloperecipient",
    "x-ms-exchange-organization-originalenveloperecipients",
    "x-ms-exchange-organization-originalto",
}


@dataclass(frozen=True)
class OutlookMessageCandidate:
    message_id: str
    content: str
    received_datetime: datetime


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
    prefer: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    response: httpx.Response | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
            if prefer:
                headers["Prefer"] = ", ".join(prefer)
            response = await client.get(
                url,
                params=params,
                headers=headers,
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


async def fetch_primary_account_email(
    *,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    async def _fetch(client_: httpx.AsyncClient) -> str | None:
        payload = await _get_json(
            client_,
            f"{GRAPH_API_BASE}/me",
            access_token=access_token,
            params={"$select": "mail"},
        )
        if not isinstance(payload, dict):
            return None
        value = payload.get("mail")
        if isinstance(value, str):
            candidate = value.strip()
            if _SAFE_EMAIL_IDENTIFIER.fullmatch(candidate):
                return candidate
        return None

    if client is not None:
        return await _fetch(client)
    async with httpx.AsyncClient(timeout=20.0) as owned_client:
        return await _fetch(owned_client)


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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _graph_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _graph_datetime_parameter(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _normalize_email_address(value: str) -> str:
    normalized = value.strip()
    if normalized[:5].casefold() == "smtp:":
        normalized = normalized[5:].strip()
    return normalized.casefold()


def _message_recipient_addresses(message: dict[str, Any]) -> set[str]:
    addresses: set[str] = set()
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        raw_recipients = message.get(field)
        recipients = raw_recipients if isinstance(raw_recipients, list) else []
        addresses.update(
            normalized
            for address in _recipient_addresses(recipients)
            if (normalized := _normalize_email_address(address))
        )

    raw_headers = message.get("internetMessageHeaders")
    headers = raw_headers if isinstance(raw_headers, list) else []
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if not isinstance(name, str) or name.casefold() not in _DELIVERY_HEADER_NAMES or not isinstance(value, str):
            continue
        addresses.update(
            normalized for _, address in getaddresses([value]) if (normalized := _normalize_email_address(address))
        )
    return addresses


def _mailbox_identity_set(payload: dict[str, Any]) -> set[str]:
    raw_values: list[str] = []
    for field in ("mail", "userPrincipalName"):
        value = payload.get(field)
        if isinstance(value, str):
            raw_values.append(value)
    for field in ("proxyAddresses",):
        values = payload.get(field)
        if isinstance(values, list):
            raw_values.extend(value for value in values if isinstance(value, str))
    return {normalized for value in raw_values if (normalized := _normalize_email_address(value))}


async def _load_mailbox_identities(client: httpx.AsyncClient, access_token: str) -> tuple[set[str], bool]:
    try:
        payload = await _get_json(
            client,
            f"{GRAPH_API_BASE}/me",
            access_token=access_token,
            params={"$select": "mail,userPrincipalName,proxyAddresses"},
        )
    except (OutlookAPIError, ValueError) as exc:
        if isinstance(exc, OutlookAPIError) and exc.code == "reconnect_required":
            raise
        LOG.warning(
            "Failed to resolve Outlook mailbox identities for OTP search",
            status=exc.status if isinstance(exc, OutlookAPIError) else None,
            code=exc.code if isinstance(exc, OutlookAPIError) else None,
        )
        return set(), False
    return _mailbox_identity_set(payload), True


async def _load_well_known_folder_id(
    client: httpx.AsyncClient,
    access_token: str,
    folder: str,
) -> tuple[str | None, bool]:
    try:
        payload = await _get_json(
            client,
            f"{GRAPH_API_BASE}/me/mailFolders/{folder}",
            access_token=access_token,
            params={"$select": "id"},
        )
    except (OutlookAPIError, ValueError) as exc:
        if isinstance(exc, OutlookAPIError) and exc.code == "reconnect_required":
            raise
        LOG.warning(
            "Failed to resolve excluded Outlook folder for OTP search",
            folder=folder,
            status=exc.status if isinstance(exc, OutlookAPIError) else None,
            code=exc.code if isinstance(exc, OutlookAPIError) else None,
        )
        return None, False
    folder_id = payload.get("id")
    resolved_folder_id = folder_id if isinstance(folder_id, str) and folder_id else None
    return resolved_folder_id, resolved_folder_id is not None


async def _load_excluded_folder_ids(client: httpx.AsyncClient, access_token: str) -> tuple[set[str], bool]:
    folder_results = await asyncio.gather(
        _load_well_known_folder_id(client, access_token, "junkemail"),
        _load_well_known_folder_id(client, access_token, "deleteditems"),
        _load_well_known_folder_id(client, access_token, "sentitems"),
    )
    return (
        {folder_id for folder_id, _ in folder_results if folder_id},
        all(succeeded for _, succeeded in folder_results),
    )


def _cached_string_set(state: dict, key: str) -> set[str] | None:
    if key not in state:
        return None
    value = state[key]
    if not isinstance(value, (list, set, tuple)):
        return set()
    return {item for item in value if isinstance(item, str)}


async def _otp_search_state(
    client: httpx.AsyncClient,
    access_token: str,
    state: dict,
) -> tuple[set[str], set[str], bool]:
    excluded_folder_ids = _cached_string_set(state, _OTP_EXCLUDED_FOLDER_IDS_STATE_KEY)
    mailbox_identities = _cached_string_set(state, _OTP_MAILBOX_IDENTITIES_STATE_KEY)
    exclusions_resolved = excluded_folder_ids is not None
    if excluded_folder_ids is None:
        excluded_folder_ids, exclusions_resolved = await _load_excluded_folder_ids(client, access_token)
        if exclusions_resolved:
            state[_OTP_EXCLUDED_FOLDER_IDS_STATE_KEY] = excluded_folder_ids
    if mailbox_identities is None:
        mailbox_identities, succeeded = await _load_mailbox_identities(client, access_token)
        if succeeded:
            state[_OTP_MAILBOX_IDENTITIES_STATE_KEY] = mailbox_identities
    return excluded_folder_ids, mailbox_identities, exclusions_resolved


def _otp_scan_metadata(
    message: dict[str, Any],
    *,
    cutoff: datetime,
    excluded_folder_ids: set[str],
) -> tuple[str, datetime] | None:
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    received_datetime = _graph_datetime(message.get("receivedDateTime"))
    if received_datetime is None or received_datetime < cutoff or message.get("isDraft") is True:
        return None
    parent_folder_id = message.get("parentFolderId")
    if isinstance(parent_folder_id, str) and parent_folder_id in excluded_folder_ids:
        return None
    return message_id, received_datetime


def _otp_candidate(
    message: dict[str, Any],
    *,
    message_id: str,
    received_datetime: datetime,
) -> tuple[OutlookMessageCandidate, set[str]] | None:
    raw_subject = message.get("subject")
    subject = raw_subject if isinstance(raw_subject, str) else ""
    raw_preview = message.get("bodyPreview")
    preview = raw_preview if isinstance(raw_preview, str) else ""
    raw_body = message.get("body")
    body = raw_body.get("content") if isinstance(raw_body, dict) else ""
    body_content = body if isinstance(body, str) else ""
    if not _OTP_KEYWORD_PATTERN.search("\n".join((subject, preview, body_content))):
        return None
    content = "\n".join(
        part
        for part in (
            f"Subject: {subject}" if subject else "",
            f"Snippet: {preview}" if preview else "",
            f"Body:\n{body_content}" if body_content else "",
        )
        if part
    ).strip()
    if not content:
        return None
    return (
        OutlookMessageCandidate(
            message_id=message_id,
            content=content,
            received_datetime=received_datetime,
        ),
        _message_recipient_addresses(message),
    )


def _otp_message_has_body_content(message: dict[str, Any]) -> bool:
    body = message.get("body")
    if not isinstance(body, dict):
        return False
    content = body.get("content")
    return isinstance(content, str) and bool(content)


async def _hydrate_otp_message_body(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    message: dict[str, Any],
    prefer: tuple[str, ...],
) -> dict[str, Any] | None:
    if _otp_message_has_body_content(message):
        return message
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        return message
    payload = await _get_json(
        client,
        f"{GRAPH_API_BASE}/me/messages/{quote(message_id, safe='')}",
        access_token=access_token,
        params={"$select": "id,subject,bodyPreview,body,receivedDateTime"},
        prefer=prefer,
    )
    if not isinstance(payload.get("body"), dict):
        return None
    hydrated = dict(message)
    for field in ("subject", "bodyPreview", "body", "receivedDateTime"):
        if field in payload:
            hydrated[field] = payload[field]
    return hydrated


async def search_recent_otp_messages(
    *,
    access_token: str,
    totp_identifier: str,
    created_after: datetime | None = None,
    max_results: int = 10,
    client: httpx.AsyncClient | None = None,
    state: dict | None = None,
    excluded_message_ids: Collection[str] | None = None,
) -> list[OutlookMessageCandidate]:
    identifier = totp_identifier.strip()
    if not _SAFE_EMAIL_IDENTIFIER.fullmatch(identifier):
        return []
    cutoff = _as_utc(created_after) if created_after else datetime.now(UTC) - timedelta(hours=24)
    excluded_ids = set(excluded_message_ids or ())
    max_results_clamped = max(1, min(max_results, 20))
    page_size = max(25, min(50, max_results_clamped * 5))
    params: dict[str, Any] = {
        "$filter": f"receivedDateTime ge {_graph_datetime_parameter(cutoff - timedelta(seconds=1))}",
        "$orderby": "receivedDateTime desc",
        "$select": (
            "id,parentFolderId,isDraft,subject,bodyPreview,receivedDateTime,"
            "toRecipients,ccRecipients,bccRecipients,internetMessageHeaders"
        ),
        "$top": page_size,
    }
    prefer = ('IdType="ImmutableId"', 'outlook.body-content-type="html"')

    async def _search(client_: httpx.AsyncClient) -> list[OutlookMessageCandidate]:
        lookup_state = state if state is not None else {}
        excluded_folder_ids, mailbox_identities, exclusions_resolved = await _otp_search_state(
            client_, access_token, lookup_state
        )
        if not exclusions_resolved:
            # Scanning without exclusions would parse attacker-influenceable junk mail.
            LOG.warning("Skipping Outlook OTP scan; folder exclusions unresolved")
            return []
        normalized_identifier = _normalize_email_address(identifier)
        strong_candidates: list[OutlookMessageCandidate] = []
        fallback_candidates: list[OutlookMessageCandidate] = []
        request_url: str | None = f"{GRAPH_API_BASE}/me/messages"
        request_params: dict[str, Any] | None = params
        page_count = 0
        fetched_count = 0
        truncated = False

        while request_url and len(strong_candidates) < max_results_clamped:
            payload = await _get_json(
                client_,
                request_url,
                access_token=access_token,
                params=request_params,
                prefer=prefer,
            )
            page_count += 1
            raw_items = payload.get("value")
            items = raw_items if isinstance(raw_items, list) else []
            for index, item in enumerate(items):
                if fetched_count >= _MAX_OTP_SEARCH_FETCHED:
                    truncated = True
                    break
                fetched_count += 1
                if not isinstance(item, dict):
                    continue
                scan_metadata = _otp_scan_metadata(
                    item,
                    cutoff=cutoff,
                    excluded_folder_ids=excluded_folder_ids,
                )
                if scan_metadata is None:
                    continue
                message_id, received_datetime = scan_metadata
                if message_id in excluded_ids:
                    continue
                observable_recipients = _message_recipient_addresses(item)
                could_match = normalized_identifier in observable_recipients or (
                    normalized_identifier in mailbox_identities and not observable_recipients
                )
                if could_match and not _otp_message_has_body_content(item):
                    try:
                        hydrated_item = await _hydrate_otp_message_body(
                            client_,
                            access_token=access_token,
                            message=item,
                            prefer=prefer,
                        )
                    except (OutlookAPIError, ValueError) as exc:
                        if isinstance(exc, OutlookAPIError) and exc.code == "reconnect_required":
                            raise
                        LOG.warning(
                            "Failed to hydrate Outlook OTP message body",
                            status=exc.status if isinstance(exc, OutlookAPIError) else None,
                            code=exc.code if isinstance(exc, OutlookAPIError) else None,
                        )
                        # Skipping avoids burning the candidate's one-shot seen-key on a bodyless parse.
                        continue
                    if hydrated_item is None:
                        LOG.warning("Failed to hydrate Outlook OTP message body")
                        # Skipping avoids burning the candidate's one-shot seen-key on a bodyless parse.
                        continue
                    item = hydrated_item
                candidate_match = _otp_candidate(
                    item,
                    message_id=message_id,
                    received_datetime=received_datetime,
                )
                if candidate_match is None:
                    continue
                candidate, observable_recipients = candidate_match
                if normalized_identifier in observable_recipients:
                    strong_candidates.append(candidate)
                    if len(strong_candidates) >= max_results_clamped:
                        break
                elif normalized_identifier in mailbox_identities and not observable_recipients:
                    fallback_candidates.append(candidate)
                if index + 1 < len(items) and fetched_count >= _MAX_OTP_SEARCH_FETCHED:
                    truncated = True
                    break
            if len(strong_candidates) >= max_results_clamped:
                break
            next_link = _validated_next_link(payload.get("@odata.nextLink"))
            if next_link is None:
                break
            if page_count >= _MAX_OTP_SEARCH_PAGES or fetched_count >= _MAX_OTP_SEARCH_FETCHED:
                truncated = True
                break
            request_url = next_link
            request_params = None

        if truncated:
            LOG.debug(
                "Truncated Outlook OTP pagination",
                pages=page_count,
                fetched=fetched_count,
                max_pages=_MAX_OTP_SEARCH_PAGES,
                max_fetched=_MAX_OTP_SEARCH_FETCHED,
            )
        strong_candidates.sort(key=lambda item: item.received_datetime, reverse=True)
        fallback_candidates.sort(key=lambda item: item.received_datetime, reverse=True)
        return (strong_candidates + fallback_candidates)[:max_results_clamped]

    if client is None:
        async with httpx.AsyncClient(timeout=20.0) as owned_client:
            return await _search(owned_client)
    return await _search(client)


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
