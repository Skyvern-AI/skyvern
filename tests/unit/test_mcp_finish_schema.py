"""Regression tests for the skyvern_finish tool JSON schema (issue #8611)."""

import json

import pytest

fastmcp = pytest.importorskip("fastmcp")

from fastmcp.tools.function_tool import FunctionTool

from skyvern.cli.mcp_tools.output_tools import skyvern_finish


async def _output_property_schema() -> dict:
    tool = FunctionTool.from_function(skyvern_finish, name="skyvern_finish")
    schema = await tool.get_schema() if hasattr(tool, "get_schema") else None
    if schema is None:
        schema = tool.parameters
    return schema["properties"]["output"]


@pytest.mark.asyncio
async def test_finish_output_schema_declares_array_items():
    """Every array schema must carry `items` or Gemini rejects the tool list.

    Google's function-calling API answers the whole request with HTTP 400
    INVALID_ARGUMENT (`properties.output.any_of[1].items: missing field`)
    when a client converts the `type` list into anyOf branches and the array
    branch has no `items`.
    """
    output = await _output_property_schema()

    assert "array" in output["type"]
    assert "items" in output, f"output schema is missing `items`: {json.dumps(output)}"


@pytest.mark.asyncio
async def test_finish_output_schema_keeps_value_types():
    output = await _output_property_schema()

    assert output["type"] == ["object", "array", "string", "number", "boolean", "null"]
