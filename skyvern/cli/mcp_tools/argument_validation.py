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
(the same ``make_result``/``make_error`` envelope every tool uses). A call
with no ``arguments`` at all also falls through: pydantic handles it,
rejecting only when the tool has required parameters.

Type-level mismatches the repair step doesn't cover (an out-of-range number with
no unit to infer, a still-malformed list) still reach FastMCP's own pydantic
validation inside ``call_next``. Those are deliberately not silently coerced — an ambiguous
shape should fail rather than be guessed at — but the resulting
``fastmcp.exceptions.ValidationError`` is caught here too and wrapped in the
same structured envelope, so the model always gets an actionable message
instead of the raw pydantic text.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastmcp.exceptions import ValidationError as FastMCPValidationError
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
_MILLISECOND_FLOOR = 1000
# Same policy for production-observed fields documented as a JSON string that callers send as
# the object that string is supposed to encode.
_JSON_STRING_OBJECT_ARGUMENTS = frozenset({("skyvern_workflow_run", "parameters")})


def _schema_accepts_type(schema: Any, json_type: str) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == json_type or (isinstance(schema_type, list) and json_type in schema_type):
        return True
    return any(
        _schema_accepts_type(option, json_type)
        for alternatives in (schema.get("anyOf"), schema.get("oneOf"))
        if isinstance(alternatives, list)
        for option in alternatives
    )


def _split_comma_separated_list(value: Any) -> Any:
    if not isinstance(value, str) or len(value) > _MAX_REPAIR_STRING_LENGTH:
        return value
    stripped = value.strip()
    if not stripped or stripped[0] in "[{(":
        return value
    items = [item.strip() for item in stripped.split(",", _MAX_REPAIR_LIST_ITEMS)]
    if len(items) > _MAX_REPAIR_LIST_ITEMS or any(not item for item in items):
        return value
    return items


def _millisecond_bounds(schema: Any, *, minimum_floor: float) -> tuple[float, float | None] | None:
    """Return ``(minimum, maximum)`` when ``schema`` describes a millisecond duration.

    The floor is 1000 for ambiguous ``timeout`` names; an explicit ``_ms`` suffix can use
    the schema's lower bound directly. This avoids inferring units from unrelated parameters.
    """
    if not isinstance(schema, dict):
        return None
    for alternatives in (schema.get("anyOf"), schema.get("oneOf")):
        if isinstance(alternatives, list):
            for option in alternatives:
                bounds = _millisecond_bounds(option, minimum_floor=minimum_floor)
                if bounds is not None:
                    return bounds
    schema_type = schema.get("type")
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    if not any(entry in ("integer", "number") for entry in types):
        return None
    minimum = schema.get("minimum")
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or minimum < minimum_floor:
        return None
    maximum = schema.get("maximum")
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool):
        return minimum, None
    return minimum, maximum


def _rescale_seconds_to_milliseconds(name: str, schema: Any, value: Any) -> Any:
    """Scale a sub-floor value on a millisecond parameter, which callers undershoot in seconds.

    The name gate matters as much as the bound: a non-duration parameter could legitimately
    carry a 1000 minimum, and multiplying its value by 1000 would be silent corruption.
    """
    if name == "timeout":
        minimum_floor = _MILLISECOND_FLOOR
    elif name.endswith("_ms"):
        # An explicit unit suffix identifies milliseconds even when the schema floor is < 1s.
        minimum_floor = 0
    else:
        return value
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    bounds = _millisecond_bounds(schema, minimum_floor=minimum_floor)
    if bounds is None:
        return value
    minimum, maximum = bounds
    if not 0 < value < minimum:
        return value
    rescaled = value * 1000
    if rescaled < minimum:
        return value
    # Rescaling past the ceiling would swap one validation failure for a less recognizable one.
    if maximum is not None and rescaled > maximum:
        return value
    return int(rescaled) if isinstance(rescaled, float) and rescaled.is_integer() else rescaled


def _encode_json_object(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return value
    return encoded if len(encoded) <= _MAX_REPAIR_STRING_LENGTH else value


def _repair_argument_types(tool_name: str, tool: Any, arguments: dict[str, Any]) -> None:
    if tool_name == "skyvern_browser_session_create" and arguments.get("generate_browser_profile") is None:
        arguments.pop("generate_browser_profile", None)

    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return

    for name, value in arguments.items():
        schema = properties.get(name)
        if (tool_name, name) in _COMMA_SEPARATED_LIST_ARGUMENTS and _schema_accepts_type(schema, "array"):
            arguments[name] = _split_comma_separated_list(value)
        elif (
            (tool_name, name) in _JSON_STRING_OBJECT_ARGUMENTS
            and _schema_accepts_type(schema, "string")
            and not _schema_accepts_type(schema, "object")
        ):
            arguments[name] = _encode_json_object(value)
        else:
            arguments[name] = _rescale_seconds_to_milliseconds(name, schema, value)


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
        try:
            return await call_next(context)
        except FastMCPValidationError as exc:
            # Argument shapes/ranges that _reject_bad_arguments and _repair_argument_types don't
            # cover (e.g. an out-of-range int, a still-malformed list) reach here from FastMCP's
            # own pydantic validation inside tool dispatch. Without this, the caller gets the raw
            # exception text instead of the same structured envelope every other tool error uses.
            tool_name = getattr(context.message, "name", None)
            return _validation_error_result(tool_name, exc)

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


def _validation_error_result(tool_name: str | None, exc: FastMCPValidationError) -> ToolResult:
    name = tool_name or "unknown_tool"
    payload = make_result(
        name,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            str(exc),
            f"Check the argument types and values against {name}'s documented parameters.",
        ),
    )
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


__all__ = ["MCPArgumentValidationMiddleware"]
