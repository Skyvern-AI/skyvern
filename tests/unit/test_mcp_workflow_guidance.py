from __future__ import annotations

from pathlib import Path

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


def test_bundled_skyvern_skill_documents_browser_profile_lifecycle() -> None:
    # Anchor to this test file rather than CWD so the test doesn't depend on
    # where pytest is invoked from.
    repo_root = Path(__file__).resolve().parents[2]
    skill_dir = repo_root / "skyvern/cli/skills/skyvern"
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    sessions = (skill_dir / "references" / "sessions.md").read_text(encoding="utf-8")
    cli_parity = (skill_dir / "references" / "cli-parity.md").read_text(encoding="utf-8")
    combined = "\n".join([skill_md, sessions, cli_parity])
    normalized = " ".join(combined.split())

    assert "save" in normalized.lower()
    assert "reuse" in normalized.lower()
    assert "skyvern workflow run --id wpid_123 --browser-profile-id" in normalized
    assert "skyvern browser session create --browser-profile-id" in normalized
    assert "validate logged-in state before re-login" in normalized.lower()
    assert "state_save/state_load" in combined
    assert "not the cloud browser-profile reuse path" in normalized


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
