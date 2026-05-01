from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.items import RunItem
from agents.stream_events import RunItemStreamEvent

from skyvern.forge.sdk.copilot.output_utils import (
    _sanitize_failure_text,
    build_run_blocks_response,
    sanitize_tool_result_for_llm,
    summarize_tool_result_detail,
)
from skyvern.forge.sdk.copilot.streaming_adapter import stream_to_sse
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType


def test_summarize_tool_result_detail_returns_none_on_success() -> None:
    assert summarize_tool_result_detail({"ok": True, "data": {"block_count": 2}}) is None


def test_summarize_tool_result_detail_caps_at_max_chars() -> None:
    long_error = "Workflow validation failed: " + ("missing field 'foo'; " * 200)
    detail = summarize_tool_result_detail({"ok": False, "error": long_error}, max_chars=400)
    assert detail is not None
    assert len(detail) <= 400
    assert detail.endswith("...")


def test_summarize_tool_result_detail_preserves_short_full_message() -> None:
    detail = summarize_tool_result_detail(
        {"ok": False, "error": "Workflow validation failed: title field required"},
    )
    assert detail == "Workflow validation failed: title field required"


def test_summarize_tool_result_detail_strips_header_blobs() -> None:
    text = "Failure with headers: {'host': 'x', 'authorization': 'Bearer abc'} please retry"
    detail = summarize_tool_result_detail({"ok": False, "error": text})
    assert detail is not None
    assert "authorization" not in detail
    assert "Bearer" not in detail


def test_sanitize_failure_text_default_cap_unchanged() -> None:
    sanitized = _sanitize_failure_text("x" * 200)
    assert len(sanitized) == 120
    assert sanitized.endswith("...")


def test_sanitize_failure_text_respects_max_chars() -> None:
    sanitized = _sanitize_failure_text("x" * 1000, max_chars=500)
    assert len(sanitized) == 500
    assert sanitized.endswith("...")


def test_sanitize_tool_result_for_llm_passes_through_failure_dict() -> None:
    failure = {"ok": False, "error": "Workflow validation failed: title required"}
    sanitized = sanitize_tool_result_for_llm("update_workflow", failure)
    assert sanitized["ok"] is False
    assert sanitized["error"] == "Workflow validation failed: title required"


def test_build_run_blocks_response_success_passes_through() -> None:
    response = build_run_blocks_response(True, {"workflow_run_id": "wr_test", "blocks": []})
    assert response == {"ok": True, "data": {"workflow_run_id": "wr_test", "blocks": []}}


def test_build_run_blocks_response_promotes_run_level_failure_reason() -> None:
    response = build_run_blocks_response(
        False,
        {
            "workflow_run_id": "wr_test",
            "overall_status": "failed",
            "failure_reason": "Navigation timed out after 60s",
            "blocks": [],
        },
    )
    assert response["ok"] is False
    assert response["error"] == "Navigation timed out after 60s"


def test_build_run_blocks_response_falls_back_when_no_failure_reason() -> None:
    response = build_run_blocks_response(False, {"workflow_run_id": "wr_test"})
    assert response["error"] == "Unknown error (no failure reason provided)"


async def _capture_tool_result(tool_name: str, parsed_output: dict[str, Any]) -> Any:
    """Drive `stream_to_sse` over a single tool round-trip and return the
    emitted ``WorkflowCopilotToolResultUpdate``."""
    call_item = MagicMock(spec=RunItem)
    call_item.raw_item = {"call_id": "c1", "name": tool_name, "arguments": "{}"}
    tool_call = RunItemStreamEvent(name="tool_called", item=call_item)

    out_item = MagicMock(spec=RunItem)
    out_item.raw_item = {"call_id": "c1", "name": tool_name}
    out_item.output = [{"type": "text", "text": json.dumps(parsed_output)}]
    tool_output = RunItemStreamEvent(name="tool_output", item=out_item)

    async def _events() -> Any:
        yield tool_call
        yield tool_output

    result = MagicMock()
    result.stream_events = lambda: _events()
    result.cancel = MagicMock()

    sent: list[Any] = []

    async def _send(payload: Any) -> bool:
        sent.append(payload)
        return True

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    stream.send = _send

    await stream_to_sse(result, stream, SimpleNamespace())

    tool_results = [p for p in sent if getattr(p, "type", None) == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert len(tool_results) == 1
    return tool_results[0]


@pytest.mark.asyncio
async def test_stream_emits_detail_for_failure() -> None:
    long_error = "Workflow validation failed: " + (
        "blocks.0.task expects 'navigation_goal' but the emitted YAML omitted it. " * 4
    )
    payload = await _capture_tool_result(
        "update_workflow",
        {"ok": False, "error": long_error},
    )
    assert payload.success is False
    assert payload.detail is not None
    assert len(payload.detail) > 120
    # `summary` is the visible bullet, capped tighter than `detail` (the
    # tooltip-grade text) — strictly longer detail is the contract.
    assert len(payload.detail) > len(payload.summary)


@pytest.mark.asyncio
async def test_stream_emits_no_detail_for_success() -> None:
    payload = await _capture_tool_result(
        "update_workflow",
        {"ok": True, "data": {"block_count": 3}},
    )
    assert payload.success is True
    assert payload.detail is None
