from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.routes.streaming import cdp_input, registries
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


@pytest.mark.asyncio
async def test_public_workflow_stream_rejects_attach_after_closing_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_late_attach"
    websocket = SimpleNamespace(close=AsyncMock(), send_json=AsyncMock())
    workflow_run = SimpleNamespace(
        workflow_run_id=workflow_run_id,
        organization_id="org_stream",
        status=WorkflowRunStatus.running,
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run)))
    )
    monkeypatch.setattr(cdp_input, "app", fake_app)
    monkeypatch.setattr(cdp_input, "auth", AsyncMock(return_value="org_stream"))
    wait_for_browser_state = AsyncMock(side_effect=AssertionError("late attach reached BrowserState acquisition"))
    monkeypatch.setattr(cdp_input, "wait_for_browser_state", wait_for_browser_state)
    registries.mark_stream_closing(workflow_run_id)

    await cdp_input.cdp_input_stream(websocket, workflow_run_id, client_id="client_late")

    websocket.close.assert_awaited_once_with(code=4409, reason="workflow_run_closing")
    websocket.send_json.assert_not_awaited()
    wait_for_browser_state.assert_not_awaited()
