"""Characterization tests for WorkflowService._execute_single_block.

Pin current behavior of each phase (final-state guard, login profile prep,
script execution, agent fallback gate, cache tracking, conditional metadata)
before carving the method into per-phase helpers. Expected to pass unchanged
before and after every extraction.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import BrowserSeedSource
from skyvern.forge.sdk.workflow.context_manager import BlockOutcome, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    BranchCondition,
    CodeBlock,
    ConditionalBlock,
    ForLoopBlock,
    JinjaBranchCriteria,
    LoginBlock,
    NavigationBlock,
    WaitBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import (
    DebugSessionProfileDecision,
    WorkflowRunDispatchStopped,
    WorkflowService,
)
from skyvern.schemas.scripts import ScriptBlock
from skyvern.schemas.workflows import BlockResult, BlockStatus
from skyvern.services import script_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id",
        key=key,
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


def _navigation_block(label: str) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_parameter(f"{label}_output"),
    )


def _login_block(label: str, url: str) -> LoginBlock:
    return LoginBlock(
        url=url,
        label=label,
        title=label,
        navigation_goal="log in",
        output_parameter=_output_parameter(f"{label}_output"),
    )


def _code_block(label: str, *, continue_on_failure: bool = False) -> CodeBlock:
    return CodeBlock(
        label=label,
        code="pass",
        continue_on_failure=continue_on_failure,
        output_parameter=_output_parameter(f"{label}_output"),
    )


def _workflow(run_with: str = "agent") -> MagicMock:
    workflow = MagicMock()
    workflow.run_with = run_with
    workflow.code_version = None
    workflow.adaptive_caching = False
    workflow.generate_script_on_terminal = False
    workflow.workflow_permanent_id = "wpid_test"
    return workflow


def _adaptive_workflow() -> MagicMock:
    # run_with="code" + code_version>=2 makes is_adaptive_caching(...) return True.
    workflow = _workflow(run_with="code")
    workflow.code_version = 2
    return workflow


def _workflow_run(ai_fallback: bool | None = None) -> MagicMock:
    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_test"
    workflow_run.status = WorkflowRunStatus.running
    workflow_run.run_with = None
    workflow_run.ai_fallback = ai_fallback
    workflow_run.start_fresh_browser = False
    return workflow_run


def _script_block(label: str, run_signature: str, requires_agent: bool = False) -> ScriptBlock:
    now = datetime.now(UTC)
    return ScriptBlock(
        script_block_id="sb_1",
        organization_id="org_test",
        script_id="s_1",
        script_revision_id="sr_1",
        script_block_label=label,
        run_signature=run_signature,
        requires_agent=requires_agent,
        created_at=now,
        modified_at=now,
    )


def _completed_result(block: NavigationBlock | LoginBlock | ConditionalBlock | ForLoopBlock) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        status=BlockStatus.completed,
        workflow_run_block_id=f"wrb_{block.label}",
    )


async def _run_single_block(
    service: WorkflowService,
    block: NavigationBlock | LoginBlock | ConditionalBlock | ForLoopBlock,
    *,
    workflow: MagicMock | None = None,
    workflow_run: MagicMock | None = None,
    is_script_run: bool = False,
    script_blocks_by_label: dict | None = None,
    loaded_script_module: Any = None,
    blocks_to_update: set[str] | None = None,
) -> tuple:
    organization = MagicMock()
    organization.organization_id = "org_test"
    return await service._execute_single_block(
        workflow=workflow if workflow is not None else _workflow(),
        block=block,
        block_idx=0,
        blocks_cnt=1,
        workflow_run=workflow_run if workflow_run is not None else _workflow_run(),
        organization=organization,
        workflow_run_id="wr_test",
        browser_session_id=None,
        script_blocks_by_label=script_blocks_by_label if script_blocks_by_label is not None else {},
        loaded_script_module=loaded_script_module,
        is_script_run=is_script_run,
        blocks_to_update=blocks_to_update if blocks_to_update is not None else set(),
    )


@pytest.fixture(autouse=True)
def _stub_run_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # The method opens by re-fetching the run; returning None keeps the passed-in run.
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "register_block_parameters_for_workflow_run", AsyncMock())
    # get_all_parameters needs a sync context object; the stub app's auto-AsyncMock returns a coroutine.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=MagicMock()))

    @asynccontextmanager
    async def admit_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield _workflow_run()

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_dispatch)


@pytest.mark.asyncio
async def test_dispatch_waits_for_successful_admission_scope_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    scope_exiting = asyncio.Event()
    release_scope = asyncio.Event()
    executor_started = asyncio.Event()

    @asynccontextmanager
    async def admit_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield _workflow_run()
        scope_exiting.set()
        await release_scope.wait()

    async def execute() -> str:
        executor_started.set()
        return "executed"

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_dispatch)
    dispatch_task = asyncio.create_task(WorkflowService()._dispatch_workflow_run_block("wr_test", execute))

    await scope_exiting.wait()
    next_turn = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(next_turn.set_result, None)
    await next_turn
    assert executor_started.is_set() is False

    release_scope.set()
    assert await dispatch_task == "executed"
    assert executor_started.is_set() is True


@pytest.mark.asyncio
async def test_dispatch_cancellation_cancels_dormant_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    scope_exiting = asyncio.Event()
    release_scope = asyncio.Event()
    execute = AsyncMock()

    @asynccontextmanager
    async def admit_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield _workflow_run()
        scope_exiting.set()
        await release_scope.wait()

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_dispatch)
    dispatch_task = asyncio.create_task(WorkflowService()._dispatch_workflow_run_block("wr_test", execute))
    await scope_exiting.wait()

    dispatch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch_task
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_propagates_executor_exception_once(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = AsyncMock(side_effect=RuntimeError("executor failed"))

    with pytest.raises(RuntimeError, match="executor failed"):
        await WorkflowService()._dispatch_workflow_run_block("wr_test", execute)

    execute.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_dispatch_scope_failure_cancels_dormant_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = AsyncMock(return_value="must not run")

    @asynccontextmanager
    async def admit_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield _workflow_run()
        raise RuntimeError("commit failed")

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_dispatch)

    with pytest.raises(RuntimeError, match="commit failed"):
        await WorkflowService()._dispatch_workflow_run_block("wr_test", execute)

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_initial_read_prevents_agent_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled

    @asynccontextmanager
    async def deny_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield canceled_run

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", deny_dispatch)
    execute_safe = AsyncMock()
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(WorkflowService(), _navigation_block("nav"))

    assert workflow_run is canceled_run
    assert block_result is None
    assert should_stop is True
    execute_safe.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_before_script_handoff_prevents_script_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled

    @asynccontextmanager
    async def deny_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield canceled_run

    script_calls: list[str] = []
    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", deny_dispatch)
    observer_read = AsyncMock()
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", observer_read)

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(
        WorkflowService(),
        _navigation_block("cached_nav"),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "record_dispatch()")},
        loaded_script_module=SimpleNamespace(record_dispatch=lambda: script_calls.append("called")),
    )

    assert workflow_run is canceled_run
    assert block_result is None
    assert should_stop is True
    assert script_calls == []
    observer_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_before_script_fallback_prevents_agent_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    running_run = _workflow_run()
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled
    admissions = iter((running_run, canceled_run))

    @asynccontextmanager
    async def admit_then_deny(_: str) -> AsyncIterator[MagicMock]:
        yield next(admissions)

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_then_deny)
    execute_safe = AsyncMock()
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(
        WorkflowService(),
        _navigation_block("cached_nav"),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 / 0")},
        loaded_script_module=SimpleNamespace(),
    )

    assert workflow_run is canceled_run
    assert block_result is None
    assert should_stop is True
    execute_safe.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_denial_returns_typed_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled
    execute = AsyncMock()

    @asynccontextmanager
    async def deny_dispatch(_: str) -> AsyncIterator[MagicMock]:
        yield canceled_run

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", deny_dispatch)

    result = await WorkflowService()._dispatch_workflow_run_block("wr_test", execute)

    assert result == WorkflowRunDispatchStopped(workflow_run=canceled_run)
    execute.assert_not_awaited()


class _PausableAwait:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args: Any, **kwargs: Any) -> bytes:
        self.entered.set()
        await self.release.wait()
        return b"png"


def _wire_real_execute_safe(
    monkeypatch: pytest.MonkeyPatch,
    status_holder: list[WorkflowRunStatus],
    *,
    execute: AsyncMock | None = None,
    pause_point: str | None = None,
) -> _PausableAwait:
    pause = _PausableAwait()

    @asynccontextmanager
    async def admit_from_holder(_: str) -> AsyncIterator[MagicMock]:
        admitted = _workflow_run()
        admitted.status = status_holder[0]
        yield admitted

    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit_from_holder)
    workflow_run_block = MagicMock()
    workflow_run_block.workflow_run_block_id = "wrb_nav"
    monkeypatch.setattr(app.DATABASE.observer, "create_workflow_run_block", AsyncMock(return_value=workflow_run_block))
    context = MagicMock()
    context.cancel_failure_evidence_capture = pause if pause_point == "capture" else AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    browser_state = MagicMock()
    browser_state.take_fullpage_screenshot = pause if pause_point == "screenshot" else AsyncMock(return_value=b"png")
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=browser_state))
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER,
        "create_workflow_run_block_artifact",
        pause if pause_point == "artifact" else AsyncMock(),
    )
    monkeypatch.setattr(Block, "_generate_workflow_run_block_description", AsyncMock())
    if execute is not None:
        monkeypatch.setattr(NavigationBlock, "execute", execute)
    return pause


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pause_point", "terminal_status"),
    [
        ("capture", WorkflowRunStatus.canceled),
        ("screenshot", WorkflowRunStatus.canceled),
        ("artifact", WorkflowRunStatus.canceled),
        ("capture", WorkflowRunStatus.failed),
        ("capture", WorkflowRunStatus.timed_out),
    ],
)
async def test_terminal_status_during_pre_effect_setup_skips_execute(
    monkeypatch: pytest.MonkeyPatch,
    pause_point: str,
    terminal_status: WorkflowRunStatus,
) -> None:
    status_holder = [WorkflowRunStatus.running]
    execute = AsyncMock()
    pause = _wire_real_execute_safe(monkeypatch, status_holder, execute=execute, pause_point=pause_point)
    terminal_run = _workflow_run()
    terminal_run.status = terminal_status
    conditional_cancel = AsyncMock(return_value=terminal_run if terminal_status == WorkflowRunStatus.canceled else None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", conditional_cancel)

    async def get_run_from_holder(*args: Any, **kwargs: Any) -> MagicMock | None:
        return None if status_holder[0] == WorkflowRunStatus.running else terminal_run

    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_run_from_holder)
    monkeypatch.setattr(app.AGENT_FUNCTION, "record_run_duration", AsyncMock())

    run_task = asyncio.create_task(_run_single_block(WorkflowService(), _navigation_block("nav")))
    await pause.entered.wait()
    status_holder[0] = terminal_status
    pause.release.set()
    workflow_run, _, block_result, should_stop, _ = await run_task

    assert execute.await_count == 0
    assert block_result is not None
    assert block_result.status == BlockStatus(terminal_status.value)
    assert should_stop is True
    assert workflow_run.status == terminal_status
    conditional_cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_run_executes_block_once_after_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _navigation_block("nav")
    completed = _completed_result(block)
    execute = AsyncMock(return_value=completed)
    _wire_real_execute_safe(monkeypatch, [WorkflowRunStatus.running], execute=execute)

    _, _, block_result, should_stop, _ = await _run_single_block(WorkflowService(), block)

    assert execute.await_count == 1
    assert block_result is completed
    assert should_stop is False


@pytest.mark.asyncio
async def test_terminal_status_after_execute_entered_keeps_in_flight_result(monkeypatch: pytest.MonkeyPatch) -> None:
    status_holder = [WorkflowRunStatus.running]
    block = _navigation_block("nav")
    completed = _completed_result(block)

    async def execute_then_cancel(*args: Any, **kwargs: Any) -> BlockResult:
        status_holder[0] = WorkflowRunStatus.canceled
        return completed

    execute = AsyncMock(side_effect=execute_then_cancel)
    _wire_real_execute_safe(monkeypatch, status_holder, execute=execute)

    _, _, block_result, should_stop, _ = await _run_single_block(WorkflowService(), block)

    assert execute.await_count == 1
    assert block_result is completed
    assert should_stop is False


@pytest.mark.asyncio
async def test_returns_early_when_run_already_final(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    final_run = MagicMock()
    final_run.status = WorkflowRunStatus.completed
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=final_run))
    execute_safe = AsyncMock()
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)

    workflow_run, _, block_result, should_stop, branch_metadata = await _run_single_block(
        service, _navigation_block("nav")
    )

    assert workflow_run is final_run
    assert block_result is None
    assert should_stop is True
    assert branch_metadata is None
    execute_safe.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_path_passes_through_block_result(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _navigation_block("nav")
    completed = _completed_result(block)
    execute_safe = AsyncMock(return_value=completed)
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)

    blocks_to_update: set[str] = set()
    _, returned_blocks, block_result, should_stop, branch_metadata = await _run_single_block(
        service, block, blocks_to_update=blocks_to_update
    )

    assert block_result is completed
    assert should_stop is False
    assert branch_metadata is None
    assert returned_blocks is blocks_to_update
    assert returned_blocks == set()
    execute_safe.assert_awaited_once_with(
        workflow_run_id="wr_test",
        parent_workflow_run_block_id=None,
        organization_id="org_test",
        browser_session_id=None,
    )


@pytest.mark.asyncio
async def test_missing_block_result_marks_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(return_value=None))
    failed_run = MagicMock()
    mark_failed = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed_if_not_final", mark_failed)

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(service, _navigation_block("nav"))

    mark_failed.assert_awaited_once_with(workflow_run_id="wr_test", failure_reason="Block result is None")
    assert workflow_run is failed_run
    assert block_result is None
    assert should_stop is True


@pytest.mark.asyncio
async def test_missing_result_cannot_overwrite_concurrent_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(return_value=None))
    conditional_failure = AsyncMock(return_value=None)
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed_if_not_final", conditional_failure)
    monkeypatch.setattr(service, "_current_row_after_lost_finalize", AsyncMock(return_value=canceled_run))

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(service, _navigation_block("nav"))

    assert workflow_run is canceled_run
    assert block_result is None
    assert should_stop is True
    conditional_failure.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_status", "conditional_method"),
    [
        (BlockStatus.failed, "mark_workflow_run_as_failed_if_not_final"),
        (BlockStatus.terminated, "mark_workflow_run_as_terminated_if_not_final"),
    ],
)
async def test_block_terminal_outcome_cannot_overwrite_concurrent_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    block_status: BlockStatus,
    conditional_method: str,
) -> None:
    service = WorkflowService()
    block = _navigation_block("nav")
    block_result = BlockResult(
        success=False,
        failure_reason="block stopped",
        output_parameter=block.output_parameter,
        status=block_status,
    )
    canceled_run = _workflow_run()
    canceled_run.status = WorkflowRunStatus.canceled
    conditional_terminal = AsyncMock(return_value=None)
    monkeypatch.setattr(service, conditional_method, conditional_terminal)
    monkeypatch.setattr(service, "_current_row_after_lost_finalize", AsyncMock(return_value=canceled_run))

    workflow_run, should_stop = await service._handle_block_result_status(
        block=block,
        block_idx=0,
        blocks_cnt=1,
        block_result=block_result,
        workflow_run=_workflow_run(),
        workflow_run_id="wr_test",
    )

    assert workflow_run is canceled_run
    assert should_stop is True
    conditional_terminal.assert_awaited_once()


@pytest.mark.asyncio
async def test_code_render_failure_stops_despite_continue_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _code_block("code", continue_on_failure=True)
    block_result = BlockResult(
        success=False,
        failure_reason="Failed to format CodeBlock parameters.",
        output_parameter=block.output_parameter,
        status=BlockStatus.failed,
        can_continue_after_failure=False,
    )
    failed_run = MagicMock()
    mark_failed = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(service, "mark_workflow_run_as_failed_if_not_final", mark_failed)

    workflow_run, should_stop = await service._handle_block_result_status(
        block=block,
        block_idx=0,
        blocks_cnt=1,
        block_result=block_result,
        workflow_run=_workflow_run(),
        workflow_run_id="wr_test",
    )

    assert workflow_run is failed_run
    assert should_stop is True
    mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_code_failure_still_honors_continue_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _code_block("code", continue_on_failure=True)
    block_result = BlockResult(
        success=False,
        failure_reason="Code failed after execution.",
        output_parameter=block.output_parameter,
        status=BlockStatus.failed,
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(service, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    running = _workflow_run()

    workflow_run, should_stop = await service._handle_block_result_status(
        block=block,
        block_idx=0,
        blocks_cnt=1,
        block_result=block_result,
        workflow_run=running,
        workflow_run_id="wr_test",
    )

    assert workflow_run is running
    assert should_stop is False
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_exception_marks_run_failed_with_block_type_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(side_effect=RuntimeError("boom")))
    failed_run = MagicMock()
    mark_failed = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed_if_not_final", mark_failed)

    workflow_run, _, _, should_stop, _ = await _run_single_block(service, _navigation_block("nav"))

    mark_failed.assert_awaited_once_with(
        workflow_run_id="wr_test",
        failure_reason="navigation block failed. failure reason: Unexpected error: boom",
    )
    assert workflow_run is failed_run
    assert should_stop is True


@pytest.mark.asyncio
async def test_script_run_tracks_uncached_completed_block_for_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _navigation_block("uncached_nav")
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(return_value=_completed_result(block)))

    blocks_to_update: set[str] = set()
    _, returned_blocks, _, should_stop, _ = await _run_single_block(
        service, block, is_script_run=True, blocks_to_update=blocks_to_update
    )

    assert returned_blocks == {"uncached_nav"}
    assert should_stop is False


@pytest.mark.asyncio
async def test_ai_fallback_disabled_keeps_script_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _navigation_block("cached_nav")
    execute_safe = AsyncMock()
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)
    monkeypatch.setattr(NavigationBlock, "_apply_workflow_system_prompt", lambda self, ctx: None)
    failed_run = MagicMock()
    mark_failed = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed_if_not_final", mark_failed)

    workflow_run, _, _, should_stop, _ = await _run_single_block(
        service,
        block,
        workflow_run=_workflow_run(ai_fallback=False),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 / 0")},
        loaded_script_module=SimpleNamespace(),
    )

    execute_safe.assert_not_awaited()
    mark_failed.assert_awaited_once_with(
        workflow_run_id="wr_test",
        failure_reason="Script error (ZeroDivisionError): division by zero",
    )
    assert workflow_run is failed_run
    assert should_stop is True


@pytest.mark.asyncio
async def test_script_success_skips_agent_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _navigation_block("cached_nav")
    execute_safe = AsyncMock()
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)
    monkeypatch.setattr(NavigationBlock, "_apply_workflow_system_prompt", lambda self, ctx: None)
    script_row = SimpleNamespace(
        label="cached_nav",
        created_at=datetime.now(UTC),
        status=BlockStatus.completed,
        failure_reason=None,
        output={"ok": True},
        workflow_run_block_id="wrb_script_1",
    )
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[script_row]))

    _, returned_blocks, block_result, should_stop, _ = await _run_single_block(
        service,
        block,
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 + 1")},
        loaded_script_module=SimpleNamespace(),
    )

    execute_safe.assert_not_awaited()
    assert block_result is not None
    assert block_result.success is True
    assert block_result.status == BlockStatus.completed
    assert block_result.workflow_run_block_id == "wrb_script_1"
    assert returned_blocks == set()
    assert should_stop is False


def _loop_holding_a_conditional() -> ForLoopBlock:
    conditional = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("cond_output"),
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ current_value }}"), next_block_label="nav_a"),
            BranchCondition(is_default=True, next_block_label="nav_b"),
        ],
    )
    return ForLoopBlock(
        label="loop",
        output_parameter=_output_parameter("loop_output"),
        loop_blocks=[conditional, _navigation_block("nav_a"), _navigation_block("nav_b")],
    )


async def _run_cached_loop_holding_a_conditional(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], AsyncMock, set]:
    """Script run of a loop whose script and executed branch are already cached; nav_b never ran."""
    block = _loop_holding_a_conditional()
    script_calls: list[str] = []
    execute_safe = AsyncMock(return_value=_completed_result(block))
    monkeypatch.setattr(ForLoopBlock, "execute_safe", execute_safe)
    # Only reached if the cached script runs and fails; stubbed so that regression fails on the assertions.
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed_if_not_final", AsyncMock())

    _, blocks_to_update, _, _, _ = await _run_single_block(
        WorkflowService(),
        block,
        workflow_run=_workflow_run(ai_fallback=False),
        is_script_run=True,
        script_blocks_by_label={
            "loop": _script_block("loop", "record_dispatch()"),
            "nav_a": _script_block("nav_a", "record_dispatch()"),
        },
        loaded_script_module=SimpleNamespace(record_dispatch=lambda: script_calls.append("called")),
    )
    return script_calls, execute_safe, blocks_to_update


@pytest.mark.asyncio
async def test_cached_loop_holding_a_conditional_runs_through_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    script_calls, execute_safe, _ = await _run_cached_loop_holding_a_conditional(monkeypatch)

    assert script_calls == []
    execute_safe.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_only_loop_logs_which_child_kept_it_off_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    with capture_logs() as logs:
        await _run_cached_loop_holding_a_conditional(monkeypatch)

    [mode_resolved] = [log for log in logs if log["event"] == "Block execution mode resolved"]
    assert mode_resolved["execution_mode"] == "ai"
    assert mode_resolved["engine_only_child_types"] == ["conditional"]


@pytest.mark.asyncio
async def test_engine_only_loop_does_not_queue_its_children_for_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    # nav_b sits on a branch that did not run, so codegen has no actions to mint it from:
    # queueing it would regenerate the script on every run.
    _, _, blocks_to_update = await _run_cached_loop_holding_a_conditional(monkeypatch)

    assert blocks_to_update == set()


@pytest.mark.asyncio
async def test_conditional_block_returns_branch_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("cond_output"),
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ flag }}"), next_block_label="next"),
            BranchCondition(is_default=True, next_block_label=None),
        ],
    )
    metadata = {"branch_taken": "next", "branch_index": 0, "next_block_label": "next"}
    result = BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        output_parameter_value=metadata,
        status=BlockStatus.completed,
        workflow_run_block_id="wrb_cond",
    )
    monkeypatch.setattr(ConditionalBlock, "execute_safe", AsyncMock(return_value=result))

    _, _, block_result, should_stop, branch_metadata = await _run_single_block(service, block)

    assert branch_metadata == metadata
    assert block_result is result
    assert should_stop is False


@pytest.mark.asyncio
async def test_login_block_without_saved_profile_keeps_navigation_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _login_block("login", "https://example.com/login")
    monkeypatch.setattr(WorkflowService, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_resolve_login_block_browser_profile_id", AsyncMock(return_value=None))
    execute_safe = AsyncMock(return_value=_completed_result(block))
    monkeypatch.setattr(LoginBlock, "execute_safe", execute_safe)

    _, _, _, should_stop, _ = await _run_single_block(service, block)

    assert block.navigation_goal == "log in"
    execute_safe.assert_awaited_once()
    assert should_stop is False


@pytest.mark.asyncio
async def test_login_block_with_saved_profile_rewrites_goal_and_persists_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkflowService()
    block = _login_block("login", "https://example.com/home")
    monkeypatch.setattr(WorkflowService, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_resolve_login_block_browser_profile_id", AsyncMock(return_value="bp_123"))
    monkeypatch.setattr(
        WorkflowService,
        "_evaluate_debug_session_profile_decision",
        AsyncMock(return_value=DebugSessionProfileDecision(attach_browser_session_id=None, incompatible_reason=None)),
    )
    update_run = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_run)
    page = AsyncMock()
    page.url = "https://example.com/home"
    browser_state = AsyncMock()
    browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.browser_artifacts = BrowserArtifacts(applied_browser_profile_id="bp_123")
    # No browser open yet: the credential profile loads into a fresh browser (the seed path this test
    # covers). A pre-existing browser would instead degrade to fresh (see the cached-browser guard).
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *a, **k: None)
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", AsyncMock(return_value=browser_state))
    execute_safe = AsyncMock(return_value=_completed_result(block))
    monkeypatch.setattr(LoginBlock, "execute_safe", execute_safe)

    await _run_single_block(service, block)

    update_run.assert_awaited_once_with(
        workflow_run_id="wr_test",
        browser_profile_id="bp_123",
        browser_seed_source=BrowserSeedSource.credential,
    )
    assert block.navigation_goal is not None
    assert block.navigation_goal.startswith("A saved browser session has been loaded.")
    assert "Original goal: log in" in block.navigation_goal
    execute_safe.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_block_does_not_clobber_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkflowService()
    block = _login_block("login", "https://example.com/home")
    monkeypatch.setattr(WorkflowService, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(
        WorkflowService, "_resolve_login_block_browser_profile_id", AsyncMock(return_value="bp_credential")
    )
    decision = AsyncMock(
        return_value=DebugSessionProfileDecision(attach_browser_session_id=None, incompatible_reason=None)
    )
    monkeypatch.setattr(WorkflowService, "_evaluate_debug_session_profile_decision", decision)
    update_run = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update_run)
    get_or_create = AsyncMock()
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", get_or_create)
    execute_safe = AsyncMock(return_value=_completed_result(block))
    monkeypatch.setattr(LoginBlock, "execute_safe", execute_safe)

    workflow_run = _workflow_run()
    workflow_run.browser_seed_source = BrowserSeedSource.override

    await _run_single_block(service, block, workflow_run=workflow_run)

    # An explicit per-run override must survive the login block: no re-stamp, no credential boot, no
    # goal rewrite — just a normal login into the overridden profile.
    update_run.assert_not_awaited()
    decision.assert_not_awaited()
    get_or_create.assert_not_awaited()
    assert block.navigation_goal == "log in"
    execute_safe.assert_awaited_once()


@pytest.mark.asyncio
async def test_adaptive_caching_script_failure_records_and_updates_fallback_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkflowService()
    block = _navigation_block("cached_nav")
    monkeypatch.setattr(NavigationBlock, "_apply_workflow_system_prompt", lambda self, ctx: None)
    # Script code runs cleanly ("1 + 1") but the recorded block failed, so the
    # method resets the result and records a fallback episode before AI retry.
    failed_row = SimpleNamespace(
        label="cached_nav",
        created_at=datetime.now(UTC),
        status=BlockStatus.failed,
        failure_reason="xpath drift",
        output=None,
        workflow_run_block_id="wrb_script_1",
    )
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[failed_row]))
    record_episode = AsyncMock(return_value=("ep_1", None))
    monkeypatch.setattr(WorkflowService, "_record_fallback_episode", record_episode)
    monkeypatch.setattr(WorkflowService, "_mark_script_fallback_triggered", AsyncMock())
    ai_result = BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        status=BlockStatus.completed,
    )
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(return_value=ai_result))
    update_episode = AsyncMock()
    monkeypatch.setattr(app.DATABASE.scripts, "update_fallback_episode", update_episode)

    _, _, block_result, should_stop, _ = await _run_single_block(
        service,
        block,
        workflow=_adaptive_workflow(),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 + 1")},
        loaded_script_module=SimpleNamespace(),
    )

    record_episode.assert_awaited_once()
    assert record_episode.await_args.kwargs["error_message"].startswith("Script completed but block failed:")
    update_episode.assert_awaited_once()
    assert update_episode.await_args.kwargs["episode_id"] == "ep_1"
    assert update_episode.await_args.kwargs["fallback_succeeded"] is True
    assert block_result is ai_result
    assert should_stop is False


@pytest.mark.asyncio
async def test_non_adaptive_script_failure_skips_fallback_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = _navigation_block("cached_nav")
    monkeypatch.setattr(NavigationBlock, "_apply_workflow_system_prompt", lambda self, ctx: None)
    failed_row = SimpleNamespace(
        label="cached_nav",
        created_at=datetime.now(UTC),
        status=BlockStatus.failed,
        failure_reason="xpath drift",
        output=None,
        workflow_run_block_id="wrb_script_1",
    )
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[failed_row]))
    record_episode = AsyncMock(return_value=("ep_1", None))
    monkeypatch.setattr(WorkflowService, "_record_fallback_episode", record_episode)
    monkeypatch.setattr(WorkflowService, "_mark_script_fallback_triggered", AsyncMock())
    execute_safe = AsyncMock(
        return_value=BlockResult(success=True, output_parameter=block.output_parameter, status=BlockStatus.completed)
    )
    monkeypatch.setattr(NavigationBlock, "execute_safe", execute_safe)
    update_episode = AsyncMock()
    monkeypatch.setattr(app.DATABASE.scripts, "update_fallback_episode", update_episode)

    # Default agent workflow => is_adaptive_caching(...) is False, so neither the
    # create nor the update fallback-episode path fires even though the script failed.
    await _run_single_block(
        service,
        block,
        workflow=_workflow(),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 + 1")},
        loaded_script_module=SimpleNamespace(),
    )

    # The script path must actually have run (script executed, DB row says failed, mid-block
    # fallback to the agent) for this test to say anything about the non-adaptive-caching skip;
    # otherwise these assertions would hold vacuously because the script path was never entered.
    execute_safe.assert_awaited_once()
    record_episode.assert_not_awaited()
    update_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_adaptive_caching_conditional_records_conditional_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("cond_output"),
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ flag }}"), next_block_label="next"),
            BranchCondition(is_default=True, next_block_label=None),
        ],
    )
    metadata = {
        "branch_taken": "next",
        "branch_index": 0,
        "next_block_label": "next",
        "evaluations": [{"branch_index": 0, "result": True}],
    }
    result = BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        output_parameter_value=metadata,
        status=BlockStatus.completed,
        workflow_run_block_id="wrb_cond",
    )
    monkeypatch.setattr(ConditionalBlock, "execute_safe", AsyncMock(return_value=result))
    monkeypatch.setattr(WorkflowService, "_mark_script_fallback_triggered", AsyncMock())
    create_episode = AsyncMock(return_value=SimpleNamespace(episode_id="cep_1"))
    update_episode = AsyncMock()
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", create_episode)
    monkeypatch.setattr(app.DATABASE.scripts, "update_fallback_episode", update_episode)

    # requires_agent forces the agent path (block_requires_agent True), which is
    # the gate that opens conditional-episode recording under adaptive caching.
    _, _, block_result, should_stop, branch_metadata = await _run_single_block(
        service,
        block,
        workflow=_adaptive_workflow(),
        is_script_run=True,
        script_blocks_by_label={"cond": _script_block("cond", "True", requires_agent=True)},
    )

    create_episode.assert_awaited_once()
    assert create_episode.await_args.kwargs["fallback_type"] == "conditional_agent"
    assert create_episode.await_args.kwargs["agent_actions"]["block_type"] == "conditional"
    update_episode.assert_awaited_once_with(episode_id="cep_1", organization_id="org_test", fallback_succeeded=True)
    assert branch_metadata == metadata
    assert block_result is result
    assert should_stop is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_expression", "plan", "branch_taken", "records_episode"),
    [
        pytest.param("{{ plan == 'pro' }}", "pro", "paid", True, id="branch_matched"),
        pytest.param("{{ plan == 'pro' }}", "basic", "free", True, id="every_condition_false_takes_default"),
        pytest.param("{{ plan == 'pro' || plan == 'team' }}", "basic", "free", False, id="errored_takes_default"),
        pytest.param("{{ plan == 'pro' || plan == 'team' }}", "trial", "trial", False, id="errored_then_later_match"),
    ],
)
async def test_conditional_episode_is_recorded_only_when_every_walked_branch_was_evaluated(
    monkeypatch: pytest.MonkeyPatch,
    first_expression: str,
    plan: str,
    branch_taken: str,
    records_episode: bool,
) -> None:
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("cond_output"),
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression=first_expression), next_block_label="paid"),
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ plan == 'trial' }}"), next_block_label="trial"),
            BranchCondition(is_default=True, next_block_label="free"),
        ],
    )
    # The block itself runs, so the episode gate is exercised against the output a real evaluation writes.
    _wire_real_execute_safe(monkeypatch, [WorkflowRunStatus.running])
    monkeypatch.setattr(
        app.WORKFLOW_CONTEXT_MANAGER,
        "get_workflow_run_context",
        MagicMock(return_value=FakeWorkflowRunContext(values={"plan": plan})),
    )
    monkeypatch.setattr(ConditionalBlock, "record_output_parameter_value", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_mark_script_fallback_triggered", AsyncMock())
    create_episode = AsyncMock(return_value=SimpleNamespace(episode_id="cep_1"))
    update_episode = AsyncMock()
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", create_episode)
    monkeypatch.setattr(app.DATABASE.scripts, "update_fallback_episode", update_episode)

    _, _, block_result, should_stop, branch_metadata = await _run_single_block(
        WorkflowService(),
        block,
        workflow=_adaptive_workflow(),
        is_script_run=True,
        script_blocks_by_label={"cond": _script_block("cond", "True", requires_agent=True)},
    )

    # Every case routes and reports completed, including the two whose first branch could not be evaluated.
    assert block_result is not None
    assert block_result.status == BlockStatus.completed
    assert branch_metadata is not None
    assert branch_metadata["branch_taken"] == branch_taken
    assert should_stop is False
    if records_episode:
        create_episode.assert_awaited_once()
        assert create_episode.await_args.kwargs["fallback_type"] == "conditional_agent"
        assert create_episode.await_args.kwargs["agent_actions"]["branch_taken"] == branch_taken
        update_episode.assert_awaited_once_with(episode_id="cep_1", organization_id="org_test", fallback_succeeded=True)
    else:
        create_episode.assert_not_awaited()
        update_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_fallback_episode_excludes_decision_row_from_agent_action_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every terminal v3 task run now persists a synthesized COMPLETE/TERMINATE decision row even
    # when the agent made zero real page actions. If that row counted toward agent_action_count,
    # a script that silently mis-verified a run (the verifier-swap failure mode this downgrade
    # exists to catch) would read as "the agent did something" and keep fallback_succeeded=True.
    service = WorkflowService()
    block = _navigation_block("cached_nav")
    block_result = BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        status=BlockStatus.completed,
        workflow_run_block_id="wrb_1",
    )
    monkeypatch.setattr(
        app.DATABASE.observer,
        "get_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(task_id="tsk_1")),
    )
    monkeypatch.setattr(
        app.DATABASE.tasks,
        "get_task_actions",
        AsyncMock(return_value=[Action(action_type=ActionType.COMPLETE)]),
    )
    update_episode = AsyncMock()
    monkeypatch.setattr(app.DATABASE.scripts, "update_fallback_episode", update_episode)

    await service._enrich_fallback_episode_with_agent_actions(
        block=block,
        workflow_run_block_result=block_result,
        fallback_episode_id="ep_1",
        form_fields_for_episode=None,
        organization_id="org_test",
    )

    update_episode.assert_awaited_once()
    assert update_episode.await_args.kwargs["fallback_succeeded"] is False
    # The happy-path summary must exist (a raising summarizer would fall into the except arm and
    # leave this test unable to discriminate the count filter).
    summarized = update_episode.await_args.kwargs["agent_actions"]["actions"]
    assert [entry["action_type"] for entry in summarized] == [ActionType.COMPLETE]
    assert (
        update_episode.await_args.kwargs["agent_actions"]["failure_reason"]
        == script_service.VERIFIER_SWAP_FAILURE_REASON
    )


def _real_run_context(monkeypatch: pytest.MonkeyPatch) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=AsyncMock(),
    )
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    return context


@pytest.mark.asyncio
async def test_outcome_record_failure_does_not_fail_completed_block(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _real_run_context(monkeypatch)
    monkeypatch.setattr(context, "mask_secrets_in_data", MagicMock(side_effect=RuntimeError("Mask unavailable")))
    block = _navigation_block("nav")
    result = BlockResult(
        success=True,
        status=BlockStatus.completed,
        failure_reason="diagnostic",
        output_parameter=block.output_parameter,
        workflow_run_block_id="wrb_nav",
    )
    monkeypatch.setattr(NavigationBlock, "_execute_to_block_result", AsyncMock(return_value=result))

    assert await block.execute_safe("wr_test") is result
    assert context.get_block_outcome("nav") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_codes", "failure_reason", "ends_run"),
    [
        (BlockStatus.completed, [], None, False),
        (BlockStatus.failed, ["AUTH_FAILURE"], "login rejected", True),
        (BlockStatus.failed, [], "login rejected", True),
        (BlockStatus.terminated, [], "nothing left to do", True),
        (BlockStatus.canceled, [], None, True),
        (BlockStatus.timed_out, [], "page never loaded", True),
    ],
)
async def test_engine_records_the_same_outcome_shape_for_every_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
    status: BlockStatus,
    error_codes: list[str],
    failure_reason: str | None,
    ends_run: bool,
) -> None:
    block = _navigation_block("nav")
    result = BlockResult(
        success=status is BlockStatus.completed,
        output_parameter=block.output_parameter,
        status=status,
        failure_reason=failure_reason,
        error_codes=error_codes,
        workflow_run_block_id="wrb_nav",
    )
    _wire_real_execute_safe(monkeypatch, [WorkflowRunStatus.running], execute=AsyncMock(return_value=result))
    context = _real_run_context(monkeypatch)
    service = WorkflowService()
    for finalizer in (
        "mark_workflow_run_as_failed_if_not_final",
        "mark_workflow_run_as_terminated_if_not_final",
        "mark_workflow_run_as_canceled",
    ):
        monkeypatch.setattr(service, finalizer, AsyncMock(return_value=_workflow_run()))

    _, _, block_result, should_stop, _ = await _run_single_block(service, block)

    assert block_result is result
    assert should_stop is ends_run
    assert context.get_block_outcome("nav") == BlockOutcome(
        status=status, error_codes=error_codes, failure_reason=failure_reason
    )
    assert context.get_block_outcome("never_ran") is None


@pytest.mark.asyncio
async def test_a_block_the_run_ended_before_is_recorded_as_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _navigation_block("nav")
    execute = AsyncMock()
    status_holder = [WorkflowRunStatus.running]
    pause = _wire_real_execute_safe(monkeypatch, status_holder, execute=execute, pause_point="screenshot")
    context = _real_run_context(monkeypatch)

    run_task = asyncio.create_task(_run_single_block(WorkflowService(), block))
    await pause.entered.wait()
    status_holder[0] = WorkflowRunStatus.completed
    pause.release.set()
    _, _, block_result, _, _ = await run_task

    execute.assert_not_awaited()
    assert block_result is not None and block_result.status is BlockStatus.skipped
    assert context.get_block_outcome("nav") == BlockOutcome(
        status=BlockStatus.skipped, error_codes=[], failure_reason=None
    )


@pytest.mark.asyncio
async def test_loop_child_outcome_is_its_last_iteration_and_the_loop_records_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = WaitBlock(
        label="child", output_parameter=_output_parameter("child_output"), wait_sec=1, continue_on_failure=True
    )
    loop = ForLoopBlock(label="each_row", output_parameter=_output_parameter("each_row_output"), loop_blocks=[child])
    monkeypatch.setattr(
        app.DATABASE.observer,
        "create_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(workflow_run_block_id="wrb_1")),
    )
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=None))
    monkeypatch.setattr(Block, "_generate_workflow_run_block_description", AsyncMock())
    monkeypatch.setattr(ForLoopBlock, "get_loop_over_parameter_values", AsyncMock(return_value=["r1", "r2"]))
    monkeypatch.setattr(ForLoopBlock, "get_loop_block_context_parameters", lambda self, *args, **kwargs: [])
    monkeypatch.setattr(ForLoopBlock, "_snapshot_loop_baseline_pages", AsyncMock(return_value=None))
    monkeypatch.setattr(ForLoopBlock, "_reset_browser_tabs_for_iteration", AsyncMock())
    monkeypatch.setattr(ForLoopBlock, "_persist_partial_loop_output", AsyncMock())
    context = _real_run_context(monkeypatch)
    iteration_results = iter(
        [(BlockStatus.failed, ["ROW_REJECTED"], "row rejected"), (BlockStatus.completed, [], None)]
    )
    seen_before_second_iteration: list[BlockOutcome | None] = []

    async def run_child(
        self: WaitBlock, workflow_run_id: str, workflow_run_block_id: str, **kwargs: Any
    ) -> BlockResult:
        status, error_codes, failure_reason = next(iteration_results)
        if status is BlockStatus.completed:
            seen_before_second_iteration.append(context.get_block_outcome("child"))
        return await self.build_block_result(
            success=status is BlockStatus.completed,
            failure_reason=failure_reason,
            status=status,
            error_codes=error_codes or None,
            workflow_run_block_id=workflow_run_block_id,
        )

    monkeypatch.setattr(WaitBlock, "execute", run_child)
    with patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as block_context:
        block_context.current.return_value = None
        _, _, block_result, should_stop, _ = await _run_single_block(WorkflowService(), loop)

    assert block_result is not None and block_result.status is BlockStatus.completed
    assert should_stop is False
    # Each iteration writes the child's record; the one that survives is the last iteration's.
    assert seen_before_second_iteration == [
        BlockOutcome(status=BlockStatus.failed, error_codes=["ROW_REJECTED"], failure_reason="row rejected")
    ]
    assert context.get_block_outcome("child") == BlockOutcome(
        status=BlockStatus.completed, error_codes=[], failure_reason=None
    )
    assert context.get_block_outcome("each_row") == BlockOutcome(
        status=BlockStatus.completed, error_codes=[], failure_reason=None
    )
