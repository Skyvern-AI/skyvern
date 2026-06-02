"""Unit tests for copilot.turn outcome-verification telemetry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace as otel_trace

from skyvern.forge.sdk.copilot import agent as copilot_agent
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.outcome_verification_trace import (
    finalize_outcome_verification_trace,
    outcome_verification_turn_fields,
    record_completion_verification,
    record_gate_decision,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest


def _evaluated_result() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        verdicts=[
            CriterionVerdict(criterion_id="c0", satisfied=True, reason_code="evidence_confirms", evidence_ref="cart"),
            CriterionVerdict(criterion_id="c1", satisfied=False, reason_code="no_evidence"),
        ],
    )


def test_record_completion_verification_populates_evaluated_block() -> None:
    ctx = SimpleNamespace()
    record_completion_verification(ctx, _evaluated_result())

    snapshot = ctx.outcome_verification_trace_snapshot
    assert snapshot["completion_verification_status"] == "evaluated"
    assert snapshot["completion_verification_criterion_count"] == 2
    assert snapshot["completion_verification_satisfied_count"] == 1
    assert snapshot["completion_verification_fully_satisfied"] is False
    assert snapshot["completion_verification_evaluated_on_final_run"] is True


def test_record_completion_verification_replaces_stale_verdict_on_later_unevaluated_run() -> None:
    ctx = SimpleNamespace()
    record_completion_verification(ctx, _evaluated_result())
    # A later recorded run executed but did not evaluate completion criteria.
    record_completion_verification(ctx, None)

    snapshot = ctx.outcome_verification_trace_snapshot
    assert snapshot["completion_verification_evaluated_on_final_run"] is False
    assert snapshot["completion_verification_status"] == "not_run"
    # The prior run's verdict counts must not linger as if they describe this run.
    assert "completion_verification_criterion_count" not in snapshot
    assert "completion_verification_satisfied_count" not in snapshot
    assert "completion_verification_fully_satisfied" not in snapshot


def test_record_gate_decision_merges_into_snapshot() -> None:
    ctx = SimpleNamespace()
    record_gate_decision(ctx, {"gate_satisfied": False, "gate_last_full_workflow_test_ok": False})

    snapshot = ctx.outcome_verification_trace_snapshot
    assert snapshot["gate_satisfied"] is False
    assert snapshot["gate_last_full_workflow_test_ok"] is False


def test_outcome_verification_turn_fields_composes_all_sources() -> None:
    ctx = SimpleNamespace(
        workflow_verification_evidence=WorkflowVerificationEvidence(full_workflow_verified=True),
        request_policy=RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="c0", outcome="cart shows the item", implicit=True),
                CompletionCriterion(id="c1", outcome="submitted via the search bar", method_mandated=True),
            ]
        ),
    )
    record_gate_decision(ctx, {"gate_satisfied": True})
    record_completion_verification(ctx, _evaluated_result())

    fields = outcome_verification_turn_fields(ctx)
    assert fields["gate_satisfied"] is True
    assert fields["completion_verification_status"] == "evaluated"
    assert fields["verification_evidence_full_workflow_verified"] is True
    assert fields["request_policy_completion_criteria_count"] == 2
    assert fields["request_policy_completion_criteria_implicit_count"] == 1
    assert fields["request_policy_completion_criteria_method_mandated_count"] == 1


def test_finalize_is_best_effort_when_source_raises() -> None:
    class Exploding:
        def to_trace_data(self) -> dict[str, Any]:
            raise RuntimeError("boom")

    ctx = SimpleNamespace(workflow_verification_evidence=Exploding())
    with otel_trace.get_tracer("test.finalize").start_as_current_span("copilot.turn") as span:
        finalize_outcome_verification_trace(ctx, span)  # must not raise


def test_finalize_noop_on_none_ctx() -> None:
    finalize_outcome_verification_trace(None)


@pytest.mark.asyncio
async def test_finalize_lands_fields_on_finished_turn_span(span_exporter: Any) -> None:
    ctx = SimpleNamespace(
        workflow_verification_evidence=WorkflowVerificationEvidence(full_workflow_verified=True),
        request_policy=RequestPolicy(completion_criteria=[CompletionCriterion(id="c0", outcome="done")]),
    )
    record_gate_decision(ctx, {"gate_satisfied": True, "gate_last_full_workflow_test_ok": True})
    record_completion_verification(ctx, _evaluated_result())

    with otel_trace.get_tracer("test.finalize").start_as_current_span("copilot.turn") as span:
        finalize_outcome_verification_trace(ctx, span)

    finished = [s for s in span_exporter.get_finished_spans() if s.name == "copilot.turn"]
    assert len(finished) == 1
    attrs = dict(finished[0].attributes or {})
    assert attrs["gate_satisfied"] is True
    assert attrs["completion_verification_status"] == "evaluated"
    assert attrs["verification_evidence_full_workflow_verified"] is True
    assert attrs["request_policy_completion_criteria_count"] == 1


@pytest.mark.asyncio
async def test_run_copilot_agent_finalizes_false_gate_on_turn_span(
    span_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stub_impl(*, ctx_sink: list[Any] | None = None, **_: Any) -> None:
        ctx = SimpleNamespace(
            workflow_verification_evidence=WorkflowVerificationEvidence(test_attempted_but_incomplete=True),
            request_policy=RequestPolicy(),
        )
        record_gate_decision(
            ctx,
            {"gate_satisfied": False, "gate_last_full_workflow_test_ok": False, "gate_evaluated_this_turn": True},
        )
        record_completion_verification(ctx, None)
        if ctx_sink is not None:
            ctx_sink.append(ctx)
        return None

    monkeypatch.setattr(copilot_agent, "_run_copilot_turn_impl", stub_impl)
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_xyz",
        workflow_id="w_001",
        workflow_copilot_chat_id="chat_abc",
        message="add the item to my cart",
        workflow_yaml="",
    )

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
    )

    turn_spans = [s for s in span_exporter.get_finished_spans() if s.name == "copilot.turn"]
    assert len(turn_spans) == 1
    attrs = dict(turn_spans[0].attributes or {})
    assert attrs["gate_satisfied"] is False
    assert attrs["gate_last_full_workflow_test_ok"] is False
    assert attrs["gate_evaluated_this_turn"] is True
    assert attrs["completion_verification_evaluated_on_final_run"] is False
    assert attrs["verification_evidence_test_attempted_but_incomplete"] is True
