"""Locks the bookkeeping contract for _handle_script_termination (SKY-9568):
IllegitCompleteScriptTermination -> BlockStatus.failed (so AI fallback fires);
plain ScriptTerminationException -> BlockStatus.terminated (no fallback)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.core.script_generations.skyvern_page import RunContext
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import (
    IllegitCompleteScriptTermination,
    ScriptTerminationException,
)
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.repositories.observer import ObserverRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import NavigationBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.services import script_service
from skyvern.services.script_service import _handle_script_termination
from skyvern.webeye.actions.actions import TerminateAction


@pytest.mark.asyncio
async def test_helper_writes_failed_for_illegit_complete():
    e = IllegitCompleteScriptTermination("Illegit complete, data={'error': '...'}")
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id="wrb_1",
            task_id="tsk_1",
            step_id="stp_1",
            cache_key="MyTaskBlock",
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        positional = mock_update.await_args.args
        assert positional[0] == "wrb_1"
        assert positional[1] == BlockStatus.failed
        assert kwargs["task_status"] == TaskStatus.failed
        assert kwargs["step_status"] == StepStatus.failed
        assert kwargs["failure_reason"] == "Illegit complete, data={'error': '...'}"


@pytest.mark.asyncio
async def test_helper_writes_terminated_for_plain_termination():
    error = UserDefinedError(error_code="no_results", reasoning="No results found", confidence_float=1.0)
    e = ScriptTerminationException("Terminate called: no results found", user_defined_errors=[error])
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id="wrb_2",
            task_id="tsk_2",
            step_id="stp_2",
            cache_key="MyTaskBlock",
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        positional = mock_update.await_args.args
        assert positional[0] == "wrb_2"
        assert positional[1] == BlockStatus.terminated
        assert kwargs["task_status"] == TaskStatus.terminated
        assert kwargs["step_status"] == StepStatus.failed
        assert kwargs["failure_reason"] == "Terminate called: no results found"
        assert kwargs["user_defined_errors"] == [error]


@pytest.mark.asyncio
async def test_helper_skips_db_write_when_no_workflow_run_block_id():
    e = ScriptTerminationException("Terminate called")
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id=None,
            task_id=None,
            step_id=None,
            cache_key="MyTaskBlock",
        )
        mock_update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criterion,block_mapping,workflow_mapping,expected_criterion,expected_mapping",
    [
        pytest.param(
            "Stop when {{ state }}",
            {"{{ code }}": "Block {{ state }}"},
            {"unavailable": "Workflow collision", "global": "Inherited condition"},
            "Stop when unavailable",
            {"unavailable": "Block unavailable", "global": "Inherited condition"},
            id="templated-block-precedence",
        ),
        pytest.param(None, None, {"global": "Inherited"}, None, {"global": "Inherited"}, id="null-map-inherits"),
        pytest.param("", {}, {"global": "Inherited"}, "", {"global": "Inherited"}, id="empty-map-inherits"),
        pytest.param(None, None, None, None, None, id="null-settings"),
        pytest.param("", {}, None, "", {}, id="empty-settings"),
    ],
)
async def test_cached_creation_and_termination_persist_contract(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    criterion: str | None,
    block_mapping: dict[str, str] | None,
    workflow_mapping: dict[str, str] | None,
    expected_criterion: str | None,
    expected_mapping: dict[str, str] | None,
) -> None:
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    tasks = TasksRepository(session_factory=factory, debug_enabled=False)
    workflows = WorkflowsRepository(session_factory=factory, debug_enabled=False)
    observer = ObserverRepository(session_factory=factory, task_reader=tasks)
    output = OutputParameter(
        key="target_output",
        output_parameter_id="op_target",
        workflow_id="wf_cached",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    target = NavigationBlock(
        label="target",
        navigation_goal="Inspect the requested item",
        output_parameter=output,
        terminate_criterion=criterion,
        error_code_mapping=block_mapping,
    )
    unrelated = target.model_copy(
        update={
            "label": "unrelated",
            "terminate_criterion": "Stop for another condition",
            "error_code_mapping": {"other": "Unrelated condition"},
        }
    )
    definition = WorkflowDefinition(
        parameters=[output], blocks=[unrelated, target], error_code_mapping=workflow_mapping
    )
    workflow = await workflows.create_workflow(
        title="Cached contract",
        organization_id="o_cached",
        workflow_id="wf_cached",
        workflow_definition=definition.model_dump(mode="json"),
        run_with="code",
    )
    context = skyvern_context.SkyvernContext(
        organization_id="o_cached",
        workflow_id=workflow.workflow_id,
        workflow_run_id="wr_cached",
        script_mode=True,
    )
    manager = WorkflowContextManager()
    workflow_context = WorkflowRunContext(
        workflow_title=workflow.title,
        workflow_id=workflow.workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_run_id="wr_cached",
        aws_client=Mock(),
        attempt_number=3,
    )
    workflow_context.values.update(state="unavailable", code="unavailable")
    manager.workflow_run_contexts["wr_cached"] = workflow_context
    monkeypatch.setattr(skyvern_context, "current", lambda: context)
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(app.DATABASE, "tasks", tasks)
    monkeypatch.setattr(app.DATABASE, "workflows", workflows)
    monkeypatch.setattr(app.DATABASE, "observer", observer)
    monkeypatch.setattr(tasks, "sync_task_run_status", AsyncMock())
    for name in ("_create_video_artifact", "_take_workflow_run_block_screenshot", "_record_output_parameter_value"):
        monkeypatch.setattr(script_service, name, AsyncMock())
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "flush_step_archive", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", AsyncMock(return_value=[]))
    for name in ("get_recent_task_screenshot_artifacts", "get_recent_workflow_screenshot_artifacts"):
        monkeypatch.setattr(app.WORKFLOW_SERVICE, name, AsyncMock(return_value=[]))

    block_id, task_id, step_id = await script_service._create_workflow_block_run_and_task(
        BlockType.NAVIGATION, label="target"
    )
    assert block_id is not None and task_id is not None and step_id is not None
    created = await tasks.get_task(task_id, organization_id="o_cached")
    assert created is not None
    assert created.terminate_criterion == expected_criterion
    assert created.error_code_mapping == expected_mapping
    assert (created.organization_id, created.workflow_run_id, created.attempt_number) == ("o_cached", "wr_cached", 3)
    existing_error = {"error_code": "existing", "reasoning": "Earlier condition", "confidence_float": 0.5}
    await tasks.update_task(task_id, organization_id="o_cached", errors=[existing_error])
    error = UserDefinedError(error_code="unavailable", reasoning="Requested item unavailable", confidence_float=0.9)
    run_context = RunContext(parameters={}, page=Mock())
    run_context.actions_and_results.append((TerminateAction(errors=[error]), []))
    monkeypatch.setattr(script_service.script_run_context_manager, "get_run_context", lambda: run_context)

    await _handle_script_termination(
        ScriptTerminationException("Requested item unavailable", user_defined_errors=[error]),
        "task block",
        block_id,
        task_id,
        step_id,
        "target",
    )
    finalized = await tasks.get_task(task_id, organization_id="o_cached")
    assert finalized is not None
    assert finalized.status == TaskStatus.terminated
    assert finalized.attempt_number == 3
    assert finalized.errors == [existing_error, error.model_dump()]
    step = await tasks.get_step(step_id, organization_id="o_cached")
    assert step is not None and step.output is not None
    assert step.output.errors == [error]
    block = await observer.get_workflow_run_block(block_id, organization_id="o_cached")
    assert block.status == BlockStatus.terminated
    assert block.attempt_number == 3
