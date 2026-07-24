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
# Deliberately kept a byte-identical parallel of the copilot-side pre-hook
# (``_normalize_block_json_alias`` in forge/sdk/copilot/tools/mcp_hooks.py) so the
# REMOTE MCP path is covered too — the copilot-only shim missed it (SKY-11133).
# NOTE: keep this tuple and ``_promote_block_json_alias`` in sync with that copy.
# They are not shared because cli/mcp_tools must stay import-light (the copilot
# module is heavy) and importing it here would be circular (copilot imports mcp).
_BLOCK_JSON_ALIASES = ("block", "block_definition", "definition", "block_yaml")

# Params whose contract requires a JSON string but which are sometimes sent as
# the already-decoded object. Keep this registry tool-specific: a dict can be a
# legitimate value for other parameters and must not be serialized globally.
_JSON_OBJECT_STRING_ARGUMENTS = {
    "skyvern_extract": "schema",
    "skyvern_run_task": "data_extraction_schema",
}

# Exact, production-observed aliases with only one plausible canonical target.
# Unknown names are deliberately absent so the argument validator still rejects
# hallucinated parameters and wrong-tool calls.
_KNOWN_ARGUMENT_ALIASES = {
    "skyvern_browser_profile_get": ("profile_id", "browser_profile_id"),
    "skyvern_workflow_status": ("workflow_run_id", "run_id"),
}

# Cap the string length parsed by the coercion helpers. Parameter-key lists are
# small; this bounds work done on unbounded, attacker-controlled remote input
# before validation (a bracket-bomb / multi-MB list string never gets parsed).
_MAX_COERCE_STR_LEN = 50_000


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
    an otherwise-valid call. A non-object (or over-long) ``raw_arguments`` is
    likewise left to error instead of being silently swallowed.
    """
    if list(arguments) != ["raw_arguments"]:
        return
    raw = arguments["raw_arguments"]
    if isinstance(raw, str):
        if len(raw) > _MAX_COERCE_STR_LEN:
            return  # never parse unbounded remote input before validation
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
    if not isinstance(raw, dict):
        return
    arguments.pop("raw_arguments")
    arguments.update(raw)


def _coerce_str_to_str_list(value: Any) -> Any:
    """Coerce a stringified list-of-strings into ``list[str]``.

    Only a JSON/py-literal list whose elements are ALL strings is unambiguous
    (``'["a","b"]'`` or ``"['a','b']"`` -> ``["a","b"]``). Anything else — a
    bare string, tuple repr, list with non-string or nested elements,
    unparseable list-shaped string, over-long string, or non-string value — is
    left untouched so it still errors at validation instead of being silently
    turned into phantom keys.
    """
    if not isinstance(value, str) or len(value) > _MAX_COERCE_STR_LEN:
        return value
    stripped = value.strip()
    if not stripped.startswith("["):
        return value
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(stripped)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
        return value  # parsed, but not a flat list of strings -> leave to error
    return value  # a list-shaped string neither parser could read -> leave to error


def _coerce_json_object_to_str(value: Any) -> Any:
    """Serialize a dict arg to a JSON string for a string-typed param.

    A JSON Schema passed as an object (dict) instead of its serialized string
    form has one unambiguous reading. A list is not a valid schema object and
    scalars are not schemas, so both are left untouched to error rather than
    being pushed downstream as an invalid schema. An over-long serialization is
    also left to error so an unbounded string never crosses the boundary.
    """
    if isinstance(value, dict):
        dumped = json.dumps(value)
        if len(dumped) <= _MAX_COERCE_STR_LEN:
            return dumped
    return value


def _promote_known_alias(arguments: dict[str, Any], alias: str, canonical: str) -> None:
    """Rename one exact alias only when it cannot conflict with its canonical key."""
    if alias in arguments and canonical not in arguments:
        arguments[canonical] = arguments.pop(alias)


def _promote_block_json_alias(arguments: dict[str, Any]) -> None:
    """Promote a misnamed block-definition arg to ``block_json`` in place.

    Only the unambiguous single-payload case is repaired; ambiguity is left to
    error rather than silently resolved:

    - if ``block_json`` is already present, nothing is promoted over it and stray
      aliases are left in place — otherwise a distinct alias would be silently
      discarded, or a malformed non-string ``block_json`` silently overwritten;
    - with no ``block_json``, an alias is promoted only when exactly ONE distinct,
      promotable (str, or dict within the size cap) payload is present.
      Multiple distinct payloads, or any non-promotable / over-long value, are
      left to error.

    Intentionally stricter than the copilot pre-hook copy
    (``_normalize_block_json_alias``), which runs first on the copilot path; this
    is the remote-facing boundary and must not silently discard a conflicting arg.
    """
    if "block_json" in arguments:
        return
    serialized: dict[str, str | None] = {}
    for alias in _BLOCK_JSON_ALIASES:
        if alias not in arguments:
            continue
        value = arguments[alias]
        if isinstance(value, str):
            serialized[alias] = value
        elif isinstance(value, dict):
            dumped = json.dumps(value)
            serialized[alias] = dumped if len(dumped) <= _MAX_COERCE_STR_LEN else None
        else:
            serialized[alias] = None
    if not serialized:
        return
    distinct = {payload for payload in serialized.values() if payload is not None}
    if len(distinct) == 1 and None not in serialized.values():
        promoted = next(iter(distinct))
        for alias in serialized:
            arguments.pop(alias)
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

    json_object_string_argument = _JSON_OBJECT_STRING_ARGUMENTS.get(tool_name)
    if json_object_string_argument is not None and json_object_string_argument in arguments:
        arguments[json_object_string_argument] = _coerce_json_object_to_str(arguments[json_object_string_argument])

    known_alias = _KNOWN_ARGUMENT_ALIASES.get(tool_name)
    if known_alias is not None:
        _promote_known_alias(arguments, *known_alias)

    if tool_name == "skyvern_block_validate":
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
