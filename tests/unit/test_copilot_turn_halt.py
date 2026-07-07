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
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    RecordedRunOutcome,
)
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHaltKind,
    raise_if_turn_halt,
    stash_repair_ceiling_turn_halt,
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
    ctx = SimpleNamespace(
        turn_halt=None,
        latest_tool_blocker_signal=None,
        blocker_signal=None,
        last_artifact_health_blocker_reason=None,
        completion_verification_result=None,
    )
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


@pytest.mark.parametrize(
    ("ctx", "blocked_tool", "expect_signal"),
    [
        pytest.param(
            SimpleNamespace(
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
            ),
            "update_and_run_blocks",
            False,
            id="does_not_halt_before_bounded_attempt",
        ),
        pytest.param(
            SimpleNamespace(
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
            ),
            "evaluate",
            True,
            id="does_not_defer_to_empty_result_shell",
        ),
        pytest.param(
            SimpleNamespace(
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
            ),
            "update_and_run_blocks",
            True,
            id="does_not_defer_to_form_container_text",
        ),
        pytest.param(
            SimpleNamespace(
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
            ),
            "update_and_run_blocks",
            True,
            id="reads_flow_evidence_packets",
        ),
        pytest.param(
            SimpleNamespace(
                last_failure_category_top=None,
                last_run_blocks_workflow_run_id=None,
                composition_page_evidence={
                    "challenge_state": {
                        "detected": True,
                        "kind": "human_verification",
                        "requires_human_verification": False,
                        "gates_submit_controls": False,
                    },
                    "result_containers": [
                        {"selector": "#results", "row_count": 1, "sample_rows": ["Visible result row"]}
                    ],
                },
            ),
            "evaluate",
            False,
            id="defers_to_populated_result_container_evidence",
        ),
    ],
)
def test_current_page_challenge_signal_from_current_page_evidence(
    ctx: SimpleNamespace, blocked_tool: str, expect_signal: bool
) -> None:
    signal = terminal_challenge_blocker_signal_from_current_page_evidence(ctx, blocked_tool=blocked_tool)

    if expect_signal:
        assert signal is not None
        assert signal.internal_reason_code == TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
    else:
        assert signal is None


def _involuntary_repair_ceiling_signal() -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="repair ceiling",
        user_facing_reason="I could not get the run to pass after several repair attempts.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="repair_ceiling_reached",
        blocked_tool="update_and_run_blocks",
        extra={},
    )


def _consume_ctx(
    *,
    turn_halt: object = None,
    blocker_signal: CopilotToolBlockerSignal | None = None,
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None = None,
    tool_blocker_signals: list[CopilotToolBlockerSignal] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        turn_halt=turn_halt,
        blocker_signal=blocker_signal,
        latest_tool_blocker_signal=latest_tool_blocker_signal,
        tool_blocker_signals=tool_blocker_signals if tool_blocker_signals is not None else [],
    )


def test_verified_outcome_suppresses_and_consumes_involuntary_halt() -> None:
    signal = _involuntary_repair_ceiling_signal()
    halt = stash_repair_ceiling_turn_halt(SimpleNamespace(turn_halt=None), signal, consecutive_identical_repair_count=3)
    ctx = _consume_ctx(
        turn_halt=halt,
        blocker_signal=signal,
        latest_tool_blocker_signal=signal,
        tool_blocker_signals=[signal],
    )

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None
    assert ctx.latest_tool_blocker_signal is None
    assert ctx.tool_blocker_signals == []


def test_verified_outcome_consumes_loop_blocker_signal() -> None:
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="hook")

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None


def test_verified_outcome_does_not_suppress_voluntary_terminal_challenge() -> None:
    signal = _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE)
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="hook")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is not None
    assert ctx.blocker_signal is signal


def test_verified_outcome_does_not_clear_voluntary_blocker_when_involuntary_absent() -> None:
    challenge_signal = _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE)
    loop_halt = stash_turn_halt_from_blocker_signal(
        SimpleNamespace(turn_halt=None),
        _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step"),
        source="hook",
    )
    ctx = _consume_ctx(
        turn_halt=loop_halt,
        blocker_signal=challenge_signal,
        latest_tool_blocker_signal=challenge_signal,
        tool_blocker_signals=[challenge_signal],
    )

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is challenge_signal
    assert ctx.latest_tool_blocker_signal is challenge_signal
    assert ctx.tool_blocker_signals == [challenge_signal]


def test_verified_outcome_consumes_involuntary_tool_blocker_history() -> None:
    involuntary = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")
    voluntary = _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE)
    ctx = _consume_ctx(blocker_signal=involuntary)
    ctx.latest_tool_blocker_signal = involuntary
    ctx.tool_blocker_signals = [voluntary, involuntary]
    stash_turn_halt_from_blocker_signal(ctx, involuntary, source="hook")

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.latest_tool_blocker_signal is None
    assert ctx.tool_blocker_signals == [voluntary]


def test_involuntary_suppression_lets_later_voluntary_challenge_raise() -> None:
    signal = _involuntary_repair_ceiling_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_repair_ceiling_turn_halt(ctx, signal, consecutive_identical_repair_count=3)

    raise_if_turn_halt(ctx, verified=True)
    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None

    challenge_signal = _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE)
    ctx.blocker_signal = challenge_signal
    stash_turn_halt_from_blocker_signal(ctx, challenge_signal, source="hook")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=True)
    assert ctx.blocker_signal is challenge_signal


def test_unverified_involuntary_halt_still_raises() -> None:
    signal = _involuntary_repair_ceiling_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_repair_ceiling_turn_halt(ctx, signal, consecutive_identical_repair_count=3)

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=False)

    assert ctx.turn_halt is not None
    assert ctx.blocker_signal is signal


def test_default_verified_argument_is_fail_safe_and_raises() -> None:
    signal = _involuntary_repair_ceiling_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_repair_ceiling_turn_halt(ctx, signal, consecutive_identical_repair_count=3)

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)


def _output_contract_ctx(*, granted: bool) -> SimpleNamespace:
    states = {"sig_a": OutputContractAdvisoryState.GRANTED} if granted else {}
    return SimpleNamespace(
        turn_halt=None,
        output_contract_actuation_by_signature=states,
        output_contract_actuation_count_by_signature={},
    )


def test_loop_detected_deferred_while_output_contract_ladder_unresolved() -> None:
    ctx = _output_contract_ctx(granted=True)
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="code_authoring_guardrail_churn")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement_backstop")

    assert halt is None
    assert ctx.turn_halt is None


def test_loop_detected_promotes_once_output_contract_ladder_resolves() -> None:
    ctx = _output_contract_ctx(granted=False)
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="code_authoring_guardrail_churn")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement_backstop")

    assert halt is not None
    assert halt.kind == TurnHaltKind.LOOP_DETECTED


def test_output_source_unobservable_terminal_promotes_even_while_ladder_unresolved() -> None:
    ctx = _output_contract_ctx(granted=True)
    signal = _signal(blocker_kind="tool_error", internal_reason_code="output_source_unobservable")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="workflow_update")

    assert halt is not None
    assert halt.kind == TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE


def test_active_terminal_challenge_promotes_while_ladder_unresolved() -> None:
    ctx = _output_contract_ctx(granted=True)
    signal = _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE)

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="run_execution")

    assert halt is not None
    assert halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE


def _defer_ledger_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        turn_halt=None,
        blocker_signal=None,
        latest_tool_blocker_signal=None,
        tool_blocker_signals=[],
        output_contract_actuation_by_signature={"sig_a": OutputContractAdvisoryState.GRANTED},
        output_contract_actuation_count_by_signature={},
        output_contract_run_output_observed_by_signature={},
        output_contract_page_extraction_imposed_by_signature={},
        output_contract_pending_run_evidence={"sig_a": ["output.confirmation_number"]},
    )


def _defer_count_ledger_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        turn_halt=None,
        blocker_signal=None,
        latest_tool_blocker_signal=None,
        tool_blocker_signals=[],
        output_contract_actuation_by_signature={"sig_a": OutputContractAdvisoryState.UNUSED},
        output_contract_actuation_count_by_signature={"sig_a": 1},
        output_contract_run_output_observed_by_signature={},
        output_contract_page_extraction_imposed_by_signature={},
        output_contract_pending_run_evidence={"sig_a": ["output.confirmation_number"]},
    )


def _loop_signal() -> CopilotToolBlockerSignal:
    return _signal(blocker_kind="loop_detected", internal_reason_code="code_authoring_guardrail_churn")


def test_defer_swallows_first_loop_signal_and_snapshots_progress() -> None:
    ctx = _defer_ledger_ctx()
    assert stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop") is None
    assert ctx.turn_halt is None
    assert ctx.output_contract_defer_progress_token is not None


def test_defer_never_expires_granted_grant_awaiting_forced_dispatch() -> None:
    ctx = _defer_ledger_ctx()
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.GRANTED


def test_defer_expires_countonly_ladder_into_typed_terminal() -> None:
    ctx = _defer_count_ledger_ctx()
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED


def test_defer_re_arms_when_lifecycle_advances() -> None:
    ctx = _defer_ledger_ctx()
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    ctx.output_contract_run_output_observed_by_signature["sig_a"] = True
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.GRANTED


def test_defer_countonly_ladder_reaches_a_terminal_within_two_stalled_signals() -> None:
    ctx = _defer_count_ledger_ctx()
    for _ in range(6):
        stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
        if ctx.turn_halt is not None:
            break
    assert ctx.turn_halt is not None
