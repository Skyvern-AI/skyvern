"""The completion gate (AGENT_FUNCTION.gate_step_completion) can veto an agent completion.

A vetoed CompleteAction must not mark the task completed; the agent continues (and fails safe
at max steps) instead of falsely completing. See SKY-12992.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.exceptions import CompletionGateTerminationError
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import CompleteAction
from skyvern.webeye.actions.responses import ActionSuccess
from tests.unit.helpers import (
    make_browser_state,
    make_organization,
    make_step,
    make_task,
    setup_parallel_verification_mocks,
)


@pytest.mark.asyncio
async def test_completion_gate_veto_does_not_complete_task(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)

    step = make_step(
        now,
        task,
        step_id="step-123",
        status=StepStatus.completed,
        order=0,
        output=AgentStepOutput(action_results=[], actions_and_results=[]),
    )
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)

    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=next_step,
        complete_action=CompleteAction(reasoning="done", verified=True),
        handle_action_responses=[[ActionSuccess()]],
    )

    # Veto the completion.
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(app.AGENT_FUNCTION, "gate_step_completion", gate)

    browser_state, scraped_page, page = make_browser_state()
    completed, _last_step, next_created_step = await agent._handle_completed_step_with_parallel_verification(
        organization=organization,
        task=task,
        step=step,
        page=page,
        browser_state=browser_state,
        scraped_page=scraped_page,
        engine=RunEngine.skyvern_v1,
    )

    assert gate.await_count == 1
    assert completed is not True
    assert next_created_step is not None  # loop continues with another step
    completed_calls = [c for c in mocks.update_task.await_args_list if c.kwargs.get("status") == TaskStatus.completed]
    assert completed_calls == []


@pytest.mark.asyncio
async def test_completion_gate_accept_completes_task(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal=None)  # skip data-extraction branch

    step = make_step(
        now,
        task,
        step_id="step-123",
        status=StepStatus.completed,
        order=0,
        output=AgentStepOutput(action_results=[], actions_and_results=[]),
    )

    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=step,
        complete_action=CompleteAction(reasoning="done", verified=True),
        handle_action_responses=[[ActionSuccess()]],
    )

    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(app.AGENT_FUNCTION, "gate_step_completion", gate)

    browser_state, scraped_page, page = make_browser_state()
    completed, _last_step, _next = await agent._handle_completed_step_with_parallel_verification(
        organization=organization,
        task=task,
        step=step,
        page=page,
        browser_state=browser_state,
        scraped_page=scraped_page,
        engine=RunEngine.skyvern_v1,
    )

    assert gate.await_count == 1
    assert completed is True
    completed_calls = [c for c in mocks.update_task.await_args_list if c.kwargs.get("status") == TaskStatus.completed]
    assert completed_calls != []


@pytest.mark.asyncio
async def test_decisive_completion_gate_veto_creates_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # A decisive COMPLETE action bypasses parallel verification and completes via
    # handle_completed_step's is_goal_achieved branch — the gate must fire here too.
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)

    complete = CompleteAction(reasoning="done", verified=True)
    output = AgentStepOutput(action_results=[ActionSuccess()], actions_and_results=[(complete, [ActionSuccess()])])
    step = make_step(now, task, step_id="step-123", status=StepStatus.completed, order=0, output=output)
    assert step.is_goal_achieved(has_navigation_goal=bool(task.navigation_goal))  # sanity

    update_task = AsyncMock()
    monkeypatch.setattr(agent, "update_task", update_task)
    monkeypatch.setattr(agent, "update_step", AsyncMock(side_effect=lambda s, **k: s))
    monkeypatch.setattr(agent, "_check_workflow_run_step_budget", AsyncMock(return_value=None))
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", AsyncMock(return_value=next_step))

    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(app.AGENT_FUNCTION, "gate_step_completion", gate)

    browser_state, scraped_page, page = make_browser_state()
    completed, _last_step, created_next = await agent.handle_completed_step(
        organization=organization,
        task=task,
        step=step,
        page=page,
        browser_state=browser_state,
        scraped_page=scraped_page,
        engine=RunEngine.skyvern_v1,
    )

    assert gate.await_count == 1
    assert completed is None  # not completed; loop continues
    assert created_next is next_step
    completed_calls = [c for c in update_task.await_args_list if c.kwargs.get("status") == TaskStatus.completed]
    assert completed_calls == []


async def _execute_step_through_the_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decisive: bool,
    gate_error: Exception,
    update_task_error: Exception | None = None,
    last_step_error: Exception | None = None,
    flush_error: Exception | None = None,
    captured: dict[str, object] | None = None,
    **task_overrides: object,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Drive execute_step into the real handle_completed_step, reaching the parallel-verification
    call site of the gate, or the decisive-completion one when the step itself emitted complete."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, **task_overrides)
    complete = CompleteAction(reasoning="done", verified=True)
    output = (
        AgentStepOutput(action_results=[ActionSuccess()], actions_and_results=[(complete, [ActionSuccess()])])
        if decisive
        else AgentStepOutput(action_results=[], actions_and_results=[])
    )
    step = make_step(now, task, step_id="step-123", status=StepStatus.completed, order=0, output=output)
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)
    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=next_step,
        complete_action=complete,
        handle_action_responses=[[ActionSuccess()]],
    )
    mocks.update_task.side_effect = update_task_error
    if last_step_error is not None:

        async def _update_step(step_to_update: Step, **kwargs: object) -> Step:
            if kwargs.get("is_last"):
                raise last_step_error
            return step_to_update

        mocks.update_step.side_effect = _update_step
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())
    detailed_output = SimpleNamespace(scraped_page=MagicMock(), cua_response=None, run_cancellation=None)
    gate = AsyncMock(side_effect=gate_error)
    monkeypatch.setattr(app.AGENT_FUNCTION, "gate_step_completion", gate)
    monkeypatch.setattr(app.AGENT_FUNCTION, "validate_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "post_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False))
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER, "flush_step_archive", AsyncMock(return_value=None, side_effect=flush_error)
    )
    monkeypatch.setattr("skyvern.forge.agent.record_fail_fast_shadow", AsyncMock(return_value=None))
    agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, detailed_output))  # type: ignore[method-assign]
    agent.register_async_operations = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.agent_step = AsyncMock(return_value=(step, detailed_output))  # type: ignore[method-assign]
    agent.update_task_errors_from_detailed_output = AsyncMock(side_effect=lambda task, _output: task)  # type: ignore[method-assign]
    fail_task = AsyncMock(return_value=True)
    agent.fail_task = fail_task  # type: ignore[method-assign]
    clean_up_task = AsyncMock(return_value=None)
    agent.clean_up_task = clean_up_task  # type: ignore[method-assign]

    skyvern_context.set(
        SkyvernContext(organization_id=organization.organization_id, task_id=task.task_id, tz_info=ZoneInfo("UTC"))
    )
    try:
        _step, _output, returned_next = await agent.execute_step(
            organization=organization, task=task, step=step, api_key="api-key", download_baseline_files=[]
        )
    finally:
        skyvern_context.reset()
    assert returned_next is None
    gate.assert_awaited_once()
    if captured is not None:
        captured["speculative_next_step"] = next_step
        captured["persist_discarded_plan"] = mocks.persist_speculative_metadata
    return mocks.update_task, fail_task, clean_up_task


@pytest.mark.asyncio
async def test_completion_gate_termination_redacts_run_secrets_from_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.agent._task_v3_run_secret_values", lambda task: {"hunter2-secret"})
    update_task, _, _ = await _execute_step_through_the_gate(
        monkeypatch, decisive=True, gate_error=CompletionGateTerminationError("held by hunter2-secret")
    )

    terminated = next(c for c in update_task.await_args_list if c.kwargs.get("status") == TaskStatus.terminated)
    assert "hunter2-secret" not in terminated.kwargs["failure_reason"]
