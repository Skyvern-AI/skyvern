from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import BlockResult, BlockStatus


def _block(*, continue_on_failure: bool = False, block_type: str = "cloud_storage") -> SimpleNamespace:
    return SimpleNamespace(
        block_type=block_type,
        label="upload_results",
        continue_on_failure=continue_on_failure,
        output_parameter=object(),
    )


def _block_result(
    status: BlockStatus,
    *,
    failure_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        failure_reason=failure_reason,
        output_parameter_value=None,
    )


def _workflow_run(
    status: WorkflowRunStatus,
    *,
    failure_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        status=status,
        failure_reason=failure_reason,
    )


@pytest.mark.asyncio
async def test_execute_finally_block_returns_block_result(monkeypatch: pytest.MonkeyPatch) -> None:
    block_result = _block_result(BlockStatus.failed, failure_reason="upload failed")
    block = _block()
    block.get_all_parameters = Mock(return_value=[])
    block.execute_safe = AsyncMock(return_value=block_result)
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            finally_block_label=block.label,
            blocks=[block],
        )
    )
    workflow_run = _workflow_run(WorkflowRunStatus.completed)
    organization = SimpleNamespace(organization_id="org_test")
    register_parameters = AsyncMock()
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(register_block_parameters_for_workflow_run=register_parameters),
    )

    result = await WorkflowService()._execute_finally_block_if_configured(
        workflow=workflow,
        workflow_run=workflow_run,
        organization=organization,
        browser_session_id=None,
    )

    assert result == (block, block_result)
    register_parameters.assert_awaited_once_with("wr_test", [], organization)


@pytest.mark.asyncio
async def test_execute_finally_block_converts_exception_to_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _block()
    block.get_all_parameters = Mock(return_value=[])
    block.execute_safe = AsyncMock(side_effect=RuntimeError("upload exploded"))
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            finally_block_label=block.label,
            blocks=[block],
        )
    )
    workflow_run = _workflow_run(WorkflowRunStatus.completed)
    organization = SimpleNamespace(organization_id="org_test")
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(register_block_parameters_for_workflow_run=AsyncMock()),
    )

    result = await WorkflowService()._execute_finally_block_if_configured(
        workflow=workflow,
        workflow_run=workflow_run,
        organization=organization,
        browser_session_id=None,
    )

    assert result is not None
    returned_block, block_result = result
    assert returned_block is block
    assert isinstance(block_result, BlockResult)
    assert block_result.success is False
    assert block_result.status == BlockStatus.failed
    assert block_result.failure_reason == "Unexpected error: upload exploded"


@pytest.mark.asyncio
async def test_failed_finally_block_fails_successful_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_run = _workflow_run(WorkflowRunStatus.running)
    failed_run = _workflow_run(
        WorkflowRunStatus.failed,
        failure_reason="cloud_storage block failed. failure reason: upload failed",
    )
    service = WorkflowService()
    conditional_failure = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_failure)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=False),
        block_result=_block_result(BlockStatus.failed, failure_reason="upload failed"),
        workflow_run=workflow_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is failed_run
    assert final_status == WorkflowRunStatus.failed
    assert failure_reason == failed_run.failure_reason
    conditional_failure.assert_awaited_once()
    assert conditional_failure.await_args is not None
    assert conditional_failure.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert conditional_failure.await_args.kwargs["status"] == WorkflowRunStatus.failed
    assert (
        conditional_failure.await_args.kwargs["failure_reason"]
        == "cloud_storage block failed. failure reason: upload failed"
    )


@pytest.mark.asyncio
async def test_timed_out_finally_block_can_finalize_paused_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    paused_run = _workflow_run(WorkflowRunStatus.paused)
    failed_run = _workflow_run(
        WorkflowRunStatus.failed,
        failure_reason="human_interaction block timed out. Reason: human interaction timed out",
    )
    service = WorkflowService()
    conditional_failure = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_failure)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(block_type="human_interaction"),
        block_result=_block_result(BlockStatus.timed_out, failure_reason="human interaction timed out"),
        workflow_run=paused_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is failed_run
    assert final_status == WorkflowRunStatus.failed
    assert failure_reason == failed_run.failure_reason
    conditional_failure.assert_awaited_once()
    assert conditional_failure.await_args is not None
    assert conditional_failure.await_args.kwargs["status"] == WorkflowRunStatus.failed


@pytest.mark.asyncio
async def test_deferred_finally_outcome_applies_to_refreshed_paused_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paused_run = _workflow_run(WorkflowRunStatus.paused)
    failed_run = _workflow_run(
        WorkflowRunStatus.failed,
        failure_reason="human_interaction block timed out. Reason: human interaction timed out",
    )
    service = WorkflowService()
    conditional_failure = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_failure)

    result = await service._finalize_workflow_run_status(
        workflow_run_id="wr_test",
        workflow_run=paused_run,
        pre_finally_status=WorkflowRunStatus.failed,
        pre_finally_failure_reason=failed_run.failure_reason,
    )

    assert result is failed_run
    conditional_failure.assert_awaited_once()
    assert conditional_failure.await_args is not None
    assert conditional_failure.await_args.kwargs["status"] == WorkflowRunStatus.failed


@pytest.mark.asyncio
async def test_failed_continue_on_failure_finally_block_preserves_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run = _workflow_run(WorkflowRunStatus.running)
    service = WorkflowService()
    mark_failed = AsyncMock()
    monkeypatch.setattr(service, "mark_workflow_run_as_failed", mark_failed)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=True),
        block_result=_block_result(BlockStatus.failed, failure_reason="upload failed"),
        workflow_run=workflow_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is workflow_run
    assert final_status == WorkflowRunStatus.running
    assert failure_reason is None
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_canceled_finally_block_ignores_continue_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    canceled_run = _workflow_run(WorkflowRunStatus.canceled)
    service = WorkflowService()
    conditional_cancel = AsyncMock(return_value=canceled_run)
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_cancel)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=True),
        block_result=_block_result(BlockStatus.canceled),
        workflow_run=running_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is canceled_run
    assert final_status == WorkflowRunStatus.canceled
    assert failure_reason is None
    conditional_cancel.assert_awaited_once()
    assert conditional_cancel.await_args is not None
    assert conditional_cancel.await_args.kwargs["status"] == WorkflowRunStatus.canceled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_status", "workflow_status", "block_failure_reason", "workflow_failure_reason"),
    [
        (
            BlockStatus.timed_out,
            WorkflowRunStatus.failed,
            "upload timed out",
            "cloud_storage block timed out. Reason: upload timed out",
        ),
        (
            BlockStatus.terminated,
            WorkflowRunStatus.terminated,
            "upload terminated",
            "cloud_storage block terminated. Reason: upload terminated",
        ),
    ],
)
async def test_nonrecoverable_finally_block_sets_workflow_outcome(
    monkeypatch: pytest.MonkeyPatch,
    block_status: BlockStatus,
    workflow_status: WorkflowRunStatus,
    block_failure_reason: str,
    workflow_failure_reason: str,
) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    terminal_run = _workflow_run(
        workflow_status,
        failure_reason=workflow_failure_reason,
    )
    service = WorkflowService()
    conditional_update = AsyncMock(return_value=terminal_run)
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_update)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(),
        block_result=_block_result(block_status, failure_reason=block_failure_reason),
        workflow_run=running_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is terminal_run
    assert final_status == workflow_status
    assert failure_reason == workflow_failure_reason
    conditional_update.assert_awaited_once()
    assert conditional_update.await_args is not None
    assert conditional_update.await_args.kwargs["status"] == workflow_status
    assert conditional_update.await_args.kwargs["failure_reason"] == workflow_failure_reason


@pytest.mark.asyncio
async def test_failed_finally_block_preserves_concurrent_completed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_run = _workflow_run(WorkflowRunStatus.completed)
    service = WorkflowService()
    conditional_failure = AsyncMock()
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_failure)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=False),
        block_result=_block_result(BlockStatus.failed, failure_reason="cleanup also failed"),
        workflow_run=completed_run,
        pre_finally_status=WorkflowRunStatus.completed,
        pre_finally_failure_reason=None,
    )

    assert result_run is completed_run
    assert final_status == WorkflowRunStatus.completed
    assert failure_reason is None
    conditional_failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pre_finally_status", "pre_finally_failure_reason"),
    [
        (WorkflowRunStatus.failed, "primary block failed"),
        (WorkflowRunStatus.terminated, "primary block terminated"),
        (WorkflowRunStatus.timed_out, "workflow timed out"),
        (WorkflowRunStatus.canceled, "workflow canceled"),
    ],
)
async def test_failed_finally_block_preserves_earlier_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    pre_finally_status: WorkflowRunStatus,
    pre_finally_failure_reason: str,
) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    service = WorkflowService()
    mark_failed = AsyncMock()
    monkeypatch.setattr(service, "mark_workflow_run_as_failed", mark_failed)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=False),
        block_result=_block_result(BlockStatus.failed, failure_reason="cleanup also failed"),
        workflow_run=running_run,
        pre_finally_status=pre_finally_status,
        pre_finally_failure_reason=pre_finally_failure_reason,
    )

    assert result_run is running_run
    assert final_status == pre_finally_status
    assert failure_reason == pre_finally_failure_reason
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_canceled_finally_block_preserves_earlier_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    service = WorkflowService()
    conditional_cancel = AsyncMock()
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", conditional_cancel)

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(),
        block_result=_block_result(BlockStatus.canceled),
        workflow_run=running_run,
        pre_finally_status=WorkflowRunStatus.failed,
        pre_finally_failure_reason="primary block failed",
    )

    assert result_run is running_run
    assert final_status == WorkflowRunStatus.failed
    assert failure_reason == "primary block failed"
    conditional_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_cancellation_wins_over_finally_block_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    canceled_run = _workflow_run(WorkflowRunStatus.canceled)
    service = WorkflowService()
    conditional_failure = AsyncMock(return_value=None)
    monkeypatch.setattr(
        service,
        "_update_workflow_run_status_if_not_final",
        conditional_failure,
        raising=False,
    )
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=canceled_run))

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(continue_on_failure=False),
        block_result=_block_result(BlockStatus.failed, failure_reason="upload failed"),
        workflow_run=running_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is canceled_run
    assert final_status == WorkflowRunStatus.canceled
    assert failure_reason is None
    conditional_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_finally_status_lost_race_falls_back_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_run = _workflow_run(WorkflowRunStatus.running)
    service = WorkflowService()
    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(side_effect=RuntimeError("row disappeared")))

    result_run, final_status, failure_reason = await service._apply_finally_block_result(
        block=_block(),
        block_result=_block_result(BlockStatus.failed, failure_reason="upload failed"),
        workflow_run=running_run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert result_run is running_run
    assert final_status == WorkflowRunStatus.running
    assert failure_reason is None
