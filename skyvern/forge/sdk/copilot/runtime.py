"""Shared copilot runtime types and helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.core import skyvern_context

if TYPE_CHECKING:
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

LOG = structlog.get_logger()


@dataclass
class AgentContext:
    organization_id: str
    workflow_id: str
    workflow_permanent_id: str
    workflow_yaml: str
    browser_session_id: str | None
    stream: EventSourceStream
    api_key: str | None = None
    supports_vision: bool = True
    pending_screenshots: list[ScreenshotEntry] = field(default_factory=list)


def mcp_to_copilot(mcp_result: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP result dict to the copilot {ok, data, error} format."""
    result: dict[str, Any] = {"ok": mcp_result.get("ok", True)}

    data = mcp_result.get("data")
    if data is not None:
        result["data"] = data

    error = mcp_result.get("error")
    if error is not None:
        if isinstance(error, dict):
            # MCP error: {code, message, hint, details}
            msg = error.get("message", "Unknown error")
            hint = error.get("hint", "")
            result["error"] = f"{msg}. {hint}".strip() if hint else msg
        else:
            result["error"] = str(error)

    warnings = mcp_result.get("warnings")
    if warnings:
        result["warnings"] = warnings

    return result


@asynccontextmanager
async def mcp_browser_context(ctx: AgentContext) -> AsyncIterator[None]:
    """Push copilot browser state into the MCP session ContextVar for tool calls."""
    from skyvern.cli.core.client import get_active_api_key, get_skyvern
    from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
    from skyvern.cli.core.session_manager import (
        SessionState,
        _api_key_hash,
        register_copilot_session,
        scoped_session,
        unregister_copilot_session,
    )
    from skyvern.library.skyvern_browser import SkyvernBrowser

    if not ctx.browser_session_id:
        raise RuntimeError("No browser_session_id set on agent context")
    browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
        session_id=ctx.browser_session_id,
        organization_id=ctx.organization_id,
    )
    if not browser_state or not browser_state.browser_context:
        raise RuntimeError(f"No browser context for session {ctx.browser_session_id}")

    skyvern_client = get_skyvern()
    skyvern_browser = SkyvernBrowser(
        skyvern_client,
        browser_state.browser_context,
        browser_session_id=ctx.browser_session_id,
    )
    mcp_ctx = MCPBrowserContext(mode="cloud_session", session_id=ctx.browser_session_id)
    state = SessionState(
        browser=skyvern_browser,
        context=mcp_ctx,
        api_key_hash=_api_key_hash(get_active_api_key()),
    )
    register_copilot_session(ctx.browser_session_id, state)
    try:
        async with scoped_session(state):
            yield
    finally:
        unregister_copilot_session(ctx.browser_session_id)


async def ensure_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    """Create a browser session if needed. Returns None on success, error dict on failure."""
    if ctx.browser_session_id:
        return None

    try:
        with copilot_span("browser_session_create", data={"organization_id": ctx.organization_id}):
            session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=ctx.organization_id,
                timeout_minutes=30,
            )
        ctx.browser_session_id = session.persistent_browser_session_id

        sc = skyvern_context.current()
        if sc:
            sc.run_id = ctx.browser_session_id

        LOG.info(
            "Auto-created browser session for copilot",
            session_id=ctx.browser_session_id,
        )
        return None
    except Exception as e:
        LOG.warning("Failed to auto-create browser session", error=str(e), exc_info=True)
        failed_session_id = ctx.browser_session_id
        ctx.browser_session_id = None
        if failed_session_id:
            try:
                await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                    organization_id=ctx.organization_id,
                    browser_session_id=failed_session_id,
                )
            except Exception:
                LOG.debug(
                    "Failed to clean up partial browser session",
                    session_id=failed_session_id,
                    exc_info=True,
                )
        return {"ok": False, "error": f"Failed to create browser session: {e}"}
