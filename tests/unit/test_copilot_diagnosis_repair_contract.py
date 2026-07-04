from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    recorded_outcome_from_author_time_reject,
)
from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    RepairLoopState,
    RepairNextAction,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.enforcement import latest_diagnosis_contract_satisfies_goal
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    RecordedRunOutcome,
)
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    finalize_runtime_authoring_repair_context_from_page_observation,
    inject_runtime_authoring_repair_context,
    post_run_inspection_cleanly_matches,
    record_pending_runtime_authoring_repair_context,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.composition_capture import store_post_run_page_evidence
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="Fix the workflow with password=hunter2",
        turn_intent=TurnIntent(
            mode=TurnIntentMode.EDIT,
            user_goal="Fix the workflow with password=hunter2",
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        ),
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
    ) == (DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT, RepairNextAction.ASK, ["may_answer_without_mutation"])


def test_authoring_repair_contexts_have_distinct_structural_root_cause_signatures() -> None:
    ambiguous = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='View / Download']",
    )
    sandbox = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["row_text", "confirmation_number"],
    )
    sandbox_reordered = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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
    sandbox_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(sandbox),
        ctx=_ctx(),
    )
    sandbox_reordered_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(sandbox_reordered),
        ctx=_ctx(),
    )
    synthesized_binding_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(synthesized_binding),
        ctx=_ctx(),
    )

    ambiguous_signature = ambiguous_contract.to_trace_data()["root_cause_signature"]
    sandbox_signature = sandbox_contract.to_trace_data()["root_cause_signature"]
    synthesized_binding_signature = synthesized_binding_contract.to_trace_data()["root_cause_signature"]
    assert ambiguous_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert sandbox_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert synthesized_binding_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert synthesized_binding_contract.repair_decision.target_blocks == ["retrieve_document_link"]
    assert ambiguous_signature is not None
    assert sandbox_signature is not None
    assert synthesized_binding_signature is not None
    assert ambiguous_signature != sandbox_signature
    assert synthesized_binding_signature not in {ambiguous_signature, sandbox_signature}
    assert (
        synthesized_binding_contract.diagnosis_result.root_cause_identity.error_class
        == "code_authoring_synthesized_parameter_binding_ambiguous"
    )
    assert sandbox_reordered_contract.to_trace_data()["root_cause_signature"] == sandbox_signature


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
        current_url_present=True,
        current_title_present=True,
        page_evidence_source="inspect_page_for_composition",
        observed_after_workflow_run=True,
        page_form_summaries=["text input labeled Search"],
        page_result_summaries=["no results container is visible"],
        page_action_summaries=["button Search is disabled"],
    )
    changed_page = base.model_copy(update={"page_result_summaries": ["results table is visible"]})

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

    assert base_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert (
        base_contract.to_trace_data()["root_cause_signature"]
        != changed_page_contract.to_trace_data()["root_cause_signature"]
    )
    assert base_contract.diagnosis_result.root_cause_identity.error_class == (
        "code_authoring_runtime_block_failure_timeout_waiting_for_selector"
    )


def test_repair_loop_state_resets_when_authoring_repair_context_identity_changes() -> None:
    ctx = _ctx()
    ambiguous = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='View / Download']",
    )
    sandbox = CodeAuthoringRepairContext(
        block_label="retrieve_document_link",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["confirmation_number", "row_text"],
    )

    for expected_count in (1, 2):
        contract = build_diagnosis_repair_contract(
            source_tool="update_and_run_blocks",
            result=_authoring_repair_result(ambiguous),
            ctx=ctx,
        )
        run_execution_module._update_repair_loop_state(ctx, contract)
        assert contract.repair_loop_state.consecutive_identical_repair_count == expected_count
        assert contract.repair_loop_state.ceiling_reached is False

    sandbox_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(sandbox),
        ctx=ctx,
    )
    run_execution_module._update_repair_loop_state(ctx, sandbox_contract)

    assert sandbox_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert sandbox_contract.repair_loop_state.consecutive_identical_repair_count == 1
    assert sandbox_contract.repair_loop_state.ceiling_reached is False
    assert getattr(ctx, "blocker_signal", None) is None


def _uncovered_output_turn_state(output_path: str) -> SimpleNamespace:
    criterion = CompletionCriterion(id=output_path, outcome="the value is captured", output_path=output_path)
    return SimpleNamespace(decision=SimpleNamespace(criteria=(criterion,)))


def _uncovered_output_author_reject(output_path: str) -> RecordedBuildTestOutcome:
    return recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        block_labels=["extract_order"],
        structural_payload={
            "reason_code": "recorded_outcome_missing_output_coverage",
            "missing_output_paths": [output_path],
        },
        missing_requested_output_facts=[{"output_path": output_path, "output_root": output_path.split(".", 1)[0]}],
    )


def _run_repair_loop_state(ctx: CopilotContext) -> RepairLoopState:
    repair_context = CodeAuthoringRepairContext(
        block_label="extract_order",
        reason_code="runtime_block_failure",
        runtime_failure_reason="output missing",
    )
    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_authoring_repair_result(repair_context),
        ctx=ctx,
    )
    run_execution_module._update_repair_loop_state(ctx, contract)
    return contract.repair_loop_state


def test_uncovered_output_author_reject_reopens_once_then_counts_to_ceiling() -> None:
    ctx = _ctx()
    ctx.completion_criteria_turn_state = _uncovered_output_turn_state("output.document_name")
    ctx.latest_recorded_build_test_outcome = _uncovered_output_author_reject("output.document_name")

    first = _run_repair_loop_state(ctx)
    assert first.consecutive_identical_repair_count == 0
    assert first.ceiling_reached is False
    assert ctx.synthesized_block_reopened_for_output_coverage is True
    assert ctx.consecutive_non_converging_repair_count == 0

    counts = [_run_repair_loop_state(ctx).consecutive_identical_repair_count for _ in range(3)]
    assert counts == [1, 2, 3]
    assert counts[-1] == settings.COPILOT_REPAIR_CEILING_CONSECUTIVE_IDENTICAL
    assert isinstance(getattr(ctx, "blocker_signal", None), CopilotToolBlockerSignal)


def test_persisted_run_outcome_is_not_excluded_from_repair_streak() -> None:
    ctx = _ctx()
    ctx.completion_criteria_turn_state = _uncovered_output_turn_state("output.document_name")
    ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="outcome_not_demonstrated",
        structural_failure_identity="completion:unsatisfied-output",
        missing_requested_output_facts=[{"output_path": "output.document_name", "output_root": "output"}],
    )
    state = _run_repair_loop_state(ctx)
    assert state.consecutive_identical_repair_count == 1
    assert ctx.synthesized_block_reopened_for_output_coverage is False


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
        "current_url": "https://example.test/search?case=secret",
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
    assert repair_context.runtime_failure_class == "timeout_waiting_for_selector"
    assert repair_context.current_origin == "https://example.test"
    assert repair_context.current_url_present is True
    assert repair_context.current_title_present is True
    assert repair_context.page_evidence_source == "inspect_page_for_composition"
    assert repair_context.observed_after_workflow_run is True
    assert repair_context.page_form_summaries == ["Search #search", "Go button.search disabled"]
    assert repair_context.page_result_summaries == ["#results No matching records"]
    assert repair_context.page_action_summaries == ["Next page a.next"]
    assert "case=secret" not in repair_context.model_dump_json()


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
    assert repair_context.runtime_failure_class
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


def test_runtime_authoring_repair_context_suppressed_for_authority_ask_and_state_stop() -> None:
    ask_ctx = _ctx()
    ask_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ask_ctx.turn_intent.authority.may_update_workflow = False
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

    assert "authoring_repair_context" not in ask_result["data"]
    assert ask_ctx.last_code_authoring_repair_context is None

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
        "current_url": "https://user:secret@example.test/search?password=hunter2",
        "page_title": "Search",
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


def test_schema_incompatibility_failure_type_stops_without_repair() -> None:
    # SKY-11380: the typed schema-incompatibility reject must route to STOP, never repair,
    # so the agent reports the mismatch instead of churning toward repair_ceiling_reached.
    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "STOP: the edited extraction_schema declares field(s) [shoebox] that map to no output.",
            "data": {
                "failure_type": "schema_incompatibility",
                "workflow_updated": False,
                "schema_incompatibility": {
                    "block_label": "capture_row",
                    "incompatible_paths": ["shoebox"],
                    "known_output_paths": ["order_date", "order_total"],
                },
            },
        },
        ctx=_ctx(),
        workflow_updated=False,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SCHEMA_INCOMPATIBILITY
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.repair_decision.next_action != RepairNextAction.REPAIR
    assert contract.repair_decision.target_blocks == []


def test_judge_confirmed_suspicious_success_forces_no_change() -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True
    ctx.completion_verification_result = _satisfied_completion_verification()
    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_verified",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert contract.diagnosis_result.missing_context == []
    assert contract.verification_result.remaining_blocker is None
    trace = contract.to_trace_data()
    assert trace["failure_type"] == "no_failure"
    assert trace["missing_context"] == []


def test_verified_success_ignores_incidental_login_prose_without_structured_blocker() -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True
    ctx.completion_verification_result = _satisfied_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "error": "The extracted public instructions mention login credentials, but no login was attempted.",
            "data": {
                "workflow_run_id": "wr_verified_login_text",
                "overall_status": "completed",
                "frontier_start_label": "extract",
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
            },
        },
        ctx=ctx,
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert contract.verification_result.remaining_blocker is None


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
def test_clean_run_with_present_unsatisfied_completion_verification_fails_safe(
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

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert contract.verification_result.remaining_blocker is not None


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
    assert contract.repair_decision.completion_check == "No repair selected; completion remains unverified."
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert latest_diagnosis_contract_satisfies_goal(ctx) is False


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


def test_first_pass_contradiction_does_not_satisfy_latest_diagnosis_contract() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = _contradictory_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result=_clean_completed_result(),
        ctx=ctx,
        workflow_updated=True,
    )
    ctx.latest_diagnosis_repair_contract = contract

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert latest_diagnosis_contract_satisfies_goal(ctx) is False


def test_failed_run_with_satisfied_completion_verification_has_no_repair_or_blocker() -> None:
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
                "failure_categories": [{"category": "OUTCOME_UNVERIFIED"}],
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

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert contract.verification_result.remaining_blocker is None
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
    assert contract.repair_decision.target_blocks == ["login"]
    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    assert contract.repair_decision.next_action != RepairNextAction.ASK


def test_contradictory_completion_auth_evidence_repairs_frontier_block() -> None:
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
                        "category": "OUTCOME_UNVERIFIED",
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
    assert contract.repair_decision.target_blocks == ["login"]
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False
    assert contract.diagnosis_result.suspected_failure_type != DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
    assert contract.repair_decision.next_action != RepairNextAction.ASK


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


def test_result_unresolved_symbol_context_prefers_repair_over_credential_ask() -> None:
    ctx = _ctx()
    repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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
    assert "may_update_workflow" in contract.repair_decision.required_authority
    assert contract.repair_decision.target_blocks == ["create_request"]
    assert contract.to_trace_data()["next_action"] == "repair"


def test_stale_stored_unresolved_symbol_context_does_not_override_credential_ask() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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
    assert contract.repair_decision.required_authority == ["may_answer_without_mutation"]


def test_non_credential_unresolved_name_result_repairs_instead_of_credential_ask() -> None:
    ctx = _ctx()
    repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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


def test_missing_credential_without_unresolved_symbol_context_still_asks() -> None:
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
    assert contract.repair_decision.required_authority == ["may_answer_without_mutation"]


def test_unresolved_symbol_context_does_not_preempt_terminal_challenge_stop() -> None:
    ctx = _ctx()
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="create_request",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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
                "failure_categories": [{"category": "ANTI_BOT_DETECTION"}],
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


def test_active_run_terminal_evidence_contract_stops_without_marking_workflow_success() -> None:
    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": "Active run terminal evidence was observed.",
            "data": {
                "workflow_run_id": "wr_active",
                "overall_status": "canceled",
                "active_run_terminal_evidence_detected": True,
                "active_run_terminal_completion_verification": {
                    "status": "evaluated",
                    "criterion_count": 1,
                    "satisfied_count": 1,
                    "fully_satisfied": True,
                    "reason_codes": ["evidence_confirms"],
                },
                "failure_categories": [{"category": "ACTIVE_RUN_TERMINAL_EVIDENCE"}],
            },
        },
        ctx=_ctx(),
        workflow_updated=True,
    )

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.ACTIVE_RUN_TERMINAL_EVIDENCE
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True
    assert "not verified end-to-end" in contract.repair_decision.proposed_change_summary


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
                "failure_categories": [{"category": "ANTI_BOT_DETECTION"}],
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
                "failure_categories": [{"category": "ANTI_BOT_DETECTION"}],
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
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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
    ctx.turn_intent.user_goal = "Fix https://example.test/account?id=secret now"

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
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
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


@pytest.mark.asyncio
async def test_update_workflow_failure_records_diagnosis_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot import tools as tools_module

    error = (
        "Unable to impose synthesized code block: dropped scout interaction 0 from `click` (ambiguous_bare_selector)."
    )
    recorded: list[tuple[str, dict[str, object], str]] = []

    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_request_policy_allows_credential_deferred_draft", lambda *args: False)
    monkeypatch.setattr(tools_module, "_update_and_run_blocks_composition_evidence_precheck", lambda *args: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr(tools_module, "_record_workflow_update_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_update_workflow", AsyncMock(return_value={"ok": False, "error": error}))

    def fake_record_contract(ctx: CopilotContext, *, source_tool: str, result: dict[str, object]) -> None:
        contract = build_diagnosis_repair_contract(source_tool=source_tool, result=result, ctx=ctx)
        recorded.append((source_tool, result, contract.to_trace_data()["root_cause_error_class"]))

    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", fake_record_contract)

    result = await tools_module.update_workflow_tool.on_invoke_tool(
        SimpleNamespace(context=_ctx(), tool_name="update_workflow"),
        json.dumps({"workflow_yaml": "title: Test\nworkflow_definition:\n  parameters: []\n  blocks: []\n"}),
    )

    assert json.loads(result)["ok"] is False
    assert recorded == [
        ("update_workflow", {"ok": False, "error": error}, "code_block_synthesis_ambiguous_bare_selector")
    ]


def test_diagnosis_tool_error_preserves_terminal_challenge_blocker_category() -> None:
    from skyvern.forge.sdk.copilot.tools.run_execution import _diagnosis_repair_tool_error

    ctx = _ctx()
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="terminal challenge",
        user_facing_reason="The page is gated by a site verification challenge.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blocked_tool="update_and_run_blocks",
        extra={
            "run_outcome_reason_code": TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
            "evidence_source": "page_evidence",
            "evidence_reason": "human verification requires human verification",
        },
    )

    payload = json.loads(_diagnosis_repair_tool_error(ctx, "update_and_run_blocks", "terminal challenge"))

    assert payload["data"]["failure_categories"][0]["category"] == "ANTI_BOT_DETECTION"
    assert ctx.latest_diagnosis_repair_contract is not None
    assert (
        ctx.latest_diagnosis_repair_contract.diagnosis_result.suspected_failure_type
        == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    )
    assert ctx.latest_diagnosis_repair_contract.repair_decision.next_action == RepairNextAction.STOP


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


def test_post_run_failure_page_store_mark_inject_grounds_repair_without_finalizing_early() -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER

    stored = store_post_run_page_evidence(
        ctx, _bounded_failure_page_evidence(), run_id="wr_failed", current_url="https://example.test/app/results"
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
    assert any("button.icon-btn" in summary for summary in grounded)
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

    monkeypatch.setattr(run_execution_module, "_capture_composition_evidence", fake_capture)

    await run_execution_module._capture_and_store_post_run_failure_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app/results"
    )

    evidence = ctx.composition_page_evidence
    assert isinstance(evidence, dict)
    assert evidence["workflow_run_id"] == "wr_failed"
    assert evidence["observed_after_workflow_run"] is True
    assert post_run_inspection_cleanly_matches(evidence, "wr_failed")
    assert ctx.page_inspection_calls_this_turn == 0
    assert ctx.browser_session_id is None


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

    monkeypatch.setattr(run_execution_module, "_capture_composition_evidence", fake_capture)

    await run_execution_module._capture_and_store_post_run_failure_page(
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

    monkeypatch.setattr(run_execution_module, "_capture_composition_evidence", fake_capture)

    await run_execution_module._capture_and_store_post_run_failure_page(
        ctx, run_session_id="run_session", run_id="wr_failed", current_url="https://example.test/app"
    )
    assert ctx.composition_page_evidence is clean
