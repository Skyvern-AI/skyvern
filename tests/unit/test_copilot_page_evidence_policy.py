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

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


def test_agent_prompt_frames_inspection_as_build_time_context_not_workflow_shape_policy() -> None:
    rendered = _render_agent_prompt()

    assert "PAGE EVIDENCE POLICY" in rendered
    assert "gather ground-truth evidence in ANY phase of building" in rendered
    assert "exploring, composing, editing, and repairing after a failed block run" in rendered
    assert "not adding defensive verification blocks for every website shape" in rendered
    assert "Do not invent website-specific paths, query parameters, form fields" in rendered
    assert (
        "Add `validation` blocks only when the reusable workflow's task actually includes a durable check" in rendered
    )
    assert "before each action" in rendered
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
    assert DEFAULT_MAX_TURNS >= 35


def test_block_observation_ref_rejects_negative_steps() -> None:
    with pytest.raises(ValidationError):
        BlockObservationRef(label="add_to_cart", observation_step=-1)
