from __future__ import annotations

from skyvern.forge.prompts import prompt_engine

_AGENT_TEMPLATE_DEFAULTS = {
    "workflow_knowledge_base": "test kb",
    "current_datetime": "2026-01-01T00:00:00Z",
    "tool_usage_guide": "",
    "security_rules": "",
}


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


def test_prompt_consolidates_ask_vs_edit_routing_without_a_separate_policy_section() -> None:
    rendered = _render_agent_prompt()

    assert "Follow the user's intent for which kind a message is" in rendered
    assert "Ask only when the turn is genuinely blocked on something only the user can supply" in rendered
    assert (
        "Never re-ask what the conversation, the account state, the current workflow, or run evidence already answers"
        in rendered
    )
    assert "ASK-vs-EDIT ROUTING:" not in rendered
    assert "Workflow-improvement questions about a specific present block" not in rendered
    assert "Resolve from context before asking on build/edit requests" not in rendered
    assert "Carry forward edit intent from chat_history" not in rendered
    assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS:" not in rendered
    assert "Explicit edit/debug requests remain edit requests" not in rendered


def test_prompt_does_not_encode_request_policy_credential_verdicts() -> None:
    rendered = _render_agent_prompt()

    assert "REQUEST POLICY: WORKFLOW CREDENTIAL INPUTS UNBOUND." not in rendered
    assert "`clarification_reason: workflow_credential_inputs_unbound`" not in rendered
    assert 'data.skip_reason="workflow_credential_inputs_unbound"' not in rendered


def test_prompt_requires_display_ready_plain_text_responses() -> None:
    rendered = _render_agent_prompt()

    assert "`user_response` is rendered as Markdown." in rendered
    assert "use Markdown lists when a sequence helps" in rendered
    assert "fenced code blocks for JSON, code, templates" in rendered
    assert "rather than flattening them" in rendered
    assert "Do not output raw HTML" in rendered
