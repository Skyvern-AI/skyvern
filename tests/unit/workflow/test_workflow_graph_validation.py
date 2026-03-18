"""Tests for WorkflowService.validate_workflow_block_graph.

Validates that the block graph validation is called correctly with support for
orphaned blocks, cycles, dangling references, finally blocks, and ForLoopBlock nesting.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    ForLoopBlock,
    JinjaBranchCriteria,
    NavigationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService


def _output_param(key: str) -> OutputParameter:
    now = datetime.now(tz=timezone.utc)
    return OutputParameter(
        output_parameter_id=f"op_{key}",
        key=key,
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _nav_block(label: str, next_block_label: str | None = None) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_param(f"{label}_output"),
        next_block_label=next_block_label,
    )


def _for_loop_block(label: str, loop_blocks: list, next_block_label: str | None = None) -> ForLoopBlock:
    return ForLoopBlock(
        label=label,
        output_parameter=_output_param(f"{label}_output"),
        loop_blocks=loop_blocks,
        next_block_label=next_block_label,
    )


def _workflow_def(blocks: list, finally_block_label: str | None = None, version: int = 2) -> WorkflowDefinition:
    return WorkflowDefinition(parameters=[], blocks=blocks, finally_block_label=finally_block_label, version=version)


class TestValidateWorkflowBlockGraph:
    """Tests for validate_workflow_block_graph on WorkflowService."""

    def test_valid_linear_chain(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b")]
        svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_single_block(self) -> None:
        svc = WorkflowService()
        svc.validate_workflow_block_graph(_workflow_def([_nav_block("only")]))

    def test_empty_workflow(self) -> None:
        svc = WorkflowService()
        svc.validate_workflow_block_graph(_workflow_def([]))

    def test_orphaned_block_raises(self) -> None:
        """Three blocks where both a and b point to c, making a and b both roots (in-degree 0)."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "c"), _nav_block("b", "c"), _nav_block("c")]
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_circular_reference_two_blocks_raises(self) -> None:
        """Two blocks pointing to each other — both have in-degree 1, so no root exists."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "a")]
        with pytest.raises(InvalidWorkflowDefinition, match="Circular reference detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_circular_reference_with_root_raises(self) -> None:
        """Three blocks: a -> b -> c -> b. Has a valid root (a) but b-c form a cycle."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "c"), _nav_block("c", "b")]
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_dangling_next_block_label_raises(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "nonexistent")]
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_conditional_block(self) -> None:
        svc = WorkflowService()
        blocks = [
            ConditionalBlock(
                label="cond",
                output_parameter=_output_param("cond_output"),
                branch_conditions=[
                    BranchCondition(
                        criteria=JinjaBranchCriteria(expression="{{ true }}"),
                        next_block_label="a",
                        is_default=False,
                    ),
                    BranchCondition(next_block_label="b", is_default=True),
                ],
            ),
            _nav_block("a"),
            _nav_block("b"),
        ]
        svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_conditional_with_dangling_branch_raises(self) -> None:
        svc = WorkflowService()
        blocks = [
            ConditionalBlock(
                label="cond",
                output_parameter=_output_param("cond_output"),
                branch_conditions=[
                    BranchCondition(next_block_label="missing", is_default=True),
                ],
            ),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_with_finally_block(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "fin"), _nav_block("fin")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, finally_block_label="fin"))

    def test_block_pointing_to_finally_block_passes(self) -> None:
        """Edge to finally block is nullified before validation, so it should pass."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "fin"), _nav_block("fin")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, finally_block_label="fin"))

    def test_for_loop_with_valid_nested_blocks(self) -> None:
        svc = WorkflowService()
        loop = _for_loop_block("loop", loop_blocks=[_nav_block("inner_a", "inner_b"), _nav_block("inner_b")])
        svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_cycle_in_nested_blocks_raises(self) -> None:
        """Cycle inside a ForLoopBlock's loop_blocks should be detected."""
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[
                _nav_block("inner_a", "inner_b"),
                _nav_block("inner_b", "inner_c"),
                _nav_block("inner_c", "inner_b"),
            ],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_orphan_in_nested_blocks_raises(self) -> None:
        """Orphaned blocks inside a ForLoopBlock should be detected.

        Both inner_a and inner_b have explicit next_block_label pointing to inner_c,
        making them both roots (in-degree 0) → multiple entry blocks.
        """
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[_nav_block("inner_a", "inner_c"), _nav_block("inner_b", "inner_c"), _nav_block("inner_c")],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_dangling_reference_in_nested_blocks_raises(self) -> None:
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[_nav_block("inner_a", "missing")],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_deeply_nested_for_loop_with_cycle_raises(self) -> None:
        """A ForLoopBlock nested inside another ForLoopBlock — cycle in the inner loop is detected."""
        svc = WorkflowService()
        inner_loop = _for_loop_block(
            "inner_loop",
            loop_blocks=[
                _nav_block("deep_a", "deep_b"),
                _nav_block("deep_b", "deep_c"),
                _nav_block("deep_c", "deep_b"),
            ],
        )
        outer_loop = _for_loop_block("outer_loop", loop_blocks=[inner_loop])
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def([outer_loop]))

    def test_deeply_nested_for_loop_valid(self) -> None:
        """A valid ForLoopBlock nested inside another ForLoopBlock passes validation."""
        svc = WorkflowService()
        inner_loop = _for_loop_block(
            "inner_loop",
            loop_blocks=[_nav_block("deep_a", "deep_b"), _nav_block("deep_b")],
        )
        outer_loop = _for_loop_block("outer_loop", loop_blocks=[inner_loop])
        svc.validate_workflow_block_graph(_workflow_def([outer_loop]))

    def test_duplicate_label_raises(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("a")]
        with pytest.raises(InvalidWorkflowDefinition, match="Duplicate block label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_disconnected_paths_raises(self) -> None:
        """Two disconnected paths should be detected as multiple entry blocks.

        Path 1: a -> b -> c (c has next_block_label=None, end of path)
        Path 2: d -> e (disconnected from path 1)

        Without sequential defaulting, c does NOT auto-connect to d,
        so both a and d are roots → multiple entry blocks.
        """
        svc = WorkflowService()
        blocks = [
            _nav_block("a", "b"),
            _nav_block("b", "c"),
            _nav_block("c"),
            _nav_block("d", "e"),
            _nav_block("e"),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_v1_workflow_skips_validation(self) -> None:
        """v1 workflows (sequential execution) should skip DAG validation entirely."""
        svc = WorkflowService()
        # Two disconnected blocks — would fail v2 validation but should pass v1
        blocks = [_nav_block("a"), _nav_block("b")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, version=1))
