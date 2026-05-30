"""Tests for Copilot's build-time page evidence guidance."""

from __future__ import annotations

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.config import DEFAULT_MAX_TURNS
from skyvern.forge.sdk.copilot.tools import (
    _COMPOSITION_INSPECTION_PER_CHAT_BUDGET,
    _COMPOSITION_INSPECTION_PER_TURN_BUDGET,
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


def test_prompt_requires_evidence_grounded_form_and_result_composition() -> None:
    rendered = _render_agent_prompt()

    assert "grounded in observed evidence" in rendered
    assert "not one guessed site model" in rendered
    assert "Do not pre-author row/detail expansion from the user's words alone" in rendered
    assert "the expansion step also needs result-page evidence" in rendered


def test_agent_prompt_frames_inspection_as_build_time_context_not_workflow_shape_policy() -> None:
    rendered = _render_agent_prompt()

    assert "PAGE EVIDENCE POLICY" in rendered
    assert "fill knowledge gaps while building, editing, or debugging a workflow" in rendered
    assert "not to add defensive verification blocks for every website shape" in rendered
    assert "Do not invent website-specific paths, query parameters, form fields" in rendered
    assert (
        "Add `validation` blocks only when the reusable workflow's task actually includes a durable check" in rendered
    )
    assert "before each action" in rendered
    assert "GOTO_URL STATE SHORTCUT POLICY" not in rendered
    assert "Before extraction on stateful search/result tasks" not in rendered


def test_agent_prompt_accounts_for_observed_challenges_without_blind_waits() -> None:
    rendered = _render_agent_prompt()

    assert "anti-bot or challenge controls" in rendered
    assert "Use `get_browser_screenshot` when DOM evidence identifies a challenge" in rendered
    assert "Prefer explicit challenge handling available to Skyvern over blind waits" in rendered
    assert "report the observed blocker instead of retrying the same flow" in rendered
    assert "challenge_state.gates_submit_controls=true" in rendered
    assert "submit/search control disabled" in rendered


def test_agent_prompt_continues_from_observed_state_after_budgeted_runs() -> None:
    rendered = _render_agent_prompt()

    assert "live browser page that advanced beyond the workflow's entrypoint" in rendered
    assert "Do not turn the observed current page into a new workflow entrypoint" in rendered
    assert "Preserve unchanged upstream labels and text" in rendered
    assert "run only the missing downstream labels" in rendered


def test_agent_prompt_requires_result_page_evidence_before_expansion_blocks() -> None:
    rendered = _render_agent_prompt()

    assert "Do not pre-author row/detail expansion from the user's words alone" in rendered
    assert "Do not author or run an expansion block before you have reached or inspected the results page" in rendered
    assert "Do not author downstream row expansion or extraction that depends on a future page state" in rendered
    assert "add and run the expansion block before adding extraction" in rendered
    assert "if the requested details are already visible" in rendered
    assert "omit the expansion block and proceed to extraction from the visible results" in rendered


def test_tool_descriptions_ground_composition_without_prescribing_extra_workflow_blocks() -> None:
    for tool in (update_workflow_tool, run_blocks_tool, update_and_run_blocks_tool):
        desc = tool.description  # type: ignore[attr-defined]
        assert "browser inspection" in desc
        assert "fill knowledge gaps" in desc
        assert "observed" in desc
        assert "evidence" in desc
        assert "validation` or `navigation` block" not in desc
        assert "verify every requested constraint" not in desc


def test_inspection_budget_allows_multi_page_authoring_evidence_but_remains_bounded() -> None:
    assert _COMPOSITION_INSPECTION_PER_TURN_BUDGET >= 3
    assert _COMPOSITION_INSPECTION_PER_CHAT_BUDGET >= _COMPOSITION_INSPECTION_PER_TURN_BUDGET
    assert _COMPOSITION_INSPECTION_PER_CHAT_BUDGET <= 6


def test_default_loop_budget_allows_inspect_build_run_answer_trajectory() -> None:
    assert DEFAULT_MAX_TURNS >= 35
