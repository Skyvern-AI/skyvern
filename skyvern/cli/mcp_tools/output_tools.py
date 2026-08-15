"""MCP tools for schema-constrained extraction and terminal output declaration."""

from __future__ import annotations

import json
from typing import Annotated, Any

from jsonschema import validators
from jsonschema.exceptions import SchemaError
from pydantic import Field

from ._common import ErrorCode, make_error, make_result
from .browser import skyvern_extract

EXTRACT_STRUCTURED_DESCRIPTION = (
    "Extract one schema-conformant JSON object from the current page. Uses the same AI extraction path as "
    "skyvern_extract, then strictly validates the returned value against schema, including format assertions the "
    "runtime recognizes. Returns the validated object only after validation succeeds; schema or output violations "
    "return actionable JSON paths. Navigate first. Use session_id or cdp_url to target an existing browser."
)

FINISH_DESCRIPTION = (
    "Declare one authoritative terminal record; does not interact with the browser. status must be exactly: "
    "completed — the stated goal was achieved, including when the goal itself requested a safe stop or termination; "
    "terminated — deliberately stopped short of the goal because safety, permission, or impossibility was discovered "
    "mid-run; failed — attempted but could not achieve the goal. Optionally include output and reason. If schema is "
    "provided, output must validate, including format assertions the runtime recognizes. The response itself is the "
    "terminal record. A later call supersedes an earlier one only in the caller's own transcript."
)

_STATUS_SEMANTICS = {
    "completed": "the stated goal was achieved, including a goal that requested a safe stop or termination",
    "terminated": "deliberately stopped short of the goal because safety, permission, or impossibility was discovered mid-run",
    "failed": "attempted but could not achieve the goal",
}
_MAX_VALIDATION_FAILURES = 20
_MAX_ACTUAL_VALUE_CHARS = 200
# Schemas are wordier than the values that break them.
_MAX_CONSTRAINT_VALUE_CHARS = 400
_MAX_MESSAGE_CHARS = 400
_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")


def _remote_reference(schema: Any) -> str | None:
    """Return the first non-local reference in the schema, if any.

    jsonschema resolves unknown references over the network (`urllib.request.urlopen`), so a
    caller-supplied `$ref` would otherwise make the worker fetch any URL it can reach. Walked
    iteratively because the schema is caller-supplied and can nest arbitrarily deep.
    """
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in _REF_KEYS:
                target = node.get(key)
                if isinstance(target, str) and not target.startswith("#"):
                    return target
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _schema_validator(action: str, schema: str) -> tuple[Any | None, dict[str, Any] | None]:
    try:
        parsed_schema = json.loads(schema)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        return None, make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid JSON Schema: malformed JSON ({exc})",
                "Provide schema as a valid JSON Schema encoded as a JSON string",
            ),
        )

    if not isinstance(parsed_schema, (dict, bool)):
        return None, make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid JSON Schema: expected an object or boolean schema, got {_json_type(parsed_schema)}",
                "Provide a JSON Schema object or boolean encoded as a JSON string",
            ),
        )

    remote_ref = _remote_reference(parsed_schema)
    if remote_ref is not None:
        return None, make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Remote schema reference is not supported: {_truncated(remote_ref, _MAX_ACTUAL_VALUE_CHARS)}",
                "Inline the referenced schema, or point at a local '#/...' pointer",
            ),
        )

    try:
        validator_class = validators.validator_for(parsed_schema)
        validator_class.check_schema(parsed_schema)
    except SchemaError as exc:
        schema_path = _json_path(exc.absolute_schema_path)
        return None, make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid JSON Schema at {schema_path}: {exc.message}",
                "Correct the schema at the named path and retry",
                details={"schema_path": schema_path, "schema_error": exc.message},
            ),
        )
    except RecursionError:
        # A schema can nest deeply enough to survive json.loads and still blow the stack inside
        # check_schema, which raises RecursionError rather than SchemaError.
        return None, make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Invalid JSON Schema: nested too deeply to evaluate",
                "Flatten the schema, or hoist the repeated shape into $defs and reference it",
            ),
        )

    return validator_class(parsed_schema, format_checker=validator_class.FORMAT_CHECKER), None


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            path += f".{part}"
        else:
            path += f"[{json.dumps(part)}]"
    return path


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _truncated(serialized: str, limit: int) -> str:
    if len(serialized) <= limit:
        return serialized
    return serialized[: limit - 3] + "..."


def _serialized_value(value: Any) -> str:
    return _truncated(_serialize(value), _MAX_ACTUAL_VALUE_CHARS)


def _bounded_constraint(value: Any) -> tuple[Any, bool]:
    """Keep small constraints verbatim (native JSON type, comparable to the caller's schema);
    truncate the rest, since combinators echo whole subschemas and enums echo every member.
    Returns the value and whether it was truncated, so a preview is never read as the real thing.
    """
    serialized = _serialize(value)
    if len(serialized) <= _MAX_CONSTRAINT_VALUE_CHARS:
        return value, False
    return _truncated(serialized, _MAX_CONSTRAINT_VALUE_CHARS), True


def _failure_entry(error: Any, *, path: str | None = None, actual_type: str | None = None) -> dict[str, Any]:
    failure_path = path or _json_path(error.absolute_path)
    constraint_value, constraint_truncated = _bounded_constraint(error.validator_value)
    failure = {
        "path": failure_path,
        "constraint": error.validator,
        "constraint_value": constraint_value,
        "actual_value": _serialized_value(error.instance),
        "actual_type": actual_type or _json_type(error.instance),
        # jsonschema builds this from the instance and the constraint, so it is unbounded too.
        "message": _truncated(f"{failure_path}: {error.message}", _MAX_MESSAGE_CHARS),
    }
    if constraint_truncated:
        failure["constraint_value_truncated"] = True
    if error.validator == "type":
        failure["expected_type"] = constraint_value
    return failure


def _validation_failures(errors: list[Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def emit(failure: dict[str, Any]) -> None:
        # jsonschema raises one `required` error per missing property but each
        # error carries the FULL required list; deduplicate so repeated walks
        # of that list cannot exhaust the failure cap with identical entries.
        key = (failure["path"], str(failure["constraint"]), failure["message"])
        if key in seen or len(failures) >= _MAX_VALIDATION_FAILURES:
            return
        seen.add(key)
        failures.append(failure)

    def sorted_errors(errors: Any) -> list[Any]:
        return sorted(errors, key=lambda error: (_json_path(error.absolute_path), error.message))

    def visit(error: Any) -> None:
        if len(failures) >= _MAX_VALIDATION_FAILURES:
            return

        if error.context:
            emit(_failure_entry(error))
            for child in sorted_errors(error.context):
                visit(child)
            return

        if error.validator == "required" and isinstance(error.instance, dict):
            properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
            for key in sorted(error.validator_value):
                if key in error.instance:
                    continue
                property_schema = properties.get(key, {}) if isinstance(properties, dict) else {}
                expected = property_schema.get("type", "present") if isinstance(property_schema, dict) else "present"
                path = _json_path([*error.absolute_path, key])
                failure = _failure_entry(error, path=path, actual_type="missing")
                # `error.validator_value` is the whole required list; this entry is about one key.
                failure["constraint_value"] = key
                failure.pop("constraint_value_truncated", None)
                failure["message"] = f"{path}: required value is missing (expected {expected})"
                emit(failure)
            return

        emit(_failure_entry(error))

    for error in sorted_errors(errors):
        visit(error)
        if len(failures) >= _MAX_VALIDATION_FAILURES:
            break
    return failures


def _collect_errors(validator: Any, output: Any) -> list[Any]:
    """Force the lazy jsonschema pass so nothing it raises escapes into our own walk.

    `iter_errors` resolves `$ref`s as it goes, so it is the one place a caller's schema can raise
    rather than merely fail. Materializing also builds every `context` child, leaving the walk in
    `_validation_failures` pure data access.
    """
    return list(validator.iter_errors(output))


def _validate_output(action: str, validator: Any, output: Any) -> dict[str, Any] | None:
    try:
        errors = _collect_errors(validator, output)
    except Exception as exc:
        # Broad on purpose, and scoped on purpose: an arbitrary caller schema drives third-party
        # evaluation, so anything raised here is theirs. Our own entries are built below, outside
        # the guard, so a bug there raises instead of reading as "your schema is bad".
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"JSON Schema could not be evaluated: {exc or type(exc).__name__}",
                "Remove unresolvable references, unknown types, or unbounded recursion from the schema and retry",
            ),
        )

    failures = _validation_failures(errors)

    if not failures:
        return None
    details: dict[str, Any] = {"validation_errors": failures}
    if len(failures) >= _MAX_VALIDATION_FAILURES:
        # The walk stops at the cap, so the caller must not read this list as exhaustive.
        details["validation_errors_capped"] = _MAX_VALIDATION_FAILURES
    return make_result(
        action,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            "Output does not match JSON Schema: " + "; ".join(failure["message"] for failure in failures),
            "Retry with output matching every reported constraint at the named JSON paths",
            details=details,
        ),
    )


def _rename_action(result: dict[str, Any], action: str) -> dict[str, Any]:
    renamed = dict(result)
    if "action" in renamed:
        renamed["action"] = action
    return renamed


async def skyvern_extract_structured(
    prompt: Annotated[str, "Natural language description of what data to extract from the page"],
    schema: Annotated[str, Field(description="JSON Schema string defining the required output structure")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Extract from the current page and return only schema-valid output."""
    action = "skyvern_extract_structured"
    validator, schema_error = _schema_validator(action, schema)
    if schema_error is not None:
        return schema_error

    extraction = _rename_action(
        await skyvern_extract(prompt=prompt, schema=schema, session_id=session_id, cdp_url=cdp_url),
        action,
    )
    if not extraction.get("ok"):
        return extraction

    extracted = (extraction.get("data") or {}).get("extracted")
    validation_error = _validate_output(action, validator, extracted)
    if validation_error is not None:
        if "browser_context" in extraction:
            validation_error["browser_context"] = extraction["browser_context"]
            validation_error["timing_ms"] = extraction.get("timing_ms", {})
        return validation_error

    # Overlay, not replace: keeps the wrapped tool's other data fields (e.g. sdk_equivalent).
    extraction["data"] = {**(extraction.get("data") or {}), "extracted": extracted, "schema_valid": True}
    return extraction


async def skyvern_finish(
    status: Annotated[str, Field(description="Terminal status: completed, terminated, or failed")],
    output: Annotated[
        Any,
        Field(description="Optional final JSON value: object, array, string, number, boolean, or null"),
    ] = None,
    schema: Annotated[str | None, Field(description="Optional JSON Schema string for output validation")] = None,
    reason: Annotated[str | None, Field(description="Optional concise reason for the declared status")] = None,
) -> dict[str, Any]:
    """Validate and return one authoritative terminal record."""
    action = "skyvern_finish"
    if status not in _STATUS_SEMANTICS:
        allowed = [{"status": name, "semantics": semantics} for name, semantics in _STATUS_SEMANTICS.items()]
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Unknown status {status!r}; allowed values are completed, terminated, failed",
                "Choose the status whose semantics match the actual run outcome",
                details={"allowed_statuses": allowed},
            ),
        )

    if schema is not None:
        validator, schema_error = _schema_validator(action, schema)
        if schema_error is not None:
            return schema_error
        validation_error = _validate_output(action, validator, output)
        if validation_error is not None:
            return validation_error

    record = {"status": status, "output": output, "reason": reason}
    return make_result(action, data={"finish_record": record})


__all__ = [
    "EXTRACT_STRUCTURED_DESCRIPTION",
    "FINISH_DESCRIPTION",
    "skyvern_extract_structured",
    "skyvern_finish",
]
