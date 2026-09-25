from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block, ForLoopBlock, WaitBlock
from skyvern.forge.sdk.workflow.models.terminate_block import TerminateBlock
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockStatus, TerminateBlockYAML
from tests.unit.conftest import make_block_output_parameter


def _terminate_block(reason: str) -> TerminateBlock:
    # continue_on_failure and next_loop_on_failure are set to prove the block ignores both.
    block = block_yaml_to_block(
        TerminateBlockYAML(label="stop", reason=reason, continue_on_failure=True, next_loop_on_failure=True),
        {"stop_output": make_block_output_parameter("stop_output")},
    )
    assert isinstance(block, TerminateBlock)
    return block


@pytest.fixture
def run_context(monkeypatch: pytest.MonkeyPatch) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="Terminate test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid",
        workflow_run_id="run-id",
        aws_client=AsyncMock(),
    )
    context.values["account_number"] = "A-42"
    monkeypatch.setattr(TerminateBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock())
    monkeypatch.setattr(app.DATABASE.observer, "update_workflow_run_block", AsyncMock())
    return context


@pytest.mark.asyncio
async def test_terminate_ends_the_run_with_the_rendered_reason(run_context: WorkflowRunContext) -> None:
    block = _terminate_block("ACCOUNT_NOT_FOUND: {{ account_number }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "ACCOUNT_NOT_FOUND: A-42"
    run_status, run_failure_reason, _ = WorkflowService._resolve_block_terminal_outcome(
        block=block, block_result=result
    )
    assert run_status == WorkflowRunStatus.terminated
    assert run_failure_reason is not None and "ACCOUNT_NOT_FOUND: A-42" in run_failure_reason


@pytest.mark.asyncio
async def test_blank_rendered_reason_still_terminates_with_a_fallback_reason(run_context: WorkflowRunContext) -> None:
    run_context.values["reason_code"] = "  "
    block = _terminate_block("{{ reason_code }}")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.terminated
    assert result.failure_reason == "Terminated by the stop block"
    assert result.can_continue_after_failure is False


@pytest.mark.asyncio
async def test_unrenderable_reason_still_stops_the_run(run_context: WorkflowRunContext) -> None:
    block = _terminate_block("{{ account_number ")

    result = await block.execute("run-id", "block-id", organization_id="org-id")

    assert result.status is BlockStatus.failed
    run_status, _, _ = WorkflowService._resolve_block_terminal_outcome(block=block, block_result=result)
    assert run_status == WorkflowRunStatus.failed


@pytest.mark.asyncio
async def test_terminate_inside_a_loop_stops_the_loop_helper_on_that_iteration(run_context: WorkflowRunContext) -> None:
    stop = _terminate_block("ACCOUNT_NOT_FOUND: {{ account_number }}")
    pause = WaitBlock(
        label="pause", output_parameter=make_block_output_parameter(), wait_sec=1, continue_on_failure=True
    )
    loop = ForLoopBlock(
        label="each_account",
        output_parameter=make_block_output_parameter(),
        loop_blocks=[stop, pause],
        continue_on_failure=True,
        next_loop_on_failure=True,
    )
    pause_result = await pause.build_block_result(success=True, failure_reason=None, status=BlockStatus.completed)

    async def run_child(self: Block, workflow_run_id: str, **kwargs: Any) -> Any:
        # The terminate block runs for real; the wait block is stubbed so the test needs no browser.
        if isinstance(self, TerminateBlock):
            return await self.execute(workflow_run_id, "stop-block-id", organization_id=kwargs["organization_id"])
        return pause_result

    with (
        patch.object(Block, "execute_safe", autospec=True, side_effect=run_child),
        patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
        patch.object(ForLoopBlock, "_snapshot_loop_baseline_pages", new_callable=AsyncMock, return_value=None),
        patch.object(ForLoopBlock, "_reset_browser_tabs_for_iteration", new_callable=AsyncMock),
        patch.object(ForLoopBlock, "_persist_partial_loop_output", new_callable=AsyncMock),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
    ):
        mock_skyvern_ctx.current.return_value = None
        run_context.cancel_failure_evidence_capture = AsyncMock()  # type: ignore[method-assign]
        result = await loop.execute_loop_helper(
            workflow_run_id="run-id",
            workflow_run_block_id="loop-block-id",
            workflow_run_context=run_context,
            loop_over_values=["A-1", "A-42"],
            organization_id="org-id",
        )

    # The first iteration's terminate ended the loop: no wait block ran and no second iteration started.
    assert [r.status for r in result.block_outputs] == [BlockStatus.terminated]
    assert len(result.outputs_with_loop_values) == 1
    assert result.can_continue_after_failure() is False
    loop_status, _, _ = result.resolve_status(parent_next_loop_on_failure=True)
    assert loop_status is BlockStatus.terminated


@pytest.mark.parametrize("reason", ["", "   "])
def test_blank_reason_is_rejected_at_the_schema(reason: str) -> None:
    with pytest.raises(ValidationError):
        TerminateBlockYAML(label="stop", reason=reason)
