from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.agent import _store_turn_context_packet_on_context
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_context import TurnContextAssembler, TurnContextInputs
from skyvern.forge.sdk.copilot.turn_intent import (
    RequiredContextKey,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentExpectedOutput,
    TurnIntentMode,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


def _history(*pairs: tuple[str, str]) -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender(sender),
            content=content,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for sender, content in pairs
    ]


def test_edit_turn_includes_workflow_proposal_and_transcript_context() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        required_context=[RequiredContextKey.CURRENT_WORKFLOW, RequiredContextKey.LATEST_ASSISTANT_PROPOSAL],
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        expected_output=TurnIntentExpectedOutput.WORKFLOW_UPDATE,
    )

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=intent,
            request_policy=RequestPolicy(),
            user_message="Update the first block",
            workflow_yaml="workflow_definition:\n  blocks: []",
            chat_history=_history(("user", "Build a workflow"), ("ai", "Drafted v1")),
        )
    )

    assert packet.workflow_context is not None
    assert packet.workflow_context.yaml == "workflow_definition:\n  blocks: []"
    assert packet.proposal_context is not None
    assert packet.proposal_context.latest_assistant_proposal == "Drafted v1"
    assert packet.transcript_context.latest_assistant_turn == "Drafted v1"
    assert packet.omissions == []


def test_repeated_reply_context_attached_when_assistant_repeats() -> None:
    from skyvern.forge.sdk.copilot.turn_intent import TurnIntentReasonCode
    from skyvern.forge.sdk.copilot.turn_outcome import build_minimal_turn_outcome
    from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind

    answer = "The file is stored as an artifact in the Artifacts section of the run results page."
    outcome = build_minimal_turn_outcome(answer, response_kind=ResponseKind.DIAGNOSE)
    history = [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="where is my file",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI,
            content=answer,
            turn_outcome=outcome,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="i cannot see it",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI,
            content=answer,
            turn_outcome=outcome,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.UNKNOWN,
                reason_codes=[TurnIntentReasonCode.USER_NON_PROGRESS],
            ),
            request_policy=RequestPolicy(),
            user_message="i still cannot see it",
            chat_history=history,
        )
    )

    assert packet.repeated_reply_context is not None
    assert outcome.normalized_reply_signature in packet.repeated_reply_context.blocked_signatures
    assert "repeated_reply_detected" in packet.repeated_reply_context.rendered_summary
    trace = packet.to_trace_data()
    assert "repeated_reply_context" in trace["sections"]


def test_repeated_reply_context_omitted_without_repeat() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(mode=TurnIntentMode.UNKNOWN),
            request_policy=RequestPolicy(),
            user_message="where is my file",
            chat_history=_history(("user", "build a workflow"), ("ai", "Drafted v1")),
        )
    )

    assert packet.repeated_reply_context is None
    trace = packet.to_trace_data()
    assert "repeated_reply_context" not in trace["sections"]
    assert trace["repeated_reply_count"] == 0


def test_diagnose_turn_includes_run_context_or_reports_missing() -> None:
    available = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.DIAGNOSE,
                required_context=[RequiredContextKey.LATEST_RUN_RESULT],
                expected_output=TurnIntentExpectedOutput.RUN_RESULT,
            ),
            request_policy=RequestPolicy(),
            user_message="Why did the run fail?",
            debug_run_info_text="Block Label: block_1\nFailure Reason: timeout",
        )
    )
    missing = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.DIAGNOSE,
                required_context=[RequiredContextKey.LATEST_RUN_RESULT],
                expected_output=TurnIntentExpectedOutput.RUN_RESULT,
            ),
            request_policy=RequestPolicy(),
            user_message="Diagnose the failure",
        )
    )

    assert available.run_context is not None
    assert "Failure Reason: timeout" in available.run_context.summary
    assert available.omissions == []
    assert missing.run_context is None
    assert missing.omissions[0].context_key == RequiredContextKey.LATEST_RUN_RESULT
    assert missing.omissions[0].reason == "unavailable"


def test_raw_secrets_are_redacted_across_context_packet() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.EDIT,
                required_context=[
                    RequiredContextKey.CURRENT_WORKFLOW,
                    RequiredContextKey.LATEST_ASSISTANT_PROPOSAL,
                    RequiredContextKey.LATEST_RUN_RESULT,
                ],
            ),
            request_policy=RequestPolicy(),
            user_message="Use password: hunter2",
            workflow_yaml="navigation_goal: use password=hunter2 and token=sk-abcdefghijklmnopqrstuvwxyz1234567890",
            chat_history=_history(("ai", "The password=hunter2 failed")),
            debug_run_info_text="Failure includes password=hunter2",
        )
    )

    dumped = packet.model_dump_json()
    assert "hunter2" not in dumped
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in dumped
    assert "[REDACTED_SECRET]" in dumped


def test_docs_answer_turn_does_not_pull_workflow_or_run_context() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.DOCS_ANSWER,
                required_context=[RequiredContextKey.DOCS_CONTEXT],
            ),
            request_policy=RequestPolicy(),
            user_message="What is a loop block?",
            workflow_yaml="workflow_definition:\n  blocks:\n    - label: block_1",
            debug_run_info_text="Failure Reason: hidden",
        )
    )

    assert packet.docs_context is not None
    assert packet.workflow_context is None
    assert packet.run_context is None


def test_browser_state_required_context_reports_not_implemented_omission() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.EDIT,
                required_context=[RequiredContextKey.CURRENT_WORKFLOW, RequiredContextKey.BROWSER_STATE],
            ),
            request_policy=RequestPolicy(),
            user_message="Continue in the open browser",
            workflow_yaml="workflow_definition:\n  blocks: []",
        )
    )

    assert packet.workflow_context is not None
    browser_state_omissions = [
        omission for omission in packet.omissions if omission.context_key == RequiredContextKey.BROWSER_STATE
    ]
    assert len(browser_state_omissions) == 1
    assert browser_state_omissions[0].reason == "not_implemented"


def test_size_budget_truncates_and_reports_omission() -> None:
    packet = TurnContextAssembler(workflow_char_budget=24).assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.EDIT,
                required_context=[RequiredContextKey.CURRENT_WORKFLOW],
            ),
            request_policy=RequestPolicy(),
            user_message="Update it",
            workflow_yaml="workflow_definition:\n  blocks:\n    - label: very_long_block_label",
        )
    )

    assert packet.workflow_context is not None
    assert packet.workflow_context.truncated is True
    assert len(packet.workflow_context.yaml) <= 24
    assert packet.omissions[0].context_key == RequiredContextKey.CURRENT_WORKFLOW
    assert packet.omissions[0].reason == "truncated_to_budget"


def test_credential_context_contains_safe_metadata_only() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.BUILD,
                required_context=[RequiredContextKey.CREDENTIAL_METADATA],
            ),
            request_policy=RequestPolicy(
                credential_input_kind="credential_id",
                credential_refs=["cred_safe"],
                resolved_credentials=[
                    SimpleNamespace(
                        credential_id="cred_safe",
                        name="Saved Login",
                        credential_type="password",
                        vault_type="bitwarden",
                        tested_url="https://example.test/login",
                        browser_profile_id="bp-1",
                        username="private@example.test",
                        totp_identifier="private-phone",
                        user_context="Click SSO",
                    )
                ],
            ),
            user_message="Build with cred_safe",
        )
    )

    assert packet.credential_context is not None
    assert packet.credential_context.credentials[0].credential_id == "cred_safe"
    dumped = packet.credential_context.model_dump_json()
    assert "private@example.test" not in dumped
    assert "private-phone" not in dumped
    assert "Click SSO" not in dumped


_WORKFLOW_V1 = (
    "title: t\nworkflow_definition:\n  parameters: []\n  blocks:\n"
    "    - block_type: goto_url\n      label: open_site\n      url: https://example.com\n"
)
_WORKFLOW_V2 = _WORKFLOW_V1 + (
    "    - block_type: text_prompt\n      label: summarize_result\n      llm_key: x\n      prompt: ok\n"
)


def _change_intent() -> TurnIntent:
    return TurnIntent(
        mode=TurnIntentMode.EDIT,
        required_context=[RequiredContextKey.CURRENT_WORKFLOW, RequiredContextKey.WORKFLOW_CHANGE],
        authority=TurnIntentAuthority(may_update_workflow=True),
    )


def test_workflow_change_context_reports_user_edit() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=_change_intent(),
            request_policy=RequestPolicy(),
            user_message="I added a block, does this look right?",
            workflow_yaml=_WORKFLOW_V2,
            prior_workflow_yaml=_WORKFLOW_V1,
        )
    )

    assert packet.workflow_change_context is not None
    assert packet.workflow_change_context.kind == "user_modified_since_last_turn"
    assert "summarize_result" in packet.workflow_change_context.rendered_summary
    assert packet.to_trace_data()["workflow_change_kind"] == "user_modified_since_last_turn"


def test_workflow_change_context_omitted_when_unchanged() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=_change_intent(),
            request_policy=RequestPolicy(),
            user_message="Still broken, fix it",
            workflow_yaml=_WORKFLOW_V1,
            prior_workflow_yaml=_WORKFLOW_V1,
        )
    )

    assert packet.workflow_change_context is None
    assert packet.to_trace_data()["workflow_change_kind"] is None


def test_workflow_change_context_omitted_on_first_turn() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=_change_intent(),
            request_policy=RequestPolicy(),
            user_message="Build me a workflow",
            workflow_yaml=_WORKFLOW_V1,
            prior_workflow_yaml="",
        )
    )

    assert packet.workflow_change_context is None
    assert packet.to_trace_data()["workflow_change_kind"] is None


def test_workflow_change_context_skipped_when_not_required() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=TurnIntent(
                mode=TurnIntentMode.EDIT,
                required_context=[RequiredContextKey.CURRENT_WORKFLOW],
            ),
            request_policy=RequestPolicy(),
            user_message="Update it",
            workflow_yaml=_WORKFLOW_V2,
            prior_workflow_yaml=_WORKFLOW_V1,
        )
    )

    assert packet.workflow_change_context is None


def test_shadow_attachment_stores_packet_on_copilot_context() -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        turn_intent=TurnIntent(
            mode=TurnIntentMode.EDIT,
            required_context=[RequiredContextKey.CURRENT_WORKFLOW],
        ),
    )

    _store_turn_context_packet_on_context(
        ctx,
        request_policy=RequestPolicy(),
        chat_request=SimpleNamespace(
            message="Update it",
            workflow_yaml="workflow_definition:\n  blocks: []",
        ),
        chat_history=[],
        debug_run_info_text="",
        prior_copilot_workflow_yaml=None,
    )

    assert ctx.turn_context_packet is not None
    assert ctx.turn_context_packet.workflow_context is not None
