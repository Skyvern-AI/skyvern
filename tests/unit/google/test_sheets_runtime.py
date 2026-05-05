"""Runtime-side Sheets verbs (values.get / append / update, batchUpdate).

These are the methods the workflow runtime calls via app.AGENT_FUNCTION; the
picker UX already covered in test_sheets.py.
"""

from typing import Any, Callable

import httpx
import pytest

from skyvern.forge.sdk.services import google_sheets_service


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(google_sheets_service.httpx, "AsyncClient", fake_async_client)


@pytest.mark.asyncio
async def test_values_get_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "1abc_xyz_1234567890ABCDEF" in request.url.path
        return httpx.Response(200, json={"sheets": [{"properties": {"title": "t"}, "data": [{"rowData": []}]}]})

    _install_transport(monkeypatch, handler)
    payload = await google_sheets_service.values_get(
        access_token="tok",
        spreadsheet_id="1abc_xyz_1234567890ABCDEF",
        ranges="A1:B2",
        fields="sheets(properties(title))",
    )
    assert payload["sheets"][0]["properties"]["title"] == "t"


@pytest.mark.asyncio
async def test_values_append_posts_to_append_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"updates": {"updatedRows": 1}})

    _install_transport(monkeypatch, handler)
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
async def test_batch_update_sends_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":batchUpdate")
        return httpx.Response(200, json={"replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "new"}}}]})

    _install_transport(monkeypatch, handler)
    payload = await google_sheets_service.batch_update(
        access_token="tok",
        spreadsheet_id="1abc_xyz_1234567890ABCDEF",
        requests=[{"addSheet": {"properties": {"title": "new"}}}],
    )
    assert payload["replies"][0]["addSheet"]["properties"]["sheetId"] == 7


@pytest.mark.asyncio
async def test_get_sheet_id_by_title_matches(monkeypatch: pytest.MonkeyPatch) -> None:
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

    _install_transport(monkeypatch, handler)
    sheet_id = await google_sheets_service.get_sheet_id_by_title(
        access_token="tok", spreadsheet_id="1abc_xyz_1234567890ABCDEF", sheet_title="Leads"
    )
    assert sheet_id == 42
