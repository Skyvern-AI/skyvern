from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import (
    _build_user_context,
    _prior_run_debug_text,
    _store_turn_context_packet_on_context,
    _test_outcome_unseen,
    _unseen_copilot_test_packets,
    _workflow_yaml_for_prompt,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.repair_origin_run import RepairOriginBinding
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_context import (
    COPILOT_TESTS_ENDED_AFTER_THEIR_TURN_KEY,
    TurnContextAssembler,
    TurnContextInputs,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from tests.unit.copilot_route_test_support import narrative_payload_with_run
from tests.unit.copilot_test_helpers import stub_copilot_agent_loop


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
_WORKFLOW_WITH_NOTE = _WORKFLOW_V1 + "      notes: hidden note\n"
_WORKFLOW_WITH_SECRET_SHAPES = (
    "workflow_definition:\n  blocks:\n"
    "    - block_type: http_request\n      label: fetch\n      url: https://example.com\n"
    '      headers:\n        Authorization: "Bearer {{ token }}"\n'
    "    - block_type: code\n      label: prepare\n      code: |\n"
    '        api_key = "{{ key }}"\n'
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


@pytest.mark.parametrize(
    ("request_yaml", "prior_yaml", "doc_editor_enabled", "changed"),
    [
        pytest.param(_WORKFLOW_WITH_NOTE, _WORKFLOW_WITH_NOTE, False, False, id="doc-disabled-annotated-prior"),
        pytest.param(_WORKFLOW_V1, _WORKFLOW_WITH_NOTE, False, False, id="doc-disabled-client-omits-notes"),
        pytest.param(
            _WORKFLOW_WITH_NOTE,
            _WORKFLOW_V1 + "      notes: previous note\n      description: previous description\n",
            False,
            False,
            id="doc-disabled-annotation-only-difference",
        ),
        pytest.param(_WORKFLOW_WITH_NOTE, _WORKFLOW_WITH_NOTE, True, False, id="doc-enabled-unchanged"),
        pytest.param(_WORKFLOW_WITH_NOTE, _WORKFLOW_V1, True, True, id="doc-enabled-note-edit"),
        pytest.param(
            _WORKFLOW_WITH_SECRET_SHAPES, _WORKFLOW_WITH_SECRET_SHAPES, False, False, id="doc-disabled-secret-shapes"
        ),
        pytest.param(
            _WORKFLOW_WITH_SECRET_SHAPES, _WORKFLOW_WITH_SECRET_SHAPES, True, False, id="doc-enabled-secret-shapes"
        ),
        pytest.param(
            _WORKFLOW_WITH_SECRET_SHAPES.replace("example.com", "example.org"),
            _WORKFLOW_WITH_SECRET_SHAPES,
            False,
            True,
            id="real-edit-with-secret-shapes",
        ),
    ],
)
def test_turn_context_change_summary_applies_doc_policy_to_both_sides(
    request_yaml: str,
    prior_yaml: str,
    doc_editor_enabled: bool,
    changed: bool,
) -> None:
    ctx = CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml=request_yaml,
        browser_session_id=None,
        stream=MagicMock(),
        doc_editor_enabled=doc_editor_enabled,
    )
    safe_yaml = _workflow_yaml_for_prompt(ctx, request_yaml)

    _store_turn_context_packet_on_context(
        ctx,
        request_policy=RequestPolicy(),
        chat_request=SimpleNamespace(message="Continue", workflow_yaml=request_yaml),
        chat_history=[],
        prior_copilot_workflow_yaml=prior_yaml,
    )

    assert ctx.turn_context_packet is not None
    assert ctx.turn_context_packet.workflow_context is not None
    assert ctx.turn_context_packet.workflow_context.yaml == safe_yaml
    assert (ctx.turn_context_packet.workflow_change_context is not None) is changed


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


def _goal_workflow_yaml(*, goal_needs_regeneration: bool | None) -> str:
    stale = (
        ""
        if goal_needs_regeneration is None
        else f"    goal_needs_regeneration: {str(goal_needs_regeneration).lower()}\n"
    )
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: get_invoice\n"
        "    prompt: Download last month's invoice\n"
        "    user_owned_goal: true\n"
        f"{stale}"
        "    code: |\n"
        "      await page.goto(url)\n"
    )


def test_a_block_awaiting_a_goal_rebuild_reaches_the_prompt_as_a_fact() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="how long does this take to run?",
            workflow_yaml=_goal_workflow_yaml(goal_needs_regeneration=True),
        )
    )

    assert packet.user_goal_context is not None
    assert packet.user_goal_context.block_labels == ["get_invoice"]

    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message="how long does this take to run?",
        user_goal_summary=packet.user_goal_context.rendered_summary,
    )

    assert "USER-WRITTEN GOALS NOT APPLIED YET" in rendered
    assert "get_invoice" in rendered


def test_a_user_owned_goal_already_rebuilt_carries_no_prompt_section() -> None:
    packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            request_policy=RequestPolicy(),
            user_message="how long does this take to run?",
            workflow_yaml=_goal_workflow_yaml(goal_needs_regeneration=False),
        )
    )

    assert packet.user_goal_context is None

    rendered = _build_user_context(
        workflow_yaml="workflow_definition:\n  blocks: []",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text="",
        user_message="how long does this take to run?",
    )

    assert "USER-WRITTEN GOALS NOT APPLIED YET" not in rendered


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


_PAUSED_TEST_RUN_ID = "wr_paused_test"
_USER_RUN_ID = "wr_user_run"
_PAUSED_TURN_REPLY_AT = datetime(2026, 10, 30, 16, 50, 32, 751246)
_FOLLOW_UP_STARTED_AT = datetime(2026, 10, 30, 16, 51, 47, 912004, tzinfo=timezone.utc)


def _reply(
    content: str, run_id: str | None, started_at: datetime | None, created_at: datetime
) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
        content=content,
        narrative_payload={
            **narrative_payload_with_run(run_id),
            "startedAt": started_at.isoformat() if started_at else None,
        },
        created_at=created_at,
    )


def _paused_test_history() -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="Test this whole workflow end to end exactly as it is.",
            created_at=datetime(2026, 10, 30, 16, 50, 7, 747874),
        ),
        _reply(
            "The test has not completed end to end: manager_approval is paused.",
            _PAUSED_TEST_RUN_ID,
            datetime(2026, 10, 30, 16, 50, 8, 102311, tzinfo=timezone.utc),
            _PAUSED_TURN_REPLY_AT,
        ),
    ]


def _history_with_unrelated_follow_up(follow_up_started_at: datetime | None) -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        *_paused_test_history(),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="Unrelated question: what is the title of this workflow?",
            created_at=datetime(2026, 10, 30, 16, 51, 47, 548647),
        ),
        _reply(
            "The title of this workflow is Approval then final step.",
            None,
            follow_up_started_at,
            datetime(2026, 10, 30, 16, 51, 56, 552923),
        ),
    ]


def _test_run(finished_at: datetime | None) -> WorkflowRun:
    return WorkflowRun(
        workflow_run_id=_PAUSED_TEST_RUN_ID,
        workflow_id="w_1",
        workflow_permanent_id="wfp-1",
        organization_id="org-1",
        status=WorkflowRunStatus.failed if finished_at else WorkflowRunStatus.paused,
        created_at=datetime(2026, 10, 30, 16, 50, 18, 670262),
        modified_at=datetime(2026, 10, 30, 16, 50, 44, tzinfo=timezone.utc),
        finished_at=finished_at,
        failure_reason="RuntimeError: QA deliberate failure in the final block" if finished_at else None,
    )


def _binding(run: WorkflowRun | None) -> RepairOriginBinding:
    return RepairOriginBinding(
        workflow_run_id=None,
        browser_session_id=None,
        refusal=None,
        status=run.status if run else None,
        copilot_run=run is not None,
        run=run,
    )


def _copilot_ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )


_ENDED_AFTER_REPLY = datetime(2026, 10, 30, 16, 50, 44, 159261, tzinfo=timezone.utc)
_ENDED_DURING_FOLLOW_UP = datetime(2026, 10, 30, 16, 51, 50, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("history", "finished_at", "reported"),
    [
        pytest.param(_paused_test_history(), _ENDED_AFTER_REPLY, True, id="ended-after-reply"),
        pytest.param(_paused_test_history(), _ENDED_AFTER_REPLY.replace(tzinfo=None), True, id="naive-utc-run-row"),
        pytest.param(
            _paused_test_history(),
            datetime(2026, 10, 30, 16, 50, 30, tzinfo=timezone.utc),
            False,
            id="ended-in-own-turn",
        ),
        pytest.param(_paused_test_history(), _PAUSED_TURN_REPLY_AT, False, id="ended-at-reply"),
        pytest.param(_paused_test_history(), None, False, id="still-paused"),
        pytest.param(
            _history_with_unrelated_follow_up(_FOLLOW_UP_STARTED_AT), _ENDED_AFTER_REPLY, False, id="later-turn-saw-it"
        ),
        pytest.param(
            _history_with_unrelated_follow_up(_FOLLOW_UP_STARTED_AT),
            _ENDED_DURING_FOLLOW_UP,
            True,
            id="ended-during-unrelated-turn",
        ),
        pytest.param(
            _history_with_unrelated_follow_up(None),
            _ENDED_AFTER_REPLY,
            True,
            id="later-error-reply-without-started-at",
        ),
    ],
)
def test_a_test_outcome_is_reported_only_to_the_first_turn_after_it_ended(
    history: list[WorkflowCopilotChatHistoryMessage],
    finished_at: datetime | None,
    reported: bool,
) -> None:
    assert _test_outcome_unseen(history, _PAUSED_TEST_RUN_ID, finished_at) is reported


_TEST_PACKET = {
    "run": {"workflow_run_id": _PAUSED_TEST_RUN_ID, "status": "failed"},
    "failure": {"block_label": "final_step", "reason": "RuntimeError: QA deliberate failure in the final block"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_run_seed_id", "repair_origin_binding", "looked_up"),
    [
        pytest.param(_USER_RUN_ID, _binding(None), [_PAUSED_TEST_RUN_ID], id="looks-up-the-test-run"),
        pytest.param(_PAUSED_TEST_RUN_ID, _binding(_test_run(_ENDED_AFTER_REPLY)), [], id="reuses-a-finished-seed"),
        pytest.param(
            _PAUSED_TEST_RUN_ID, _binding(_test_run(None)), [_PAUSED_TEST_RUN_ID], id="re-reads-a-seed-still-paused"
        ),
    ],
)
async def test_a_paused_test_that_ended_later_reaches_the_next_turn_beside_the_users_run(
    monkeypatch: pytest.MonkeyPatch,
    last_run_seed_id: str,
    repair_origin_binding: RepairOriginBinding,
    looked_up: list[str],
) -> None:
    hydrated: list[str] = []
    resolved: list[str] = []
    user_run_packet = {"run": {"workflow_run_id": _USER_RUN_ID, "status": "failed"}}

    async def fake_hydrate(_ctx: CopilotContext, *, workflow_run_id: str | None) -> dict[str, dict[str, str]]:
        hydrated.append(workflow_run_id or "")
        return _TEST_PACKET

    async def fake_resolve(*, workflow_run_id: str, **_kwargs: object) -> RepairOriginBinding:
        resolved.append(workflow_run_id)
        return _binding(_test_run(_ENDED_AFTER_REPLY))

    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", fake_hydrate)
    monkeypatch.setattr(agent_module, "resolve_repair_origin_binding", fake_resolve)
    ctx = _copilot_ctx()
    history = _paused_test_history()

    packets = await _unseen_copilot_test_packets(
        ctx,
        messages=history,
        hydrated_run_id=_USER_RUN_ID,
        last_run_seed_id=last_run_seed_id,
        repair_origin_binding=repair_origin_binding,
    )

    assert resolved == looked_up
    assert hydrated == [_PAUSED_TEST_RUN_ID]
    _store_turn_context_packet_on_context(
        ctx,
        request_policy=RequestPolicy(),
        chat_request=SimpleNamespace(message="Unrelated question", workflow_yaml=""),
        chat_history=history,
        prior_copilot_workflow_yaml=None,
        prior_run_packet=user_run_packet,
        copilot_test_packets=packets,
    )
    assert ctx.turn_context_packet is not None
    turn_context = ctx.turn_context_packet
    assert turn_context.run_context is not None
    assert turn_context.run_context.packet == user_run_packet
    assert turn_context.copilot_tests_ended_after_their_turn is not None
    assert [context.packet for context in turn_context.copilot_tests_ended_after_their_turn] == [_TEST_PACKET]
    assert {"run_context", "copilot_tests_ended_after_their_turn"} <= set(turn_context.to_trace_data()["sections"])
    rendered = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(user_run_packet, packets),
        user_message="Unrelated question",
        user_workflow_change_summary="",
    )
    debug_block = rendered.split("DEBUGGER RUN INFORMATION:", 1)[1].split("```")[1]
    rendered_packets = json.loads(debug_block.removeprefix("json").strip())
    assert rendered_packets["build_test_packet"] == user_run_packet
    assert rendered_packets[COPILOT_TESTS_ENDED_AFTER_THEIR_TURN_KEY] == [_TEST_PACKET]


@pytest.mark.asyncio
async def test_a_test_the_turn_already_hydrates_gets_no_second_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    hydrate = AsyncMock(return_value=_TEST_PACKET)
    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", hydrate)

    packets = await _unseen_copilot_test_packets(
        _copilot_ctx(),
        messages=_paused_test_history(),
        hydrated_run_id=_PAUSED_TEST_RUN_ID,
        last_run_seed_id=_PAUSED_TEST_RUN_ID,
        repair_origin_binding=_binding(_test_run(_ENDED_AFTER_REPLY)),
    )

    assert packets == []
    hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_paused_tests_that_both_ended_between_turns_both_reach_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [
        *_paused_test_history(),
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER,
            content="Test it again.",
            created_at=datetime(2026, 10, 30, 16, 51, 0),
        ),
        _reply(
            "The second test is paused at manager_approval too.",
            "wr_second_test",
            datetime(2026, 10, 30, 16, 51, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 30, 16, 51, 30),
        ),
    ]
    ended = datetime(2026, 10, 30, 16, 52, tzinfo=timezone.utc)

    async def fake_resolve(*, workflow_run_id: str, **_kwargs: object) -> RepairOriginBinding:
        return _binding(_test_run(ended).model_copy(update={"workflow_run_id": workflow_run_id}))

    async def fake_hydrate(_ctx: CopilotContext, *, workflow_run_id: str | None) -> dict[str, dict[str, str | None]]:
        return {"run": {"workflow_run_id": workflow_run_id, "status": "failed"}}

    monkeypatch.setattr(agent_module, "resolve_repair_origin_binding", fake_resolve)
    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", fake_hydrate)

    packets = await _unseen_copilot_test_packets(
        _copilot_ctx(),
        messages=history,
        hydrated_run_id=None,
        last_run_seed_id="wr_second_test",
        repair_origin_binding=_binding(None),
    )

    assert [packet["run"]["workflow_run_id"] for packet in packets] == [_PAUSED_TEST_RUN_ID, "wr_second_test"]


@pytest.mark.asyncio
async def test_a_named_test_still_paused_at_seed_time_reaches_the_turn_once_it_has_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hydrate = AsyncMock(return_value=_TEST_PACKET)
    monkeypatch.setattr(agent_module, "restore_pending_workflow_proposal", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "seed_repair_origin_run", AsyncMock(return_value=_binding(_test_run(None))))
    monkeypatch.setattr(
        agent_module, "resolve_repair_origin_binding", AsyncMock(return_value=_binding(_test_run(_ENDED_AFTER_REPLY)))
    )
    monkeypatch.setattr(agent_module, "hydrate_prior_run_packet", hydrate)
    stub_copilot_agent_loop(
        monkeypatch,
        AsyncMock(
            return_value=SimpleNamespace(
                final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[]
            )
        ),
    )
    build_context = MagicMock(wraps=agent_module._build_user_context)
    monkeypatch.setattr(agent_module, "_build_user_context", build_context)

    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="how did it go?",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_run_id=_PAUSED_TEST_RUN_ID,
            workflow_yaml="title: Canonical\n",
        ),
        chat_history=[],
        prior_user_messages=_paused_test_history(),
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(),
    )

    assert [call.kwargs["workflow_run_id"] for call in hydrate.await_args_list] == [_PAUSED_TEST_RUN_ID]
    debug = json.loads(build_context.call_args.kwargs["debug_run_info_text"])
    assert debug == {COPILOT_TESTS_ENDED_AFTER_THEIR_TURN_KEY: [_TEST_PACKET]}
