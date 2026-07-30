"""Lever B threading: a planner-emitted complete_criterion must turn on the v1 navigation
block's per-block completion check; its absence must preserve the legacy no-check behavior."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.workflow.models.block import NavigationBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import NavigationBlockYAML
from skyvern.services import task_v2_service


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="nav_output",
        description="nav output",
        output_parameter_id="op_nav",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


async def _gen(complete_criterion: str | None):
    app_obj = task_v2_service.app
    saved = app_obj.WORKFLOW_SERVICE
    ws_mock = MagicMock()
    ws_mock.create_output_parameter_for_block = AsyncMock(return_value=_make_output_parameter())
    app_obj.WORKFLOW_SERVICE = ws_mock
    try:
        return await task_v2_service._generate_navigation_task(
            workflow_id="w_test",
            workflow_permanent_id="wpid_test",
            workflow_run_id="wr_test",
            navigation_goal="fill the search form with X, Y, Z and submit",
            complete_criterion=complete_criterion,
        )
    finally:
        app_obj.WORKFLOW_SERVICE = saved


@pytest.mark.asyncio
async def test_criterion_enables_completion_check() -> None:
    block, block_yaml_list, _ = await _gen("the results table for X is visible")
    assert isinstance(block, NavigationBlock)
    assert block.complete_criterion == "the results table for X is visible"
    assert block.complete_verification is True
    yaml_block = block_yaml_list[0]
    assert isinstance(yaml_block, NavigationBlockYAML)
    assert yaml_block.complete_criterion == "the results table for X is visible"
    assert yaml_block.complete_verification is True


@pytest.mark.asyncio
async def test_no_criterion_preserves_legacy_behavior() -> None:
    block, block_yaml_list, _ = await _gen(None)
    assert block.complete_criterion is None
    assert block.complete_verification is False
    assert block_yaml_list[0].complete_verification is False
