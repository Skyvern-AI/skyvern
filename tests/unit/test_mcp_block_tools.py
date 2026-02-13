"""Tests for MCP block tools (skyvern_block_schema, skyvern_block_validate)."""

from __future__ import annotations

import json

import pytest

from skyvern.cli.mcp_tools.blocks import skyvern_block_schema, skyvern_block_validate


@pytest.mark.asyncio
async def test_block_schema_task_redirects_to_navigation() -> None:
    """Requesting schema for 'task' should return navigation info with a deprecation warning."""
    result = await skyvern_block_schema(block_type="task")

    assert result["ok"] is True
    assert result["data"]["block_type"] == "navigation"
    assert "navigation_goal" in result["data"]["schema"].get("properties", {})
    assert len(result["warnings"]) > 0
    assert any("deprecated" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_block_schema_unknown_type_returns_error() -> None:
    """Requesting schema for a nonexistent type should return an error with available types."""
    result = await skyvern_block_schema(block_type="invalid_xyz")

    assert result["ok"] is False
    assert result["error"] is not None
    assert "invalid_xyz" in result["error"]["message"]
    assert "navigation" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_block_validate_task_type_warns_deprecated() -> None:
    """Validating a 'task' block should succeed with a deprecation warning."""
    block = {
        "block_type": "task",
        "label": "test",
        "url": "https://example.com",
        "navigation_goal": "do something",
    }
    result = await skyvern_block_validate(block_json=json.dumps(block))

    assert result["ok"] is True
    assert result["data"]["valid"] is True
    assert len(result["warnings"]) > 0
    assert any("deprecated" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_block_schema_no_type_lists_all() -> None:
    """Calling without a block_type should list all available types."""
    result = await skyvern_block_schema(block_type=None)

    assert result["ok"] is True
    block_types = result["data"]["block_types"]
    assert "navigation" in block_types
    assert "extraction" in block_types
    assert "task" not in block_types
    assert result["data"]["count"] > 0
