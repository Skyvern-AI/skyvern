"""Tests for the MCP code-block synthesis tool.

OSS-synced: only example.* / RFC-2606 placeholder targets.
"""

from __future__ import annotations

import json

import pytest

from skyvern.cli.mcp_tools.code_block import skyvern_code_block_synthesize


def _fixture_trajectory() -> list[dict]:
    return [
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/catalog",
            "typed_value": "widget",
            "role": "textbox",
            "accessible_name": "Search",
        },
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/catalog",
            "role": "button",
            "accessible_name": "Submit",
        },
    ]


@pytest.mark.asyncio
async def test_fixture_trajectory_synthesizes_non_empty_code_block() -> None:
    result = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))

    assert result["ok"] is True
    code = result["data"]["code"]
    assert "page.goto" in code  # nosemgrep: incomplete-url-substring-sanitization
    assert "https://example.com/catalog" in code  # nosemgrep: incomplete-url-substring-sanitization
    assert ".fill(" in code
    assert ".click()" in code
    assert result["data"]["emitted_interaction_count"] == 2


@pytest.mark.asyncio
async def test_same_trajectory_synthesizes_byte_identical_code() -> None:
    r1 = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))
    r2 = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))

    assert r1["data"]["code"] == r2["data"]["code"]


@pytest.mark.asyncio
async def test_empty_trajectory_is_rejected() -> None:
    result = await skyvern_code_block_synthesize(json.dumps([]))

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_non_array_json_is_rejected() -> None:
    result = await skyvern_code_block_synthesize(json.dumps({"not": "a list"}))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("trajectory", ([1], ["click"]))
async def test_non_object_trajectory_items_are_rejected(trajectory: list[object]) -> None:
    result = await skyvern_code_block_synthesize(json.dumps(trajectory))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_bad_json_is_rejected() -> None:
    result = await skyvern_code_block_synthesize("{not valid json")

    assert result["ok"] is False
