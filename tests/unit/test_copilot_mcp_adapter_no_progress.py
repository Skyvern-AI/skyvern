"""MCP adapter failures and retained safety gates. Fixture references use example.* only."""

from __future__ import annotations

from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy


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
async def test_raised_click_returns_a_structured_error() -> None:
    ctx = _agent_ctx()
    server = _make_server(ctx, "click")

    result = await server.call_tool("click", {"selector": "#submit"})

    assert result.isError is True


@pytest.mark.asyncio
async def test_mcp_browser_tool_ignores_legacy_failed_step_loop_state() -> None:
    ctx = _agent_ctx()
    ctx.failed_tool_step_tracker = {"click:credential_error": 99}  # type: ignore[attr-defined]
    server = _make_server(ctx, "click")

    result = await server.call_tool("click", {"selector": "#submit"})

    assert result.isError is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "Timeout 5000ms exceeded" in text
    assert "LOOP DETECTED" not in text


@pytest.mark.asyncio
async def test_mcp_browser_tool_ignores_current_page_challenge_as_an_admission_gate() -> None:
    ctx = _agent_ctx()
    ctx.composition_page_evidence = {
        "observed_after_workflow_run": True,
        "challenge_state": {
            "detected": True,
            "requires_human_verification": True,
            "gates_submit_controls": True,
        },
    }
    server = _make_server(ctx, "click")

    result = await server.call_tool("click", {"selector": "#submit"})

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "Timeout 5000ms exceeded" in text
    assert "verification challenge" not in text


@pytest.mark.asyncio
async def test_redacted_raw_secret_refuses_browser_mcp_call_at_action_seam() -> None:
    ctx = _agent_ctx()
    ctx.request_policy = RequestPolicy(raw_secret_detected=True, raw_secret_handling="redacted_draft")
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={"click": SchemaOverlay(requires_browser=True)},
        alias_map={},
        allowlist=frozenset(),
        context_provider=lambda: ctx,
    )
    server._client = _RaisingClient()

    result = await server.call_tool("click", {"selector": "#submit"})

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "raw-secret draft cannot use browser tools" in text


class _HangingClient:
    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> NoReturn:
        import asyncio

        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_an_overlay_ceiling_bounds_a_call_that_never_returns() -> None:
    # Live shape (SKY-13226): an evaluate against a stale session handle answered nothing and held
    # the turn for 307s of a 900s budget; the overlay's declared ceiling was consumed by no code.
    ctx = _agent_ctx()
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={"evaluate": SchemaOverlay(timeout=1)},
        alias_map={},
        allowlist=frozenset(),
        context_provider=lambda: ctx,
    )
    server._client = _HangingClient()

    result = await server.call_tool("evaluate", {"expression": "1+1"})

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"ok": false' in text
    assert "1s" in text
    # The call is cancelled mid-flight, and click/type_text carry a ceiling too, so the result says
    # the effect is unknown rather than reporting an action that may have landed as a clean failure.
    assert "unknown" in text


@pytest.mark.asyncio
async def test_no_declared_ceiling_means_no_timeout() -> None:
    import asyncio

    class _SlowClient:
        async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False):
            await asyncio.sleep(0.05)
            raise RuntimeError("made it past any implicit ceiling")

    ctx = _agent_ctx()
    server = _make_server(ctx, "get_block_schema")
    server._client = _SlowClient()

    result = await server.call_tool("get_block_schema", {})

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "made it past any implicit ceiling" in text
