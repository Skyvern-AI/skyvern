from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service, google_sheets_service, microsoft_oauth_service
from skyvern.schemas.google_sheets import (
    GoogleSheetsAPIError,
    build_a1,
    column_index_to_letter,
    extract_sheet_gid,
    extract_spreadsheet_id,
    is_plain_cell_range,
)

from .credentials import _eligible_sheets_connections

LOG = structlog.get_logger()

_SHEET_READ_TIMEOUT_SECONDS = 5.0
_SHEET_MAX_ROWS = 50
_SHEET_MAX_COLUMNS = 26
_SHEET_MAX_CELL_CHARS = 200
_SHEET_MAX_TABS = 50
_SHEET_MAX_VALUE_CHARS = 12_000
_SHEET_ERROR_MESSAGE_MAX_CHARS = 200
_SHEET_METADATA_FIELDS = "properties(title),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
_SHEET_VALUES_FIELDS = "sheets(data(rowData(values(formattedValue))))"

SheetConnectionStatus = Literal["opened", "no_access", "token_unavailable", "error"]


def _serialize(
    credential: GoogleOAuthCredentialBase | MicrosoftOAuthCredentialBase,
    provider: str,
) -> dict[str, Any]:
    # Allowlist rather than a model dump: both source models are token-free today,
    # so a dump would start leaking the day either one gains a token field.
    result: dict[str, Any] = {
        "connection_id": credential.id,
        "provider": provider,
        "name": credential.credential_name,
        "state": credential.state,
        "scopes_granted": list(credential.scopes_granted),
    }
    if credential.email_address:
        result["email_address"] = credential.email_address
    return result


async def _list_integrations(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    # Each provider is read through whatever its own Integrations page shows, so a connection the
    # user can see is never reported as absent. Google surfaces expired grants as state=error;
    # Microsoft has no such listing, so active is all it can offer.
    google_credentials = await google_oauth_service.get_visible_credentials_for_org(ctx.organization_id)
    microsoft_credentials = await microsoft_oauth_service.get_credentials_for_org(ctx.organization_id)
    integrations = [_serialize(credential, "google") for credential in google_credentials] + [
        _serialize(credential, "microsoft") for credential in microsoft_credentials
    ]
    return {
        "ok": True,
        "data": {
            "integrations": integrations,
            "count": len(integrations),
        },
    }


@dataclass(frozen=True)
class _SheetOpenAttempt:
    status: SheetConnectionStatus
    reason: str | None = None
    access_token: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class _SheetTab:
    title: str
    gid: int
    row_count: int
    column_count: int


def _sheet_error_reason(error: GoogleSheetsAPIError, access_token: str) -> str:
    code = f" {error.code}" if error.code else ""
    message = error.message.replace(access_token, "[token]")[:_SHEET_ERROR_MESSAGE_MAX_CHARS]
    return f"{error.status}{code}: {message}"


async def _open_sheet_through_connection(
    organization_id: str, connection_id: str, spreadsheet_id: str
) -> _SheetOpenAttempt:
    access_token = await app.AGENT_FUNCTION.get_google_sheets_credentials(organization_id, connection_id)
    if access_token is None:
        return _SheetOpenAttempt("token_unavailable")
    try:
        metadata = await google_sheets_service.values_get(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            ranges="",
            fields=_SHEET_METADATA_FIELDS,
        )
    except GoogleSheetsAPIError as error:
        reason = _sheet_error_reason(error, access_token)
        # A 404 stays per connection: a sheet one account cannot find may still open through another.
        if error.status == 404 or (error.status == 403 and error.code != google_sheets_service._RECONNECT_SCOPE_CODE):
            return _SheetOpenAttempt("no_access", reason=reason)
        return _SheetOpenAttempt("error", reason=reason)
    return _SheetOpenAttempt("opened", access_token=access_token, metadata=metadata)


async def _bounded_sheet_open(organization_id: str, connection_id: str, spreadsheet_id: str) -> _SheetOpenAttempt:
    try:
        return await asyncio.wait_for(
            _open_sheet_through_connection(organization_id, connection_id, spreadsheet_id),
            timeout=_SHEET_READ_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _SheetOpenAttempt("error", reason="timeout")
    except Exception as error:
        LOG.warning(
            "copilot_read_google_sheet_open_failed",
            organization_id=organization_id,
            connection_id=connection_id,
            exc_info=True,
        )
        return _SheetOpenAttempt("error", reason=type(error).__name__)


def _sheet_tabs(metadata: dict[str, Any]) -> list[_SheetTab]:
    tabs: list[_SheetTab] = []
    for sheet in metadata.get("sheets") or []:
        properties = sheet.get("properties") or {}
        if properties.get("sheetId") is None or properties.get("title") is None:
            continue
        grid = properties.get("gridProperties") or {}
        tabs.append(
            _SheetTab(
                title=str(properties["title"]),
                gid=int(properties["sheetId"]),
                row_count=int(grid.get("rowCount") or 0),
                column_count=int(grid.get("columnCount") or 0),
            )
        )
    return tabs


def _first_rows_range(tab: _SheetTab) -> str | None:
    if not tab.row_count or not tab.column_count:
        return None
    last_column = column_index_to_letter(min(tab.column_count, _SHEET_MAX_COLUMNS) - 1)
    return build_a1(tab.title, f"A1:{last_column}{min(tab.row_count, _SHEET_MAX_ROWS)}")


def _bounded_sheet_values(payload: dict[str, Any]) -> tuple[list[list[str]], bool]:
    # Picked by its data, so a response that also lists other tabs cannot read as an empty range.
    grids: list[dict[str, Any]] = next(
        (sheet["data"] for sheet in payload.get("sheets") or [] if sheet.get("data")), []
    )
    row_data = (grids[0].get("rowData") or []) if grids else []
    rows: list[list[str]] = []
    truncated = len(row_data) > _SHEET_MAX_ROWS
    used_chars = 0
    for row in row_data[:_SHEET_MAX_ROWS]:
        cells = row.get("values") or []
        truncated = truncated or len(cells) > _SHEET_MAX_COLUMNS
        values: list[str] = []
        for cell in cells[:_SHEET_MAX_COLUMNS]:
            text = str(cell.get("formattedValue") or "")
            truncated = truncated or len(text) > _SHEET_MAX_CELL_CHARS
            text = text[:_SHEET_MAX_CELL_CHARS]
            # Counted as serialized: non-ASCII text is escaped to several characters in the tool result.
            used_chars += len(json.dumps(text))
            if used_chars > _SHEET_MAX_VALUE_CHARS:
                return ([*rows, values] if values else rows), True
            values.append(text)
        rows.append(values)
    return rows, truncated


async def _read_sheet_values(
    access_token: str, spreadsheet_id: str, tabs: list[_SheetTab], gid: int | None, requested_range: str | None
) -> dict[str, Any]:
    gid_tab = next((tab for tab in tabs if tab.gid == gid), None)
    plain_cells = requested_range is not None and is_plain_cell_range(requested_range.upper())
    if gid is not None and gid_tab is None and (plain_cells or not requested_range):
        return {"values_error": f"gid {gid} in the URL matches no tab of this spreadsheet."}
    first_rows_tab: _SheetTab | None = None
    if requested_range:
        # A plain cell range with no tab prefix reads the first tab, so the tab the URL names is made explicit.
        on_gid_tab = build_a1(gid_tab.title, requested_range) if gid_tab is not None and plain_cells else None
        values_range: str | None = on_gid_tab or requested_range
    else:
        first_rows_tab = gid_tab if gid_tab is not None else (tabs[0] if tabs else None)
        values_range = _first_rows_range(first_rows_tab) if first_rows_tab is not None else None
    if values_range is None:
        return {}
    try:
        payload = await asyncio.wait_for(
            google_sheets_service.values_get(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                ranges=values_range,
                fields=_SHEET_VALUES_FIELDS,
            ),
            timeout=_SHEET_READ_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return {"range": values_range, "values_error": "timeout"}
    except GoogleSheetsAPIError as error:
        return {"range": values_range, "values_error": _sheet_error_reason(error, access_token)}
    except ValueError as error:
        return {"range": values_range, "values_error": type(error).__name__}
    values, truncated = _bounded_sheet_values(payload)
    if first_rows_tab is not None:
        truncated = (
            truncated
            or (len(values) == _SHEET_MAX_ROWS and first_rows_tab.row_count > _SHEET_MAX_ROWS)
            or (
                first_rows_tab.column_count > _SHEET_MAX_COLUMNS
                and any(len(row) == _SHEET_MAX_COLUMNS for row in values)
            )
        )
    return {"range": values_range, "values": values, "truncated": truncated}


async def _read_google_sheet(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    spreadsheet_url = str(params.get("spreadsheet_url") or "")
    try:
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    except ValueError:
        return {"ok": False, "error": "spreadsheet_url is not a Google Sheets URL."}
    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if policy is None or spreadsheet_id not in policy.user_provided_spreadsheet_ids:
        return {
            "ok": False,
            "error": "This spreadsheet is not in a URL the user wrote in this chat, so it was not read.",
        }

    eligible = _eligible_sheets_connections(
        await google_oauth_service.get_visible_credentials_for_org(ctx.organization_id)
    )
    requested_connection_id = params.get("connection_id")
    connections: list[dict[str, Any]] = []
    if requested_connection_id:
        if all(connection.id != requested_connection_id for connection in eligible):
            connections.append({"connection_id": str(requested_connection_id), "status": "not_eligible"})
        eligible = [connection for connection in eligible if connection.id == requested_connection_id]

    attempts = await asyncio.gather(
        *(_bounded_sheet_open(ctx.organization_id, connection.id, spreadsheet_id) for connection in eligible)
    )
    for connection, attempt in zip(eligible, attempts):
        row: dict[str, Any] = {
            "connection_id": connection.id,
            "name": connection.credential_name,
            "status": attempt.status,
        }
        if connection.email_address:
            row["email_address"] = connection.email_address
        if attempt.reason:
            row["reason"] = attempt.reason
        connections.append(row)

    data: dict[str, Any] = {"spreadsheet_id": spreadsheet_id, "connections": connections}
    opened = next(
        ((connection, attempt) for connection, attempt in zip(eligible, attempts) if attempt.status == "opened"),
        None,
    )
    access_token = opened[1].access_token if opened is not None else None
    if opened is not None and access_token is not None:
        connection, attempt = opened
        metadata = attempt.metadata or {}
        tabs = _sheet_tabs(metadata)
        title = (metadata.get("properties") or {}).get("title")
        data["title"] = str(title)[:_SHEET_MAX_CELL_CHARS] if title is not None else None
        data["tabs"] = [
            {"title": tab.title, "gid": tab.gid, "row_count": tab.row_count, "column_count": tab.column_count}
            for tab in tabs[:_SHEET_MAX_TABS]
        ]
        if len(tabs) > _SHEET_MAX_TABS:
            data["tabs_total"] = len(tabs)
        data["read_through"] = connection.id
        requested_range = params.get("range")
        data.update(
            await _read_sheet_values(
                access_token,
                spreadsheet_id,
                tabs,
                extract_sheet_gid(spreadsheet_url),
                str(requested_range) if requested_range else None,
            )
        )
    scrubbed: dict[str, Any] = scrub_secrets_from_structure(ctx, data)
    return {"ok": True, "data": scrubbed}
