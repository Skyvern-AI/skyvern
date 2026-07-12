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

import pytest
from fastmcp import Client

from skyvern.cli.mcp_tools import mcp


async def _call(tool_name: str, arguments: dict) -> object:
    async with Client(mcp) as client:
        return await client.call_tool(tool_name, arguments, raise_on_error=False)


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
async def test_explicit_sibling_arg_wins_over_wrapped_value() -> None:
    # An explicit top-level arg must never be clobbered by the wrapper.
    res = await _call(
        "skyvern_block_schema",
        {"block_type": "navigation", "raw_arguments": {"block_type": "extraction"}},
    )
    assert res.is_error is False
    assert res.structured_content["data"]["block_type"] == "navigation"


@pytest.mark.asyncio
async def test_non_object_raw_arguments_is_not_masked() -> None:
    # A non-object raw_arguments is a genuinely malformed call; it must still
    # error rather than be silently swallowed.
    res = await _call("skyvern_block_schema", {"raw_arguments": "navigation"})
    assert res.is_error is True
