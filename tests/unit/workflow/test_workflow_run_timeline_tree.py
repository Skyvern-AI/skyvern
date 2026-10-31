"""Tests for WorkflowService.get_workflow_run_timeline tree assembly."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from skyvern.forge import app
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.actions.actions import Action, DownloadFileAction, ExtractAction
from tests.unit.helpers import (
    ONE_TIME_CODE,
    assert_one_time_code_withheld,
    make_one_time_code_action_rows,
    make_session_factory_yielding,
)


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
async def test_timeline_attaches_actions_to_code_blocks(mock_db: AsyncMock) -> None:
    """A code block's task actions render through the standard task_id path, like any task block."""
    base = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    code_block = _block(
        "wrb_code",
        created_at=base,
        block_type=BlockType.CODE,
        task_id="tsk_code",
    )
    action = ExtractAction(action_id="act_code", task_id="tsk_code")
    mock_db.return_value = [code_block]
    app.DATABASE.tasks.get_tasks_actions.return_value = [action]

    service = WorkflowService()
    timeline = await service.get_workflow_run_timeline(workflow_run_id="wr_test", organization_id="o_test")

    assert timeline[0].block is not None
    assert timeline[0].block.actions == [action]


@pytest.mark.asyncio
async def test_native_download_url_survives_registered_timeline_response(
    mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    mock_db.return_value = [_block("wrb_download", created_at=stamp, block_type=BlockType.CODE, task_id="tsk_download")]
    app.DATABASE.tasks.get_tasks_actions.return_value = [
        DownloadFileAction(
            action_id="act_download",
            organization_id="o_test",
            workflow_run_id="wr_test",
            task_id="tsk_download",
            step_id="stp_download",
            status="completed",
            file_name="report.pdf",
            download_url="https://example.com/files/report.pdf?token=synthetic-private-query",
            byte=b"PRIVATE_DOWNLOAD_BYTES",
            created_by="script",
        )
    ]
    monkeypatch.setattr(
        app.DATABASE.tasks, "get_run", AsyncMock(return_value=SimpleNamespace(task_run_type=RunType.workflow_run))
    )
    monkeypatch.setattr(app.DATABASE.observer, "get_task_v2_by_workflow_run_id", AsyncMock(return_value=None))
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *_args, **_kwargs: None)
    api = FastAPI()
    api.include_router(agent_protocol.base_router, prefix="/v1")
    api.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(organization_id="o_test")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/v1/runs/wr_test/timeline")

    assert response.status_code == 200
    action = response.json()[0]["block"]["actions"][0]
    assert action["download_url"] == "https://example.com/files/report.pdf?token=synthetic-private-query"
    assert action["file_name"] == "report.pdf" and action["created_by"] == "script"
    assert "byte" not in action and "PRIVATE_DOWNLOAD_BYTES" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_output_values", [True, False])
async def test_timeline_serves_the_typed_value_and_withholds_the_one_time_code(
    mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch, cap_output_values: bool
) -> None:
    """Driven through the real repository read, because the typed value is resolved in hydration."""
    base = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    mock_db.return_value = [_block("wrb_task", created_at=base, task_id="tsk_test")]
    repo = TasksRepository.__new__(TasksRepository)
    monkeypatch.setattr(
        TasksRepository, "Session", make_session_factory_yielding(make_one_time_code_action_rows()), raising=False
    )
    hydrated: list[Action] = []
    read = repo.get_tasks_actions

    async def read_and_keep(**kwargs: Any) -> list[Action]:
        hydrated.extend(await read(**kwargs))
        return hydrated

    monkeypatch.setattr(repo, "get_tasks_actions", read_and_keep)
    monkeypatch.setattr(app.DATABASE, "tasks", repo)

    timeline = await WorkflowService().get_workflow_run_timeline(
        workflow_run_id="wr_test", organization_id="o_test", cap_output_values=cap_output_values
    )

    body = TypeAdapter(list[WorkflowRunTimeline]).dump_json(timeline).decode()
    assert_one_time_code_withheld(body, json.loads(body)[0]["block"]["actions"], hydrated)
    assert hydrated[0].text == ONE_TIME_CODE


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


@pytest.mark.asyncio
async def test_flatten_task_v2_timeline_inherits_enclosing_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    parent_block = _block("wrb_parent", created_at=base, block_type=BlockType.TaskV2).model_copy(
        update={"block_workflow_run_id": "wr_child"}
    )
    parent = WorkflowRunTimeline(
        type=WorkflowRunTimelineType.block,
        attempt=2,
        block=parent_block,
        created_at=base,
        modified_at=base,
    )
    leaf = WorkflowRunTimeline(
        type=WorkflowRunTimelineType.block,
        attempt=1,
        block=_block("wrb_leaf", created_at=base),
        created_at=base,
        modified_at=base,
    )
    conditional = leaf.model_copy(
        update={"block": _block("wrb_cond", created_at=base, block_type=BlockType.CONDITIONAL), "children": [leaf]}
    )
    loop = leaf.model_copy(
        update={"block": _block("wrb_loop", created_at=base, block_type=BlockType.FOR_LOOP), "children": [conditional]}
    )
    thought = leaf.model_copy(update={"type": WorkflowRunTimelineType.thought, "block": None})
    monkeypatch.setattr(agent_protocol, "_flatten_workflow_run_timeline", AsyncMock(return_value=[loop, thought]))

    result = await agent_protocol._flatten_workflow_run_timeline_recursive(parent, organization_id="o_test")

    assert len(result) == 2
    assert len(result[0].children) == 1
    assert len(result[0].children[0].children) == 1
    flattened_items = [*result, result[0].children[0], result[0].children[0].children[0]]
    assert [item.attempt for item in flattened_items] == [2, 2, 2, 2]
    assert result[0].block == loop.block
    assert result[1].type == WorkflowRunTimelineType.thought
    assert result[0].children[0].children[0].block == leaf.block
    assert [item.attempt for item in [loop, thought, conditional, leaf]] == [1, 1, 1, 1]
    # The first block row that replaced the task_v2 block carries it once; the thought row does not.
    assert [item.task_v2_blocks for item in result] == [[parent_block], None]


@pytest.mark.asyncio
async def test_flatten_task_v2_keeps_wrapper_when_child_has_no_block_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    parent_block = _block("wrb_parent", created_at=base, block_type=BlockType.TaskV2).model_copy(
        update={"block_workflow_run_id": "wr_child"}
    )
    parent = WorkflowRunTimeline(
        type=WorkflowRunTimelineType.block,
        block=parent_block,
        created_at=base,
        modified_at=base,
    )
    thought = WorkflowRunTimeline(
        type=WorkflowRunTimelineType.thought,
        created_at=base,
        modified_at=base,
    )
    monkeypatch.setattr(agent_protocol, "_flatten_workflow_run_timeline", AsyncMock(return_value=[thought]))

    result = await agent_protocol._flatten_workflow_run_timeline_recursive(parent, organization_id="o_test")

    assert [item.block for item in result] == [None, parent_block]


@pytest.mark.asyncio
async def test_flatten_nested_task_v2_keeps_both_replaced_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)

    def task_v2_row(block_id: str, child_run_id: str) -> WorkflowRunTimeline:
        block = _block(block_id, created_at=base, block_type=BlockType.TaskV2).model_copy(
            update={"block_workflow_run_id": child_run_id}
        )
        return WorkflowRunTimeline(type=WorkflowRunTimelineType.block, block=block, created_at=base, modified_at=base)

    outer, inner = task_v2_row("wrb_outer", "wr_outer_child"), task_v2_row("wrb_inner", "wr_inner_child")
    leaf = WorkflowRunTimeline(
        type=WorkflowRunTimelineType.block, block=_block("wrb_leaf", created_at=base), created_at=base, modified_at=base
    )
    runs = {"wr_outer_child": [inner], "wr_inner_child": [leaf]}

    async def flatten_run(organization_id: str, workflow_run_id: str, cap_output_values: bool) -> list:
        return [
            row
            for item in runs[workflow_run_id]
            for row in await agent_protocol._flatten_workflow_run_timeline_recursive(item, organization_id)
        ]

    monkeypatch.setattr(agent_protocol, "_flatten_workflow_run_timeline", flatten_run)

    result = await agent_protocol._flatten_workflow_run_timeline_recursive(outer, organization_id="o_test")

    assert [item.block for item in result] == [leaf.block]
    assert result[0].task_v2_blocks == [inner.block, outer.block]
