"""End-to-end coverage for response formatting at the FastMCP boundary."""

from __future__ import annotations

import inspect
import json
from collections import deque
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client

import skyvern.cli.mcp_tools.browser as browser_tools
import skyvern.cli.mcp_tools.inspection as inspection_tools
import skyvern.cli.mcp_tools.storage as storage_tools
import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.response import (
    FULL_DEFAULT_TOOL_NAMES,
    MCP_MAX_RESPONSE_CHARS,
    SUMMARY_DEFAULT_TOOL_NAMES,
    response_transformed,
)
from tests.unit._mcp_browser_fakes import make_mock_page, make_page, make_session_state, patch_get_page
from tests.unit._mcp_test_helpers import patch_skyvern_client

_TARGETED_TOOLS = {name: True for name in FULL_DEFAULT_TOOL_NAMES | SUMMARY_DEFAULT_TOOL_NAMES}


def _serialized_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _record_tool_results(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Snapshot each execution result immediately before it enters its registered wrapper."""
    original_make_result = module.make_result
    records: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def recording_make_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_make_result(*args, **kwargs)
        records.append((result, deepcopy(result)))
        return result

    monkeypatch.setattr(module, "make_result", recording_make_result)
    return records


def _assert_pipeline_inputs_unchanged(records: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    assert records
    assert all(result == snapshot for result, snapshot in records)


def _patch_inspection_session(monkeypatch: pytest.MonkeyPatch, state: Any) -> None:
    raw_page = MagicMock()
    raw_page.on = MagicMock()

    async def fake_get_page(**kwargs: Any) -> tuple[Any, BrowserContext]:
        return make_page(raw_page), BrowserContext(mode="local")

    monkeypatch.setattr(inspection_tools, "get_page", fake_get_page)
    monkeypatch.setattr(inspection_tools, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)


@pytest.mark.asyncio
async def test_every_targeted_registration_uses_response_transform_and_preserves_schema() -> None:
    async def probe() -> dict[str, Any]:
        return {}

    wrapper_code = response_transformed()(probe).__code__
    listed = {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}

    assert _TARGETED_TOOLS.keys() <= listed.keys()
    for name, exposes_verbosity in _TARGETED_TOOLS.items():
        registered = await mcp.get_tool(name)
        assert registered.fn.__name__ == name
        assert registered.fn.__code__ is wrapper_code, f"{name} bypasses response_transformed"
        assert inspect.signature(registered.fn) == inspect.signature(inspect.unwrap(registered.fn))
        properties = listed[name].parameters.get("properties", {})
        assert ("verbosity" in properties) is exposes_verbosity
        if exposes_verbosity:
            assert set(properties["verbosity"]["enum"]) == {"summary", "full"}
            assert properties["verbosity"]["default"] == ("full" if name in FULL_DEFAULT_TOOL_NAMES else "summary")


@pytest.mark.asyncio
async def test_browser_response_tiers_full_recovery_and_cap_are_remote_and_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_tool_results(monkeypatch, browser_tools)
    raw = [{"row_id": f"row-{index}", "body": "browser value " * 100} for index in range(12)]
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=raw)
    patch_get_page(
        monkeypatch,
        browser_tools,
        page,
        BrowserContext(mode="cloud_session", session_id="pbs_response_integration"),
    )

    async with Client(mcp) as client:
        full = await client.call_tool("skyvern_evaluate", {"expression": "window.catalog"})
        compact = await client.call_tool("skyvern_evaluate", {"expression": "window.catalog", "verbosity": "summary"})

        degraded_raw = json.dumps({"items": raw}) + " trailing browser diagnostic"
        page.evaluate.return_value = degraded_raw
        degraded = await client.call_tool("skyvern_evaluate", {"expression": "window.degraded", "verbosity": "summary"})

        page.evaluate.return_value = "z" * (MCP_MAX_RESPONSE_CHARS + 2_000)
        capped = await client.call_tool("skyvern_evaluate", {"expression": "window.oversized"})

    assert compact.is_error is False
    assert isinstance(compact.data, dict)
    assert compact.data["ok"] is True
    assert compact.data["error"] is None
    assert compact.data["data"]["result"]["_length"] == 12
    assert compact.data["_response_distillation"]["complete"] is False
    assert compact.data["_response_distillation"]["tier"] == "structured"
    assert "verbosity='full'" in compact.data["_response_distillation"]["recovery_hint"]

    assert isinstance(full.data, dict)
    assert full.data["data"]["result"] == raw
    assert "_response_distillation" not in full.data

    assert isinstance(degraded.data, dict)
    assert degraded.data["_response_distillation"]["complete"] is False
    assert degraded.data["_response_distillation"]["tier"] == "degraded"
    assert degraded.data["data"]["result"]["items"]["_length"] == 12

    assert isinstance(capped.data, dict)
    assert capped.data["_truncated"] is True
    assert capped.data["ok"] is False
    assert capped.data["error"]["code"] == "RESPONSE_TRUNCATED"
    assert capped.data["_original_chars"] > MCP_MAX_RESPONSE_CHARS
    assert capped.data["_max_chars"] == MCP_MAX_RESPONSE_CHARS
    assert "response_offset_chars" in capped.data["_hint"]
    assert _serialized_chars(capped.data) <= MCP_MAX_RESPONSE_CHARS
    _assert_pipeline_inputs_unchanged(records)


@pytest.mark.asyncio
async def test_network_detail_and_har_are_formatted_after_redaction_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_tool_results(monkeypatch, inspection_tools)
    state = make_session_state(har_enabled=True)
    request_id = 42
    state.network_requests.append(
        {
            "request_id": request_id,
            "url": "https://api.example.test/items?token=REDACTED",
            "method": "GET",
            "status": 200,
            "resource_type": "fetch",
            "content_type": "application/json",
            "response_headers": {"content-type": "application/json", "x-request-id": "safe-id"},
        }
    )
    body = json.dumps(
        {
            "result": {
                "groups": [
                    {"group_id": group, "items": [{"item_id": item, "value": "x" * 500} for item in range(8)]}
                    for group in range(8)
                ]
            }
        }
    )
    state._body_store[request_id] = body

    secret = "super-secret-value"

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(secret, "REDACTED")
        return value

    state._codeblock_redactor = redact
    state._har_entries = deque(
        (
            {
                "time": float(index),
                "request": {
                    "method": "GET",
                    "url": f"https://api.example.test/items/{index}?token={secret}",
                    "headers": [{"name": "authorization", "value": secret}],
                    "queryString": [{"name": "token", "value": secret}],
                },
                "response": {
                    "status": 200,
                    "content": {"mimeType": "application/json", "size": 100 + index},
                },
                "timings": {"send": 1, "wait": index, "receive": 1},
            }
            for index in range(8)
        ),
        maxlen=5_000,
    )
    _patch_inspection_session(monkeypatch, state)

    async with Client(mcp) as client:
        detail = await client.call_tool("skyvern_network_request_detail", {"request_id": request_id})
        har = await client.call_tool("skyvern_har_stop", {})

    assert isinstance(detail.data, dict)
    assert detail.data["ok"] is True
    assert detail.data["data"]["request"]["request_id"] == request_id
    assert isinstance(detail.data["data"]["body"], dict)
    assert isinstance(detail.data["data"]["body"]["result"], dict)
    assert body not in json.dumps(detail.data["data"]["body"], ensure_ascii=False)
    assert detail.data["_response_distillation"]["complete"] is False

    assert isinstance(har.data, dict)
    assert har.data["ok"] is True
    assert har.data["data"]["entry_count"] == 8
    assert har.data["data"]["omitted_entry_count"] == 3
    assert har.data["_response_distillation"]["complete"] is False
    serialized_har = json.dumps(har.data, ensure_ascii=False)
    assert secret not in serialized_har
    assert "REDACTED" in serialized_har
    _assert_pipeline_inputs_unchanged(records)


@pytest.mark.asyncio
async def test_session_storage_summary_is_remote_and_does_not_mutate_pipeline_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_tool_results(monkeypatch, storage_tools)
    items = {f"storage-key-{index:02d}": f"value-{index}-" + ("v" * 80) for index in range(30)}
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=items)
    patch_get_page(monkeypatch, storage_tools, page, BrowserContext(mode="local"))

    async with Client(mcp) as client:
        result = await client.call_tool("skyvern_get_session_storage", {})

    assert isinstance(result.data, dict)
    assert result.data["ok"] is True
    assert result.data["data"]["count"] == 30
    assert result.data["data"]["items"]["_key_count"] == 30
    assert result.data["data"]["items"]["_omitted_keys"] == 8
    assert result.data["_response_distillation"]["complete"] is False
    assert "verbosity='full'" in result.data["_response_distillation"]["recovery_hint"]
    _assert_pipeline_inputs_unchanged(records)


@pytest.mark.asyncio
async def test_workflow_run_summary_recovers_through_full_status_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_tool_results(monkeypatch, workflow_tools)
    output = {"records": [{"record_id": f"record-{index}", "details": "workflow output " * 100} for index in range(12)]}
    run = SimpleNamespace(
        run_id="wr_response_integration",
        status="completed",
        workflow_id="wpid_response_integration",
        output=output,
    )
    patch_skyvern_client(monkeypatch, SimpleNamespace(run_workflow=AsyncMock(return_value=run)))
    status_payload = {
        "workflow_run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "status": "completed",
        "outputs": output,
        "recording_url": "https://recordings.example.test/run",
    }
    status = AsyncMock(return_value=status_payload)
    monkeypatch.setattr(workflow_tools, "get_workflow_run_status", status)

    async with Client(mcp) as client:
        summary = await client.call_tool(
            "skyvern_workflow_run",
            {"workflow_id": run.workflow_id, "wait": True},
        )
        recovered = await client.call_tool(
            "skyvern_workflow_status",
            {"run_id": run.run_id, "verbosity": "full"},
        )

    assert isinstance(summary.data, dict)
    assert summary.data["ok"] is True
    assert summary.data["data"]["run_id"] == run.run_id
    assert "output" not in summary.data["data"]
    assert summary.data["data"]["output_summary"]["present"] is True
    marker = summary.data["_response_distillation"]
    assert marker["complete"] is False
    assert f"skyvern_workflow_status(run_id='{run.run_id}', verbosity='full')" in marker["recovery_hint"]

    assert isinstance(recovered.data, dict)
    assert recovered.data["ok"] is True
    assert recovered.data["data"]["run_id"] == run.run_id
    assert recovered.data["data"]["output"] == output
    assert recovered.data["data"]["recording_url"] == status_payload["recording_url"]
    assert "_response_distillation" not in recovered.data
    status.assert_awaited_once_with(run.run_id, include_output_details=True)
    _assert_pipeline_inputs_unchanged(records)
