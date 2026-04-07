"""Tests for nested for-loop script generation and transformation (SKY-8757).

Covers:
1. _process_forloop_children in transform_workflow_run.py — recursive merging
   of task data for nested for-loop children.
2. generate_workflow_script_python_code in generate_script.py — code generation
   for nested for-loop inner blocks (script_block creation + function bodies).
"""

import ast
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.transform_workflow_run import (
    _build_children_by_parent,
    _process_forloop_children,
)
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.actions.actions import Action


# ---------------------------------------------------------------------------
# Part 1: _process_forloop_children tests
# ---------------------------------------------------------------------------
class TestProcessForloopChildren:
    """Test recursive merging of for-loop children in transform_workflow_run."""

    def test_single_level_merges_task_data(self) -> None:
        """Direct task children get task_id and actions merged."""
        forloop_run_block = MagicMock()
        forloop_run_block.workflow_run_block_id = "wfrb_outer"
        forloop_run_block.label = "outer_loop"

        child_run_block = MagicMock()
        child_run_block.workflow_run_block_id = "wfrb_child"
        child_run_block.parent_workflow_run_block_id = "wfrb_outer"
        child_run_block.block_type = "extraction"
        child_run_block.label = "extract_data"
        child_run_block.task_id = "task_1"
        child_run_block.status = "completed"
        child_run_block.output = {"data": "extracted"}
        child_run_block.workflow_run_id = "wr_1"

        mock_task = MagicMock()
        mock_task.model_dump.return_value = {"task_id": "task_1", "navigation_goal": "Extract"}

        mock_action = MagicMock(spec=Action)
        mock_action.model_dump.return_value = {"action_type": "extract", "action_id": "a1"}
        mock_action.get_xpath.return_value = "//div"
        mock_action.has_mini_agent = False
        mock_action.action_type = "extract"
        mock_action.task_id = "task_1"

        loop_blocks_def = [
            {"block_type": "extraction", "label": "extract_data", "data_extraction_goal": "Extract"},
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}
        all_blocks = [forloop_run_block, child_run_block]

        result = _process_forloop_children(
            forloop_run_block=forloop_run_block,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_blocks),
            tasks_by_id={"task_1": mock_task},
            actions_by_task_id={"task_1": [mock_action]},
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        assert result[0]["task_id"] == "task_1"
        assert result[0]["status"] == "completed"
        assert "task_1" in actions_by_task

    def test_nested_forloop_recurses_into_children(self) -> None:
        """Nested for-loop's children should get task data merged recursively."""
        outer_run = MagicMock()
        outer_run.workflow_run_block_id = "wfrb_outer"
        outer_run.label = "outer_loop"

        inner_run = MagicMock()
        inner_run.workflow_run_block_id = "wfrb_inner"
        inner_run.parent_workflow_run_block_id = "wfrb_outer"
        inner_run.block_type = BlockType.FOR_LOOP
        inner_run.label = "inner_loop"
        inner_run.task_id = None
        inner_run.workflow_run_id = "wr_1"

        grandchild_run = MagicMock()
        grandchild_run.workflow_run_block_id = "wfrb_grandchild"
        grandchild_run.parent_workflow_run_block_id = "wfrb_inner"
        grandchild_run.block_type = "extraction"
        grandchild_run.label = "deep_extract"
        grandchild_run.task_id = "task_deep"
        grandchild_run.status = "completed"
        grandchild_run.output = {"deep": True}
        grandchild_run.workflow_run_id = "wr_1"

        mock_task = MagicMock()
        mock_task.model_dump.return_value = {"task_id": "task_deep"}

        mock_action = MagicMock(spec=Action)
        mock_action.model_dump.return_value = {"action_type": "extract"}
        mock_action.get_xpath.return_value = "//span"
        mock_action.has_mini_agent = False
        mock_action.action_type = "extract"
        mock_action.task_id = "task_deep"

        loop_blocks_def = [
            {
                "block_type": "for_loop",
                "label": "inner_loop",
                "loop_blocks": [
                    {"block_type": "extraction", "label": "deep_extract", "data_extraction_goal": "Deep extract"},
                ],
            },
        ]

        all_run_blocks = [outer_run, inner_run, grandchild_run]
        actions_by_task: dict[str, list[dict[str, Any]]] = {}

        result = _process_forloop_children(
            forloop_run_block=outer_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_run_blocks),
            tasks_by_id={"task_deep": mock_task},
            actions_by_task_id={"task_deep": [mock_action]},
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        inner_loop = result[0]
        assert inner_loop["block_type"] == "for_loop"
        assert inner_loop["workflow_run_block_id"] == "wfrb_inner"

        # Verify the grandchild got task data merged
        inner_children = inner_loop.get("loop_blocks", [])
        assert len(inner_children) == 1
        assert inner_children[0]["task_id"] == "task_deep"
        assert "task_deep" in actions_by_task

    def test_multi_iteration_picks_best_task_block(self) -> None:
        """When the outer loop iterates multiple times, the run block with task_id should win."""
        outer_run = MagicMock()
        outer_run.workflow_run_block_id = "wfrb_outer"
        outer_run.parent_workflow_run_block_id = None
        outer_run.label = "outer_loop"

        # Iteration 1: has task_id (good)
        child_iter1 = MagicMock()
        child_iter1.workflow_run_block_id = "wfrb_child_iter1"
        child_iter1.parent_workflow_run_block_id = "wfrb_outer"
        child_iter1.block_type = "extraction"
        child_iter1.label = "extract_data"
        child_iter1.task_id = "task_good"
        child_iter1.status = "completed"
        child_iter1.output = {"data": True}
        child_iter1.workflow_run_id = "wr_1"

        # Iteration 2: no task_id (empty iteration)
        child_iter2 = MagicMock()
        child_iter2.workflow_run_block_id = "wfrb_child_iter2"
        child_iter2.parent_workflow_run_block_id = "wfrb_outer"
        child_iter2.block_type = "extraction"
        child_iter2.label = "extract_data"
        child_iter2.task_id = None
        child_iter2.status = None
        child_iter2.output = None
        child_iter2.workflow_run_id = "wr_1"

        mock_task = MagicMock()
        mock_task.model_dump.return_value = {"task_id": "task_good"}

        mock_action = MagicMock(spec=Action)
        mock_action.model_dump.return_value = {"action_type": "extract"}
        mock_action.get_xpath.return_value = "//div"
        mock_action.has_mini_agent = False
        mock_action.action_type = "extract"
        mock_action.task_id = "task_good"

        loop_blocks_def = [
            {"block_type": "extraction", "label": "extract_data", "data_extraction_goal": "Extract"},
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}
        # child_iter2 comes after child_iter1 — old code would keep iter2 (no task_id)
        all_blocks = [outer_run, child_iter1, child_iter2]

        result = _process_forloop_children(
            forloop_run_block=outer_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_blocks),
            tasks_by_id={"task_good": mock_task},
            actions_by_task_id={"task_good": [mock_action]},
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        # Should pick the block with task_id, not the empty one
        assert result[0]["task_id"] == "task_good"
        assert "task_good" in actions_by_task

    def test_multi_iteration_prefers_richer_actions(self) -> None:
        """When both iterations have task_id, prefer the one with more actions."""
        outer_run = MagicMock()
        outer_run.workflow_run_block_id = "wfrb_outer"
        outer_run.parent_workflow_run_block_id = None
        outer_run.label = "outer_loop"

        # Iteration 1: has task_id but only 1 action (partial)
        child_iter1 = MagicMock()
        child_iter1.workflow_run_block_id = "wfrb_child_iter1"
        child_iter1.parent_workflow_run_block_id = "wfrb_outer"
        child_iter1.block_type = "extraction"
        child_iter1.label = "extract_data"
        child_iter1.task_id = "task_partial"
        child_iter1.status = "completed"
        child_iter1.output = {}
        child_iter1.workflow_run_id = "wr_1"

        # Iteration 2: has task_id with 3 actions (richer)
        child_iter2 = MagicMock()
        child_iter2.workflow_run_block_id = "wfrb_child_iter2"
        child_iter2.parent_workflow_run_block_id = "wfrb_outer"
        child_iter2.block_type = "extraction"
        child_iter2.label = "extract_data"
        child_iter2.task_id = "task_rich"
        child_iter2.status = "completed"
        child_iter2.output = {"data": "full"}
        child_iter2.workflow_run_id = "wr_1"

        mock_task_partial = MagicMock()
        mock_task_partial.model_dump.return_value = {"task_id": "task_partial"}
        mock_task_rich = MagicMock()
        mock_task_rich.model_dump.return_value = {"task_id": "task_rich"}

        def _make_action(task_id: str) -> MagicMock:
            a = MagicMock(spec=Action)
            a.model_dump.return_value = {"action_type": "extract"}
            a.get_xpath.return_value = "//div"
            a.has_mini_agent = False
            a.action_type = "extract"
            a.task_id = task_id
            return a

        loop_blocks_def = [
            {"block_type": "extraction", "label": "extract_data", "data_extraction_goal": "Extract"},
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}
        all_blocks = [outer_run, child_iter1, child_iter2]

        result = _process_forloop_children(
            forloop_run_block=outer_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_blocks),
            tasks_by_id={"task_partial": mock_task_partial, "task_rich": mock_task_rich},
            actions_by_task_id={
                "task_partial": [_make_action("task_partial")],
                "task_rich": [_make_action("task_rich"), _make_action("task_rich"), _make_action("task_rich")],
            },
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        # Should pick task_rich (3 actions) over task_partial (1 action)
        assert result[0]["task_id"] == "task_rich"
        assert "task_rich" in actions_by_task

    def test_multi_iteration_nested_forloop_picks_richest(self) -> None:
        """When a nested for-loop has multiple iterations, pick the one with most grandchildren."""
        outer_run = MagicMock()
        outer_run.workflow_run_block_id = "wfrb_outer"
        outer_run.parent_workflow_run_block_id = None
        outer_run.label = "outer_loop"

        # Iteration 1 of inner loop: has grandchildren
        inner_iter1 = MagicMock()
        inner_iter1.workflow_run_block_id = "wfrb_inner_iter1"
        inner_iter1.parent_workflow_run_block_id = "wfrb_outer"
        inner_iter1.block_type = BlockType.FOR_LOOP
        inner_iter1.label = "inner_loop"
        inner_iter1.task_id = None
        inner_iter1.workflow_run_id = "wr_1"

        grandchild = MagicMock()
        grandchild.workflow_run_block_id = "wfrb_grandchild"
        grandchild.parent_workflow_run_block_id = "wfrb_inner_iter1"
        grandchild.block_type = "extraction"
        grandchild.label = "deep_extract"
        grandchild.task_id = "task_deep"
        grandchild.status = "completed"
        grandchild.output = {"deep": True}
        grandchild.workflow_run_id = "wr_1"

        # Iteration 2 of inner loop: empty (no grandchildren)
        inner_iter2 = MagicMock()
        inner_iter2.workflow_run_block_id = "wfrb_inner_iter2"
        inner_iter2.parent_workflow_run_block_id = "wfrb_outer"
        inner_iter2.block_type = BlockType.FOR_LOOP
        inner_iter2.label = "inner_loop"
        inner_iter2.task_id = None
        inner_iter2.workflow_run_id = "wr_1"

        mock_task = MagicMock()
        mock_task.model_dump.return_value = {"task_id": "task_deep"}

        mock_action = MagicMock(spec=Action)
        mock_action.model_dump.return_value = {"action_type": "extract"}
        mock_action.get_xpath.return_value = "//span"
        mock_action.has_mini_agent = False
        mock_action.action_type = "extract"
        mock_action.task_id = "task_deep"

        loop_blocks_def = [
            {
                "block_type": "for_loop",
                "label": "inner_loop",
                "loop_blocks": [
                    {"block_type": "extraction", "label": "deep_extract", "data_extraction_goal": "Deep"},
                ],
            },
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}
        # inner_iter2 comes last but has no grandchildren — should pick inner_iter1
        all_blocks = [outer_run, inner_iter1, grandchild, inner_iter2]

        result = _process_forloop_children(
            forloop_run_block=outer_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_blocks),
            tasks_by_id={"task_deep": mock_task},
            actions_by_task_id={"task_deep": [mock_action]},
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        inner = result[0]
        # Should have picked inner_iter1 (has grandchildren)
        assert inner["workflow_run_block_id"] == "wfrb_inner_iter1"
        # Grandchild should have task data merged
        inner_children = inner.get("loop_blocks", [])
        assert len(inner_children) == 1
        assert inner_children[0]["task_id"] == "task_deep"
        assert "task_deep" in actions_by_task

    def test_nested_forloop_tie_broken_by_descendant_actions(self) -> None:
        """When two nested for-loop iterations have the same child count,
        prefer the one whose descendants have more actions."""
        outer_run = MagicMock()
        outer_run.workflow_run_block_id = "wfrb_outer"
        outer_run.parent_workflow_run_block_id = None
        outer_run.label = "outer_loop"

        # Iteration 1: 1 grandchild, no actions
        inner_iter1 = MagicMock()
        inner_iter1.workflow_run_block_id = "wfrb_inner_iter1"
        inner_iter1.parent_workflow_run_block_id = "wfrb_outer"
        inner_iter1.block_type = BlockType.FOR_LOOP
        inner_iter1.label = "inner_loop"
        inner_iter1.task_id = None
        inner_iter1.workflow_run_id = "wr_1"

        gc1 = MagicMock()
        gc1.workflow_run_block_id = "wfrb_gc1"
        gc1.parent_workflow_run_block_id = "wfrb_inner_iter1"
        gc1.block_type = "extraction"
        gc1.label = "deep_extract"
        gc1.task_id = "task_empty"
        gc1.status = "completed"
        gc1.output = {}
        gc1.workflow_run_id = "wr_1"

        # Iteration 2: 1 grandchild, with actions (richer)
        inner_iter2 = MagicMock()
        inner_iter2.workflow_run_block_id = "wfrb_inner_iter2"
        inner_iter2.parent_workflow_run_block_id = "wfrb_outer"
        inner_iter2.block_type = BlockType.FOR_LOOP
        inner_iter2.label = "inner_loop"
        inner_iter2.task_id = None
        inner_iter2.workflow_run_id = "wr_1"

        gc2 = MagicMock()
        gc2.workflow_run_block_id = "wfrb_gc2"
        gc2.parent_workflow_run_block_id = "wfrb_inner_iter2"
        gc2.block_type = "extraction"
        gc2.label = "deep_extract"
        gc2.task_id = "task_rich"
        gc2.status = "completed"
        gc2.output = {"data": True}
        gc2.workflow_run_id = "wr_1"

        mock_task = MagicMock()
        mock_task.model_dump.return_value = {"task_id": "task_rich"}

        mock_action = MagicMock(spec=Action)
        mock_action.model_dump.return_value = {"action_type": "extract"}
        mock_action.get_xpath.return_value = "//div"
        mock_action.has_mini_agent = False
        mock_action.action_type = "extract"
        mock_action.task_id = "task_rich"

        loop_blocks_def = [
            {
                "block_type": "for_loop",
                "label": "inner_loop",
                "loop_blocks": [
                    {"block_type": "extraction", "label": "deep_extract", "data_extraction_goal": "Extract"},
                ],
            },
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}
        # Both iterations have 1 grandchild, but only iter2's grandchild has actions
        all_blocks = [outer_run, inner_iter1, gc1, inner_iter2, gc2]

        result = _process_forloop_children(
            forloop_run_block=outer_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent(all_blocks),
            tasks_by_id={"task_rich": mock_task},
            actions_by_task_id={"task_rich": [mock_action]},  # only task_rich has actions
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        inner = result[0]
        # Should pick iter2 (descendant has actions) over iter1 (no actions)
        assert inner["workflow_run_block_id"] == "wfrb_inner_iter2"

    def test_no_matching_run_block_preserves_definition(self) -> None:
        """If no run block matches a definition child, the definition is preserved unchanged."""
        forloop_run = MagicMock()
        forloop_run.workflow_run_block_id = "wfrb_loop"
        forloop_run.label = "loop"

        loop_blocks_def = [
            {"block_type": "extraction", "label": "unexecuted_block"},
        ]

        actions_by_task: dict[str, list[dict[str, Any]]] = {}

        result = _process_forloop_children(
            forloop_run_block=forloop_run,
            loop_blocks_def=loop_blocks_def,
            children_by_parent=_build_children_by_parent([forloop_run]),
            tasks_by_id={},
            actions_by_task_id={},
            actions_by_task=actions_by_task,
        )

        assert len(result) == 1
        assert result[0]["label"] == "unexecuted_block"
        assert "task_id" not in result[0]


# ---------------------------------------------------------------------------
# Part 2: generate_workflow_script_python_code tests for nested for-loops
# ---------------------------------------------------------------------------
class TestNestedForloopCodeGeneration:
    """Test that nested for-loops generate correct script blocks and code."""

    @pytest.mark.asyncio
    async def test_nested_forloop_creates_script_blocks_for_all_levels(self) -> None:
        """A double-nested for-loop should create script_blocks for:
        1. Outer for-loop
        2. Inner for-loop
        3. Inner task blocks
        """
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        blocks = [
            {
                "block_type": "for_loop",
                "label": "outer_loop",
                "loop_variable_reference": "{{ urls }}",
                "workflow_run_block_id": "wfrb_outer",
                "loop_blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "inner_loop",
                        "loop_variable_reference": "{{ documents }}",
                        "workflow_run_block_id": "wfrb_inner",
                        "loop_blocks": [
                            {
                                "block_type": "extraction",
                                "label": "extract_data",
                                "data_extraction_goal": "Get content",
                                "task_id": "task_extract",
                                "workflow_run_block_id": "wfrb_extract",
                            },
                        ],
                    },
                ],
            },
        ]

        actions_by_task = {
            "task_extract": [
                {
                    "action_type": "extract",
                    "action_id": "action_1",
                    "xpath": "//div[@id='content']",
                    "element_id": "elem_1",
                    "text": None,
                    "data_extraction_goal": "Get content",
                },
            ],
        }

        workflow = {
            "workflow_id": "wf_test",
            "title": "Nested ForLoop Test",
            "workflow_definition": {"parameters": []},
        }

        mock_create_script_block = AsyncMock(return_value=True)

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                mock_create_script_block,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test_nested.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="script_1",
                script_revision_id="rev_1",
                organization_id="org_1",
            )

            # Must compile
            try:
                ast.parse(result.source_code)
            except SyntaxError as e:
                pytest.fail(f"Generated script has SyntaxError: {e}\n\n{result.source_code}")

            # Verify script blocks created for all three levels
            call_labels = [call.kwargs.get("block_label") for call in mock_create_script_block.call_args_list]
            assert "outer_loop" in call_labels, f"outer for-loop missing. Labels: {call_labels}"
            assert "inner_loop" in call_labels, f"inner for-loop missing. Labels: {call_labels}"
            assert "extract_data" in call_labels, f"inner extraction missing. Labels: {call_labels}"

    @pytest.mark.asyncio
    async def test_nested_forloop_inner_block_gets_cached_function(self) -> None:
        """The extraction block inside a nested for-loop should get a @skyvern.cached function."""
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        blocks = [
            {
                "block_type": "for_loop",
                "label": "page_loop",
                "loop_variable_reference": "{{ pages }}",
                "workflow_run_block_id": "wfrb_page",
                "loop_blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "doc_loop",
                        "loop_variable_reference": "{{ docs }}",
                        "workflow_run_block_id": "wfrb_doc",
                        "loop_blocks": [
                            {
                                "block_type": "file_download",
                                "label": "download_file",
                                "url": "https://example.com",
                                "navigation_goal": "Download the file",
                                "task_id": "task_download",
                                "workflow_run_block_id": "wfrb_download",
                            },
                        ],
                    },
                ],
            },
        ]

        actions_by_task = {
            "task_download": [
                {
                    "action_type": "click",
                    "action_id": "act_1",
                    "xpath": "//a[@class='download']",
                    "element_id": "elem_dl",
                    "text": None,
                },
            ],
        }

        workflow = {
            "workflow_id": "wf_test",
            "title": "Nested Download Test",
            "workflow_definition": {"parameters": []},
        }

        mock_create_script_block = AsyncMock(return_value=True)

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                mock_create_script_block,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test_nested_download.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="script_1",
                script_revision_id="rev_1",
                organization_id="org_1",
            )

            try:
                ast.parse(result.source_code)
            except SyntaxError as e:
                pytest.fail(f"SyntaxError: {e}\n\n{result.source_code}")

            # Inner block should have a @skyvern.cached function
            assert "@skyvern.cached" in result.source_code
            assert "download_file" in result.source_code

    @pytest.mark.asyncio
    async def test_nested_forloop_labels_tracked_in_processed_labels(self) -> None:
        """Nested for-loop labels should be tracked to avoid duplication
        when preserving unexecuted branch cached blocks."""
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        blocks = [
            {
                "block_type": "for_loop",
                "label": "outer_loop",
                "loop_variable_reference": "{{ urls }}",
                "workflow_run_block_id": "wfrb_outer",
                "loop_blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "inner_loop",
                        "loop_variable_reference": "{{ items }}",
                        "workflow_run_block_id": "wfrb_inner",
                        "loop_blocks": [
                            {
                                "block_type": "extraction",
                                "label": "deep_extract",
                                "data_extraction_goal": "Extract",
                                "task_id": "task_deep",
                                "workflow_run_block_id": "wfrb_deep",
                            },
                        ],
                    },
                ],
            },
        ]

        actions_by_task = {
            "task_deep": [
                {
                    "action_type": "extract",
                    "action_id": "a1",
                    "xpath": "//div",
                    "element_id": "e1",
                    "text": None,
                    "data_extraction_goal": "Extract",
                },
            ],
        }

        # Also provide the same labels as cached_blocks to test dedup
        mock_cached_extract = MagicMock()
        mock_cached_extract.code = "@skyvern.cached(cache_key='deep_extract')\nasync def deep_extract_fn(): pass"
        mock_cached_extract.run_signature = "await skyvern.extract(prompt='Extract', label='deep_extract')"
        mock_cached_extract.workflow_run_id = "wr_old"
        mock_cached_extract.workflow_run_block_id = "wfrb_old"
        mock_cached_extract.input_fields = None

        mock_cached_loop = MagicMock()
        mock_cached_loop.code = (
            "async for current_value in skyvern.loop(values='{{ items }}', label='inner_loop'): pass"
        )
        mock_cached_loop.run_signature = (
            "async for current_value in skyvern.loop(values='{{ items }}', label='inner_loop'): pass"
        )
        mock_cached_loop.workflow_run_id = "wr_old"
        mock_cached_loop.workflow_run_block_id = "wfrb_old"
        mock_cached_loop.input_fields = None

        workflow = {
            "workflow_id": "wf_test",
            "title": "Dedup Test",
            "workflow_definition": {"parameters": []},
        }

        mock_create_script_block = AsyncMock(return_value=True)

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                mock_create_script_block,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test_dedup.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="s1",
                script_revision_id="r1",
                organization_id="o1",
                cached_blocks={
                    "deep_extract": mock_cached_extract,
                    "inner_loop": mock_cached_loop,
                },
            )

            try:
                ast.parse(result.source_code)
            except SyntaxError as e:
                pytest.fail(f"SyntaxError: {e}\n\n{result.source_code}")

            # @skyvern.cached should appear exactly once (not duplicated by
            # the "preserve unexecuted branch" section)
            cached_count = result.source_code.count("@skyvern.cached")
            assert cached_count == 1, (
                f"Expected 1 @skyvern.cached but found {cached_count}. "
                f"Nested labels may not be tracked in processed_labels.\n\n{result.source_code}"
            )

    @pytest.mark.asyncio
    async def test_nested_forloop_uses_cached_entry_when_valid(self) -> None:
        """When a nested for-loop has a valid cached entry and is NOT in
        updated_block_labels, create_or_update_script_block should still be
        called (to persist metadata) but use the cached code, not rebuild."""
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        cached_inner_loop = MagicMock()
        cached_inner_loop.code = (
            "async for current_value in skyvern.loop(values='{{ docs }}', label='inner_loop'):\n    pass"
        )
        cached_inner_loop.run_signature = cached_inner_loop.code.strip()
        cached_inner_loop.workflow_run_id = "wr_cached"
        cached_inner_loop.workflow_run_block_id = "wfrb_cached"
        cached_inner_loop.input_fields = None

        cached_deep_extract = MagicMock()
        cached_deep_extract.code = "@skyvern.cached(cache_key='deep_extract')\nasync def deep_extract_fn(page, context):\n    await skyvern.extract(prompt='Get data', label='deep_extract')"
        cached_deep_extract.run_signature = "await skyvern.extract(prompt='Get data', label='deep_extract')"
        cached_deep_extract.workflow_run_id = "wr_cached"
        cached_deep_extract.workflow_run_block_id = "wfrb_cached_deep"
        cached_deep_extract.input_fields = None

        blocks = [
            {
                "block_type": "for_loop",
                "label": "outer_loop",
                "loop_variable_reference": "{{ urls }}",
                "workflow_run_block_id": "wfrb_outer",
                "loop_blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "inner_loop",
                        "loop_variable_reference": "{{ docs }}",
                        "workflow_run_block_id": "wfrb_inner",
                        "loop_blocks": [
                            {
                                "block_type": "extraction",
                                "label": "deep_extract",
                                "data_extraction_goal": "Get data",
                                "task_id": "task_deep",
                                "workflow_run_block_id": "wfrb_deep",
                            },
                        ],
                    },
                ],
            },
        ]

        workflow = {
            "workflow_id": "wf_test",
            "title": "Cache Hit Test",
            "workflow_definition": {"parameters": []},
        }

        mock_create_script_block = AsyncMock(return_value=True)

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                mock_create_script_block,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test_cache_hit.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task={},  # No fresh actions — relying on cache
                script_id="s1",
                script_revision_id="r1",
                organization_id="o1",
                cached_blocks={
                    "inner_loop": cached_inner_loop,
                    "deep_extract": cached_deep_extract,
                },
                # Neither inner_loop nor its parent outer_loop are in
                # updated_block_labels → should use cached entries.
                updated_block_labels={"__start_block__"},
            )

            try:
                ast.parse(result.source_code)
            except SyntaxError as e:
                pytest.fail(f"SyntaxError: {e}\n\n{result.source_code}")

            # The nested for-loop's cached code should be used
            call_labels = [call.kwargs.get("block_label") for call in mock_create_script_block.call_args_list]
            # inner_loop and deep_extract should both have script_block entries
            assert "inner_loop" in call_labels, f"inner_loop missing from calls: {call_labels}"
            assert "deep_extract" in call_labels, f"deep_extract missing from calls: {call_labels}"

            # The inner_loop call should use the cached code, not freshly built
            inner_loop_call = next(
                c for c in mock_create_script_block.call_args_list if c.kwargs.get("block_label") == "inner_loop"
            )
            assert inner_loop_call.kwargs["block_code"] == cached_inner_loop.code
            assert inner_loop_call.kwargs["workflow_run_id"] == "wr_cached"

    @pytest.mark.asyncio
    async def test_nested_forloop_extraction_url_uses_render_template(self) -> None:
        """Regression: nested extraction with a templated URL (e.g. `{{ outer_loop.current_value.url }}`)
        must be emitted as a `skyvern.render_template("...")` call, not a Python literal.

        This is the follow-up to SKY-8757 surfaced by tests/manual/test_nested_forloop_workflow.py's
        second-run cache-hit check. Without this fix, the cached code ran `await skyvern.extract(...)`
        without a `url=` arg and the fallback `ExtractionBlock(url=None)` hit
        `InvalidWorkflowTaskURLState` at agent.py:200 on every cache hit.
        """
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        blocks = [
            {
                "block_type": "for_loop",
                "label": "outer_page_loop",
                "loop_variable_reference": "{{ pages }}",
                "workflow_run_block_id": "wfrb_outer",
                "loop_blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "inner_field_loop",
                        "loop_variable_reference": "{{ current_value.fields }}",
                        "workflow_run_block_id": "wfrb_inner",
                        "loop_blocks": [
                            {
                                "block_type": "extraction",
                                "label": "extract_field_data",
                                "url": "{{ outer_page_loop.current_value.url }}",
                                "data_extraction_goal": "Extract {{ current_value }}.",
                                "data_schema": {"type": "object", "properties": {}},
                                "task_id": "task_extract",
                                "workflow_run_block_id": "wfrb_extract",
                            },
                        ],
                    },
                ],
            },
        ]

        actions_by_task = {
            "task_extract": [
                {
                    "action_type": "extract",
                    "action_id": "action_1",
                    "xpath": "//div",
                    "element_id": "elem_1",
                    "text": None,
                    "data_extraction_goal": "Extract",
                },
            ],
        }

        workflow = {
            "workflow_id": "wf_test",
            "title": "Nested Templated URL Test",
            "workflow_definition": {"parameters": []},
        }

        mock_create_script_block = AsyncMock(return_value=True)

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                mock_create_script_block,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test_templated_url.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="script_1",
                script_revision_id="rev_1",
                organization_id="org_1",
            )

            # Generated code must compile.
            try:
                ast.parse(result.source_code)
            except SyntaxError as e:
                pytest.fail(f"Generated script has SyntaxError: {e}\n\n{result.source_code}")

            # Find the block_code stored for extract_field_data.
            extract_call = next(
                (
                    c
                    for c in mock_create_script_block.call_args_list
                    if c.kwargs.get("block_label") == "extract_field_data"
                ),
                None,
            )
            assert extract_call is not None, (
                "No create_or_update_script_block call for extract_field_data. "
                f"Labels seen: {[c.kwargs.get('block_label') for c in mock_create_script_block.call_args_list]}"
            )
            block_code: str = extract_call.kwargs["block_code"]

            # The URL must be emitted as a skyvern.render_template() call so that
            # {{ outer_page_loop.current_value.url }} resolves at runtime from the
            # workflow_run_context.values populated by skyvern.loop().
            assert "skyvern.render_template" in block_code, (
                f"extract_field_data block_code does not contain skyvern.render_template:\n\n{block_code}"
            )
            assert "{{ outer_page_loop.current_value.url }}" in block_code, (
                f"Template string not present in block_code:\n\n{block_code}"
            )
            # And must NOT appear as a raw Python literal passed to extract(..., url=...).
            assert "url='{{ outer_page_loop.current_value.url }}'" not in block_code, (
                f"URL was emitted as a literal, not a render_template call:\n\n{block_code}"
            )
            assert 'url="{{ outer_page_loop.current_value.url }}"' not in block_code, (
                f"URL was emitted as a literal, not a render_template call:\n\n{block_code}"
            )


# ---------------------------------------------------------------------------
# Part 3: _render_value unit tests
# ---------------------------------------------------------------------------
class TestRenderValue:
    """Unit tests for the _render_value CST helper in generate_script.py."""

    def test_empty_or_none_returns_valid_cst_node(self) -> None:
        """Empty/None prompts return valid CST nodes that libcst can serialize.

        Regression: the original helper returned `cst.SimpleString("")` which
        libcst rejects because it lacks enclosing quotes. The helper now
        delegates to `_value` for the empty/None case — `_value("")` emits a
        `SimpleString("''")` and `_value(None)` emits `Name("None")`.
        """
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _render_value

        module = cst.Module(body=[])

        result_empty = _render_value("")
        assert isinstance(result_empty, cst.SimpleString)
        assert module.code_for_node(result_empty) == "''"

        # None passes through _value → cst.parse_expression("None") which
        # is a Name node, not a SimpleString, but still a valid BaseExpression.
        result_none = _render_value(None)
        assert module.code_for_node(result_none) == "None"

    def test_plain_string_returns_literal_simple_string(self) -> None:
        """A non-template string falls back to _value() and emits a Python literal."""
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _render_value

        result = _render_value("https://example.com")
        # _value wraps strings via repr() → "'https://example.com'"
        assert isinstance(result, cst.SimpleString)
        assert result.value == "'https://example.com'"

    def test_template_string_emits_render_template_call(self) -> None:
        """A string containing {{...}} is emitted as skyvern.render_template(...) call."""
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _render_value

        result = _render_value("{{ outer_page_loop.current_value.url }}")
        assert isinstance(result, cst.Call)
        # The call should be skyvern.render_template("{{ ... }}")
        module = cst.Module(body=[])
        rendered = module.code_for_node(result)
        assert rendered.startswith("skyvern.render_template(")
        assert '"{{ outer_page_loop.current_value.url }}"' in rendered or (
            "'{{ outer_page_loop.current_value.url }}'" in rendered
        )

    def test_template_string_with_data_variable_appends_kwarg(self) -> None:
        """When a data_variable_name is provided, it is passed as a data= kwarg."""
        import libcst as cst

        from skyvern.core.script_generations.generate_script import _render_value

        result = _render_value("{{ current_value }}", data_variable_name="context_params")
        assert isinstance(result, cst.Call)
        module = cst.Module(body=[])
        rendered = module.code_for_node(result)
        # libcst may emit with or without whitespace around =; accept both.
        assert "data=context_params" in rendered or "data = context_params" in rendered
