"""Tests for Copilot's build-time page evidence guidance."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.config import DEFAULT_MAX_TURNS
from skyvern.forge.sdk.copilot.tools import (
    BlockObservationRef,
    run_blocks_tool,
    update_and_run_blocks_tool,
    update_workflow_tool,
)

_AGENT_TEMPLATE_DEFAULTS = {
    "workflow_knowledge_base": "test kb",
    "current_datetime": "2026-01-01T00:00:00Z",
    "tool_usage_guide": "",
    "security_rules": "",
}


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


def test_agent_prompt_keeps_page_evidence_in_the_build_and_repair_loop() -> None:
    rendered = _render_agent_prompt()

    assert "Driving a browser — navigating, inspecting, or extracting on request" in rendered
    assert "inspect the resulting page and run evidence before deciding what to change" in rendered
    assert "preserve known-good blocks" in rendered
    assert "retry unchanged only when fresh evidence shows the action did not land" in rendered
    assert "A page challenge (CAPTCHA, anti-bot wall) is an observation, not a verdict" in rendered
    assert "PAGE EVIDENCE POLICY" not in rendered
    assert "GOTO_URL STATE SHORTCUT POLICY" not in rendered
    assert "Before extraction on stateful search/result tasks" not in rendered


def test_tool_descriptions_ground_composition_without_prescribing_extra_workflow_blocks() -> None:
    for tool in (update_workflow_tool, run_blocks_tool, update_and_run_blocks_tool):
        desc = tool.description  # type: ignore[attr-defined]
        assert "browser inspection" in desc
        assert "fill knowledge gaps" in desc
        assert "observed" in desc
        assert "evidence" in desc
        assert "validation` or `navigation` block" not in desc
        assert "verify every requested constraint" not in desc


def test_structured_page_inspection_is_not_rationed() -> None:
    """Capping structured inspection sent the agent to hand-rolled `evaluate` probes once it ran
    out, so understanding a page got harder the more the page needed understanding."""
    from skyvern.forge.sdk.copilot.tools import composition_capture

    source = Path(composition_capture.__file__).read_text()

    assert "_COMPOSITION_INSPECTION_PER_TURN_BUDGET" not in source
    assert "_COMPOSITION_INSPECTION_PER_CHAT_BUDGET" not in source


def test_default_loop_budget_allows_inspect_build_run_answer_trajectory() -> None:
    assert DEFAULT_MAX_TURNS == 200


def test_agent_prompt_directs_past_redundant_output_schema_confirmation() -> None:
    rendered = _render_agent_prompt()

    assert "Everything else you decide: choose the sensible default, act on it" in rendered
    assert "If your question states what you would do, you are not blocked" in rendered


def test_block_observation_ref_rejects_negative_steps() -> None:
    with pytest.raises(ValidationError):
        BlockObservationRef(label="add_to_cart", observation_step=-1)
