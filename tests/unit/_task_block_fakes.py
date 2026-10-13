from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def output_parameter(key: str = "task_output") -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id="op_task_block_test",
        workflow_id="w_task_block_test",
        created_at=now,
        modified_at=now,
    )


def _workflow_run_context(values: dict[str, Any] | None = None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_task_block_test",
        workflow_permanent_id="wpid_task_block_test",
        workflow_run_id="wr_task_block_test",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


@dataclass(frozen=True)
class BlockExecuteDeps:
    task: SimpleNamespace
    tasks_db: MagicMock
    observer_db: MagicMock
    agent: MagicMock
    browser_state: MagicMock
    browser_manager: MagicMock
    workflow_run: SimpleNamespace


@contextmanager
def mock_block_execute_deps(
    working_page_url: str, run_context_values: dict[str, Any] | None = None
) -> Iterator[BlockExecuteDeps]:
    """Patch the app-level singletons used by TaskBlock.execute() for a first-task
    scenario and hand the test back the mocks it needs to assert on."""

    workflow_run = SimpleNamespace(
        workflow_run_id="wr_task_block_test",
        workflow_permanent_id="wpid_task_block_test",
        organization_id="o_test",
        browser_profile_id=None,
        browser_address=None,
    )

    working_page = SimpleNamespace(url=working_page_url)
    browser_state = MagicMock()
    browser_state.must_get_working_page = AsyncMock(return_value=working_page)
    browser_state.take_fullpage_screenshot = AsyncMock(return_value=None)
    browser_state.navigate_to_url = AsyncMock()

    browser_manager = MagicMock()
    browser_manager.get_or_create_for_workflow_run = AsyncMock(return_value=browser_state)

    workflow_service = MagicMock()
    workflow_service.get_workflow_run = AsyncMock(return_value=workflow_run)
    workflow_service.get_workflow_by_permanent_id = AsyncMock(return_value=MagicMock(workflow_id="w_task_block_test"))

    organization = SimpleNamespace(organization_id="o_test")
    organizations_db = MagicMock()
    organizations_db.get_organization = AsyncMock(return_value=organization)

    tasks_db = MagicMock()
    tasks_db.update_task = AsyncMock()
    tasks_db.get_last_task_for_workflow_run = AsyncMock(return_value=None)

    observer_db = MagicMock()
    observer_db.update_workflow_run_block = AsyncMock(return_value=MagicMock())

    database = MagicMock()
    database.tasks = tasks_db
    database.organizations = organizations_db
    database.observer = observer_db

    # workflow_run_id is on the real Task; the block failure handler keys its secret lookup on it.
    task = SimpleNamespace(task_id="tsk_test", status=TaskStatus.failed, workflow_run_id="wr_test")
    step = SimpleNamespace(step_id="stp_test")

    agent = MagicMock()
    agent.create_task_and_step_from_block = AsyncMock(return_value=(task, step))
    agent.execute_step = AsyncMock()

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block.Block.get_workflow_run_context",
            return_value=_workflow_run_context(run_context_values),
        ),
        patch(
            "skyvern.forge.sdk.workflow.models.block.capture_block_download_baseline",
            new=AsyncMock(),
        ),
    ):
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.WORKFLOW_SERVICE = workflow_service
        mock_app.DATABASE = database
        mock_app.agent = agent

        yield BlockExecuteDeps(
            task=task,
            tasks_db=tasks_db,
            observer_db=observer_db,
            agent=agent,
            browser_state=browser_state,
            browser_manager=browser_manager,
            workflow_run=workflow_run,
        )
