import re
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, select, update

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.models import TaskModel, TaskRunModel, WorkflowRunAttemptModel
from skyvern.forge.sdk.models import TASK_V3_TURN_STEP_CREATED_BY, StepStatus
from skyvern.schemas.steps import AgentStepOutput, StepToolCall
from skyvern.webeye.actions.actions import ClickAction


@pytest.mark.asyncio
async def test_total_unique_step_order_count_deduplicates_retries_in_sqlite(agent_db) -> None:
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization",
        organization_id="o_test_task_step_count",
    )
    task = await agent_db.tasks.create_task(
        url="https://example.com",
        title="Count steps",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization.organization_id,
    )
    excluded_task = await agent_db.tasks.create_task(
        url="https://example.com",
        title="Excluded task",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization.organization_id,
    )

    await agent_db.tasks.create_step(task.task_id, order=0, retry_index=0, organization_id=organization.organization_id)
    await agent_db.tasks.create_step(task.task_id, order=0, retry_index=1, organization_id=organization.organization_id)
    await agent_db.tasks.create_step(task.task_id, order=1, retry_index=0, organization_id=organization.organization_id)
    await agent_db.tasks.create_step(
        excluded_task.task_id,
        order=0,
        retry_index=0,
        organization_id=organization.organization_id,
    )

    assert (
        await agent_db.tasks.get_total_unique_step_order_count_by_task_ids(
            task_ids=[task.task_id],
            organization_id=organization.organization_id,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_task_v3_turn_steps_count_as_steps_but_not_budget_or_latest_steps(agent_db) -> None:
    # A Task V3 block's turn steps sit above its own order-0 step (SKY-18790). The step budgets still count that step
    # (the workflow one plus the action rounds), and the latest step stays the one holding the block's final screenshot.
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization", organization_id="o_test_v3_turn_steps"
    )
    org_id = organization.organization_id
    task = await agent_db.tasks.create_task(
        url="https://example.com",
        title="v3 block",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=org_id,
    )
    block_step = await agent_db.tasks.create_step(task.task_id, order=0, retry_index=0, organization_id=org_id)
    turn_steps = [
        await agent_db.tasks.create_step(
            task.task_id, order=turn, retry_index=0, organization_id=org_id, created_by=TASK_V3_TURN_STEP_CREATED_BY
        )
        for turn in (1, 2, 3)
    ]
    # Turn 1 reads, turns 2 and 3 each run a round: round indexes 0 and 1.
    for round_index, turn_step in enumerate(turn_steps[1:]):
        await agent_db.workflow_params.create_action(
            action=ClickAction(
                element_id="go",
                organization_id=org_id,
                task_id=task.task_id,
                step_id=turn_step.step_id,
                step_order=round_index,
                action_order=round_index,
            )
        )

    assert (
        await agent_db.tasks.get_total_unique_progress_round_count_by_task_ids(
            task_ids=[task.task_id], organization_id=org_id
        )
        == 2
    )
    await agent_db.tasks.finish_step(
        task.task_id,
        turn_steps[0].step_id,
        org_id,
        StepStatus.completed,
        AgentStepOutput(llm_seconds=1.5, tool_calls=[StepToolCall(tool="observe", status="ok")]),
    )

    assert (
        await agent_db.tasks.get_total_unique_step_order_count_by_task_ids(
            task_ids=[task.task_id], organization_id=org_id
        )
        == 1
    )
    assert await agent_db.tasks.get_task_step_count(task.task_id, organization_id=org_id) == 4
    assert await agent_db.tasks.get_step_counts_by_task_ids([task.task_id], organization_id=org_id) == (4, 1)
    finished = await agent_db.tasks.get_step(turn_steps[0].step_id, organization_id=org_id)
    assert finished is not None and finished.output is not None
    assert finished.output.tool_calls == [StepToolCall(tool="observe", status="ok")]
    latest = await agent_db.tasks.get_latest_step(task.task_id, organization_id=org_id)
    assert latest is not None and latest.step_id == block_step.step_id


@pytest.mark.asyncio
async def test_internal_synthetic_sdk_task_round_trips_through_repository(agent_db) -> None:
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization",
        organization_id="o_test_synthetic_sdk_task",
    )
    task = await agent_db.tasks.create_task(
        url="https://example.com",
        title="Synthetic SDK action",
        navigation_goal="Click the button",
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization.organization_id,
        task_type=TaskType.synthetic_sdk_action,
    )

    assert task.task_type == TaskType.synthetic_sdk_action

    persisted = await agent_db.tasks.get_task(task.task_id, organization.organization_id)
    assert persisted is not None
    assert persisted.task_type == TaskType.synthetic_sdk_action


@pytest.mark.asyncio
async def test_workflow_step_budget_starts_fresh_on_retry(agent_db, monkeypatch: pytest.MonkeyPatch) -> None:
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization", organization_id="o_attempt_budget"
    )
    organization.max_steps_per_workflow_run = 2
    tasks = []
    for attempt in (None, 1, 2):
        task = await agent_db.tasks.create_task(
            url="https://example.com",
            title=None,
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=organization.organization_id,
            workflow_run_id="wr_budget",
            attempt_number=attempt,
        )
        tasks.append(task)
        await agent_db.tasks.create_step(
            task.task_id, order=0, retry_index=0, organization_id=organization.organization_id
        )
    monkeypatch.setattr(app, "DATABASE", agent_db)
    agent = ForgeAgent()
    assert await agent._check_workflow_run_step_budget(organization, tasks[0]) == (2, 2)
    assert await agent._check_workflow_run_step_budget(organization, tasks[1]) == (2, 2)
    assert await agent._check_workflow_run_step_budget(organization, tasks[2]) == (1, 2)
    assert len(await agent_db.tasks.get_tasks_by_workflow_run_id("wr_budget")) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("first_attempt", [None, 1])
async def test_compute_cost_preserves_first_attempt_assignment_and_adds_retries(agent_db, first_attempt) -> None:
    async with agent_db.Session() as session:
        session.add(
            TaskRunModel(
                organization_id="o_cost",
                run_id="wr_cost",
                task_run_type="workflow_run",
                duration_ms=999,
                compute_cost=Decimal(9),
            )
        )
        if first_attempt is not None:
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_cost", organization_id="o_cost", attempt_number=1, status="failed"
                )
            )
        await session.commit()
    await agent_db.tasks.update_job_run_compute_cost(
        organization_id="o_cost",
        run_id="wr_cost",
        duration_ms=1000,
        compute_cost=Decimal("0.10"),
        llm_cost=Decimal("0.20"),
    )
    async with agent_db.Session() as session:
        row = await session.scalar(select(TaskRunModel).where(TaskRunModel.run_id == "wr_cost"))
        assert (row.duration_ms, row.compute_cost) == (1000, Decimal("0.10"))
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id="wr_cost", organization_id="o_cost", attempt_number=2, status="completed"
            )
        )
        await session.commit()
    await agent_db.tasks.update_job_run_compute_cost(
        organization_id="o_cost",
        run_id="wr_cost",
        duration_ms=2000,
        compute_cost=Decimal("0.30"),
        llm_cost=Decimal("0.50"),
    )
    async with agent_db.Session() as session:
        row = await session.scalar(select(TaskRunModel).where(TaskRunModel.run_id == "wr_cost"))
        assert (row.duration_ms, row.compute_cost, row.llm_cost) == (3000, Decimal("0.40"), Decimal("0.50"))


async def _create_workflow_run_task(agent_db, organization_id: str, workflow_run_id: str, attempt_number: int | None):
    return await agent_db.tasks.create_task(
        url="https://example.com",
        title=None,
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload={"attempt": attempt_number},
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        attempt_number=attempt_number,
    )


@pytest.mark.asyncio
async def test_workflow_run_status_task_read_omits_only_extracted_information(agent_db) -> None:
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization", organization_id="o_status_tasks"
    )
    org_id = organization.organization_id
    for attempt_number, extracted_information in ((None, {"rows": [{"i": 1}]}), (1, ["a"]), (2, "text")):
        task = await _create_workflow_run_task(agent_db, org_id, "wr_status_tasks", attempt_number)
        await agent_db.tasks.update_task(
            task.task_id,
            organization_id=org_id,
            extracted_information=extracted_information,
            errors=[{"error_code": f"E{attempt_number}", "reasoning": "r"}],
        )
    await _create_workflow_run_task(agent_db, org_id, "wr_other", None)

    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(agent_db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        status_tasks = await agent_db.tasks.get_tasks_for_workflow_run_status("wr_status_tasks")
    finally:
        event.remove(agent_db.engine.sync_engine, "before_cursor_execute", capture)
    full_tasks = await agent_db.tasks.get_tasks_by_workflow_run_id("wr_status_tasks")

    assert [task.extracted_information for task in full_tasks] == [{"rows": [{"i": 1}]}, ["a"], "text"]
    assert status_tasks == [task.model_copy(update={"extracted_information": None}) for task in full_tasks]
    task_selects = [statement for statement in statements if re.search(r"\bFROM tasks\b", statement)]
    assert len(task_selects) == 1
    assert re.search(r"\.extracted_information\b", task_selects[0]) is None
    assert "extracted_information_schema" in task_selects[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_values",
    [{"status": "not_a_status"}, {"navigation_payload": 7}, {"errors": 5}, {"cdp_connect_headers": [1]}],
)
async def test_workflow_run_status_task_read_keeps_retained_field_failures(agent_db, stored_values) -> None:
    organization = await agent_db.organizations.create_organization(
        organization_name="Test organization", organization_id="o_status_task_failures"
    )
    task = await _create_workflow_run_task(agent_db, organization.organization_id, "wr_status_failures", 1)
    async with agent_db.Session() as session:
        await session.execute(update(TaskModel).where(TaskModel.task_id == task.task_id).values(**stored_values))
        await session.commit()

    with pytest.raises(Exception) as full_read_error:
        await agent_db.tasks.get_tasks_by_workflow_run_id("wr_status_failures")
    with pytest.raises(type(full_read_error.value)) as status_read_error:
        await agent_db.tasks.get_tasks_for_workflow_run_status("wr_status_failures")
    assert str(status_read_error.value) == str(full_read_error.value)
