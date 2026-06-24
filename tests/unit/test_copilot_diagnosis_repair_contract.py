from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    RepairNextAction,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
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


def _satisfied_completion_verification() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
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
        block_label="retrieve_resale_demand_document_link",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='View / Download']",
    )
    sandbox = CodeAuthoringRepairContext(
        block_label="retrieve_resale_demand_document_link",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["row_text", "confirmation_number"],
    )
    sandbox_reordered = CodeAuthoringRepairContext(
        block_label="retrieve_resale_demand_document_link",
        reason_code=SANDBOX_UNRESOLVED_NAME_REASON_CODE,
        unresolved_names=["confirmation_number", "row_text"],
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

    ambiguous_signature = ambiguous_contract.to_trace_data()["root_cause_signature"]
    sandbox_signature = sandbox_contract.to_trace_data()["root_cause_signature"]
    assert ambiguous_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert sandbox_contract.repair_decision.next_action == RepairNextAction.REPAIR
    assert ambiguous_signature is not None
    assert sandbox_signature is not None
    assert ambiguous_signature != sandbox_signature
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


def test_repair_loop_state_resets_when_authoring_repair_context_identity_changes() -> None:
    ctx = _ctx()
    ambiguous = CodeAuthoringRepairContext(
        block_label="retrieve_resale_demand_document_link",
        reason_code="ambiguous_bare_selector",
        selector="button",
        refiner_selector="xpath=//button[normalize-space()='View / Download']",
    )
    sandbox = CodeAuthoringRepairContext(
        block_label="retrieve_resale_demand_document_link",
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


def test_no_change_contracts_do_not_carry_remaining_blocker() -> None:
    satisfied_ctx = _ctx()
    satisfied_ctx.completion_verification_result = _satisfied_completion_verification()
    partial_ctx = _ctx()
    partial_ctx.completion_verification_result = _satisfied_completion_verification()

    contracts = [
        build_diagnosis_repair_contract(
            source_tool="update_and_run_blocks",
            result=_clean_completed_result(),
            ctx=satisfied_ctx,
            workflow_updated=True,
        ),
        build_diagnosis_repair_contract(
            source_tool="run_blocks_and_collect_debug",
            result={"ok": True, "data": {"workflow_run_id": "wr_4", "overall_status": "completed", "blocks": []}},
            ctx=_ctx(),
        ),
        build_diagnosis_repair_contract(
            source_tool="update_and_run_blocks",
            result={
                "ok": False,
                "error": "Completion verification confirmed the requested outcome despite partial run status.",
                "data": {
                    "workflow_run_id": "wr_partial_verified",
                    "overall_status": "failed",
                    "frontier_start_label": "extract",
                    "failure_categories": [{"category": "OUTCOME_UNVERIFIED"}],
                    "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "failed"}],
                },
            },
            ctx=partial_ctx,
            workflow_updated=True,
        ),
    ]

    for contract in contracts:
        assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
        assert contract.verification_result.remaining_blocker is None


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


def test_anti_bot_suspicious_success_contract_stops_instead_of_repairing() -> None:
    ctx = _ctx()
    ctx.last_test_suspicious_success = True
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.last_test_failure_reason = "Run completed, but extracted data reported a blocker: Verify you are human"
    ctx.completion_verification_result = _satisfied_completion_verification()

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


def test_terminal_challenge_preempts_clean_run_even_with_satisfied_completion_verification() -> None:
    ctx = _ctx()
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.last_test_failure_reason = "Run completed, but extracted data reported a blocker: Verify you are human"
    ctx.completion_verification_result = _satisfied_completion_verification()

    contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": True,
            "data": {
                "workflow_run_id": "wr_blocked_clean",
                "overall_status": "completed",
                "failure_reason": ctx.last_test_failure_reason,
                "failure_categories": [{"category": "ANTI_BOT_DETECTION"}],
                "blocks": [{"label": "extract", "block_type": "EXTRACTION", "status": "completed"}],
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


def test_challenge_category_preempts_clean_run_ok_contract() -> None:
    ctx = _ctx()
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
    assert contract.verification_result.user_goal_satisfied is False


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
