"""The completion gate (AGENT_FUNCTION.gate_step_completion) can veto an agent completion.

A vetoed CompleteAction must not mark the task completed; the agent continues (and fails safe
at max steps) instead of falsely completing. See SKY-12992.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
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
