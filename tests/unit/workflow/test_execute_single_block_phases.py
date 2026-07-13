"""Characterization tests for WorkflowService._execute_single_block.

Pin current behavior of each phase (final-state guard, login profile prep,
script execution, agent fallback gate, cache tracking, conditional metadata)
before carving the method into per-phase helpers. Expected to pass unchanged
before and after every extraction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    JinjaBranchCriteria,
    LoginBlock,
    NavigationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import DebugSessionProfileDecision, WorkflowService
from skyvern.schemas.scripts import ScriptBlock
from skyvern.schemas.workflows import BlockResult, BlockStatus


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


def _completed_result(block: NavigationBlock | LoginBlock | ConditionalBlock) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=block.output_parameter,
        status=BlockStatus.completed,
        workflow_run_block_id=f"wrb_{block.label}",
    )


async def _run_single_block(
    service: WorkflowService,
    block: NavigationBlock | LoginBlock | ConditionalBlock,
    *,
    workflow: MagicMock | None = None,
    workflow_run: MagicMock | None = None,
    is_script_run: bool = False,
    script_blocks_by_label: dict | None = None,
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
        loaded_script_module=None,
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
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed", mark_failed)

    workflow_run, _, block_result, should_stop, _ = await _run_single_block(service, _navigation_block("nav"))

    mark_failed.assert_awaited_once_with(workflow_run_id="wr_test", failure_reason="Block result is None")
    assert workflow_run is failed_run
    assert block_result is None
    assert should_stop is True


@pytest.mark.asyncio
async def test_block_exception_marks_run_failed_with_block_type_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    monkeypatch.setattr(NavigationBlock, "execute_safe", AsyncMock(side_effect=RuntimeError("boom")))
    failed_run = MagicMock()
    mark_failed = AsyncMock(return_value=failed_run)
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed", mark_failed)

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
    monkeypatch.setattr(WorkflowService, "mark_workflow_run_as_failed", mark_failed)

    workflow_run, _, _, should_stop, _ = await _run_single_block(
        service,
        block,
        workflow_run=_workflow_run(ai_fallback=False),
        is_script_run=True,
        script_blocks_by_label={"cached_nav": _script_block("cached_nav", "1 / 0")},
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
    )

    execute_safe.assert_not_awaited()
    assert block_result is not None
    assert block_result.success is True
    assert block_result.status == BlockStatus.completed
    assert block_result.workflow_run_block_id == "wrb_script_1"
    assert returned_blocks == set()
    assert should_stop is False


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
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", AsyncMock(return_value=browser_state))
    execute_safe = AsyncMock(return_value=_completed_result(block))
    monkeypatch.setattr(LoginBlock, "execute_safe", execute_safe)

    await _run_single_block(service, block)

    update_run.assert_awaited_once_with(workflow_run_id="wr_test", browser_profile_id="bp_123")
    assert block.navigation_goal is not None
    assert block.navigation_goal.startswith("A saved browser session has been loaded.")
    assert "Original goal: log in" in block.navigation_goal
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
    monkeypatch.setattr(
        NavigationBlock,
        "execute_safe",
        AsyncMock(
            return_value=BlockResult(
                success=True, output_parameter=block.output_parameter, status=BlockStatus.completed
            )
        ),
    )
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
    )

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
