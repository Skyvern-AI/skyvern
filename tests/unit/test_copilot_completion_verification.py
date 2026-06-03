from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.agent import _completion_contract_not_violated, _verified_workflow_or_none
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    _coerce_result,
    evaluate_completion_criteria,
    summarize_unsatisfied_outcomes,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
    _verification_satisfaction,
)
from skyvern.forge.sdk.copilot.enforcement import (
    _outcome_criteria_evaluated,
    outcome_fully_verified,
    verified_goal_satisfied_context,
)
from skyvern.forge.sdk.copilot.hooks import _tool_completion_satisfies_turn
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy, _parse_completion_criteria
from skyvern.forge.sdk.copilot.tools import (
    _build_run_evidence_snapshot,
    _current_workflow_has_evidence_block,
    _is_outcome_evidence_candidate,
    _is_unfinished_run_verification_candidate,
    _maybe_run_completion_verification,
    _outcome_failure_warrants_repair,
    _outcome_unverified_reason,
    _record_run_blocks_result,
)


def _criterion(cid: str, outcome: str, *, method_mandated: bool = False) -> CompletionCriterion:
    return CompletionCriterion(id=cid, outcome=outcome, method_mandated=method_mandated)


def _evaluated(*satisfied_by_id: tuple[str, bool]) -> CompletionVerificationResult:
    ids = [cid for cid, _ in satisfied_by_id]
    verdicts = [
        CriterionVerdict(criterion_id=cid, satisfied=ok, reason_code="evidence_confirms" if ok else "no_evidence")
        for cid, ok in satisfied_by_id
    ]
    return CompletionVerificationResult(status="evaluated", criterion_ids=ids, verdicts=verdicts)


def _completion_handler_lookup(handler: object) -> Callable[[object], Awaitable[object]]:
    async def _lookup(_ctx: object) -> object:
        return handler

    return _lookup


def test_is_fully_satisfied_requires_every_criterion() -> None:
    assert _evaluated(("c0", True), ("c1", True)).is_fully_satisfied() is True
    assert _evaluated(("c0", True), ("c1", False)).is_fully_satisfied() is False


def test_empty_verdicts_with_criteria_is_not_vacuously_satisfied() -> None:
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["c0"], verdicts=[])
    assert result.is_fully_satisfied() is False


def test_unavailable_and_empty_criteria_never_satisfied() -> None:
    assert CompletionVerificationResult(status="unavailable").is_fully_satisfied() is False
    assert CompletionVerificationResult(status="evaluated", criterion_ids=[]).is_fully_satisfied() is False


def test_coerce_requires_evidence_confirms_for_satisfied() -> None:
    raw = {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "unknown"}]}
    result = _coerce_result(raw, ["c0"])
    assert result.status == "evaluated"
    assert result.verdicts[0].satisfied is False


def test_coerce_missing_criterion_defaults_to_no_evidence() -> None:
    result = _coerce_result({"verdicts": []}, ["c0", "c1"])
    assert [v.reason_code for v in result.verdicts] == ["no_evidence", "no_evidence"]
    assert result.is_fully_satisfied() is False


def test_coerce_ignores_unknown_ids_and_dedupes_first_wins() -> None:
    raw = {
        "verdicts": [
            {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
            {"criterion_id": "c0", "satisfied": False, "reason_code": "no_evidence"},
            {"criterion_id": "ghost", "satisfied": True, "reason_code": "evidence_confirms"},
        ]
    }
    result = _coerce_result(raw, ["c0"])
    assert len(result.verdicts) == 1
    assert result.verdicts[0].satisfied is True


def test_coerce_accepts_bytes_and_rejects_malformed() -> None:
    raw_bytes = b'{"verdicts": [{"criterion_id": "c0", "satisfied": true, "reason_code": "evidence_confirms"}]}'
    assert _coerce_result(raw_bytes, ["c0"]).is_fully_satisfied() is True
    assert _coerce_result("not json at all", ["c0"]).status == "unavailable"
    assert _coerce_result({"no_verdicts_key": 1}, ["c0"]).status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_no_handler_or_no_criteria_is_unavailable() -> None:
    snapshot = RunEvidenceSnapshot(current_url="https://example.com")
    assert (await evaluate_completion_criteria([_criterion("c0", "x")], snapshot, None)).status == "unavailable"
    assert (await evaluate_completion_criteria([], snapshot, lambda **_: {})).status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_handler_exception_is_unavailable() -> None:
    async def boom(**_: object) -> object:
        raise RuntimeError("llm down")

    snapshot = RunEvidenceSnapshot(current_url="https://example.com")
    result = await evaluate_completion_criteria([_criterion("c0", "x")], snapshot, boom)
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_happy_path_returns_evaluated() -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    snapshot = RunEvidenceSnapshot(block_outputs={"confirm": {"count": 1}})
    result = await evaluate_completion_criteria([_criterion("c0", "item in cart")], snapshot, handler)
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


def test_snapshot_has_evidence() -> None:
    assert RunEvidenceSnapshot().has_evidence() is False
    assert RunEvidenceSnapshot(current_url="https://example.com").has_evidence() is True
    assert RunEvidenceSnapshot(block_outputs={"a": 1}).has_evidence() is True


def test_summarize_unsatisfied_lists_unmet_outcomes() -> None:
    criteria = [_criterion("c0", "item in cart"), _criterion("c1", "added exactly once")]
    result = _evaluated(("c0", True), ("c1", False))
    assert summarize_unsatisfied_outcomes(result, criteria) == "added exactly once"


def test_parse_assigns_deterministic_ids_and_dedupes() -> None:
    raw = [
        {"outcome": "Item in cart", "id": "model-supplied-ignored"},
        {"outcome": "item in cart"},
        {"outcome": "", "implicit": True},
        {"outcome": "Added exactly once", "implicit": True},
    ]
    criteria = _parse_completion_criteria(raw)
    assert [c.id for c in criteria] == ["c0", "c1"]
    assert [c.outcome for c in criteria] == ["Item in cart", "Added exactly once"]
    assert criteria[1].implicit is True


def test_parse_caps_count() -> None:
    raw = [{"outcome": f"outcome {i}"} for i in range(20)]
    assert len(_parse_completion_criteria(raw)) == 8


def test_verification_satisfaction_no_cvr_uses_prior_proxy() -> None:
    assert _verification_satisfaction(True, False, "completed", None) == (True, True)
    assert _verification_satisfaction(True, True, "completed", None) == (False, False)
    assert _verification_satisfaction(False, False, None, None) == (None, None)


def test_verification_satisfaction_evaluated_drives_contract_signal() -> None:
    assert _verification_satisfaction(True, False, "completed", _evaluated(("c0", True))) == (True, True)
    _, contract = _verification_satisfaction(True, False, "completed", _evaluated(("c0", False)))
    assert contract is False


def test_verification_satisfaction_unavailable_fails_closed() -> None:
    _, contract = _verification_satisfaction(True, False, "completed", CompletionVerificationResult("unavailable"))
    assert contract is False


def _satisfied_contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="run_blocks_and_collect_debug"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
        verification_result=VerificationResult(user_goal_satisfied=True, completion_contract_satisfied=True),
    )


def _gate_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="do A then B",
    )
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.latest_diagnosis_repair_contract = _satisfied_contract()
    ctx.last_update_block_count = 1
    ctx.request_policy = RequestPolicy(completion_contract="done when B happens")
    return ctx


def test_gate_bypasses_heuristic_only_on_evaluated_verdict() -> None:
    bypass = _gate_ctx()
    bypass.completion_verification_result = _evaluated(("c0", True))
    assert verified_goal_satisfied_context(bypass) is True

    retained = _gate_ctx()
    retained.completion_verification_result = None
    assert verified_goal_satisfied_context(retained) is False


def test_gate_flag_off_ignores_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_OUTCOME_VERIFICATION_ENABLED", False)
    ctx = _gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _outcome_criteria_evaluated(ctx) is False
    assert verified_goal_satisfied_context(ctx) is False


def test_gate_withholds_on_evaluated_unconfirmed_even_with_clean_run_status() -> None:
    # The judge verdict is authoritative in both directions: an evaluated-but-
    # unconfirmed verdict withholds even when run-status latches and the diagnosis
    # contract would otherwise pass -- recognition must weigh the verdict, not just
    # whether the judge ran.
    ctx = _gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert verified_goal_satisfied_context(ctx) is False


def test_completion_contract_not_violated() -> None:
    ctx = SimpleNamespace(completion_verification_result=None)
    assert _completion_contract_not_violated(ctx) is True  # type: ignore[arg-type]
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _completion_contract_not_violated(ctx) is True  # type: ignore[arg-type]
    ctx.completion_verification_result = _evaluated(("c0", False))
    assert _completion_contract_not_violated(ctx) is False  # type: ignore[arg-type]


def test_outcome_unverified_reason_for_unsatisfied_and_unavailable() -> None:
    policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])
    ctx = SimpleNamespace(request_policy=policy)
    assert _outcome_unverified_reason(ctx, None) is None
    assert _outcome_unverified_reason(ctx, _evaluated(("c0", True))) is None
    unsatisfied = _outcome_unverified_reason(ctx, _evaluated(("c0", False)))
    assert unsatisfied is not None and "item in cart" in unsatisfied
    unavailable = _outcome_unverified_reason(ctx, CompletionVerificationResult("unavailable"))
    assert unavailable is not None and "could not be verified" in unavailable


def _clean_success_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_x",
            "overall_status": "completed",
            "executed_block_labels": ["confirm"],
            "current_url": "https://example.com/cart",
            "blocks": [
                {
                    "label": "confirm",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"extracted_information": {"items": ["a"]}},
                }
            ],
        },
    }


def _run_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="add item to cart and confirm",
    )
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])
    return ctx


def _ctx_with_blocks(*block_types: str) -> CopilotContext:
    ctx = _run_ctx()
    blocks = [SimpleNamespace(block_type=bt, label=f"b{i}") for i, bt in enumerate(block_types)]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks))
    ctx.verified_prefix_labels = [b.label for b in blocks]
    return ctx


def _contradicted(cid: str) -> CompletionVerificationResult:
    verdict = CriterionVerdict(criterion_id=cid, satisfied=False, reason_code="evidence_contradicts")
    return CompletionVerificationResult(status="evaluated", criterion_ids=[cid], verdicts=[verdict])


def test_record_run_blocks_downgrades_when_confirmation_block_present_but_unmet() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_good_workflow is None
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert "item in cart" in (ctx.last_test_failure_reason or "")


def test_record_run_blocks_keeps_building_on_mid_build_no_evidence() -> None:
    ctx = _ctx_with_blocks("goto_url", "navigation")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    # A nav-only WIP that has not added a confirmation block yet must keep building,
    # not enter repair...
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
    # ...but terminal success stays withheld because the outcome is unverified.
    assert _completion_contract_not_violated(ctx) is False


def test_record_run_blocks_downgrades_on_contradiction_without_confirmation_block() -> None:
    ctx = _ctx_with_blocks("goto_url", "navigation")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_contradicted("c0"))
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False


def test_record_run_blocks_keeps_success_when_outcome_verified() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.workflow_verification_evidence.full_workflow_verified is True


def test_current_workflow_has_evidence_block() -> None:
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("extraction")) is True
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("goto_url", "validation")) is True
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("goto_url", "navigation")) is False
    assert _current_workflow_has_evidence_block(_run_ctx()) is False


def test_outcome_failure_warrants_repair() -> None:
    has_block = _ctx_with_blocks("extraction")
    nav_only = _ctx_with_blocks("goto_url", "navigation")
    assert _outcome_failure_warrants_repair(nav_only, None) is False
    # Contradiction is a real failure regardless of which blocks exist.
    assert _outcome_failure_warrants_repair(nav_only, _contradicted("c0")) is True
    # Absence of evidence: failure only once a confirmation block exists.
    assert _outcome_failure_warrants_repair(has_block, _evaluated(("c0", False))) is True
    assert _outcome_failure_warrants_repair(nav_only, _evaluated(("c0", False))) is False


# --- Direction 2: recognition governed by evidence, not run status ---------------
#
# A run canceled or only partially completed (ok=False) still produces runtime
# evidence. When that evidence confirms every outcome criterion, the goal the user
# can observe was reached, and recognition must not be suppressed by run status.


def _canceled_budget_result() -> dict:
    # The watchdog budget-cancel result shape: ok=False, no "blocks" list (the
    # result is returned before block harvest), only the reached URL survives.
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_cancel",
            "overall_status": "canceled",
            "current_url": "https://example.com/cart",
            "failure_reason": "Task wr_cancel was canceled",
            "failure_categories": [{"category": "PER_TOOL_BUDGET", "confidence_float": 1.0, "reasoning": "budget"}],
        },
    }


def _canceled_gate_ctx() -> CopilotContext:
    # A run that did not finish cleanly: every run-status latch is false and the
    # diagnosis routed to repair, yet the judge confirmed the outcome from evidence.
    ctx = _gate_ctx()
    ctx.last_test_ok = False
    ctx.last_full_workflow_test_ok = False
    ctx.latest_diagnosis_repair_contract = DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="run_blocks_and_collect_debug"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.REPAIR),
        verification_result=VerificationResult(user_goal_satisfied=False, completion_contract_satisfied=True),
    )
    return ctx


def test_unfinished_run_verification_candidate_admits_canceled_with_evidence() -> None:
    ctx = _run_ctx()
    assert _is_unfinished_run_verification_candidate(ctx, _canceled_budget_result()) is True
    # ok=True belongs to the clean-success candidate path, not this one.
    assert _is_unfinished_run_verification_candidate(ctx, _clean_success_result()) is False
    # ok=False with no reached runtime URL leaves nothing to judge.
    assert _is_unfinished_run_verification_candidate(ctx, {"ok": False, "data": {}}) is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_runs_on_canceled_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._completion_verification_handler",
        _completion_handler_lookup(handler),
    )
    ctx = _run_ctx()
    result = await _maybe_run_completion_verification(ctx, _canceled_budget_result(), time.monotonic())
    assert result is not None
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


def test_outcome_fully_verified_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert outcome_fully_verified(ctx) is True
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert outcome_fully_verified(ctx) is False
    ctx.completion_verification_result = None
    assert outcome_fully_verified(ctx) is False
    ctx.completion_verification_result = _evaluated(("c0", True))
    monkeypatch.setattr(settings, "COPILOT_OUTCOME_VERIFICATION_ENABLED", False)
    assert outcome_fully_verified(ctx) is False


def test_gate_recognizes_canceled_run_on_full_evidence() -> None:
    ctx = _canceled_gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert verified_goal_satisfied_context(ctx) is True


def test_gate_does_not_recognize_partial_canceled_run() -> None:
    ctx = _canceled_gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert verified_goal_satisfied_context(ctx) is False


def test_tool_completion_recognizes_canceled_run_on_full_evidence() -> None:
    ctx = _canceled_gate_ctx()
    parsed = {"ok": False, "data": {"workflow_run_id": "wr_cancel"}}
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _tool_completion_satisfies_turn(ctx, "run_blocks_and_collect_debug", parsed) is True
    # A canceled run whose outcome is only partially confirmed does not satisfy the turn.
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert _tool_completion_satisfies_turn(ctx, "run_blocks_and_collect_debug", parsed) is False


def test_verified_workflow_presented_on_recognized_canceled_run() -> None:
    ctx = _canceled_gate_ctx()
    ctx.last_workflow = SimpleNamespace()
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _verified_workflow_or_none(ctx) == (ctx.last_workflow, "workflow: {}")
    # Run-status latches false and outcome not fully confirmed: nothing is surfaced.
    ctx.completion_verification_result = _evaluated(("c0", False))
    assert _verified_workflow_or_none(ctx) == (None, None)


# --- SKY-10576: recognition governed by whole-workflow outcome, not per-block prefix ---
#
# A clean ok=True run can reach the goal (its outcome block produced data and the
# browser is on the goal page) while earlier block labels are not in the verified
# end-to-end prefix. Recognition must come from the outcome judge, not from whether
# every block was verified as a prefix; otherwise an achieved goal is hedged as an
# "unverified draft" and the agent overruns (SKY-10576, confirmed in live QA).


def _ctx_unverified_prefix() -> CopilotContext:
    ctx = _run_ctx()
    blocks = [
        SimpleNamespace(block_type="navigation", label="b0"),
        SimpleNamespace(block_type="navigation", label="b1"),
        SimpleNamespace(block_type="navigation", label="b2"),
        SimpleNamespace(block_type="extraction", label="b3"),
    ]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks))
    # Only the suffix is in the verified prefix; the goal was reached on the final
    # incremental run, but b0/b1 never entered the prefix.
    ctx.verified_prefix_labels = ["b2", "b3"]
    ctx.verified_block_outputs = {"b3": {"one_star_review_text": "For the life of me..."}}
    return ctx


def _empty_data_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_empty",
            "overall_status": "completed",
            "current_url": "https://example.com/reviews",
            "blocks": [{"label": "confirm", "block_type": "EXTRACTION", "status": "completed"}],
        },
    }


def test_outcome_evidence_candidate_admits_clean_run_despite_unverified_prefix() -> None:
    ctx = _ctx_unverified_prefix()
    # A clean run is admitted for the judge even though b0/b1 are not in the verified
    # prefix -- recognition is governed by the outcome judge, not the per-block prefix.
    assert _is_outcome_evidence_candidate(ctx, _clean_success_result()) is True
    # It still rejects empty-data runs (no clean outcome to judge) and ok=False runs.
    assert _is_outcome_evidence_candidate(ctx, _empty_data_result()) is False
    assert _is_outcome_evidence_candidate(ctx, {"ok": False, "data": {}}) is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_runs_on_unverified_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._completion_verification_handler",
        _completion_handler_lookup(handler),
    )
    ctx = _ctx_unverified_prefix()
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic())
    assert result is not None
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


def test_gate_recognizes_clean_run_despite_unverified_prefix() -> None:
    ctx = _ctx_unverified_prefix()
    # The full-workflow run-status latch is False (incremental run), yet the judge
    # confirmed the outcome: recognition must fire on the evidence.
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert outcome_fully_verified(ctx) is True
    assert verified_goal_satisfied_context(ctx) is True


# --- Review hardening: method-mandated criteria, per-run evidence, fail-closed ---


@pytest.mark.asyncio
async def test_method_mandated_criteria_excluded_from_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._completion_verification_handler",
        _completion_handler_lookup(handler),
    )
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c0", "item in cart"),
            _criterion("c1", "use the search bar", method_mandated=True),
        ]
    )
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic())
    # The method-mandated criterion is not sent to the end-state judge (it could only
    # ever return no_evidence), so it cannot false-block a legitimate success.
    assert result is not None
    assert result.criterion_ids == ["c0"]
    assert result.is_fully_satisfied() is True


def test_snapshot_uses_current_run_blocks_not_stale_outputs() -> None:
    ctx = _ctx_unverified_prefix()  # verified_block_outputs carries a stale b3 from a prior run
    stale_run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_now",
            "current_url": "https://example.com/x",
            "executed_block_labels": ["b0"],
            "blocks": [{"label": "b0", "block_type": "NAVIGATION", "status": "completed"}],
        },
    }
    snap = _build_run_evidence_snapshot(ctx, stale_run)
    # A prior run's output must not leak in as this run's evidence.
    assert "b3" not in snap.block_outputs
    assert snap.block_outputs == {}
    fresh_run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_now",
            "current_url": "https://example.com/x",
            "executed_block_labels": ["b3"],
            "blocks": [
                {
                    "label": "b3",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"extracted_information": {"price": "9.99"}},
                }
            ],
        },
    }
    snap2 = _build_run_evidence_snapshot(ctx, fresh_run)
    assert snap2.block_outputs.get("b3") == {"extracted_information": {"price": "9.99"}}


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_unavailable_on_low_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._completion_verification_handler",
        _completion_handler_lookup(handler),
    )
    ctx = _run_ctx()
    starved = time.monotonic() - 100_000  # no budget left to verify this candidate run
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), starved)
    # Fail closed: a candidate run we could not verify must not fall back to the
    # run-status proxy and claim success.
    assert result is not None
    assert result.status == "unavailable"
    assert result.is_fully_satisfied() is False

    # A missing judge handler stays a soft fallback (None), not a hard block.
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._completion_verification_handler",
        _completion_handler_lookup(None),
    )
    assert await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic()) is None


def test_completion_contract_not_violated_unavailable_blocks_surfacing() -> None:
    ctx = SimpleNamespace(completion_verification_result=CompletionVerificationResult("unavailable"))
    # An unavailable verdict means the outcome could not be verified: do not surface
    # the workflow as verified on run status alone.
    assert _completion_contract_not_violated(ctx) is False  # type: ignore[arg-type]


def test_request_policy_prompt_gates_completion_criteria_on_flag() -> None:
    from skyvern.forge.prompts import prompt_engine

    common = dict(
        user_message="",
        raw_secret_present="false",
        workflow_yaml="",
        earliest_user_turn="",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history="",
        global_llm_context="",
    )
    on = prompt_engine.load_prompt(
        template="workflow-copilot-request-policy", outcome_verification_enabled=True, **common
    )
    off = prompt_engine.load_prompt(
        template="workflow-copilot-request-policy", outcome_verification_enabled=False, **common
    )
    assert "completion_criteria" in on
    # Flag off must not perturb the classifier prompt with the criteria contract.
    assert "completion_criteria" not in off
