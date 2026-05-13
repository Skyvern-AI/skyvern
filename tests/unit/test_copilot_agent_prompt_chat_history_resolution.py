from __future__ import annotations

from skyvern.forge.prompts import prompt_engine

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


def test_prompt_resolves_from_context_before_asking_on_build_edit_requests() -> None:
    rendered = _render_agent_prompt()

    assert "Resolve from context before asking on build/edit requests" in rendered
    assert (
        "use it as the default and commit a draft via `update_and_run_blocks` "
        "(or `update_workflow` when testing is skipped)" in rendered
    )
    assert "Reserve `ASK_QUESTION` for inputs missing from BOTH the latest message AND prior context" in rendered


def test_prompt_keeps_explicit_position_literal_and_ordinal_vs_semantic_tiebreaker() -> None:
    rendered = _render_agent_prompt()

    assert (
        'When the user gives an explicit position (e.g., "after `block_placeholder_0`"), use it literally' in rendered
    )
    assert 'do NOT propose a semantically "better-fitting" location' in rendered
    assert (
        'When a reference is ambiguous between a numeric ordinal ("block N") and a description '
        '("the download block"), match the description against existing block labels and `block_type` values first'
        in rendered
    )


def test_classification_skips_ask_question_when_context_resolvable() -> None:
    rendered = _render_agent_prompt()

    assert "not plainly resolvable from chat_history, the current workflow_yaml, or global_llm_context" in rendered
    assert "and not inferable from chat_history or the current workflow_yaml" in rendered


def test_carve_out_section_headers_present_in_prompt() -> None:
    rendered = _render_agent_prompt()

    assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS:" in rendered
    assert "CREDENTIAL HANDLING - CRITICAL:" in rendered
    assert "PARAMETERIZED REQUESTS WITHOUT A SAMPLE VALUE:" in rendered
