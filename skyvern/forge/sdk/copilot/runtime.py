"""Shared copilot runtime types and helpers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog

from skyvern.cli.core.api_key_hash import hash_api_key_for_cache
from skyvern.cli.core.client import (
    get_active_api_key,
    get_skyvern,
    reset_api_key_override,
    set_api_key_override,
)
from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
from skyvern.cli.core.session_manager import (
    SessionState,
    register_copilot_session,
    scoped_session,
    unregister_copilot_session,
)
from skyvern.forge import app
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.core import skyvern_context
from skyvern.library.skyvern_browser import SkyvernBrowser

if TYPE_CHECKING:
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

_SESSION_CLEANUP_TIMEOUT_SECONDS = 5.0
_BROWSER_BOOT_WAIT_SECONDS = 15.0
_BROWSER_BOOT_POLL_INTERVAL_SECONDS = 0.25


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
    tool_activity: list[dict[str, Any]] = field(default_factory=list)

    # Cross-turn agent state accumulated by tools.py as the agent runs.
    # Read back by failure_tracking / loop_detection to detect stuck loops,
    # preserve verified prefixes across partial runs, etc. All optional —
    # downstream accessors use ``getattr(ctx, name, default)`` where
    # tolerant-to-unset is the right default.
    last_requested_block_labels: list[str] = field(default_factory=list)
    last_executed_block_labels: list[str] = field(default_factory=list)
    last_frontier_start_label: str | None = None
    pending_action_sequence_fingerprint: str | None = None
    verified_block_outputs: dict[str, Any] = field(default_factory=dict)
    verified_prefix_labels: list[str] = field(default_factory=list)

    # Enforcement state. Set lazily by streaming_adapter, tools, and
    # failure_tracking; declared here so _check_enforcement can read them on a
    # fresh context without AttributeError.
    navigate_called: bool = False
    observation_after_navigate: bool = False
    navigate_enforcement_done: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    post_update_nudge_count: int = 0
    coverage_nudge_count: int = 0
    format_nudge_count: int = 0
    copilot_total_timeout_exceeded: bool = False
    failed_test_nudge_count: int = 0
    explore_without_workflow_nudge_count: int = 0
    null_data_streak_count: int = 0
    last_test_ok: bool | None = None
    last_test_suspicious_success: bool = False
    last_test_anti_bot: str | None = None
    last_test_failure_reason: str | None = None
    last_failure_category_top: str | None = None
    last_update_block_count: int | None = None
    last_failed_workflow_yaml: str | None = None
    repeated_failure_streak_count: int = 0
    repeated_failure_nudge_emitted_at_streak: int = 0
    last_test_non_retriable_nav_error: str | None = None
    non_retriable_nav_error_last_emitted_signature: str | None = None
    workflow_persisted: bool = False
    last_workflow: Any | None = None
    last_workflow_yaml: str | None = None


def mcp_to_copilot(mcp_result: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP result dict to the copilot {ok, data, error} format."""
    error = mcp_result.get("error")
    # Default ok=False when error is present so an upstream tool that returns
    # an error-shaped response without an explicit `ok` field doesn't produce
    # the contradictory {"ok": True, "error": "..."} envelope.
    result: dict[str, Any] = {"ok": mcp_result.get("ok", error is None)}

    data = mcp_result.get("data")
    if data is not None:
        result["data"] = data

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
    if not ctx.browser_session_id:
        raise RuntimeError("No browser_session_id set on agent context")
    # Validate api_key at the boundary, before touching any backend.
    #
    # The copilot FastAPI route runs outside MCPAPIKeyMiddleware, so the CLI
    # falls back to settings.SKYVERN_API_KEY — the server default, not the
    # authenticated caller's key — unless we install set_api_key_override
    # below. Silently skipping the override when ctx.api_key is missing
    # would re-open the exact coarse-grained-auth hole the override exists
    # to close. Fail loudly instead. The copilot route is always behind
    # auth, so this is an assertion, not a runtime branch.
    if not ctx.api_key:
        LOG.warning(
            "mcp_browser_context invoked without api_key",
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        raise RuntimeError("Copilot agent context missing api_key")

    browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
        session_id=ctx.browser_session_id,
        organization_id=ctx.organization_id,
    )
    if not browser_state or not browser_state.browser_context:
        # Keep the session id out of the raised message -- it can propagate
        # to LLM- or user-visible output -- but log it for operators.
        LOG.warning(
            "No browser context for copilot session",
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        raise RuntimeError("No browser context for copilot session")

    override_token = set_api_key_override(ctx.api_key)
    try:
        skyvern_client = get_skyvern()
        skyvern_browser = SkyvernBrowser(
            skyvern_client,
            browser_state.browser_context,
            browser_session_id=ctx.browser_session_id,
        )
        mcp_ctx = MCPBrowserContext(mode="cloud_session", session_id=ctx.browser_session_id)
        active_key = get_active_api_key()
        state = SessionState(
            browser=skyvern_browser,
            context=mcp_ctx,
            api_key_hash=hash_api_key_for_cache(active_key) if active_key else None,
        )
        register_copilot_session(ctx.browser_session_id, state)
        try:
            async with scoped_session(state):
                yield
        finally:
            unregister_copilot_session(ctx.browser_session_id)
    finally:
        reset_api_key_override(override_token)


async def ensure_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    """Create a browser session if needed. Returns None on success, error dict on failure."""
    if ctx.browser_session_id:
        # Probe attachability — a stale DB row demotes to auto-create here
        # instead of bubbling up as a "No browser context" tool failure.
        try:
            state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            if state and state.browser_context:
                return None
            LOG.warning(
                "Supplied browser_session_id is no longer attachable; auto-creating",
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
        except Exception as exc:
            LOG.warning(
                "Browser state probe raised for supplied session; auto-creating",
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
        ctx.browser_session_id = None

    session = None
    try:
        with copilot_span("browser_session_create", data={"organization_id": ctx.organization_id}):
            session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=ctx.organization_id,
                timeout_minutes=30,
            )
        ctx.browser_session_id = session.persistent_browser_session_id

        # DefaultPersistentSessionsManager schedules chromium in a background
        # task and returns from create_session before browser_context is set,
        # so the next mcp_browser_context lookup raises. Wait for it.
        async with asyncio.timeout(_BROWSER_BOOT_WAIT_SECONDS):
            while True:
                state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    session_id=ctx.browser_session_id,
                    organization_id=ctx.organization_id,
                )
                if state and state.browser_context:
                    break
                await asyncio.sleep(_BROWSER_BOOT_POLL_INTERVAL_SECONDS)

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
        # Cleanup keys off the local `session`, not ctx.browser_session_id --
        # if the failure happened between create_session returning and the
        # attribute assignment, ctx still reads None but the session is live.
        # Wrap in wait_for because create_session likely failed due to a
        # degraded session-manager backend, and close_session hitting the
        # same backend could hang the whole request if left unbounded.
        if session is not None:
            try:
                await asyncio.wait_for(
                    app.PERSISTENT_SESSIONS_MANAGER.close_session(
                        organization_id=ctx.organization_id,
                        browser_session_id=session.persistent_browser_session_id,
                    ),
                    timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception:
                LOG.debug(
                    "Failed to clean up partial browser session",
                    session_id=session.persistent_browser_session_id,
                    exc_info=True,
                )
        ctx.browser_session_id = None
        # Detail stays in the log above (exc_info=True). The returned string
        # flows back through the tool/agent path and could end up in
        # LLM-visible or user-visible output, so strip raw exception text
        # that may carry internal URLs, paths, or backend identifiers.
        return {"ok": False, "error": "Failed to create browser session"}
