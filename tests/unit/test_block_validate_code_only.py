from __future__ import annotations

import json
from typing import Any

import pytest

from skyvern.cli.mcp_tools._common import CODE_ONLY_POLICY_HINT
from skyvern.cli.mcp_tools.blocks import skyvern_block_validate
from skyvern.forge.sdk.copilot.tools.banned_blocks import collect_code_only_banned_items


def _navigation_block() -> dict[str, str]:
    return {
        "block_type": "navigation",
        "label": "do_search",
        "url": "https://example.com",
        "navigation_goal": "Search and click the first result",
    }


def _assert_code_only_rejection(result: dict[str, Any], *, label: str) -> None:
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert "not allowed in code-only mode" in str(error["message"])
    assert label in str(error["message"])
    assert "use a `code` block" in str(error["hint"])
    # Exact-string on purpose: the guidance text is the behavior surface under test — a
    # keyword check would pass on inverted advice ("pass code_only=false to continue").
    assert CODE_ONLY_POLICY_HINT in str(error["hint"])


@pytest.mark.asyncio
async def test_navigation_block_rejected_in_code_only_mode() -> None:
    result = await skyvern_block_validate(json.dumps(_navigation_block()), code_only=True)

    _assert_code_only_rejection(result, label="do_search")


@pytest.mark.asyncio
async def test_label_less_navigation_block_rejected_in_code_only_mode() -> None:
    block = _navigation_block()
    block.pop("label")

    result = await skyvern_block_validate(json.dumps(block), code_only=True)

    _assert_code_only_rejection(result, label="(unlabeled)")


@pytest.mark.asyncio
async def test_task_block_rejected_in_code_only_mode() -> None:
    block = {
        "block_type": "task",
        "label": "do_search",
        "url": "https://example.com",
        "navigation_goal": "Search and click the first result",
    }
    result = await skyvern_block_validate(json.dumps(block), code_only=True)

    _assert_code_only_rejection(result, label="do_search")


@pytest.mark.asyncio
async def test_code_block_accepted_in_code_only_mode() -> None:
    block = {
        "block_type": "code",
        "label": "do_it_in_code",
        "code": 'await page.goto("https://example.com")\nreturn {"ok": True}',
    }
    result = await skyvern_block_validate(json.dumps(block), code_only=True)

    assert result["ok"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["block_type"] == "code"


@pytest.mark.asyncio
async def test_navigation_block_validates_when_code_only_defaults_off() -> None:
    result = await skyvern_block_validate(json.dumps(_navigation_block()))

    assert result["ok"] is True
    assert result["data"]["block_type"] == "navigation"


def test_collect_code_only_banned_items_includes_label_less_nested_blocks() -> None:
    nested_block: dict[str, Any] = {
        "block_type": "navigation",
        "url": "https://example.com",
        "navigation_goal": "Open the item",
    }
    blocks: list[Any] = [
        {
            "block_type": "for_loop",
            "label": "for_each_item",
            "loop_over_parameter_key": "items",
            "loop_blocks": [nested_block],
        }
    ]

    assert collect_code_only_banned_items(blocks) == [("(unlabeled)", "navigation")]
    assert "label" not in nested_block
