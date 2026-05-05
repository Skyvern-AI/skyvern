"""Tests for skyvern.forge.sdk.services.google_sheets_service.

Uses httpx.MockTransport to fake the Drive v3 + Sheets v4 endpoints so we can
verify parameter encoding, id extraction, response parsing, and error mapping
(including the 403 insufficient_scope -> reconnect_required translation) end
to end without hitting Google.
"""

from typing import Any, Callable
from unittest.mock import patch

import httpx
import pytest

from skyvern.forge.sdk.services import google_sheets_service


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(google_sheets_service.httpx, "AsyncClient", fake_async_client)


def test_extract_spreadsheet_id_from_url() -> None:
    url = "https://docs.google.com/spreadsheets/d/1AbC-XyZ_123/edit#gid=0"
    assert google_sheets_service.extract_spreadsheet_id(url) == "1AbC-XyZ_123"


def test_extract_spreadsheet_id_bare() -> None:
    bare = "1AbCdEfGhIjKlMnOpQrSt_12345"
    assert google_sheets_service.extract_spreadsheet_id(bare) == bare


def test_extract_spreadsheet_id_invalid() -> None:
    with pytest.raises(ValueError):
        google_sheets_service.extract_spreadsheet_id("")
    with pytest.raises(ValueError):
        google_sheets_service.extract_spreadsheet_id("not-a-sheet")


def test_build_drive_q_escapes_user_input() -> None:
    q = google_sheets_service._build_drive_q("Jen's sheet")
    assert "Jen\\'s sheet" in q
    assert "mimeType = 'application/vnd.google-apps.spreadsheet'" in q
    assert "trashed = false" in q


@pytest.mark.asyncio
async def test_list_spreadsheets_sends_expected_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "nextPageToken": "tok-2",
                "files": [
                    {
                        "id": "1xyz",
                        "name": "Leads",
                        "modifiedTime": "2026-04-10T00:00:00Z",
                        "webViewLink": "https://docs.google.com/spreadsheets/d/1xyz",
                    },
                    {"id": "", "name": "skip-empty-id"},
                ],
            },
        )

    _install_transport(monkeypatch, handler)

    paged = await google_sheets_service.list_spreadsheets(
        access_token="at-1",
        query="leads",
        page_token="tok-1",
        page_size=50,
    )

    assert paged.next_page_token == "tok-2"
    assert len(paged.spreadsheets) == 1
    assert paged.spreadsheets[0].id == "1xyz"
    assert paged.spreadsheets[0].name == "Leads"
    assert captured["auth"] == "Bearer at-1"
    assert "pageSize=50" in captured["url"]
    assert "pageToken=tok-1" in captured["url"]
    # Shared-drive support is required so picker results include sheets
    # owned by shared drives, not just the user's My Drive.
    assert "supportsAllDrives=true" in captured["url"]
    assert "includeItemsFromAllDrives=true" in captured["url"]


@pytest.mark.asyncio
async def test_list_spreadsheets_403_insufficient_scope_maps_to_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "Request had insufficient authentication scopes.",
                    "errors": [{"reason": "insufficientPermissions"}],
                }
            },
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc_info:
        await google_sheets_service.list_spreadsheets(access_token="at")

    assert exc_info.value.status == 403
    assert exc_info.value.code == "reconnect_required"


@pytest.mark.asyncio
async def test_get_spreadsheet_tabs_parses_sheet_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v4/spreadsheets/1xyz" in str(request.url)
        return httpx.Response(
            200,
            json={
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}},
                    {"properties": {"sheetId": 123, "title": "Q2", "index": 1}},
                    {"properties": {"title": "no-id-skip"}},
                ]
            },
        )

    _install_transport(monkeypatch, handler)

    tabs = await google_sheets_service.get_spreadsheet_tabs(
        access_token="at",
        spreadsheet_id="https://docs.google.com/spreadsheets/d/1xyz/edit",
    )

    assert [t.title for t in tabs] == ["Sheet1", "Q2"]
    assert [t.sheet_id for t in tabs] == [0, 123]


@pytest.mark.asyncio
async def test_create_spreadsheet_posts_title(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "spreadsheetId": "1newid",
                "properties": {"title": "My Sheet"},
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/1newid",
                "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}],
            },
        )

    _install_transport(monkeypatch, handler)

    created = await google_sheets_service.create_spreadsheet(access_token="at", title="My Sheet")

    assert created.id == "1newid"
    assert created.title == "My Sheet"
    assert created.web_view_link and "1newid" in created.web_view_link
    assert created.first_sheet_name == "Sheet1"
    assert '"My Sheet"' in captured["body"]


@pytest.mark.asyncio
async def test_create_sheet_tab_returns_new_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert ":batchUpdate" in str(request.url)
        return httpx.Response(
            200,
            json={"replies": [{"addSheet": {"properties": {"sheetId": 55, "title": "New Tab", "index": 3}}}]},
        )

    _install_transport(monkeypatch, handler)

    tab = await google_sheets_service.create_sheet_tab(
        access_token="at",
        spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
        title="New Tab",
    )

    assert tab.sheet_id == 55
    assert tab.title == "New Tab"
    assert tab.index == 3


@pytest.mark.asyncio
async def test_api_error_maps_status_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"message": "Requested entity was not found.", "errors": [{"reason": "notFound"}]}},
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc_info:
        await google_sheets_service.get_spreadsheet_tabs(
            access_token="at", spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345"
        )

    assert exc_info.value.status == 404
    assert "not found" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_get_sheet_grid_properties_returns_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 0,
                            "title": "Other",
                            "gridProperties": {"columnCount": 50, "rowCount": 1000},
                        }
                    },
                    {
                        "properties": {
                            "sheetId": 42,
                            "title": "Target",
                            "gridProperties": {"columnCount": 26, "rowCount": 1000},
                        }
                    },
                ]
            },
        )

    _install_transport(monkeypatch, handler)

    grid = await google_sheets_service.get_sheet_grid_properties(
        access_token="at",
        spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
        sheet_title="Target",
    )

    assert grid is not None
    assert grid.sheet_id == 42
    assert grid.title == "Target"
    assert grid.column_count == 26
    assert grid.row_count == 1000


@pytest.mark.asyncio
async def test_get_sheet_grid_properties_by_id_matches_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 7,
                            "title": "Target",
                            "gridProperties": {"columnCount": 26, "rowCount": 1000},
                        }
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handler)

    grid = await google_sheets_service.get_sheet_grid_properties_by_id(
        access_token="at",
        spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
        sheet_id=7,
    )

    assert grid is not None
    assert grid.sheet_id == 7
    assert grid.title == "Target"
    assert grid.column_count == 26


@pytest.mark.asyncio
async def test_get_sheet_grid_properties_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sheets": []})

    _install_transport(monkeypatch, handler)

    grid = await google_sheets_service.get_sheet_grid_properties(
        access_token="at",
        spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
        sheet_title="Missing",
    )

    assert grid is None


def test_build_append_dimension_request_validates_inputs() -> None:
    from skyvern.schemas.google_sheets import build_append_dimension_request

    req = build_append_dimension_request(sheet_id=42, dimension="COLUMNS", length=5)
    assert req == {"appendDimension": {"sheetId": 42, "dimension": "COLUMNS", "length": 5}}

    with pytest.raises(ValueError):
        build_append_dimension_request(sheet_id=42, dimension="DIAGONAL", length=1)
    with pytest.raises(ValueError):
        build_append_dimension_request(sheet_id=42, dimension="ROWS", length=0)


# Ensure patch is not flagged as unused by linters in case of future use
_ = patch
