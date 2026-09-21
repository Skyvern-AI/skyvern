"""Unit tests for the Task V3 workflow-position analysis
(skyvern/forge/taskv3/workflow_position.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.models.block import ForLoopBlock
from skyvern.forge.taskv3.workflow_position import is_last_block, select_previous_block
from skyvern.schemas.workflows import BlockStatus, BlockType
from tests.unit._taskv3_block_fakes import make_block as _make_block
from tests.unit._taskv3_block_fakes import make_workflow_run_context as _make_workflow_run_context
from tests.unit._taskv3_block_fakes import output_param as _output_param
from tests.unit._taskv3_block_fakes import run_block as _run_block


def test_is_last_block_true_for_last_label() -> None:
    first, last = _make_block("first"), _make_block("last")
    workflow_run_context = _make_workflow_run_context([first, last])
    assert is_last_block(last, workflow_run_context) is True


def test_is_last_block_false_for_first_label() -> None:
    first, last = _make_block("first"), _make_block("last")
    workflow_run_context = _make_workflow_run_context([first, last])
    assert is_last_block(first, workflow_run_context) is False


def test_is_last_block_none_when_label_not_in_definition() -> None:
    workflow_run_context = _make_workflow_run_context([_make_block("other")])
    assert is_last_block(_make_block("missing"), workflow_run_context) is None


def test_is_last_block_none_when_workflow_run_context_is_none() -> None:
    assert is_last_block(_make_block("solo"), None) is None


def test_is_last_block_none_for_dag_workflow_with_next_block_label() -> None:
    # A DAG workflow executes along next_block_label edges, not definition order, so position in
    # the flattened list is meaningless for any block once any block declares an edge.
    first, last = _make_block("first"), _make_block("last")
    first.next_block_label = "last"
    workflow_run_context = _make_workflow_run_context([first, last])

    assert is_last_block(first, workflow_run_context) is None
    assert is_last_block(last, workflow_run_context) is None


def test_is_last_block_none_when_nested_inside_trailing_loop() -> None:
    inner = _make_block("inner")
    first = _make_block("first")
    loop_block = ForLoopBlock(label="loop", output_parameter=_output_param("loop"), loop_blocks=[inner])
    workflow_run_context = _make_workflow_run_context([first, loop_block])

    assert is_last_block(inner, workflow_run_context) is None
    assert is_last_block(first, workflow_run_context) is False


def test_select_previous_block_returns_none_with_no_candidates() -> None:
    assert select_previous_block([], "wrb_current") is None


def test_select_previous_block_skips_current_running_loop_and_future_rows() -> None:
    now = datetime.now(UTC)
    current = _run_block(
        workflow_run_block_id="wrb_current", task_id="task_current", created_at=now, status=BlockStatus.running
    )
    older_terminal = _run_block(
        workflow_run_block_id="wrb_older",
        created_at=now - timedelta(minutes=10),
        status=BlockStatus.completed,
        label="older",
        finish_reason="older finish",
    )
    newer_terminal = _run_block(
        workflow_run_block_id="wrb_newer",
        created_at=now - timedelta(minutes=5),
        status=BlockStatus.completed,
        label="newer",
        finish_reason="newer finish",
    )
    still_running = _run_block(
        workflow_run_block_id="wrb_running",
        created_at=now - timedelta(minutes=1),
        status=BlockStatus.running,
        label="running",
    )
    loop_container = _run_block(
        workflow_run_block_id="wrb_loop",
        created_at=now - timedelta(minutes=1),
        status=BlockStatus.completed,
        block_type=BlockType.FOR_LOOP,
        label="loop",
    )
    skipped_row = _run_block(
        workflow_run_block_id="wrb_skipped",
        created_at=now - timedelta(seconds=30),
        status=BlockStatus.skipped,
        label="skipped",
    )
    created_after_current = _run_block(
        workflow_run_block_id="wrb_future",
        created_at=now + timedelta(minutes=1),
        status=BlockStatus.completed,
        label="future",
    )
    blocks = [
        current,
        older_terminal,
        newer_terminal,
        still_running,
        loop_container,
        skipped_row,
        created_after_current,
    ]

    result = select_previous_block(blocks, "task_current")

    assert result is not None
    assert result.label == "newer"
    assert result.reason == "newer finish"


def test_select_previous_block_reason_prefers_finish_reason_over_failure_reason() -> None:
    row = _run_block(status=BlockStatus.failed, finish_reason="finish text", failure_reason="failure text")
    result = select_previous_block([row], None)
    assert result is not None
    assert result.reason == "finish text"


def test_select_previous_block_reason_falls_back_to_failure_reason() -> None:
    row = _run_block(status=BlockStatus.failed, finish_reason=None, failure_reason="failure text")
    result = select_previous_block([row], None)
    assert result is not None
    assert result.reason == "failure text"


def test_is_last_block_none_when_finally_block_configured() -> None:
    first, last = _make_block("first"), _make_block("cleanup_finally")
    ctx = _make_workflow_run_context([last, first])
    ctx.workflow.workflow_definition.finally_block_label = "cleanup_finally"
    assert is_last_block(first, ctx) is None
    assert is_last_block(last, ctx) is None


def test_is_last_block_none_when_definition_has_a_conditional_block() -> None:
    first, last = _make_block("first"), _make_block("last")
    conditional = MagicMock()
    conditional.label = "router"
    conditional.block_type = BlockType.CONDITIONAL
    conditional.next_block_label = None
    ctx = _make_workflow_run_context([first, conditional, last])
    assert is_last_block(last, ctx) is None
    assert is_last_block(first, ctx) is None


def test_is_last_block_none_for_partial_run_selection() -> None:
    first, last = _make_block("first"), _make_block("last")
    ctx = _make_workflow_run_context([first, last])
    assert is_last_block(last, ctx, selected_block_labels=["last"]) is None
    # A selection covering the whole definition is a full run; position is still meaningful.
    assert is_last_block(last, ctx, selected_block_labels=["first", "last"]) is True
