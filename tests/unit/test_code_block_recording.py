"""Tests for the per-action screenshot sink in ``CodeBlockActionRecording``.

The sink is awaited inside the user's own call chain (``_Recorder``'s ``finally``), so its
timeout is charged to ``CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS`` on the failure path — a hung
page must degrade fast, and must never change the block outcome.

OSS-synced: synthetic ids and example.* placeholders only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recording import CodeBlockActionRecording
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.actions.actions import Action, ActionType
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext

_RECORDING_PATH = "skyvern.forge.sdk.workflow.models.code_block_recording.app"


def _code_block() -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="sink_output",
        description="sink test output",
        output_parameter_id="op_sink",
        workflow_id="w_sink",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="sink_block", code="value = 'ok'", output_parameter=output_parameter)


def _recording(page: SimpleNamespace) -> CodeBlockActionRecording:
    recording = CodeBlockActionRecording(
        code_block=_code_block(),
        page=page,  # type: ignore[arg-type]
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
        workflow_run_context=FakeWorkflowRunContext(values={}, secrets={}),
    )
    recording._recording_enabled = True
    recording._task = SimpleNamespace(task_id="tsk_1")
    recording._step = SimpleNamespace(step_id="stp_1", order=0)
    recording._workflow_run_block = SimpleNamespace(workflow_run_block_id="wrb_1")
    return recording


@pytest.mark.asyncio
async def test_capture_uses_the_short_recording_budget_not_the_browser_default() -> None:
    # 20s of a dying page is charged to the block's execution timeout and buys nothing, so this
    # best-effort capture must not inherit BROWSER_SCREENSHOT_TIMEOUT_MS.
    screenshot = AsyncMock(return_value=b"png-bytes")
    page = SimpleNamespace(url="https://example.com/", screenshot=screenshot, is_closed=lambda: False)

    with patch(f"{_RECORDING_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", AsyncMock()):
        await _recording(page)._recorded_action_sink(Action(action_type=ActionType.CLICK))

    timeout_ms = screenshot.call_args.kwargs["timeout"]
    assert timeout_ms == settings.CODE_BLOCK_RECORDING_SCREENSHOT_TIMEOUT_MS
    assert timeout_ms < settings.BROWSER_SCREENSHOT_TIMEOUT_MS


@pytest.mark.asyncio
async def test_capture_failure_still_persists_the_action_and_reports_page_state() -> None:
    # The screenshot is the only thing lost: the timeline row must still be written, and the
    # warning must say whether the page was already closed or merely hung (the two are
    # indistinguishable from the Playwright TimeoutError alone).
    page = SimpleNamespace(
        url="https://example.com/",
        screenshot=AsyncMock(side_effect=TimeoutError("Page.screenshot: Timeout exceeded")),
        is_closed=lambda: True,
    )
    upsert = AsyncMock()

    with (
        patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert),
        capture_logs() as logs,
    ):
        await _recording(page)._recorded_action_sink(Action(action_type=ActionType.CLICK))

    upsert.assert_awaited_once()
    warning = next(log for log in logs if log["event"] == "Code block screenshot capture failed")
    assert warning["page_closed"] is True
