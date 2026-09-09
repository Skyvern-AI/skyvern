"""Per-run outcome verdict carried into the narrative stream and payload.

Fixtures model a public registry site with a search form and expandable
result rows; domains and person names are generic placeholders.
"""

from __future__ import annotations

import inspect
import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot import tools as copilot_tools
from skyvern.forge.sdk.copilot.agent import _build_narrative_payload
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import (
    RecordedRunOutcome,
    run_outcome_display_reason,
    trusted_terminal_challenge_category_name,
)
from skyvern.forge.sdk.copilot.tools import run_execution
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _record_executed_block_labels,
    _record_run_blocks_result,
    _recorded_run_outcome,
    _recorded_watchdog_block_receipts,
    _stash_recorded_run_outcome,
    _verify_and_record_run_blocks_result,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotRunOutcomeUpdate
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockType


class _FakeStream:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> bool:
        self.sent.append(data)
        return True


def _code_block(label: str, extracted: Any) -> dict[str, Any]:
    return {"label": label, "block_type": "CODE", "status": "completed", "extracted_data": extracted}


def _run_result(blocks: list[dict[str, Any]], *, ok: bool = True) -> dict[str, Any]:
    return {
        "ok": ok,
        "data": {
            "workflow_run_id": "wr_test",
            "browser_session_id": "pbs_run",
            "overall_status": "completed" if ok else "failed",
            "current_url": "https://registry.example.com/search",
            "blocks": blocks,
        },
    }


def test_recorded_execution_labels_accumulate_across_runs_and_ignore_unexecuted_statuses() -> None:
    ctx = _ctx()

    _record_executed_block_labels(
        ctx,
        _run_result(
            [
                {"label": "completed_step", "status": "completed"},
                {"label": "failed_step", "status": "failed"},
                {"label": "skipped_step", "status": "skipped"},
                {"label": "queued_step", "status": "queued"},
            ],
            ok=False,
        ),
    )
    ctx.block_state_map.clear()
    _record_executed_block_labels(
        ctx,
        _run_result(
            [
                {"label": "timed_out_step", "status": "timed_out"},
                {"label": "skipped_step", "status": "skipped"},
            ],
            ok=False,
        ),
    )

    assert ctx.executed_block_labels == {"completed_step", "failed_step", "timed_out_step"}


def test_recorded_execution_fingerprint_changes_with_the_workflow_shape() -> None:
    ctx = _ctx()
    ctx.workflow_yaml = """
workflow_definition:
  parameters: []
  blocks:
    - block_type: task
      label: step
      prompt: Before
"""

    _record_executed_block_labels(ctx, _run_result([{"label": "step", "status": "completed"}]))
    before = set(ctx.executed_block_fingerprints["step"])
    ctx.workflow_yaml = ctx.workflow_yaml.replace("Before", "After")
    _record_executed_block_labels(ctx, _run_result([{"label": "step", "status": "completed"}]))

    assert before < ctx.executed_block_fingerprints["step"]


def _run_block(label: str, status: str, **fields: Any) -> WorkflowRunBlock:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    return WorkflowRunBlock(
        workflow_run_block_id=f"wrb_{label}",
        workflow_run_id="wr_test",
        organization_id="org",
        block_type=BlockType.CODE,
        label=label,
        status=status,
        created_at=now,
        modified_at=now,
        **fields,
    )


@pytest.mark.asyncio
async def test_watchdog_receipts_carry_why_a_block_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run capped by the watchdog still has to say what failed, not only that something did."""

    async def get_workflow_run_blocks(**_kwargs: Any) -> list[WorkflowRunBlock]:
        return [
            _run_block("waiting", "queued"),
            _run_block(
                "ran",
                "failed",
                failure_reason="CodeBlock failed with NameError at line 2: name 'total' is not defined.",
                error_codes=["user_code_error"],
            ),
        ]

    monkeypatch.setattr(
        run_execution.app.DATABASE,
        "observer",
        SimpleNamespace(get_workflow_run_blocks=get_workflow_run_blocks),
    )

    _rows, receipts = await _recorded_watchdog_block_receipts("wr_test", "org")

    assert [receipt["status"] for receipt in receipts] == ["failed", "queued"]
    assert receipts[0]["block_type"] == "CODE"
    assert receipts[0]["error_codes"] == ["user_code_error"]
    assert "NameError" in receipts[0]["failure_reason"]


def _ctx(blocks: list[dict[str, Any]] | None = None) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="blocks: []",
        browser_session_id=None,
        stream=_FakeStream(),  # type: ignore[arg-type]
        turn_id="turn_test",
        workflow_copilot_chat_id="chat_test",
        user_message="search the public registry for a person and expand their result rows",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c0", outcome="result rows extracted")]
    )
    labels = [block["label"] for block in (blocks or [])]
    workflow_blocks = [SimpleNamespace(block_type="code", label=label) for label in labels]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=workflow_blocks))  # type: ignore[assignment]
    ctx.last_workflow_yaml = "blocks: []"
    ctx.verified_prefix_labels = labels
    ctx.composition_verified_labels = list(labels)
    ctx.last_run_blocks_block_ids = [f"wrb_{label}" for label in labels]
    ctx.last_run_blocks_block_labels = labels
    return ctx


def _blocked_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block("open_registry_search", {"submit_button_enabled": False}),
            _code_block(
                "search_registry_person",
                {
                    "anti_bot_blocked": True,
                    "blocker": "The search form is gated by a human verification challenge; the search never ran.",
                    "has_results": False,
                    "records": [],
                },
            ),
        ]
    )


def _challenge_failure_result() -> dict[str, Any]:
    result = _run_result([], ok=False)
    result["error"] = "The run stopped on a terminal site challenge."
    result["data"]["workflow_run_id"] = "wr_challenge"
    result["data"]["failure_reason"] = "Human verification challenge blocked the search."
    result["data"]["failure_categories"] = [
        {
            "category": "ANTI_BOT_DETECTION",
            "confidence_float": 0.95,
            "reasoning": "Typed run analysis reported an anti-bot challenge.",
            "evidence_source": "challenge_state",
        }
    ]
    result["data"]["blocks"] = [
        {
            "label": "search_registry_person",
            "block_type": "CODE",
            "status": "failed",
            "failure_reason": "Human verification challenge blocked the search.",
        }
    ]
    return result


def _clean_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "search_registry_person",
                {"result_row_count": 1, "visible_results_evidence": "DOE, JANE - Status: Active"},
            ),
        ]
    )


def _evaluated(satisfied: bool) -> CompletionVerificationResult:
    verdict = CriterionVerdict(
        criterion_id="c0",
        state="satisfied" if satisfied else "unsatisfied",
        reason_code="evidence_confirms" if satisfied else "no_evidence",
    )
    return CompletionVerificationResult(status="evaluated", criterion_ids=["c0"], verdicts=[verdict])


def _mixed_observed_reach_state_with_reperception_contradiction() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_reach", "c_reperception"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_reach",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
            CriterionVerdict(
                criterion_id="c_reperception",
                state="unsatisfied",
                reason_code="evidence_contradicts",
                evidence_ref="scout_synthesized_browser_steps_output",
            ),
        ],
    )


def _mixed_observed_reach_state_with_requested_output_contradiction() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_reach", "c_requested_output"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_reach",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
            CriterionVerdict(
                criterion_id="c_requested_output",
                state="unsatisfied",
                reason_code="evidence_contradicts",
                evidence_ref="block_outputs:search_registry_person.confirmation_number",
            ),
        ],
    )


def _run_outcome_frames(stream: _FakeStream) -> list[WorkflowCopilotRunOutcomeUpdate]:
    return [frame for frame in stream.sent if isinstance(frame, WorkflowCopilotRunOutcomeUpdate)]


def test_run_outcome_event_role_defaults_to_recorded() -> None:
    frame = WorkflowCopilotRunOutcomeUpdate.model_validate(
        {
            "type": "run_outcome",
            "workflow_run_id": "wr_test",
            "verdict": "not_evaluated",
            "iteration": 0,
            "timestamp": "2026-06-10T00:00:00Z",
        }
    )

    assert frame.role == "recorded"


@pytest.mark.asyncio
async def test_blocker_run_emits_not_demonstrated() -> None:
    result = _blocked_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_demonstrated"]
    final = frames[-1]
    assert final.reason_code == "blocker_reported"
    assert final.workflow_run_id == "wr_test"
    assert final.workflow_run_block_ids == ["wrb_open_registry_search", "wrb_search_registry_person"]
    assert final.block_labels == ["open_registry_search", "search_registry_person"]
    assert final.display_reason is not None and "human verification challenge" in final.display_reason
    assert final.role == "recorded"
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_run_outcome == RecordedRunOutcome(
        verdict=final.verdict,
        reason_code=final.reason_code,
        display_reason=final.display_reason,
        workflow_run_id="wr_test",
        run_completed=False,
    )
    assert ctx.last_run_outcome_block_labels == final.block_labels


def test_challenge_failure_records_observation_without_halting_agent() -> None:
    result = _challenge_failure_result()
    ctx = _ctx(result["data"]["blocks"])

    outcome = _record_run_blocks_result(ctx, result, completion_verification=None)

    assert outcome == RecordedRunOutcome(
        verdict="not_demonstrated",
        reason_code="blocker_reported",
        display_reason=run_outcome_display_reason("Human verification challenge blocked the search."),
        workflow_run_id="wr_challenge",
        run_completed=False,
    )
    assert ctx.last_run_outcome == outcome
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_anti_bot is not None
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None


def test_challenge_failure_sanitizes_model_observation_reason() -> None:
    result = _challenge_failure_result()
    raw_reason = (
        "Human verification challenge blocked https://user:secret@example.com/path?token=abc "
        "after password=topsecret was submitted."
    )
    result["data"]["failure_reason"] = raw_reason
    result["data"]["blocks"][0]["failure_reason"] = raw_reason
    ctx = _ctx(result["data"]["blocks"])

    outcome = _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.turn_halt is None
    assert outcome is not None
    evidence_reason = outcome.display_reason or ""
    assert re.search(r"https://example\.com", evidence_reason) is not None
    assert "[REDACTED_SECRET]" in evidence_reason
    assert "user:secret" not in evidence_reason
    assert "password=" not in evidence_reason
    assert "topsecret" not in evidence_reason
    assert "token=abc" not in evidence_reason


@pytest.mark.asyncio
async def test_empty_data_run_reports_completion_without_grading_the_output() -> None:
    result = _run_result([_code_block("search_registry_person", {"records": [], "result_count": 0})])
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert frames[-1].reason_code is None
    assert frames[-1].role == "recorded"
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_run_outcome is not None and ctx.last_run_outcome.role == "recorded"


def _terminal_metadata_entry(label: str) -> dict[str, Any]:
    return {
        "block_label": label,
        "declared_goal": "extract result rows for the requested person",
        "claimed_outcomes": [
            {
                "id": "claim:goal",
                "scope": "outcome",
                "text": "result rows extracted",
                "status": "observed_not_verified",
                "covered_criteria": ["criterion:goal_0"],
            }
        ],
        "completion_criteria": [
            {"id": "criterion:goal_0", "text": "result rows extracted", "level": "terminal", "terminal": True}
        ],
    }


@pytest.mark.asyncio
async def test_completion_judge_cannot_overturn_run_output() -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry("search_registry_person")}

    outcome = _record_run_blocks_result(ctx, result, completion_verification=_evaluated(satisfied=False))

    assert outcome == RecordedRunOutcome(
        verdict="not_evaluated",
        workflow_run_id="wr_test",
        run_completed=True,
    )
    assert ctx.completion_verification_result is None
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


@pytest.mark.asyncio
async def test_judge_dissatisfaction_does_not_change_the_verdict() -> None:
    """The outcome derives from what the run produced; a judge re-reading the same run does not."""
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert ctx.last_test_suspicious_success is False


@pytest.mark.asyncio
async def test_completed_run_emits_factual_ungraded_record() -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert frames[-1].reason_code is None
    assert frames[-1].role == "recorded"
    assert frames[-1].browser_session_id == "pbs_run"
    assert frames[-1].workflow_permanent_id == "wp"
    assert frames[-1].turn_id == "turn_test"
    assert frames[-1].workflow_copilot_chat_id == "chat_test"
    assert frames[-1].continuity_source == "workflow_run"
    assert frames[-1].terminal_disposition == "completed"
    assert ctx.last_full_workflow_test_ok is True


@pytest.mark.asyncio
async def test_completed_partial_run_does_not_promote_full_workflow() -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(block_type="code", label="search_registry_person"),
                SimpleNamespace(block_type="code", label="review_results"),
            ]
        )
    )

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert frames[-1].reason_code is None
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_run_outcome == RecordedRunOutcome(
        verdict="not_evaluated",
        workflow_run_id="wr_test",
        run_completed=True,
    )


@pytest.mark.asyncio
async def test_completed_run_needs_no_verification_frame() -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert ctx.last_test_suspicious_success is False


@pytest.mark.asyncio
async def test_failed_run_emits_its_own_outcome() -> None:
    result = _run_result([], ok=False)
    ctx = _ctx()

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_demonstrated"]
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"
    assert ctx.last_run_outcome.run_completed is False


@pytest.mark.asyncio
async def test_recording_error_emits_no_invented_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("recording failed")

    monkeypatch.setattr(run_execution, "_record_run_blocks_result", _boom)
    with pytest.raises(RuntimeError, match="recording failed"):
        await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert _run_outcome_frames(ctx.stream) == []  # type: ignore[arg-type]


def test_failed_rerun_clears_prior_recorded_outcome() -> None:
    ctx = _ctx()
    ctx.last_run_outcome = RecordedRunOutcome(verdict="not_demonstrated", reason_code="blocker_reported")
    ctx.last_run_outcome_block_labels = ["search_registry_person"]

    outcome = _record_run_blocks_result(ctx, _run_result([], ok=False), completion_verification=None)

    assert outcome is not None and outcome.verdict == "not_demonstrated"
    assert ctx.last_run_outcome == outcome


def test_recorded_run_outcome_carries_producing_workflow_run_id() -> None:
    ctx = _ctx([_code_block("search_registry_person", {"records": []})])
    outcome = _record_run_blocks_result(
        ctx,
        _run_result([_code_block("search_registry_person", {"records": []})]),
        completion_verification=_evaluated(satisfied=False),
    )

    assert outcome is not None
    assert outcome.workflow_run_id == "wr_test"
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.workflow_run_id == "wr_test"


def test_completion_reperception_does_not_override_the_empty_output_gate() -> None:
    ctx = _ctx([_code_block("search_registry_person", {"records": []})])

    outcome = _record_run_blocks_result(
        ctx,
        _run_result([_code_block("search_registry_person", {"records": []})]),
        completion_verification=_mixed_observed_reach_state_with_reperception_contradiction(),
    )

    assert outcome is not None and outcome.verdict == "not_evaluated"
    assert ctx.last_run_outcome == outcome
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert outcome.reason_code is None


def test_requested_output_judge_does_not_change_completed_run_record() -> None:
    ctx = _ctx([_code_block("search_registry_person", {"records": []})])

    outcome = _record_run_blocks_result(
        ctx,
        _run_result([_code_block("search_registry_person", {"records": []})]),
        completion_verification=_mixed_observed_reach_state_with_requested_output_contradiction(),
    )

    assert outcome is not None
    assert outcome.verdict == "not_evaluated"
    assert ctx.last_run_outcome == outcome


def test_run_outcome_trace_is_append_only_across_pointer_updates() -> None:
    ctx = _ctx([_code_block("search_registry_person", {"records": []})])
    ctx.last_run_blocks_workflow_run_id = "wr_test"
    committed = _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="not_evaluated"))

    assert committed == RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_test")

    stashed = _stash_recorded_run_outcome(
        ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="blocker_reported",
            workflow_run_id="wr_test",
        ),
    )

    assert stashed.verdict == "not_demonstrated"
    assert ctx.last_run_outcome == stashed
    assert ctx.run_outcome_trace == [committed, stashed]


def test_recorded_outcome_for_new_run_uses_current_run_id() -> None:
    ctx = _ctx([_code_block("search_registry_person", {"records": []})])
    ctx.last_run_outcome = RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_prior")
    ctx.last_run_blocks_workflow_run_id = "wr_test"

    recorded = _recorded_run_outcome(workflow_run_id="wr_test")

    assert recorded is not ctx.last_run_outcome
    assert recorded.workflow_run_id == "wr_test"


@pytest.mark.asyncio
async def test_missing_run_id_does_not_reuse_prior_run_id() -> None:
    result = _clean_run_result()
    result["data"].pop("workflow_run_id")
    ctx = _ctx(result["data"]["blocks"])
    ctx.last_run_outcome = RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_test")
    ctx.last_run_blocks_workflow_run_id = "wr_test"

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["not_evaluated"]
    assert frames[-1].workflow_run_id != "wr_test"


def test_both_consumers_route_through_single_producer() -> None:
    source = inspect.getsource(copilot_tools)
    assert source.count("await _verify_and_record_run_blocks_result(") == 2
    assert source.count("recorded_outcome = await _verify_and_record_run_blocks_result(") == 2
    assert source.count("recorded_outcome=recorded_outcome") == 2
    assert "_record_run_blocks_result(copilot_ctx, result, completion_verification" not in source
    assert "_record_run_blocks_result(copilot_ctx, run_result, completion_verification" not in source
    assert "await _maybe_run_completion_verification(copilot_ctx" not in source


@pytest.mark.parametrize(
    "result_name",
    ["result", "run_result"],
    ids=["run_blocks_tool", "run_updated_workflow_blocks"],
)
def test_each_current_run_consumer_passes_the_recorded_outcome_to_finalization(
    result_name: str,
) -> None:
    source = inspect.getsource(copilot_tools)
    producer = (
        f"recorded_outcome = await _verify_and_record_run_blocks_result(copilot_ctx, {result_name}, handler_start)"
    )
    start = source.index(producer)
    consumer = source[start : start + 700]
    assert f"result={result_name}" in consumer
    assert "recorded_outcome=recorded_outcome" in consumer


@pytest.mark.asyncio
async def test_same_run_recorded_operation_remains_packet_authority_after_raw_result_mutation() -> None:
    result = _run_result(
        [
            {
                "label": "collect_failure_rate",
                "block_type": "CODE",
                "status": "failed",
                "workflow_run_block_id": "wrb_recorded",
                "error_codes": ["browser_operation_failed"],
                "failure_reason": "browser operation failed",
            }
        ],
        ok=False,
    )
    result["data"]["requested_block_labels"] = ["collect_failure_rate"]
    result["data"]["executed_block_labels"] = ["collect_failure_rate"]
    result["data"]["failing_code_line"] = 1
    ctx = _ctx(result["data"]["blocks"])
    ctx.workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: collect_failure_rate
      code: |
        return await page.locator("canvas.failure-rate").inner_text()
"""
    ctx.persisted_workflow_yaml = ctx.workflow_yaml

    recorded_outcome = await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())
    assert recorded_outcome is not None
    assert recorded_outcome.failed_operation is not None
    assert recorded_outcome.failed_operation.workflow_run_block_id == "wrb_recorded"

    result["data"]["blocks"][0]["workflow_run_block_id"] = "wrb_mutated"
    run_execution.finalize_build_test_result(
        ctx,
        source_tool="run_blocks_and_collect_debug",
        result=result,
        recorded_outcome=recorded_outcome,
    )

    packet = result["data"]["build_test_packet"]
    assert packet["failure"]["failed_operation"]["workflow_run_block_id"] == "wrb_recorded"


def test_display_reason_collapses_whitespace_and_caps_length() -> None:
    assert run_outcome_display_reason("  a\n  b  ") == "a b"
    at_limit = "x" * 160
    assert run_outcome_display_reason(at_limit) == at_limit
    long_text = "x" * 500
    capped = run_outcome_display_reason(long_text)
    assert capped == "x" * 160
    assert run_outcome_display_reason("   ") is None
    assert run_outcome_display_reason(None) is None


def test_display_reason_preserves_playwright_error_code_crossing_character_limit() -> None:
    error_code = "net::ERR_TUNNEL_CONNECTION_FAILED"
    leading_context = (
        "Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): Failed to execute code block. "
        "Reason: Error: Page.goto: retry exhausted: "
    )
    reason = f"{leading_context}{error_code} at https://example.com/path Call log: navigating to the target"
    old_clamp = reason[:160]

    assert 0 < 160 - len(leading_context) < len(error_code)
    assert old_clamp.endswith(error_code[: 160 - len(leading_context)])
    assert error_code not in old_clamp

    display_reason = run_outcome_display_reason(reason)

    assert display_reason is not None
    assert display_reason.endswith(error_code)
    assert display_reason.rsplit(" ... ", maxsplit=1)[-1] == error_code
    assert len(display_reason) <= 160


def test_display_reason_adaptively_preserves_long_url_origin_crossing_character_limit() -> None:
    origin = f"https://{'a' * 48}.{'b' * 48}.example.com"
    leading_context = "Navigation failed after the browser followed the redirect chain: "
    normalized_reason = f"{leading_context}{origin}"
    old_clamp = normalized_reason[:160]

    assert len(origin) > (160 - len(" ... ")) // 2
    assert 0 < 160 - len(leading_context) < len(origin)
    assert old_clamp.endswith(origin[: 160 - len(leading_context)])
    assert origin not in old_clamp

    display_reason = run_outcome_display_reason(f"{normalized_reason}/path/to/failure")

    assert display_reason is not None
    assert display_reason.endswith(origin)
    assert display_reason != origin
    assert " ... " in display_reason
    assert len(display_reason) <= 160


def test_display_reason_relocation_does_not_sever_an_earlier_error_code() -> None:
    error_code = "net::ERR_TUNNEL_CONNECTION_FAILED"
    leading_context = "Navigation failed: "
    between_tokens = " while retrying the redirected request at "
    origin = f"https://{'a' * 42}.{'b' * 41}.example.com"
    normalized_reason = f"{leading_context}{error_code}{between_tokens}{origin}"
    origin_start = normalized_reason.index(origin)
    relocated_cut = 160 - len(" ... ") - len(origin)

    assert 0 < 160 - origin_start < len(origin)
    assert len(leading_context) < relocated_cut < len(leading_context) + len(error_code)

    display_reason = run_outcome_display_reason(f"{normalized_reason}/failure")

    assert display_reason == f"{leading_context.rstrip()} ... {origin}"
    assert error_code not in display_reason
    assert "net::ERR_" not in display_reason


def test_display_reason_does_not_relocate_error_prefix_with_identifier_suffix() -> None:
    leading_context = f"{'x' * 148} "
    invalid_token = "net::ERR_FOObar"
    valid_token = "net::ERR_FOO"
    invalid_reason = f"{leading_context}{invalid_token} trailing"
    valid_reason = f"{leading_context}{valid_token} trailing"
    valid_display_reason = run_outcome_display_reason(valid_reason)

    assert run_outcome_display_reason(invalid_reason) == invalid_reason[:160]
    assert valid_display_reason is not None
    assert valid_display_reason != valid_reason[:160]
    assert valid_display_reason.endswith(valid_token)


def test_display_reason_does_not_relocate_error_prefix_with_identifier_prefix() -> None:
    leading_context = "x" * 149
    invalid_token = "xnet::ERR_ABC"
    valid_token = "net::ERR_ABC"
    invalid_reason = f"{leading_context}{invalid_token} trailing"
    valid_reason = f"{leading_context} {valid_token} trailing"
    valid_display_reason = run_outcome_display_reason(valid_reason)

    assert run_outcome_display_reason(invalid_reason) == invalid_reason[:160]
    assert valid_display_reason is not None
    assert valid_display_reason != valid_reason[:160]
    assert valid_display_reason.endswith(valid_token)


@pytest.mark.parametrize(
    ("token_length", "expected_kind"),
    [(154, "prefix_and_token"), (155, "token"), (156, "token"), (157, "prefix"), (159, "prefix"), (160, "prefix")],
)
def test_display_reason_reserves_marker_at_protected_token_budget_boundaries(
    token_length: int, expected_kind: str
) -> None:
    error_code = f"net::ERR_{'A' * (token_length - len('net::ERR_'))}"
    leading_context = "context: "
    reason = f"{leading_context}{error_code} trailing"

    display_reason = run_outcome_display_reason(reason)

    assert display_reason is not None
    assert len(display_reason) <= 160
    assert "..." in display_reason
    if expected_kind == "prefix_and_token":
        assert display_reason.startswith(leading_context[:1])
        assert display_reason.endswith(error_code)
    elif expected_kind == "token":
        assert display_reason == f"... {error_code}"
    else:
        assert display_reason == f"{leading_context.rstrip()} ..."
        assert error_code not in display_reason


@pytest.mark.parametrize(
    "protected_token",
    [f"net::ERR_{'A' * 170}", f"https://{'a' * 170}.example.com"],
)
def test_display_reason_keeps_safe_prose_when_intersected_token_is_oversize(protected_token: str) -> None:
    leading_context = "Failure remained actionable after retries: "
    reason = f"{leading_context}{protected_token} trailing"

    assert run_outcome_display_reason(reason) == f"{leading_context.rstrip()} ..."


def test_display_reason_does_not_sever_earlier_span_when_omitting_oversize_token() -> None:
    leading_context = "x" * 145
    earlier_origin = "http://a.co"
    error_code = f"net::ERR_{'A' * 170}"
    reason = f"{leading_context} {earlier_origin} {error_code} trailing"

    display_reason = run_outcome_display_reason(reason)

    assert display_reason == f"{leading_context} ..."
    assert earlier_origin not in display_reason
    assert "net::ERR_" not in display_reason


def test_display_reason_marks_elision_when_relocation_leaves_no_safe_prefix() -> None:
    earlier_origin = f"https://{'a' * 128}.com"
    target_origin = "https://target.test"
    reason = f"{earlier_origin} {'x' * 8} {target_origin}/failure"

    assert len(earlier_origin) == 140
    assert reason.index(target_origin) == 150
    assert run_outcome_display_reason(reason) == f"... {target_origin}"


def test_display_reason_returns_marker_when_oversize_token_has_no_safe_prefix() -> None:
    error_code = f"net::ERR_{'A' * 170}"

    assert run_outcome_display_reason(f"{error_code} trailing") == "..."


@pytest.mark.parametrize("protected_token", ["https://example.com", "net::ERR_CONNECTION_REFUSED"])
def test_display_reason_only_relocates_boundaries_inside_protected_tokens(protected_token: str) -> None:
    token_positions = {
        "before": 160,
        "inside": 159,
        "after": 160 - len(protected_token),
    }

    for relation, token_start in token_positions.items():
        leading_context = f"{'x' * (token_start - 1)} "
        normalized_reason = f"{leading_context}{protected_token} trailing"
        old_clamp = normalized_reason[:160]
        display_reason = run_outcome_display_reason(normalized_reason)

        assert display_reason is not None
        assert len(display_reason) <= 160
        if relation == "inside":
            assert old_clamp.endswith(protected_token[:1])
            assert display_reason.endswith(protected_token)
        else:
            assert display_reason == old_clamp


def test_display_reason_redacts_secrets_and_url_credentials() -> None:
    reason = run_outcome_display_reason(
        "Blocked at https://user:secret@example.com/path?token=abc after password=topsecret was submitted."
    )

    assert reason is not None
    assert re.search(r"https://example\.com", reason) is not None
    assert "[REDACTED_SECRET]" in reason
    assert "user:secret" not in reason
    assert "password=" not in reason
    assert "topsecret" not in reason
    assert "token=abc" not in reason


def _payload_ctx() -> CopilotContext:
    ctx = _ctx()
    workflow_blocks = [
        SimpleNamespace(block_type=None, label="open_registry_search"),
        SimpleNamespace(block_type=None, label="search_registry_person"),
        SimpleNamespace(block_type=None, label="untested_block"),
    ]
    ctx.staged_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=workflow_blocks))  # type: ignore[assignment]
    ctx.block_state_map = {
        "open_registry_search": "completed",
        "search_registry_person": "completed",
    }
    return ctx


def test_narrative_payload_stamps_outcome_on_adjudicated_labels() -> None:
    ctx = _payload_ctx()
    ctx.last_run_outcome = RecordedRunOutcome(
        verdict="not_demonstrated",
        reason_code="blocker_reported",
        display_reason="The search form is gated by a human verification challenge.",
        role="interim_build_test",
    )
    ctx.last_run_outcome_block_labels = ["open_registry_search", "search_registry_person"]

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary=None)

    by_label = {block["label"]: block for block in payload["blocks"]}
    for label in ("open_registry_search", "search_registry_person"):
        assert by_label[label]["state"] == "completed"
        assert by_label[label]["outcome"] == "not_demonstrated"
        assert by_label[label]["outcomeReason"] == "The search form is gated by a human verification challenge."
        assert by_label[label]["outcomeRole"] == "interim_build_test"
    assert "outcome" not in by_label["untested_block"]
    assert "outcomeReason" not in by_label["untested_block"]
    assert "outcomeRole" not in by_label["untested_block"]


def test_narrative_payload_without_recorded_outcome_has_no_outcome_keys() -> None:
    ctx = _payload_ctx()

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary=None)

    for block in payload["blocks"]:
        assert "outcome" not in block
        assert "outcomeReason" not in block
        assert "outcomeRole" not in block


class TestGenuineAttemptRunStamp:
    def test_ok_run_counts_as_genuine_attempt(self) -> None:
        ctx = _ctx([_code_block("b0", {"records": [{"id": 1}]})])
        _record_run_blocks_result(ctx, _run_result([_code_block("b0", {"records": [{"id": 1}]})]))
        assert ctx.last_test_ok is True
        assert ctx.last_run_blocks_workflow_run_id == "wr_test"
        assert ctx.has_genuine_workflow_attempt() is True

    def test_failed_run_counts_as_genuine_attempt(self) -> None:
        ctx = _ctx([_code_block("b0", {})])
        _record_run_blocks_result(ctx, _run_result([_code_block("b0", {})], ok=False))
        assert ctx.last_test_ok is False
        assert ctx.has_genuine_workflow_attempt() is True

    def test_watchdog_softened_run_counts_as_genuine_attempt(self) -> None:
        ctx = _ctx([_code_block("b0", {})])
        ctx.copilot_total_timeout_exceeded = True
        result = _run_result([_code_block("b0", {})], ok=False)
        result[_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY] = True
        _record_run_blocks_result(ctx, result)
        assert ctx.last_test_ok is None
        assert ctx.last_run_blocks_workflow_run_id == "wr_test"
        assert ctx.has_genuine_workflow_attempt() is True


def test_trusted_terminal_challenge_category_requires_carrier() -> None:
    carried = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "evidence_source": "artifact"}
    keyword = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "evidence_source": "keyword_only"}
    legacy = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9}

    assert trusted_terminal_challenge_category_name(carried) == "ANTI_BOT_DETECTION"
    assert trusted_terminal_challenge_category_name(keyword) is None
    assert trusted_terminal_challenge_category_name(legacy) is None


@pytest.mark.asyncio
async def test_completed_run_adds_no_mandatory_next_action() -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert "next_step" not in result["data"]


@pytest.mark.asyncio
async def test_registered_output_remains_a_fact_without_a_verdict_or_instruction() -> None:
    result = _clean_run_result()
    result["data"]["registered_output_parameter_values"] = [
        {
            "workflow_run_id": "wr_test",
            "output_parameter_key": "extract_document_output",
            "block_label": "extract_document",
            "block_type": "code",
            "value": {"document_name": "Resale Demand Package (Required Statement of Fees - Demand)"},
        }
    ]
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert result["data"]["registered_output_parameter_values"][0]["value"] == {
        "document_name": "Resale Demand Package (Required Statement of Fees - Demand)"
    }
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.verdict == "not_evaluated"
    assert "next_step" not in result["data"]


@pytest.mark.asyncio
async def test_failed_run_carries_no_conclude_signal() -> None:
    result = _run_result([], ok=False)
    ctx = _ctx()

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert "next_step" not in result["data"]


@pytest.mark.asyncio
async def test_conclude_cue_absent_when_nothing_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_result([_code_block("extract_count", {})])
    ctx = _ctx(result["data"]["blocks"])
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.run_execution._record_run_blocks_result",
        lambda *_a, **_k: RecordedRunOutcome(verdict="not_demonstrated"),
    )

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert result["data"].get("next_step") is None
