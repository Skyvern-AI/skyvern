from types import SimpleNamespace

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.schemas.runs import RunStatus, RunType
from skyvern.services.task_v2_service import DEFAULT_WORKFLOW_TITLE, initialize_task_v2


@pytest.mark.asyncio
async def test_initialize_task_v2_populates_task_run_url_when_user_url_is_known() -> None:
    organization = SimpleNamespace(organization_id="org_123")
    user_url = "https://example.com"

    app.DATABASE.observer.create_task_v2.return_value = SimpleNamespace(
        observer_cruise_id="tsk_123",
        workflow_run_id=None,
        url=user_url,
    )
    app.WORKFLOW_SERVICE.create_empty_workflow.return_value = SimpleNamespace(
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        title=DEFAULT_WORKFLOW_TITLE,
    )
    app.WORKFLOW_SERVICE.setup_workflow_run.return_value = SimpleNamespace(workflow_run_id="wr_123")
    app.DATABASE.observer.update_task_v2.return_value = SimpleNamespace(
        observer_cruise_id="tsk_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        url=user_url,
    )
    app.DATABASE.tasks.create_task_run.return_value = SimpleNamespace(run_id="tsk_123")

    await initialize_task_v2(
        organization=organization,
        user_prompt="Open the page",
        user_url=user_url,
        create_task_run=True,
    )

    app.DATABASE.tasks.create_task_run.assert_awaited_once_with(
        task_run_type=RunType.task_v2,
        organization_id="org_123",
        run_id="tsk_123",
        title=DEFAULT_WORKFLOW_TITLE,
        url=user_url,
        url_hash=generate_url_hash(user_url),
        status=RunStatus.queued,
    )
