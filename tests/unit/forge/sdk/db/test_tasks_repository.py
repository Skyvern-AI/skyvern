import pytest


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
