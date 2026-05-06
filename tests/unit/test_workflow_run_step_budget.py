from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent


def _make_organization(*, organization_id: str = "org-1", max_steps_per_workflow_run: int | None = None) -> MagicMock:
    org = MagicMock()
    org.organization_id = organization_id
    org.max_steps_per_workflow_run = max_steps_per_workflow_run
    return org


def _make_task(
    *, task_id: str = "task-1", organization_id: str = "org-1", workflow_run_id: str | None = "wr-1"
) -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.workflow_run_id = workflow_run_id
    return task


@pytest.mark.asyncio
async def test_returns_none_when_task_has_no_workflow_run_id() -> None:
    agent = ForgeAgent()
    org = _make_organization(max_steps_per_workflow_run=20)
    task = _make_task(workflow_run_id=None)

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock()
        mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock()

        result = await agent._check_workflow_run_step_budget(org, task)

    assert result is None
    mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id.assert_not_called()
    mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_org_has_no_cap() -> None:
    agent = ForgeAgent()
    org = _make_organization(max_steps_per_workflow_run=None)
    task = _make_task(workflow_run_id="wr-1")

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock()
        mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock()

        result = await agent._check_workflow_run_step_budget(org, task)

    assert result is None
    mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id.assert_not_called()


@pytest.mark.asyncio
async def test_returns_zero_count_when_workflow_run_has_no_tasks() -> None:
    agent = ForgeAgent()
    org = _make_organization(max_steps_per_workflow_run=20)
    task = _make_task(workflow_run_id="wr-1")

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=[])
        mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock()

        result = await agent._check_workflow_run_step_budget(org, task)

    assert result == (0, 20)
    # Skip the count query when there are no task ids to filter on.
    mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids.assert_not_called()


@pytest.mark.asyncio
async def test_returns_count_and_cap_when_tasks_present() -> None:
    agent = ForgeAgent()
    org = _make_organization(max_steps_per_workflow_run=20)
    task = _make_task(workflow_run_id="wr-1")
    sibling_tasks = [
        MagicMock(task_id="task-1"),
        MagicMock(task_id="task-2"),
    ]

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=sibling_tasks)
        mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock(return_value=18)

        result = await agent._check_workflow_run_step_budget(org, task)

    assert result == (18, 20)
    mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids.assert_awaited_once_with(
        task_ids=["task-1", "task-2"],
        organization_id="org-1",
    )


@pytest.mark.asyncio
async def test_treats_none_count_as_zero() -> None:
    # ``get_total_unique_step_order_count_by_task_ids`` can return None for a no-row count.
    agent = ForgeAgent()
    org = _make_organization(max_steps_per_workflow_run=20)
    task = _make_task(workflow_run_id="wr-1")

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=[MagicMock(task_id="task-1")])
        mock_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock(return_value=None)

        result = await agent._check_workflow_run_step_budget(org, task)

    assert result == (0, 20)
