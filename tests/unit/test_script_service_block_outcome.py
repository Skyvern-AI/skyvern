"""Outcome records from cached blocks and loops."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.context_manager import BlockOutcome, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import ForLoopBlock
from skyvern.schemas.workflows import BlockStatus
from skyvern.services import script_service
from tests.unit.conftest import make_block_output_parameter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("complete_if_empty", "expected_status", "expected_reason"),
    [
        (True, BlockStatus.completed, None),
        (False, BlockStatus.terminated, "No iterable value found for the loop block"),
    ],
)
async def test_cached_loop_with_no_values_records_its_outcome(
    monkeypatch: pytest.MonkeyPatch,
    complete_if_empty: bool,
    expected_status: BlockStatus,
    expected_reason: str | None,
) -> None:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_cached",
        aws_client=AsyncMock(),
    )
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(
        script_service, "_create_workflow_block_run_and_task", AsyncMock(return_value=("wrb_rows", None, None))
    )
    monkeypatch.setattr(
        script_service,
        "_validate_and_get_output_parameter",
        AsyncMock(
            return_value=script_service.BlockValidationOutput(
                context=run_context,
                label="rows",
                output_parameter=make_block_output_parameter("rows_output"),
                input_parameters=[],
                workflow=MagicMock(),
                workflow_id="wf",
                workflow_run_id="wr_cached",
                organization_id="o_cached",
            )
        ),
    )
    monkeypatch.setattr(ForLoopBlock, "get_values_from_loop_variable_reference", AsyncMock(return_value=[]))

    async def drain() -> None:
        async for _ in script_service.loop([], complete_if_empty=complete_if_empty, label="rows"):
            pass

    if complete_if_empty:
        await drain()
    else:
        with pytest.raises(Exception, match="No iterable value"):
            await drain()

    assert context.get_block_outcome("rows") == BlockOutcome(
        status=expected_status, error_codes=[], failure_reason=expected_reason
    )


@pytest.mark.asyncio
async def test_cached_outcome_survives_output_parameter_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_cached",
        aws_client=AsyncMock(),
    )
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(script_service.skyvern_context, "current", lambda: run_context)
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(
        app.DATABASE.observer,
        "update_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(label="cached", error_codes=["picked"])),
    )
    monkeypatch.setattr(
        script_service, "_record_output_parameter_value", AsyncMock(side_effect=RuntimeError("Output unavailable"))
    )

    await script_service._update_workflow_block(
        "wrb_cached", BlockStatus.failed, label="cached", failure_reason="Failed", error_codes=["picked"]
    )

    assert context.get_block_outcome("cached") == BlockOutcome(
        status=BlockStatus.failed, error_codes=["picked"], failure_reason="Failed"
    )


@pytest.mark.asyncio
async def test_outcome_record_failure_does_not_skip_cached_output(monkeypatch: pytest.MonkeyPatch) -> None:
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(script_service.skyvern_context, "current", lambda: run_context)
    workflow_context = MagicMock()
    workflow_context.record_block_outcome.side_effect = RuntimeError("Outcome unavailable")
    monkeypatch.setattr(
        app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=workflow_context)
    )
    monkeypatch.setattr(
        app.DATABASE.observer,
        "update_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(label="cached", error_codes=[])),
    )
    record_output = AsyncMock()
    monkeypatch.setattr(script_service, "_record_output_parameter_value", record_output)

    await script_service._update_workflow_block("wrb_cached", BlockStatus.completed, label="cached")

    record_output.assert_awaited_once()
