from __future__ import annotations

from pathlib import Path

import pytest

import skyvern.cli.mcp_tools._common as mcp_common
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.blocks import skyvern_block_schema
from skyvern.cli.mcp_tools.prompts import BUILD_WORKFLOW_CONTENT
from skyvern.forge.sdk.routes import code_samples


def test_workflow_authoring_sources_leave_the_block_engine_unset() -> None:
    # Past TASK_V3_CHOSEN_ENGINE_CUTOFF an omitted engine is not skyvern-1.0 and a written one is a pin.
    prompts_dir = Path(code_samples.__file__).resolve().parents[2] / "prompts" / "skyvern"
    sources = {
        "mcp instructions": mcp.instructions,
        "build_workflow prompt": BUILD_WORKFLOW_CONTENT,
        "knowledge base": (prompts_dir / "workflow_knowledge_base.txt").read_text(),
        "pdf import prompt": (prompts_dir / "build-workflow-from-pdf.j2").read_text(),
        "code samples": Path(code_samples.__file__).read_text(),
        "skyvern skill": (
            Path(mcp_common.__file__).resolve().parents[1] / "skills" / "skyvern" / "SKILL.md"
        ).read_text(),
    }
    for name, text in sources.items():
        for line in text.splitlines():
            if "skyvern-1.0" in line:
                assert "asks for" in line and "default" not in line, (name, line)
        assert "engine 1.0" not in text and "1.0 blocks" not in text and "1.0 (default" not in text, name
    for name in ("mcp instructions", "build_workflow prompt", "skyvern skill"):
        assert "`engine: skyvern-1.0`" in sources[name], name


@pytest.mark.asyncio
async def test_expected_prompts_registered() -> None:
    prompts = await mcp.list_prompts()
    prompt_names = {prompt.name for prompt in prompts}
    # Deliberately additive-only: this guards that core prompts remain
    # registered without breaking when new prompts are introduced later.
    assert {"build_workflow", "debug_automation", "extract_data", "qa_test"} <= prompt_names


@pytest.mark.asyncio
async def test_text_prompt_block_schema_example_omits_raw_llm_key() -> None:
    result = await skyvern_block_schema(block_type="text_prompt")

    assert result["ok"] is True
    assert "llm_key" not in result["data"]["example"]
    assert "model" not in result["data"]["example"]


@pytest.mark.asyncio
async def test_code_block_schema_advertises_goal_prompt() -> None:
    """The MCP contract must steer clients to put each code block's plain-language goal in `prompt`
    (rendered as the block's Goal), so MCP input reaches the Goal field (SKY-12954)."""
    result = await skyvern_block_schema(block_type="code")

    assert result["ok"] is True
    example = result["data"]["example"]
    assert example["block_type"] == "code"
    assert example["prompt"]
    properties = result["data"]["schema"]["properties"]
    assert "goal" in properties["prompt"]["description"].lower()
    assert properties["steps"]["description"]
