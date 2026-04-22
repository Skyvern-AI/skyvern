from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from skyvern.forge.sdk.routes import agent_protocol


@pytest.mark.asyncio
async def test_get_runs_v2_serializes_mapping_rows_from_database(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_workflow_runs = SimpleNamespace(
        get_all_runs_v2=AsyncMock(
            return_value=[
                {
                    "task_run_id": "tr_123",
                    "run_id": "wr_123",
                    "task_run_type": "workflow_run",
                    "status": "completed",
                    "title": "Workflow run",
                    "started_at": None,
                    "finished_at": None,
                    "created_at": "2026-04-01T00:00:00Z",
                    "workflow_permanent_id": "wpid_123",
                    "script_run": False,
                    "searchable_text": "Workflow run",
                }
            ]
        )
    )
    mock_database = SimpleNamespace(workflow_runs=mock_workflow_runs)
    monkeypatch.setattr(agent_protocol.app, "DATABASE", mock_database)

    response = await agent_protocol.get_runs_v2(
        current_org=SimpleNamespace(organization_id="org_123"),
        page=2,
        page_size=5,
        search_key="abc",
    )

    mock_workflow_runs.get_all_runs_v2.assert_awaited_once_with(
        "org_123",
        page=2,
        page_size=5,
        status=None,
        search_key="abc",
    )
    assert orjson.loads(response.body) == [
        {
            "task_run_id": "tr_123",
            "run_id": "wr_123",
            "task_run_type": "workflow_run",
            "status": "completed",
            "title": "Workflow run",
            "started_at": None,
            "finished_at": None,
            "created_at": "2026-04-01T00:00:00Z",
            "workflow_permanent_id": "wpid_123",
            "script_run": False,
        }
    ]
