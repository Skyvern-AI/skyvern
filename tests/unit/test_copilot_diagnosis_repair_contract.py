from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import runtime_authoring_repair
from skyvern.forge.sdk.copilot.agent import (
    _build_dynamic_system_prompt,
    _build_user_context,
    _code_authoring_repair_context_prompt,
    _prior_run_debug_text,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestEvidencePacket,
    RecordedBuildTestOutcome,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.challenge_evidence import (
    CHALLENGE_KIND_KEY,
    ChallengeKind,
)
from skyvern.forge.sdk.copilot.completion_output_grounding import page_evidence_prose_text
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.composition_evidence import (
    has_bounded_page_schema,
    merge_visual_composition_evidence,
    model_visible_composition_evidence,
    parse_composition_html,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    RepairNextAction,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.enforcement import latest_diagnosis_contract_satisfies_goal
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import (
    RecordedRunOutcome,
)
from skyvern.forge.sdk.copilot.runtime import OriginRunRedactionRegistry
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    OBSTRUCTION_SUMMARY_MAX_CHARS,
    finalize_runtime_authoring_repair_context_from_page_observation,
    inject_runtime_authoring_repair_context,
    post_run_inspection_cleanly_matches,
    record_pending_runtime_authoring_repair_context,
)
from skyvern.forge.sdk.copilot.tools import composition_capture as composition_capture_module
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.composition_capture import store_post_run_page_evidence
from skyvern.forge.sdk.copilot.tools.scouting import _mark_post_run_page_observed
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockType
from tests.unit.copilot_test_helpers import make_stub_html_artifact


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="Fix the workflow with password=hunter2",
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
    )


def _runtime_output_dependency_yaml(*, available: bool = False) -> str:
    producer_label = "create_or_verify_resource" if available else "create_resource"
    return f"""
workflow_definition:
  blocks:
  - block_type: code
    label: {producer_label}
    code: |
      return {{"ok": True}}
  - block_type: code
    label: read_resource_table
    parameter_keys: [create_or_verify_resource_output]
    code: |
      resource = create_or_verify_resource_output["id"]
      return {{"resource": resource}}
"""


def _runtime_declared_output_named_input_yaml() -> str:
    return """
workflow_definition:
  parameters:
  - key: create_or_verify_resource_output
    parameter_type: workflow
    workflow_parameter_type: string
  blocks:
  - block_type: code
    label: read_resource_table
    parameter_keys: [create_or_verify_resource_output]
    code: |
      resource = create_or_verify_resource_output["id"]
      return {"resource": resource}
"""


def _runtime_declared_non_string_output_named_input_yaml() -> str:
    return """
workflow_definition:
  parameters:
  - key: create_or_verify_resource_output
    parameter_type: workflow
    workflow_parameter_type: number
  blocks:
  - block_type: code
    label: read_resource_table
    parameter_keys: [create_or_verify_resource_output]
    code: |
      resource = create_or_verify_resource_output["id"]
      return {"resource": resource}
"""


def _runtime_output_substring_only_yaml() -> str:
    return """
workflow_definition:
  blocks:
  - block_type: code
    label: read_resource_table
    code: |
      # foo_output appears in prose only.
      data = {"foo_output": {"id": "fixture"}}
      literal = "foo_output"
      return data["foo_output"]
"""


def _satisfied_completion_verification() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )


def _contradictory_completion_verification() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts")],
    )


def _structural_abstention_completion_verification() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="structurally_abstained")],
    )


def _clean_completed_result() -> dict[str, object]:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_clean",
            "overall_status": "completed",
            "frontier_start_label": "extract",
            "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
        },
    }


def _authoring_repair_result(repair_context: CodeAuthoringRepairContext) -> dict[str, object]:
    return {
        "ok": False,
        "error": "Workflow authoring repair needed.",
        "data": {
            "workflow_updated": False,
            "authoring_repair_context": repair_context.model_dump(mode="json"),
        },
    }


def test_contract_shapes_for_failed_suspicious_and_missing_credential_cases() -> None:
    failed = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The run ended before recording a trustworthy terminal status.",
            "data": {"workflow_run_id": "wr_1", "overall_status": "running", "failure_reason": "uncertain"},
        },
        ctx=_ctx(),
    )
    suspicious_ctx = _ctx()
    suspicious_ctx.last_test_suspicious_success = True
    suspicious = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_2",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
            },
        },
        ctx=suspicious_ctx,
        workflow_updated=True,
    )
    missing = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Skipped test run: required credentials are not configured.",
            "data": {"workflow_updated": True, "skip_reason": "workflow_credential_inputs_unbound"},
        },
        ctx=_ctx(),
        workflow_updated=True,
    )

    assert (
        failed.diagnosis_result.suspected_failure_type,
        failed.repair_decision.next_action,
        failed.diagnosis_result.missing_context,
    ) == (DiagnosisFailureType.FAILED_RUN, RepairNextAction.REPAIR, ["block_results"])
    assert (
        suspicious.diagnosis_result.suspected_failure_type,
        suspicious.repair_decision.next_action,
        suspicious.repair_decision.target_blocks,
        suspicious.verification_result.user_goal_satisfied,
    ) == (DiagnosisFailureType.SUSPICIOUS_SUCCESS, RepairNextAction.REPAIR, ["extract"], False)
    assert (
        missing.diagnosis_result.suspected_failure_type,
        missing.repair_decision.next_action,
        missing.repair_decision.required_authority,
    ) == (DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT, RepairNextAction.ASK, [])


def test_paused_run_is_not_a_failure_and_is_not_claimed_as_success() -> None:
    paused = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The run is paused at a human_interaction block, waiting for a person.",
            "data": {
                "workflow_run_id": "wr_paused",
                "overall_status": "paused",
                "failure_reason": "The run is paused, waiting for a person to approve or reject it.",
                "control_signal": {"kind": "watchdog_paused"},
            },
        },
        ctx=_ctx(),
    )

    assert (
        paused.diagnosis_result.suspected_failure_type,
        paused.repair_decision.next_action,
        paused.verification_result.user_goal_satisfied,
    ) == (DiagnosisFailureType.NO_FAILURE, RepairNextAction.NO_CHANGE, False)


def test_pause_outranks_trusted_challenge_evidence() -> None:
    """A pause on a challenge-flagged page stays NO_FAILURE. Classifying it as a challenge blocker
    maps to STOP, which would have the copilot call the site unfixable about a run that has emailed
    a person to solve that exact challenge. ``_next_action`` already excludes NO_FAILURE from its
    anti-bot latch; this keeps ``_failure_type`` agreeing with it."""
    paused_on_challenge = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The run is paused at a human_interaction block, waiting for a person.",
            "data": {
                "workflow_run_id": "wr_paused_challenge",
                "overall_status": "paused",
                "control_signal": {"kind": "watchdog_paused"},
                "failure_categories": [
                    {"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state", "confidence_float": 1.0}
                ],
            },
        },
        ctx=_ctx(),
    )

    assert (
        paused_on_challenge.diagnosis_result.suspected_failure_type,
        paused_on_challenge.repair_decision.next_action,
    ) == (DiagnosisFailureType.NO_FAILURE, RepairNextAction.NO_CHANGE)


def test_non_paused_watchdog_exit_still_classifies_as_a_failed_run() -> None:
    ceiling = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The run exceeded the 600s absolute ceiling while still showing progress.",
            "data": {
                "workflow_run_id": "wr_ceiling",
                "overall_status": "running",
                "failure_reason": "ceiling",
                "control_signal": {"kind": "watchdog_ceiling"},
            },
        },
        ctx=_ctx(),
    )

    assert ceiling.diagnosis_result.suspected_failure_type == DiagnosisFailureType.FAILED_RUN
    assert ceiling.repair_decision.next_action == RepairNextAction.REPAIR


def test_authoring_repair_contexts_have_distinct_structural_root_cause_signatures() -> None:
    ambiguous = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='View / Download']",
    )
    runtime = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="runtime_block_failure",
        unresolved_names=["row_text", "confirmation_number"],
    )
    runtime_reordered = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="runtime_block_failure",
        unresolved_names=["confirmation_number", "row_text"],
    )
    synthesized_binding = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="synthesized_parameter_binding_ambiguous",
        unresolved_names=["enter_confirmation"],
        parameter_keys=["enter_confirmation"],
        available_parameter_keys=["confirmation_number"],
        binding_candidates=["enter_confirmation", "confirmation_number"],
    )

    ambiguous_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(ambiguous),
        ctx=_ctx(),
    )
    runtime_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(runtime),
        ctx=_ctx(),
    )
    runtime_reordered_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(runtime_reordered),
        ctx=_ctx(),
    )
    synthesized_binding_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(synthesized_binding),
        ctx=_ctx(),
    )

    ambiguous_signature = ambiguous_contract.to_trace_data()["root_cause_signature"]
    runtime_signature = runtime_contract.to_trace_data()["root_cause_signature"]
    synthesized_binding_signature = synthesized_binding_contract.to_trace_data()["root_cause_signature"]
    assert ambiguous_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert runtime_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert synthesized_binding_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert synthesized_binding_contract.repair_decision.target_blocks == ["retrieve_document_link"]
    assert ambiguous_signature is not None
    assert runtime_signature is not None
    assert synthesized_binding_signature is not None
    assert ambiguous_signature != runtime_signature
    assert synthesized_binding_signature not in {ambiguous_signature, runtime_signature}
    assert (
        synthesized_binding_contract.diagnosis_result.root_cause_identity.error_class
        == "code_authoring_synthesized_parameter_binding_ambiguous"
    )
    assert runtime_reordered_contract.to_trace_data()["root_cause_signature"] == runtime_signature


def test_missing_required_output_key_repair_identity_uses_structural_context_only() -> None:
    repair_context = CodeAuthoringRepairContext(
        block_label="search_registry",
        reason_code="missing_required_output_key",
    )

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(repair_context),
        ctx=_ctx(),
    )

    expected_payload = {
        "version": "authoring_repair_context:v1",
        "reason_code": "missing_required_output_key",
        "block_label": "search_registry",
    }
    expected_signature = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.to_trace_data()["root_cause_signature"] == expected_signature
    assert contract.diagnosis_result.root_cause_identity.error_class == "code_authoring_missing_required_output_key"


def test_runtime_missing_output_dependency_identity_uses_key_and_available_contracts() -> None:
    base = CodeAuthoringRepairContext(
        block_label="read_resource_table",
        reason_code="runtime_missing_output_dependency",
        missing_output_key="create_resource_output",
        available_output_keys=["search_output", "verify_output"],
        current_block_parameter_keys=["create_resource_output"],
        output_dependency_failure_class="missing_prior_block_output",
    )
    reordered = base.model_copy(update={"available_output_keys": ["verify_output", "search_output"]})
    different_key = base.model_copy(update={"missing_output_key": "verify_resource_output"})
    different_available = base.model_copy(update={"available_output_keys": ["search_output"]})

    base_signature = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(base),
        ctx=_ctx(),
    ).to_trace_data()["root_cause_signature"]
    reordered_signature = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(reordered),
        ctx=_ctx(),
    ).to_trace_data()["root_cause_signature"]
    different_key_signature = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(different_key),
        ctx=_ctx(),
    ).to_trace_data()["root_cause_signature"]
    different_available_signature = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(different_available),
        ctx=_ctx(),
    ).to_trace_data()["root_cause_signature"]

    assert base_signature == reordered_signature
    assert different_key_signature != base_signature
    assert different_available_signature != base_signature


def test_runtime_authoring_repair_context_identity_includes_bounded_page_state() -> None:
    base = CodeAuthoringRepairContext(
        block_label="search_registry",
        reason_code="runtime_block_failure",
        runtime_failure_reason='Timeout waiting for locator("#results")',
        runtime_failure_class="timeout_waiting_for_selector",
        failed_block_status="failed",
        workflow_run_id="wr_failed",
        current_origin="https://example.test",
        current_url="https://example.test/search",
        current_title="Search results",
        page_evidence_source="inspect_page_for_composition",
        observed_after_workflow_run=True,
        page_form_summaries=["text input labeled Search"],
        page_result_summaries=["no results container is visible"],
        page_action_summaries=["button Search is disabled"],
    )
    changed_page = base.model_copy(update={"page_result_summaries": ["results table is visible"]})
    changed_location = base.model_copy(
        update={"current_url": "https://example.test/other", "current_title": "Other page"}
    )

    base_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(base),
        ctx=_ctx(),
    )
    changed_page_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(changed_page),
        ctx=_ctx(),
    )
    changed_location_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(changed_location),
        ctx=_ctx(),
    )

    assert base_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert (
        base_contract.to_trace_data()["root_cause_signature"]
        != changed_page_contract.to_trace_data()["root_cause_signature"]
    )
    assert (
        base_contract.to_trace_data()["root_cause_signature"]
        == changed_location_contract.to_trace_data()["root_cause_signature"]
    )
    assert base_contract.diagnosis_result.root_cause_identity.error_class == (
        "code_authoring_runtime_block_failure_timeout_waiting_for_selector"
    )


def test_failed_run_finalizes_runtime_authoring_repair_context_after_matching_page_observation() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER

    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "error": "Run failed.",
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "search_registry",
                        "status": "failed",
                        "failure_reason": 'Timeout waiting for locator("#results")',
                    }
                ],
            },
        },
    )
    pending_context = ctx.pending_code_authoring_runtime_repair_context
    assert isinstance(pending_context, CodeAuthoringRepairContext)
    assert pending_context.block_label == "search_registry"
    assert pending_context.workflow_run_id == "wr_failed"
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search?case=secret#result",
        "page_title": "Search results",
        "forms": [
            {
                "fields": [{"label": "Search", "selector": "#search"}],
                "submit_controls": [{"text": "Go", "selector": "button.search", "disabled": True}],
            }
        ],
        "result_containers": [{"selector": "#results", "text_excerpt": "No matching records"}],
        "navigation_targets": [{"text": "Next page", "selector": "a.next"}],
    }
    result = {
        "ok": False,
        "error": "Run failed.",
        "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"},
    }

    inject_runtime_authoring_repair_context(ctx, result)

    repair_context = ctx.last_code_authoring_repair_context
    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert result["data"]["authoring_repair_context"] == repair_context.model_dump(mode="json")
    assert repair_context.block_label == "search_registry"
    assert repair_context.runtime_failure_class is None
    assert repair_context.current_origin == "https://example.test"
    assert repair_context.current_url == "https://example.test/search"
    assert repair_context.current_title == "Search results"
    assert "current_url_present" not in CodeAuthoringRepairContext.model_fields
    assert "current_title_present" not in CodeAuthoringRepairContext.model_fields
    assert repair_context.page_evidence_source == "inspect_page_for_composition"
    assert repair_context.observed_after_workflow_run is True
    assert repair_context.page_form_summaries == ["Search", "Go disabled"]
    assert repair_context.page_result_summaries == ["No matching records"]
    assert repair_context.page_action_summaries == ["Next page"]
    assert "case=secret" not in repair_context.model_dump_json()


def test_post_run_observation_is_false_when_all_four_summary_collections_are_empty() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_result = _failed_run_result()
    run_execution_module._record_run_blocks_result(ctx, run_result)
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search?layout=cards",
        "page_title": "Search results",
        "forms": [],
        "result_containers": [],
        "navigation_targets": [],
        "challenge_controls": [],
        "observed_empty_page": True,
    }
    result = {
        "ok": False,
        "error": "Run failed.",
        "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"},
    }

    inject_runtime_authoring_repair_context(ctx, result)

    repair_context = ctx.last_code_authoring_repair_context
    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.current_url == "https://example.test/search"
    assert repair_context.current_title == "Search results"
    assert repair_context.page_form_summaries == []
    assert repair_context.page_result_summaries == []
    assert repair_context.page_action_summaries == []
    assert repair_context.page_challenge_summaries == []
    assert repair_context.observed_after_workflow_run is False


def _standalone_control_page_evidence() -> dict[str, object]:
    return {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "page_title": "Statements",
        "navigation_targets": [{"text": "Next page", "selector": "a.next"}],
        "clickable_controls": [
            {
                "text": "Continue to statements",
                "selector": "#continue-btn-x9",
                "tag": "button",
                "disabled": True,
                "html": '<button id="continue-btn-x9">Continue to statements</button>',
            },
            {"text": "Filters", "selector": "#filters-toggle-q7", "tag": "button", "expanded": False},
            {"text": "Next page", "selector": "a.next", "tag": "a", "expanded": False},
            {"selector": "#icon-only-z3", "tag": "button"},
        ],
    }


def test_page_action_summaries_carry_navigation_targets_and_standalone_clickable_controls() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = _standalone_control_page_evidence()

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries == [
        "Continue to statements disabled",
        "Filters collapsed",
        "Next page collapsed",
    ]
    assert repair_context.observed_after_workflow_run is True
    rendered = repair_context.model_dump_json()
    for leaked in ("#continue-btn-x9", "#filters-toggle-q7", "#icon-only-z3", "a.next", "<button"):
        assert leaked not in rendered


def test_page_action_summaries_keep_a_control_behind_text_less_ones() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "navigation_targets": [{"text": f"Section {index}", "selector": f"a.s{index}"} for index in range(6)],
        "clickable_controls": [{"selector": f"#icon-{index}", "tag": "button"} for index in range(5)]
        + [{"text": "Continue to statements", "selector": "#continue", "tag": "button"}],
    }

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries[0] == "Continue to statements"
    assert repair_context.page_action_summaries[1:3] == ["Section 0", "Section 1"]


def test_page_action_summaries_skip_text_less_navigation_targets() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "navigation_targets": [{"selector": f"a.icon{index}"} for index in range(5)]
        + [{"text": "Next page", "selector": "a.next"}],
        "challenge_controls": [{"selector": f"#c{index}"} for index in range(5)]
        + [{"text": "Verify you are human", "selector": "#verify"}],
    }

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries == ["Next page"]
    assert repair_context.page_challenge_summaries == []


def test_page_action_summaries_keep_two_controls_on_a_navigation_heavy_page() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "navigation_targets": [{"text": f"Section {index}", "selector": f"a.s{index}"} for index in range(6)],
        "clickable_controls": [
            {"text": "Continue to statements", "selector": "#continue", "tag": "button"},
            {"text": "Download all", "selector": "#download", "tag": "button"},
        ],
    }

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries == [
        "Continue to statements",
        "Download all",
        "Section 0",
        "Section 1",
        "Section 2",
    ]


def test_page_action_summaries_keep_an_element_listed_in_both_collections_when_the_cap_is_full() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "navigation_targets": [{"text": f"Section {index}", "selector": f"a.s{index}"} for index in range(4)]
        + [{"text": "Continue to statements", "selector": "a.continue"}],
        "clickable_controls": [{"text": "Continue to statements", "selector": "#continue", "tag": "button"}]
        + [{"text": f"Filter {index}", "selector": f"#f{index}", "tag": "button"} for index in range(5)],
    }

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries.count("Continue to statements") == 1
    assert repair_context.page_action_summaries[0] == "Continue to statements"


def test_runtime_authoring_repair_admits_a_page_whose_only_content_is_a_clickable_control() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "clickable_controls": [{"text": "Continue to statements", "selector": "#continue", "tag": "button"}],
    }

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.page_action_summaries == ["Continue to statements"]
    assert repair_context.observed_after_workflow_run is True
    assert "page_actions: Continue to statements" in _code_authoring_repair_context_prompt(ctx)


def _text_less_control_only_page_evidence() -> dict[str, object]:
    return {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/statements",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "clickable_controls": [{"selector": f"#icon-{index}", "tag": "button"} for index in range(3)],
    }


def test_a_text_less_control_only_packet_is_not_admissible_repair_evidence() -> None:
    assert post_run_inspection_cleanly_matches(_text_less_control_only_page_evidence(), "wr_failed") is False


def test_a_text_less_control_only_packet_finalizes_no_repair_context() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    ctx.composition_page_evidence = _text_less_control_only_page_evidence()

    assert finalize_runtime_authoring_repair_context_from_page_observation(ctx) is None


def test_failed_run_injects_pending_runtime_authoring_context_before_page_observation() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_result = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "search_registry",
                    "status": "failed",
                    "failure_reason": 'Locator.wait_for: strict mode violation: get_by_text("Order Details")',
                }
            ],
        },
    }

    run_execution_module._record_run_blocks_result(ctx, run_result)
    inject_runtime_authoring_repair_context(ctx, run_result)

    raw_context = run_result["data"]["authoring_repair_context"]
    repair_context = CodeAuthoringRepairContext.model_validate(raw_context)
    assert repair_context.reason_code == "runtime_block_failure"
    assert repair_context.block_label == "search_registry"
    assert repair_context.workflow_run_id == "wr_failed"
    assert repair_context.runtime_failure_class is None
    assert repair_context.observed_after_workflow_run is False

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=run_result,
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.diagnosis_result.root_cause_identity.primary_category == "CODE_AUTHORING_REPAIR"
    assert contract.diagnosis_result.root_cause_identity.error_class.startswith("code_authoring_runtime_block_failure")
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.repair_decision.target_blocks == ["search_registry"]


def test_runtime_key_error_for_missing_prior_output_records_typed_authoring_context() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.workflow_yaml = _runtime_output_dependency_yaml(available=False)
    result = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_missing_output",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "read_resource_table",
                    "status": "failed",
                    "failure_reason": "KeyError: 'create_or_verify_resource_output'",
                }
            ],
        },
    }

    record_pending_runtime_authoring_repair_context(ctx, result)
    inject_runtime_authoring_repair_context(ctx, result)

    repair_context = ctx.last_code_authoring_repair_context
    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.reason_code == "runtime_missing_output_dependency"
    assert repair_context.block_label == "read_resource_table"
    assert repair_context.workflow_run_id == "wr_missing_output"
    assert repair_context.output_dependency_failure_class == "missing_prior_block_output"
    assert repair_context.missing_output_key == "create_or_verify_resource_output"
    assert repair_context.available_output_keys == ["create_resource_output"]
    assert repair_context.current_block_parameter_keys == ["create_or_verify_resource_output"]
    assert result["data"]["authoring_repair_context"] == repair_context.model_dump(mode="json")


@pytest.mark.parametrize(
    ("yaml_builder", "run_id", "keyerror_name"),
    [
        pytest.param(
            lambda: _runtime_output_dependency_yaml(available=True),
            "wr_available_output",
            "create_or_verify_resource_output",
            id="available_prior_output",
        ),
        pytest.param(
            _runtime_declared_output_named_input_yaml,
            "wr_declared_input",
            "create_or_verify_resource_output",
            id="declared_workflow_input",
        ),
        pytest.param(
            _runtime_declared_non_string_output_named_input_yaml,
            "wr_declared_number_input",
            "create_or_verify_resource_output",
            id="declared_non_string_workflow_input",
        ),
        pytest.param(
            _runtime_output_substring_only_yaml,
            "wr_substring_only",
            "foo_output",
            id="code_substring_only",
        ),
    ],
)
def test_runtime_key_error_boundary_keeps_generic_runtime_repair(
    yaml_builder: Callable[[], str], run_id: str, keyerror_name: str
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.workflow_yaml = yaml_builder()
    result = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "read_resource_table",
                    "status": "failed",
                    "failure_reason": f"KeyError: '{keyerror_name}'",
                }
            ],
        },
    }

    record_pending_runtime_authoring_repair_context(ctx, result)

    pending_context = ctx.pending_code_authoring_runtime_repair_context
    assert isinstance(pending_context, CodeAuthoringRepairContext)
    assert pending_context.reason_code == "runtime_block_failure"
    assert pending_context.missing_output_key is None


def _injected_repair_log(events: list[dict[str, object]]) -> dict[str, object]:
    matches = [event for event in events if event.get("event") == "Injected runtime authoring repair context"]
    assert len(matches) == 1
    return matches[0]


def test_runtime_authoring_repair_injection_logs_observed_flip() -> None:
    grounded_ctx = _ctx()
    grounded_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(
        grounded_ctx,
        {
            "ok": False,
            "error": "Run failed.",
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "search_registry",
                        "status": "failed",
                        "failure_reason": 'Timeout waiting for locator("#results")',
                    }
                ],
            },
        },
    )
    grounded_ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "page_title": "Search results",
        "forms": [{"fields": [{"label": "Search", "selector": "#search"}]}],
        "result_containers": [{"selector": "#results", "text_excerpt": "No matching records"}],
        "navigation_targets": [{"text": "Next page", "selector": "a.next"}],
    }
    grounded_result = {
        "ok": False,
        "error": "Run failed.",
        "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"},
    }
    with capture_logs() as grounded_events:
        inject_runtime_authoring_repair_context(grounded_ctx, grounded_result)
    grounded_log = _injected_repair_log(grounded_events)
    assert grounded_log["observed_after_workflow_run"] is True
    assert grounded_log["workflow_run_id"] == "wr_failed"
    assert grounded_log["page_form_summary_count"] > 0
    assert grounded_log["page_result_summary_count"] > 0
    assert grounded_log["page_action_summary_count"] > 0

    fallback_ctx = _ctx()
    fallback_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    fallback_result = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "search_registry",
                    "status": "failed",
                    "failure_reason": 'Locator.wait_for: strict mode violation: get_by_text("Order Details")',
                }
            ],
        },
    }
    run_execution_module._record_run_blocks_result(fallback_ctx, fallback_result)
    with capture_logs() as fallback_events:
        inject_runtime_authoring_repair_context(fallback_ctx, fallback_result)
    fallback_log = _injected_repair_log(fallback_events)
    assert fallback_log["observed_after_workflow_run"] is False
    assert fallback_log["workflow_run_id"] == "wr_failed"
    assert fallback_log["page_form_summary_count"] == 0
    assert fallback_log["page_result_summary_count"] == 0
    assert fallback_log["page_action_summary_count"] == 0


def test_runtime_authoring_repair_context_suppressed_for_stale_or_successful_runs() -> None:
    stale_ctx = _ctx()
    run_execution_module._record_run_blocks_result(
        stale_ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "blocks": [{"label": "search_registry", "status": "failed", "failure_reason": "Button missing"}],
            },
        },
    )
    stale_ctx.composition_page_evidence = {
        "workflow_run_id": "wr_other",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "forms": [{"label": "Search", "selector": "#search"}],
    }
    stale_result = {"ok": False, "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"}}

    inject_runtime_authoring_repair_context(stale_ctx, stale_result)

    assert "authoring_repair_context" not in stale_result["data"]
    assert stale_ctx.last_code_authoring_repair_context is None

    success_ctx = _ctx()
    success_ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="search_registry",
        reason_code="runtime_block_failure",
    )
    run_execution_module._record_run_blocks_result(success_ctx, _clean_completed_result())

    assert success_ctx.last_code_authoring_repair_context is None


def test_runtime_authoring_repair_context_does_not_override_terminal_stop() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_terminal",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "search_registry",
                        "status": "failed",
                        "failure_reason": "Browser session not found.",
                    }
                ],
            },
        },
    )
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_terminal",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "forms": [{"label": "Search", "selector": "#search"}],
    }
    result = {
        "ok": False,
        "error": "Browser session not found.",
        "data": {
            "workflow_run_id": "wr_terminal",
            "overall_status": "failed",
            "failure_categories": [{"category": "UNRECOVERABLE_TOOL_ERROR"}],
        },
    }

    contract = run_execution_module._record_diagnosis_repair_contract(
        ctx,
        source_tool="update_and_run_blocks",
        result=result,
        workflow_updated=True,
    )

    assert "authoring_repair_context" not in result["data"]
    assert ctx.last_code_authoring_repair_context is None
    assert contract.repair_decision.next_action == RepairNextAction.STOP


def test_runtime_authoring_repair_context_requires_bounded_inspect_evidence() -> None:
    for evidence_update in (
        {"source_tool": "evaluate", "forms": [{"label": "Search", "selector": "#search"}]},
        {"source_tool": "inspect_page_for_composition", "forms": []},
    ):
        ctx = _ctx()
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        run_execution_module._record_run_blocks_result(
            ctx,
            {
                "ok": False,
                "data": {
                    "workflow_run_id": "wr_failed",
                    "overall_status": "failed",
                    "blocks": [{"label": "search_registry", "status": "failed", "failure_reason": "Button missing"}],
                },
            },
        )
        ctx.composition_page_evidence = {
            "workflow_run_id": "wr_failed",
            "observed_after_workflow_run": True,
            "current_url": "https://example.test/search",
            **evidence_update,
        }

        assert finalize_runtime_authoring_repair_context_from_page_observation(ctx) is None
        assert ctx.last_code_authoring_repair_context is None


def test_runtime_authoring_repair_context_suppressed_for_terminal_page_evidence() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "blocks": [{"label": "search_registry", "status": "failed", "failure_reason": "Search disabled"}],
            },
        },
    )
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "requires_human_verification": True,
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }

    assert finalize_runtime_authoring_repair_context_from_page_observation(ctx) is None
    assert ctx.pending_code_authoring_runtime_repair_context is None
    assert ctx.last_code_authoring_repair_context is None


def test_runtime_authoring_repair_context_ignores_policy_verdict_but_respects_state_stop() -> None:
    ask_ctx = _ctx()
    ask_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ask_ctx.request_policy = RequestPolicy(
        user_response_policy="ask_clarification",
        allow_update_workflow=False,
        allow_run_blocks=False,
    )
    run_execution_module._record_run_blocks_result(
        ask_ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_ask",
                "overall_status": "failed",
                "blocks": [{"label": "search_registry", "status": "failed", "failure_reason": "Button missing"}],
            },
        },
    )
    ask_ctx.composition_page_evidence = {
        "workflow_run_id": "wr_ask",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "forms": [{"label": "Search", "selector": "#search"}],
    }
    ask_result = {"ok": False, "data": {"workflow_run_id": "wr_ask", "overall_status": "failed"}}

    inject_runtime_authoring_repair_context(ask_ctx, ask_result)

    assert "authoring_repair_context" in ask_result["data"]
    assert ask_ctx.last_code_authoring_repair_context is not None

    stop_ctx = _ctx()
    stop_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    stop_ctx.last_test_non_retriable_nav_error = "net::ERR_NAME_NOT_RESOLVED"
    run_execution_module._record_run_blocks_result(
        stop_ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_stop",
                "overall_status": "failed",
                "blocks": [{"label": "open", "status": "failed", "failure_reason": "net::ERR_NAME_NOT_RESOLVED"}],
            },
        },
    )
    stop_ctx.composition_page_evidence = {
        "workflow_run_id": "wr_stop",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/search",
        "forms": [{"label": "Search", "selector": "#search"}],
    }
    stop_result = {
        "ok": False,
        "error": "Failed to navigate to url https://bad.example.",
        "data": {"workflow_run_id": "wr_stop", "overall_status": "failed"},
    }

    inject_runtime_authoring_repair_context(stop_ctx, stop_result)

    assert "authoring_repair_context" not in stop_result["data"]
    assert stop_ctx.last_code_authoring_repair_context is None


def test_direct_runtime_authoring_repair_finalization_suppresses_stop_class_state() -> None:
    cases = [
        {
            "failure_reason": "Failed to navigate to url https://bad.example.",
            "ctx_attr": ("last_test_non_retriable_nav_error", "net::ERR_NAME_NOT_RESOLVED"),
        },
        {
            "failure_reason": "Browser session not found while taking screenshot.",
            "ctx_attr": None,
        },
        {
            "failure_reason": "Skipped test run: required credentials are not configured.",
            "ctx_attr": None,
        },
    ]
    for case in cases:
        ctx = _ctx()
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        run_execution_module._record_run_blocks_result(
            ctx,
            {
                "ok": False,
                "data": {
                    "workflow_run_id": "wr_stop",
                    "overall_status": "failed",
                    "blocks": [
                        {
                            "label": "search_registry",
                            "status": "failed",
                            "failure_reason": case["failure_reason"],
                        }
                    ],
                },
            },
        )
        ctx_attr = case["ctx_attr"]
        if ctx_attr is not None:
            setattr(ctx, ctx_attr[0], ctx_attr[1])
        ctx.composition_page_evidence = {
            "workflow_run_id": "wr_stop",
            "observed_after_workflow_run": True,
            "source_tool": "inspect_page_for_composition",
            "current_url": "https://example.test/search",
            "forms": [{"label": "Search", "selector": "#search"}],
        }

        assert finalize_runtime_authoring_repair_context_from_page_observation(ctx) is None
        assert ctx.pending_code_authoring_runtime_repair_context is None
        assert ctx.last_code_authoring_repair_context is None


def test_new_pending_runtime_failure_clears_prior_finalized_runtime_context() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="old_search",
        reason_code="runtime_block_failure",
        runtime_failure_reason="Old failure",
        workflow_run_id="wr_old",
        current_origin="https://old.example",
        observed_after_workflow_run=True,
        page_form_summaries=["Old #search"],
    )

    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_new",
                "overall_status": "failed",
                "blocks": [{"label": "new_search", "status": "failed", "failure_reason": "New button missing"}],
            },
        },
    )

    pending_context = ctx.pending_code_authoring_runtime_repair_context
    assert isinstance(pending_context, CodeAuthoringRepairContext)
    assert pending_context.block_label == "new_search"
    assert pending_context.workflow_run_id == "wr_new"
    assert ctx.last_code_authoring_repair_context is None


def test_runtime_authoring_repair_context_sanitizes_failure_and_page_summaries() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_secret",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "search_registry",
                        "status": "failed",
                        "failure_reason": "Timeout after entering password=hunter2",
                    }
                ],
            },
        },
    )
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_secret",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://user:secret@example.test/search/" + "p" * 200 + "?password=hunter2#token",
        "page_title": "Search\n" + "x" * 200,
        "forms": [
            {
                "fields": [{"label": "Password password=hunter2", "selector": "#password"}],
                "submit_controls": [{"text": "Submit", "selector": "#submit"}],
            }
        ],
        "result_containers": [{"selector": "#result", "text_excerpt": "token=secret-token"}],
    }
    result = {"ok": False, "data": {"workflow_run_id": "wr_secret", "overall_status": "failed"}}

    inject_runtime_authoring_repair_context(ctx, result)

    repair_context = ctx.last_code_authoring_repair_context
    assert isinstance(repair_context, CodeAuthoringRepairContext)
    dumped = repair_context.model_dump_json()
    assert "hunter2" not in dumped
    assert "secret-token" not in dumped
    assert "user:secret" not in dumped
    assert "password=hunter2" not in dumped
    assert repair_context.current_origin == "https://example.test"
    assert repair_context.current_url is not None
    assert repair_context.current_url.startswith("https://example.test/search/")
    assert len(repair_context.current_url) == 160
    assert repair_context.current_title is not None
    assert "\n" not in repair_context.current_title
    assert len(repair_context.current_title) == 160


@pytest.mark.parametrize(
    "completion_verification",
    [
        CompletionVerificationResult(status="unavailable"),
        CompletionVerificationResult(status="evaluated", criterion_ids=[]),
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c0"],
            verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts")],
        ),
    ],
)
def test_clean_run_ignores_interactive_completion_verification(
    completion_verification: CompletionVerificationResult,
) -> None:
    ctx = _ctx()
    ctx.completion_verification_result = completion_verification

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert contract.verification_result.remaining_blocker is None


def test_clean_run_with_structural_abstention_completion_verification_does_not_repair() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = _structural_abstention_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )
    ctx.latest_diagnosis_repair_contract = contract

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.repair_decision.next_action != RepairNextAction.REPAIR
    assert contract.repair_decision.completion_check == "Current run already satisfies the goal."
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert latest_diagnosis_contract_satisfies_goal(ctx) is True


def test_clean_run_with_satisfied_completion_verification_has_no_repair_or_blocker() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = _satisfied_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert contract.verification_result.remaining_blocker is None


def test_committed_same_run_outcome_satisfies_diagnosis_after_later_contradiction() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_clean"
    ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_clean")
    ctx.completion_verification_result = _contradictory_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )
    ctx.latest_diagnosis_repair_contract = contract

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert latest_diagnosis_contract_satisfies_goal(ctx) is True


def test_first_pass_completion_contradiction_cannot_overturn_clean_run() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = _contradictory_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )
    ctx.latest_diagnosis_repair_contract = contract

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert latest_diagnosis_contract_satisfies_goal(ctx) is True


def test_failed_run_is_not_rescued_by_completion_verification() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = _satisfied_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Completion verification confirmed the requested outcome despite partial run status.",
            "data": {
                "workflow_run_id": "wr_partial_verified",
                "overall_status": "failed",
                "frontier_start_label": "extract",
                "failure_categories": [{"category": "DATA_EXTRACTION_FAILURE"}],
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "EXTRACTION",
                        "status": "failed",
                        "failure_reason": "Extraction result was empty before verification.",
                    }
                ],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert contract.verification_result.remaining_blocker is not None
    assert contract.diagnosis_result.missing_context == []


@pytest.mark.parametrize(
    "completion_verification",
    [
        None,
        CompletionVerificationResult(status="unavailable"),
        CompletionVerificationResult(status="evaluated", criterion_ids=[]),
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c0"],
            verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts")],
        ),
    ],
)
def test_unverified_completion_evidence_does_not_suppress_suspicious_success(
    completion_verification: CompletionVerificationResult | None,
) -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True
    ctx.completion_verification_result = completion_verification

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_unverified",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.verification_result.user_goal_satisfied is False


def test_degraded_path_without_terminal_state_still_routes_repair() -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True
    ctx.last_run_blocks_workflow_run_id = "wr_unverified"
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["__copilot_fallback_floor__run", "requested_output"],
        verdicts=[
            CriterionVerdict(
                criterion_id="__copilot_fallback_floor__run",
                state="unsatisfied",
                reason_code="no_evidence",
            ),
            CriterionVerdict(
                criterion_id="requested_output",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:extract.document_name",
                output_path="output.document_name",
                grounding_mode="missing",
                evidence_source="runtime_output",
            ),
        ],
        degraded_criterion_ids=["__copilot_fallback_floor__run"],
    )

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_unverified",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR


@pytest.mark.parametrize(
    "completion_verification",
    [
        None,
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c0"],
            verdicts=[CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts")],
        ),
    ],
)
def test_run_ok_with_failed_blocks_repairs_unless_outcome_is_fully_verified(
    completion_verification: CompletionVerificationResult | None,
) -> None:
    ctx = _ctx()
    ctx.last_test_ok = True
    ctx.completion_verification_result = completion_verification

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_run_ok_failed_block",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "EXTRACTION",
                        "status": "failed",
                        "failure_reason": "Required output was not produced.",
                    }
                ],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.NO_FAILURE
    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.repair_decision.target_blocks == ["extract"]
    if completion_verification is not None:
        assert contract.verification_result.user_goal_satisfied is False
        assert contract.verification_result.completion_contract_satisfied is False


def test_repairable_block_failure_contract_is_queryable_and_safe() -> None:
    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_3",
                "overall_status": "failed",
                "requested_block_labels": ["login", "extract"],
                "executed_block_labels": ["extract"],
                "frontier_start_label": "extract",
                "current_url": "https://example.test/account?id=secret",
                "page_title": "Account page",
                "failure_categories": [{"category": "DATA_EXTRACTION_FAILURE", "reasoning": "missing fields"}],
                "blocks": [
                    {"label": "extract", "block_type": "EXTRACTION", "status": "failed", "failure_reason": "No rows"}
                ],
            },
        },
        ctx=_ctx(),
    )

    trace = contract.to_trace_data()
    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.target_blocks == ["extract"]
    assert trace["failure_type"] == "repairable_block_failure"
    assert trace["next_action"] == "repair"
    assert trace["failure_categories"] == ["DATA_EXTRACTION_FAILURE"]
    assert contract.diagnosis_input.browser_page_state["current_origin"] == "https://example.test"
    assert "secret" not in contract.model_dump_json()
    assert "hunter2" not in contract.model_dump_json()


def test_credentialed_runtime_auth_failure_repairs_failed_code_block() -> None:
    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The code block used saved credentials but the browser ended on Login Failure.",
            "data": {
                "workflow_run_id": "wr_auth",
                "overall_status": "failed",
                "requested_block_labels": ["login"],
                "executed_block_labels": ["login"],
                "frontier_start_label": "login",
                "current_url": "https://example.test/loginFail/",
                "page_title": "Login Failure",
                "failure_categories": [{"category": "AUTH_FAILURE", "reasoning": "login rejected"}],
                "blocks": [
                    {
                        "label": "login",
                        "block_type": "CODE",
                        "status": "failed",
                        "failure_reason": "Saved credentials were submitted, but the page showed Login Failure.",
                    }
                ],
            },
        },
        ctx=_ctx(),
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR


def test_failed_auth_run_repairs_frontier_block_without_authoring_judge_authority() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="evidence_contradicts",
                evidence_ref="current_url,page_title",
            )
        ],
    )

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": (
                "Completion verification contradicted code output: login_succeeded=True, "
                "but saved credentials landed on /loginFail/ with Login Failure page evidence."
            ),
            "data": {
                "workflow_run_id": "wr_outcome",
                "overall_status": "completed",
                "frontier_start_label": "login",
                "current_url": "https://example.test/loginFail/",
                "page_title": "Login Failure",
                "failure_categories": [
                    {
                        "category": "AUTH_FAILURE",
                        "reasoning": "success flag contradicted by current page evidence",
                    }
                ],
                "completion_verification": ctx.completion_verification_result.to_trace_data(),
                "blocks": [{"label": "login", "block_type": "CODE", "status": "completed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False


def test_unbound_credential_skip_and_parameter_binding_errors_still_ask() -> None:
    unbound = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "message": "Skipped test run: required credentials are not configured.",
            "data": {
                "workflow_updated": True,
                "skipped_run": True,
                "skip_reason": "workflow_credential_inputs_unbound",
            },
        },
        ctx=_ctx(),
        workflow_updated=True,
    )
    binding_error = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Missing required workflow parameter for credential binding.",
            "data": {
                "overall_status": "failed",
                "failure_categories": [{"category": "PARAMETER_BINDING_ERROR"}],
            },
        },
        ctx=_ctx(),
        workflow_updated=True,
    )

    assert (
        unbound.diagnosis_result.suspected_failure_type,
        unbound.repair_decision.next_action,
        unbound.verification_result.user_goal_satisfied,
        unbound.verification_result.completion_contract_satisfied,
        unbound.verification_result.remaining_blocker,
        binding_error.diagnosis_result.suspected_failure_type,
        binding_error.repair_decision.next_action,
    ) == (
        DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
        RepairNextAction.ASK,
        False,
        False,
        "Skipped test run: required credentials are not configured.",
        DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
        RepairNextAction.ASK,
    )


def test_result_runtime_repair_context_prefers_repair_over_credential_ask() -> None:
    ctx = _ctx()
    repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )
    data = {
        "failure_type": "missing_credential_or_init",
        "diagnostic_code_safety_errors": ["Code block references names that are unavailable."],
    }
    data["authoring_repair_context"] = repair_context.model_dump(mode="json")

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Saved credential needs verification before running.",
            "data": data,
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.repair_decision.required_authority == []
    assert contract.repair_decision.target_blocks == ["create_request"]
    assert contract.to_trace_data()["next_action"] == "repair"


def test_stale_stored_runtime_context_does_not_override_credential_ask() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Saved credential needs verification before running.",
            "data": {
                "failure_type": "missing_credential_or_init",
                "diagnostic_code_safety_errors": ["Code block reads saved credential fields before live scouting."],
            },
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    assert contract.repair_decision.next_action == RepairNextAction.ASK
    assert contract.repair_decision.required_authority == []


def test_non_credential_runtime_failure_result_repairs_instead_of_credential_ask() -> None:
    ctx = _ctx()
    repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Code block `create_request` references names that are unavailable: business_name.",
            "data": {"authoring_repair_context": repair_context.model_dump(mode="json")},
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.repair_decision.target_blocks == ["create_request"]


def test_missing_credential_without_runtime_repair_context_still_asks() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="SANDBOX_SAFETY_CHECK",
        unresolved_names=[],
        parameter_keys=[],
    )
    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Saved credential needs verification before running.",
            "data": {"failure_type": "missing_credential_or_init"},
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    assert contract.repair_decision.next_action == RepairNextAction.ASK
    assert contract.repair_decision.required_authority == []


def test_runtime_repair_context_does_not_preempt_terminal_challenge_stop() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )
    ctx.last_test_anti_bot = "Typed run analysis reported an anti-bot challenge."
    ctx.last_test_failure_reason = "Run output reported a blocker: Verify you are human."

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_blocked",
                "overall_status": "completed",
                "failure_reason": ctx.last_test_failure_reason,
                "failure_categories": [{"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state"}],
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "EXTRACTION",
                        "status": "completed",
                    }
                ],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.to_trace_data()["next_action"] == "stop"


@pytest.mark.parametrize(
    ("suspicious", "completion_verification", "anti_bot", "failure_reason", "run_id"),
    [
        pytest.param(
            True,
            _satisfied_completion_verification(),
            "Extracted data reported anti-bot blocker: Verify you are human",
            "Run completed, but extracted data reported a blocker: Verify you are human",
            "wr_blocked",
            id="suspicious_success_flag_with_satisfied_verification",
        ),
        pytest.param(
            False,
            _satisfied_completion_verification(),
            "Extracted data reported anti-bot blocker: Verify you are human",
            "Run completed, but extracted data reported a blocker: Verify you are human",
            "wr_blocked_clean",
            id="satisfied_completion_verification_only",
        ),
        pytest.param(
            False,
            None,
            "Typed run analysis reported an anti-bot challenge.",
            "Run output reported a blocker: Verify you are human.",
            "wr_blocked",
            id="bare_challenge_category",
        ),
    ],
)
def test_terminal_challenge_preempts_clean_run_ok_contract_stops(
    suspicious: bool,
    completion_verification: CompletionVerificationResult | None,
    anti_bot: str,
    failure_reason: str,
    run_id: str,
) -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = suspicious
    ctx.last_test_anti_bot = anti_bot
    ctx.last_test_failure_reason = failure_reason
    ctx.completion_verification_result = completion_verification

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": run_id,
                "overall_status": "completed",
                "failure_reason": failure_reason,
                "failure_categories": [{"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state"}],
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "EXTRACTION",
                        "status": "completed",
                    }
                ],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert "Verify you are human" in contract.verification_result.remaining_blocker
    assert contract.to_trace_data()["failure_type"] == "terminal_challenge_blocker"


def test_terminal_challenge_preempts_failed_run_even_with_satisfied_completion_verification() -> None:
    ctx = _ctx()
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.last_test_failure_reason = "Run failed after challenge-gated submit controls were observed."
    ctx.completion_verification_result = _satisfied_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_blocked_failed",
                "overall_status": "failed",
                "failure_reason": ctx.last_test_failure_reason,
                "failure_categories": [{"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state"}],
                "blocks": [{"label": "submit", "block_type": "NAVIGATION", "status": "failed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert contract.verification_result.remaining_blocker is not None


def test_keyword_only_challenge_category_never_reaches_contract() -> None:
    ctx = _ctx()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Run failed.",
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "failure_reason": "Timeout waiting for search results",
                "failure_categories": [
                    {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.7, "evidence_source": "keyword_only"},
                ],
                "blocks": [{"label": "search", "block_type": "NAVIGATION", "status": "failed"}],
            },
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert "ANTI_BOT_DETECTION" not in contract.diagnosis_input.failure_categories
    assert contract.repair_decision.next_action != RepairNextAction.STOP
    assert "ANTI_BOT_CHALLENGE" not in contract.diagnosis_result.root_cause_identity.failure_categories


def test_low_confidence_challenge_category_does_not_preempt_clean_run_ok_contract() -> None:
    ctx = _ctx()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_clean",
                "overall_status": "completed",
                "failure_categories": [
                    {
                        "category": "ANTI_BOT_DETECTION",
                        "confidence_float": 0.2,
                        "reasoning": "Low-confidence upstream category.",
                    }
                ],
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "EXTRACTION",
                        "status": "completed",
                    }
                ],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.remaining_blocker is None


def test_pre_run_challenge_observation_does_not_force_stop_on_repairable_failure() -> None:
    ctx = _ctx()
    ctx.last_test_anti_bot = (
        "Observed anti-bot challenge evidence before the run: challenge-gated disabled submit/search control: Search"
    )
    ctx.last_test_failure_reason = "The search button selector changed before submit."

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_repair",
                "overall_status": "failed",
                "failure_reason": ctx.last_test_failure_reason,
                "blocks": [{"label": "submit_search", "block_type": "NAVIGATION", "status": "failed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR


def test_post_run_gated_challenge_observation_forces_stop_on_repairable_failure() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )
    ctx.last_test_anti_bot = (
        "Observed anti-bot challenge evidence before the run: challenge-gated disabled submit/search control: Search"
    )
    ctx.last_test_failure_reason = "The Search button remains disabled after verification."
    ctx.composition_page_evidence = {
        "observed_after_workflow_run": True,
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "requires_human_verification": True,
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_terminal",
                "overall_status": "failed",
                "failure_reason": ctx.last_test_failure_reason,
                "blocks": [{"label": "submit_search", "block_type": "NAVIGATION", "status": "failed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.STOP


def test_user_goal_urls_are_reduced_to_origins() -> None:
    ctx = _ctx()
    ctx.user_message = "Fix https://example.test/account?id=secret now"

    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={"ok": False, "error": "failed", "data": {"overall_status": "failed"}},
        ctx=ctx,
    )

    assert contract.diagnosis_input.user_goal == "Fix https://example.test now"
    assert "id=secret" not in contract.model_dump_json()


def test_suspicious_success_flag_does_not_override_failed_run() -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True

    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "error": "The run failed before output validation.",
            "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"},
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.FAILED_RUN
    assert contract.repair_decision.proposed_change_summary == (
        "Repair the workflow based on: The run failed before output validation."
    )


def test_stop_and_no_change_decisions_preserve_current_behavior_shadow_only() -> None:
    stop_ctx = _ctx()
    stop_ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code="runtime_block_failure",
        unresolved_names=["business_name"],
        parameter_keys=[],
    )
    stop_ctx.last_test_non_retriable_nav_error = "net::ERR_NAME_NOT_RESOLVED"
    stop_contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "data": {
                "overall_status": "failed",
                "blocks": [{"label": "open", "status": "failed", "failure_reason": "net::ERR_NAME_NOT_RESOLVED"}],
            },
        },
        ctx=stop_ctx,
    )
    no_change = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={"ok": True, "data": {"workflow_run_id": "wr_4", "overall_status": "completed", "blocks": []}},
        ctx=_ctx(),
    )

    assert stop_contract.repair_decision.next_action == RepairNextAction.STOP
    assert no_change.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert no_change.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert no_change.verification_result.completion_contract_satisfied is True
    assert no_change.verification_result.remaining_blocker is None


def test_unrecoverable_browser_session_contract_stops_with_blocker() -> None:
    reason = "Browser session not found while taking screenshot (404)."
    contract = build_diagnosis_repair_contract(
        source_tool="get_browser_screenshot",
        result={
            "ok": False,
            "error": reason,
            "data": {
                "overall_status": "aborted",
                "failure_categories": [{"category": "UNRECOVERABLE_TOOL_ERROR"}],
            },
        },
        ctx=_ctx(),
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.remaining_blocker == reason


def test_contract_trace_exposes_stable_root_cause_identity() -> None:
    def result(reason: str, status: str, label: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": reason,
            "data": {
                "workflow_run_id": f"wr_{label}",
                "overall_status": status,
                "failure_reason": reason,
                "frontier_start_label": label,
                "failure_categories": [{"category": "UNRECOVERABLE_TOOL_ERROR"}],
                "blocks": [{"label": label, "status": status, "failure_reason": reason}],
            },
        }

    base = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result=result('Browser session not found while waiting for locator("#submit")', "failed", "login_v1"),
        ctx=_ctx(),
    )
    renamed = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result=result('No browser context while waiting for locator("#submit")', "terminated", "login_v2"),
        ctx=_ctx(),
    )

    base_trace = base.to_trace_data()
    renamed_trace = renamed.to_trace_data()
    assert base_trace["root_cause_signature"] == renamed_trace["root_cause_signature"]
    assert base_trace["root_cause_error_class"] == "browser_session_not_found"
    assert base_trace["root_cause_selector_kind"] == "locator"
    assert base_trace["root_cause_selector"] == "#submit"
    assert (
        base.model_dump()["diagnosis_result"]["root_cause_identity"]["root_cause_signature"]
        == base_trace["root_cause_signature"]
    )
    assert base_trace["run_status"] != renamed_trace["run_status"]
    assert {base.diagnosis_result.suspected_failure_type, renamed.diagnosis_result.suspected_failure_type} == {
        DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR
    }


def _failed_run_result(run_id: str = "wr_failed") -> dict[str, object]:
    return {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "search_registry",
                    "status": "failed",
                    "failure_reason": 'Timeout waiting for locator("button.icon-btn")',
                }
            ],
        },
    }


def _bounded_failure_page_evidence() -> dict[str, object]:
    return {
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/results",
        "page_title": "Results",
        "forms": [
            {
                "fields": [{"label": "Query", "selector": "#q"}],
                "submit_controls": [{"text": "", "selector": "button.icon-btn", "disabled": False}],
            }
        ],
        "navigation_targets": [{"text": "Details", "selector": "a.detail"}],
        "result_containers": [
            {
                "tag": "table",
                "selector": "#results",
                "row_selector": "#results tbody tr",
                "expand_toggle_candidates": ["#results tbody tr button"],
                "sample_rows": ["First result row"],
            }
        ],
        "challenge_controls": [],
    }


@pytest.mark.parametrize(
    "failure_reason",
    [
        "Failed to execute code block.",
        "CodeBlock failed because a browser operation failed at line 12: Locator.wait_for timed out.",
    ],
    ids=["inline", "secure_runner"],
)
def test_codeblock_failure_result_carries_same_run_page_facts_for_both_execution_engines(
    failure_reason: str,
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_result = _failed_run_result()
    data = run_result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    block["failure_reason"] = failure_reason
    evidence = _bounded_failure_page_evidence()
    evidence["workflow_run_id"] = "wr_failed"
    evidence["observed_after_workflow_run"] = True
    ctx.composition_page_evidence = evidence

    run_execution_module._record_run_blocks_result(ctx, run_result)
    inject_runtime_authoring_repair_context(ctx, run_result)

    assert block["failure_reason"] == failure_reason
    repair_context = CodeAuthoringRepairContext.model_validate(data["authoring_repair_context"])
    assert repair_context.current_url == "https://example.test/app/results"
    assert repair_context.current_title == "Results"
    assert repair_context.page_result_summaries == ["First result row"]
    assert repair_context.observed_after_workflow_run is True


_SOURCE_MISMATCH_EVENT = "copilot_post_run_evidence_source_mismatch_refused"


def _stale_login_page_evidence() -> dict[str, object]:
    return {**_bounded_failure_page_evidence(), "current_url": "https://example.test/account/login"}


def _cross_session_run_result() -> dict[str, object]:
    result = _failed_run_result()
    data = result["data"]
    assert isinstance(data, dict)
    data["browser_session_id"] = "pbs_run"
    return result


def test_store_post_run_page_evidence_refuses_a_packet_read_from_another_browser_session() -> None:
    ctx = _ctx()
    with capture_logs() as logs:
        stored, _ = store_post_run_page_evidence(
            ctx,
            _stale_login_page_evidence(),
            run_id="wr_failed",
            current_url="https://example.test/account/login",
            source_browser_session_id="pbs_scout",
            run_browser_session_id="pbs_run",
        )

    assert stored["source_browser_session_id"] == "pbs_scout"
    assert stored["observed_after_workflow_run"] is False
    assert "workflow_run_id" not in stored
    assert any(entry["event"] == _SOURCE_MISMATCH_EVENT for entry in logs)

    refused = recorded_outcome_from_run_blocks_result(
        _cross_session_run_result(), page_evidence={**stored, "source_browser_session_id": "pbs_run"}
    )
    assert refused is None


def test_unknown_run_session_grants_a_foreign_packet_so_producers_must_stamp_the_run_session() -> None:
    """An envelope that omits browser_session_id reads as 'unknown' and grants, which is why every
    result path — watchdog cancels included — stamps the run session rather than leaving it out."""
    ctx = _ctx()
    with capture_logs() as logs:
        stored, _ = store_post_run_page_evidence(
            ctx,
            _stale_login_page_evidence(),
            run_id="wr_failed",
            current_url="https://example.test/account/login",
            source_browser_session_id="pbs_scout",
            run_browser_session_id=None,
        )

    assert stored["source_browser_session_id"] == "pbs_scout"
    assert stored["observed_after_workflow_run"] is True
    assert stored["workflow_run_id"] == "wr_failed"
    assert not any(entry["event"] == _SOURCE_MISMATCH_EVENT for entry in logs)

    refused = recorded_outcome_from_run_blocks_result(_cross_session_run_result(), page_evidence=stored)
    assert refused is None


def test_mark_post_run_page_observed_refuses_a_packet_read_from_another_browser_session() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_test_ok = False

    with capture_logs() as logs:
        _mark_post_run_page_observed(
            ctx,
            source_tool="inspect_page_for_composition",
            url="https://example.test/account/login",
            page_evidence=_stale_login_page_evidence(),
            source_browser_session_id="pbs_scout",
        )

    assert ctx.post_run_page_observation_tool is None
    assert ctx.post_run_page_observation_workflow_run_id is None
    assert any(entry["event"] == _SOURCE_MISMATCH_EVENT for entry in logs)


@pytest.mark.parametrize("run_browser_session_id", ["pbs_run", None])
def test_post_run_page_evidence_keeps_run_identity_without_a_foreign_source(
    run_browser_session_id: str | None,
) -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_run_blocks_browser_session_id = run_browser_session_id
    ctx.last_test_ok = False

    stored, _ = store_post_run_page_evidence(
        ctx,
        _bounded_failure_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id="pbs_run",
        run_browser_session_id=run_browser_session_id,
    )
    _mark_post_run_page_observed(
        ctx,
        source_tool="inspect_page_for_composition",
        url="https://example.test/app/results",
        page_evidence=stored,
        source_browser_session_id="pbs_run",
    )

    assert stored["source_browser_session_id"] == "pbs_run"
    assert stored["observed_after_workflow_run"] is True
    assert stored["workflow_run_id"] == "wr_failed"
    assert ctx.post_run_page_observation_workflow_run_id == "wr_failed"


def test_post_run_failure_page_store_mark_inject_grounds_repair_without_finalizing_early() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER

    stored, _ = store_post_run_page_evidence(
        ctx,
        _bounded_failure_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id="pbs_run",
        run_browser_session_id="pbs_run",
    )
    assert ctx.composition_page_evidence is stored
    assert stored["observed_after_workflow_run"] is True
    assert stored["workflow_run_id"] == "wr_failed"
    assert ctx.last_code_authoring_repair_context is None

    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    assert ctx.pending_code_authoring_runtime_repair_context is not None
    assert ctx.post_run_page_observation_workflow_run_id is None

    run_execution_module._mark_stored_post_run_failure_page(ctx)
    assert ctx.post_run_page_observation_tool == "inspect_page_for_composition"
    assert ctx.post_run_page_observation_workflow_run_id == "wr_failed"
    assert ctx.post_run_page_observation_after_failed_test is True
    assert ctx.last_code_authoring_repair_context is None

    result = {"ok": False, "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"}}
    inject_runtime_authoring_repair_context(ctx, result)

    repair_context = ctx.last_code_authoring_repair_context
    assert isinstance(repair_context, CodeAuthoringRepairContext)
    assert repair_context.observed_after_workflow_run is True
    assert result["data"]["authoring_repair_context"]["observed_after_workflow_run"] is True
    grounded = repair_context.page_form_summaries + repair_context.page_action_summaries
    assert any(summary in {"Query", "Details"} for summary in grounded)
    assert all("button.icon-btn" not in summary for summary in grounded)
    assert repair_context.page_result_summaries


def test_stored_terminal_challenge_page_feeds_classifier_and_suppresses_authoring_context() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.composition_page_evidence = {
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_failed",
        "current_url": "https://example.test/challenge",
        "anti_bot_indicators": ["verify you are human"],
        "challenge_controls": [{"text": "Verify", "selector": "#verify"}],
        "challenge_state": {"detected": True, "gates_submit_controls": True},
        "forms": [{"fields": [{"label": "Query", "selector": "#q"}], "submit_controls": []}],
    }

    run_execution_module._record_run_blocks_result(ctx, _failed_run_result())
    assert ctx.last_test_anti_bot

    result = {"ok": False, "data": {"workflow_run_id": "wr_failed", "overall_status": "failed"}}
    inject_runtime_authoring_repair_context(ctx, result)
    assert "authoring_repair_context" not in result["data"]
    assert ctx.last_code_authoring_repair_context is None


@pytest.mark.parametrize(
    "evidence,run_id,expected",
    [
        (
            {
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "workflow_run_id": "wr",
                "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
            },
            "wr",
            True,
        ),
        (
            {
                "source_tool": "evaluate",
                "observed_after_workflow_run": True,
                "workflow_run_id": "wr",
                "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
            },
            "wr",
            False,
        ),
        (
            {
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": False,
                "workflow_run_id": "wr",
                "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
            },
            "wr",
            False,
        ),
        (
            {
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "workflow_run_id": "other",
                "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
            },
            "wr",
            False,
        ),
        (
            {
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "workflow_run_id": "wr",
                "forms": [],
            },
            "wr",
            False,
        ),
        (None, "wr", False),
    ],
)
def test_post_run_inspection_cleanly_matches_predicate(evidence: object, run_id: str, expected: bool) -> None:
    assert post_run_inspection_cleanly_matches(evidence, run_id) is expected


def test_same_run_post_run_page_evidence_is_redacted_and_copied_for_the_run_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    evidence = _bounded_failure_page_evidence()
    evidence["observed_after_workflow_run"] = True
    evidence["workflow_run_id"] = "wr_current"
    evidence["result_containers"] = [{"rows": [{"cells": ["private-value"]}]}]
    ctx.composition_page_evidence = evidence
    ctx.codeblock_redaction_parameters = {"account": "private-value"}
    safe_evidence = {**evidence, "result_containers": [{"rows": [{"cells": ["[REDACTED]"]}]}]}
    scrubber = MagicMock(return_value=safe_evidence)
    monkeypatch.setattr(run_execution_module.app.AGENT_FUNCTION, "redact_codeblock_parameter_values", scrubber)

    result_evidence = run_execution_module._same_run_page_evidence_for_result(ctx, "wr_current")

    assert result_evidence == safe_evidence
    assert result_evidence is not evidence
    scrubber.assert_called_once_with(evidence, ctx.codeblock_redaction_parameters)


def test_foreign_post_run_page_evidence_is_excluded_from_the_run_result() -> None:
    ctx = _ctx()
    evidence = _bounded_failure_page_evidence()
    evidence["observed_after_workflow_run"] = True
    evidence["workflow_run_id"] = "wr_other"
    ctx.composition_page_evidence = evidence

    assert run_execution_module._same_run_page_evidence_for_result(ctx, "wr_current") is None


@pytest.mark.asyncio
async def test_bounded_seam_capture_is_stored_stamped_without_touching_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    captured = _bounded_failure_page_evidence()
    captured.pop("workflow_run_id", None)

    async def fake_capture(
        _ctx: CopilotContext, *, inspected_url: str, current_url: str
    ) -> tuple[dict[str, object], None]:
        return dict(captured), None

    monkeypatch.setattr(composition_capture_module, "_capture_composition_evidence", fake_capture)

    capture = await run_execution_module._capture_and_store_post_run_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app/results"
    )

    evidence = ctx.composition_page_evidence
    assert isinstance(evidence, dict)
    assert evidence["workflow_run_id"] == "wr_failed"
    assert evidence["observed_after_workflow_run"] is True
    assert post_run_inspection_cleanly_matches(evidence, "wr_failed")
    assert capture.status == "captured"
    assert ctx.page_inspection_calls_this_turn == 0
    assert ctx.browser_session_id is None


@pytest.mark.asyncio
async def test_sensitive_post_run_capture_redacts_registry_values_and_keeps_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_sensitive",
        {
            "credential": {"username": "private-user", "password": "private-pass"},
            "copilot_run_runtime_secret_values": ("654321",),
        },
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )

    async def fake_read(
        _ctx: CopilotContext, *, run_session_id: str, current_url: str
    ) -> tuple[dict[str, object], str, None, None]:
        evidence = _bounded_failure_page_evidence()
        evidence["forms"] = [{"fields": [{"label": "Verification code", "selector": "#totp", "value": "654321"}]}]
        return evidence, run_session_id, None, None

    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", fake_read)

    capture = await run_execution_module._capture_and_store_post_run_page(
        ctx,
        run_session_id="run_session",
        run_id="wr_sensitive",
        current_url="https://example.test/otp",
    )

    assert capture.status == "captured"
    stored = ctx.composition_page_evidence
    assert isinstance(stored, dict)
    assert stored["forms"][0]["fields"][0]["selector"] == "#totp"
    assert "private-user" not in json.dumps(stored)
    assert "private-pass" not in json.dumps(stored)
    assert "654321" not in json.dumps(stored)
    assert "[REDACTED_SECRET]" in json.dumps(stored)


@pytest.mark.asyncio
async def test_sensitive_post_run_capture_stays_withheld_when_registry_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.composition_page_evidence = _bounded_failure_page_evidence()
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_sensitive",
        {"credential": {"username": "private-user"}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=False,
    )
    read = AsyncMock()
    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", read)

    capture = await run_execution_module._capture_and_store_post_run_page(
        ctx,
        run_session_id="run_session",
        run_id="wr_sensitive",
        current_url="https://example.test/otp",
    )

    assert capture.status == "unavailable"
    assert ctx.composition_page_evidence is None
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_run_capture_refused_for_a_foreign_session_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER

    async def fake_read(
        _ctx: CopilotContext, *, run_session_id: str, current_url: str
    ) -> tuple[dict[str, object], str, None, None]:
        return _bounded_failure_page_evidence(), "foreign_session", None, None

    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", fake_read)

    capture = await run_execution_module._capture_and_store_post_run_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app/results"
    )

    assert capture.status == "unavailable"
    assert capture.omission == "page_capture_unavailable"
    assert run_execution_module._same_run_page_evidence_for_result(ctx, "wr_failed") is None


@pytest.mark.asyncio
async def test_screenshot_capture_failure_keeps_a_typed_omission_on_structured_page_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    monkeypatch.setattr(
        composition_capture_module,
        "_composition_get_screenshot",
        AsyncMock(return_value={"ok": False, "error": "browser session unavailable"}),
    )

    evidence, captured_frame = await composition_capture_module._augment_composition_evidence_with_visual_fallback(
        ctx,
        {
            "current_url": "https://example.test/checkout/payment",
            "forms": [{"submit_controls": [{"text": "Place order", "disabled": False}]}],
        },
    )

    assert captured_frame is None
    assert evidence["visual_capture_omissions"] == ["screenshot_capture_failed"]
    assert evidence["forms"] == [{"submit_controls": [{"text": "Place order", "disabled": False}]}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale",
    [
        {
            "source_tool": "evaluate",
            "observed_after_workflow_run": True,
            "workflow_run_id": "wr_failed",
            "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
        },
        {
            "source_tool": "inspect_page_for_composition",
            "observed_after_workflow_run": False,
            "workflow_run_id": "wr_failed",
            "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
        },
        {
            "source_tool": "inspect_page_for_composition",
            "observed_after_workflow_run": True,
            "workflow_run_id": "wr_other",
            "forms": [{"fields": [{"label": "a", "selector": "#a"}]}],
        },
    ],
)
async def test_failed_seam_capture_neutralizes_non_matching_evidence(
    monkeypatch: pytest.MonkeyPatch, stale: dict[str, object]
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.composition_page_evidence = stale

    async def fake_capture(_ctx: CopilotContext, *, inspected_url: str, current_url: str) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(composition_capture_module, "_capture_composition_evidence", fake_capture)

    await run_execution_module._capture_and_store_post_run_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app"
    )
    assert ctx.composition_page_evidence is None


@pytest.mark.asyncio
async def test_failed_seam_capture_preserves_clean_matching_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    clean = _bounded_failure_page_evidence()
    clean["observed_after_workflow_run"] = True
    clean["workflow_run_id"] = "wr_failed"
    ctx.composition_page_evidence = clean

    async def fake_capture(_ctx: CopilotContext, *, inspected_url: str, current_url: str) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(composition_capture_module, "_capture_composition_evidence", fake_capture)

    await run_execution_module._capture_and_store_post_run_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app"
    )
    assert ctx.composition_page_evidence is clean


def _post_run_inspect_ctx() -> CopilotContext:
    ctx = _ctx()
    ctx.browser_session_id = "scout_session"
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_run_blocks_browser_session_id = "run_session"
    ctx.last_test_ok = False
    ctx.org_credentials_for_turn = []
    return ctx


async def _drive_inspect_page(
    monkeypatch: pytest.MonkeyPatch,
    ctx: CopilotContext,
    *,
    captured: dict[str, object] | None,
    target_url: str = "current_page",
    capture_lands_on_session: str | None = None,
    capture_clears_session: bool = False,
    capture_raises: bool = False,
) -> dict[str, object]:
    async def fake_capture(
        inner_ctx: CopilotContext, *, inspected_url: str, current_url: str
    ) -> tuple[dict[str, object] | None, None]:
        if capture_clears_session:
            inner_ctx.browser_session_id = None
        elif capture_lands_on_session is not None:
            inner_ctx.browser_session_id = capture_lands_on_session
        if capture_raises:
            raise TimeoutError("capture timed out after the session was substituted")
        return (dict(captured) if captured is not None else None), None

    async def fake_page_info(inner_ctx: CopilotContext, session_id_override: str | None = None) -> tuple[str, str]:
        return "https://example.test/app/results", "Results"

    monkeypatch.setattr(composition_capture_module, "_capture_composition_evidence", fake_capture)
    monkeypatch.setattr(composition_capture_module, "_fallback_page_info", fake_page_info)
    return await composition_capture_module._inspect_page_for_composition_impl(ctx, target_url)


@pytest.mark.asyncio
async def test_post_run_current_page_inspect_is_sourced_from_the_run_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()

    with capture_logs() as logs:
        result = await _drive_inspect_page(monkeypatch, ctx, captured=_bounded_failure_page_evidence())

    assert result["ok"] is True
    stored = ctx.composition_page_evidence
    assert isinstance(stored, dict)
    assert stored["source_browser_session_id"] == "run_session"
    assert stored["observed_after_workflow_run"] is True
    assert stored["workflow_run_id"] == "wr_failed"
    assert ctx.post_run_page_observation_workflow_run_id == "wr_failed"
    assert result["reached_via"] == "post_run"
    assert ctx.browser_session_id == "scout_session"
    assert not any(entry.get("event") == "copilot_post_run_evidence_source_mismatch_refused" for entry in logs)


@pytest.mark.asyncio
async def test_sensitive_current_page_inspect_redacts_registry_values_and_keeps_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()
    ctx.browser_session_id = "run_session"
    ctx.last_run_blocks_browser_session_id = "run_session"
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_failed",
        {
            "credential": {"username": "private-user", "password": "private-pass"},
            "copilot_run_runtime_secret_values": ("654321",),
        },
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    ctx.sensitive_origin_browser_session_ids = {"run_session"}
    evidence = _bounded_failure_page_evidence()
    evidence["forms"] = [{"fields": [{"label": "Verification code", "selector": "#totp", "value": "654321"}]}]

    result = await _drive_inspect_page(monkeypatch, ctx, captured=evidence)

    assert result["ok"] is True
    assert result["data"]["forms"][0]["fields"][0]["label"] == "Verification code"
    assert "private-user" not in json.dumps(result)
    assert "private-pass" not in json.dumps(result)
    assert "654321" not in json.dumps(result)
    assert "[REDACTED_SECRET]" in json.dumps(result)


@pytest.mark.asyncio
async def test_sensitive_current_page_inspect_stays_withheld_while_origin_run_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()
    ctx.browser_session_id = "run_session"
    ctx.last_run_blocks_browser_session_id = "run_session"
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_failed",
        {"credential": {"username": "private-user", "password": "private-pass"}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    ctx.sensitive_origin_browser_session_ids = {"run_session"}
    ctx.active_sensitive_origin_browser_session_ids = {"run_session"}

    result = await _drive_inspect_page(monkeypatch, ctx, captured=_bounded_failure_page_evidence())

    assert result["ok"] is False
    assert "specific named URL" in result["error"]


@pytest.mark.asyncio
async def test_post_run_inspect_drops_a_packet_whose_source_session_is_unprovable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed mid-capture session create clears the id, and an unknown source id grants post-run
    identity, so the packet has to be dropped rather than laundered into the run's own evidence."""
    ctx = _post_run_inspect_ctx()

    result = await _drive_inspect_page(
        monkeypatch,
        ctx,
        captured=_bounded_failure_page_evidence(),
        capture_clears_session=True,
    )

    assert result["ok"] is False
    assert ctx.composition_page_evidence is None
    assert ctx.post_run_page_observation_workflow_run_id is None
    assert ctx.browser_session_id == "scout_session"


@pytest.mark.asyncio
async def test_failed_post_run_capture_on_a_substituted_session_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substitution racing a capture failure is the trickiest path here: it must neither grant post-run
    identity nor disturb the packet already stored for the run."""
    ctx = _post_run_inspect_ctx()
    clean = _clean_same_run_page_evidence()
    ctx.composition_page_evidence = clean

    result = await _drive_inspect_page(
        monkeypatch,
        ctx,
        captured=_bounded_failure_page_evidence(),
        capture_lands_on_session="replacement_session",
        capture_raises=True,
    )

    assert result["ok"] is False
    assert ctx.composition_page_evidence is clean
    assert ctx.post_run_page_observation_workflow_run_id is None
    assert ctx.browser_session_id == "scout_session"


@pytest.mark.asyncio
async def test_post_run_inspect_does_not_close_the_session_it_landed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool calls in one batch run concurrently, so a session read back off the shared context may be
    a sibling's rather than this capture's substitute; closing it would kill a live browser."""
    ctx = _post_run_inspect_ctx()
    closed: list[str] = []

    class _Sessions:
        async def close_session(self, *, organization_id: str, browser_session_id: str) -> None:
            closed.append(browser_session_id)

    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", _Sessions(), raising=False)

    await _drive_inspect_page(
        monkeypatch,
        ctx,
        captured=_bounded_failure_page_evidence(),
        capture_lands_on_session="replacement_session",
    )

    assert closed == []
    assert ctx.browser_session_id == "scout_session"


@pytest.mark.asyncio
async def test_post_run_inspect_stamps_the_session_the_capture_landed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()

    result = await _drive_inspect_page(
        monkeypatch,
        ctx,
        captured=_bounded_failure_page_evidence(),
        capture_lands_on_session="replacement_session",
    )

    evidence = result["data"]
    assert isinstance(evidence, dict)
    assert evidence["source_browser_session_id"] == "replacement_session"
    assert evidence["observed_after_workflow_run"] is False
    assert "workflow_run_id" not in evidence
    assert ctx.post_run_page_observation_workflow_run_id is None
    assert result["reached_via"] == "current_page"


def _clean_same_run_page_evidence() -> dict[str, object]:
    clean = _bounded_failure_page_evidence()
    clean["observed_after_workflow_run"] = True
    clean["workflow_run_id"] = "wr_failed"
    clean["source_browser_session_id"] = "run_session"
    return clean


@pytest.mark.asyncio
async def test_preserved_post_run_capture_does_not_move_the_observation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture too hollow to store must not move the marker either, or the marker and the stored
    packet describe different pages."""
    ctx = _post_run_inspect_ctx()
    ctx.composition_page_evidence = _clean_same_run_page_evidence()
    ctx.post_run_page_observation_generation = 3
    hollow: dict[str, object] = {
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/login",
        "page_title": "Sign in",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
    }

    await _drive_inspect_page(monkeypatch, ctx, captured=hollow)

    assert ctx.post_run_page_observation_generation == 3
    stored = ctx.composition_page_evidence
    assert isinstance(stored, dict)
    assert stored["page_title"] != "Sign in"


@pytest.mark.asyncio
async def test_refused_post_run_capture_preserves_stored_evidence_but_returns_the_fresh_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()
    clean = _clean_same_run_page_evidence()
    ctx.composition_page_evidence = clean
    replacement_page = _bounded_failure_page_evidence()
    replacement_page["page_title"] = "Replacement session page"

    result = await _drive_inspect_page(
        monkeypatch, ctx, captured=replacement_page, capture_lands_on_session="replacement_session"
    )

    assert ctx.composition_page_evidence is clean
    evidence = result["data"]
    assert isinstance(evidence, dict)
    assert evidence["page_title"] == "Replacement session page"
    assert evidence["observed_after_workflow_run"] is False


@pytest.mark.asyncio
async def test_hollow_post_run_capture_preserves_stored_evidence_but_returns_the_fresh_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()
    clean = _clean_same_run_page_evidence()
    ctx.composition_page_evidence = clean
    hollow: dict[str, object] = {
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.test/app/login",
        "page_title": "Sign in",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
    }

    result = await _drive_inspect_page(monkeypatch, ctx, captured=hollow)

    assert ctx.composition_page_evidence is clean
    evidence = result["data"]
    assert isinstance(evidence, dict)
    assert evidence["page_title"] == "Sign in"
    assert evidence["observed_after_workflow_run"] is True
    assert evidence["source_browser_session_id"] == "run_session"


@pytest.mark.asyncio
async def test_post_run_url_target_inspect_returns_the_page_it_inspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _post_run_inspect_ctx()
    clean = _bounded_failure_page_evidence()
    clean["observed_after_workflow_run"] = True
    clean["workflow_run_id"] = "wr_failed"
    ctx.composition_page_evidence = clean
    inspected = _bounded_failure_page_evidence()
    inspected["page_title"] = "Other page"

    result = await _drive_inspect_page(
        monkeypatch, ctx, captured=inspected, target_url="https://example.test/app/results"
    )

    evidence = result["data"]
    assert isinstance(evidence, dict)
    assert evidence["page_title"] == "Other page"
    assert evidence["source_browser_session_id"] == "scout_session"
    assert evidence["observed_after_workflow_run"] is False
    assert ctx.composition_page_evidence is clean


def _secure_runner_unavailable_result() -> dict[str, object]:
    return {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_runner_unavailable",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "run_code",
                    "block_type": "CODE",
                    "status": "failed",
                    "failure_reason": "Secure CodeBlock runner is unavailable. Please retry.",
                    "error_codes": ["runner_unavailable"],
                }
            ],
        },
    }


def test_runner_unavailable_stops_even_with_a_code_authoring_repair_context() -> None:
    ctx = _ctx()
    result = _secure_runner_unavailable_result()
    run_execution_module._record_run_blocks_result(ctx, result, completion_verification=None)
    data = result["data"]
    assert isinstance(data, dict)
    data["authoring_repair_context"] = CodeAuthoringRepairContext(
        block_label="run_code",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='Download']",
    ).model_dump(mode="json")

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=result,
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR
    assert contract.repair_decision.next_action == RepairNextAction.STOP


def test_user_code_error_still_repairs_through_the_contract() -> None:
    ctx = _ctx()
    result = _secure_runner_unavailable_result()
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    blocks[0]["error_codes"] = ["user_code_error"]
    blocks[0]["failure_reason"] = "NameError: name 'undefined_helper' is not defined"
    run_execution_module._record_run_blocks_result(ctx, result, completion_verification=None)

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=result,
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.UNRECOVERABLE_TOOL_ERROR
    assert contract.repair_decision.next_action != RepairNextAction.STOP


@pytest.mark.asyncio
async def test_completed_missing_output_complete_fact_packet_reaches_ordinary_repair_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    now = datetime(2026, 8, 28, tzinfo=UTC)
    output_parameter = OutputParameter(
        output_parameter_id="out_confirmation",
        workflow_id="wf_run_snapshot",
        key="confirmation",
        description="Confirmation details",
        created_at=now,
        modified_at=now,
    )
    run_workflow = SimpleNamespace(
        organization_id=ctx.organization_id,
        workflow_definition=SimpleNamespace(
            parameters=[output_parameter],
            blocks=[SimpleNamespace(label="open_result", block_type="CODE", output_parameter=output_parameter)],
        ),
    )
    run = SimpleNamespace(
        workflow_permanent_id=ctx.workflow_permanent_id,
        browser_session_id="pbs_completed_missing_output",
        status="completed",
        failure_reason=None,
    )
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_completed_missing_output",
        workflow_run_id="wr_completed_missing_output",
        organization_id=ctx.organization_id,
        label="open_result",
        block_type=BlockType.CODE,
        status="completed",
        failure_reason=None,
        error_codes=[],
        output=None,
        final_url="https://example.test/complete",
        created_at=now,
        modified_at=now,
    )
    artifact = make_stub_html_artifact("art_completed_terminal", ArtifactType.HTML_ACTION)
    html = (
        b"<html><body><main><h1>Complete</h1><form>"
        b"<button type='submit'>View confirmation</button></form></main></body></html>"
    )
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(return_value=run),
                get_workflow_run_output_parameters=AsyncMock(return_value=[]),
            ),
            observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
            workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=run_workflow)),
            artifacts=SimpleNamespace(get_artifacts_for_run=AsyncMock(return_value=[artifact])),
        ),
        AGENT_FUNCTION=SimpleNamespace(should_dispatch_copilot_block_run_to_worker=AsyncMock(return_value=True)),
        ARTIFACT_MANAGER=SimpleNamespace(retrieve_artifact=AsyncMock(return_value=html)),
    )
    monkeypatch.setattr(run_execution_module, "app", fake_app)
    monkeypatch.setattr(run_execution_module, "_attach_action_traces", AsyncMock())
    monkeypatch.setattr(run_execution_module, "_attach_failed_block_screenshots", AsyncMock())
    produced_outcomes: list[RecordedBuildTestOutcome] = []
    transported_outcomes: list[RecordedBuildTestOutcome | None] = []
    real_producer = run_execution_module.recorded_outcome_from_run_blocks_result
    real_packet_builder = run_execution_module.build_test_evidence_packet

    def capture_producer(*args: object, **kwargs: object) -> RecordedBuildTestOutcome:
        produced = real_producer(*args, **kwargs)
        produced_outcomes.append(produced)
        return produced

    def capture_packet(
        context: CopilotContext,
        result: object,
        *,
        recorded_outcome: RecordedBuildTestOutcome | None = None,
    ) -> BuildTestEvidencePacket:
        transported_outcomes.append(recorded_outcome)
        return real_packet_builder(context, result, recorded_outcome=recorded_outcome)

    monkeypatch.setattr(run_execution_module, "recorded_outcome_from_run_blocks_result", capture_producer)
    monkeypatch.setattr(run_execution_module, "build_test_evidence_packet", capture_packet)
    hydrated = await run_execution_module.hydrate_prior_run_packet(ctx, workflow_run_id="wr_completed_missing_output")
    system_input = str(
        _build_dynamic_system_prompt("", CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER))(
            SimpleNamespace(context=ctx), None
        )
    )
    ordinary_input = (
        system_input
        + "\n"
        + _build_user_context(
            workflow_yaml=ctx.workflow_yaml,
            chat_history_text="",
            global_llm_context="",
            debug_run_info_text=_prior_run_debug_text(hydrated),
            user_message="Repair the recorded run.",
        )
    )

    assert hydrated is not None
    assert len(produced_outcomes) == 1
    assert produced_outcomes[0].workflow_run_id == "wr_completed_missing_output"
    assert len(produced_outcomes[0].missing_requested_output_facts) == 1
    assert produced_outcomes[0].missing_requested_output_facts[0]["output_path"] == "output.confirmation"
    assert produced_outcomes[0].missing_requested_output_facts[0]["reason_code"] == "registered_output_missing"
    assert transported_outcomes == [produced_outcomes[0]]
    assert ctx.latest_recorded_build_test_outcome is None
    assert '"status": "completed"' in ordinary_input
    assert '"workflow_run_id": "wr_completed_missing_output"' in ordinary_input
    assert '"output_parameter_id": "out_confirmation"' in ordinary_input
    assert '"output_parameter_key": "confirmation"' in ordinary_input
    assert '"output_path": "output.confirmation"' in ordinary_input
    assert '"reason_code": "registered_output_missing"' in ordinary_input
    assert hydrated.get("page_state") is not None
    assert '"page_state"' in ordinary_input
    assert "Complete" in ordinary_input
    assert "View confirmation" in ordinary_input
    assert "success_verdict" not in ordinary_input
    assert "RECORDED BUILD-TEST OUTCOME" not in system_input
    assert "repairable_failure" not in system_input
    assert "wr_completed_missing_output" not in system_input


def test_sheets_missing_binding_failure_arms_repair_on_the_failed_block() -> None:
    # SKY-13624 B2: the strict-render failure is run evidence that arms repair targeting the failed
    # block, never an evidence-free fresh-build route.
    run_result = {
        "ok": False,
        "error": "Run failed.",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {"label": "collect_visitors", "status": "completed"},
                {
                    "label": "append_visitors_to_sheet",
                    "status": "failed",
                    "failure_reason": (
                        "Failed to format jinja template: block `append_visitors_to_sheet` field `values` "
                        "references a value no upstream block produced: 'dict object' has no attribute "
                        "'visitor_count'. Return that key from the producing block, or write an explicit "
                        "default (e.g. {{ block_label.field | default('') }}) if an empty cell is intended."
                    ),
                },
            ],
        },
    }

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=run_result,
        ctx=_ctx(),
        workflow_updated=True,
    )

    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.repair_decision.target_blocks == ["append_visitors_to_sheet"]
    assert contract.diagnosis_input.failed_block_labels == ["append_visitors_to_sheet"]


def _challenge_wall_page_evidence(challenge_kind: str | None, *, run_id: str = "wr_device_wall") -> dict[str, object]:
    challenge_state: dict[str, object] = {
        "detected": True,
        "kind": "2-step verification",
        "requires_human_verification": True,
    }
    if challenge_kind is not None:
        challenge_state[CHALLENGE_KIND_KEY] = challenge_kind
    return {
        "current_url": "https://sso.example.test/challenge",
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "workflow_run_id": run_id,
        "challenge_state": challenge_state,
    }


def _fresh_session_run_result(
    page_evidence: dict[str, object] | None,
    used_fresh_run_session: bool | None = True,
    stalled_pre_auth: bool | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "workflow_run_id": "wr_device_wall",
        "overall_status": "failed",
        "failure_reason": "Failed to execute code block.",
        "failure_categories": [{"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state"}],
        "blocks": [{"label": "authenticate", "status": "failed"}],
    }
    if page_evidence is not None:
        data["post_run_page_evidence"] = page_evidence
    if used_fresh_run_session is not None:
        data["used_fresh_run_session"] = used_fresh_run_session
    if stalled_pre_auth is not None:
        data["challenge_stalled_fresh_session"] = stalled_pre_auth
    return {"ok": False, "error": "Run failed.", "data": data}


def test_fresh_session_run_envelope_carries_typed_session_facts() -> None:
    data: dict[str, object] = {}

    run_execution_module._attach_run_session_facts(
        data,
        used_fresh_run_session=True,
        run_detached_from_chat=False,
        run_ok=False,
        page_evidence=_challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value),
    )

    assert data["used_fresh_run_session"] is True
    assert data["challenge_stalled_fresh_session"] is True


def test_run_envelope_omits_the_challenge_stall_fact_without_a_structured_packet() -> None:
    data: dict[str, object] = {}

    run_execution_module._attach_run_session_facts(
        data,
        used_fresh_run_session=True,
        run_detached_from_chat=False,
        run_ok=False,
        page_evidence=None,
    )

    assert data["used_fresh_run_session"] is True
    assert "challenge_stalled_fresh_session" not in data


def test_passing_fresh_session_run_did_not_stall_on_the_challenge() -> None:
    data: dict[str, object] = {}

    run_execution_module._attach_run_session_facts(
        data,
        used_fresh_run_session=True,
        run_detached_from_chat=False,
        run_ok=True,
        page_evidence=_challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value),
    )

    assert data["challenge_stalled_fresh_session"] is False


def test_a_challenge_wall_remains_an_observation_for_the_model() -> None:
    ctx = _ctx()
    page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value)
    ctx.composition_page_evidence = page_evidence
    run_result = _fresh_session_run_result(page_evidence)

    run_execution_module._record_run_blocks_result(ctx, run_result)

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.reason_code == "runtime_block_failure"
    assert ctx.latest_recorded_build_test_outcome.verdict == "repairable_failure"


@pytest.mark.parametrize("used_fresh_run_session", [False, None])
def test_a_challenge_reply_names_a_fresh_session_only_when_the_run_reported_one(
    used_fresh_run_session: bool | None,
) -> None:
    ctx = _ctx()
    page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value)
    ctx.composition_page_evidence = page_evidence

    run_execution_module._record_run_blocks_result(
        ctx, _fresh_session_run_result(page_evidence, used_fresh_run_session)
    )

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"


@pytest.mark.parametrize(
    ("challenge_kind", "used_fresh_run_session"),
    [(ChallengeKind.CAPTCHA.value, True), (None, None)],
)
def test_a_wall_the_classifier_did_not_name_keeps_the_site_verification_label(
    challenge_kind: str | None,
    used_fresh_run_session: bool | None,
) -> None:
    """A run that stopped at a wall reports the recorded facts on its payload; the reply keeps
    today's wording whether or not the classifier typed the wall."""
    ctx = _ctx()
    page_evidence = _challenge_wall_page_evidence(challenge_kind)
    ctx.composition_page_evidence = page_evidence

    run_execution_module._record_run_blocks_result(
        ctx, _fresh_session_run_result(page_evidence, used_fresh_run_session, stalled_pre_auth=True)
    )

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"


@pytest.mark.parametrize("challenge_kind", [ChallengeKind.CAPTCHA.value, "moon_phase", None])
def test_unclassified_and_captcha_walls_keep_the_site_verification_label(challenge_kind: str | None) -> None:
    ctx = _ctx()
    page_evidence = _challenge_wall_page_evidence(challenge_kind)
    ctx.composition_page_evidence = page_evidence

    run_execution_module._record_run_blocks_result(ctx, _fresh_session_run_result(page_evidence))

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"


def test_a_challenge_wall_seen_on_another_run_cannot_name_this_one() -> None:
    ctx = _ctx()
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_earlier_run")

    run_execution_module._record_run_blocks_result(ctx, _fresh_session_run_result(None))

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"


def test_a_run_that_never_started_does_not_count_as_a_run_of_this_turn() -> None:
    ctx = _ctx()
    preflight_failure: dict[str, object] = {
        "ok": False,
        "error": "Unable to prepare the Copilot test-run snapshot; execution was not started.",
        "data": {"workflow_run_id": None, "overall_status": "failed", "blocks": []},
    }

    run_execution_module._record_run_blocks_result(ctx, preflight_failure)
    assert ctx.block_run_calls_this_turn == 0

    run_execution_module._record_run_blocks_result(ctx, _fresh_session_run_result(_challenge_wall_page_evidence(None)))

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.block_run_calls_this_turn == 1


def test_repeated_challenge_runs_remain_model_owned() -> None:
    ctx = _ctx()
    run_execution_module._record_run_blocks_result(ctx, _fresh_session_run_result(_challenge_wall_page_evidence(None)))
    assert ctx.block_run_calls_this_turn == 1
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None

    run_execution_module._record_run_blocks_result(ctx, _fresh_session_run_result(_challenge_wall_page_evidence(None)))

    assert ctx.block_run_calls_this_turn == 2
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None


_SATISFIABLE_TOTP_PAGE_HTML = (
    "<html><head><title>Two-Factor Authentication</title></head><body>"
    "<p>Complete the challenge to continue.</p>"
    "<form><label for='token'>Authenticator token</label>"
    "<input id='token' name='token' type='text' placeholder='123456' />"
    "<button type='submit' class='btn--login'>Login</button></form></body></html>"
)
_TOTP_VISION_CHALLENGE_SUMMARY = {
    "summary": "A centered Two-Factor Authentication card requests an authenticator token; a Login button is shown.",
    "challenge_detected": True,
    "challenge_kind": "other",
    "challenge_location": "Centered page card",
    "submit_blocked": True,
    "blocked_submit_controls": ["Login button requires successful two-factor authentication"],
}


_TOTP_VISION_OCCLUSION_ONLY_SUMMARY = {
    "summary": "A centered Two-Factor Authentication card is covered by a site verification interstitial.",
    "challenge_detected": True,
    "challenge_kind": "other",
    "challenge_location": "Full-page interstitial",
}
_FAILED_BLOCK_ACTION_TRACE = [
    {
        "action": "null_action",
        "status": "failed",
        "reasoning": None,
        "element": None,
        "description": "page.wait_for_selector(\"button[name='Continue']\", timeout=15000)",
        "code_line": 27,
    },
    {
        "action": "null_action",
        "status": "completed",
        "reasoning": None,
        "element": None,
        "description": "token = credential.otp()",
    },
]


def _failed_run_blocks_result() -> dict[str, Any]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "login_and_read_visitors",
                    "status": "failed",
                    "failure_reason": "Timed out waiting for button[name='Continue']",
                }
            ],
            "action_trace_summary": run_execution_module._summarize_action_trace(_FAILED_BLOCK_ACTION_TRACE),
        },
    }


def _post_run_totp_page_evidence(visual_summary: dict[str, Any]) -> dict[str, Any]:
    merged = merge_visual_composition_evidence(
        parse_composition_html(
            _SATISFIABLE_TOTP_PAGE_HTML,
            inspected_url="https://example.test/login",
            current_url="https://example.test/login",
        ),
        visual_summary=dict(visual_summary),
    )
    return {
        **merged,
        "workflow_run_id": "wr_failed",
        "observed_after_workflow_run": True,
        "source_tool": "inspect_page_for_composition",
    }


_NAMED_CONTROL_OVERLAY_HTML = (Path(__file__).parent / "data" / "click_overlay_named_dismiss.html").read_text()

_LONG_OVERLAY_SELECTOR_ID = "overlay-" + "notice-gate-region-" * 5

_LONG_SELECTOR_OVERLAY_HTML = f"""
<html><body>
  <main><button id="btn-open-statements">Continue to statements</button></main>
  <div class="overlay" id="{_LONG_OVERLAY_SELECTOR_ID}">
    <div class="modal" id="inner-modal" role="dialog" aria-modal="true">
      <h2>Notice</h2>
      <button id="btn-one">Accept</button>
      <button id="btn-two">Decline</button>
      <button id="btn-three">Manage</button>
      <button id="btn-four">Close</button>
    </div>
  </div>
</body></html>
"""


def _overlay_repair_ctx(evidence: dict[str, Any]) -> CopilotContext:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.composition_page_evidence = {
        **evidence,
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_overlay",
    }
    ctx.pending_code_authoring_runtime_repair_context = CodeAuthoringRepairContext(
        block_label="click_continue_and_extract_success",
        reason_code="runtime_block_failure",
        runtime_failure_reason="TimeoutError: Locator.wait_for: Timeout 10000ms exceeded.",
        workflow_run_id="wr_overlay",
    )
    return ctx


def _overlay_page_evidence(html: str) -> dict[str, Any]:
    return parse_composition_html(html, inspected_url="http://localhost/x", current_url="http://localhost/x")


def _obstruction_only_page_evidence() -> dict[str, Any]:
    return {
        "page_obstructions": [
            {
                "kind": "other",
                "source": "vision_summary",
                "visual_location": "Full-page overlay covering the page and its button.",
                "visible_controls": [],
                "underlying_page_blocked": True,
            }
        ],
        "modal_overlays": [],
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
    }


def test_an_overlay_seen_later_in_the_run_replaces_the_earlier_clean_capture() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_test_ok = False

    clean, _ = store_post_run_page_evidence(
        ctx,
        _bounded_failure_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )
    ctx.composition_page_evidence = clean

    overlay, _ = store_post_run_page_evidence(
        ctx,
        {"source_tool": "inspect_page_for_composition", **_obstruction_only_page_evidence()},
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )

    assert overlay.get("page_obstructions"), "the overlay the run actually hit must not be discarded"
    assert ctx.composition_page_evidence.get("page_obstructions")


def test_a_control_only_page_seen_later_in_the_run_replaces_the_earlier_clean_capture() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_test_ok = False

    clean, _ = store_post_run_page_evidence(
        ctx,
        _bounded_failure_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )
    ctx.composition_page_evidence = clean

    control_only, preserved = store_post_run_page_evidence(
        ctx,
        {
            "source_tool": "inspect_page_for_composition",
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "challenge_controls": [],
            "clickable_controls": [{"text": "Continue to statements", "selector": "#continue", "tag": "button"}],
        },
        run_id="wr_failed",
        current_url="https://example.test/app/statements",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )

    assert preserved is False
    assert control_only.get("clickable_controls")
    assert ctx.composition_page_evidence.get("clickable_controls")
    assert not ctx.composition_page_evidence.get("navigation_targets")


def test_a_text_less_control_only_page_does_not_evict_the_earlier_clean_capture() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_test_ok = False

    clean, _ = store_post_run_page_evidence(
        ctx,
        _bounded_failure_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/results",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )
    ctx.composition_page_evidence = clean

    _, preserved = store_post_run_page_evidence(
        ctx,
        _text_less_control_only_page_evidence(),
        run_id="wr_failed",
        current_url="https://example.test/app/statements",
        source_browser_session_id=None,
        run_browser_session_id=None,
    )

    assert preserved is True
    assert ctx.composition_page_evidence.get("navigation_targets")


def test_obstruction_without_dismiss_controls_still_finalizes_a_repair_context() -> None:
    ctx = _overlay_repair_ctx(_obstruction_only_page_evidence())
    assert has_bounded_page_schema(ctx.composition_page_evidence) is False

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.observed_after_workflow_run is True
    assert repair_context.page_form_summaries == []
    assert repair_context.page_obstruction_summaries == [
        "other Full-page overlay covering the page and its button. "
        "obstruction present, no dismiss control found in page evidence"
    ]
    rendered_line = (
        "page_obstructions: other Full-page overlay covering the page and its button. "
        "obstruction present, no dismiss control found in page evidence"
    )
    assert rendered_line in _code_authoring_repair_context_prompt(ctx).splitlines()
    assert "obstruction present, no dismiss control found" in rendered_line


def test_overlay_dismiss_controls_reach_the_repair_prompt_beside_the_runtime_failure() -> None:
    ctx = _overlay_repair_ctx(_overlay_page_evidence(_NAMED_CONTROL_OVERLAY_HTML))

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summaries = repair_context.page_obstruction_summaries
    source_obstructions = ctx.composition_page_evidence["page_obstructions"]
    assert [obstruction.model_dump(exclude_none=True) for obstruction in repair_context.page_obstructions] == (
        [model_visible_composition_evidence(obstruction) for obstruction in source_obstructions]
    )
    assert [
        [candidate.model_dump() for candidate in control.selector_candidates]
        for obstruction in repair_context.page_obstructions
        for control in obstruction.visible_controls
    ] == [
        model_visible_composition_evidence(control)["selector_candidates"]
        for obstruction in source_obstructions
        for control in obstruction["visible_controls"]
    ]
    assert [
        control.identity.model_dump()
        for obstruction in repair_context.page_obstructions
        for control in obstruction.visible_controls
        if control.identity is not None
    ] == [
        control["identity"]
        for obstruction in source_obstructions
        for control in obstruction["visible_controls"]
        if "identity" in control
    ]
    assert len(summaries) == 2
    for summary in summaries:
        assert "Terms" in summary
        assert "Continue" in summary
        assert "#terms-" not in summary
        assert "#accept-terms" not in summary
        assert "#btn-continue" not in summary
    prompt_lines = _code_authoring_repair_context_prompt(ctx).splitlines()
    assert "runtime_failure_reason: TimeoutError: Locator.wait_for: Timeout 10000ms exceeded." in prompt_lines
    assert "observed_after_workflow_run: true" in prompt_lines
    obstruction_line = next(line for line in prompt_lines if line.startswith("page_obstructions:"))
    assert "Terms" in obstruction_line
    assert "Continue" in obstruction_line
    assert "#terms-overlay" not in obstruction_line
    assert "#btn-continue" not in obstruction_line

    ctx.last_code_authoring_repair_context = repair_context.model_copy(update={"selector": "#btn-continue"})
    selector_prompt_lines = _code_authoring_repair_context_prompt(ctx).splitlines()
    assert "selector: #btn-continue" in selector_prompt_lines
    assert obstruction_line in selector_prompt_lines


def test_overlay_selectors_do_not_reach_the_repair_prompt() -> None:
    ctx = _overlay_repair_ctx(_overlay_page_evidence(_LONG_SELECTOR_OVERLAY_HTML))
    long_selector = f"#{_LONG_OVERLAY_SELECTOR_ID}"
    assert len(long_selector) > 100

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summary = repair_context.page_obstruction_summaries[0]
    assert summary.startswith("modal_overlay Notice ")
    assert summary.endswith("Close")
    assert long_selector not in summary
    assert "#btn-four" not in summary
    assert long_selector not in _code_authoring_repair_context_prompt(ctx)


def test_every_obstruction_keeps_dismiss_controls_when_controls_outnumber_the_summary_budget() -> None:
    ctx = _overlay_repair_ctx(_overlay_page_evidence(_LONG_SELECTOR_OVERLAY_HTML))
    long_selector = f"#{_LONG_OVERLAY_SELECTOR_ID}"

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summaries = repair_context.page_obstruction_summaries
    assert len(summaries) == 2
    assert all(label in summaries[1] for label in ("Accept", "Decline", "Manage", "Close"))
    assert long_selector not in " ".join(summaries)
    assert "#inner-modal" not in " ".join(summaries)
    assert all(selector not in " ".join(summaries) for selector in ("#btn-one", "#btn-two", "#btn-three", "#btn-four"))


def test_dismiss_controls_are_reported_without_an_action_or_a_preference() -> None:
    ctx = _overlay_repair_ctx(_overlay_page_evidence(_NAMED_CONTROL_OVERLAY_HTML))
    evidence_before = json.dumps(ctx.composition_page_evidence, sort_keys=True)
    pending_before = ctx.pending_code_authoring_runtime_repair_context

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    for summary, obstruction in zip(
        repair_context.page_obstruction_summaries, ctx.composition_page_evidence["page_obstructions"]
    ):
        source_control_order = [
            control.get("text") or control.get("aria_label") or control.get("title")
            for control in obstruction["visible_controls"]
        ]
        source_control_order = [value for value in source_control_order if isinstance(value, str) and value]
        assert sorted(source_control_order, key=summary.index) == source_control_order
    assert json.dumps(ctx.composition_page_evidence, sort_keys=True) == evidence_before
    assert pending_before is not None
    before = pending_before.model_dump()
    assert {key for key, value in repair_context.model_dump().items() if before[key] != value} <= {
        "current_origin",
        "current_url",
        "current_title",
        "page_evidence_source",
        "rendered_value_excerpt",
        "observed_after_workflow_run",
        "page_form_summaries",
        "page_result_summaries",
        "page_action_summaries",
        "page_challenge_summaries",
        "page_obstruction_summaries",
        "page_obstructions",
        "page_obstruction_omission_notices",
    }


def test_modal_overlay_dismiss_controls_are_read_only_when_page_obstructions_is_absent() -> None:
    ctx = _overlay_repair_ctx(
        {
            "modal_overlays": [
                {
                    "selector": "#gate-modal",
                    "dismiss_controls": [{"text": "Accept All", "selector": "button.accept"}],
                }
            ],
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "challenge_controls": [],
        }
    )

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_obstruction_summaries == ["Accept All"]
    assert [obstruction.model_dump(exclude_none=True) for obstruction in repair_context.page_obstructions] == [
        {
            "selector_candidates": [],
            "visible_controls": [
                {
                    "text": "Accept All",
                    "selector_candidates": [],
                }
            ],
        }
    ]


def test_page_free_of_obstructions_renders_no_obstruction_line() -> None:
    ctx = _overlay_repair_ctx(
        {
            "page_obstructions": [],
            "modal_overlays": [],
            "forms": [{"fields": [], "submit_controls": [{"text": "Continue", "selector": "#btn-continue"}]}],
            "navigation_targets": [],
            "result_containers": [],
            "challenge_controls": [],
        }
    )

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_obstruction_summaries == []
    assert repair_context.page_obstructions == []
    assert "page_obstructions:" not in _code_authoring_repair_context_prompt(ctx)


def test_malformed_canonical_obstruction_is_omitted_with_an_exact_notice() -> None:
    evidence = _obstruction_only_page_evidence()
    evidence["page_obstructions"].append("malformed")
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert len(repair_context.page_obstructions) == 1
    assert repair_context.page_obstruction_omission_notices == [
        "failure.page_state.obstructions omitted: 1 malformed item(s)."
    ]


def test_page_obstruction_summaries_separate_runtime_repair_root_cause_signatures() -> None:
    def signature(summaries: list[str]) -> str | None:
        repair_context = CodeAuthoringRepairContext(
            block_label="click_continue_and_extract_success",
            reason_code="runtime_block_failure",
            observed_after_workflow_run=True,
            page_obstruction_summaries=summaries,
        )
        contract = build_diagnosis_repair_contract(
            source_tool="update_and_run_blocks",
            result=_authoring_repair_result(repair_context),
            ctx=_ctx(),
        )
        return contract.to_trace_data()["root_cause_signature"]

    named_control_signature = signature(["#terms-overlay Continue #btn-continue"])
    textless_signature = signature(
        ["other Full-page overlay obstruction present, no dismiss control found in page evidence"]
    )

    assert named_control_signature is not None
    assert named_control_signature != textless_signature


@pytest.mark.asyncio
async def test_obstruction_only_packet_survives_the_automatic_post_run_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    packet = _obstruction_only_page_evidence()

    async def _read(
        _ctx_arg: CopilotContext, *, run_session_id: str, current_url: str
    ) -> tuple[dict[str, Any], str, None, None]:
        return packet, run_session_id, None, None

    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", _read)

    await run_execution_module._capture_and_store_post_run_page(
        ctx, run_session_id="pbs_run", run_id="wr_overlay", current_url="https://example.test/statements"
    )

    stored = ctx.composition_page_evidence
    assert stored is not None
    assert has_bounded_page_schema(stored) is False
    assert stored["observed_after_workflow_run"] is True
    assert stored["workflow_run_id"] == "wr_overlay"
    assert stored["page_obstructions"] == packet["page_obstructions"]


def test_dispatched_terminal_capture_admits_an_obstruction_only_packet() -> None:
    packet = _obstruction_only_page_evidence()

    assert has_bounded_page_schema(packet) is False
    assert page_evidence_prose_text(packet).strip() != ""
    assert run_execution_module._dispatched_terminal_page_evidence_is_usable(packet) is True


def test_same_run_matcher_keeps_a_stored_obstruction_only_packet() -> None:
    stored = {
        **_obstruction_only_page_evidence(),
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_overlay",
    }

    assert has_bounded_page_schema(stored) is False
    assert post_run_inspection_cleanly_matches(stored, "wr_overlay") is True
    assert post_run_inspection_cleanly_matches(stored, "wr_other") is False


def test_a_long_control_text_never_leaks_the_dismiss_selector() -> None:
    control_selector = "#" + "dismiss-the-full-page-notice-" * 5
    ctx = _overlay_repair_ctx(
        {
            "page_obstructions": [
                {
                    "kind": "modal_overlay",
                    "selector": "#" + "notice-gate-region-" * 8,
                    "visual_location": "Full-viewport notice covering the statements card. " * 4,
                    "visible_controls": [
                        {"text": "Accept and continue past this notice. " * 6, "selector": control_selector}
                    ],
                }
            ],
            "modal_overlays": [],
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "challenge_controls": [],
        }
    )

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summary = repair_context.page_obstruction_summaries[0]
    assert len(summary) <= OBSTRUCTION_SUMMARY_MAX_CHARS
    assert control_selector not in summary
    assert "Accept and continue past this notice." in summary
    obstruction_lines = [
        line
        for line in _code_authoring_repair_context_prompt(ctx).splitlines()
        if line.startswith("page_obstructions:")
    ]
    assert obstruction_lines
    assert control_selector not in obstruction_lines[0]


_CHALLENGE_PAGE_URL = "https://sso.example.test/challenge"


def _precedence_ctx(monkeypatch: pytest.MonkeyPatch) -> CopilotContext:
    """A run whose page evidence shows a captcha this deployment can clear."""
    ctx = _ctx()
    _with_solver(ctx, True)
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_mixed")
    return ctx


def _with_solver(ctx: CopilotContext, available: bool, *, url: str = _CHALLENGE_PAGE_URL) -> None:
    """Set the answer the run path resolves once, and the page it was resolved against."""
    ctx.captcha_solver_available = available
    ctx.captcha_solver_available_for_url = url


def test_a_clearable_captcha_alone_yields_the_repair_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _precedence_ctx(monkeypatch)
    data = {
        "workflow_run_id": "wr_mixed",
        "failure_categories": [
            {"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state", "confidence_float": 1.0}
        ],
    }

    assert runtime_authoring_repair._result_has_terminal_or_ask_precedence(ctx, data, {"ok": False}) is False


def test_a_clearable_captcha_is_released_without_a_post_run_packet() -> None:
    """Only one authoring policy mints a run-matched post-run packet, and the stop this releases fires
    on every policy. A release that needed the packet would be unreachable exactly where the stop
    still fires, so an unstamped observation has to be enough."""
    ctx = _ctx()
    _with_solver(ctx, True)
    ctx.composition_page_evidence = {
        "current_url": "https://sso.example.test/challenge",
        "challenge_state": {"detected": True, CHALLENGE_KIND_KEY: ChallengeKind.CAPTCHA.value},
    }

    assert runtime_authoring_repair.run_challenge_is_runtime_clearable(ctx, "wr_standard_policy") is True


def test_a_clearable_captcha_leaves_the_repair_path_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The halt is only one of the routes that stops the turn: diagnosis must not route to STOP
    either, or the turn ends anyway with no blocker, no halt and no repair context."""
    ctx = _ctx()
    _with_solver(ctx, True)
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_captcha")

    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_captcha",
                "overall_status": "failed",
                "failure_reason": "blocked by a verification challenge",
                "failure_categories": [
                    {"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state", "confidence_float": 1.0}
                ],
            },
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action != RepairNextAction.STOP


def test_an_answer_resolved_for_another_page_does_not_release_this_one() -> None:
    """The gate behind the cached answer is a domain denylist, so an answer obtained against an
    allowed page must not authorise a later one that may not be."""
    ctx = _ctx()
    _with_solver(ctx, True, url="https://allowed.example.test/login")
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_other")

    assert runtime_authoring_repair.run_challenge_is_runtime_clearable(ctx, "wr_other") is False


def test_an_unclearable_challenge_still_routes_diagnosis_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    _with_solver(ctx, False)
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_captcha")

    contract = build_diagnosis_repair_contract(
        source_tool="run_blocks_and_collect_debug",
        result={
            "ok": False,
            "data": {
                "workflow_run_id": "wr_captcha",
                "overall_status": "failed",
                "failure_reason": "blocked by a verification challenge",
                "failure_categories": [
                    {"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state", "confidence_float": 1.0}
                ],
            },
        },
        ctx=ctx,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action == RepairNextAction.STOP


def test_an_unreachable_sandbox_still_stops_even_beside_a_clearable_captcha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-run page inspection runs whatever failed, so a solvable captcha routinely shares a run
    with an unrelated stop condition. No edit to the block can reach a sandbox that is not there."""
    ctx = _precedence_ctx(monkeypatch)
    data = {
        "workflow_run_id": "wr_mixed",
        "failure_categories": [
            {"category": "UNRECOVERABLE_TOOL_ERROR", "evidence_source": "challenge_state", "confidence_float": 1.0},
            {"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state", "confidence_float": 1.0},
        ],
    }

    assert runtime_authoring_repair._result_has_terminal_or_ask_precedence(ctx, data, {"ok": False}) is True


def test_an_unresolved_solver_question_keeps_the_wall() -> None:
    """A path that never resolved the capability must not read as clearable: an unanswered question
    is the deployment whose solver is switched off, not the one whose solver works."""
    ctx = _ctx()
    ctx.composition_page_evidence = _challenge_wall_page_evidence(ChallengeKind.CAPTCHA.value, run_id="wr_captcha")
    assert ctx.captcha_solver_available is None

    assert runtime_authoring_repair.run_challenge_is_runtime_clearable(ctx, "wr_captcha") is False


def test_an_unstamped_packet_still_keeps_the_wall_without_a_solver() -> None:
    ctx = _ctx()
    _with_solver(ctx, False)
    ctx.composition_page_evidence = {
        "current_url": "https://sso.example.test/challenge",
        "challenge_state": {"detected": True, CHALLENGE_KIND_KEY: ChallengeKind.CAPTCHA.value},
    }

    assert runtime_authoring_repair.run_challenge_is_runtime_clearable(ctx, "wr_standard_policy") is False


def test_unbound_credentials_still_ask_even_beside_a_clearable_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _precedence_ctx(monkeypatch)
    data = {"workflow_run_id": "wr_mixed", "skip_reason": "workflow_credential_inputs_unbound"}

    assert runtime_authoring_repair._result_has_terminal_or_ask_precedence(ctx, data, {"ok": False}) is True


_OBSERVED_FIELD_SECRET = "s3cret-not-a-real-password"


def _observed_state_page_evidence() -> dict[str, Any]:
    return {
        "current_url": "https://example.test/booking",
        "forms": [
            {
                "fields": [
                    {
                        "label": "Depart date",
                        "type": "date",
                        "value": "",
                        "observed_value": "2026-09-14",
                        "identity": {"tag": "input"},
                    },
                    {
                        "label": "Cabin",
                        "type": "select",
                        "identity": {"tag": "select"},
                        "options": [
                            {"text": "Economy", "value": "economy", "selected": True, "observed_selected": False},
                            {"text": "Business", "value": "business", "selected": False, "observed_selected": True},
                        ],
                    },
                    {
                        "label": "Add insurance",
                        "type": "checkbox",
                        "value": "yes",
                        "checked": False,
                        "observed_checked": True,
                        "identity": {"tag": "input"},
                    },
                    {
                        "label": "Password",
                        "type": "password",
                        "value": _OBSERVED_FIELD_SECRET,
                        "filled": True,
                        "identity": {"tag": "input"},
                    },
                ],
                "submit_controls": [{"text": "Book", "disabled": False}],
            }
        ],
    }


def test_repair_prompt_carries_the_field_state_the_run_produced_not_the_markup_default() -> None:
    ctx = _overlay_repair_ctx(_observed_state_page_evidence())

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == [
        "Depart date date 2026-09-14",
        "Cabin select Business",
        "Add insurance checkbox checked",
        "Password password",
        "Book enabled",
    ]
    assert "page_forms: Depart date date 2026-09-14" in _code_authoring_repair_context_prompt(ctx)


def test_a_password_value_never_reaches_the_repair_prompt_beside_the_observed_field_state() -> None:
    ctx = _overlay_repair_ctx(_observed_state_page_evidence())

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert _OBSERVED_FIELD_SECRET not in repair_context.model_dump_json()
    assert _OBSERVED_FIELD_SECRET not in _code_authoring_repair_context_prompt(ctx)


def test_an_observed_value_on_a_secret_typed_field_is_still_not_rendered() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "Password",
            "type": "password",
            "observed_value": _OBSERVED_FIELD_SECRET,
            "identity": {"tag": "input"},
        },
        {
            "label": "Notes",
            "type": "textarea",
            "observed_value": _OBSERVED_FIELD_SECRET,
            "identity": {"tag": "textarea"},
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Password password", "Notes textarea"]
    assert _OBSERVED_FIELD_SECRET not in _code_authoring_repair_context_prompt(ctx)


def test_a_field_whose_declared_type_lies_about_its_tag_renders_no_observed_state() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "Notes",
            "type": "date",
            "observed_value": _OBSERVED_FIELD_SECRET,
            "identity": {"tag": "textarea"},
        },
        {
            "label": "Agree",
            "type": "checkbox",
            "observed_checked": True,
            "identity": {"tag": "textarea"},
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Notes date", "Agree checkbox"]
    assert _OBSERVED_FIELD_SECRET not in _code_authoring_repair_context_prompt(ctx)


def test_a_checkbox_state_is_not_rendered_through_the_disabled_key_mapping() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "Add insurance",
            "type": "checkbox",
            "checked": False,
            "observed_checked": False,
            "identity": {"tag": "input"},
        }
    ]
    evidence["forms"][0]["submit_controls"] = []

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(_overlay_repair_ctx(evidence))

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Add insurance checkbox unchecked"]


def test_a_select_typed_field_whose_real_tag_is_not_select_renders_no_observed_state() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "Departure",
            "type": "select",
            "identity": {"tag": "div"},
            "options": [{"text": "2026-09-14", "value": "2026-09-14", "observed_selected": True}],
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Departure select"]


def test_a_real_select_still_renders_its_observed_option() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "Departure",
            "type": "select",
            "identity": {"tag": "select"},
            "options": [{"text": "2026-09-14", "value": "2026-09-14", "observed_selected": True}],
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Departure select 2026-09-14"]


def test_an_admitted_but_empty_observed_value_is_distinguishable_from_no_observation() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {"label": "Departure", "type": "date", "observed_value": "", "identity": {"tag": "input"}},
        {"label": "Return", "type": "date", "identity": {"tag": "input"}},
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == ["Departure date empty", "Return date"]


def test_a_long_label_cannot_truncate_the_observed_state_away() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {
            "label": "D" * 200,
            "type": "select",
            "identity": {"tag": "select"},
            "options": [{"text": "Departing " + "x" * 40 + "2026-09-14", "observed_selected": True}],
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries[0].endswith("2026-09-14")


def test_a_filled_control_is_not_displaced_by_controls_that_observed_nothing() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {"label": "Origin station", "type": "text", "identity": {"tag": "input"}},
        {"label": "Destination station", "type": "text", "identity": {"tag": "input"}},
        {"label": "Departure date", "type": "date", "observed_value": "", "identity": {"tag": "input"}},
    ] + [
        {"label": label, "type": "checkbox", "observed_checked": False, "identity": {"tag": "input"}}
        for label in ("Aisle seat", "Window seat", "Extra legroom", "Travel insurance", "Seat alerts")
    ]
    evidence["forms"][0]["submit_controls"] = [{"text": "Continue to payment", "disabled": False}]
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summaries = repair_context.page_form_summaries
    assert summaries[0] == "Origin station text"
    assert summaries[1] == "Destination station text"
    assert "Departure date date empty" in summaries


def test_a_control_that_observed_something_still_leads_the_summary_cap() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {"label": f"Filler {index}", "type": "text", "identity": {"tag": "input"}} for index in range(8)
    ] + [
        {"label": "Departure", "type": "date", "observed_value": "2026-09-14", "identity": {"tag": "input"}},
        {"label": "Aisle seat", "type": "checkbox", "observed_checked": True, "identity": {"tag": "input"}},
    ]
    evidence["forms"][0]["submit_controls"] = [{"text": "Continue to payment", "disabled": False}]
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    summaries = repair_context.page_form_summaries
    assert summaries[:2] == ["Departure date 2026-09-14", "Aisle seat checkbox checked"]
    assert summaries[2:] == ["Filler 0 text", "Filler 1 text", "Filler 2 text"]


def test_a_select_on_its_blank_leading_option_does_not_displace_filled_controls() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {"label": "Origin station", "type": "text", "identity": {"tag": "input"}},
        {"label": "Destination station", "type": "text", "identity": {"tag": "input"}},
        {"label": "Passenger name", "type": "text", "identity": {"tag": "input"}},
    ] + [
        {
            "label": label,
            "type": "select",
            "identity": {"tag": "select"},
            "options": [
                {"text": "", "value": "", "observed_selected": True},
                {"text": option, "value": option},
            ],
        }
        for label, option in (("Departure day", "1"), ("Departure month", "Jan"), ("Departure year", "2026"))
    ]
    evidence["forms"][0]["submit_controls"] = [{"text": "Continue to payment", "disabled": False}]
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries == [
        "Origin station text",
        "Destination station text",
        "Passenger name text",
        "Departure day select",
        "Departure month select",
    ]


def test_a_select_on_a_real_option_still_leads_the_summary_cap() -> None:
    evidence = _observed_state_page_evidence()
    evidence["forms"][0]["fields"] = [
        {"label": f"Filler {index}", "type": "text", "identity": {"tag": "input"}} for index in range(6)
    ] + [
        {
            "label": "Departure month",
            "type": "select",
            "identity": {"tag": "select"},
            "options": [{"text": "", "value": ""}, {"text": "Jan", "value": "1", "observed_selected": True}],
        },
    ]
    evidence["forms"][0]["submit_controls"] = []
    ctx = _overlay_repair_ctx(evidence)

    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert repair_context is not None
    assert repair_context.page_form_summaries[0] == "Departure month select Jan"
