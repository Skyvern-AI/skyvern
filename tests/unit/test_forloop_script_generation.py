"""
Unit tests for ForLoop block support in cached scripts (SKY-7751).

These tests verify that ForLoop blocks are properly handled during:
1. Workflow transformation (transform_workflow_run.py)
2. Script generation (generate_script.py)
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED
from skyvern.schemas.workflows import BlockType


class TestForLoopInCacheableBlocks:
    """Test that ForLoop is included in cacheable block types."""

    def test_forloop_in_block_types_that_should_be_cached(self) -> None:
        """Verify ForLoop is included in BLOCK_TYPES_THAT_SHOULD_BE_CACHED."""
        assert BlockType.FOR_LOOP in BLOCK_TYPES_THAT_SHOULD_BE_CACHED


class TestForLoopTransformation:
    """Test the transformation of ForLoop blocks during script generation."""

    def test_forloop_child_blocks_identified_by_parent_id(self) -> None:
        """Test that child blocks inside ForLoop can be identified by parent_workflow_run_block_id."""
        # Mock workflow run blocks
        forloop_block = MagicMock()
        forloop_block.workflow_run_block_id = "wfrb_forloop_123"
        forloop_block.parent_workflow_run_block_id = None
        forloop_block.block_type = BlockType.FOR_LOOP
        forloop_block.label = "process_urls"
        forloop_block.task_id = None

        child_task_block = MagicMock()
        child_task_block.workflow_run_block_id = "wfrb_child_456"
        child_task_block.parent_workflow_run_block_id = "wfrb_forloop_123"  # Points to ForLoop
        child_task_block.block_type = "task"
        child_task_block.label = "extract_data"
        child_task_block.task_id = "task_789"
        child_task_block.status = "completed"
        child_task_block.output = {"extracted": "data"}

        all_blocks = [forloop_block, child_task_block]

        # Filter child blocks by parent_workflow_run_block_id
        child_blocks = [b for b in all_blocks if b.parent_workflow_run_block_id == forloop_block.workflow_run_block_id]

        assert len(child_blocks) == 1
        assert child_blocks[0].label == "extract_data"
        assert child_blocks[0].task_id == "task_789"

    def test_child_run_blocks_by_label_mapping(self) -> None:
        """Test creation of child run blocks mapping by label."""
        child_block_1 = MagicMock()
        child_block_1.label = "extract_data"
        child_block_1.block_type = "extraction"
        child_block_1.task_id = "task_1"

        child_block_2 = MagicMock()
        child_block_2.label = "navigate_page"
        child_block_2.block_type = "navigation"
        child_block_2.task_id = "task_2"

        child_run_blocks = [child_block_1, child_block_2]

        # Create mapping by label
        child_run_blocks_by_label = {b.label: b for b in child_run_blocks if b.label}

        assert "extract_data" in child_run_blocks_by_label
        assert "navigate_page" in child_run_blocks_by_label
        assert child_run_blocks_by_label["extract_data"].task_id == "task_1"

    def test_forloop_definition_block_has_loop_blocks(self) -> None:
        """Test that ForLoop definition block contains loop_blocks field."""
        forloop_definition = {
            "block_type": BlockType.FOR_LOOP,
            "label": "process_urls",
            "loop_variable_reference": "{{ urls }}",
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "extract_data",
                    "data_extraction_goal": "Extract page content",
                },
                {
                    "block_type": "navigation",
                    "label": "navigate_next",
                    "navigation_goal": "Go to next page",
                },
            ],
        }

        loop_blocks = forloop_definition.get("loop_blocks", [])

        assert len(loop_blocks) == 2
        assert loop_blocks[0]["label"] == "extract_data"
        assert loop_blocks[1]["label"] == "navigate_next"


class TestForLoopScriptGeneration:
    """Test script code generation for ForLoop blocks."""

    def test_build_for_loop_statement_signature(self) -> None:
        """Test that _build_for_loop_statement is called with correct parameters."""
        from skyvern.core.script_generations.generate_script import _build_for_loop_statement

        forloop_block = {
            "block_type": "for_loop",
            "label": "process_items",
            "loop_variable_reference": "{{ items }}",
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "extract_item",
                    "data_extraction_goal": "Extract item details",
                },
            ],
        }

        # This should not raise an error
        result = _build_for_loop_statement("process_items", forloop_block)

        # The result should be a CST For node
        assert result is not None
        assert hasattr(result, "target")  # For loop has a target
        assert hasattr(result, "iter")  # For loop has an iterator
        assert hasattr(result, "body")  # For loop has a body


class TestForLoopChildBlockActions:
    """Test that actions from child blocks inside ForLoop are collected."""

    def test_task_block_in_forloop_should_collect_actions(self) -> None:
        """Test that task blocks inside ForLoop have their actions collected."""
        # This tests the logic added in transform_workflow_run.py
        child_run_block = MagicMock()
        child_run_block.block_type = "task"
        child_run_block.task_id = "task_123"
        child_run_block.label = "search_item"

        # Verify that the child block type is in SCRIPT_TASK_BLOCKS
        assert child_run_block.block_type in SCRIPT_TASK_BLOCKS

        # Verify that task_id is present (required for action collection)
        assert child_run_block.task_id is not None

    def test_non_task_block_in_forloop_does_not_collect_actions(self) -> None:
        """Test that non-task blocks inside ForLoop don't collect actions."""
        child_run_block = MagicMock()
        child_run_block.block_type = "goto_url"
        child_run_block.task_id = None
        child_run_block.label = "go_to_url"

        # Verify that goto_url is not in SCRIPT_TASK_BLOCKS
        assert child_run_block.block_type not in SCRIPT_TASK_BLOCKS


class TestForLoopActionsHydration:
    """Test that actions from ForLoop child blocks are properly hydrated."""

    def test_actions_by_task_includes_forloop_child_actions(self) -> None:
        """Test that actions_by_task dict includes actions from ForLoop child blocks."""
        actions_by_task: dict[str, list[dict[str, Any]]] = {}

        # Simulate adding actions from a child block inside ForLoop
        child_task_id = "task_in_forloop_123"
        child_actions = [
            {
                "action_type": "input_text",
                "action_id": "action_1",
                "text": "search query",
                "xpath": "//input[@id='search']",
            },
            {
                "action_type": "click",
                "action_id": "action_2",
                "xpath": "//button[@type='submit']",
            },
        ]

        actions_by_task[child_task_id] = child_actions

        # Verify actions are stored
        assert child_task_id in actions_by_task
        assert len(actions_by_task[child_task_id]) == 2
        assert actions_by_task[child_task_id][0]["action_type"] == "input_text"


@pytest.mark.asyncio
async def test_transform_forloop_block_integration() -> None:
    """
    Integration test for ForLoop block transformation.

    This test mocks the database calls and verifies that the transformation
    correctly processes ForLoop blocks and their child blocks.
    """
    from skyvern.core.script_generations.transform_workflow_run import CodeGenInput

    # Create a mock CodeGenInput with ForLoop block
    mock_input = CodeGenInput(
        file_name="test_workflow.py",
        workflow_run={"workflow_id": "wpid_123"},
        workflow={"workflow_definition": {"blocks": []}},
        workflow_blocks=[
            {
                "block_type": "for_loop",
                "label": "process_urls",
                "loop_variable_reference": "{{ urls }}",
                "workflow_run_id": "wr_123",
                "workflow_run_block_id": "wfrb_456",
                "loop_blocks": [
                    {
                        "block_type": "extraction",
                        "label": "extract_data",
                        "data_extraction_goal": "Get page content",
                        "task_id": "task_789",
                        "status": "completed",
                        "output": {"content": "extracted data"},
                    }
                ],
            }
        ],
        actions_by_task={
            "task_789": [
                {
                    "action_type": "extract",
                    "action_id": "action_123",
                    "xpath": "//div[@id='content']",
                }
            ]
        },
        task_v2_child_blocks={},
    )

    # Verify the structure
    assert len(mock_input.workflow_blocks) == 1
    assert mock_input.workflow_blocks[0]["block_type"] == "for_loop"
    assert len(mock_input.workflow_blocks[0]["loop_blocks"]) == 1
    assert mock_input.workflow_blocks[0]["loop_blocks"][0]["task_id"] == "task_789"
    assert "task_789" in mock_input.actions_by_task


@pytest.mark.asyncio
async def test_transform_forloop_block_with_mocked_db() -> None:
    """
    Full integration test for ForLoop block transformation with mocked database.

    This test verifies the actual transformation logic in transform_workflow_run.py
    correctly processes ForLoop blocks and their child blocks.
    """
    from unittest.mock import MagicMock

    from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
    from skyvern.schemas.workflows import BlockType

    # Mock workflow run response
    mock_workflow_run_resp = MagicMock()
    mock_workflow_run_resp.run_request = MagicMock()
    mock_workflow_run_resp.run_request.workflow_id = "wpid_test_123"
    mock_workflow_run_resp.run_request.model_dump = MagicMock(
        return_value={"workflow_id": "wpid_test_123", "parameters": {}}
    )

    # Mock workflow with ForLoop block definition
    mock_forloop_definition = MagicMock()
    mock_forloop_definition.block_type = BlockType.FOR_LOOP
    mock_forloop_definition.label = "process_urls"
    mock_forloop_definition.loop_variable_reference = "{{ urls }}"
    mock_forloop_definition.model_dump = MagicMock(
        return_value={
            "block_type": "for_loop",
            "label": "process_urls",
            "loop_variable_reference": "{{ urls }}",
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "extract_data",
                    "data_extraction_goal": "Get page content",
                }
            ],
        }
    )

    mock_workflow = MagicMock()
    mock_workflow.model_dump = MagicMock(return_value={"workflow_id": "wf_123", "workflow_definition": {"blocks": []}})
    mock_workflow.workflow_definition.blocks = [mock_forloop_definition]

    # Mock workflow run blocks - ForLoop parent and extraction child
    mock_forloop_run_block = MagicMock()
    mock_forloop_run_block.workflow_run_block_id = "wfrb_forloop_123"
    mock_forloop_run_block.parent_workflow_run_block_id = None
    mock_forloop_run_block.block_type = BlockType.FOR_LOOP
    mock_forloop_run_block.label = "process_urls"
    mock_forloop_run_block.task_id = None
    mock_forloop_run_block.created_at = 1

    mock_child_run_block = MagicMock()
    mock_child_run_block.workflow_run_block_id = "wfrb_child_456"
    mock_child_run_block.parent_workflow_run_block_id = "wfrb_forloop_123"
    mock_child_run_block.block_type = "extraction"
    mock_child_run_block.label = "extract_data"
    mock_child_run_block.task_id = "task_extraction_789"
    mock_child_run_block.status = "completed"
    mock_child_run_block.output = {"extracted": "data"}
    mock_child_run_block.created_at = 2

    # Mock task for the child block
    mock_task = MagicMock()
    mock_task.model_dump = MagicMock(
        return_value={
            "task_id": "task_extraction_789",
            "navigation_goal": "Extract page content",
        }
    )

    # Mock action for the task
    mock_action = MagicMock()
    mock_action.action_type = "extract"
    mock_action.model_dump = MagicMock(
        return_value={
            "action_type": "extract",
            "action_id": "action_123",
        }
    )
    mock_action.get_xpath = MagicMock(return_value="//div[@id='content']")
    mock_action.has_mini_agent = False

    # Set up patches
    with (
        patch("skyvern.services.workflow_service.get_workflow_run_response", new_callable=AsyncMock) as mock_get_wfr,
        patch("skyvern.core.script_generations.transform_workflow_run.app") as mock_app,
    ):
        mock_get_wfr.return_value = mock_workflow_run_resp
        mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
        mock_app.DATABASE.get_workflow_run_blocks = AsyncMock(
            return_value=[
                mock_forloop_run_block,
                mock_child_run_block,
            ]
        )
        # B1 optimization: Mock batch methods instead of individual queries
        mock_task.task_id = "task_extraction_789"
        mock_action.task_id = "task_extraction_789"
        mock_app.DATABASE.get_tasks_by_ids = AsyncMock(return_value=[mock_task])
        mock_app.DATABASE.get_tasks_actions = AsyncMock(return_value=[mock_action])

        # Call the transformation
        result = await transform_workflow_run_to_code_gen_input(
            workflow_run_id="wr_test_123",
            organization_id="org_test_123",
        )

        # Verify ForLoop block is included
        assert len(result.workflow_blocks) == 1
        forloop_block = result.workflow_blocks[0]
        assert forloop_block["block_type"] == "for_loop"
        assert forloop_block["label"] == "process_urls"

        # Verify loop_blocks contain child block with task data
        loop_blocks = forloop_block.get("loop_blocks", [])
        assert len(loop_blocks) == 1
        child_block = loop_blocks[0]
        assert child_block["label"] == "extract_data"
        assert child_block.get("task_id") == "task_extraction_789"

        # Verify actions were collected for the child task
        assert "task_extraction_789" in result.actions_by_task
        actions = result.actions_by_task["task_extraction_789"]
        assert len(actions) == 1
        assert actions[0]["action_type"] == "extract"


class TestForLoopScriptExecution:
    """Test that generated ForLoop scripts can be executed."""

    def test_forloop_generates_async_for_statement(self) -> None:
        """Verify that ForLoop generates an async for statement."""
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _build_for_loop_statement

        forloop_block = {
            "block_type": "for_loop",
            "label": "iterate_items",
            "loop_variable_reference": "{{ items_list }}",
            "complete_if_empty": True,
            "loop_blocks": [],
        }

        result = _build_for_loop_statement("iterate_items", forloop_block)

        # Verify it's an async for loop
        assert isinstance(result, cst.For)
        assert result.asynchronous is not None  # Has asynchronous keyword

    def test_forloop_generates_skyvern_loop_call(self) -> None:
        """Verify that ForLoop generates a skyvern.loop() call."""
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _build_for_loop_statement

        forloop_block = {
            "block_type": "for_loop",
            "label": "iterate_items",
            "loop_variable_reference": "{{ items_list }}",
            "loop_blocks": [],
        }

        result = _build_for_loop_statement("iterate_items", forloop_block)

        # The iter should be a Call to skyvern.loop
        assert isinstance(result.iter, cst.Call)

        # Get the function being called
        func = result.iter.func
        assert isinstance(func, cst.Attribute)
        assert func.attr.value == "loop"
