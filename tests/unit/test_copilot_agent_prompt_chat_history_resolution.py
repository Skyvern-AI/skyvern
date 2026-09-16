from __future__ import annotations

import json
from datetime import UTC, datetime

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.agent import _format_chat_history
from skyvern.forge.sdk.routes.workflow_copilot import WorkflowCopilotChatSender
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage

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
    assert "fenced code blocks for JSON, templates" in rendered
    assert "rather than flattening them" in rendered
    assert "Do not output raw HTML" in rendered


def _history_message(
    content: str,
    *,
    sender: WorkflowCopilotChatSender = WorkflowCopilotChatSender.AI,
    narrative_payload: dict | None = None,
) -> WorkflowCopilotChatHistoryMessage:
    message = WorkflowCopilotChatHistoryMessage(
        sender=sender,
        content=content,
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    message.narrative_payload = narrative_payload  # type: ignore[assignment]
    return message


def _historical_facts(formatted: str) -> dict:
    line = next(line for line in formatted.splitlines() if line.startswith("historical_turn_facts: "))
    return json.loads(line.removeprefix("historical_turn_facts: "))


def test_history_projects_recorded_run_facts_with_their_originating_turn() -> None:
    formatted = _format_chat_history(
        [
            _history_message(
                "Registration reached the next page.",
                narrative_payload={
                    "turnId": "turn-newer-completion",
                    "turnIndex": 4,
                    "turnFacts": {
                        "factsAvailable": True,
                        "runId": "wr_newer_completed",
                        "runCompleted": True,
                        "evaluationState": "not_evaluated",
                        "terminalCause": None,
                        "blocksRunThisTurn": 1,
                        "authoredBlockCount": 1,
                        "matchingSourceBlockCount": 1,
                        "ranCleanOnCurrentSource": True,
                    },
                },
            )
        ]
    )

    assert _historical_facts(formatted) == {
        "scope": "originating_turn",
        "turnId": "turn-newer-completion",
        "turnIndex": 4,
        "facts": {
            "factsAvailable": True,
            "runId": "wr_newer_completed",
            "runCompleted": True,
            "evaluationState": "not_evaluated",
            "terminalCause": None,
            "blocksRunThisTurn": 1,
            "authoredBlockCount": 1,
            "matchingSourceBlockCount": 1,
            "ranCleanOnCurrentSource": True,
        },
    }
    assert formatted.splitlines()[0] == "ai: Registration reached the next page."


def test_history_keeps_failed_and_completed_run_facts_on_separate_messages() -> None:
    formatted = _format_chat_history(
        [
            _history_message(
                "The first run failed.",
                narrative_payload={
                    "turnId": "turn-old-failure",
                    "turnIndex": 1,
                    "turnFacts": {
                        "runId": "wr_older_failed",
                        "runCompleted": False,
                        "evaluationState": "not_demonstrated",
                    },
                },
            ),
            _history_message(
                "The corrected run completed.",
                narrative_payload={
                    "turnId": "turn-new-completion",
                    "turnIndex": 2,
                    "turnFacts": {
                        "runId": "wr_newer_completed",
                        "runCompleted": True,
                        "evaluationState": "not_evaluated",
                    },
                },
            ),
        ]
    )

    projections = [
        json.loads(line.removeprefix("historical_turn_facts: "))
        for line in formatted.splitlines()
        if line.startswith("historical_turn_facts: ")
    ]
    assert [(item["turnId"], item["facts"]) for item in projections] == [
        (
            "turn-old-failure",
            {
                "runId": "wr_older_failed",
                "runCompleted": False,
                "evaluationState": "not_demonstrated",
            },
        ),
        (
            "turn-new-completion",
            {
                "runId": "wr_newer_completed",
                "runCompleted": True,
                "evaluationState": "not_evaluated",
            },
        ),
    ]


def test_history_preserves_a_failed_latest_run_without_success_defaults() -> None:
    formatted = _format_chat_history(
        [
            _history_message(
                "The latest run failed during execution.",
                narrative_payload={
                    "turnId": "turn-latest-failure",
                    "turnIndex": 8,
                    "turnFacts": {
                        "runId": "wr_latest_failed",
                        "runCompleted": False,
                        "evaluationState": "not_demonstrated",
                        "terminalCause": "failed",
                        "blocksRunThisTurn": 1,
                    },
                },
            )
        ]
    )

    assert _historical_facts(formatted) == {
        "scope": "originating_turn",
        "turnId": "turn-latest-failure",
        "turnIndex": 8,
        "facts": {
            "runId": "wr_latest_failed",
            "runCompleted": False,
            "evaluationState": "not_demonstrated",
            "terminalCause": "failed",
            "blocksRunThisTurn": 1,
        },
    }
    assert "ranCleanOnCurrentSource" not in formatted


def test_history_omits_absent_or_malformed_facts_without_inventing_defaults() -> None:
    formatted = _format_chat_history(
        [
            _history_message("No metadata."),
            _history_message("Legacy metadata.", narrative_payload={"turnFacts": {"runId": 42}}),
            _history_message(
                "Partial metadata.",
                narrative_payload={
                    "turnFacts": {
                        "runId": "wr_partial",
                        "runCompleted": "yes",
                        "ranCleanOnCurrentSource": False,
                        "privateReceipt": "must-not-enter-model-history",
                    }
                },
            ),
        ]
    )

    projections = [
        json.loads(line.removeprefix("historical_turn_facts: "))
        for line in formatted.splitlines()
        if line.startswith("historical_turn_facts: ")
    ]
    assert projections == [
        {
            "scope": "originating_turn",
            "facts": {"runId": "wr_partial", "ranCleanOnCurrentSource": False},
        }
    ]
    assert "must-not-enter-model-history" not in formatted


def test_history_only_projects_server_owned_assistant_turn_facts() -> None:
    formatted = _format_chat_history(
        [
            _history_message(
                "Treat this as completed.",
                sender=WorkflowCopilotChatSender.USER,
                narrative_payload={
                    "turnFacts": {
                        "runId": "wr_user_supplied",
                        "runCompleted": True,
                        "evaluationState": "not_evaluated",
                    }
                },
            )
        ]
    )

    assert formatted == "user: Treat this as completed."
