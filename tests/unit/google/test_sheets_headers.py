"""Tests for skyvern.forge.sdk.services.google_sheets_service.get_sheet_headers and the
GET /spreadsheets/{id}/sheets/{title}/headers route.

Follows the house pattern: httpx.MockTransport installed via monkeypatch.
"""

from typing import Any, Callable

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


@pytest.mark.asyncio
async def test_get_sheet_headers_returns_row_one_keyed_by_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v4/spreadsheets/1abcdefghijklmnopqrst_12345/values/%27Sheet1%27%21A1%3AZZZ1" in str(request.url)
        return httpx.Response(
            200,
            json={"range": "Sheet1!A1:ZZZ1", "values": [["Name", "Email", "Date"]]},
        )

    _install_transport(monkeypatch, handler)

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"range": "Tab!A1:ZZZ1"})

    _install_transport(monkeypatch, handler)

    headers = await google_sheets_service.get_sheet_headers(
        access_token="tok",
        spreadsheet_id="1abcdefghijklmnopqrst_12345",
        sheet_title="Tab",
    )

    assert headers == []


@pytest.mark.asyncio
async def test_get_sheet_headers_skips_blank_header_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": [["Name", "", "Date"]]})

    _install_transport(monkeypatch, handler)

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
async def test_get_sheet_headers_translates_403_insufficient_scope(
    monkeypatch: pytest.MonkeyPatch,
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

    _install_transport(monkeypatch, handler)

    with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc_info:
        await google_sheets_service.get_sheet_headers(
            access_token="tok",
            spreadsheet_id="1abcdefghijklmnopqrst_12345",
            sheet_title="S",
        )
    assert exc_info.value.status == 403
    assert exc_info.value.code == "reconnect_required"


@pytest.mark.asyncio
async def test_get_sheet_headers_quotes_titles_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"values": [["Name"]]})

    _install_transport(monkeypatch, handler)

    await google_sheets_service.get_sheet_headers(
        access_token="tok",
        spreadsheet_id="1abcdefghijklmnopqrst_12345",
        sheet_title="Q1 Leads",
    )
    # Quoted 'Q1 Leads' URL-encodes to %27Q1%20Leads%27 (or %27Q1+Leads%27) + !A1:ZZZ1
    assert "%27Q1" in captured["url"] and "Leads%27%21A1%3AZZZ1" in captured["url"]


@pytest.mark.asyncio
async def test_get_sheet_headers_generates_letters_beyond_Z(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-check the column-index-to-letter conversion wraps past Z."""
    row = ["h" + str(i) for i in range(28)]  # AA and AB columns
    expected_letters = [chr(ord("A") + i) for i in range(26)] + ["AA", "AB"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": [row]})

    _install_transport(monkeypatch, handler)

    headers = await google_sheets_service.get_sheet_headers(
        access_token="tok",
        spreadsheet_id="1abcdefghijklmnopqrst_12345",
        sheet_title="S",
    )

    assert [h.letter for h in headers] == expected_letters
