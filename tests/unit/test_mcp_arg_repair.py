"""Boundary tests for the FastMCP arg-repair middleware.

The middleware runs on the shared ``mcp`` app before pydantic signature
validation, so it covers every client of that app — the in-memory Workflow
Copilot overlay client and remote/HTTP MCP clients alike. These tests drive the
real ``mcp`` app through an in-memory FastMCP ``Client`` so the full
middleware + validation path is exercised, not the tool functions in isolation.

``skyvern_block_schema`` is used as the probe tool: it is pure metadata (no
browser session, no API/network) and ``block_type`` is optional, so a bare call
succeeds and we can assert on the repaired arguments deterministically.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.arg_repair import repair_tool_arguments


async def _call(tool_name: str, arguments: dict) -> object:
    async with Client(mcp) as client:
        return await client.call_tool(tool_name, arguments, raise_on_error=False)


def _assert_invalid_input(result: object, unsupported: list[str]) -> None:
    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict), f"expected structured content dict, got {result!r}"
    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["details"]["unsupported_arguments"] == unsupported


# --- mechanism (a): raw_arguments wrapper (SKY-12124 / SKY-12125 / SKY-12127) ---


@pytest.mark.asyncio
async def test_raw_arguments_dict_wrapper_is_unwrapped() -> None:
    res = await _call("skyvern_block_schema", {"raw_arguments": {"block_type": "navigation"}})
    assert res.is_error is False
    assert res.structured_content["data"]["block_type"] == "navigation"


@pytest.mark.asyncio
async def test_raw_arguments_json_string_wrapper_is_unwrapped() -> None:
    res = await _call("skyvern_block_schema", {"raw_arguments": '{"block_type": "extraction"}'})
    assert res.is_error is False
    assert res.structured_content["data"]["block_type"] == "extraction"


@pytest.mark.asyncio
async def test_raw_arguments_with_sibling_arg_is_not_unwrapped() -> None:
    # Only a SOLE raw_arguments is unwrapped. With a real sibling arg present the
    # call is ambiguous (the blob may be garbage or inject stray keys), so it is
    # left to error rather than merged — no masking.
    res = await _call(
        "skyvern_block_schema",
        {"block_type": "navigation", "raw_arguments": {"block_type": "extraction"}},
    )
    _assert_invalid_input(res, ["raw_arguments"])


@pytest.mark.asyncio
async def test_non_object_raw_arguments_is_not_masked() -> None:
    # A non-object raw_arguments is a genuinely malformed call; it must still
    # error rather than be silently swallowed.
    res = await _call("skyvern_block_schema", {"raw_arguments": "navigation"})
    _assert_invalid_input(res, ["raw_arguments"])


@pytest.mark.asyncio
async def test_json_array_string_raw_arguments_is_not_masked() -> None:
    # A JSON-*array* string is not an object payload; it must still error.
    res = await _call("skyvern_block_schema", {"raw_arguments": '["navigation"]'})
    _assert_invalid_input(res, ["raw_arguments"])


def test_oversized_raw_arguments_string_not_parsed() -> None:
    # An unbounded raw_arguments string is never parsed before validation.
    huge = '{"block_type":"' + "n" * 60000 + '"}'
    args = {"raw_arguments": huge}
    repair_tool_arguments("skyvern_block_schema", args)
    assert args == {"raw_arguments": huge}


# --- mechanism (b): parameter_keys str -> list (SKY-12048 / SKY-12049) ---


def test_parameter_keys_json_list_string_parsed() -> None:
    args = {"code": "value = 1\n", "parameter_keys": '["a", "b"]'}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == ["a", "b"]


def test_parameter_keys_python_repr_list_string_parsed() -> None:
    args = {"parameter_keys": "['a', 'b']"}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == ["a", "b"]


def test_parameter_keys_bare_string_wrapped() -> None:
    args = {"parameter_keys": "only_key"}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == ["only_key"]


def test_parameter_keys_real_list_untouched() -> None:
    args = {"parameter_keys": ["a", "b"]}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == ["a", "b"]


def test_parameter_keys_non_string_scalar_not_masked() -> None:
    # An int is genuinely malformed; leave it so pydantic still rejects it.
    args = {"parameter_keys": 5}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == 5


def test_parameter_keys_nested_list_not_masked() -> None:
    # A list whose elements are not strings must NOT be str()-flattened into
    # phantom keys — leave the raw value so it still errors at validation.
    args = {"parameter_keys": '[["a", "b"]]'}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == '[["a", "b"]]'


def test_parameter_keys_non_string_elements_not_masked() -> None:
    args = {"parameter_keys": "[1, 2]"}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == "[1, 2]"


def test_parameter_keys_oversized_string_not_parsed() -> None:
    # An unbounded attacker-controlled string is left unparsed (DoS guard).
    huge = "[" + ",".join(["1"] * 40000) + "]"
    args = {"parameter_keys": huge}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == huge


def test_parameter_keys_json_object_string_not_masked() -> None:
    # A JSON-object-looking string is not a bare key; it must be left to error,
    # not wrapped into a single phantom key. No real key starts with '{'.
    args = {"parameter_keys": '{"a": 1}'}
    repair_tool_arguments("skyvern_code_block_lint", args)
    assert args["parameter_keys"] == '{"a": 1}'


# --- mechanism (b): extract schema dict -> json string (SKY-12338) ---


def test_extract_schema_object_serialized() -> None:
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    args = {"prompt": "get names", "schema": schema}
    repair_tool_arguments("skyvern_extract", args)
    assert args["schema"] == json.dumps(schema)


def test_extract_schema_string_untouched() -> None:
    args = {"prompt": "x", "schema": '{"type":"object"}'}
    repair_tool_arguments("skyvern_extract", args)
    assert args["schema"] == '{"type":"object"}'


def test_extract_schema_list_not_masked() -> None:
    # A JSON *array* is not a valid schema object; do not serialize it past the
    # boundary — leave it so pydantic still rejects the non-string value.
    schema_list = [{"type": "object"}]
    args = {"prompt": "x", "schema": schema_list}
    repair_tool_arguments("skyvern_extract", args)
    assert args["schema"] == schema_list


def test_extract_schema_oversized_dict_not_serialized() -> None:
    # An unbounded dict is left to error rather than serialized into an
    # unbounded string that crosses the boundary.
    big = {f"k{i}": "v" for i in range(20000)}
    args = {"prompt": "x", "schema": big}
    repair_tool_arguments("skyvern_extract", args)
    assert args["schema"] is big


# --- mechanism (b): block_json alias for validate (SKY-11133) ---


def test_block_validate_block_alias_promoted() -> None:
    args = {"block": '{"block_type":"navigation"}'}
    repair_tool_arguments("skyvern_block_validate", args)
    assert "block" not in args
    assert args["block_json"] == '{"block_type":"navigation"}'


def test_block_validate_dict_alias_serialized() -> None:
    definition = {"block_type": "navigation", "label": "x"}
    args = {"definition": definition}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args["block_json"] == json.dumps(definition)
    assert "definition" not in args


def test_block_validate_identical_aliases_promoted() -> None:
    # One payload under two names is unambiguous — promote it.
    args = {"block": '{"x":1}', "definition": '{"x":1}'}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args == {"block_json": '{"x":1}'}


def test_block_validate_distinct_alias_alongside_block_json_left_to_error() -> None:
    # A distinct alias next to a present block_json is ambiguous; it must NOT be
    # silently dropped (that discards a payload). Leave both to error.
    args = {"block_json": '{"a":1}', "block": '{"b":2}'}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args == {"block_json": '{"a":1}', "block": '{"b":2}'}


def test_block_validate_conflicting_distinct_aliases_left_to_error() -> None:
    # Two distinct payloads under different aliases — do not first-wins-pick one.
    args = {"block": '{"a":1}', "definition": '{"b":2}'}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args == {"block": '{"a":1}', "definition": '{"b":2}'}


def test_block_validate_non_string_canonical_not_overwritten() -> None:
    # A malformed non-string block_json must error, not be silently overwritten
    # by an alias.
    args = {"block_json": 7, "block": '{"x":1}'}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args == {"block_json": 7, "block": '{"x":1}'}


def test_block_validate_non_promotable_alias_left_to_error() -> None:
    # A non-string/non-dict alias value is malformed; leave it to error.
    args = {"block": 7}
    repair_tool_arguments("skyvern_block_validate", args)
    assert args == {"block": 7}


# --- deliberately NOT masked: wrong-tool block_schema (SKY-12140 / SKY-12141) ---


def test_block_schema_definition_arg_is_not_coerced() -> None:
    # A full block definition sent to block_schema is a wrong-tool call (the
    # model meant block_validate). It must be left untouched so it still errors.
    args = {"block_json": '{"block_type":"navigation","label":"x"}'}
    repair_tool_arguments("skyvern_block_schema", args)
    assert args == {"block_json": '{"block_type":"navigation","label":"x"}'}


# --- boundary integration: session-free tools validate after repair ---


@pytest.mark.asyncio
async def test_block_validate_block_alias_validates_at_boundary() -> None:
    block = {
        "block_type": "navigation",
        "label": "test",
        "url": "https://example.com",
        "navigation_goal": "do something",
    }
    res = await _call("skyvern_block_validate", {"block": json.dumps(block)})
    assert res.is_error is False
    assert res.structured_content["data"]["valid"] is True


@pytest.mark.asyncio
async def test_parameter_keys_string_lints_at_boundary() -> None:
    res = await _call("skyvern_code_block_lint", {"code": "value = 1\n", "parameter_keys": '["value"]'})
    assert res.is_error is False


@pytest.mark.asyncio
async def test_block_schema_wrong_tool_payload_still_errors_at_boundary() -> None:
    # SKY-12140 / SKY-12141: a full definition to block_schema must not be masked.
    res = await _call("skyvern_block_schema", {"block_json": '{"block_type":"navigation"}'})
    _assert_invalid_input(res, ["block_json"])
