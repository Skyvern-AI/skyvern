"""Verify the Sheets client retries 429 + 5xx with exponential backoff.

Uses httpx.MockTransport to emit a 429 with Retry-After, then a 200. The
helper must honor Retry-After, backoff, and eventually succeed.
"""

from datetime import datetime, timezone
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
async def test_values_append_does_not_retry_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST mutations must not retry on 429 either: Google does not guarantee
    the quota check rejected the request before the write landed."""
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "rate limit"}})

    _install_transport(monkeypatch, handler)

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
async def test_values_get_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET is idempotent so 429 still retries with Retry-After backoff."""
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

    _install_transport(monkeypatch, handler)

    payload = await google_sheets_service.values_get(
        access_token="tok",
        spreadsheet_id="1abc_xyz_1234567890ABCDEF",
        ranges="Sheet1!A1",
    )
    assert payload == {"values": [["a"]]}
    assert calls["n"] == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_values_get_honors_http_date_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 7231 allows Retry-After as an HTTP-date; we must sleep until that instant
    instead of falling through to exponential backoff."""
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

    frozen_now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr(google_sheets_service, "datetime", _FrozenDatetime)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Tue, 21 Apr 2026 12:00:05 GMT"},
                json={"error": {"message": "rate limit"}},
            )
        return httpx.Response(200, json={"values": [["a"]]})

    _install_transport(monkeypatch, handler)

    payload = await google_sheets_service.values_get(
        access_token="tok",
        spreadsheet_id="1abc_xyz_1234567890ABCDEF",
        ranges="Sheet1!A1",
    )
    assert payload == {"values": [["a"]]}
    assert calls["n"] == 2
    assert sleeps == [5.0]


@pytest.mark.asyncio
async def test_values_get_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET is idempotent so transient transport failures retry with backoff."""
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

    _install_transport(monkeypatch, handler)

    payload = await google_sheets_service.values_get(
        access_token="tok",
        spreadsheet_id="1abc_xyz_1234567890ABCDEF",
        ranges="Sheet1!A1",
    )
    assert payload == {"sheets": []}
    assert calls["n"] == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_values_append_does_not_replay_post_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST mutations must not replay on transport failures: Google may have
    processed the write before the response was lost, and a retry would
    duplicate rows."""
    calls = {"n": 0}

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("response lost", request=request)

    _install_transport(monkeypatch, handler)

    with pytest.raises(google_sheets_service.GoogleSheetsAPIError):
        await google_sheets_service.values_append(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            range_="Sheet1!A1",
            values=[["a"]],
        )
    assert calls["n"] == 1  # one attempt, no retry


@pytest.mark.asyncio
async def test_values_append_terminal_transport_error_becomes_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, handler)

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
async def test_values_append_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(google_sheets_service.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "upstream down"}})

    _install_transport(monkeypatch, handler)

    with pytest.raises(google_sheets_service.GoogleSheetsAPIError) as exc:
        await google_sheets_service.values_append(
            access_token="tok",
            spreadsheet_id="1abc_xyz_1234567890ABCDEF",
            range_="Sheet1!A1",
            values=[["a"]],
        )
    assert exc.value.status == 503
