"""Pre-dispatch validation of MCP tool-call arguments.

FastMCP validates tool arguments with pydantic *inside* the tool-dispatch core
(``FunctionTool.run`` -> ``type_adapter.validate_python``). When a caller sends
argument names/shapes that don't match a tool's signature, that layer raises a
raw ``pydantic.ValidationError`` which FastMCP logs via
``logger.exception("Error validating tool ...")`` before returning an opaque
failure the calling model cannot act on. The logged exception surfaces in error
tracking as a recurring signature, and the model keeps re-sending the wrong
shape because the failure text is a wall of pydantic internals.

This middleware checks arguments against the tool's published input schema
*before* dispatch. It repairs narrow, unambiguous type mistakes and
short-circuits with a structured error when argument keys don't match. Because
invalid keys never reach dispatch, no raw validation error is logged, and the
model receives a clear message naming the unsupported and expected arguments
(the same ``make_result``/``make_error`` envelope every tool uses). Other
type-level mismatches still fall through to pydantic. A call with no
``arguments`` at all also falls through: pydantic handles it, rejecting only
when the tool has required parameters.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from skyvern.cli.core.result import ErrorCode, make_error, make_result

LOG = structlog.get_logger(__name__)
_MAX_REPAIR_STRING_LENGTH = 50_000
_MAX_REPAIR_LIST_ITEMS = 100
# Repair only production-observed fields whose comma-delimited meaning is known;
# the schema check below prevents this policy from outliving a signature change.
_COMMA_SEPARATED_LIST_ARGUMENTS = frozenset({("skyvern_workflow_run_list", "status")})


def _schema_accepts_array(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type):
        return True
    return any(
        _schema_accepts_array(option)
        for alternatives in (schema.get("anyOf"), schema.get("oneOf"))
        if isinstance(alternatives, list)
        for option in alternatives
    )


def _split_comma_separated_list(value: Any) -> Any:
    if not isinstance(value, str) or "," not in value or len(value) > _MAX_REPAIR_STRING_LENGTH:
        return value
    stripped = value.strip()
    if not stripped or stripped[0] in "[{(":
        return value
    items = [item.strip() for item in stripped.split(",", _MAX_REPAIR_LIST_ITEMS)]
    if len(items) > _MAX_REPAIR_LIST_ITEMS or any(not item for item in items):
        return value
    return items


def _repair_argument_types(tool_name: str, tool: Any, arguments: dict[str, Any]) -> None:
    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return

    for name, value in arguments.items():
        if (tool_name, name) in _COMMA_SEPARATED_LIST_ARGUMENTS and _schema_accepts_array(properties.get(name)):
            arguments[name] = _split_comma_separated_list(value)

    timeout = arguments.get("timeout")
    if (
        tool_name == "skyvern_navigate"
        and isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and 0 < timeout < 1000
    ):
        timeout_ms = timeout * 1000
        arguments["timeout"] = (
            int(timeout_ms) if isinstance(timeout_ms, float) and timeout_ms.is_integer() else timeout_ms
        )


def _argument_contract(tool: Any) -> tuple[set[str], set[str]] | None:
    """Return ``(allowed, required)`` argument names from a tool's input schema.

    Returns ``None`` when the schema is missing or permits arbitrary properties
    (``additionalProperties``), in which case unsupported-key checks don't apply.
    """
    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict):
        return None
    # Only an explicit `additionalProperties: false` closes the argument set. A
    # permissive schema ({} or true) allows arbitrary keys, so don't flag extras.
    if parameters.get("additionalProperties", False) is not False:
        return None
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return None
    allowed = {str(key) for key in properties}
    required = {str(key) for key in parameters.get("required", []) if isinstance(key, str)}
    return allowed, required


class MCPArgumentValidationMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        rejection = await self._reject_bad_arguments(context)
        if rejection is not None:
            return rejection
        return await call_next(context)

    async def _reject_bad_arguments(self, context: MiddlewareContext[Any]) -> ToolResult | None:
        tool_name = getattr(context.message, "name", None)
        arguments = getattr(context.message, "arguments", None)
        if not tool_name or not isinstance(arguments, dict):
            return None

        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            return None
        try:
            tool = await fastmcp_context.fastmcp.get_tool(tool_name)
        except Exception:
            LOG.debug("mcp_argument_validation_skipped_tool_lookup_failed", tool=tool_name, exc_info=True)
            return None
        if tool is None:
            return None

        repaired = dict(arguments)
        try:
            _repair_argument_types(tool_name, tool, repaired)
        except Exception:
            LOG.warning("mcp_argument_type_repair_failed", tool=tool_name, exc_info=True)
        else:
            if repaired != arguments:
                # FastMCP dispatch reads this same dict reference, so rebinding `arguments` would discard the repair.
                arguments.clear()
                arguments.update(repaired)
        contract = _argument_contract(tool)
        if contract is None:
            return None
        allowed, required = contract

        provided = {str(key) for key in arguments}
        unsupported = sorted(provided - allowed)
        missing = sorted(required - provided)
        if not unsupported and not missing:
            return None
        return _rejection_result(tool_name, sorted(allowed), unsupported, missing)


def _rejection_result(
    tool_name: str,
    expected: list[str],
    unsupported: list[str],
    missing: list[str],
) -> ToolResult:
    problems: list[str] = []
    if unsupported:
        problems.append(f"unsupported argument(s): {', '.join(unsupported)}")
    if missing:
        problems.append(f"missing required argument(s): {', '.join(missing)}")
    message = f"{tool_name} was called with {'; '.join(problems)}."
    expected_str = ", ".join(expected) if expected else "(none)"

    payload = make_result(
        tool_name,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            message,
            f"Call {tool_name} using only its documented parameters: {expected_str}.",
            details={
                "unsupported_arguments": unsupported,
                "missing_required_arguments": missing,
                "expected_arguments": expected,
            },
        ),
    )
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


__all__ = ["MCPArgumentValidationMiddleware"]
