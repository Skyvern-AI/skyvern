"""Nested while_loop handling in transform (_process_forloop_children)."""

from typing import Any
from unittest.mock import MagicMock

from skyvern.core.script_generations.transform_workflow_run import (
    _build_children_by_parent,
    _process_forloop_children,
)
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.actions.actions import Action


def test_while_outer_merges_inner_while_recursively() -> None:
    outer = MagicMock()
    outer.workflow_run_block_id = "w_outer"
    outer.label = "outer_while"

    inner_run = MagicMock()
    inner_run.workflow_run_block_id = "w_inner_block"
    inner_run.parent_workflow_run_block_id = "w_outer"
    inner_run.block_type = BlockType.WHILE_LOOP
    inner_run.label = "inner_while"
    inner_run.task_id = None
    inner_run.workflow_run_id = "wr_1"

    grand = MagicMock()
    grand.workflow_run_block_id = "w_gc"
    grand.parent_workflow_run_block_id = "w_inner_block"
    grand.block_type = "extraction"
    grand.label = "deep_ex"
    grand.task_id = "t_deep"
    grand.status = "completed"
    grand.output = {}
    grand.workflow_run_id = "wr_1"

    mock_task = MagicMock()
    mock_task.model_dump.return_value = {"task_id": "t_deep"}

    mock_action = MagicMock(spec=Action)
    mock_action.model_dump.return_value = {"action_type": "extract"}
    mock_action.get_xpath.return_value = "//d"
    mock_action.has_mini_agent = False
    mock_action.action_type = "extract"
    mock_action.task_id = "t_deep"

    loop_blocks_def: list[dict[str, Any]] = [
        {
            "block_type": "while_loop",
            "label": "inner_while",
            "condition": {"criteria_type": "jinja2_template", "expression": "{{ false }}"},
            "loop_blocks": [
                {"block_type": "extraction", "label": "deep_ex", "data_extraction_goal": "d"},
            ],
        },
    ]
    actions_by_task: dict[str, list[dict[str, Any]]] = {}
    all_blocks = [outer, inner_run, grand]

    result = _process_forloop_children(
        forloop_run_block=outer,
        loop_blocks_def=loop_blocks_def,
        children_by_parent=_build_children_by_parent(all_blocks),
        tasks_by_id={"t_deep": mock_task},
        actions_by_task_id={"t_deep": [mock_action]},
        actions_by_task=actions_by_task,
    )
    assert len(result) == 1
    inner = result[0]
    assert inner["block_type"] == "while_loop"
    assert inner["workflow_run_block_id"] == "w_inner_block"
    kids = inner["loop_blocks"]
    assert len(kids) == 1
    assert kids[0]["task_id"] == "t_deep"


def test_for_inside_while_nested_merge() -> None:
    outer_while = MagicMock()
    outer_while.workflow_run_block_id = "w_o"
    outer_while.label = "outer"

    inner_for = MagicMock()
    inner_for.workflow_run_block_id = "f_i"
    inner_for.parent_workflow_run_block_id = "w_o"
    inner_for.block_type = BlockType.FOR_LOOP
    inner_for.label = "inner_for"
    inner_for.task_id = None

    task_child = MagicMock()
    task_child.workflow_run_block_id = "c1"
    task_child.parent_workflow_run_block_id = "f_i"
    task_child.block_type = "task"
    task_child.label = "t_block"
    task_child.task_id = "tid"
    task_child.status = "completed"
    task_child.output = {}

    mock_task = MagicMock()
    mock_task.model_dump.return_value = {"task_id": "tid"}

    mock_action = MagicMock(spec=Action)
    mock_action.model_dump.return_value = {"action_type": "click"}
    mock_action.get_xpath.return_value = "//b"
    mock_action.has_mini_agent = False
    mock_action.action_type = "click"
    mock_action.task_id = "tid"

    loop_def: list[dict[str, Any]] = [
        {
            "block_type": "for_loop",
            "label": "inner_for",
            "loop_variable_reference": "items",
            "loop_blocks": [{"block_type": "task", "label": "t_block", "url": "https://x.test"}],
        },
    ]
    actions_by_task: dict[str, list[dict[str, Any]]] = {}
    blocks = [outer_while, inner_for, task_child]

    out = _process_forloop_children(
        forloop_run_block=outer_while,
        loop_blocks_def=loop_def,
        children_by_parent=_build_children_by_parent(blocks),
        tasks_by_id={"tid": mock_task},
        actions_by_task_id={"tid": [mock_action]},
        actions_by_task=actions_by_task,
    )
    assert out[0]["workflow_run_block_id"] == "f_i"
    assert out[0]["loop_blocks"][0]["task_id"] == "tid"
