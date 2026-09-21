"""Where a Task V3 block sits in the workflow around it: which block ran before it, and whether
it is the workflow's terminal block.

Both answers are derived from workflow structure, not from prompt text. ``None`` from
``is_last_block`` means UNKNOWN -- a DAG, a finally block, or a partial run makes definition order
say nothing about terminality -- and callers must not read it as "not last".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
    from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock


@dataclass(frozen=True)
class PreviousBlockHandoff:
    label: str | None
    status: str | None
    reason: str | None
    final_url: str | None


def is_last_block(
    task_block: BaseTaskBlock,
    workflow_run_context: WorkflowRunContext | None,
    selected_block_labels: list[str] | None = None,
) -> bool | None:
    """Whether ``task_block`` is the last block of the workflow definition. None when unknown — including
    for a block nested in the trailing loop, where further iterations of it may still run."""
    from skyvern.forge.sdk.workflow.models.block import get_all_blocks
    from skyvern.schemas.workflows import BlockType

    workflow = workflow_run_context.workflow if workflow_run_context is not None else None
    if workflow is None or not task_block.label:
        return None
    top_level = workflow.workflow_definition.blocks
    flattened = get_all_blocks(top_level)
    labels = [block.label for block in flattened]
    if not top_level or task_block.label not in labels:
        return None
    if selected_block_labels and not set(labels).issubset(set(selected_block_labels)):
        # A partial run executes only the caller-selected labels, in the caller's order; definition
        # position says nothing about what runs last.
        return None
    if any(getattr(block, "next_block_label", None) for block in flattened) or any(
        block.block_type == BlockType.CONDITIONAL for block in flattened
    ):
        # A DAG workflow executes along next_block_label / conditional-branch edges, not
        # definition order, so list position says nothing about terminality.
        return None
    if workflow.workflow_definition.finally_block_label:
        # The finally block is pulled out of normal traversal and runs last regardless of position,
        # so definition order says nothing about terminality.
        return None
    trailing = top_level[-1]
    if trailing.block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP) and task_block.label in {
        block.label for block in get_all_blocks(trailing.loop_blocks)
    }:
        return None
    return labels[-1] == task_block.label


def select_previous_block(
    blocks: Sequence[WorkflowRunBlock], current_task_id: str | None
) -> PreviousBlockHandoff | None:
    """Pick the predecessor handoff from a run's block rows (any order): the most recently created row
    that actually ran to a terminal state before the current block (the row bound to ``current_task_id``),
    skipping loop containers, whose status is their body's, and skipped branches."""
    from skyvern.schemas.workflows import BlockStatus, BlockType

    current = next((b for b in blocks if current_task_id and b.task_id == current_task_id), None)
    candidates = [
        b
        for b in blocks
        if b is not current
        and b.status not in (None, BlockStatus.running, BlockStatus.skipped)
        and b.block_type not in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP)
        and (current is None or b.created_at <= current.created_at)
    ]
    if not candidates:
        return None
    previous = max(candidates, key=lambda b: (b.created_at, b.workflow_run_block_id))
    return PreviousBlockHandoff(
        label=previous.label,
        status=str(previous.status) if previous.status else None,
        reason=previous.finish_reason or previous.failure_reason,
        final_url=previous.final_url,
    )
