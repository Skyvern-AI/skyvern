"""Loop values are grounded by the extraction block before iteration."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import ExtractionBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.services import task_v2_service


def _output_param() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="loop_out",
        description="d",
        output_parameter_id="op",
        workflow_id="w",
        created_at=now,
        modified_at=now,
    )


async def _call_and_capture_extraction() -> AsyncMock:
    """Invoke _generate_loop_task with the app surfaces needed to REACH the branch mocked, and
    return the patched ExtractionBlock.execute_safe so the test can assert whether it ran. Anything
    downstream of the branch is unmocked and swallowed — the branch is all we assert here."""
    app_obj = task_v2_service.app
    saved_db = app_obj.DATABASE
    saved_ws = app_obj.WORKFLOW_SERVICE
    db = MagicMock()
    db.observer.create_thought = AsyncMock(return_value=MagicMock())
    db.observer.update_thought = AsyncMock(return_value=MagicMock())
    ws = MagicMock()
    ws.create_output_parameter_for_block = AsyncMock(return_value=_output_param())
    app_obj.DATABASE = db
    app_obj.WORKFLOW_SERVICE = ws
    task_v2 = MagicMock(observer_cruise_id="tsk", organization_id="o")
    scraped = MagicMock(screenshots=[])
    try:
        with (
            patch.object(
                ExtractionBlock,
                "execute_safe",
                new=AsyncMock(return_value=MagicMock(success=False)),
            ) as mock_exec,
            patch.object(task_v2_service, "_get_task_v2_llm_api_handler", return_value=AsyncMock(return_value={})),
        ):
            try:
                await task_v2_service._generate_loop_task(
                    task_v2=task_v2,
                    workflow_id="w",
                    workflow_permanent_id="wpid",
                    workflow_run_id="wr",
                    plan="loop over the top schools",
                    browser_state=MagicMock(),
                    original_url="https://example.com",
                    scraped_page=scraped,
                )
            except Exception:
                pass
            return mock_exec
    finally:
        app_obj.DATABASE = saved_db
        app_obj.WORKFLOW_SERVICE = saved_ws


@pytest.mark.asyncio
async def test_loop_values_are_grounded_by_extraction() -> None:
    mock_exec = await _call_and_capture_extraction()
    mock_exec.assert_awaited()
