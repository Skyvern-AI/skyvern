"""Tests for the small pure helpers on workflow_copilot.py.

Covers the rollback/auto-accept safety net (``_should_restore_persisted_workflow``,
``_effective_auto_accept``, ``_proposal_disposition``), YAML normalization
(``_normalize_copilot_yaml``), prior-YAML resolution
(``_blockless_submission_fallback``, ``_prior_copilot_workflow_yaml``), and the
SSE terminal-frame invariant (``_ensure_terminal_frame``, SKY-9232).
"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.agent import _build_timeout_exit_result
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext, ProposedCredential, StructuredContext
from skyvern.forge.sdk.copilot.workflow_credential_utils import workflow_credential_ids
from skyvern.forge.sdk.routes.workflow_copilot import (
    _assistant_execution_receipts,
    _blockless_submission_fallback,
    _build_proposed_workflow_data,
    _effective_auto_accept,
    _ensure_terminal_frame,
    _normalize_copilot_yaml,
    _prior_copilot_workflow_yaml,
    _prior_global_llm_context,
    _proposal_disposition,
    _run_grant_workflow_yaml,
    _should_commit_staged_workflow,
    _should_restore_persisted_workflow,
    _workflow_copilot_ingress_log_fields,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatSender,
    WorkflowCopilotStreamResponseUpdate,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.runs import ProxyLocation
from tests.unit.copilot_test_helpers import make_copilot_ctx


def test_workflow_copilot_ingress_log_fields_are_content_free() -> None:
    literal = "Hunter2Portal!"
    fields = _workflow_copilot_ingress_log_fields(f"The password is {literal}")

    assert fields == {"message_length": len(f"The password is {literal}")}
    assert literal not in repr(fields)


def test_proposed_workflow_persists_exact_version_execution_receipts() -> None:
    workflow = MagicMock()
    workflow.title = "Draft"
    workflow.model_dump.return_value = {"workflow_id": "w_test"}
    result = AgentResult(
        user_response="done",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml="title: Draft\nworkflow_definition:\n  blocks: []\n",
        executed_block_fingerprints={"step": {"version_b", "version_a"}},
    )

    proposed = _build_proposed_workflow_data(workflow, result)

    assert proposed["_copilot_tested_block_fingerprints"] == {"step": ["version_a", "version_b"]}


def test_assistant_history_retains_execution_receipts_after_proposal_clear() -> None:
    first = MagicMock(
        sender=WorkflowCopilotChatSender.AI,
        narrative_payload={"testedBlockFingerprints": {"step": ["version_a"]}},
    )
    second = MagicMock(
        sender=WorkflowCopilotChatSender.AI,
        narrative_payload={"testedBlockFingerprints": {"step": ["version_b"], "other": ["version_c"]}},
    )

    assert _assistant_execution_receipts([first, second]) == {
        "step": {"version_a", "version_b"},
        "other": {"version_c"},
    }


def test_interrupted_assistant_turn_expires_older_credential_proposal() -> None:
    context = StructuredContext(
        proposed_credential=ProposedCredential(
            credential_id="cred_previous",
            admitted_url="https://example.invalid",
        )
    ).to_json_str()
    prior = MagicMock(global_llm_context=context, turn_outcome=MagicMock())
    interrupted = MagicMock(global_llm_context=None, turn_outcome=MagicMock())

    carried = _prior_global_llm_context([prior, interrupted])

    assert StructuredContext.from_json_str(carried).proposed_credential is None


def _agent_result(
    *,
    persisted: bool,
    proposal_disposition: str = "auto_applicable",
    cancelled: bool = False,
    updated_workflow: Any = None,
    canonical_was_persisted_due_to_param_change: bool = False,
    **kwargs: Any,
) -> MagicMock:
    """MagicMock with override flags explicitly set so a forgotten attr can't pass via MagicMock truthiness."""
    r = MagicMock()
    r.workflow_was_persisted = persisted
    r.proposal_disposition = proposal_disposition
    r.cancelled = cancelled
    r.updated_workflow = updated_workflow
    # SKY-10318: explicitly set the new staging flag so MagicMock truthiness
    # doesn't accidentally trigger the degraded-path branch in
    # `_should_restore_persisted_workflow`.
    r.canonical_was_persisted_due_to_param_change = canonical_was_persisted_due_to_param_change
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


class TestShouldRestorePersistedWorkflow:
    def test_restores_for_non_auto_accept_and_persisted_workflow(self) -> None:
        agent_result = _agent_result(persisted=True)

        assert _should_restore_persisted_workflow(False, agent_result) is True
        assert _should_restore_persisted_workflow(None, agent_result) is True

    def test_does_not_restore_for_auto_accept_or_unpersisted_result(self) -> None:
        persisted = _agent_result(persisted=True, updated_workflow=MagicMock())
        not_persisted = _agent_result(persisted=False)

        assert _should_restore_persisted_workflow(True, persisted) is False
        assert _should_restore_persisted_workflow(False, not_persisted) is False
        assert _should_restore_persisted_workflow(False, None) is False

    @pytest.mark.parametrize(
        "override_kwargs",
        [
            pytest.param({"proposal_disposition": "review_untested"}, id="review_untested"),
            pytest.param({"cancelled": True}, id="cancelled"),
            pytest.param({"proposal_disposition": "review_tested"}, id="review_tested"),
        ],
    )
    def test_wip_forces_rollback_under_auto_accept(self, override_kwargs: dict[str, Any]) -> None:
        agent_result = _agent_result(persisted=True, updated_workflow=MagicMock(), **override_kwargs)

        assert _should_restore_persisted_workflow(True, agent_result) is True
        assert _should_restore_persisted_workflow(False, agent_result) is True


class TestShouldCommitStagedWorkflow:
    def test_tested_proposal_from_a_question_turn_stays_pending_under_auto_accept(self) -> None:
        ask_result = _agent_result(
            persisted=False,
            proposal_disposition="review_tested",
            updated_workflow=MagicMock(),
            has_staged_proposal=True,
        )

        assert _effective_auto_accept(True, ask_result) is False
        assert _should_commit_staged_workflow(True, ask_result) is False

    def test_auto_applicable_proposal_still_commits_under_auto_accept(self) -> None:
        reply_result = _agent_result(
            persisted=False,
            proposal_disposition="auto_applicable",
            updated_workflow=MagicMock(),
            has_staged_proposal=True,
        )

        assert _effective_auto_accept(True, reply_result) is True
        assert _should_commit_staged_workflow(True, reply_result) is True
        assert _should_commit_staged_workflow(False, reply_result) is False


class TestEffectiveAutoAccept:
    @pytest.mark.parametrize(
        ("proposal_disposition", "cancelled"),
        [
            pytest.param("review_untested", False, id="review_untested"),
            pytest.param("auto_applicable", True, id="cancelled"),
            pytest.param("review_tested", False, id="review_tested"),
            pytest.param("no_proposal", False, id="no_proposal"),
        ],
    )
    def test_disposition_or_cancellation_overrides_auto_accept(
        self, proposal_disposition: str, cancelled: bool
    ) -> None:
        result = MagicMock()
        result.proposal_disposition = proposal_disposition
        result.cancelled = cancelled

        assert _effective_auto_accept(True, result) is False
        assert _effective_auto_accept(False, result) is False

    def test_missing_proposal_disposition_is_no_proposal_without_updated_workflow(self) -> None:
        result = MagicMock(spec=["updated_workflow"])
        result.updated_workflow = None

        assert _proposal_disposition(result) == "no_proposal"

    def test_validated_proposal_respects_auto_accept_setting(self) -> None:
        validated = MagicMock()
        validated.proposal_disposition = "auto_applicable"
        validated.cancelled = False

        assert _effective_auto_accept(True, validated) is True
        assert _effective_auto_accept(False, validated) is False
        assert _effective_auto_accept(None, validated) is False

    def test_verified_fix_does_not_auto_apply_without_explicit_auto_accept(self) -> None:
        # Only the chat's explicit ``auto_accept`` opt-in may auto-apply an
        # auto_applicable proposal; a truthy ``apply_without_review`` attribute
        # must not force an auto-apply on its own.
        validated = MagicMock()
        validated.proposal_disposition = "auto_applicable"
        validated.cancelled = False
        validated.apply_without_review = True

        assert _effective_auto_accept(False, validated) is False
        assert _effective_auto_accept(None, validated) is False

    def test_no_agent_result_is_not_auto_applicable(self) -> None:
        assert _proposal_disposition(None) == "no_proposal"
        assert _effective_auto_accept(True, None) is False
        assert _effective_auto_accept(False, None) is False


def test_response_update_schema_omits_legacy_review_flags() -> None:
    assert "unvalidated" not in WorkflowCopilotStreamResponseUpdate.model_fields
    assert "force_review" not in WorkflowCopilotStreamResponseUpdate.model_fields


class TestNormalizeCopilotYamlTitleCoercion:
    def test_missing_top_level_title_is_coerced_to_empty(self) -> None:
        yaml_str = "workflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == ""

    def test_explicit_top_level_title_is_preserved(self) -> None:
        yaml_str = "title: My Workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == "My Workflow"


class TestNormalizeCopilotYamlBlockTypeAliases:
    def test_browser_task_alias_is_canonicalized_to_navigation(self) -> None:
        yaml_str = (
            "title: Browser Task Alias\n"
            "workflow_definition:\n"
            "  parameters: []\n"
            "  blocks:\n"
            "    - block_type: browser_task\n"
            "      label: open_picker\n"
            "      navigation_goal: Click the picker.\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        assert request.workflow_definition.blocks[0].block_type == "navigation"

    def test_nested_browser_task_alias_is_canonicalized_to_navigation(self) -> None:
        yaml_str = (
            "title: Nested Browser Task Alias\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "    - parameter_type: workflow\n"
            "      key: items\n"
            "      workflow_parameter_type: json\n"
            "      default_value: '[]'\n"
            "  blocks:\n"
            "    - block_type: for_loop\n"
            "      label: loop_items\n"
            "      loop_over_parameter_key: items\n"
            "      loop_blocks:\n"
            "        - block_type: browser_task\n"
            "          label: click_item\n"
            "          navigation_goal: Click the current item.\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        loop_block = request.workflow_definition.blocks[0]
        assert loop_block.loop_blocks[0].block_type == "navigation"


class TestNormalizeCopilotYamlProxyLocation:
    def test_missing_proxy_location_is_preserved(self) -> None:
        yaml_str = "title: Proxy Workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location is None

    def test_explicit_null_proxy_location_is_preserved(self) -> None:
        yaml_str = "title: Proxy Workflow\nproxy_location: null\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location is None

    @pytest.mark.parametrize(
        ("raw_value", "expected"),
        [
            ("US", ProxyLocation.RESIDENTIAL),
            ("USA", ProxyLocation.RESIDENTIAL),
            ("RESIDENTIAL_US", ProxyLocation.RESIDENTIAL),
            ("UK", ProxyLocation.RESIDENTIAL_GB),
            ("GB", ProxyLocation.RESIDENTIAL_GB),
            ("CA", ProxyLocation.RESIDENTIAL_CA),
            ("US_CA", ProxyLocation.US_CA),
            ("us-ny", ProxyLocation.US_NY),
        ],
    )
    def test_known_proxy_location_shorthands_are_canonicalized(self, raw_value: str, expected: ProxyLocation) -> None:
        yaml_str = (
            f"title: Proxy Workflow\n"
            f"proxy_location: {raw_value}\n"
            f"workflow_definition:\n"
            f"  blocks: []\n"
            f"  parameters: []\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location == expected

    def test_unknown_proxy_location_still_fails_validation(self) -> None:
        yaml_str = "title: Proxy Workflow\nproxy_location: MARS\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        with pytest.raises(ValidationError):
            _normalize_copilot_yaml(yaml_str)


_PROPOSED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
    """
)

_PERSISTED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
        - block_type: navigation
          label: do_thing
          navigation_goal: Click the primary action.
    """
)

_USER_MODIFIED_YAML = _PROPOSED_YAML + (
    "    - block_type: text_prompt\n      label: summarize_result\n      llm_key: x\n      prompt: ok\n"
)


_BLOCKLESS_EXPLICIT_YAML = "title: t\nworkflow_definition:\n  parameters: []\n  blocks: []\n"


class TestBlocklessSubmissionFallback:
    def test_none_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=None,
            )
            == _PROPOSED_YAML
        )

    def test_empty_string_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="",
            )
            == _PROPOSED_YAML
        )

    def test_whitespace_only_submission_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="   \n",
            )
            == _PROPOSED_YAML
        )

    def test_explicit_blocks_empty_submission_is_NOT_overwritten(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_populated_submission_preserves_user_edit(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_USER_MODIFIED_YAML,
            )
            is None
        )

    def test_no_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow=None, submitted_workflow_yaml="") is None

    def test_empty_dict_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow={}, submitted_workflow_yaml="") is None

    def test_non_string_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": None},
                submitted_workflow_yaml="",
            )
            is None
        )

    def test_malformed_blockless_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                submitted_workflow_yaml="",
            )
            is None
        )


class TestPriorCopilotWorkflowYaml:
    def test_uses_proposal_when_present(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PROPOSED_YAML
        )

    def test_falls_back_to_persisted_when_no_proposal(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow=None,
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_proposal_has_no_copilot_yaml(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"some_other_field": "x"},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_copilot_yaml_is_blockless(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_returns_none_when_neither_has_blocks(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_returns_none_when_no_inputs(self) -> None:
        assert _prior_copilot_workflow_yaml(proposed_workflow=None, persisted_workflow_yaml=None) is None


class _FakeStream:
    def __init__(self, raise_on_send: BaseException | None = None) -> None:
        self.sent: list[Any] = []
        self._raise_on_send = raise_on_send

    async def send(self, message: Any) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(message)


@pytest.mark.asyncio
async def test_ensure_terminal_frame_noop_when_already_emitted() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=True)  # type: ignore[arg-type]
    assert stream.sent == []


@pytest.mark.asyncio
async def test_ensure_terminal_frame_sends_fallback_error_when_missing() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]
    assert len(stream.sent) == 1
    frame = stream.sent[0]
    assert getattr(frame, "error", "").startswith("The assistant didn't finish")


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_exception() -> None:
    stream = _FakeStream(raise_on_send=RuntimeError("client already gone"))
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_cancellation() -> None:
    stream = _FakeStream(raise_on_send=asyncio.CancelledError())
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]


def _persisted_message(narrative_payload: dict[str, Any]) -> WorkflowCopilotChatMessage:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return WorkflowCopilotChatMessage(
        workflow_copilot_chat_message_id="wccm_1",
        workflow_copilot_chat_id="wcc_1",
        sender=WorkflowCopilotChatSender.AI,
        content="reply",
        narrative_payload=narrative_payload,  # type: ignore[arg-type]
        created_at=now,
        modified_at=now,
    )


def _non_error_narrative_payload() -> dict[str, Any]:
    """The shape the acting path builds — every key it actually supplies, and nothing else."""
    return {
        "turnId": "turn_1",
        "turnIndex": 0,
        "designStarted": True,
        "designEnded": True,
        "draft": None,
        "blocks": [],
        "terminal": "done",
        "terminalMessage": None,
        "narrativeSummary": "answered",
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }


def test_non_error_narrative_payload_survives_persistence_validation() -> None:
    """Every required TurnNarrativePayload key must have a live supplier.

    A required key whose only supplier was deleted passes type checking and every test that
    stubs persistence, then raises on both write and read at the Pydantic boundary, halting
    every turn. Grade the real boundary, not a stub.
    """
    message = _persisted_message(_non_error_narrative_payload())

    assert message.narrative_payload is not None
    assert message.narrative_payload["turnId"] == "turn_1"


def test_narrative_payload_tolerates_keys_persisted_before_the_field_was_removed() -> None:
    legacy = _non_error_narrative_payload() | {"mode": "build"}

    message = _persisted_message(legacy)

    assert message.narrative_payload is not None
    assert "mode" not in message.narrative_payload


def _credential_bound_workflow(credential_id: str) -> Any:
    """A saved workflow row whose login block binds ``credential_id``."""
    parameter = WorkflowParameter(
        parameter_type="workflow",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        key="login_credential",
        workflow_parameter_id="wp_1",
        workflow_id="w_1",
        default_value=credential_id,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )
    return Workflow(
        workflow_id="w_1",
        organization_id="o_1",
        title="saved",
        workflow_permanent_id="wpid_1",
        version=1,
        proxy_location=ProxyLocation.NONE,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            parameters=[parameter],
            blocks=[
                {
                    "label": "login",
                    "block_type": "login",
                    "url": "https://example.com/login",
                    "parameter_keys": ["login_credential"],
                    "output_parameter": OutputParameter(
                        output_parameter_id="op_1",
                        key="login_output",
                        workflow_id="w_1",
                        created_at=datetime.now(timezone.utc),
                        modified_at=datetime.now(timezone.utc),
                    ),
                }
            ],
        ),
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def test_run_grant_yaml_carries_the_saved_rows_credential() -> None:
    grant_yaml = _run_grant_workflow_yaml(_credential_bound_workflow("cred_saved"))

    assert grant_yaml is not None
    assert workflow_credential_ids(grant_yaml) == {"cred_saved"}


def test_run_grant_yaml_ignores_a_binding_that_exists_only_on_the_submitted_canvas() -> None:
    """The authority boundary: the grant reads the workflow row, never the submission.

    A copilot proposal sits on the canvas until the user accepts it, so the next turn resubmits
    it as a non-empty ``workflow_yaml``. If that ever reached the grant, a binding the model
    staged would authorize its own run.
    """
    saved_row = _credential_bound_workflow("cred_saved")
    # What the frontend would submit next turn: the canvas, still showing a staged proposal.
    submitted_canvas_yaml = _run_grant_workflow_yaml(_credential_bound_workflow("cred_staged_by_model"))
    assert submitted_canvas_yaml is not None
    assert workflow_credential_ids(submitted_canvas_yaml) == {"cred_staged_by_model"}

    grant_yaml = _run_grant_workflow_yaml(saved_row)

    assert grant_yaml is not None
    assert workflow_credential_ids(grant_yaml) == {"cred_saved"}


def test_run_grant_yaml_is_none_when_the_row_has_no_blocks() -> None:
    assert _run_grant_workflow_yaml(None) is None


def _timed_out_ctx(*, workflow_yaml: str | None, last_test_ok: bool | None) -> CopilotContext:
    workflow = (
        Workflow(
            workflow_id="wf_staged",
            organization_id="org-1",
            title="Staged draft",
            workflow_permanent_id="wfp-1",
            version=1,
            proxy_location=ProxyLocation.NONE,
            is_saved_task=False,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
            created_at=datetime.now(timezone.utc),
            modified_at=datetime.now(timezone.utc),
        )
        if workflow_yaml is not None
        else None
    )
    return make_copilot_ctx(
        last_workflow=workflow,
        last_workflow_yaml=workflow_yaml,
        staged_workflow=workflow,
        staged_workflow_yaml=workflow_yaml,
        has_staged_proposal=workflow is not None,
        last_test_ok=last_test_ok,
        copilot_total_timeout_exceeded=True,
        test_after_update_done=last_test_ok is not None,
    )


class TestTimedOutFailedTestDraftIsNotAutoApplied:
    def test_timeout_failed_test_result_offers_review_instead_of_committing(self) -> None:
        ctx = _timed_out_ctx(workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_workflow
        assert result.proposal_disposition == "review_untested"
        assert _effective_auto_accept(True, result) is False
        assert _should_commit_staged_workflow(True, result) is False

    def test_empty_timeout_result_does_not_commit_a_staged_workflow(self) -> None:
        ctx = _timed_out_ctx(workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.proposal_disposition == "no_proposal"
        assert _should_commit_staged_workflow(True, result) is False
