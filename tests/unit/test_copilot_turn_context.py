from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import _prior_run_debug_text, _store_turn_context_packet_on_context
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
            prior_run_packet={"failure": {"block_label": "block_1"}},
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
            prior_run_packet={"failure": {"reason": "timeout"}},
        )
    )

    assert packet.workflow_context is not None
    assert packet.run_context is not None
    assert packet.run_context.packet == {"failure": {"reason": "timeout"}}


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
        )
    )

    dumped = packet.model_dump_json()
    assert "hunter2" not in dumped
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in dumped
    assert "[REDACTED_SECRET]" in dumped


def test_the_assembler_stores_a_prior_run_packet_exactly_as_it_arrives() -> None:
    """The packet is redacted where it is built, not here, so this pins that the assembler adds no
    second pass — and that anything reaching it unredacted stays that way."""
    arrived = {"failure": {"reason": "extraction failed with password=hunter2", "failing_line": 6}}

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="fix it",
            workflow_yaml="workflow_definition:\n  blocks: []",
            prior_run_packet=arrived,
        )
    )

    assert packet.run_context is not None
    assert packet.run_context.packet == arrived


def test_size_budget_truncates_and_reports_omission() -> None:
    packet = TurnContextAssembler(workflow_char_budget=24).assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="Update it",
            workflow_yaml="workflow_definition:\n  blocks:\n    - label: very_long_block_label",
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
        prior_copilot_workflow_yaml=None,
    )

    assert ctx.turn_context_packet is not None
    assert ctx.turn_context_packet.workflow_context is not None


def test_a_prior_runs_typed_packet_reaches_the_turn_context() -> None:
    # A chat opened about a run this turn did not perform. Without the packet the only record of
    # that run is a rendered sentence, which cannot say user_code_error at line 6.
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="fix the extraction",
            workflow_yaml="workflow_definition:\n  blocks: []",
            prior_run_packet={
                "contract_version": "build_test_evidence_packet_v1",
                "failure": {"error_codes": ["user_code_error"], "failing_line": 6},
            },
        )
    )

    assert packet.run_context is not None
    assert packet.run_context.packet is not None
    assert packet.run_context.packet["failure"]["error_codes"] == ["user_code_error"]
    assert packet.run_context.packet["failure"]["failing_line"] == 6


def test_a_turn_with_no_prior_run_reports_it_unavailable_rather_than_empty() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="build something new",
            workflow_yaml="workflow_definition:\n  blocks: []",
        )
    )

    assert packet.run_context is None
    assert "latest_run_result" in [omission.context_key for omission in packet.omissions]


def test_the_prior_runs_failing_line_reaches_the_model_input_without_a_request_policy() -> None:
    packet = {"run": {"workflow_run_id": "wr_1", "status": "failed"}, "failure": {"failing_line": 6}}

    assert "6" in _prior_run_debug_text(packet)
    assert _prior_run_debug_text(None) == ""


def test_the_rendered_prompt_hides_the_secret_and_keeps_the_facts() -> None:
    """The bar is the rendered user turn, not the redaction helper: a redaction that destroys the
    packet and one that works are indistinguishable from the secret's absence alone."""
    from skyvern.forge.sdk.copilot.agent import _build_user_context, _prior_run_debug_text

    packet = {
        "run": {"workflow_run_id": "wr_42", "status": "failed"},
        "failure": {
            "reason": "extraction failed with password=hunter2",
            "failing_line": 6,
            "error_codes": ["user_code_error"],
        },
    }

    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="fix it",
        user_workflow_change_summary=None,
    )

    assert "hunter2" not in rendered
    assert "wr_42" in rendered
    assert "user_code_error" in rendered
    assert '"failing_line": 6' in rendered or '"failing_line":6' in rendered
