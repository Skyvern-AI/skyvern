from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS
from skyvern.cli.mcp_tools.response_distillation import TransformTier
from skyvern.cli.mcp_tools.response_workflow import format_workflow_response
from tests.unit._mcp_test_helpers import patch_skyvern_client


def _nested_output() -> dict[str, Any]:
    long_url = "https://artifacts.skyvern.example/" + ("x" * 1_000)
    return {
        "collect_customer_data": {
            "task_screenshot_artifact_ids": [f"art_task_{index}" for index in range(8)],
            "workflow_screenshot_artifact_ids": [f"art_workflow_{index}" for index in range(7)],
            "task_screenshots": [f"{long_url}/task/{index}" for index in range(6)],
            "workflow_screenshots": [f"{long_url}/workflow/{index}" for index in range(5)],
        },
        "extracted_information": [{"account_id": "acct_123", "state": "complete"}],
    }


def _run_response(*, output: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "action": "skyvern_workflow_run",
        "data": {
            "run_id": "wr_waited",
            "status": "completed",
            "workflow_id": "wpid_customer_export",
            "workflow_title": "Customer export",
            "failure_reason": "A recoverable step failed before retrying",
            "step_count": 9,
            "total_steps": 9,
            "output": output,
            "sdk_equivalent": "await skyvern.run_workflow(workflow_id='wpid_customer_export')",
        },
        "warnings": [],
    }


def test_workflow_formatter_is_deterministic_and_does_not_mutate_nested_outputs() -> None:
    response = _run_response(output=_nested_output())
    original = deepcopy(response)

    first = format_workflow_response(response, tool_name="skyvern_workflow_run")
    second = format_workflow_response(response, tool_name="skyvern_workflow_run")

    assert first == second
    assert response == original
    assert first.tier is TransformTier.STRUCTURED
    assert first.value["data"]["run_id"] == "wr_waited"
    assert first.value["data"]["workflow_id"] == "wpid_customer_export"
    assert first.value["data"]["failure_reason"] == response["data"]["failure_reason"]
    assert first.value["data"]["step_count"] == 9
    assert "output" not in first.value["data"]
    assert first.value["data"]["output_summary"] == {
        "present": True,
        "top_level_keys": ["collect_customer_data", "extracted_information"],
        "block_output_count": 1,
        "has_extracted_information": True,
        "nested_screenshot_count": 11,
        "artifact_id_count": 15,
    }
    assert first.value["data"]["artifact_summary"]["artifact_ids_preview"] == [
        "art_task_0",
        "art_task_1",
        "art_task_2",
        "art_task_3",
    ]
    assert "workflow_screenshot_count" not in first.value["data"]["artifact_summary"]
    assert "downloaded_file_count" not in first.value["data"]["artifact_summary"]
    marker = first.value["_response_distillation"]
    assert marker["complete"] is False
    assert first.owns_completeness_marker is True
    assert "skyvern_workflow_status(run_id='wr_waited', verbosity='full')" in marker["recovery_hint"]
    assert first.complete is False


def test_workflow_status_formatter_reports_omitted_anchor_provenance_as_incomplete() -> None:
    omitted_task_id = "tsk_omitted"
    response = {
        "ok": True,
        "data": {
            "run_id": "wr_status",
            "status": "completed",
            "steps": [{"task_id": f"tsk_{index}"} for index in range(5)] + [{"task_id": omitted_task_id}],
        },
    }

    result = format_workflow_response(response, tool_name="skyvern_workflow_status")

    assert result.complete is False
    assert omitted_task_id not in str(result.value["data"]["steps"])
    marker = result.value["_response_distillation"]
    assert marker["complete"] is False
    assert "skyvern_workflow_status(run_id='wr_status', verbosity='full')" in marker["recovery_hint"]


def test_workflow_formatter_handles_fastmcp_concise_shape_without_action() -> None:
    response = {
        "ok": True,
        "data": {
            "run_id": "wr_fastmcp",
            "status": "completed",
            "workflow_id": "wpid_fastmcp",
            "outputs": _nested_output(),
        },
    }

    result = format_workflow_response(response, tool_name="skyvern_workflow_run")

    assert result.value["data"]["run_id"] == "wr_fastmcp"
    assert result.value["data"]["workflow_id"] == "wpid_fastmcp"
    assert "outputs" not in result.value["data"]
    assert result.value["data"]["output_summary"]["artifact_id_count"] == 15
    assert "wr_fastmcp" in result.value["_response_distillation"]["recovery_hint"]


def test_workflow_status_default_summary_is_a_noop() -> None:
    response = {
        "ok": True,
        "action": "skyvern_workflow_status",
        "data": {
            "run_id": "wr_summary",
            "status": "completed",
            "run_type": "workflow_run",
            "artifact_summary": {
                "recording_available": True,
                "workflow_screenshot_count": 2,
                "downloaded_file_count": 0,
                "artifact_id_count": 3,
            },
            "output_summary": {
                "present": True,
                "top_level_keys": ["collect", "extracted_information"],
                "block_output_count": 1,
                "has_extracted_information": True,
                "nested_screenshot_count": 0,
                "artifact_id_count": 3,
            },
        },
    }

    result = format_workflow_response(response, tool_name="skyvern_workflow_status")

    assert result.tier is TransformTier.PASSTHROUGH
    assert result.value is response


@pytest.mark.asyncio
async def test_waited_workflow_run_compacts_nested_output_with_status_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        run_id="wr_waited",
        status="completed",
        workflow_id="wpid_customer_export",
        workflow_title="Customer export",
        failure_reason="A recoverable step failed before retrying",
        step_count=9,
        total_steps=9,
        output=_nested_output(),
    )
    fake_client = SimpleNamespace(run_workflow=AsyncMock(return_value=run))
    patch_skyvern_client(monkeypatch, fake_client)

    tool = await mcp.get_tool("skyvern_workflow_run")
    result = await tool.fn(workflow_id="wpid_customer_export", wait=True)

    fake_client.run_workflow.assert_awaited_once()
    data = result["data"]
    assert data["run_id"] == "wr_waited"
    assert data["status"] == "completed"
    assert data["workflow_id"] == "wpid_customer_export"
    assert data["workflow_title"] == "Customer export"
    assert data["failure_reason"] == "A recoverable step failed before retrying"
    assert data["step_count"] == 9
    assert data["total_steps"] == 9
    assert "output" not in data
    assert data["output_summary"]["has_extracted_information"] is True
    assert "wr_waited" in result["_response_distillation"]["recovery_hint"]
    assert data["sdk_equivalent"] == (
        "await skyvern.run_workflow(workflow_id='wpid_customer_export', wait_for_completion=True, timeout=300)"
    )


@pytest.mark.asyncio
async def test_registered_workflow_run_does_not_reinject_formatter_omitted_task_id_into_response_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    omitted_task_id = "tsk_omitted"
    run = SimpleNamespace(
        run_id="wr_anchor_recovery",
        status="completed",
        workflow_id="wpid_anchor_recovery",
        output={
            "steps": [
                {
                    "task_id": omitted_task_id if index == 5 else f"tsk_{index}",
                    "result": {
                        "records": [
                            {
                                "record": f"record-{index}-{record_index}",
                                "content": "x" * 1_000,
                            }
                            for record_index in range(8)
                        ]
                    },
                }
                for index in range(6)
            ]
        },
    )
    fake_client = SimpleNamespace(run_workflow=AsyncMock(return_value=run))
    patch_skyvern_client(monkeypatch, fake_client)

    tool = await mcp.get_tool("skyvern_workflow_run")
    result = await tool.fn(workflow_id="wpid_anchor_recovery", wait=True)
    assert "output" not in result["data"]
    assert result["data"]["output_summary"] == {
        "present": True,
        "top_level_keys": ["steps"],
        "block_output_count": 1,
        "has_extracted_information": False,
        "nested_screenshot_count": 0,
        "artifact_id_count": 0,
    }

    assert omitted_task_id not in str(result.get("_response_anchors", {}))
    assert result["_response_distillation"]["recovery_hint"] == (
        "Call skyvern_workflow_status(run_id='wr_anchor_recovery', verbosity='full') to retrieve the full output."
    )


@pytest.mark.asyncio
async def test_workflow_status_full_bypasses_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    expanded_output = {"result": "x" * 1_000, "nested": {"items": list(range(10))}}
    run = SimpleNamespace(
        run_id="tsk_v2_full",
        status="completed",
        run_type="task_v2",
        output=expanded_output,
        failure_reason=None,
        step_count=4,
    )
    fake_client = SimpleNamespace(get_run=AsyncMock(return_value=run))
    patch_skyvern_client(monkeypatch, fake_client)

    tool = await mcp.get_tool("skyvern_workflow_status")
    result = await tool.fn(run_id="tsk_v2_full", verbosity="full")

    assert result["data"]["output"] == expanded_output
    assert "output_summary" not in result["data"]
    assert "_response_distillation" not in result


@pytest.mark.asyncio
async def test_workflow_status_full_over_cap_keeps_existing_summary_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = {
        "workflow_run_id": "wr_over_cap",
        "workflow_id": "wpid_over_cap",
        "status": "completed",
        "outputs": {"result": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)},
    }
    status = AsyncMock(return_value=run)
    monkeypatch.setattr(workflow_tools, "get_workflow_run_status", status)

    tool = await mcp.get_tool("skyvern_workflow_status")
    result = await tool.fn(run_id="wr_over_cap", verbosity="full")

    status.assert_awaited_once_with("wr_over_cap", include_output_details=True)
    assert result["data"]["run_id"] == "wr_over_cap"
    assert "output" not in result["data"]
    assert result["data"]["output_summary"]["present"] is True
    assert any("returning a reduced payload" in warning for warning in result["warnings"])
    assert "_truncated" not in result


@pytest.mark.asyncio
async def test_workflow_status_default_does_not_expand_backend_output(monkeypatch: pytest.MonkeyPatch) -> None:
    status = AsyncMock(
        return_value={
            "workflow_run_id": "wr_no_expand",
            "workflow_id": "wpid_no_expand",
            "status": "running",
            "outputs": {"progress": "step 2"},
        }
    )
    monkeypatch.setattr(workflow_tools, "get_workflow_run_status", status)

    tool = await mcp.get_tool("skyvern_workflow_status")
    result = await tool.fn(run_id="wr_no_expand")

    status.assert_awaited_once_with("wr_no_expand", include_output_details=False)
    assert result["data"]["output_summary"]["scalar_preview"] == {"progress": "step 2"}
    assert "output" not in result["data"]
    assert "_response_distillation" not in result
