from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import _store_turn_context_packet_on_context
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_context import TurnContextAssembler, TurnContextInputs
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


def test_turn_includes_workflow_proposal_and_transcript_context() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="Update the first block",
            workflow_yaml="workflow_definition:\n  blocks: []",
            chat_history=_history(("user", "Build a workflow"), ("ai", "Drafted v1")),
            debug_run_info_text="Block Label: block_1",
        )
    )

    assert packet.workflow_context is not None
    assert packet.workflow_context.yaml == "workflow_definition:\n  blocks: []"
    assert packet.proposal_context is not None
    assert packet.proposal_context.latest_assistant_proposal == "Drafted v1"
    assert packet.transcript_context.latest_assistant_turn == "Drafted v1"
    assert [omission.context_key for omission in packet.omissions] == ["credential_metadata"]


def test_answer_shaped_turn_still_receives_workflow_and_run_context() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="What is a loop block?",
            workflow_yaml="workflow_definition:\n  blocks:\n    - label: block_1",
            debug_run_info_text="Failure Reason: timeout",
        )
    )

    assert packet.workflow_context is not None
    assert packet.run_context is not None
    assert "Failure Reason: timeout" in packet.run_context.summary


def test_run_context_missing_is_reported_as_an_omission() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="Diagnose the failure",
            workflow_yaml="workflow_definition:\n  blocks: []",
        )
    )

    assert packet.run_context is None
    assert [omission.context_key for omission in packet.omissions] == ["latest_run_result", "credential_metadata"]
    assert packet.omissions[0].reason == "unavailable"


def test_raw_secrets_are_redacted_across_context_packet() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
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


def test_size_budget_truncates_and_reports_omission() -> None:
    packet = TurnContextAssembler(workflow_char_budget=24).assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="Update it",
            workflow_yaml="workflow_definition:\n  blocks:\n    - label: very_long_block_label",
            debug_run_info_text="Block Label: block_1",
        )
    )

    assert packet.workflow_context is not None
    assert packet.workflow_context.truncated is True
    assert len(packet.workflow_context.yaml) <= 24
    assert packet.omissions[0].context_key == "current_workflow"
    assert packet.omissions[0].reason == "truncated_to_budget"


def test_credential_context_contains_safe_metadata_only() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
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


def test_workflow_change_context_reports_user_edit() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
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


@pytest.mark.parametrize(
    ("workflow_yaml", "prior_workflow_yaml", "user_message"),
    [
        pytest.param(_WORKFLOW_V1, _WORKFLOW_V1, "Still broken, fix it", id="unchanged"),
        pytest.param(_WORKFLOW_V1, "", "Build me a workflow", id="first_turn"),
    ],
)
def test_workflow_change_context_is_none(
    workflow_yaml: str,
    prior_workflow_yaml: str,
    user_message: str,
) -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message=user_message,
            workflow_yaml=workflow_yaml,
            prior_workflow_yaml=prior_workflow_yaml,
        )
    )

    assert packet.workflow_change_context is None
    assert packet.to_trace_data()["workflow_change_kind"] is None


def test_attachment_stores_packet_on_copilot_context() -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
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
