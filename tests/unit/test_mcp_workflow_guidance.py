from __future__ import annotations

import pytest

from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.blocks import skyvern_block_schema
from skyvern.cli.mcp_tools.prompts import BUILD_WORKFLOW_CONTENT


def test_build_workflow_prompt_guides_text_prompt_defaults() -> None:
    assert "Default to Skyvern Optimized for text_prompt blocks by omitting both `model` and `llm_key`." in (
        BUILD_WORKFLOW_CONTENT
    )
    assert "Do NOT invent internal `llm_key` strings like `ANTHROPIC_CLAUDE_3_5_SONNET`." in BUILD_WORKFLOW_CONTENT


def test_mcp_instructions_guide_text_prompt_defaults() -> None:
    assert "For **text_prompt** blocks, default to Skyvern Optimized by omitting both `model` and `llm_key`." in (
        mcp.instructions
    )


@pytest.mark.asyncio
async def test_text_prompt_block_schema_example_omits_raw_llm_key() -> None:
    result = await skyvern_block_schema(block_type="text_prompt")

    assert result["ok"] is True
    assert "llm_key" not in result["data"]["example"]
    assert "model" not in result["data"]["example"]
