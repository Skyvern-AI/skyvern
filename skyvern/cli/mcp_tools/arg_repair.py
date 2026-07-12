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

import ast
import json
from typing import Any

import structlog
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

LOG = structlog.get_logger(__name__)

# ``skyvern_block_validate`` takes ``block_json`` (a JSON string of one block
# definition); the model sometimes names it under one of these shorter keys.
# Mirror of the copilot-side pre-hook (``_normalize_block_json_alias``) so the
# REMOTE MCP path is covered too — the copilot-only shim missed it (SKY-11133).
_BLOCK_JSON_ALIASES = ("block", "block_definition", "definition", "block_yaml")


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


def _coerce_str_to_str_list(value: Any) -> Any:
    """Coerce a stringified list or a bare string into ``list[str]``.

    LLMs sometimes serialize a list arg to a string — either a JSON/py-literal
    list (``'["a","b"]'`` or ``"['a','b']"``) or a single bare key (``"a"``).
    Both have exactly one unambiguous list reading. Non-string values (an actual
    list, or a scalar like an int) pass through untouched so a genuinely
    malformed value still errors instead of being masked.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] in "[(" and stripped[-1:] in ")]":
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(stripped)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(parsed, (list, tuple)):
                return [str(item) for item in parsed]
    return [value]


def _coerce_json_object_to_str(value: Any) -> Any:
    """Serialize a dict/list arg to a JSON string for a string-typed param.

    A JSON Schema passed as an object instead of its serialized string form has
    one unambiguous reading. Strings and scalars pass through unchanged.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _promote_block_json_alias(arguments: dict[str, Any]) -> None:
    """Promote a misnamed block-definition arg to ``block_json`` in place.

    An explicit non-empty ``block_json`` always wins; only the first usable
    alias is promoted, and every stray alias key is dropped so it cannot trip
    the unexpected-keyword check.
    """
    has_block_json = isinstance(arguments.get("block_json"), str) and bool(arguments["block_json"].strip())
    promoted: str | None = None
    for alias in _BLOCK_JSON_ALIASES:
        if alias not in arguments:
            continue
        value = arguments.pop(alias)
        if has_block_json or promoted is not None:
            continue
        if isinstance(value, str):
            promoted = value
        elif isinstance(value, (dict, list)):
            promoted = json.dumps(value)
    if promoted is not None:
        arguments["block_json"] = promoted


def repair_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    """Mutate ``arguments`` in place to fix unambiguous arg-shape mistakes.

    ``tool_name`` is the raw MCP tool name (e.g. ``skyvern_navigate``). The
    middleware wraps this whole pass so any repair bug degrades to passthrough
    rather than a failed tool call.

    Deliberately NOT handled: a full block definition sent to
    ``skyvern_block_schema`` (which takes a ``block_type`` string, not a
    definition). That is a wrong-tool call — the model meant
    ``skyvern_block_validate`` — so coercing it would mask a tool-selection bug
    (SKY-12140 / SKY-12141). It must keep erroring.
    """
    _unwrap_raw_arguments(arguments)

    if tool_name == "skyvern_code_block_lint" and "parameter_keys" in arguments:
        arguments["parameter_keys"] = _coerce_str_to_str_list(arguments["parameter_keys"])
    elif tool_name == "skyvern_extract" and "schema" in arguments:
        arguments["schema"] = _coerce_json_object_to_str(arguments["schema"])
    elif tool_name == "skyvern_block_validate":
        _promote_block_json_alias(arguments)


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
