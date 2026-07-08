from __future__ import annotations

from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot.agent import _recorded_build_test_outcome_prompt
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    arm_recorded_outcome_grounding_requirement,
    authored_block_signatures_from_workflow,
    maybe_satisfy_recorded_outcome_grounding_requirement,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.blockers import _tool_loop_error
from skyvern.forge.sdk.copilot.tools.run_execution import _update_repair_loop_state
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _commit_recorded_outcome_early_terminal,
    _record_author_time_reject_outcome,
    _record_code_authoring_guardrail_reject,
    _recorded_outcome_convergence_reject,
)
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode

_OWNING_BLOCK_WORKFLOW = """
title: Registry lookup
workflow_definition:
  blocks:
  - block_type: code
    label: search_records
    code: |
      return {"records": [{"npi": "123"}]}
"""

_OWNING_BLOCK_WORKFLOW_DESCRIPTION_EDIT = """
title: Registry lookup
workflow_definition:
  blocks:
  - block_type: code
    label: search_records
    description: Human-facing note adjusted; steps identical
    code: |
      return {"records": [{"npi": "123"}]}
"""

_OWNING_BLOCK_WORKFLOW_RENAMED = """
title: Registry lookup
workflow_definition:
  blocks:
  - block_type: code
    label: renamed_records
    code: |
      return {"records": [{"npi": "123"}]}
"""

_OWNING_BLOCK_WORKFLOW_SIBLING_MOVE = """
title: Registry lookup
workflow_definition:
  parameters:
  - key: added_param
    parameter_type: workflow
  blocks:
  - block_type: code
    label: search_records
    code: |
      return {"records": [{"npi": "123"}]}
"""


@pytest.fixture(autouse=True)
def _disable_author_time_gate_log_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", False)


def _outcome(**updates: object) -> RecordedBuildTestOutcome:
    base = {
        "phase": "persisted_block_run",
        "attempted_tool": "update_and_run_blocks",
        "verdict": "repairable_failure",
        "reason_code": "runtime_block_failure",
        "workflow_run_id": "wr_123",
        "block_labels": ["search_records"],
        "structural_failure_identity": "runtime:timeout_waiting_for_selector:failed",
        "page_evidence_refs": ["origin_present", "results:empty"],
        "observed_evidence_summary": "Timeout waiting for #results.",
    }
    base.update(updates)
    return RecordedBuildTestOutcome(**base)  # type: ignore[arg-type]


def _ctx(outcome: RecordedBuildTestOutcome | None = None) -> CopilotContext:
    history = []
    if outcome is not None:
        history = [
            {"structural_key": outcome.structural_key, "is_authoritative": True},
            {"structural_key": outcome.structural_key, "is_authoritative": True},
        ]
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="Fix the workflow",
        turn_intent=TurnIntent(
            mode=TurnIntentMode.EDIT,
            user_goal="Fix the workflow",
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        ),
        latest_recorded_build_test_outcome=outcome,
        recorded_build_test_outcome_history=history,
        recorded_outcome_grounding_requirement=None,
        composition_page_evidence=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        completion_verification_result=None,
        verified_criteria_high_water=frozenset(),
        verified_prefix_labels=[],
        verified_prefix_high_water_len=0,
        last_full_workflow_test_ok=False,
        verified_full_pass_consumed=False,
        blocker_signal=None,
        turn_halt=None,
        observed_browser_urls=["https://example.com/results"],
        consecutive_tool_tracker=[],
        tool_activity=[],
        pending_reconciliation_run_id=None,
        pending_reconciliation_requires_user_input=False,
        post_budget_page_inspection_required=False,
        last_failure_category_top=None,
        repeated_action_fingerprint_streak_count=0,
        last_test_non_retriable_nav_error=None,
    )


def _contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.REPAIR),
        verification_result=VerificationResult(run_status="failed"),
    )


def _bounded_inspect_evidence(**updates: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "source_tool": "inspect_page_for_composition",
        "current_url": "https://example.com/results",
        "page_title": "Results",
        "forms": [
            {
                "fields": [{"label": "Search", "selector": "#q"}],
                "submit_controls": [{"text": "Search", "selector": "button[type=submit]", "disabled": False}],
            }
        ],
        "navigation_targets": [{"text": "Next", "selector": "a.next", "disabled": False}],
        "result_containers": [{"selector": "#results", "text_excerpt": "No results"}],
        "challenge_controls": [],
        "anti_bot_indicators": [],
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_123",
    }
    evidence.update(updates)
    return evidence


def test_repeated_authoritative_outcome_arms_grounding_before_repair_ceiling() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)

    _update_repair_loop_state(ctx, _contract())

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    assert requirement.structural_key == outcome.structural_key
    assert requirement.required_tool == "inspect_page_for_composition"
    assert requirement.required_target_url == "current_page"
    assert requirement.workflow_run_id == "wr_123"
    assert ctx.blocker_signal is None


def test_log_only_recorded_outcome_grounding_records_without_stashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)

    result = _tool_loop_error(ctx, "update_workflow")

    assert result is None
    assert ctx.blocker_signal is None
    assert ctx.latest_tool_blocker_signal is None
    assert ctx.turn_halt is None
    event = ctx.author_time_gate_ablation_events[-1]
    assert event.gate_id == "recorded_outcome_grounding"
    assert event.reason_code == "recorded_outcome_grounding_required"
    assert event.blocked_tool == "update_workflow"
    assert event.fingerprint == outcome.structural_key
    assert event.log_only is True


def test_authoritative_persisted_outcome_arms_without_recorded_signature_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    ctx.recorded_build_test_outcome_history = [{"structural_key": outcome.structural_key, "is_authoritative": True}]
    monkeypatch.setattr(
        run_execution_module,
        "_repair_non_convergence_signature",
        lambda *_: "repair_no_verified_progress",
    )

    run_execution_module._update_repair_loop_state(ctx, _contract())

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    assert requirement.structural_key == outcome.structural_key
    assert requirement.workflow_run_id == "wr_123"
    assert requirement.satisfied is False


def test_progress_observed_outcome_does_not_arm_from_executed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome(verdict="progress_observed", reason_code="verified_success")
    ctx = _ctx(outcome)
    ctx.recorded_build_test_outcome_history = [{"structural_key": outcome.structural_key, "is_authoritative": True}]
    monkeypatch.setattr(
        run_execution_module,
        "_repair_non_convergence_signature",
        lambda *_: "repair_no_verified_progress",
    )

    run_execution_module._update_repair_loop_state(ctx, _contract())

    assert ctx.recorded_outcome_grounding_requirement is None


def test_authoritative_outcome_uses_last_executed_run_id_when_outcome_run_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome(workflow_run_id=None)
    ctx = _ctx(outcome)
    ctx.last_run_blocks_workflow_run_id = "wr_fallback"
    monkeypatch.setattr(
        run_execution_module,
        "_repair_non_convergence_signature",
        lambda *_: "repair_no_verified_progress",
    )

    run_execution_module._update_repair_loop_state(ctx, _contract())

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    assert requirement.workflow_run_id == "wr_fallback"

    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id=None, observed_after_workflow_run=False)
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False
    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id="wr_fallback")
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True


def test_changed_structural_key_resets_grounding_requirement_to_latest_run_outcome() -> None:
    first = _outcome()
    ctx = _ctx(first)
    arm_recorded_outcome_grounding_requirement(ctx)
    assert ctx.recorded_outcome_grounding_requirement is not None

    second = _outcome(structural_failure_identity="runtime:other")
    ctx.latest_recorded_build_test_outcome = second
    ctx.recorded_build_test_outcome_history.append({"structural_key": second.structural_key, "is_authoritative": True})

    _update_repair_loop_state(ctx, _contract())

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    assert requirement.structural_key == second.structural_key


def test_grounding_blocks_mutation_until_matching_current_page_inspect_evidence() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)

    error = _tool_loop_error(ctx, "update_and_run_blocks", {"workflow_yaml": "workflow_definition: {blocks: []}"})

    assert error is not None
    assert "inspect_page_for_composition" in error
    assert "current_page" in error
    assert ctx.recorded_outcome_grounding_requirement.satisfied is False

    ctx.composition_page_evidence = _bounded_inspect_evidence()
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    assert (
        _tool_loop_error(ctx, "update_and_run_blocks", {"workflow_yaml": "workflow_definition: {blocks: []}"}) is None
    )


@pytest.mark.parametrize(
    ("evidence_updates", "reject_reason"),
    [
        ({"source_tool": "evaluate"}, "not_inspect_source"),
        ({"workflow_run_id": "wr_old", "observed_after_workflow_run": True}, "run_id_mismatch"),
        ({"observed_after_workflow_run": False}, "run_id_mismatch"),
        ({"current_url": "", "inspected_url": ""}, "no_url"),
    ],
)
def test_grounding_rejection_logs_reason_and_run_id_fields(
    evidence_updates: dict[str, object],
    reject_reason: str,
) -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(**evidence_updates)

    with capture_logs() as logs:
        assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False

    event = next(log for log in logs if log["event"] == "copilot recorded outcome grounding rejected")
    assert event["reject_reason"] == reject_reason
    assert event["structural_key"] == outcome.structural_key
    assert event["requirement_workflow_run_id"] == "wr_123"
    assert event["evidence_workflow_run_id"] == ctx.composition_page_evidence.get("workflow_run_id")
    assert event["evidence_observed_after_workflow_run"] == ctx.composition_page_evidence.get(
        "observed_after_workflow_run"
    )
    assert event["source_tool"] == ctx.composition_page_evidence.get("source_tool")
    assert event["current_url_present"] is (reject_reason != "no_url")


def test_no_run_degraded_grounding_remains_unsatisfied_and_logs_degraded_page() -> None:
    outcome = _outcome(phase="scout_evaluate", workflow_run_id=None, attempted_tool="evaluate")
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(
        workflow_run_id=None,
        observed_after_workflow_run=False,
        forms=[],
        result_containers=[],
        navigation_targets=[],
        challenge_controls=[],
    )

    with capture_logs() as logs:
        assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False

    event = next(log for log in logs if log["event"] == "copilot recorded outcome grounding rejected")
    assert event["reject_reason"] == "degraded_page"


def test_persisted_degraded_empty_grounding_satisfies_and_reaches_prompt() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(
        forms=[],
        result_containers=[],
        navigation_targets=[],
        challenge_controls=[],
    )

    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    assert ctx.last_full_workflow_test_ok is False
    assert ctx.completion_verification_result is None
    assert ctx.verified_criteria_high_water == frozenset()
    assert ctx.verified_prefix_high_water_len == 0

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    payload = requirement.payload
    assert payload is not None
    assert payload.observed_empty_page is True
    assert payload.capture_degraded is True
    assert payload.challenge_gated is False
    assert payload.diagnostic_reason == "capture_degraded"
    assert payload.target_url == "current_page"
    assert payload.source_url == "https://example.com/results"
    assert payload.requirement_workflow_run_id == "wr_123"
    assert payload.payload_workflow_run_id == "wr_123"

    prompt = _recorded_build_test_outcome_prompt(ctx)  # type: ignore[arg-type]
    assert "observed_empty_page: true" in prompt
    assert "capture_degraded: true" in prompt
    assert "diagnostic_reason: capture_degraded" in prompt
    assert "requirement_workflow_run_id: wr_123" in prompt
    assert "payload_workflow_run_id: wr_123" in prompt


def test_unsatisfied_grounding_does_not_mask_repair_ceiling_final_blocker() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id="wr_old")
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False

    _tool_loop_error(ctx, "update_and_run_blocks", {"workflow_yaml": "workflow_definition: {blocks: []}"})
    assert ctx.blocker_signal.internal_reason_code == "recorded_outcome_grounding_required"

    repair_ceiling = CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="Stop retrying and report the blocker.",
        user_facing_reason="I couldn't get past the same problem after several attempts.",
        recovery_hint="report_blocker_to_user",
        renders_final_reply=True,
        internal_reason_code="repair_ceiling_reached",
    )
    stash_blocker_signal(ctx, repair_ceiling)

    assert ctx.blocker_signal is repair_ceiling


def test_same_structural_key_new_workflow_run_rearms_grounding_requirement() -> None:
    first = _outcome(workflow_run_id="wr_123")
    ctx = _ctx(first)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id="wr_123")
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    second = _outcome(workflow_run_id="wr_456")
    ctx.latest_recorded_build_test_outcome = second
    ctx.recorded_build_test_outcome_history.append({"structural_key": second.structural_key, "is_authoritative": True})

    requirement = arm_recorded_outcome_grounding_requirement(ctx)

    assert requirement is not None
    assert requirement.workflow_run_id == "wr_456"
    assert requirement.satisfied is False
    assert requirement.payload is None

    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id="wr_123")
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False

    ctx.composition_page_evidence = _bounded_inspect_evidence(workflow_run_id="wr_456")
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    prompt = _recorded_build_test_outcome_prompt(ctx)  # type: ignore[arg-type]
    assert "grounding_workflow_run_id: wr_456" in prompt
    assert "grounding_workflow_run_id: wr_123" not in prompt


def test_terminal_blocker_takes_precedence_over_grounding_requirement() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.workflow_verification_evidence = SimpleNamespace(
        active_run_terminal_evidence_detected=True,
        page_title="Done",
        active_run_terminal_evidence_workflow_run_id="wr_terminal",
    )

    error = _tool_loop_error(ctx, "update_and_run_blocks", {"workflow_yaml": "workflow_definition: {blocks: []}"})

    assert error is not None
    assert ctx.blocker_signal.internal_reason_code == ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
    assert "recorded_outcome_grounding_required" not in error


def test_pending_reconciliation_takes_precedence_over_grounding_requirement() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.pending_reconciliation_run_id = "wr_pending"

    error = _tool_loop_error(ctx, "update_and_run_blocks", {"workflow_yaml": "workflow_definition: {blocks: []}"})

    assert error is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_pending_reconciliation_no_input"
    assert "recorded_outcome_grounding_required" not in error


def test_grounding_abstains_for_non_authoritative_or_missing_current_url_and_records_challenge_payload() -> None:
    non_authoritative = _outcome(structural_failure_identity="", page_evidence_refs=[])
    ctx = _ctx(non_authoritative)
    assert arm_recorded_outcome_grounding_requirement(ctx) is None

    outcome = _outcome()
    terminal_ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(terminal_ctx)
    terminal_ctx.composition_page_evidence = _bounded_inspect_evidence(
        challenge_state={"detected": True, "requires_human_verification": True},
        challenge_controls=[{"text": "Verify", "selector": "#captcha"}],
    )
    assert maybe_satisfy_recorded_outcome_grounding_requirement(terminal_ctx) is True
    challenge_payload = terminal_ctx.recorded_outcome_grounding_requirement.payload
    assert challenge_payload is not None
    assert challenge_payload.challenge_gated is True
    assert challenge_payload.diagnostic_reason == "challenge_gated"

    no_page_ctx = _ctx(_outcome(phase="scout_evaluate", workflow_run_id=None))
    no_page_ctx.observed_browser_urls = []
    arm_recorded_outcome_grounding_requirement(no_page_ctx)
    assert (
        _tool_loop_error(no_page_ctx, "update_workflow", {"workflow_yaml": "workflow_definition: {blocks: []}"}) is None
    )


def test_no_run_grounding_requires_fresh_post_arm_inspect_evidence() -> None:
    outcome = _outcome(phase="scout_evaluate", workflow_run_id=None, attempted_tool="evaluate")
    ctx = _ctx(outcome)
    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)

    arm_recorded_outcome_grounding_requirement(ctx)

    assert ctx.composition_page_evidence is None
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False

    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True


def test_repeated_no_run_author_time_outcome_arms_through_repair_loop() -> None:
    outcome = _outcome(
        phase="author_time_reject",
        attempted_tool="update_workflow",
        verdict="authoring_rejected",
        reason_code="synthesized_parameter_binding_ambiguous",
        workflow_run_id=None,
        structural_failure_identity="authoring:binding",
    )
    ctx = _ctx(outcome)
    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)

    _update_repair_loop_state(ctx, _contract())

    requirement = ctx.recorded_outcome_grounding_requirement
    assert requirement is not None
    assert requirement.structural_key == outcome.structural_key
    assert requirement.workflow_run_id is None
    assert ctx.composition_page_evidence is None


def test_author_time_grounding_requires_fresh_post_arm_inspect_evidence() -> None:
    outcome = _outcome(
        phase="author_time_reject",
        attempted_tool="update_workflow",
        verdict="authoring_rejected",
        reason_code="synthesized_parameter_binding_ambiguous",
        workflow_run_id=None,
        structural_failure_identity="authoring:binding",
    )
    ctx = _ctx(outcome)
    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)

    arm_recorded_outcome_grounding_requirement(ctx)

    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is False
    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)
    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True


def test_grounding_payload_reaches_recorded_outcome_prompt() -> None:
    outcome = _outcome(phase="scout_evaluate", workflow_run_id=None, attempted_tool="evaluate")
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(observed_after_workflow_run=False, workflow_run_id=None)

    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    prompt = _recorded_build_test_outcome_prompt(ctx)  # type: ignore[arg-type]
    assert "RECORDED OUTCOME GROUNDING EVIDENCE:" in prompt
    assert f"repeated_structural_key: {outcome.structural_key}" in prompt
    assert "source_tool: inspect_page_for_composition" in prompt
    assert "observed_after_workflow_run: false" in prompt


def _binding_ctx() -> SimpleNamespace:
    outcome = _outcome()
    ctx = _ctx(outcome)
    ctx.workflow_yaml = _OWNING_BLOCK_WORKFLOW
    ctx.code_artifact_metadata = None
    ctx.recorded_outcome_binding_constraint = None
    return ctx


def test_satisfy_binds_typed_constraint_with_frontier_facet_and_owning_blocks() -> None:
    ctx = _binding_ctx()
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence()

    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    constraint = ctx.recorded_outcome_binding_constraint
    assert isinstance(constraint, RecordedOutcomeBindingConstraint)
    assert constraint.repeated_structural_key == ctx.latest_recorded_build_test_outcome.structural_key
    assert constraint.frontier_facet == "selector_frontier"
    assert constraint.owning_block_labels == ["search_records"]
    assert constraint.diagnostic_reason == "none"
    assert constraint.frontier_uncrossable is False
    assert constraint.recorded_block_signatures == {
        "search_records": authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)["search_records"]
    }

    prompt = _recorded_build_test_outcome_prompt(ctx)  # type: ignore[arg-type]
    assert "RECORDED OUTCOME BINDING CONSTRAINT:" in prompt
    assert "frontier_facet: selector_frontier" in prompt


def test_degraded_capture_binds_uncrossable_constraint() -> None:
    ctx = _binding_ctx()
    arm_recorded_outcome_grounding_requirement(ctx)
    ctx.composition_page_evidence = _bounded_inspect_evidence(
        forms=[], result_containers=[], navigation_targets=[], challenge_controls=[]
    )

    assert maybe_satisfy_recorded_outcome_grounding_requirement(ctx) is True

    constraint = ctx.recorded_outcome_binding_constraint
    assert isinstance(constraint, RecordedOutcomeBindingConstraint)
    assert constraint.diagnostic_reason == "capture_degraded"
    assert constraint.frontier_uncrossable is True


def test_convergence_reject_only_when_owning_block_frontier_unchanged() -> None:
    outcome = _outcome(authored_structure_signature="different_whole_signature")
    recorded_sig = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)["search_records"]
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=outcome.structural_key or "",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
        owning_block_labels=["search_records"],
        recorded_block_signatures={"search_records": recorded_sig},
    )
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=outcome,
        recorded_outcome_binding_constraint=constraint,
    )

    unchanged = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW, code_artifact_metadata=None
    )
    assert unchanged is not None
    assert unchanged.reason == "frontier_unchanged"
    assert unchanged.commit_early_terminal is False

    moved_yaml = _OWNING_BLOCK_WORKFLOW.replace('"123"', '"456"')
    assert _recorded_outcome_convergence_reject(ctx, workflow_yaml=moved_yaml, code_artifact_metadata=None) is None


def test_convergence_reject_identical_authored_structure() -> None:
    signature = run_execution_module.authored_structure_signature_from_workflow(_OWNING_BLOCK_WORKFLOW, None)
    outcome = _outcome(authored_structure_signature=signature)
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=outcome,
        recorded_outcome_binding_constraint=None,
    )

    decision = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW, code_artifact_metadata=None
    )
    assert decision is not None
    assert decision.reason == "identical_authored_structure"


def test_convergence_reject_uncrossable_frontier_commits_early_terminal() -> None:
    outcome = _outcome(authored_structure_signature="different_whole_signature")
    recorded_sig = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)["search_records"]
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=outcome.structural_key or "",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
        owning_block_labels=["search_records"],
        diagnostic_reason="challenge_gated",
        recorded_block_signatures={"search_records": recorded_sig},
    )
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=outcome,
        recorded_outcome_binding_constraint=constraint,
        blocker_signal=None,
        turn_halt=None,
        consecutive_non_converging_repair_count=2,
    )

    decision = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW, code_artifact_metadata=None
    )
    assert decision is not None
    assert decision.reason == "frontier_unchanged"
    assert decision.commit_early_terminal is True

    _commit_recorded_outcome_early_terminal(ctx)
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.REPAIR_CEILING_REACHED
    assert ctx.blocker_signal.internal_reason_code == "repair_ceiling_reached"
    assert ctx.blocker_signal.renders_final_reply is True
    assert ctx.blocker_signal.preserves_workflow_draft is True
    assert ctx.blocker_signal.user_facing_reason.strip()
    assert "can't get past this page" in ctx.blocker_signal.user_facing_reason
    assert ctx.turn_halt.blocker_signal is ctx.blocker_signal


def test_block_signature_keys_on_code_not_label_or_description() -> None:
    base = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)
    described = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW_DESCRIPTION_EDIT, None)
    renamed = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW_RENAMED, None)
    code_moved = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW.replace('"123"', '"456"'), None)

    assert described["search_records"] == base["search_records"]
    assert renamed["renamed_records"] == base["search_records"]
    assert code_moved["search_records"] != base["search_records"]


def test_description_only_edit_reads_as_identical_authored_structure() -> None:
    signature = run_execution_module.authored_structure_signature_from_workflow(_OWNING_BLOCK_WORKFLOW, None)
    outcome = _outcome(authored_structure_signature=signature)
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=outcome,
        recorded_outcome_binding_constraint=None,
    )

    decision = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW_DESCRIPTION_EDIT, code_artifact_metadata=None
    )
    assert decision is not None
    assert decision.reason == "identical_authored_structure"


def test_frontier_unchanged_reject_fires_when_whole_signature_moves_but_owning_block_holds() -> None:
    outcome = _outcome(authored_structure_signature="whole_signature_from_prior_attempt")
    recorded_sig = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)["search_records"]
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=outcome.structural_key or "",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
        owning_block_labels=["search_records"],
        recorded_block_signatures={"search_records": recorded_sig},
    )
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=outcome,
        recorded_outcome_binding_constraint=constraint,
    )

    decision = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW_SIBLING_MOVE, code_artifact_metadata=None
    )
    assert decision is not None
    assert decision.reason == "frontier_unchanged"
    assert decision.commit_early_terminal is False


def test_binding_enforces_across_consecutive_author_time_rejects() -> None:
    run_outcome = _outcome(authored_structure_signature="run_whole_signature")
    recorded_sig = authored_block_signatures_from_workflow(_OWNING_BLOCK_WORKFLOW, None)["search_records"]
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=run_outcome.structural_key or "",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
        owning_block_labels=["search_records"],
        recorded_block_signatures={"search_records": recorded_sig},
    )
    ctx = SimpleNamespace(
        latest_recorded_build_test_outcome=run_outcome,
        recorded_outcome_binding_constraint=constraint,
    )

    first = _recorded_outcome_convergence_reject(ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW, code_artifact_metadata=None)
    assert first is not None
    assert first.reason == "frontier_unchanged"

    # The first reject re-keys `latest` to an author-time reject outcome whose structural key
    # differs from the bound run outcome; the binding must still enforce on the next
    # different-structure-but-frontier-unchanged submit.
    author_time_latest = _outcome(
        phase="author_time_reject",
        reason_code="unchanged_after_recorded_outcome",
        structural_failure_identity="author_time:rekeyed",
        authored_structure_signature="author_time_reject_whole_signature",
    )
    assert author_time_latest.structural_key != constraint.repeated_structural_key
    ctx.latest_recorded_build_test_outcome = author_time_latest

    second = _recorded_outcome_convergence_reject(
        ctx, workflow_yaml=_OWNING_BLOCK_WORKFLOW_SIBLING_MOVE, code_artifact_metadata=None
    )
    assert second is not None
    assert second.reason == "frontier_unchanged"


def test_sibling_churn_frontier_unchanged_rejects_reach_honest_churn_stop() -> None:
    ctx = _ctx()

    for index in range(MAX_CODE_AUTHORING_GUARDRAIL_REJECTS):
        _record_author_time_reject_outcome(
            ctx,
            reason_code="unchanged_after_recorded_outcome",
            summary="The authored code and output structure are unchanged after the last recorded test outcome.",
            structural_payload={
                "reason_code": "unchanged_after_recorded_outcome",
                "authored_structure_signature": f"moving_whole_signature_{index}",
                "block_labels": ["search_records"],
            },
            authored_structure_signature=f"moving_whole_signature_{index}",
            block_labels=["search_records"],
        )
        _record_code_authoring_guardrail_reject(ctx, frontier_unchanged=True)

    # Sibling churn moves the whole-signature key every turn, so each recorded outcome reads as a
    # non-repeat; the frontier-unchanged flag keeps the churn counter climbing to the honest stop
    # instead of resetting and riding to max_turns.
    assert (
        ctx.recorded_build_test_outcome_history[-1]["structural_key"]
        != ctx.recorded_build_test_outcome_history[-2]["structural_key"]
    )
    assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    assert isinstance(ctx.blocker_signal, CopilotToolBlockerSignal)
    assert ctx.blocker_signal.internal_reason_code == "code_authoring_guardrail_churn"


def test_early_terminal_renders_typed_final_reply_and_preserves_draft() -> None:
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key="repeated_key",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
        owning_block_labels=["search_records"],
        diagnostic_reason="challenge_gated",
    )
    ctx = SimpleNamespace(
        recorded_outcome_binding_constraint=constraint,
        blocker_signal=None,
        turn_halt=None,
        consecutive_non_converging_repair_count=3,
    )

    _commit_recorded_outcome_early_terminal(ctx)

    signal = ctx.blocker_signal
    assert signal.renders_final_reply is True
    assert signal.recovery_hint == "report_blocker_to_user"
    assert signal.blocked_tool == "update_workflow"
    assert signal.cleared_by_tools == frozenset()
    assert signal.preserves_workflow_draft is True
    assert signal.internal_reason_code == "repair_ceiling_reached"
    assert "can't get past this page" in signal.user_facing_reason
    assert signal.internal_reason_code not in signal.user_facing_reason

    assert ctx.turn_halt.kind == TurnHaltKind.REPAIR_CEILING_REACHED
    assert ctx.turn_halt.blocker_signal is signal
    assert ctx.turn_halt.draft_state == {"preserves_workflow_draft": True}
    assert ctx.turn_halt.extra["consecutive_identical_repair_count"] == 3


def test_update_repair_loop_state_clears_stale_requirement_when_outcome_not_authoritative() -> None:
    outcome = _outcome()
    ctx = _ctx(outcome)
    arm_recorded_outcome_grounding_requirement(ctx)
    assert ctx.recorded_outcome_grounding_requirement is not None
    ctx.recorded_outcome_binding_constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=outcome.structural_key or "",
        phase="persisted_block_run",
        reason_code="runtime_block_failure",
        frontier_facet="selector_frontier",
    )

    ctx.latest_recorded_build_test_outcome = None
    ctx.recorded_build_test_outcome_history = []

    _update_repair_loop_state(ctx, _contract())

    assert ctx.recorded_outcome_grounding_requirement is None
    assert ctx.recorded_outcome_binding_constraint is None
