"""Tests for Copilot's build-time page evidence guidance."""

from __future__ import annotations

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.tools import update_and_run_blocks_tool, update_workflow_tool

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
    assert "fill knowledge gaps while building, editing, or debugging a workflow" in rendered
    assert "not to add defensive verification blocks for every website shape" in rendered
    assert "Do not invent website-specific paths, query parameters, form fields" in rendered
    assert "GOTO_URL STATE SHORTCUT POLICY" not in rendered
    assert "Before extraction on stateful search/result tasks" not in rendered


def test_tool_descriptions_ground_composition_without_prescribing_extra_workflow_blocks() -> None:
    for tool in (update_workflow_tool, update_and_run_blocks_tool):
        desc = tool.description  # type: ignore[attr-defined]
        assert "browser inspection" in desc
        assert "build-time context" in desc
        assert "validation` or `navigation` block" not in desc
        assert "verify every requested constraint" not in desc
