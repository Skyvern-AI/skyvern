from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import Block, TaskBlock
from tests.unit._task_block_fakes import mock_block_execute_deps, output_parameter
from tests.unit.helpers import make_organization, make_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_category",
    [None, [{"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "reasoning": "captcha"}]],
    ids=["classified_from_reason", "recorded_category"],
)
async def test_a_task_block_failing_on_captcha_text_retries_as_configured(failure_category: list | None) -> None:
    block = TaskBlock(
        label="nav",
        url="https://example.com",
        navigation_goal="go",
        output_parameter=output_parameter(),
        max_retries=1,
    )
    now = datetime.now(timezone.utc)
    organization = make_organization(now)
    first = make_task(
        now,
        organization,
        task_id="tsk_first",
        status=TaskStatus.failed,
        failure_reason="Page blocked by captcha challenge",
        failure_category=failure_category,
    )
    second = make_task(now, organization, task_id="tsk_second", status=TaskStatus.completed)

    with (
        skyvern_context.scoped(SkyvernContext()),
        mock_block_execute_deps(working_page_url="https://example.com") as deps,
        patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
    ):
        deps.browser_manager.get_for_workflow_run = MagicMock(return_value=None)
        deps.tasks_db.get_last_task_for_workflow_run = AsyncMock(side_effect=[None, first])
        deps.tasks_db.get_task = AsyncMock(side_effect=[first, second])
        deps.agent.create_task_and_step_from_block = AsyncMock(
            side_effect=[(first, SimpleNamespace(step_id="stp_first")), (second, SimpleNamespace(step_id="stp_second"))]
        )
        block_module.app.STORAGE = MagicMock(
            get_current_attempt_downloaded_files=AsyncMock(return_value=[]),
            get_downloaded_file_signature_aliases=MagicMock(return_value=()),
        )
        block_module.app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts = AsyncMock(return_value=[])
        block_module.app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts = AsyncMock(return_value=[])

        result = await block.execute(
            workflow_run_id="wr_task_block_test", workflow_run_block_id="wrb_test", organization_id="o_test"
        )

    assert result.success is True
