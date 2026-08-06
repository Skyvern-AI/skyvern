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


def test_workflow_credential_inputs_unbound_branch_teaches_reply_framing() -> None:
    rendered = _render_agent_prompt()

    assert "REQUEST POLICY: WORKFLOW CREDENTIAL INPUTS UNBOUND." in rendered
    assert "`clarification_reason: workflow_credential_inputs_unbound`" in rendered
    assert "Call `update_workflow` to land the user's edit" in rendered
    assert "I applied your requested change. I couldn't test the modified workflow" in rendered
    assert "add them via the Credentials UI" in rendered
    assert 'data.skip_reason="workflow_credential_inputs_unbound"' in rendered
