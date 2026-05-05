import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from skyvern.config import settings
from skyvern.schemas.google_sheets import GoogleSheetsAPIError, extract_spreadsheet_id, quote_sheet_name  # noqa: F401

LOG = structlog.get_logger()

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
MIME_SPREADSHEET = "application/vnd.google-apps.spreadsheet"

_RECONNECT_SCOPE_CODE = "reconnect_required"

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_DEFAULT_BACKOFF_SECONDS = 1.0


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        value = retry_after.strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        # RFC 7231 also permits HTTP-date; without this branch we silently fall
        # through to exponential backoff and hammer a rate-limited endpoint.
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delta = (target - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta)
    return _DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))


async def _request_with_retry(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str],
    idempotent: bool = True,
) -> httpx.Response:
    """Issue an HTTP request with retry/backoff. ``idempotent=False`` means
    we never replay a mutation - not on transport/timeout, not on 5xx, not on
    429. Google does not guarantee a 429 (or 5xx) was rejected before the
    write landed, so replaying risks duplicate rows the caller can't see.
    """
    last_response: httpx.Response | None = None
    max_attempts = max(1, settings.GOOGLE_SHEETS_API_MAX_RETRIES)
    async with httpx.AsyncClient(timeout=settings.GOOGLE_SHEETS_API_TIMEOUT_SECONDS) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(method, url, params=params, json=json_body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # Non-idempotent + transport failure: ambiguous (request may have landed),
                # so surface immediately instead of replaying the mutation.
                if not idempotent or attempt == max_attempts:
                    raise GoogleSheetsAPIError(
                        status=503,
                        code="upstream_unavailable",
                        message=f"Google Sheets transport failure: {exc}",
                    ) from exc
                await asyncio.sleep(_compute_backoff(attempt, None))
                continue
            if response.status_code not in _RETRYABLE_STATUSES:
                return response
            if not idempotent:
                return response
            last_response = response
            if attempt == max_attempts:
                break
            await asyncio.sleep(_compute_backoff(attempt, response.headers.get("Retry-After")))
    if last_response is None:
        raise RuntimeError("retry loop exited without a response")
    return last_response


@dataclass(frozen=True)
class SpreadsheetSummary:
    id: str
    name: str
    modified_time: str | None = None
    web_view_link: str | None = None


@dataclass(frozen=True)
class PagedSpreadsheets:
    spreadsheets: list[SpreadsheetSummary] = field(default_factory=list)
    next_page_token: str | None = None


@dataclass(frozen=True)
class SheetTab:
    sheet_id: int
    title: str
    index: int | None = None


@dataclass(frozen=True)
class CreatedSpreadsheet:
    id: str
    title: str
    web_view_link: str | None = None
    first_sheet_name: str | None = None


@dataclass(frozen=True)
class SheetHeader:
    letter: str
    name: str


@dataclass(frozen=True)
class SheetGridProperties:
    sheet_id: int
    title: str
    column_count: int
    row_count: int


def _escape_drive_q(value: str) -> str:
    """Escape user input for a Drive v3 q= filter (backslash + single quote)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _build_drive_q(search_query: str | None) -> str:
    terms = [f"mimeType = '{MIME_SPREADSHEET}'", "trashed = false"]
    if search_query:
        terms.append(f"name contains '{_escape_drive_q(search_query)}'")
    return " and ".join(terms)


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    payload: dict[str, Any] = {}
    try:
        payload = response.json() or {}
    except ValueError:
        pass
    err = payload.get("error") if isinstance(payload, dict) else None
    message = "Google API error"
    code: str | None = None
    if isinstance(err, dict):
        message = err.get("message") or message
        details = err.get("errors")
        if isinstance(details, list) and details:
            reason = details[0].get("reason") if isinstance(details[0], dict) else None
            if reason:
                code = reason
        # Only route to reconnect when Google reports a scope/permissions error on the grant;
        # file-level 403s (e.g. `insufficientFilePermissions`) cannot be resolved by reconnecting
        # so they must keep their original code and surface as a plain 403.
        if status == 403 and code in {"insufficientPermissions", "insufficientScopes"}:
            code = _RECONNECT_SCOPE_CODE
    else:
        message = response.text[:500] or message
    raise GoogleSheetsAPIError(status=status, code=code, message=message)


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


async def list_spreadsheets(
    access_token: str,
    query: str | None = None,
    page_token: str | None = None,
    page_size: int = 25,
) -> PagedSpreadsheets:
    """List spreadsheets via Drive v3 files.list, filtered to Google Sheets MIME."""
    params: dict[str, Any] = {
        "q": _build_drive_q(query),
        "pageSize": max(1, min(100, page_size)),
        "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink)",
        "orderBy": "modifiedTime desc",
        "spaces": "drive",
        # Include spreadsheets that live in shared drives; without these, picker
        # results omit any sheet not owned by the user, which breaks orgs that
        # standardize on shared drives.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token

    response = await _request_with_retry(
        "GET",
        f"{DRIVE_API_BASE}/files",
        params=params,
        headers=_auth_headers(access_token),
    )
    _raise_for_error(response)
    payload = response.json()
    items = payload.get("files") or []
    return PagedSpreadsheets(
        spreadsheets=[
            SpreadsheetSummary(
                id=item["id"],
                name=item.get("name") or item["id"],
                modified_time=item.get("modifiedTime"),
                web_view_link=item.get("webViewLink"),
            )
            for item in items
            if item.get("id")
        ],
        next_page_token=payload.get("nextPageToken"),
    )


async def get_spreadsheet_tabs(access_token: str, spreadsheet_id: str) -> list[SheetTab]:
    """Fetch the tab (sheet) metadata for a spreadsheet via Sheets v4."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    params = {"fields": "sheets(properties(sheetId,title,index))"}
    response = await _request_with_retry(
        "GET",
        f"{SHEETS_API_BASE}/{spreadsheet_id}",
        params=params,
        headers=_auth_headers(access_token),
    )
    _raise_for_error(response)
    payload = response.json()
    tabs: list[SheetTab] = []
    for sheet in payload.get("sheets") or []:
        props = sheet.get("properties") or {}
        sheet_id = props.get("sheetId")
        title = props.get("title")
        if sheet_id is None or title is None:
            continue
        tabs.append(SheetTab(sheet_id=int(sheet_id), title=title, index=props.get("index")))
    return tabs


async def get_spreadsheet_summary(access_token: str, spreadsheet_id: str) -> SpreadsheetSummary:
    """Fetch a single spreadsheet's Drive metadata by id, for block rehydration on reload."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    params = {
        "fields": "id,name,modifiedTime,webViewLink",
        # Mirror list_spreadsheets so sheets in shared drives resolve.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    response = await _request_with_retry(
        "GET",
        f"{DRIVE_API_BASE}/files/{quote(spreadsheet_id, safe='')}",
        params=params,
        headers=_auth_headers(access_token),
    )
    _raise_for_error(response)
    payload = response.json()
    file_id = payload.get("id")
    if not file_id:
        raise GoogleSheetsAPIError(
            status=500,
            code="malformed_response",
            message="Drive response missing file id",
        )
    return SpreadsheetSummary(
        id=file_id,
        name=payload.get("name") or file_id,
        modified_time=payload.get("modifiedTime"),
        web_view_link=payload.get("webViewLink"),
    )


async def create_spreadsheet(access_token: str, title: str) -> CreatedSpreadsheet:
    """Create a new spreadsheet via Sheets v4 spreadsheets.create."""
    body = {"properties": {"title": title or "Untitled spreadsheet"}}
    response = await _request_with_retry(
        "POST",
        SHEETS_API_BASE,
        json_body=body,
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        idempotent=False,
    )
    _raise_for_error(response)
    payload = response.json()
    spreadsheet_id = payload.get("spreadsheetId")
    if not spreadsheet_id:
        raise GoogleSheetsAPIError(
            status=500,
            code="malformed_response",
            message="Sheets API response missing spreadsheetId",
        )
    props = payload.get("properties") or {}
    sheets = payload.get("sheets") or []
    first_sheet_name: str | None = None
    if sheets:
        first_sheet_name = (sheets[0].get("properties") or {}).get("title")
    return CreatedSpreadsheet(
        id=spreadsheet_id,
        title=props.get("title") or title,
        web_view_link=payload.get("spreadsheetUrl"),
        first_sheet_name=first_sheet_name,
    )


async def create_sheet_tab(access_token: str, spreadsheet_id: str, title: str) -> SheetTab:
    """Append a new tab to a spreadsheet via batchUpdate addSheet."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {"title": title},
                }
            }
        ]
    }
    response = await _request_with_retry(
        "POST",
        f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
        json_body=body,
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        idempotent=False,
    )
    _raise_for_error(response)
    payload = response.json()
    replies = payload.get("replies") or []
    if not replies:
        raise GoogleSheetsAPIError(status=500, code="malformed_response", message="batchUpdate returned no replies")
    props = (replies[0].get("addSheet") or {}).get("properties") or {}
    sheet_id = props.get("sheetId")
    out_title = props.get("title")
    if sheet_id is None or out_title is None:
        raise GoogleSheetsAPIError(status=500, code="malformed_response", message="addSheet reply missing properties")
    return SheetTab(sheet_id=int(sheet_id), title=out_title, index=props.get("index"))


def _column_index_to_letter(index: int) -> str:
    # 0 -> "A", 25 -> "Z", 26 -> "AA"
    letters: list[str] = []
    n = index
    while True:
        letters.append(chr(ord("A") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(letters))


async def values_get(
    *,
    access_token: str,
    spreadsheet_id: str,
    ranges: str,
    fields: str | None = None,
) -> dict[str, Any]:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    params: dict[str, Any] = {}
    # Sheets API rejects ranges="" as an invalid A1 range; omit when callers only want metadata.
    if ranges:
        params["ranges"] = ranges
    if fields:
        params["fields"] = fields
    url = f"{SHEETS_API_BASE}/{spreadsheet_id}"
    response = await _request_with_retry("GET", url, params=params, headers=_auth_headers(access_token))
    _raise_for_error(response)
    return response.json() or {}


async def values_append(
    *,
    access_token: str,
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
    insert_data_option: str = "INSERT_ROWS",
) -> dict[str, Any]:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{quote(range_, safe='')}:append"
    params = {"valueInputOption": value_input_option, "insertDataOption": insert_data_option}
    body = {"range": range_, "majorDimension": "ROWS", "values": values}
    response = await _request_with_retry(
        "POST",
        url,
        params=params,
        json_body=body,
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        idempotent=False,
    )
    _raise_for_error(response)
    return response.json() or {}


async def values_update(
    *,
    access_token: str,
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> dict[str, Any]:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{quote(range_, safe='')}"
    params = {"valueInputOption": value_input_option}
    body = {"range": range_, "majorDimension": "ROWS", "values": values}
    response = await _request_with_retry(
        "PUT",
        url,
        params=params,
        json_body=body,
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
    )
    _raise_for_error(response)
    return response.json() or {}


async def batch_update(
    *,
    access_token: str,
    spreadsheet_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    url = f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate"
    response = await _request_with_retry(
        "POST",
        url,
        json_body={"requests": requests},
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        idempotent=False,
    )
    _raise_for_error(response)
    return response.json() or {}


async def get_sheet_id_by_title(
    *,
    access_token: str,
    spreadsheet_id: str,
    sheet_title: str,
) -> int | None:
    payload = await values_get(
        access_token=access_token,
        spreadsheet_id=spreadsheet_id,
        ranges="",
        fields="sheets(properties(sheetId,title))",
    )
    for sheet in payload.get("sheets") or []:
        properties = sheet.get("properties") or {}
        if str(properties.get("title")) == sheet_title:
            value = properties.get("sheetId")
            if isinstance(value, int):
                return value
    return None


def _grid_props_from_sheet(sheet: dict[str, Any]) -> SheetGridProperties | None:
    props = sheet.get("properties") or {}
    sheet_id = props.get("sheetId")
    title = props.get("title")
    grid = props.get("gridProperties") or {}
    column_count = grid.get("columnCount")
    row_count = grid.get("rowCount")
    if (
        not isinstance(sheet_id, int)
        or not isinstance(title, str)
        or not isinstance(column_count, int)
        or not isinstance(row_count, int)
    ):
        return None
    return SheetGridProperties(
        sheet_id=int(sheet_id),
        title=title,
        column_count=int(column_count),
        row_count=int(row_count),
    )


async def get_sheet_grid_properties(
    *,
    access_token: str,
    spreadsheet_id: str,
    sheet_title: str,
) -> SheetGridProperties | None:
    """Return the named tab's grid dimensions, or None if missing or malformed."""
    payload = await values_get(
        access_token=access_token,
        spreadsheet_id=spreadsheet_id,
        ranges="",
        fields="sheets(properties(sheetId,title,gridProperties(columnCount,rowCount)))",
    )
    for sheet in payload.get("sheets") or []:
        if str((sheet.get("properties") or {}).get("title")) != sheet_title:
            continue
        return _grid_props_from_sheet(sheet)
    return None


async def get_sheet_grid_properties_by_id(
    *,
    access_token: str,
    spreadsheet_id: str,
    sheet_id: int,
) -> SheetGridProperties | None:
    """Return the tab's grid dimensions matched by numeric sheetId, or None."""
    payload = await values_get(
        access_token=access_token,
        spreadsheet_id=spreadsheet_id,
        ranges="",
        fields="sheets(properties(sheetId,title,gridProperties(columnCount,rowCount)))",
    )
    for sheet in payload.get("sheets") or []:
        if (sheet.get("properties") or {}).get("sheetId") != sheet_id:
            continue
        return _grid_props_from_sheet(sheet)
    return None


async def get_sheet_headers(
    *,
    access_token: str,
    spreadsheet_id: str,
    sheet_title: str,
) -> list[SheetHeader]:
    """Return the non-blank cells of row 1 of ``sheet_title`` keyed by A1 column letter."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
    a1 = f"{quote_sheet_name(sheet_title)}!A1:ZZZ1"
    encoded = quote(a1, safe="")
    url = f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded}"
    response = await _request_with_retry("GET", url, headers=_auth_headers(access_token))
    _raise_for_error(response)
    payload = response.json() or {}
    values = payload.get("values") or []
    row = values[0] if values else []
    out: list[SheetHeader] = []
    for index, cell in enumerate(row):
        name = str(cell).strip() if cell is not None else ""
        if not name:
            continue
        out.append(SheetHeader(letter=_column_index_to_letter(index), name=name))
    return out
