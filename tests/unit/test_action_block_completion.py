"""An ActionBlock must have a successful action before its step can complete the task."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Page

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.models.block import ActionBlock
from skyvern.schemas.steps import AgentStepOutput
from skyvern.utils.prompt_engine import MaxStepsReasonResponse
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.handler import check_for_invalid_web_action
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.test_agent_step_characterization import AgentStepRig, _click, make_agent_step_rig


async def _complete_action_step(
    rig: AgentStepRig, monkeypatch: pytest.MonkeyPatch
) -> tuple[bool | None, Step | None, Step | None]:
    rig.agent._check_workflow_run_step_budget = AsyncMock(return_value=None)
    rig.agent.update_task = AsyncMock(return_value=rig.task)
    rig.agent.summary_failure_reason_for_max_steps = AsyncMock(
        return_value=MaxStepsReasonResponse(page_info="", reasoning="The action did not succeed.")
    )
    next_step = rig.step.model_copy(
        update={"step_id": "next-step", "order": 1, "status": StepStatus.created, "output": None}
    )
    create_step = AsyncMock(return_value=next_step)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.create_step", create_step)
    block = ActionBlock.model_construct(
        label="click_submit",
        max_steps_per_run=rig.task.max_steps_per_run,
        complete_on_download=False,
    )
    skyvern_context.set(rig.context)
    try:
        return await rig.agent.handle_completed_step(
            organization=rig.organization,
            task=rig.task,
            step=rig.step,
            page=await rig.browser_state.must_get_working_page(),
            task_block=block,
            browser_state=rig.browser_state,
            scraped_page=rig.scraped_page,
        )
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps", [1, 2])
async def test_missing_target_cannot_pass(monkeypatch: pytest.MonkeyPatch, max_steps: int) -> None:
    async def validate_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        page: Page,
        action: Action,
        **_: object,
    ) -> list[ActionResult]:
        return check_for_invalid_web_action(action, page, scraped_page, task, step)

    rig = make_agent_step_rig(
        monkeypatch,
        parsed_actions=[_click("node-hallucinated")],
        action_handler=AsyncMock(side_effect=validate_action),
        task_overrides={"max_steps_per_run": max_steps},
    )
    step, output = await rig.run(
        task_block=ActionBlock.model_construct(label="click_submit", complete_on_download=False)
    )
    assert step.status == StepStatus.completed  # Tolerated failures still finish the step.
    assert output.actions_and_results is not None
    failure = output.actions_and_results[0][1][-1]
    assert failure.exception_type == "MissingElement"
    assert failure.success is False
    assert failure.stop_execution_on_failure is False

    completed, last_step, next_step = await _complete_action_step(rig, monkeypatch)

    if max_steps == 1:
        assert completed is False
        assert last_step is step
        assert next_step is None
        assert rig.agent.update_task.await_args.kwargs["status"] == TaskStatus.failed
        assert "maximum steps (1)" in rig.agent.update_task.await_args.kwargs["failure_reason"]
    else:
        assert completed is None
        assert last_step is None
        assert next_step is not None and next_step.order == 1
        rig.agent.update_task.assert_not_awaited()
        rig.agent.summary_failure_reason_for_max_steps.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "expected_completed"),
    [
        ([], False),
        ([False], False),
        ([True, False], False),
        ([False, True], True),
        ([True], True),
    ],
    ids=["no-results", "failed", "final-failure", "recovered", "successful"],
)
async def test_action_block_completion_requires_a_successful_final_result(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[bool], expected_completed: bool
) -> None:
    rig = make_agent_step_rig(monkeypatch, task_overrides={"max_steps_per_run": 1})
    results = [
        ActionSuccess()
        if succeeded
        else ActionFailure(
            ValueError("action failed"),
            stop_execution_on_failure=False,
        )
        for succeeded in outcomes
    ]
    rig.step.status = StepStatus.completed
    rig.step.output = AgentStepOutput(actions_and_results=[(_click(), results)])

    completed, _, next_step = await _complete_action_step(rig, monkeypatch)

    assert completed is expected_completed
    assert next_step is None
    expected_status = TaskStatus.completed if expected_completed else TaskStatus.failed
    assert rig.agent.update_task.await_args.kwargs["status"] == expected_status
