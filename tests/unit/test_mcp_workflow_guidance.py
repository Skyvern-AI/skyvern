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
    # text_prompt guidance moved to build_workflow prompt; instructions now focus on task classification.
    # Verify the classification table (the core of the rewritten instructions) is present.
    assert "Task Classification" in mcp.instructions
    assert "If a browser session is already open, keep using it." in mcp.instructions
    assert '"try this once"' in mcp.instructions
    assert "skyvern_run_task" in mcp.instructions
    assert "skyvern_workflow_create" in mcp.instructions
    assert "skyvern_validate" in mcp.instructions
    assert "skyvern_extract" in mcp.instructions
    assert "skyvern_act" in mcp.instructions
    assert "skyvern_observe" in mcp.instructions


@pytest.mark.asyncio
async def test_text_prompt_block_schema_example_omits_raw_llm_key() -> None:
    result = await skyvern_block_schema(block_type="text_prompt")

    assert result["ok"] is True
    assert "llm_key" not in result["data"]["example"]
    assert "model" not in result["data"]["example"]
