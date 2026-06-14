from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    RepairNextAction,
    build_diagnosis_repair_contract,
)
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
        binding_error.diagnosis_result.suspected_failure_type,
        binding_error.repair_decision.next_action,
    ) == (
        DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
        RepairNextAction.ASK,
        DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT,
        RepairNextAction.ASK,
    )


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

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.SUSPICIOUS_SUCCESS
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is False
    assert "Verify you are human" in contract.verification_result.remaining_blocker


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
