from __future__ import annotations

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.tools.event_triggers import (
    create_webhook_trigger_tool,
    list_workflow_triggers_tool,
    test_trigger_mapping_tool,
)
from skyvern.schemas.agent_step_tool_names import AGENT_STEP_TOOL_NAMES
from tests.unit.conftest import invoke_copilot_tool, make_copilot_context


@pytest.mark.asyncio
async def test_trigger_tools_report_triggers_unavailable_without_a_cloud_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    ctx = make_copilot_context()
    calls = [
        (create_webhook_trigger_tool, {"mapping": {"order_id": "/order_id"}}),
        (test_trigger_mapping_tool, {"mapping": {"order_id": "/order_id"}, "sample_payload": {"order_id": "1"}}),
        (list_workflow_triggers_tool, {}),
    ]

    for tool, arguments in calls:
        result = await invoke_copilot_tool(tool, ctx, arguments)
        assert result["ok"] is False
        assert "not available" in result["error"]
        assert tool.name not in AGENT_STEP_TOOL_NAMES
