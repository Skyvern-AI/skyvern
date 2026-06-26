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


def test_prompt_consolidates_ask_vs_edit_routing_rule() -> None:
    rendered = _render_agent_prompt()

    assert "ASK-vs-EDIT ROUTING:" in rendered
    assert rendered.count("ASK-vs-EDIT ROUTING:") == 1
    assert "Workflow-improvement questions about a specific present block" not in rendered
    assert "Resolve from context before asking on build/edit requests" not in rendered
    assert "Carry forward edit intent from chat_history" not in rendered
    assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS:" not in rendered
    assert "Explicit edit/debug requests remain edit requests" not in rendered


def test_prompt_routes_resolvable_builds_and_diagnostic_followups_to_edit() -> None:
    rendered = _render_agent_prompt()

    assert "If the latest turn OR prior context establishes a build/edit goal" in rendered
    assert "needed target, condition, value, or action is resolvable" in rendered
    assert "edit the workflow and call `update_and_run_blocks`" in rendered
    assert "This includes diagnostic symptom follow-ups on the same workflow after an explicit edit goal" in rendered
    assert "When the workflow structure itself is enough to make the edit" in rendered
    assert (
        "do not use direct browser tools, `get_run_results`, `run_blocks_and_collect_debug`, `update_workflow`"
        in rendered
    )
    assert (
        "Do not copy explanatory chat-history prose, example Jinja placeholders, or diagnostic transcripts" in rendered
    )
    assert (
        "diagnostic-after-edit - prior turn asked to consolidate login blocks and latest says "
        "`navigate_to_SSO` worked but `block_placeholder` has no active browser session -> connect the existing block chain"
        in rendered
    )


def test_prompt_keeps_literal_positions_and_targets_ambiguous_references() -> None:
    rendered = _render_agent_prompt()

    assert "mismatched/anonymized block references cannot be resolved" in rendered
    assert "required target, condition, value, or action is still missing" in rendered
    assert "Explicit user positions win literally" in rendered
    assert "match existing labels and `block_type` first, then fall back to ordinal" in rendered
    assert "latest says to add a conditional to a URL without saying when" in rendered
    assert (
        "latest names `WF_trigger_SSO_login` and `update_card` when the workflow only has "
        "`navigate_to_SSO` and `block_placeholder` with no unique mapping" in rendered
    )
    assert "ask naming both missing labels and candidate blocks" in rendered


def test_classification_skips_ask_question_when_context_resolvable() -> None:
    rendered = _render_agent_prompt()

    assert "not plainly resolvable from chat_history, the current workflow_yaml, or global_llm_context" in rendered
    assert "and not inferable from chat_history or the current workflow_yaml" in rendered


def test_carve_out_section_headers_present_in_prompt() -> None:
    rendered = _render_agent_prompt()

    assert "ASK-vs-EDIT ROUTING:" in rendered
    assert "CREDENTIAL HANDLING - CRITICAL:" in rendered
    assert "PARAMETERIZED REQUESTS WITHOUT A SAMPLE VALUE:" in rendered


def test_workflow_credential_inputs_unbound_branch_teaches_reply_framing() -> None:
    rendered = _render_agent_prompt()

    assert "REQUEST POLICY: WORKFLOW CREDENTIAL INPUTS UNBOUND." in rendered
    assert "`clarification_reason: workflow_credential_inputs_unbound`" in rendered
    assert "Call `update_workflow` to land the user's edit" in rendered
    assert "I applied your requested change. I couldn't test the modified workflow" in rendered
    assert "add them via the Credentials UI" in rendered
    assert 'data.skip_reason="workflow_credential_inputs_unbound"' in rendered


def test_prompt_treats_conditional_zero_results_as_terminal_evidence() -> None:
    rendered = _render_agent_prompt()

    assert 'The user requested result handling conditionally (for example: "if any"' in rendered
    assert "Treat the no-results path as a valid terminal workflow state" in rendered
    assert "do not ask for a corrected value" in rendered
    assert "Set `goal_reached: true` only if the workflow reached the searched/filtered state" in rendered
