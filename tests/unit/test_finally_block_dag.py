"""Tests for DAG validation when blocks reference the finally block.

The finally block is excluded from the DAG before validation. Any block whose
next_block_label points to the finally block must have that edge nullified so
_build_workflow_graph does not raise InvalidWorkflowDefinition for a missing label.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    HttpRequestBlock,
    TaskBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import WorkflowService


def _make_output_parameter(key: str) -> OutputParameter:
    now = datetime.now(tz=timezone.utc)
    return OutputParameter(
        key=key,
        parameter_type="output",
        output_parameter_id=f"op_{key}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_block(label: str, *, next_block_label: str | None = None) -> TaskBlock:
    return TaskBlock(
        label=label,
        url="https://example.com",
        output_parameter=_make_output_parameter(label),
        next_block_label=next_block_label,
    )


def _make_http_block(label: str, *, next_block_label: str | None = None) -> HttpRequestBlock:
    return HttpRequestBlock(
        label=label,
        url="https://example.com",
        method="GET",
        output_parameter=_make_output_parameter(label),
        next_block_label=next_block_label,
    )


class TestStripFinallyBlockReferences:
    """Tests for WorkflowService._strip_finally_block_references."""

    def test_removes_finally_block_and_nullifies_edge(self):
        block_1 = _make_task_block("block_1", next_block_label="block_2")
        block_2 = _make_task_block("block_2", next_block_label="finally_block")
        finally_block = _make_http_block("finally_block")

        result = WorkflowService._strip_finally_block_references(
            [block_1, block_2, finally_block],
            "finally_block",
        )

        assert len(result) == 2
        labels = [b.label for b in result]
        assert "finally_block" not in labels
        # block_2 should have its edge to finally_block nullified
        assert result[1].label == "block_2"
        assert result[1].next_block_label is None

    def test_conditional_branch_pointing_to_finally_is_nullified(self):
        block_1 = _make_task_block("block_1")
        cond_block = ConditionalBlock(
            label="cond_block",
            output_parameter=_make_output_parameter("cond_block"),
            branch_conditions=[
                BranchCondition(next_block_label="block_1", is_default=True),
                BranchCondition(
                    next_block_label="finally_block",
                    criteria={"criteria_type": "jinja2_template", "expression": "{{ true }}"},
                ),
            ],
        )
        finally_block = _make_http_block("finally_block")

        result = WorkflowService._strip_finally_block_references(
            [block_1, cond_block, finally_block],
            "finally_block",
        )

        assert len(result) == 2
        cond = next(b for b in result if b.label == "cond_block")
        for branch in cond.branch_conditions:
            assert branch.next_block_label != "finally_block", (
                "Branch pointing to finally_block should have been nullified"
            )

    def test_noop_when_no_finally_block(self):
        block_1 = _make_task_block("block_1", next_block_label="block_2")
        block_2 = _make_task_block("block_2")

        result = WorkflowService._strip_finally_block_references(
            [block_1, block_2],
            "nonexistent_finally",
        )

        assert len(result) == 2
        assert result[0].next_block_label == "block_2"


class TestBuildWorkflowGraphWithFinallyBlock:
    """Tests that _build_workflow_graph succeeds after stripping finally block references."""

    def test_dag_validation_with_block_pointing_to_finally_block(self):
        block_1 = _make_task_block("block_1", next_block_label="block_2")
        block_2 = _make_task_block("block_2", next_block_label="finally_block")
        finally_block = _make_http_block("finally_block")

        dag_blocks = WorkflowService._strip_finally_block_references(
            [block_1, block_2, finally_block],
            "finally_block",
        )

        svc = WorkflowService()
        start_label, label_to_block, default_next_map = svc._build_workflow_graph(dag_blocks)

        assert start_label == "block_1"
        assert set(label_to_block.keys()) == {"block_1", "block_2"}
        assert default_next_map["block_1"] == "block_2"
        assert default_next_map["block_2"] is None

    def test_dag_validation_with_conditional_block_branch_pointing_to_finally(self):
        block_1 = _make_task_block("block_1")
        cond_block = ConditionalBlock(
            label="cond_block",
            output_parameter=_make_output_parameter("cond_block"),
            branch_conditions=[
                BranchCondition(next_block_label="block_1", is_default=True),
                BranchCondition(
                    next_block_label="finally_block",
                    criteria={"criteria_type": "jinja2_template", "expression": "{{ true }}"},
                ),
            ],
        )
        finally_block = _make_http_block("finally_block")

        dag_blocks = WorkflowService._strip_finally_block_references(
            [cond_block, block_1, finally_block],
            "finally_block",
        )

        svc = WorkflowService()
        start_label, label_to_block, default_next_map = svc._build_workflow_graph(dag_blocks)

        assert start_label == "cond_block"
        assert set(label_to_block.keys()) == {"cond_block", "block_1"}

    def test_dag_validation_without_finally_block(self):
        block_1 = _make_task_block("block_1", next_block_label="block_2")
        block_2 = _make_task_block("block_2")

        svc = WorkflowService()
        start_label, label_to_block, default_next_map = svc._build_workflow_graph([block_1, block_2])

        assert start_label == "block_1"
        assert set(label_to_block.keys()) == {"block_1", "block_2"}
        assert default_next_map["block_1"] == "block_2"

    def test_dag_validation_fails_without_stripping_finally_block(self):
        """Without stripping, a block referencing the removed finally block causes an error."""
        block_1 = _make_task_block("block_1", next_block_label="block_2")
        block_2 = _make_task_block("block_2", next_block_label="finally_block")
        # Manually exclude the finally block but do NOT nullify the edge
        dag_blocks = [block_1, block_2]

        svc = WorkflowService()
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc._build_workflow_graph(dag_blocks)
