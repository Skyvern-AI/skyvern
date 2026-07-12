"""Conservative repair of well-known LLM tool-call argument-shape mistakes.

Runs as a FastMCP middleware BEFORE pydantic signature validation, so it covers
every client of the shared ``mcp`` app: the in-memory Workflow Copilot overlay
client and remote/HTTP MCP clients alike. That shared placement is deliberate —
a copilot-only pre-hook shim (SKY-11133) missed the remote path, so the same
validation error reopened. Fixing it once at the shared boundary kills the class.

STRICT RULE: only repair where the intended call is UNAMBIGUOUS. Never rewrite a
payload that should legitimately error (a payload meant for a different tool, or
a missing required arg) — masking real bugs is exactly how a tolerant shim
regresses into hiding tool-selection problems.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

LOG = structlog.get_logger(__name__)


def _unwrap_raw_arguments(arguments: dict[str, Any]) -> None:
    """Lift args wrapped in a stray *sole* ``raw_arguments`` object to the top level.

    Some callers serialize the WHOLE tool-call payload as
    ``{"raw_arguments": {...actual args...}}`` instead of spreading the args. No
    ``skyvern_`` tool declares a ``raw_arguments`` parameter, so when it is the
    SOLE argument and holds a dict (or a JSON-object string) it is unambiguously
    the misplaced payload — lift its keys up.

    Restricted to the sole-key case on purpose: if real sibling args sit next to
    ``raw_arguments`` the call is ambiguous (the blob may be garbage, or duplicate
    a top-level arg), so it is left to error rather than merging stray keys into
    an otherwise-valid call. A non-object ``raw_arguments`` is likewise left to
    error instead of being silently swallowed.
    """
    if list(arguments) != ["raw_arguments"]:
        return
    raw = arguments["raw_arguments"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
    if not isinstance(raw, dict):
        return
    arguments.pop("raw_arguments")
    arguments.update(raw)


def repair_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    """Mutate ``arguments`` in place to fix unambiguous arg-shape mistakes.

    ``tool_name`` is the raw MCP tool name (e.g. ``skyvern_navigate``). The
    middleware wraps this whole pass so any repair bug degrades to passthrough
    rather than a failed tool call.
    """
    _unwrap_raw_arguments(arguments)


class ArgRepairMiddleware(Middleware):
    """Repair unambiguous LLM arg-shape mistakes before FastMCP validation."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = context.message
        arguments = getattr(message, "arguments", None)
        if isinstance(arguments, dict) and arguments:
            repaired = dict(arguments)
            try:
                repair_tool_arguments(getattr(message, "name", "") or "", repaired)
            except Exception:
                # Enrichment only: a repair bug must never break a real tool call.
                LOG.warning("mcp_arg_repair_failed", tool=getattr(message, "name", None), exc_info=True)
            else:
                if repaired != arguments:
                    arguments.clear()
                    arguments.update(repaired)
        return await call_next(context)


__all__ = ["ArgRepairMiddleware", "repair_tool_arguments"]
