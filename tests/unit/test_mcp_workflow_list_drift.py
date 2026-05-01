"""Regression tests for MCP workflow list resilience to Fern SDK drift.

Context: the vendored Fern SDK at ``skyvern/client/`` validates workflow
responses through a discriminated pydantic Union that does not know about
block types added to the backend after the last SDK regeneration (currently
``google_sheets_read`` and ``google_sheets_write``). Before the Strategy B fix,
``skyvern_workflow_list`` deserialized through that stale Union and would
crash the entire page for any workflow whose definition referenced an
unknown block_type. These tests lock in the bypass: list calls go through
raw httpx and return plain dicts, so new backend block types pass through
unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from skyvern.cli.mcp_tools import _workflow_http
from skyvern.cli.mcp_tools import workflow as mcp_workflow
from skyvern.client.core.client_wrapper import AsyncClientWrapper
from tests.unit._mcp_test_helpers import patch_skyvern_client as _patch_skyvern_client


def _make_workflow_dict(workflow_id: str, block_type: str, *, label: str = "step1") -> dict[str, Any]:
    """Minimal workflow payload shape, close enough to what ``v1/workflows`` returns."""
    block: dict[str, Any] = {
        "block_type": block_type,
        "label": label,
    }
    if block_type == "google_sheets_read":
        block["spreadsheet_url"] = "https://docs.google.com/spreadsheets/d/xxx/edit"
        block["sheet_name"] = "Sheet1"
        block["range"] = "A1:D100"
        block["credential_id"] = "{{ google_credential_id }}"
        block["has_header_row"] = True
    elif block_type == "navigation":
        block["url"] = "https://example.com"
        block["navigation_goal"] = "do the thing"
    return {
        "workflow_permanent_id": workflow_id,
        "workflow_id": f"wf_{workflow_id.split('_', 1)[-1]}",
        "title": f"Workflow {workflow_id}",
        "version": 1,
        "status": "published",
        "description": None,
        "is_saved_task": False,
        "folder_id": None,
        "created_at": "2026-04-20T10:00:00+00:00",
        "modified_at": "2026-04-22T10:00:00+00:00",
        "workflow_definition": {
            "parameters": [],
            "blocks": [block],
        },
    }


def _patch_skyvern_list_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: list[dict[str, Any]],
    status_code: int = 200,
) -> AsyncMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = ""

    request_mock = AsyncMock(return_value=response)
    httpx_client = SimpleNamespace(request=request_mock)
    client_wrapper = SimpleNamespace(httpx_client=httpx_client)
    fake_skyvern = SimpleNamespace(_client_wrapper=client_wrapper)

    _patch_skyvern_client(monkeypatch, fake_skyvern)
    return request_mock


@pytest.mark.asyncio
async def test_list_succeeds_when_workflow_uses_google_sheets_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a google_sheets_read block must not break list deserialization.

    Pre-fix behavior: ``skyvern.get_workflows(...)`` ran ``parse_obj_as(List[Workflow], ...)``
    on the response, and the stale Fern discriminated Union raised ValidationError
    on ``google_sheets_read``, taking the entire page down. The bypass returns raw
    dicts, so any block_type string round-trips through the tool untouched.
    """
    payload = [
        _make_workflow_dict("wpid_ok", "navigation"),
        _make_workflow_dict("wpid_sheets", "google_sheets_read"),
    ]
    request_mock = _patch_skyvern_list_response(monkeypatch, payload=payload)

    result = await mcp_workflow.skyvern_workflow_list(page=1, page_size=10)

    assert result["ok"] is True, result
    data = result["data"]
    assert data["count"] == 2
    assert data["page"] == 1
    ids = {wf["workflow_permanent_id"] for wf in data["workflows"]}
    assert ids == {"wpid_ok", "wpid_sheets"}

    request_mock.assert_awaited_once()
    call = request_mock.await_args
    assert call.args[0] == "v1/workflows"
    assert call.kwargs["method"] == "GET"
    params = call.kwargs["params"]
    assert "search_key" not in params
    assert params["page"] == 1
    assert params["page_size"] == 10
    assert params["only_workflows"] is False


@pytest.mark.asyncio
async def test_list_includes_search_key_only_when_search_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid leaking a literal search_key=None query parameter."""
    request_mock = _patch_skyvern_list_response(monkeypatch, payload=[])

    result = await mcp_workflow.skyvern_workflow_list(search="invoice", page=2, page_size=5)

    assert result["ok"] is True, result
    params = request_mock.await_args.kwargs["params"]
    assert params == {
        "search_key": "invoice",
        "page": 2,
        "page_size": 5,
        "only_workflows": False,
    }


@pytest.mark.asyncio
async def test_raw_list_uses_fern_http_wrapper_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw bypass still uses Fern's HTTP wrapper, including auth and query encoding."""
    seen_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as base_client:
        client_wrapper = AsyncClientWrapper(
            api_key="sk_test",
            headers={"x-test-header": "present"},
            base_url="https://api.example.test/api",
            timeout=60,
            httpx_client=base_client,
        )
        fake_skyvern = SimpleNamespace(_client_wrapper=client_wrapper)
        _patch_skyvern_client(monkeypatch, fake_skyvern)

        result = await mcp_workflow.skyvern_workflow_list(only_workflows=True)

    assert result["ok"] is True, result
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request.url.path == "/api/v1/workflows"
    assert request.headers["x-api-key"] == "sk_test"
    assert request.headers["x-test-header"] == "present"
    assert request.headers["x-fern-sdk-name"] == "skyvern"
    assert request.url.params["only_workflows"] == "true"


def test_extract_error_detail_truncates_non_json_response_text() -> None:
    """Avoid returning an entire proxy/load-balancer HTML page in MCP errors."""
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    response.text = "x" * 600

    detail = _workflow_http.extract_error_detail(response)

    assert detail == "x" * _workflow_http._ERROR_DETAIL_LIMIT


def test_extract_error_detail_truncates_long_json_detail() -> None:
    """A verbose ``detail`` field (e.g., a backend stack trace) is truncated too."""
    response = MagicMock()
    response.json.return_value = {"detail": "x" * 600}

    detail = _workflow_http.extract_error_detail(response)

    assert detail == "x" * _workflow_http._ERROR_DETAIL_LIMIT


def test_decode_success_payload_wraps_non_json_response() -> None:
    """A non-JSON 2xx response should carry operation context, not a bare decoder error."""
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    response.text = "<html>proxy response</html>"

    with pytest.raises(RuntimeError, match="Unexpected workflows list response"):
        _workflow_http._decode_success_payload(response, operation="workflows list")


@pytest.mark.asyncio
async def test_list_surfaces_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-2xx responses return an API_ERROR result, not a crash."""
    response = MagicMock()
    response.status_code = 500
    response.json.return_value = {"detail": "boom"}
    response.text = '{"detail": "boom"}'

    request_mock = AsyncMock(return_value=response)
    fake_skyvern = SimpleNamespace(_client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request_mock)))
    _patch_skyvern_client(monkeypatch, fake_skyvern)

    result = await mcp_workflow.skyvern_workflow_list()

    assert result["ok"] is False
    assert "500" in result["error"]["message"]


@pytest.mark.asyncio
async def test_list_preserves_serialization_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Response payload keys stay stable so MCP clients see no contract change."""
    payload = [_make_workflow_dict("wpid_ok", "navigation")]
    _patch_skyvern_list_response(monkeypatch, payload=payload)

    result = await mcp_workflow.skyvern_workflow_list(page_size=5)

    assert result["ok"] is True
    data = result["data"]
    assert set(data.keys()) == {"workflows", "page", "page_size", "count", "has_more", "sdk_equivalent"}
    wf = data["workflows"][0]
    expected_keys = {
        "workflow_permanent_id",
        "workflow_id",
        "title",
        "version",
        "status",
        "description",
        "is_saved_task",
        "folder_id",
        "created_at",
        "modified_at",
    }
    assert expected_keys <= set(wf.keys())
    assert wf["created_at"] == "2026-04-20T10:00:00+00:00"
