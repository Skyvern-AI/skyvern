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

    response = await agent_protocol.run_block(
        request=MagicMock(),
        background_tasks=BackgroundTasks(),
        block_run_request=block_run_request,
        organization=SimpleNamespace(organization_id="org_123"),
        user_id="user_123",
    )

    assert response.browser_session_id == resolved_browser_session_id
    assert execute_blocks.await_args.kwargs["browser_session_id"] == resolved_browser_session_id
