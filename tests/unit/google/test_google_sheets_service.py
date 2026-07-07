"""Tests for skyvern.forge.sdk.services.google_sheets_service."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from skyvern.forge.sdk.services import google_sheets_service

TransportInstaller = Callable[[Callable[[httpx.Request], httpx.Response]], None]


class TestSpreadsheetId:
    def test_extract_spreadsheet_id_from_url(self) -> None:
        url = "https://docs.google.com/spreadsheets/d/1AbC-XyZ_123/edit#gid=0"
        assert google_sheets_service.extract_spreadsheet_id(url) == "1AbC-XyZ_123"

    def test_extract_spreadsheet_id_bare(self) -> None:
        bare = "1AbCdEfGhIjKlMnOpQrSt_12345"
        assert google_sheets_service.extract_spreadsheet_id(bare) == bare

    def test_extract_spreadsheet_id_invalid(self) -> None:
        with pytest.raises(ValueError):
            google_sheets_service.extract_spreadsheet_id("")
        with pytest.raises(ValueError):
            google_sheets_service.extract_spreadsheet_id("not-a-sheet")


class TestDrivePicker:
    def test_build_drive_q_escapes_user_input(self) -> None:
        q = google_sheets_service._build_drive_q("Jen's sheet")
        assert "Jen\\'s sheet" in q
        assert "mimeType = 'application/vnd.google-apps.spreadsheet'" in q
        assert "trashed = false" in q

    @pytest.mark.asyncio
    async def test_list_spreadsheets_sends_expected_params(self, mock_sheets_transport: TransportInstaller) -> None:
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

        mock_sheets_transport(handler)

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
        assert "supportsAllDrives=true" in captured["url"]
        assert "includeItemsFromAllDrives=true" in captured["url"]


class TestReconnectRequired:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["list_spreadsheets", "get_sheet_headers"])
    async def test_403_insufficient_scope_maps_to_reconnect_required(
        self,
        mock_sheets_transport: TransportInstaller,
        operation: str,
    ) -> None:
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

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc_info:
            if operation == "list_spreadsheets":
                await google_sheets_service.list_spreadsheets(access_token="at")
            else:
                await google_sheets_service.get_sheet_headers(
                    access_token="tok",
                    spreadsheet_id="1abcdefghijklmnopqrst_12345",
                    sheet_title="S",
                )

        assert exc_info.value.status == 403
        assert exc_info.value.code == "reconnect_required"


class TestSpreadsheetMetadata:
    @pytest.mark.asyncio
    async def test_get_spreadsheet_tabs_parses_sheet_metadata(self, mock_sheets_transport: TransportInstaller) -> None:
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

        mock_sheets_transport(handler)

        tabs = await google_sheets_service.get_spreadsheet_tabs(
            access_token="at",
            spreadsheet_id="https://docs.google.com/spreadsheets/d/1xyz/edit",
        )

        assert [t.title for t in tabs] == ["Sheet1", "Q2"]
        assert [t.sheet_id for t in tabs] == [0, 123]

    @pytest.mark.asyncio
    async def test_create_spreadsheet_posts_title(self, mock_sheets_transport: TransportInstaller) -> None:
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

        mock_sheets_transport(handler)

        created = await google_sheets_service.create_spreadsheet(access_token="at", title="My Sheet")

        assert created.id == "1newid"
        assert created.title == "My Sheet"
        assert created.web_view_link and "1newid" in created.web_view_link
        assert created.first_sheet_name == "Sheet1"
        assert '"My Sheet"' in captured["body"]

    @pytest.mark.asyncio
    async def test_create_sheet_tab_returns_new_tab(self, mock_sheets_transport: TransportInstaller) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert ":batchUpdate" in str(request.url)
            return httpx.Response(
                200,
                json={"replies": [{"addSheet": {"properties": {"sheetId": 55, "title": "New Tab", "index": 3}}}]},
            )

        mock_sheets_transport(handler)

        tab = await google_sheets_service.create_sheet_tab(
            access_token="at",
            spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
            title="New Tab",
        )

        assert tab.sheet_id == 55
        assert tab.title == "New Tab"
        assert tab.index == 3

    @pytest.mark.asyncio
    async def test_api_error_maps_status_and_message(self, mock_sheets_transport: TransportInstaller) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"message": "Requested entity was not found.", "errors": [{"reason": "notFound"}]}},
            )

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc_info:
            await google_sheets_service.get_spreadsheet_tabs(
                access_token="at", spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345"
            )

        assert exc_info.value.status == 404
        assert "not found" in exc_info.value.message.lower()


class TestHeaders:
    @pytest.mark.asyncio
    async def test_get_sheet_headers_returns_row_one_keyed_by_letter(
        self,
        mock_sheets_transport: TransportInstaller,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/v4/spreadsheets/1abcdefghijklmnopqrst_12345/values/%27Sheet1%27%21A1%3AZZZ1" in str(request.url)
            return httpx.Response(
                200,
                json={"range": "Sheet1!A1:ZZZ1", "values": [["Name", "Email", "Date"]]},
            )

        mock_sheets_transport(handler)

        headers = await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="Sheet1",
        )

        assert headers == [
            google_sheets_service.SheetHeader(letter="A", name="Name"),
            google_sheets_service.SheetHeader(letter="B", name="Email"),
            google_sheets_service.SheetHeader(letter="C", name="Date"),
        ]

    @pytest.mark.asyncio
    async def test_get_sheet_headers_returns_empty_when_no_values(
        self,
        mock_sheets_transport: TransportInstaller,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"range": "Tab!A1:ZZZ1"})

        mock_sheets_transport(handler)

        headers = await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="Tab",
        )

        assert headers == []

    @pytest.mark.asyncio
    async def test_get_sheet_headers_skips_blank_header_cells(
        self,
        mock_sheets_transport: TransportInstaller,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": [["Name", "", "Date"]]})

        mock_sheets_transport(handler)

        headers = await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="S",
        )

        assert headers == [
            google_sheets_service.SheetHeader(letter="A", name="Name"),
            google_sheets_service.SheetHeader(letter="C", name="Date"),
        ]

    @pytest.mark.asyncio
    async def test_get_sheet_headers_quotes_titles_with_spaces(
        self,
        mock_sheets_transport: TransportInstaller,
    ) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"values": [["Name"]]})

        mock_sheets_transport(handler)

        await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="Q1 Leads",
        )

        assert "%27Q1" in captured["url"] and "Leads%27%21A1%3AZZZ1" in captured["url"]

    @pytest.mark.asyncio
    async def test_get_sheet_headers_generates_letters_beyond_Z(
        self,
        mock_sheets_transport: TransportInstaller,
    ) -> None:
        row = ["h" + str(i) for i in range(28)]
        expected_letters = [chr(ord("A") + i) for i in range(26)] + ["AA", "AB"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": [row]})

        mock_sheets_transport(handler)

        headers = await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="S",
        )

        assert [h.letter for h in headers] == expected_letters


class TestGridProperties:
    @pytest.mark.asyncio
    async def test_get_sheet_grid_properties_returns_dimensions(
        self, mock_sheets_transport: TransportInstaller
    ) -> None:
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

        mock_sheets_transport(handler)

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
    async def test_get_sheet_grid_properties_by_id_matches_numeric_id(
        self, mock_sheets_transport: TransportInstaller
    ) -> None:
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

        mock_sheets_transport(handler)

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
    async def test_get_sheet_grid_properties_returns_none_when_missing(
        self, mock_sheets_transport: TransportInstaller
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sheets": []})

        mock_sheets_transport(handler)

        grid = await google_sheets_service.get_sheet_grid_properties(
            access_token="at",
            spreadsheet_id="1AbCdEfGhIjKlMnOpQrSt_12345",
            sheet_title="Missing",
        )

        assert grid is None


class TestRetry:
    @pytest.mark.asyncio
    async def test_values_append_does_not_retry_on_429(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "rate limit"}})

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc:
            await google_sheets_service.values_append(
                access_token="tok",
                spreadsheet_id="1abc_xyz_1234567890ABCDEF",
                range_="Sheet1!A1",
                values=[["a"]],
            )
        assert exc.value.status == 429
        assert calls["n"] == 1
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_values_get_retries_on_429(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "rate limit"}})
            return httpx.Response(200, json={"values": [["a"]]})

        mock_sheets_transport(handler)

        payload = await google_sheets_service.values_get(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            ranges="Sheet1!A1",
        )
        assert payload == {"values": [["a"]]}
        assert calls["n"] == 2
        assert sleeps == [2.0]

    @pytest.mark.asyncio
    async def test_values_get_honors_http_date_retry_after(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        frozen_now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return frozen_now if tz is None else frozen_now.astimezone(tz)

        monkeypatch.setattr(google_sheets_service, "datetime", FrozenDatetime)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "Tue, 21 Apr 2026 12:00:05 GMT"},
                    json={"error": {"message": "rate limit"}},
                )
            return httpx.Response(200, json={"values": [["a"]]})

        mock_sheets_transport(handler)

        payload = await google_sheets_service.values_get(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            ranges="Sheet1!A1",
        )
        assert payload == {"values": [["a"]]}
        assert calls["n"] == 2
        assert sleeps == [5.0]

    @pytest.mark.asyncio
    async def test_values_get_retries_on_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"sheets": []})

        mock_sheets_transport(handler)

        payload = await google_sheets_service.values_get(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            ranges="Sheet1!A1",
        )
        assert payload == {"sheets": []}
        assert calls["n"] == 2
        assert sleeps == [1.0]

    @pytest.mark.asyncio
    async def test_values_append_does_not_replay_post_on_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        calls = {"n": 0}

        async def fake_sleep(delay: float) -> None:
            return None

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("response lost", request=request)

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError):
            await google_sheets_service.values_append(
                access_token="tok",
                spreadsheet_id="1abc_xyz_1234567890ABCDEF",
                range_="Sheet1!A1",
                values=[["a"]],
            )
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_values_append_terminal_transport_error_becomes_api_error(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        async def fake_sleep(delay: float) -> None:
            return None

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc:
            await google_sheets_service.values_append(
                access_token="tok",
                spreadsheet_id="1abc_xyz_1234567890ABCDEF",
                range_="Sheet1!A1",
                values=[["a"]],
            )
        assert exc.value.status == 503
        assert exc.value.code == "upstream_unavailable"

    @pytest.mark.asyncio
    async def test_values_append_gives_up_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller
    ) -> None:
        async def fake_sleep(delay: float) -> None:
            return None

        monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "upstream down"}})

        mock_sheets_transport(handler)

        with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc:
            await google_sheets_service.values_append(
                access_token="tok",
                spreadsheet_id="1abc_xyz_1234567890ABCDEF",
                range_="Sheet1!A1",
                values=[["a"]],
            )
        assert exc.value.status == 503


class TestRuntime:
    @pytest.mark.asyncio
    async def test_values_get_returns_payload(self, mock_sheets_transport: TransportInstaller) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "1abc_xyz_1234567890ABCDEF" in request.url.path
            return httpx.Response(200, json={"sheets": [{"properties": {"title": "t"}, "data": [{"rowData": []}]}]})

        mock_sheets_transport(handler)
        payload = await google_sheets_service.values_get(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            ranges="A1:B2",
            fields="sheets(properties(title))",
        )
        assert payload["sheets"][0]["properties"]["title"] == "t"

    @pytest.mark.asyncio
    async def test_values_append_posts_to_append_endpoint(self, mock_sheets_transport: TransportInstaller) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"updates": {"updatedRows": 1}})

        mock_sheets_transport(handler)
        payload = await google_sheets_service.values_append(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            range_="Sheet1!A1",
            values=[["a", "b"]],
        )
        assert payload["updates"]["updatedRows"] == 1
        assert captured["path"].endswith(":append")
        assert captured["params"]["valueInputOption"] == "USER_ENTERED"

    @pytest.mark.asyncio
    async def test_batch_update_sends_requests(self, mock_sheets_transport: TransportInstaller) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(":batchUpdate")
            return httpx.Response(200, json={"replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "new"}}}]})

        mock_sheets_transport(handler)
        payload = await google_sheets_service.batch_update(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            requests=[{"addSheet": {"properties": {"title": "new"}}}],
        )
        assert payload["replies"][0]["addSheet"]["properties"]["sheetId"] == 7

    @pytest.mark.asyncio
    async def test_get_sheet_id_by_title_matches(self, mock_sheets_transport: TransportInstaller) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {"properties": {"sheetId": 1, "title": "A"}},
                        {"properties": {"sheetId": 42, "title": "Leads"}},
                    ]
                },
            )

        mock_sheets_transport(handler)
        sheet_id = await google_sheets_service.get_sheet_id_by_title(
            access_token="tok", spreadsheet_id="1abc_xyz_1234567890ABCDEF", sheet_title="Leads"
        )
        assert sheet_id == 42


class TestSchemaHelpers:
    def test_build_append_dimension_request_validates_inputs(self) -> None:
        from skyvern.schemas.google_sheets import build_append_dimension_request

        req = build_append_dimension_request(sheet_id=42, dimension="COLUMNS", length=5)
        assert req == {"appendDimension": {"sheetId": 42, "dimension": "COLUMNS", "length": 5}}

        with pytest.raises(ValueError):
            build_append_dimension_request(sheet_id=42, dimension="DIAGONAL", length=1)
        with pytest.raises(ValueError):
            build_append_dimension_request(sheet_id=42, dimension="ROWS", length=0)
