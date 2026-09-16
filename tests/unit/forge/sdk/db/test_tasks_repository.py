from decimal import Decimal

import pytest
from sqlalchemy import select

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.models import TaskRunModel, WorkflowRunAttemptModel


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
