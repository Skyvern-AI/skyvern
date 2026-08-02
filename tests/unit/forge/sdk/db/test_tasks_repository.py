"""TasksRepository tests against the default SQLite backend."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import OrganizationModel, StepModel, TaskModel
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository

pytestmark = pytest.mark.asyncio


async def test_unique_step_order_count_supports_sqlite(sqlite_engine: AsyncEngine) -> None:
    database = BaseAlchemyDB(sqlite_engine)
    repository = TasksRepository(database.Session, debug_enabled=False)

    async with repository.Session() as session:
        session.add_all(
            [
                OrganizationModel(organization_id="org_test", organization_name="Test"),
                OrganizationModel(organization_id="org_other", organization_name="Other"),
            ]
        )
        session.add_all(
            [
                TaskModel(task_id="task_a", organization_id="org_test"),
                TaskModel(task_id="task_b", organization_id="org_test"),
                TaskModel(task_id="task_not_requested", organization_id="org_test"),
                TaskModel(task_id="task_other_org", organization_id="org_other"),
            ]
        )
        session.add_all(
            [
                StepModel(step_id="step_a_0", organization_id="org_test", task_id="task_a", order=0),
                StepModel(step_id="step_a_0_retry", organization_id="org_test", task_id="task_a", order=0),
                StepModel(step_id="step_a_1", organization_id="org_test", task_id="task_a", order=1),
                StepModel(step_id="step_b_0", organization_id="org_test", task_id="task_b", order=0),
                StepModel(
                    step_id="step_not_requested",
                    organization_id="org_test",
                    task_id="task_not_requested",
                    order=0,
                ),
                StepModel(
                    step_id="step_other_org",
                    organization_id="org_other",
                    task_id="task_other_org",
                    order=0,
                ),
            ]
        )
        await session.commit()

    count = await repository.get_total_unique_step_order_count_by_task_ids(
        task_ids=["task_a", "task_b"],
        organization_id="org_test",
    )

    assert count == 3
