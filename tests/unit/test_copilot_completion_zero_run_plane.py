from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.agent import _should_apply_code_only_success_without_review
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    CompletionCriteriaTurnState,
    note_adjudication_on_turn_state,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    combine_verification_results,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools import _record_run_blocks_result
from skyvern.forge.sdk.copilot.tools.completion import (
    _completion_evidence_payload,
    _maybe_run_completion_verification,
    _maybe_run_completion_verification_from_page_observation,
    _outcome_unverified_reason,
)

_NO_GRADEABLE_PROSE = "could not be independently verified"
_ADD_OR_FIX_PROSE = "Add or fix the block"


def _criterion(cid: str, outcome: str, *, level: str = "run", method_mandated: bool = False) -> CompletionCriterion:
    return CompletionCriterion(id=cid, outcome=outcome, level=level, method_mandated=method_mandated)  # type: ignore[arg-type]


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="look up the order and report the order number",
    )


def _real_output_result() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_real",
            "overall_status": "completed",
            "executed_block_labels": ["lookup"],
            "current_url": "https://example.com/orders/A1B2C3",
            "page_title": "Order Confirmation",
            "blocks": [
                {
                    "label": "lookup",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"extracted_information": {"order_number": "A1B2C3"}},
                }
            ],
        },
    }


def _no_handler(_ctx: object) -> object:
    raise AssertionError("the zero-run-plane gate must return before the completion judge handler is resolved")


def _handler_lookup(handler: object) -> object:
    async def _lookup(_ctx: object) -> object:
        return handler

    return _lookup


@pytest.mark.asyncio
async def test_empty_fallback_gate_fires_above_criteria_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", _no_handler)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")

    verification = await _maybe_run_completion_verification(ctx, _real_output_result(), time.monotonic())

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.no_gradeable_run_plane is True
    assert verification.is_fully_satisfied() is False
    assert verification.criterion_ids == []


@pytest.mark.asyncio
async def test_all_method_mandated_floor_fallback_gate_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", _no_handler)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("floor", "navigated to the directory", method_mandated=True)],
        classifier_status="fallback",
    )

    verification = await _maybe_run_completion_verification(ctx, _real_output_result(), time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is True
    assert verification.is_fully_satisfied() is False
    assert verification.criterion_ids == []


@pytest.mark.asyncio
async def test_fallback_with_real_run_plane_criterion_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        _handler_lookup(handler),
    )
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "the order number is reported")],
        classifier_status="fallback",
    )

    verification = await _maybe_run_completion_verification(ctx, _real_output_result(), time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is True


@pytest.mark.asyncio
async def test_registered_download_in_block_output_reaches_judge_but_does_not_satisfy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(*, prompt: str, prompt_name: str) -> dict:
        captured["prompt"] = prompt
        return {"verdicts": [{"criterion_id": "c0", "satisfied": False, "reason_code": "no_evidence"}]}

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        _handler_lookup(handler),
    )
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "the requested file is downloaded")],
        classifier_status="success",
    )
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(block_type="extraction", label="lookup")])
    )
    block_extracted_data = {
        "extracted_information": {"summary": "file saved"},
        "downloaded_files": ["receipt.pdf"],
    }
    output: dict[str, Any] = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_download",
            "overall_status": "completed",
            "executed_block_labels": ["lookup"],
            "blocks": [
                {
                    "label": "lookup",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": block_extracted_data,
                }
            ],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, output, time.monotonic())

    assert _completion_evidence_payload(block_extracted_data)["download_registered"] is True
    assert captured.get("prompt") is not None
    assert "download_registered" in captured["prompt"]
    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.is_fully_satisfied() is False
    c0_verdict = next(verdict for verdict in verification.verdicts if verdict.criterion_id == "c0")
    assert c0_verdict.reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_non_fallback_param_only_uses_kept_floor_not_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", _no_handler)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "inputs are reusable", level="definition")],
        classifier_status="success",
    )

    verification = await _maybe_run_completion_verification(ctx, _real_output_result(), time.monotonic())

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.no_gradeable_run_plane is True
    assert verification.is_fully_satisfied() is False
    reason = _outcome_unverified_reason(ctx, verification)
    assert reason is not None
    assert _NO_GRADEABLE_PROSE in reason
    assert _ADD_OR_FIX_PROSE not in reason


@pytest.mark.asyncio
async def test_chat_only_no_run_fallback_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", _no_handler)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")
    no_run_result: dict[str, Any] = {"ok": False, "data": {}}

    verification = await _maybe_run_completion_verification(ctx, no_run_result, time.monotonic())

    assert verification is None


@pytest.mark.asyncio
async def test_observation_seam_fires_and_persists_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", _no_handler)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")
    ctx.post_run_page_observation_after_failed_test = True

    verification = await _maybe_run_completion_verification_from_page_observation(
        ctx, url="https://example.com/orders/A1B2C3", title="Order Confirmation"
    )

    assert verification is not None
    assert verification.no_gradeable_run_plane is True
    assert ctx.completion_verification_result is verification


@pytest.mark.asyncio
async def test_observation_seam_non_candidate_does_not_fire() -> None:
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")
    ctx.post_run_page_observation_after_failed_test = False

    verification = await _maybe_run_completion_verification_from_page_observation(ctx, url="https://example.com/orders")

    assert verification is None


def test_empty_fallback_marker_records_built_unverified_not_no_meaningful_output() -> None:
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(block_type="extraction", label="lookup")])
    )
    ctx.verified_prefix_labels = ["lookup"]
    marker = CompletionVerificationResult(
        status="evaluated", criterion_ids=[], verdicts=[], no_gradeable_run_plane=True
    )

    recorded = _record_run_blocks_result(ctx, _real_output_result(), completion_verification=marker)

    assert isinstance(recorded, RecordedRunOutcome)
    assert recorded.verdict == "not_demonstrated"
    assert recorded.reason_code != "no_meaningful_output"
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False
    assert _NO_GRADEABLE_PROSE in (recorded.display_reason or "")


def test_marker_does_not_leak_into_verdict_consumers() -> None:
    empty = combine_verification_results([], None, [])
    marked = CompletionVerificationResult(
        status="evaluated", criterion_ids=[], verdicts=[], no_gradeable_run_plane=True
    )
    assert marked.verdict_state_counts() == empty.verdict_state_counts()
    trace = marked.to_trace_data()
    assert trace["no_gradeable_run_plane"] is True
    assert trace["unmet_criterion_ids"] == []
    assert trace["missing_evidence"] == []
    assert trace["satisfied_count"] == 0
    assert trace["fully_satisfied"] is False


def test_kept_floor_rejects_definition_only_and_accepts_run_plane() -> None:
    definition_only = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="satisfied",
                reason_code="definition_parameters_referenced",
                evidence_ref="workflow_yaml:first_name",
            )
        ],
    )
    assert all(v.satisfied for v in definition_only.verdicts)
    assert definition_only.is_fully_satisfied() is False

    run_satisfied = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )
    assert run_satisfied.is_fully_satisfied() is True


def test_unsatisfied_definition_verdict_keeps_actionable_prose() -> None:
    unsatisfied = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="definition_parameters_unreferenced",
                missing_evidence="workflow does not reference the parameter",
            )
        ],
    )
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "inputs are reusable", level="definition")]
    )

    reason = _outcome_unverified_reason(ctx, unsatisfied)

    assert reason is not None
    assert _ADD_OR_FIX_PROSE in reason
    assert _NO_GRADEABLE_PROSE not in reason


def test_rollback_anchor_not_recorded_for_definition_only_contract() -> None:
    turn_state = CompletionCriteriaTurnState()
    verification = combine_verification_results(
        ["c0"],
        None,
        [
            CriterionVerdict(
                criterion_id="c0",
                state="satisfied",
                reason_code="definition_parameters_referenced",
                evidence_ref="workflow_yaml:first_name",
            )
        ],
    )

    note_adjudication_on_turn_state(turn_state, verification, fully_satisfied_workflow_yaml="x: y")

    assert turn_state.fully_satisfied_workflow_yaml is None


def test_gated_state_does_not_auto_apply_code_only_success() -> None:
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[], classifier_status="fallback")
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated", criterion_ids=[], verdicts=[], no_gradeable_run_plane=True
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.has_staged_proposal = True
    ctx.staged_workflow = SimpleNamespace()  # type: ignore[assignment]
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False

    assert _should_apply_code_only_success_without_review(ctx, "auto_applicable") is False
