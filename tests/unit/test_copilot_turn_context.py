from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _build_user_context,
    _prior_run_debug_text,
    _store_turn_context_packet_on_context,
)
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

_EMPTY_WORKFLOW = "workflow_definition:\n  parameters: []\n  blocks: []\n"


def _render_runnable_draft_context(
    *,
    user_message: str,
    workflow_yaml: str = _EMPTY_WORKFLOW,
    prior_workflow_yaml: str = _WORKFLOW_V2,
    allow_run_blocks: bool = True,
) -> tuple[str, str | None]:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(allow_run_blocks=allow_run_blocks),
            user_message=user_message,
            workflow_yaml=workflow_yaml,
            prior_workflow_yaml=prior_workflow_yaml,
        )
    )
    summary = packet.runnable_draft_context.rendered_summary if packet.runnable_draft_context else None
    rendered = _build_user_context(
        workflow_yaml=workflow_yaml,
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message=user_message,
        runnable_draft_summary=summary or "",
    )
    return rendered, summary


@pytest.mark.parametrize(
    "user_message",
    [
        pytest.param("Run the draft again unchanged.", id="rerun"),
        pytest.param("Replace the saved binding with credential B, then continue.", id="replacement"),
    ],
)
def test_runnable_draft_context_is_the_same_factual_packet_for_follow_up_requests(user_message: str) -> None:
    rendered, summary = _render_runnable_draft_context(user_message=user_message)

    assert summary is not None
    assert "uncommitted workflow draft" in summary
    assert "not the current canvas workflow" in summary
    assert "remains runnable by its top-level block labels" in summary
    assert "open_site, summarize_result" in summary
    assert "The user is asking" not in summary
    assert "run_blocks_and_collect_debug" not in summary
    assert "update_and_run_blocks" not in summary
    assert "RUNNABLE UNCOMMITTED DRAFT (not on the canvas):" in rendered
    assert summary in rendered


@pytest.mark.parametrize(
    ("workflow_yaml", "prior_workflow_yaml", "allow_run_blocks"),
    [
        pytest.param(_WORKFLOW_V1, _WORKFLOW_V2, True, id="current-canvas"),
        pytest.param(_EMPTY_WORKFLOW, _EMPTY_WORKFLOW, True, id="no-draft"),
        pytest.param(_EMPTY_WORKFLOW, _WORKFLOW_V2, False, id="no-run-authority"),
    ],
)
def test_runnable_draft_context_controls_do_not_project_a_draft_section(
    workflow_yaml: str,
    prior_workflow_yaml: str,
    allow_run_blocks: bool,
) -> None:
    rendered, summary = _render_runnable_draft_context(
        user_message="Continue.",
        workflow_yaml=workflow_yaml,
        prior_workflow_yaml=prior_workflow_yaml,
        allow_run_blocks=allow_run_blocks,
    )

    assert summary is None
    assert "RUNNABLE UNCOMMITTED DRAFT (not on the canvas):" not in rendered


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


def test_attached_files_reach_the_prompt_with_their_ids_and_missing_state() -> None:
    """The prompt is the bar: a file id the model can put in ``file_url``, and a missing file
    named as missing rather than silently dropped into a plausible-looking reference."""
    from skyvern.forge.sdk.copilot.agent import _build_user_context
    from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="check every url in this sheet",
            workflow_yaml="workflow_definition:\n  blocks: []",
            attached_files=[
                CopilotAttachedFile(file_id="file_live", filename="targets.xlsx", available=True),
                CopilotAttachedFile(file_id="file_gone", filename="old.csv", available=False),
            ],
        )
    )

    assert packet.attached_file_context is not None
    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message="check every url in this sheet",
        attached_files_summary=packet.attached_file_context.render_prompt_block(),
    )

    assert "targets.xlsx" in rendered
    assert "file_live" in rendered
    assert "old.csv (file_id: file_gone) — NO LONGER AVAILABLE" in rendered


def test_every_file_a_message_can_carry_reaches_the_prompt() -> None:
    """The prompt lists a bounded number of files, so a message must not be able to attach more than
    it shows; otherwise "process every attached sheet" silently drops the ones past the cut."""
    import pydantic

    from skyvern.forge.sdk.schemas.workflow_copilot import (
        MAX_ATTACHED_FILES_PER_MESSAGE,
        CopilotAttachedFile,
        WorkflowCopilotChatRequest,
    )

    current = [f"file_{index}" for index in range(MAX_ATTACHED_FILES_PER_MESSAGE)]
    older = [f"file_old_{index}" for index in range(5)]
    request = {"workflow_permanent_id": "wpid_1", "workflow_id": "w_1", "message": "go", "workflow_yaml": ""}
    with pytest.raises(pydantic.ValidationError):
        WorkflowCopilotChatRequest(**request, attached_file_ids=[*current, "file_one_too_many"])
    WorkflowCopilotChatRequest(**request, attached_file_ids=current)

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="process every attached sheet",
            attached_files=[
                CopilotAttachedFile(file_id=file_id, filename=f"{file_id}.csv") for file_id in [*current, *older]
            ],
        )
    )

    assert packet.attached_file_context is not None
    rendered = packet.attached_file_context.render_prompt_block()
    assert all(f"(file_id: {file_id})" in rendered for file_id in current)


def test_a_turn_with_no_attachments_renders_no_attachment_section() -> None:
    from skyvern.forge.sdk.copilot.agent import _build_user_context

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="build a workflow",
            workflow_yaml="workflow_definition:\n  blocks: []",
        )
    )

    assert packet.attached_file_context is None
    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message="build a workflow",
    )
    assert "FILES THE USER ATTACHED" not in rendered


def test_a_filename_shaped_like_a_credential_is_redacted_while_ordinary_names_survive() -> None:
    """The deterministic patterns only catch secrets they can name, and no semantic screen sees a
    filename, so a token-shaped stem is replaced while everyday names stay readable."""
    from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="parse these",
            attached_files=[
                CopilotAttachedFile(file_id="file_1", filename="AKIA1B2c3D4e5F6g7H8i9J0kLmNoP.csv"),
                CopilotAttachedFile(file_id="file_2", filename="Q3_Report_2026_Final.xlsx"),
                CopilotAttachedFile(file_id="file_3", filename="targets.xlsx"),
                CopilotAttachedFile(file_id="file_4", filename="CustomerOrdersExport20260911.csv"),
                CopilotAttachedFile(file_id="file_5", filename="SalesPipelineExport2026Q3.xlsx"),
                CopilotAttachedFile(file_id="file_6", filename="X7yqwerty2Z8A1B2C3D4E5F6G7H.csv"),
                CopilotAttachedFile(file_id="file_7", filename="ghp_A1b2c3D4e5F6g7H8i9J0kLmNoPqR.csv"),
            ],
        )
    )

    assert packet.attached_file_context is not None
    rendered = packet.attached_file_context.render_prompt_block()

    assert "AKIA1B2c3D4e5F6g7H8i9J0kLmNoP" not in rendered
    assert "[REDACTED_SECRET].csv (file_id: file_1)" in rendered
    assert "Q3_Report_2026_Final.xlsx" in rendered
    assert "targets.xlsx" in rendered
    # Separator-free export names carry whole words, so they stay readable.
    assert "CustomerOrdersExport20260911.csv" in rendered
    assert "SalesPipelineExport2026Q3.xlsx" in rendered
    # One lowercase run can happen by chance in a random token, so it is not evidence of words.
    assert "X7yqwerty2Z8A1B2C3D4E5F6G7H" not in rendered
    assert "[REDACTED_SECRET].csv (file_id: file_6)" in rendered
    # A token alphabet carries underscores and dashes, so the shape check reads those too.
    assert "ghp_A1b2c3D4e5F6g7H8i9J0kLmNoPqR" not in rendered
    assert "[REDACTED_SECRET].csv (file_id: file_7)" in rendered


def test_a_secret_in_an_attachment_filename_never_reaches_the_prompt() -> None:
    """The filename is user-chosen text that bypasses the message safety screen, and it is replayed
    on every later turn — so the prompt boundary has to redact it like every other value."""
    from skyvern.forge.sdk.copilot.agent import _build_user_context
    from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile

    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="parse it",
            workflow_yaml="workflow_definition:\n  blocks: []",
            attached_files=[
                CopilotAttachedFile(file_id="file_7", filename="export password=hunter2.csv", available=True)
            ],
        )
    )
    assert packet.attached_file_context is not None

    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message="parse it",
        attached_files_summary=packet.attached_file_context.render_prompt_block(),
    )

    assert "hunter2" not in rendered
    assert "file_7" in rendered
