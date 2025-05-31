import pytest

from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.workflow.models.workflow import WorkflowStatus


@pytest.mark.asyncio
async def test_workflow_cron_saved_and_updated() -> None:
    db = AgentDB("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    workflow = await db.create_workflow(
        title="cron-test",
        workflow_definition={"parameters": [], "blocks": []},
        cron="0 5 * * *",
        status=WorkflowStatus.published,
    )

    fetched = await db.get_workflow(workflow.workflow_id)
    assert fetched is not None
    assert fetched.cron == "0 5 * * *"

    updated = await db.update_workflow(workflow.workflow_id, cron="0 6 * * *")
    assert updated.cron == "0 6 * * *"
    fetched_after = await db.get_workflow(workflow.workflow_id)
    assert fetched_after.cron == "0 6 * * *"
