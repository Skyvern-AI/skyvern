from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import BlockRunRequest


@pytest.mark.parametrize(
    ("requested_browser_session_id", "resolved_browser_session_id"),
    [("pbs_supplied", "pbs_supplied"), (None, "pbs_server_resolved")],
)
@pytest.mark.asyncio
async def test_run_block_response_includes_resolved_browser_session(
    monkeypatch: pytest.MonkeyPatch,
    requested_browser_session_id: str | None,
    resolved_browser_session_id: str,
) -> None:
    block_run_request = BlockRunRequest(
        workflow_id="wpid_123",
        block_labels=["block_1"],
        browser_session_id=requested_browser_session_id,
    )
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_123",
        workflow_permanent_id="wpid_123",
        browser_session_id=resolved_browser_session_id,
        status=WorkflowRunStatus.created,
        failure_reason=None,
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
        modified_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    monkeypatch.setattr(agent_protocol.block_service, "validate_block_labels", AsyncMock())
    monkeypatch.setattr(agent_protocol.block_service, "ensure_workflow_run", AsyncMock(return_value=workflow_run))
    execute_blocks = AsyncMock()
    monkeypatch.setattr(agent_protocol.block_service, "execute_blocks", execute_blocks)
    monkeypatch.setattr(
        agent_protocol.app.AGENT_FUNCTION,
        "browser_session_ids_authorized_for_connect",
        AsyncMock(return_value={resolved_browser_session_id}),
    )

    response = await agent_protocol.run_block(
        request=MagicMock(),
        background_tasks=BackgroundTasks(),
        block_run_request=block_run_request,
        organization=SimpleNamespace(organization_id="org_123"),
        user_id="user_123",
    )

    assert response.browser_session_id == resolved_browser_session_id
    assert execute_blocks.await_args.kwargs["browser_session_id"] == resolved_browser_session_id


@pytest.mark.asyncio
async def test_ensure_workflow_run_marks_block_runs_as_block_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare = AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_123"))
    monkeypatch.setattr(agent_protocol.block_service.workflow_service, "prepare_workflow", prepare)
    monkeypatch.setattr(
        agent_protocol.block_service.skyvern_context, "ensure_context", lambda: SimpleNamespace(request_id="req_1")
    )

    await agent_protocol.block_service.ensure_workflow_run(
        organization=SimpleNamespace(organization_id="org_123"),
        template=False,
        workflow_permanent_id="wpid_123",
        block_run_request=BlockRunRequest(workflow_id="wpid_123", block_labels=["block_1"]),
    )

    assert prepare.await_args.kwargs["block_scoped"] is True


@pytest.mark.asyncio
async def test_execute_blocks_with_several_labels_records_one_block_run(monkeypatch: pytest.MonkeyPatch) -> None:
    block_service = agent_protocol.block_service
    rows: dict[str, str] = {}

    async def create_block_run(*, workflow_run_id: str, block_label: str, **_: object) -> None:
        # Mirrors block_runs' primary key on workflow_run_id.
        if workflow_run_id in rows:
            raise ValueError("duplicate key value violates unique constraint block_runs_pkey")
        rows[workflow_run_id] = block_label

    workflow = SimpleNamespace(get_output_parameter=lambda label: SimpleNamespace(output_parameter_id=f"op_{label}"))
    monkeypatch.setattr(
        block_service.app.DATABASE.workflows, "get_workflow_by_permanent_id", AsyncMock(return_value=workflow)
    )
    monkeypatch.setattr(block_service.app.DATABASE.debug, "create_block_run", create_block_run, raising=False)
    monkeypatch.setattr(block_service.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=None))
    executor = SimpleNamespace(execute_workflow=AsyncMock())
    monkeypatch.setattr(block_service.AsyncExecutorFactory, "get_executor", lambda: executor)

    await block_service.execute_blocks(
        request=MagicMock(),
        background_tasks=BackgroundTasks(),
        api_key="",
        block_labels=["sign_in", "open_job"],
        workflow_id="wpid_123",
        workflow_run_id="wr_123",
        workflow_permanent_id="wpid_123",
        organization=SimpleNamespace(organization_id="org_123"),
        user_id="user_123",
    )

    assert rows == {"wr_123": "open_job"}
    assert executor.execute_workflow.await_args.kwargs["block_labels"] == ["sign_in", "open_job"]
