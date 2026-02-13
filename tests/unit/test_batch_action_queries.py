"""
Tests for batch action query correctness in transform_workflow_run.py.

Verifies that the transform layer produces chronologically ordered actions
per task for script generation, even though get_tasks_actions returns
descending order (for the timeline UI).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input

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
        mock_app.DATABASE.get_workflow_run_blocks = AsyncMock(return_value=[run_block_a, run_block_b])
        mock_app.DATABASE.get_tasks_by_ids = AsyncMock(return_value=[mock_task_a, mock_task_b])
        mock_app.DATABASE.get_tasks_actions = AsyncMock(return_value=all_actions_descending)

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


@pytest.mark.asyncio
async def test_batch_actions_without_reverse_would_be_wrong() -> None:
    """
    Prove that without the reverse() call, DESC input from get_tasks_actions
    would produce wrong ordering in script generation output.

    If someone removes the reverse(), this test catches it.
    """
    from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input

    mock_workflow_run_resp = MagicMock()
    mock_workflow_run_resp.run_request = MagicMock()
    mock_workflow_run_resp.run_request.workflow_id = "wpid_test"
    mock_workflow_run_resp.run_request.model_dump = MagicMock(
        return_value={"workflow_id": "wpid_test", "parameters": {}}
    )

    def_block = MagicMock()
    def_block.block_type = "task"
    def_block.label = "my_block"
    def_block.model_dump = MagicMock(return_value={"block_type": "task", "label": "my_block"})

    mock_workflow = MagicMock()
    mock_workflow.model_dump = MagicMock(return_value={"workflow_id": "wf_1"})
    mock_workflow.workflow_definition.blocks = [def_block]

    run_block = MagicMock()
    run_block.workflow_run_block_id = "wfrb_1"
    run_block.parent_workflow_run_block_id = None
    run_block.block_type = "task"
    run_block.label = "my_block"
    run_block.task_id = "task_1"
    run_block.status = "completed"
    run_block.output = {}
    run_block.created_at = 1

    mock_task = MagicMock()
    mock_task.task_id = "task_1"
    mock_task.model_dump = MagicMock(return_value={"task_id": "task_1"})

    # Chronological order: click -> input -> extract
    # DB returns DESC: extract -> input -> click
    action_click = _make_action(ClickAction, action_id="act_1_click", task_id="task_1", element_id="el_1")
    action_input = _make_action(InputTextAction, action_id="act_2_input", task_id="task_1", element_id="el_2")
    action_extract = _make_action(ExtractAction, action_id="act_3_extract", task_id="task_1", element_id=None)

    # DESC order from DB (newest first)
    actions_descending = [action_extract, action_input, action_click]

    with (
        patch("skyvern.services.workflow_service.get_workflow_run_response", new_callable=AsyncMock) as mock_get_wfr,
        patch("skyvern.core.script_generations.transform_workflow_run.app") as mock_app,
    ):
        mock_get_wfr.return_value = mock_workflow_run_resp
        mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
        mock_app.DATABASE.get_workflow_run_blocks = AsyncMock(return_value=[run_block])
        mock_app.DATABASE.get_tasks_by_ids = AsyncMock(return_value=[mock_task])
        mock_app.DATABASE.get_tasks_actions = AsyncMock(return_value=actions_descending)

        result = await transform_workflow_run_to_code_gen_input(workflow_run_id="wr_test", organization_id="org_test")

    # After reverse, output must be chronological: click, input, extract
    actions = result.actions_by_task["task_1"]
    action_ids = [a["action_id"] for a in actions]
    assert action_ids == ["act_1_click", "act_2_input", "act_3_extract"], (
        f"Actions should be in chronological order after reverse, got: {action_ids}"
    )


@pytest.mark.asyncio
async def test_batch_actions_preserve_none_element_id() -> None:
    """
    Regression test: hydrate_action must be called WITHOUT empty_element_id=True,
    so that None element_ids remain None (matching get_task_actions_hydrated behavior).

    Previously get_tasks_actions used hydrate_action(action, empty_element_id=True)
    which silently converted None element_ids to empty strings.
    """
    from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input

    mock_workflow_run_resp = MagicMock()
    mock_workflow_run_resp.run_request = MagicMock()
    mock_workflow_run_resp.run_request.workflow_id = "wpid_test"
    mock_workflow_run_resp.run_request.model_dump = MagicMock(
        return_value={"workflow_id": "wpid_test", "parameters": {}}
    )

    def_block = MagicMock()
    def_block.block_type = "extraction"
    def_block.label = "extract_block"
    def_block.model_dump = MagicMock(return_value={"block_type": "extraction", "label": "extract_block"})

    mock_workflow = MagicMock()
    mock_workflow.model_dump = MagicMock(return_value={"workflow_id": "wf_1"})
    mock_workflow.workflow_definition.blocks = [def_block]

    run_block = MagicMock()
    run_block.workflow_run_block_id = "wfrb_1"
    run_block.parent_workflow_run_block_id = None
    run_block.block_type = "extraction"
    run_block.label = "extract_block"
    run_block.task_id = "task_1"
    run_block.status = "completed"
    run_block.output = {}
    run_block.created_at = 1

    mock_task = MagicMock()
    mock_task.task_id = "task_1"
    mock_task.model_dump = MagicMock(return_value={"task_id": "task_1"})

    # ExtractAction has element_id=None (extracts don't target a specific element)
    action_extract = _make_action(ExtractAction, action_id="act_extract", task_id="task_1", element_id=None)
    assert action_extract.element_id is None

    with (
        patch("skyvern.services.workflow_service.get_workflow_run_response", new_callable=AsyncMock) as mock_get_wfr,
        patch("skyvern.core.script_generations.transform_workflow_run.app") as mock_app,
    ):
        mock_get_wfr.return_value = mock_workflow_run_resp
        mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
        mock_app.DATABASE.get_workflow_run_blocks = AsyncMock(return_value=[run_block])
        mock_app.DATABASE.get_tasks_by_ids = AsyncMock(return_value=[mock_task])
        mock_app.DATABASE.get_tasks_actions = AsyncMock(return_value=[action_extract])

        result = await transform_workflow_run_to_code_gen_input(workflow_run_id="wr_test", organization_id="org_test")

    actions = result.actions_by_task["task_1"]
    assert len(actions) == 1
    # element_id must remain None, NOT converted to ""
    assert actions[0]["element_id"] is None, (
        f"element_id should be None but got {actions[0]['element_id']!r}. "
        "This indicates hydrate_action was called with empty_element_id=True"
    )
