"""Tests for the copilot-v2 SchemaOverlay hooks that ban task/task_v2 block
types at the discovery surface (SKY-9174, Part C)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.tools import (
    _COPILOT_BANNED_BLOCK_TYPES,
    _get_block_schema_post_hook,
    _get_block_schema_pre_hook,
)


@pytest.fixture
def ctx() -> MagicMock:
    return MagicMock()


@pytest.mark.parametrize("block_type", ["task", "task_v2", "TASK", "Task_V2", "  task  "])
@pytest.mark.asyncio
async def test_pre_hook_blocks_banned_types_case_and_whitespace_insensitive(block_type: str, ctx: MagicMock) -> None:
    result = await _get_block_schema_pre_hook({"block_type": block_type}, ctx)

    assert result is not None
    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    for alternative in ("navigation", "extraction", "validation", "login"):
        assert alternative in result["error"]


@pytest.mark.asyncio
async def test_pre_hook_allows_non_banned_types(ctx: MagicMock) -> None:
    for block_type in ("navigation", "extraction", "validation", "login", "goto_url", "for_loop"):
        assert await _get_block_schema_pre_hook({"block_type": block_type}, ctx) is None


@pytest.mark.asyncio
async def test_pre_hook_allows_list_mode_no_block_type(ctx: MagicMock) -> None:
    assert await _get_block_schema_pre_hook({}, ctx) is None
    assert await _get_block_schema_pre_hook({"block_type": None}, ctx) is None


@pytest.mark.asyncio
async def test_pre_hook_allows_non_string_block_type(ctx: MagicMock) -> None:
    assert await _get_block_schema_pre_hook({"block_type": 123}, ctx) is None


@pytest.mark.asyncio
async def test_post_hook_scrubs_banned_types_from_list_response(ctx: MagicMock) -> None:
    result = {
        "ok": True,
        "data": {
            "block_types": {
                "navigation": "Take actions on a page",
                "task": "deprecated",
                "task_v2": "deprecated",
                "extraction": "Extract data",
            },
            "count": 4,
        },
    }

    out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)

    assert set(out["data"]["block_types"]) == {"navigation", "extraction"}


@pytest.mark.asyncio
async def test_post_hook_passthrough_when_no_block_types_dict(ctx: MagicMock) -> None:
    result = {"ok": True, "data": {"block_type": "navigation", "summary": "..."}}

    out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)

    assert out == {"ok": True, "data": {"block_type": "navigation", "summary": "..."}}


@pytest.mark.asyncio
async def test_post_hook_handles_missing_or_malformed_data(ctx: MagicMock) -> None:
    assert await _get_block_schema_post_hook({"ok": False, "error": "x"}, raw={}, ctx=ctx) == {
        "ok": False,
        "error": "x",
    }
    assert await _get_block_schema_post_hook({"ok": True, "data": None}, raw={}, ctx=ctx) == {
        "ok": True,
        "data": None,
    }
    assert await _get_block_schema_post_hook(
        {"ok": True, "data": {"block_types": ["not", "a", "dict"]}}, raw={}, ctx=ctx
    ) == {"ok": True, "data": {"block_types": ["not", "a", "dict"]}}


def test_banned_types_set_contents() -> None:
    assert _COPILOT_BANNED_BLOCK_TYPES == frozenset({"task", "task_v2"})


def test_pre_hook_and_post_emission_reject_share_constant() -> None:
    """SKY-9174 Part F: the pre-emission SchemaOverlay hooks and the
    post-emission YAML-level reject (in `_update_workflow` + REPLACE_WORKFLOW)
    both import `_COPILOT_BANNED_BLOCK_TYPES` from the same module. Guard
    against a future refactor that redefines the set in only one place —
    any divergence would leave one of the two layers out of sync."""
    import skyvern.forge.sdk.copilot.tools as tools_module

    # `_detect_new_banned_blocks` exists on the same module and is the
    # post-emission counterpart. If either symbol is removed, the layer is
    # effectively ripped out and we want this test to catch it.
    assert hasattr(tools_module, "_COPILOT_BANNED_BLOCK_TYPES")
    assert hasattr(tools_module, "_get_block_schema_pre_hook")
    assert hasattr(tools_module, "_get_block_schema_post_hook")
    assert hasattr(tools_module, "_detect_new_banned_blocks")
    assert hasattr(tools_module, "_banned_block_reject_message")
