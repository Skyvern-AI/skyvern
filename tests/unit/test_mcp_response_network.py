"""Tests for tool-aware network and HAR response summaries."""

from __future__ import annotations

import json
from collections import deque
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext, make_result
from skyvern.cli.mcp_tools import inspection, mcp
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS, response_transformed
from skyvern.cli.mcp_tools.response_distillation import TransformTier
from skyvern.cli.mcp_tools.response_network import (
    format_har_response,
    format_network_request_detail_response,
    format_network_requests_response,
)
from tests.unit._mcp_browser_fakes import make_page, make_session_state


def _network_request(request_id: int) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "url": f"https://api.example.com/items/{request_id}?token=REDACTED",
        "method": "GET" if request_id % 2 == 0 else "POST",
        "status": 200 + (request_id % 3),
        "resource_type": "fetch",
        "content_type": "application/json",
        "timing_ms": float(request_id),
        "response_size": request_id * 10,
        "page_url": "https://example.com/" + ("x" * 500),
        "tab_id": "tab-1",
    }


def _network_response(count: int) -> dict[str, Any]:
    requests = [_network_request(index) for index in range(count)]
    return make_result(
        "skyvern_network_requests",
        browser_context=BrowserContext(mode="local"),
        data={"requests": requests, "count": count, "buffer_size": count},
    )


def _har_entry(index: int) -> dict[str, Any]:
    return {
        "startedDateTime": f"2026-01-01T00:00:{index:02d}Z",
        "time": float(index),
        "request": {
            "method": "GET" if index % 2 == 0 else "POST",
            "url": f"https://api.example.com/items/{index}?token=REDACTED",
            "headers": [{"name": "accept", "value": "application/json"}],
            "queryString": [{"name": "token", "value": "REDACTED"}],
        },
        "response": {
            "status": 200 + (index % 3),
            "headers": [{"name": "content-type", "value": "application/json"}],
            "content": {"mimeType": "application/json", "size": index * 100},
        },
        "timings": {"send": -1, "wait": float(index), "receive": -1},
    }


def _har_response(count: int) -> dict[str, Any]:
    return make_result(
        "skyvern_har_stop",
        browser_context=BrowserContext(mode="local"),
        data={
            "har": {
                "log": {
                    "version": "1.2",
                    "creator": {"name": "Skyvern", "version": "1.0"},
                    "pages": [],
                    "entries": [_har_entry(index) for index in range(count)],
                }
            },
            "entry_count": count,
        },
    )


def _patch_inspection(monkeypatch: pytest.MonkeyPatch, state: Any) -> None:
    raw_page = MagicMock()
    raw_page.on = MagicMock()

    async def fake_get_page(**kwargs: Any) -> tuple[Any, BrowserContext]:
        return make_page(raw_page), BrowserContext(mode="local")

    monkeypatch.setattr(inspection, "get_page", fake_get_page)
    monkeypatch.setattr(inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)


def test_network_list_formatter_keeps_exact_counts_and_representative_ids() -> None:
    original = _network_response(20)

    result = format_network_requests_response(original)

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is False
    data = result.value["data"]
    assert data["count"] == 20
    assert data["buffer_size"] == 20
    assert data["omitted_request_count"] == 15
    assert [request["request_id"] for request in data["requests"]] == [0, 4, 9, 14, 19]
    assert set(data["requests"][0]) == {
        "request_id",
        "url",
        "method",
        "status",
        "resource_type",
        "content_type",
        "timing_ms",
        "response_size",
        "page_url",
        "tab_id",
    }
    assert data["requests"][0]["page_url"] == original["data"]["requests"][0]["page_url"]
    assert data["requests"][0]["tab_id"] == "tab-1"


@pytest.mark.asyncio
async def test_har_formatter_compacts_an_over_cap_response_without_losing_log_shape() -> None:
    original = _har_response(1_000)
    assert len(json.dumps(original)) > MCP_MAX_RESPONSE_CHARS

    @response_transformed(formatter=format_har_response)
    async def tool() -> dict[str, Any]:
        return original

    result = await tool()
    log = result["data"]["har"]["log"]

    assert len(json.dumps(result)) <= MCP_MAX_RESPONSE_CHARS
    assert log["version"] == "1.2"
    assert log["creator"] == {"name": "Skyvern", "version": "1.0"}
    assert len(log["entries"]) == 5


def test_network_list_formatter_does_not_expand_a_large_response() -> None:
    original = _network_response(100)
    summarized = format_network_requests_response(original).value

    assert len(json.dumps(summarized)) < len(json.dumps(original))


@pytest.mark.asyncio
async def test_network_list_wire_response_preserves_long_request_context() -> None:
    original = _network_response(20)
    original["data"]["requests"][0]["url"] = "https://api.example.com/items?" + ("query=x&" * 100)

    @response_transformed(formatter=format_network_requests_response)
    async def tool() -> dict[str, Any]:
        return original

    result = await tool()
    request = result["data"]["requests"][0]

    assert request["url"] == original["data"]["requests"][0]["url"]
    assert request["page_url"] == original["data"]["requests"][0]["page_url"]


@pytest.mark.asyncio
async def test_network_detail_wire_response_preserves_long_request_context() -> None:
    original = _network_response(1)
    request = original["data"]["requests"][0]
    request["url"] = "https://api.example.com/items?" + ("query=x&" * 100)
    body = json.dumps({"items": [{"id": index, "value": "x" * 500} for index in range(20)]})
    original["data"] = {"request": request, "body": body}

    @response_transformed(formatter=format_network_request_detail_response)
    async def tool() -> dict[str, Any]:
        return original

    result = await tool()

    assert result["data"]["request"]["url"] == request["url"]
    assert result["data"]["request"]["page_url"] == request["page_url"]


def test_network_detail_formatter_compacts_nested_json_and_preserves_anchors() -> None:
    body = json.dumps(
        {
            "result": {
                "groups": [
                    {"id": group, "items": [{"id": item, "value": "x" * 500} for item in range(8)]}
                    for group in range(8)
                ]
            }
        }
    )
    original = make_result(
        "skyvern_network_request_detail",
        data={
            "request": {
                **_network_request(42),
                "response_headers": {"content-type": "application/json", "x-request-id": "safe-id"},
            },
            "body": body,
            "body_available": True,
        },
    )

    result = format_network_request_detail_response(original)

    assert result.tier is TransformTier.STRUCTURED
    assert result.value["data"]["request"]["request_id"] == 42
    assert result.value["data"]["request"]["url"].endswith("token=REDACTED")
    assert result.value["data"]["request"]["response_headers"] == {
        "content-type": "application/json",
        "x-request-id": "safe-id",
    }
    assert isinstance(result.value["data"]["body"], dict)
    assert len(json.dumps(result.value)) < len(json.dumps(original))


def test_network_detail_formatter_uses_central_fallback_for_malformed_body() -> None:
    malformed = '{"items": [1, 2, definitely-not-json]'
    original = make_result(
        "skyvern_network_request_detail",
        data={"request": _network_request(7), "body": malformed, "body_available": True},
    )

    result = format_network_request_detail_response(original)

    assert result.tier is TransformTier.PASSTHROUGH
    assert result.fallback_reason == "parse_failed"
    assert result.value is original
    assert result.value["data"]["body"] == malformed


def test_har_formatter_keeps_metadata_counts_and_deterministic_entries() -> None:
    original = _har_response(20)

    result = format_har_response(original)

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is False
    data = result.value["data"]
    assert data["entry_count"] == 20
    assert data["omitted_entry_count"] == 15
    assert data["har"]["log"]["version"] == "1.2"
    assert data["har"]["log"]["creator"] == {"name": "Skyvern", "version": "1.0"}
    entries = data["har"]["log"]["entries"]
    assert [entry["url"].split("/")[-1].split("?")[0] for entry in entries] == ["0", "4", "9", "14", "19"]
    assert entries[0] == {
        "method": "GET",
        "url": "https://api.example.com/items/0?token=REDACTED",
        "status": 200,
        "content_type": "application/json",
        "response_size": 0,
        "time": 0.0,
        "timings": {"send": -1, "wait": 0.0, "receive": -1},
    }
    assert "headers" not in entries[0]
    assert "body" not in entries[0]
    assert len(json.dumps(result.value)) < len(json.dumps(original))


@pytest.mark.asyncio
async def test_network_list_summary_and_full_recovery_do_not_restore_stripped_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_session_state()
    for request_id in range(20):
        request = _network_request(request_id)
        request["response_headers"] = {"content-type": "application/json"}
        state.network_requests.append(request)
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_network_requests")
    summary = await tool.fn()
    full = await tool.fn(verbosity="full")

    assert summary["data"]["count"] == 20
    assert summary["data"]["omitted_request_count"] == 15
    assert [request["request_id"] for request in summary["data"]["requests"]] == [0, 4, 9, 14, 19]
    assert full["data"]["count"] == 20
    assert len(full["data"]["requests"]) == 20
    assert all("response_headers" not in request for request in full["data"]["requests"])


@pytest.mark.asyncio
async def test_network_list_clear_marks_omitted_requests_unrecoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_session_state()
    for request_id in range(20):
        state.network_requests.append(_network_request(request_id))
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_network_requests")
    summary = await tool.fn(clear=True)

    assert not state.network_requests
    assert summary["data"]["omitted_request_count"] == 15
    hint = summary["_response_distillation"]["recovery_hint"]
    assert "clear=True" in hint
    assert "cannot be recovered" in hint


@pytest.mark.asyncio
async def test_network_detail_full_recovers_sanitized_uncompacted_body(monkeypatch: pytest.MonkeyPatch) -> None:
    state = make_session_state()
    request = _network_request(42)
    request["response_headers"] = {"content-type": "application/json", "x-request-id": "safe-id"}
    state.network_requests.append(request)
    body = json.dumps({"items": [{"id": index, "value": "x" * 500} for index in range(20)]})
    state._body_store[42] = body
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_network_request_detail")
    summary = await tool.fn(request_id=42)
    full = await tool.fn(request_id=42, verbosity="full")

    assert summary["data"]["request"]["request_id"] == 42
    assert isinstance(summary["data"]["body"], dict)
    assert full["data"]["request"]["request_id"] == 42
    assert full["data"]["body"] == body
    assert full["data"]["request"]["response_headers"]["x-request-id"] == "safe-id"


@pytest.mark.asyncio
async def test_har_stop_requires_a_new_recording_for_full_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    state = make_session_state(har_enabled=True, _har_entries=deque((_har_entry(i) for i in range(20)), maxlen=5000))
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_har_stop")
    summary = await tool.fn()
    retry = await tool.fn(verbosity="full")

    assert summary["data"]["entry_count"] == 20
    assert summary["data"]["omitted_entry_count"] == 15
    assert retry["ok"] is False


@pytest.mark.asyncio
async def test_har_full_returns_all_entries_when_requested_on_first_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    state = make_session_state(har_enabled=True, _har_entries=deque((_har_entry(i) for i in range(20)), maxlen=5000))
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_har_stop")
    full = await tool.fn(verbosity="full")

    assert full["data"]["entry_count"] == 20
    assert len(full["data"]["har"]["log"]["entries"]) == 20
    assert full["data"]["har"]["log"]["entries"][0]["request"]["headers"] == [
        {"name": "accept", "value": "application/json"}
    ]


def test_har_summary_retains_non_entry_log_keys() -> None:
    """Every non-entries HAR log key (version, creator, pages, …) must survive summarization
    verbatim because schema-sensitive consumers require the standard HAR shape."""
    pages = [{"id": "page_1", "startedDateTime": "2026-08-16T00:00:00Z", "title": "Example"}]
    response = make_result(
        "skyvern_har_stop",
        ok=True,
        browser_context=BrowserContext(mode="cloud_session", session_id="pbs_test"),
        data={
            "har": {
                "log": {
                    "version": "1.2",
                    "creator": {"name": "skyvern", "version": "1.0"},
                    "pages": pages,
                    "entries": [_har_entry(index) for index in range(20)],
                }
            },
            "entry_count": 20,
        },
    )

    result = format_har_response(response)

    log = result.value["data"]["har"]["log"]
    assert log["pages"] == pages
    assert log["version"] == "1.2"
    assert result.complete is False


@pytest.mark.asyncio
async def test_har_wire_response_preserves_standard_log_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the registered tool (formatter + generic compaction + cap), the HAR log must stay
    standard-shaped: verbatim version/creator/pages and a real entries list — not depth summaries."""
    state = make_session_state(har_enabled=True, _har_entries=deque((_har_entry(i) for i in range(20)), maxlen=5000))
    _patch_inspection(monkeypatch, state)

    tool = await mcp.get_tool("skyvern_har_stop")
    result = await tool.fn()

    log = result["data"]["har"]["log"]
    assert log["version"] == "1.2"
    assert log["creator"] == {"name": "Skyvern", "version": "1.0"}
    assert log["pages"] == []
    assert isinstance(log["entries"], list)
    assert all(isinstance(entry, dict) and "method" in entry for entry in log["entries"])
