"""Per-run outcome verdict carried into the narrative stream and payload.

Fixtures model a public registry site with a search form and expandable
result rows; domains and person names are generic placeholders.
"""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot import tools as copilot_tools
from skyvern.forge.sdk.copilot.agent import _build_narrative_payload
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome, run_outcome_display_reason
from skyvern.forge.sdk.copilot.tools import run_execution
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _record_run_blocks_result,
    _verify_and_record_run_blocks_result,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotRunOutcomeUpdate


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
            "overall_status": "completed" if ok else "failed",
            "current_url": "https://registry.example.com/search",
            "blocks": blocks,
        },
    }


def _ctx(blocks: list[dict[str, Any]] | None = None) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="blocks: []",
        browser_session_id=None,
        stream=_FakeStream(),  # type: ignore[arg-type]
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


def _run_outcome_frames(stream: _FakeStream) -> list[WorkflowCopilotRunOutcomeUpdate]:
    return [frame for frame in stream.sent if isinstance(frame, WorkflowCopilotRunOutcomeUpdate)]


@pytest.mark.asyncio
async def test_blocker_run_emits_hold_then_not_demonstrated() -> None:
    result = _blocked_run_result()
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_demonstrated"]
    final = frames[-1]
    assert final.reason_code == "terminal_challenge_blocker"
    assert final.workflow_run_id == "wr_test"
    assert final.workflow_run_block_ids == ["wrb_open_registry_search", "wrb_search_registry_person"]
    assert final.block_labels == ["open_registry_search", "search_registry_person"]
    assert final.display_reason is not None and "human verification challenge" in final.display_reason
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_run_outcome == RecordedRunOutcome(
        verdict=final.verdict,
        reason_code=final.reason_code,
        display_reason=final.display_reason,
        workflow_run_id="wr_test",
    )
    assert ctx.last_run_outcome_block_labels == final.block_labels


def test_challenge_failure_records_terminal_blocker_outcome() -> None:
    result = _challenge_failure_result()
    ctx = _ctx(result["data"]["blocks"])

    outcome = _record_run_blocks_result(ctx, result, completion_verification=None)

    assert outcome == RecordedRunOutcome(
        verdict="not_demonstrated",
        reason_code="terminal_challenge_blocker",
        display_reason=run_outcome_display_reason(
            "Run output reported a blocker: Human verification challenge blocked the search."
        ),
        workflow_run_id="wr_challenge",
    )
    assert ctx.last_run_outcome == outcome
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_anti_bot is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_terminal_challenge_blocker"
    assert ctx.blocker_signal.extra["run_outcome_reason_code"] == "terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.extra["evidence_source"] == "structured_blocker"


def test_challenge_failure_sanitizes_halt_metadata_reason() -> None:
    result = _challenge_failure_result()
    raw_reason = (
        "Human verification challenge blocked https://user:secret@example.com/path?token=abc "
        "after password=topsecret was submitted."
    )
    result["data"]["failure_reason"] = raw_reason
    result["data"]["blocks"][0]["failure_reason"] = raw_reason
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.turn_halt is not None
    evidence_reason = ctx.turn_halt.extra["evidence_reason"]
    assert "https://example.com" in evidence_reason
    assert "[REDACTED_SECRET]" in evidence_reason
    assert "user:secret" not in evidence_reason
    assert "password=" not in evidence_reason
    assert "topsecret" not in evidence_reason
    assert "token=abc" not in evidence_reason


@pytest.mark.asyncio
async def test_empty_data_run_emits_no_meaningful_output() -> None:
    result = _run_result([_code_block("search_registry_person", {"records": [], "result_count": 0})])
    ctx = _ctx(result["data"]["blocks"])

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_demonstrated"]
    assert frames[-1].reason_code == "no_meaningful_output"
    assert ctx.last_test_suspicious_success is True


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
async def test_negative_adjudication_emits_outcome_not_demonstrated(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry("search_registry_person")}

    async def _stub_verification(*args: Any, **kwargs: Any) -> CompletionVerificationResult:
        return _evaluated(satisfied=False)

    monkeypatch.setattr(run_execution, "_maybe_run_completion_verification", _stub_verification)
    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_demonstrated"]
    final = frames[-1]
    assert final.reason_code == "outcome_not_demonstrated"
    assert final.display_reason is not None and "did not demonstrate the goal outcome" in final.display_reason
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_run_outcome is not None and ctx.last_run_outcome.verdict == "not_demonstrated"


@pytest.mark.asyncio
async def test_mid_build_fall_through_still_emits_not_demonstrated(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    async def _stub_verification(*args: Any, **kwargs: Any) -> CompletionVerificationResult:
        return _evaluated(satisfied=False)

    monkeypatch.setattr(run_execution, "_maybe_run_completion_verification", _stub_verification)
    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_demonstrated"]
    assert frames[-1].reason_code == "outcome_not_demonstrated"
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False


@pytest.mark.asyncio
async def test_satisfied_adjudication_emits_demonstrated(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    async def _stub_verification(*args: Any, **kwargs: Any) -> CompletionVerificationResult:
        return _evaluated(satisfied=True)

    monkeypatch.setattr(run_execution, "_maybe_run_completion_verification", _stub_verification)
    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "demonstrated"]
    assert frames[-1].reason_code is None
    assert ctx.last_full_workflow_test_ok is True


@pytest.mark.asyncio
async def test_satisfied_adjudication_emits_demonstrated_with_unverified_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    async def _stub_verification(*args: Any, **kwargs: Any) -> CompletionVerificationResult:
        return _evaluated(satisfied=True)

    monkeypatch.setattr(run_execution, "_maybe_run_completion_verification", _stub_verification)
    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "demonstrated"]
    assert frames[-1].reason_code is None
    # A fully-verified completion promotes the full-workflow test result; this is the
    # deterministic terminal path that lets a verified run finalize success.
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_run_outcome == RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_test")


@pytest.mark.asyncio
async def test_verification_skipped_emits_not_evaluated(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    async def _stub_verification(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(run_execution, "_maybe_run_completion_verification", _stub_verification)
    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_evaluated"]
    assert ctx.last_test_suspicious_success is False


@pytest.mark.asyncio
async def test_failed_run_emits_no_frames() -> None:
    result = _run_result([], ok=False)
    ctx = _ctx()

    await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    assert _run_outcome_frames(ctx.stream) == []  # type: ignore[arg-type]
    assert ctx.last_run_outcome is None


@pytest.mark.asyncio
async def test_evaluating_hold_gets_final_frame_when_recording_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _clean_run_result()
    ctx = _ctx(result["data"]["blocks"])

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("recording failed")

    monkeypatch.setattr(run_execution, "_record_run_blocks_result", _boom)
    with pytest.raises(RuntimeError, match="recording failed"):
        await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())

    frames = _run_outcome_frames(ctx.stream)  # type: ignore[arg-type]
    assert [frame.verdict for frame in frames] == ["evaluating", "not_evaluated"]


def test_recorder_returns_none_for_failed_run() -> None:
    result = _run_result([], ok=False)
    ctx = _ctx()
    assert _record_run_blocks_result(ctx, result, completion_verification=None) is None
    assert ctx.last_run_outcome is None


def test_failed_rerun_clears_prior_recorded_outcome() -> None:
    ctx = _ctx()
    ctx.last_run_outcome = RecordedRunOutcome(verdict="not_demonstrated", reason_code="blocker_reported")
    ctx.last_run_outcome_block_labels = ["search_registry_person"]

    _record_run_blocks_result(ctx, _run_result([], ok=False), completion_verification=None)

    assert ctx.last_run_outcome is None
    assert ctx.last_run_outcome_block_labels == []


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


def test_both_consumers_route_through_single_producer() -> None:
    source = inspect.getsource(copilot_tools)
    assert source.count("await _verify_and_record_run_blocks_result(") == 2
    assert "_record_run_blocks_result(copilot_ctx, result, completion_verification" not in source
    assert "_record_run_blocks_result(copilot_ctx, run_result, completion_verification" not in source
    assert "await _maybe_run_completion_verification(copilot_ctx" not in source


def test_display_reason_collapses_whitespace_and_caps_length() -> None:
    assert run_outcome_display_reason("  a\n  b  ") == "a b"
    long_text = "x" * 500
    capped = run_outcome_display_reason(long_text)
    assert capped is not None and len(capped) == 160
    assert run_outcome_display_reason("   ") is None
    assert run_outcome_display_reason(None) is None


def test_display_reason_redacts_secrets_and_url_credentials() -> None:
    reason = run_outcome_display_reason(
        "Blocked at https://user:secret@example.com/path?token=abc after password=topsecret was submitted."
    )

    assert reason is not None
    assert "https://example.com" in reason
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
    )
    ctx.last_run_outcome_block_labels = ["open_registry_search", "search_registry_person"]

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary=None)

    by_label = {block["label"]: block for block in payload["blocks"]}
    for label in ("open_registry_search", "search_registry_person"):
        assert by_label[label]["state"] == "completed"
        assert by_label[label]["outcome"] == "not_demonstrated"
        assert by_label[label]["outcomeReason"] == "The search form is gated by a human verification challenge."
    assert "outcome" not in by_label["untested_block"]
    assert "outcomeReason" not in by_label["untested_block"]


def test_narrative_payload_without_recorded_outcome_has_no_outcome_keys() -> None:
    ctx = _payload_ctx()

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message="done", narrative_summary=None)

    for block in payload["blocks"]:
        assert "outcome" not in block
        assert "outcomeReason" not in block
