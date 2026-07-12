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
    """Lift args wrapped in a stray ``raw_arguments`` object up to the top level.

    Some callers serialize the whole tool-call payload as
    ``{"raw_arguments": {...actual args...}}`` instead of spreading the args. No
    ``skyvern_`` tool declares a ``raw_arguments`` parameter, so a dict (or a
    JSON-object string) under that key is unambiguously the misplaced payload.
    Existing top-level keys win, so an explicit arg is never clobbered by the
    wrapper. A non-object ``raw_arguments`` is left untouched so the call still
    errors instead of being silently swallowed.
    """
    if "raw_arguments" not in arguments:
        return
    raw = arguments["raw_arguments"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
    if not isinstance(raw, dict):
        return
    arguments.pop("raw_arguments", None)
    for key, value in raw.items():
        arguments.setdefault(key, value)


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
