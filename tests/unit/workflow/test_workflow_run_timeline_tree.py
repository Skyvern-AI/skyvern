"""Tests for WorkflowService.get_workflow_run_timeline tree assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.actions.actions import ExtractAction


def _block(
    block_id: str,
    *,
    parent_id: str | None = None,
    created_at: datetime,
    block_type: BlockType = BlockType.TASK,
    task_id: str | None = None,
) -> WorkflowRunBlock:
    return WorkflowRunBlock(
        workflow_run_block_id=block_id,
        workflow_run_id="wr_test",
        organization_id="o_test",
        parent_workflow_run_block_id=parent_id,
        block_type=block_type,
        task_id=task_id,
        created_at=created_at,
        modified_at=created_at,
    )


@pytest.fixture(autouse=True)
def mock_db(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    blocks_mock = AsyncMock(return_value=[])
    actions_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", blocks_mock)
    monkeypatch.setattr(app.DATABASE.tasks, "get_tasks_actions", actions_mock)
    return blocks_mock


@pytest.mark.asyncio
async def test_timeline_handles_more_than_1000_blocks_under_one_parent(mock_db: AsyncMock) -> None:
    """Conditional with >1000 nested children keeps every child in the tree."""
    base = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    parent = _block("wrb_parent", created_at=base, block_type=BlockType.CONDITIONAL)
    child_count = 1500
    children = [
        _block(f"wrb_child_{i:04d}", parent_id="wrb_parent", created_at=base + timedelta(seconds=i + 1))
        for i in range(child_count)
    ]
    mock_db.return_value = list(reversed(children)) + [parent]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert len(timeline) == 1
    root = timeline[0]
    assert root.block is not None and root.block.workflow_run_block_id == "wrb_parent"
    assert len(root.children) == child_count


@pytest.mark.asyncio
async def test_timeline_orphan_parent_surfaces_as_root(mock_db: AsyncMock) -> None:
    """Block whose parent is absent from the row set still appears as a root."""
    base = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    orphan = _block("wrb_orphan", parent_id="wrb_missing", created_at=base)
    mock_db.return_value = [orphan]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert len(timeline) == 1
    assert timeline[0].block is not None
    assert timeline[0].block.workflow_run_block_id == "wrb_orphan"


@pytest.mark.asyncio
async def test_timeline_preserves_deep_nesting(mock_db: AsyncMock) -> None:
    """Loop -> conditional -> tasks round-trips with full structure."""
    base = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    loop = _block("wrb_loop", created_at=base, block_type=BlockType.FOR_LOOP)
    cond = _block(
        "wrb_cond",
        parent_id="wrb_loop",
        created_at=base + timedelta(seconds=1),
        block_type=BlockType.CONDITIONAL,
    )
    leaf_a = _block("wrb_a", parent_id="wrb_cond", created_at=base + timedelta(seconds=2))
    leaf_b = _block("wrb_b", parent_id="wrb_cond", created_at=base + timedelta(seconds=3))
    mock_db.return_value = [leaf_b, leaf_a, cond, loop]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert len(timeline) == 1
    assert timeline[0].block is not None and timeline[0].block.workflow_run_block_id == "wrb_loop"
    assert len(timeline[0].children) == 1
    cond_node = timeline[0].children[0]
    assert cond_node.block is not None and cond_node.block.workflow_run_block_id == "wrb_cond"
    child_ids = {child.block.workflow_run_block_id for child in cond_node.children if child.block is not None}
    assert child_ids == {"wrb_a", "wrb_b"}


@pytest.mark.asyncio
async def test_timeline_attaches_actions_to_conditional_blocks(mock_db: AsyncMock) -> None:
    """Conditional prompt evaluation actions should appear on the conditional row."""
    base = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    conditional = _block(
        "wrb_cond",
        created_at=base,
        block_type=BlockType.CONDITIONAL,
        task_id="tsk_cond_eval",
    )
    action = ExtractAction(action_id="act_cond_extract", task_id="tsk_cond_eval")
    mock_db.return_value = [conditional]
    app.DATABASE.tasks.get_tasks_actions.return_value = [action]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert len(timeline) == 1
    assert timeline[0].block is not None
    assert timeline[0].block.actions == [action]


@pytest.mark.asyncio
async def test_timeline_empty_run_returns_empty_list(mock_db: AsyncMock) -> None:
    """No blocks → empty timeline."""
    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert timeline == []


@pytest.mark.asyncio
async def test_timeline_duplicate_block_ids_keep_last(mock_db: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    """Duplicate workflow_run_block_id collapses to one entry and logs a warning."""
    base = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    first = _block("wrb_dup", created_at=base)
    second = _block("wrb_dup", created_at=base + timedelta(seconds=1))
    mock_db.return_value = [first, second]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert len(timeline) == 1
    assert any("Duplicate workflow_run_block_id" in record.message for record in caplog.records)
