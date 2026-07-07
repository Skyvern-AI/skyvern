"""A raised ``client.call_tool`` for the click tool returns before the post-hook, so the shared
no-progress helper must fire from the adapter exception handler exactly once, with no double-count
against the post-hook path. OSS-synced fixture references use example.* only.
"""

from __future__ import annotations

from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode


class _RaisingClient:
    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> NoReturn:
        raise RuntimeError("Timeout 5000ms exceeded")


def _agent_ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id="pbs_1",
        stream=MagicMock(),
        user_message="scout",
        turn_intent=TurnIntent(
            mode=TurnIntentMode.EDIT,
            user_goal="scout",
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        ),
    )


def _make_server(ctx: CopilotContext, tool_name: str) -> SkyvernOverlayMCPServer:
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={tool_name: SchemaOverlay()},
        alias_map={},
        allowlist=frozenset(),
        context_provider=lambda: ctx,
    )
    server._client = _RaisingClient()
    return server


@pytest.mark.asyncio
async def test_raised_click_increments_no_progress_counter_exactly_once() -> None:
    ctx = _agent_ctx()
    server = _make_server(ctx, "click")

    result = await server.call_tool("click", {"selector": "#submit"})

    assert result.isError is True
    assert ctx.consecutive_no_progress_interaction_count == 1


@pytest.mark.asyncio
async def test_raised_non_click_tool_leaves_no_progress_counter_untouched() -> None:
    ctx = _agent_ctx()
    server = _make_server(ctx, "evaluate")

    result = await server.call_tool("evaluate", {"expression": "scan()"})

    assert result.isError is True
    assert ctx.consecutive_no_progress_interaction_count == 0
