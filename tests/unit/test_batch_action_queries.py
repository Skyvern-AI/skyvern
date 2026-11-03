"""
Tests for batch action query correctness in transform_workflow_run.py.

Verifies that the transform layer produces chronologically ordered actions
per task for script generation, even though get_tasks_actions returns
descending order (for the timeline UI).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
from skyvern.webeye.actions.actions import ClickAction, ExtractAction, InputTextAction


def _make_action(
    action_cls: type,
    action_id: str,
    task_id: str,
    element_id: str | None = None,
    **kwargs: object,
) -> MagicMock:
    """Create a real Action instance for use in tests."""
    action = action_cls(
        action_id=action_id,
        task_id=task_id,
        element_id=element_id
        if element_id is not None
        else ("elem_" + action_id if action_cls != ExtractAction else None),
        **({"text": "hello"} if action_cls == InputTextAction else {}),
        **kwargs,
    )
    return action


@pytest.mark.asyncio
async def test_batch_actions_preserve_per_task_ordering() -> None:
    """
    Regression test: transform_workflow_run must produce actions in ascending
    chronological order per task for script generation.

    get_tasks_actions returns DESC order (for timeline UI). The transform
    layer reverses to ASC. This test mocks DESC input and verifies ASC output.
    """
    mock_workflow_run_resp = MagicMock()
    mock_workflow_run_resp.run_request = MagicMock()
    mock_workflow_run_resp.run_request.workflow_id = "wpid_test"
    mock_workflow_run_resp.run_request.model_dump = MagicMock(
        return_value={"workflow_id": "wpid_test", "parameters": {}}
    )

    def_block_a = MagicMock()
    def_block_a.block_type = "task"
    def_block_a.label = "block_a"
    def_block_a.model_dump = MagicMock(return_value={"block_type": "task", "label": "block_a"})

    def_block_b = MagicMock()
    def_block_b.block_type = "task"
    def_block_b.label = "block_b"
    def_block_b.model_dump = MagicMock(return_value={"block_type": "task", "label": "block_b"})

    mock_workflow = MagicMock()
    mock_workflow.model_dump = MagicMock(return_value={"workflow_id": "wf_1"})
    mock_workflow.workflow_definition.blocks = [def_block_a, def_block_b]

    run_block_a = MagicMock()
    run_block_a.workflow_run_block_id = "wfrb_a"
    run_block_a.parent_workflow_run_block_id = None
    run_block_a.block_type = "task"
    run_block_a.label = "block_a"
    run_block_a.task_id = "task_a"
    run_block_a.status = "completed"
    run_block_a.output = {}
    run_block_a.created_at = 1

    run_block_b = MagicMock()
    run_block_b.workflow_run_block_id = "wfrb_b"
    run_block_b.parent_workflow_run_block_id = None
    run_block_b.block_type = "task"
    run_block_b.label = "block_b"
    run_block_b.task_id = "task_b"
    run_block_b.status = "completed"
    run_block_b.output = {}
    run_block_b.created_at = 2

    mock_task_a = MagicMock()
    mock_task_a.task_id = "task_a"
    mock_task_a.model_dump = MagicMock(return_value={"task_id": "task_a"})

    mock_task_b = MagicMock()
    mock_task_b.task_id = "task_b"
    mock_task_b.model_dump = MagicMock(return_value={"task_id": "task_b"})

    # Actions in chronological order:
    # task_a: click (t=1), input_text (t=3)
    # task_b: click (t=2), extract (t=4)
    action_a_click = _make_action(ClickAction, action_id="a_click", task_id="task_a", element_id="el_1")
    action_b_click = _make_action(ClickAction, action_id="b_click", task_id="task_b", element_id="el_2")
    action_a_input = _make_action(InputTextAction, action_id="a_input", task_id="task_a", element_id="el_3")
    action_b_extract = _make_action(ExtractAction, action_id="b_extract", task_id="task_b", element_id=None)

    # get_tasks_actions returns DESC order (newest first) — matching real DB behavior
    all_actions_descending = [action_b_extract, action_a_input, action_b_click, action_a_click]

    with (
        patch("skyvern.services.workflow_service.get_workflow_run_response", new_callable=AsyncMock) as mock_get_wfr,
        patch("skyvern.core.script_generations.transform_workflow_run.app") as mock_app,
    ):
        mock_get_wfr.return_value = mock_workflow_run_resp
        mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(return_value=[run_block_a, run_block_b])
        mock_app.DATABASE.observer.workflow_run_has_block_on_engine = AsyncMock(return_value=False)
        mock_app.DATABASE.tasks.get_tasks_by_ids = AsyncMock(return_value=[mock_task_a, mock_task_b])
        mock_app.DATABASE.tasks.get_tasks_actions = AsyncMock(return_value=all_actions_descending)

        result = await transform_workflow_run_to_code_gen_input(workflow_run_id="wr_test", organization_id="org_test")

    # After reverse, task_a actions must be in chronological order: click then input_text
    task_a_actions = result.actions_by_task["task_a"]
    task_a_ids = [a["action_id"] for a in task_a_actions]
    assert task_a_ids == ["a_click", "a_input"], f"task_a actions out of order: {task_a_ids}"
    assert task_a_actions[0]["action_type"] == "click"
    assert task_a_actions[1]["action_type"] == "input_text"

    # task_b actions must be in chronological order: click then extract
    task_b_actions = result.actions_by_task["task_b"]
    task_b_ids = [a["action_id"] for a in task_b_actions]
    assert task_b_ids == ["b_click", "b_extract"], f"task_b actions out of order: {task_b_ids}"
    assert task_b_actions[0]["action_type"] == "click"
    assert task_b_actions[1]["action_type"] == "extract"

    # No cross-contamination between tasks
    assert set(task_a_ids) == {"a_click", "a_input"}
    assert set(task_b_ids) == {"b_click", "b_extract"}
