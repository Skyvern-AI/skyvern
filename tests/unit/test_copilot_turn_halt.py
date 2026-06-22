from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
from skyvern.forge.sdk.copilot.enforcement import (
    _check_enforcement,
    _maybe_stash_terminal_challenge_halt,
    terminal_challenge_blocker_signal_from_current_page_evidence,
    terminal_challenge_blocker_signal_from_page_evidence,
)
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    RecordedRunOutcome,
)
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHaltKind,
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
    turn_halt_from_blocker_signal,
)


def _signal(
    *,
    blocker_kind: str = "tool_error",
    internal_reason_code: str = ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
    renders_final_reply: bool = True,
    extra: dict[str, object] | None = None,
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=blocker_kind,
        agent_steering_text="terminal blocker",
        user_facing_reason="The page appears blocked by a site challenge.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=renders_final_reply,
        internal_reason_code=internal_reason_code,
        blocked_tool="update_and_run_blocks",
        extra=extra or {},
    )


@pytest.mark.parametrize(
    ("signal", "expected_kind"),
    [
        (
            _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step"),
            TurnHaltKind.LOOP_DETECTED,
        ),
        (
            _signal(blocker_kind="loop_detected", internal_reason_code="code_authoring_guardrail_churn"),
            TurnHaltKind.LOOP_DETECTED,
        ),
        (
            _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE),
            TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        ),
        (
            _signal(internal_reason_code="tool_error_run_output_terminal_blocker"),
            TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        ),
        (
            _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE),
            TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        ),
        (_signal(internal_reason_code="probable_site_block_stop"), TurnHaltKind.PROBABLE_SITE_BLOCK),
    ],
)
def test_terminal_blockers_map_to_halts(signal: CopilotToolBlockerSignal, expected_kind: TurnHaltKind) -> None:
    halt = turn_halt_from_blocker_signal(signal, source="hook")

    assert halt is not None
    assert halt.kind == expected_kind
    assert halt.blocker_signal is signal


def test_stash_and_raise_turn_halt_sets_context_once() -> None:
    ctx = SimpleNamespace(turn_halt=None)
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="stream")

    assert halt is ctx.turn_halt
    assert halt is not None
    with pytest.raises(CopilotTurnHalt) as exc_info:
        raise_if_turn_halt(ctx)
    assert exc_info.value.halt is ctx.turn_halt


def test_enforcement_backstop_converts_existing_terminal_blocker_signal() -> None:
    ctx = SimpleNamespace(turn_halt=None, latest_tool_blocker_signal=None, blocker_signal=None)
    signal = _signal()
    stash_blocker_signal(ctx, signal)

    with pytest.raises(CopilotTurnHalt) as exc_info:
        _check_enforcement(ctx)

    assert ctx.turn_halt is exc_info.value.halt
    assert exc_info.value.halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE


def test_terminal_challenge_halt_preserves_signal_extra() -> None:
    ctx = SimpleNamespace(turn_halt=None)
    signal = _signal(
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        extra={
            "run_outcome_reason_code": "terminal_challenge_blocker",
            "evidence_source": "failure_category",
            "source": "signal_extra",
        },
    )

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="run_execution")

    assert halt is not None
    assert halt.extra["source"] == "run_execution"
    assert halt.extra["run_outcome_reason_code"] == "terminal_challenge_blocker"
    assert halt.extra["evidence_source"] == "failure_category"


def test_terminal_challenge_backstop_preserves_existing_halt() -> None:
    signal = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)
    existing_halt = turn_halt_from_blocker_signal(signal, source="run_execution")
    ctx = SimpleNamespace(
        turn_halt=existing_halt,
        blocker_signal=None,
        latest_tool_blocker_signal=None,
        last_run_outcome=RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code=TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
            display_reason="Challenge detected.",
        ),
    )

    _maybe_stash_terminal_challenge_halt(ctx)

    assert ctx.turn_halt is existing_halt
    assert ctx.blocker_signal is None


def test_page_challenge_signal_records_explicit_terminal_blocker() -> None:
    ctx = SimpleNamespace(
        last_run_blocks_workflow_run_id=None,
        composition_page_evidence={
            "challenge_state": {
                "detected": True,
                "kind": "human_verification",
                "requires_human_verification": True,
                "gates_submit_controls": True,
                "gated_submit_controls": [{"text": "Search", "disabled": True}],
            },
        },
    )

    signal = terminal_challenge_blocker_signal_from_page_evidence(ctx, blocked_tool="update_and_run_blocks")

    assert signal is not None
    assert signal.internal_reason_code == TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
    assert signal.extra["run_outcome_reason_code"] == TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE
    assert signal.extra["evidence_source"] == "page_evidence"
    assert signal.extra["evidence_reason"] == "human verification requires manual completion"
    assert signal.extra["workflow_run_id"] is None
    assert signal.blocked_tool == "update_and_run_blocks"


def test_current_page_challenge_signal_does_not_halt_before_bounded_attempt() -> None:
    ctx = SimpleNamespace(
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id=None,
        composition_page_evidence={
            "challenge_state": {
                "detected": True,
                "kind": "human_verification",
                "requires_human_verification": True,
                "gates_submit_controls": True,
                "gated_submit_controls": [{"text": "Search", "disabled": True}],
            },
        },
    )

    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool="update_and_run_blocks",
    )

    assert signal is None


def test_current_page_challenge_signal_does_not_defer_to_empty_result_shell() -> None:
    ctx = SimpleNamespace(
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id=None,
        composition_page_evidence={
            "observed_after_workflow_run": True,
            "challenge_state": {
                "detected": True,
                "kind": "human_verification",
                "requires_human_verification": True,
                "gates_submit_controls": True,
                "gated_submit_controls": [{"text": "Search", "disabled": True}],
            },
            "result_containers": [{"selector": "#results", "text_excerpt": "Results"}],
        },
    )

    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool="evaluate",
    )

    assert signal is not None
    assert signal.internal_reason_code == TERMINAL_CHALLENGE_BLOCKER_REASON_CODE


def test_current_page_challenge_signal_does_not_defer_to_form_container_text() -> None:
    ctx = SimpleNamespace(
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id=None,
        composition_page_evidence={
            "observed_after_workflow_run": True,
            "challenge_state": {
                "detected": True,
                "kind": "captcha",
                "requires_human_verification": True,
                "gates_submit_controls": True,
                "gated_submit_controls": [{"text": "Search", "disabled": True}],
            },
            "result_containers": [
                {
                    "tag": "form",
                    "selector": "#record-search",
                    "text_excerpt": (
                        "First name Last name Results No records are available because the anti-bot "
                        "challenge prevented the search from running."
                    ),
                }
            ],
        },
    )

    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool="update_and_run_blocks",
    )

    assert signal is not None
    assert signal.internal_reason_code == TERMINAL_CHALLENGE_BLOCKER_REASON_CODE


def test_current_page_challenge_signal_reads_flow_evidence_packets() -> None:
    ctx = SimpleNamespace(
        flow_evidence=[
            {
                "observation_step": 1,
                "evidence": {
                    "observed_after_workflow_run": True,
                    "challenge_state": {
                        "detected": True,
                        "kind": "captcha",
                        "requires_human_verification": True,
                        "gates_submit_controls": True,
                        "gated_submit_controls": [{"text": "Search", "disabled": True}],
                    },
                    "result_containers": [{"selector": "#results", "text_excerpt": "Results"}],
                },
            }
        ],
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id=None,
    )

    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool="update_and_run_blocks",
    )

    assert signal is not None
    assert signal.internal_reason_code == TERMINAL_CHALLENGE_BLOCKER_REASON_CODE


def test_current_page_challenge_signal_defers_to_populated_result_container_evidence() -> None:
    ctx = SimpleNamespace(
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id=None,
        composition_page_evidence={
            "challenge_state": {
                "detected": True,
                "kind": "human_verification",
                "requires_human_verification": False,
                "gates_submit_controls": False,
            },
            "result_containers": [{"selector": "#results", "row_count": 1, "sample_rows": ["Visible result row"]}],
        },
    )

    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool="evaluate",
    )

    assert signal is None
