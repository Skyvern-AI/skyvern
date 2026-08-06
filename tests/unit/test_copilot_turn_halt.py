from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.enforcement import (
    _check_enforcement,
    _maybe_stash_terminal_challenge_halt,
    terminal_challenge_blocker_signal_from_current_page_evidence,
    terminal_challenge_blocker_signal_from_page_evidence,
)
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    RecordedRunOutcome,
)
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHaltKind,
    expire_output_contract_ladder_at_turn_end,
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
    turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.turn_ownership import TurnClaimant, claim_and_stash_blocker_signal
from tests.unit.conftest import make_copilot_context


def _signal(
    *,
    blocker_kind: str = "tool_error",
    internal_reason_code: str = TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
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
            _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool"),
            TurnHaltKind.LOOP_DETECTED,
        ),
        (
            _signal(blocker_kind="loop_detected", internal_reason_code="code_authoring_guardrail_churn"),
            TurnHaltKind.LOOP_DETECTED,
        ),
        (
            _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE),
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
    ],
)
def test_terminal_blockers_map_to_halts(signal: CopilotToolBlockerSignal, expected_kind: TurnHaltKind) -> None:
    halt = turn_halt_from_blocker_signal(signal, source="hook")

    assert halt is not None
    assert halt.kind == expected_kind
    assert halt.blocker_signal is signal


def _halt_ctx(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "turn_halt": None,
        "blocker_signal": None,
        "blocker_signal_claimant": None,
        "turn_ownership": None,
        "gate_precedence_conflict_events": [],
        "latest_tool_blocker_signal": None,
        "tool_blocker_signals": [],
        "output_contract_actuation_by_signature": {},
        "output_contract_actuation_count_by_signature": {},
        "output_contract_pending_run_evidence": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_stash_and_raise_turn_halt_sets_context_once() -> None:
    ctx = _halt_ctx()
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="stream")

    assert halt is ctx.turn_halt
    assert halt is not None
    with pytest.raises(CopilotTurnHalt) as exc_info:
        raise_if_turn_halt(ctx)
    assert exc_info.value.halt is ctx.turn_halt


def test_enforcement_backstop_converts_existing_terminal_blocker_signal() -> None:
    ctx = _halt_ctx(
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
    ctx = _halt_ctx()
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


_TOTP_URL = "https://portal.example.com/account/login/totp?next=%2Freports"
_LOGIN_URL = "https://portal.example.com/account/login?next=%2Freports"
_OTHER_TOTP_URL = "https://other.example.com/account/login/totp"


def _vision_challenge_packet() -> dict[str, object]:
    """A vision-stamped verification verdict on the one-time-code page."""
    return {
        "current_url": _TOTP_URL,
        "inspected_url": _TOTP_URL,
        "observed_after_workflow_run": True,
        "challenge_state": {
            "detected": True,
            "kind": "other",
            "requires_human_verification": True,
            "evidence_source": "vision",
        },
    }


def _captcha_widget_packet() -> dict[str, object]:
    """A rendered challenge widget on the same page: a control the copilot must actually solve."""
    return {
        "current_url": _TOTP_URL,
        "inspected_url": _TOTP_URL,
        "observed_after_workflow_run": True,
        "challenge_controls": [{"kind": "recaptcha", "selector": "iframe[title='reCAPTCHA']", "visible": True}],
    }


def _captcha_kind_packet() -> dict[str, object]:
    """A vision-confirmed CAPTCHA carrying no parsed DOM control — named by kind alone."""
    packet = _vision_challenge_packet()
    challenge_state = packet["challenge_state"]
    assert isinstance(challenge_state, dict)
    challenge_state["kind"] = "captcha"
    return packet


def _unknown_kind_packet() -> dict[str, object]:
    """The DOM detector's verdict for an anti-bot vendor it has no name for."""
    packet = _vision_challenge_packet()
    challenge_state = packet["challenge_state"]
    assert isinstance(challenge_state, dict)
    challenge_state["kind"] = "unknown"
    return packet


def _interaction_packet(
    tool: str, selector: str, *, landed_url: str = _TOTP_URL, acted_url: str = _TOTP_URL
) -> dict[str, object]:
    return {
        "current_url": landed_url,
        "inspected_url": landed_url,
        "source_tool": "scout_interaction",
        "interaction_tool": tool,
        "interaction_selector": selector,
        "interaction_source_url": acted_url,
    }


def _interaction_challenge_packet(tool: str, selector: str) -> dict[str, object]:
    """A click's own post-action observation, which merges the parsed page evidence."""
    return {**_vision_challenge_packet(), **_interaction_packet(tool, selector)}


def _credential_login_ctx(
    *,
    code_filled: bool,
    code_submitted: bool = False,
    challenge_reobserved_before_submit: bool = False,
    challenge_reobserved_after_submit: bool = False,
    challenge_packet: dict[str, object] | None = None,
    intervening_click: bool = False,
    submit_navigated: bool = False,
    enter_submitted: bool = False,
    fill_on_another_page: bool = False,
    fill_acted_elsewhere: bool = False,
) -> SimpleNamespace:
    trajectory: list[dict[str, object]] = [
        {
            "tool_name": "fill_credential_field",
            "selector": "#username",
            "source_url": _LOGIN_URL,
            "credential_field": "username",
        },
        {
            "tool_name": "fill_credential_field",
            "selector": "#password",
            "source_url": _LOGIN_URL,
            "credential_field": "password",
        },
        {"tool_name": "click", "selector": 'button[aria-label="Log in"]', "source_url": _LOGIN_URL},
    ]
    flow_evidence: list[dict[str, object]] = [{"evidence": challenge_packet or _vision_challenge_packet()}]
    if code_filled:
        fill_page = _OTHER_TOTP_URL if fill_on_another_page else _TOTP_URL
        trajectory.append(
            {
                "tool_name": "fill_credential_field",
                "selector": "#totp",
                "source_url": fill_page,
                "credential_field": "totp",
            }
        )
        # A packet whose recorded source is elsewhere must not be credited to the challenged page,
        # even though it landed there and its selector matches.
        acted = _OTHER_TOTP_URL if fill_acted_elsewhere else fill_page
        flow_evidence.append({"evidence": _interaction_packet("fill_credential_field", "#totp", acted_url=acted)})
    if challenge_reobserved_before_submit:
        flow_evidence.append({"evidence": _vision_challenge_packet()})
    if intervening_click:
        trajectory.append({"tool_name": "click", "selector": "#cookie-accept", "source_url": _TOTP_URL})
        flow_evidence.append({"evidence": _interaction_challenge_packet("click", "#cookie-accept")})
    if code_submitted and enter_submitted:
        # An Enter keypress commits the code without minting an interaction observation.
        trajectory.append({"tool_name": "press_key", "key": "Enter", "source_url": _TOTP_URL})
    elif code_submitted:
        trajectory.append({"tool_name": "click", "selector": "#totp-submit", "source_url": _TOTP_URL})
        landed = "https://portal.example.com/reports" if submit_navigated else _TOTP_URL
        flow_evidence.append({"evidence": _interaction_packet("click", "#totp-submit", landed_url=landed)})
    if challenge_reobserved_after_submit:
        flow_evidence.append({"evidence": _vision_challenge_packet()})
    return SimpleNamespace(
        flow_evidence=flow_evidence,
        scout_trajectory=trajectory,
        last_failure_category_top=None,
        last_run_blocks_workflow_run_id="wr_000000000000000000",
    )


def _backstop_ctx(*, code_filled: bool) -> SimpleNamespace:
    login = _credential_login_ctx(code_filled=code_filled)
    return _halt_ctx(
        flow_evidence=login.flow_evidence,
        scout_trajectory=login.scout_trajectory,
        composition_page_evidence=login.flow_evidence[0]["evidence"],
        last_run_outcome=None,
        last_test_ok=False,
        last_test_anti_bot=True,
    )


@pytest.mark.parametrize(
    ("code_filled", "expect_halt"),
    [(True, False), (False, True)],
    ids=["backstop_declines_the_stale_packet_the_code_answered", "backstop_still_halts_without_a_code_fill"],
)
def test_finalize_backstop_shares_the_credential_served_decline(code_filled: bool, expect_halt: bool) -> None:
    ctx = _backstop_ctx(code_filled=code_filled)

    _maybe_stash_terminal_challenge_halt(ctx)

    assert (ctx.turn_halt is not None) is expect_halt


@pytest.mark.parametrize(
    ("ctx", "blocked_tool", "expect_signal"),
    [
        pytest.param(
            _credential_login_ctx(code_filled=True),
            "click",
            False,
            id="declines_on_a_challenge_seen_before_the_code_was_filled",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, code_submitted=True),
            "evaluate",
            False,
            id="stale_challenge_stays_declined_for_the_tool_after_the_submit",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, challenge_reobserved_before_submit=True),
            "click",
            True,
            id="halts_on_a_challenge_observed_after_the_fill_even_before_a_submit",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, code_submitted=True, enter_submitted=True),
            "evaluate",
            False,
            id="an_enter_submit_cannot_extend_the_decline_past_the_fill",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, fill_on_another_page=True),
            "click",
            True,
            id="halts_when_the_code_was_filled_into_a_different_page",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, fill_acted_elsewhere=True),
            "click",
            True,
            id="halts_when_the_fill_packet_acted_on_a_different_page_than_it_landed_on",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, challenge_packet=_unknown_kind_packet()),
            "click",
            True,
            id="halts_on_an_unnamed_anti_bot_kind_the_dom_detector_could_not_classify",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, code_submitted=True, challenge_reobserved_after_submit=True),
            "click",
            True,
            id="halts_when_the_page_still_demands_a_code_after_the_submit",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, intervening_click=True),
            "click",
            True,
            id="halts_when_an_intervening_click_reobserves_the_challenge_itself",
        ),
        pytest.param(
            _credential_login_ctx(
                code_filled=True,
                code_submitted=True,
                submit_navigated=True,
                challenge_reobserved_after_submit=True,
            ),
            "click",
            True,
            id="halts_after_a_navigating_submit_when_the_page_still_demands_a_code",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, challenge_packet=_captcha_widget_packet()),
            "click",
            True,
            id="halts_on_a_rendered_challenge_widget_the_code_cannot_satisfy",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=True, challenge_packet=_captcha_kind_packet()),
            "click",
            True,
            id="halts_on_a_vision_named_captcha_with_no_dom_control",
        ),
        pytest.param(
            _credential_login_ctx(code_filled=False),
            "click",
            True,
            id="halts_when_no_credential_fill_reached_the_challenged_page",
        ),
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


def _involuntary_loop_signal() -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="loop detected",
        user_facing_reason="I couldn't keep going on this turn.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="loop_detected_generic",
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
    return _halt_ctx(
        turn_halt=turn_halt,
        blocker_signal=blocker_signal,
        latest_tool_blocker_signal=latest_tool_blocker_signal,
        tool_blocker_signals=tool_blocker_signals if tool_blocker_signals is not None else [],
    )


def test_verified_outcome_suppresses_and_consumes_involuntary_halt() -> None:
    signal = _involuntary_loop_signal()
    halt = stash_turn_halt_from_blocker_signal(_halt_ctx(), signal, source="enforcement")
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
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool")
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="hook")

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None


def test_verified_outcome_does_not_suppress_voluntary_terminal_challenge() -> None:
    signal = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="hook")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=True)

    assert ctx.turn_halt is not None
    assert ctx.blocker_signal is signal


def test_verified_outcome_does_not_clear_voluntary_blocker_when_involuntary_absent() -> None:
    challenge_signal = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)
    loop_halt = stash_turn_halt_from_blocker_signal(
        _halt_ctx(),
        _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool"),
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
    involuntary = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_consecutive_same_tool")
    voluntary = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)
    ctx = _consume_ctx(blocker_signal=involuntary)
    ctx.latest_tool_blocker_signal = involuntary
    ctx.tool_blocker_signals = [voluntary, involuntary]
    stash_turn_halt_from_blocker_signal(ctx, involuntary, source="hook")

    raise_if_turn_halt(ctx, verified=True)

    assert ctx.latest_tool_blocker_signal is None
    assert ctx.tool_blocker_signals == [voluntary]


def test_involuntary_suppression_lets_later_voluntary_challenge_raise() -> None:
    signal = _involuntary_loop_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement")

    raise_if_turn_halt(ctx, verified=True)
    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None

    challenge_signal = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)
    ctx.blocker_signal = challenge_signal
    stash_turn_halt_from_blocker_signal(ctx, challenge_signal, source="hook")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=True)
    assert ctx.blocker_signal is challenge_signal


def test_unverified_involuntary_halt_still_raises() -> None:
    signal = _involuntary_loop_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx, verified=False)

    assert ctx.turn_halt is not None
    assert ctx.blocker_signal is signal


def test_default_verified_argument_is_fail_safe_and_raises() -> None:
    signal = _involuntary_loop_signal()
    ctx = _consume_ctx(blocker_signal=signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement")

    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)


def _output_contract_ctx(*, granted: bool) -> SimpleNamespace:
    states = {"sig_a": OutputContractAdvisoryState.GRANTED} if granted else {}
    return _halt_ctx(output_contract_actuation_by_signature=states)


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


def test_active_terminal_challenge_promotes_while_ladder_unresolved() -> None:
    ctx = _output_contract_ctx(granted=True)
    signal = _signal(internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE)

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="run_execution")

    assert halt is not None
    assert halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE


def _defer_ledger_ctx() -> SimpleNamespace:
    return _halt_ctx(
        output_contract_actuation_by_signature={"sig_a": OutputContractAdvisoryState.GRANTED},
        output_contract_run_output_observed_by_signature={},
        output_contract_page_extraction_imposed_by_signature={},
        output_contract_pending_run_evidence={"sig_a": ["output.confirmation_number"]},
    )


def _defer_count_ledger_ctx() -> SimpleNamespace:
    return _halt_ctx(
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


def test_turn_end_expiry_noop_without_live_grant() -> None:
    ctx = make_copilot_context()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.CONSUMED

    assert expire_output_contract_ladder_at_turn_end(ctx) is None
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.CONSUMED


def test_mid_turn_expiry_keeps_granted_early_return() -> None:
    ctx = make_copilot_context()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")
    stash_turn_halt_from_blocker_signal(ctx, _loop_signal(), source="enforcement_backstop")

    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.GRANTED


def test_raise_if_turn_halt_retires_halt_outranked_by_live_ladder() -> None:
    ctx = make_copilot_context()
    loop_signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_generic")
    claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, loop_signal)
    stash_turn_halt_from_blocker_signal(ctx, loop_signal, source="test")
    assert ctx.turn_halt is not None
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED

    raise_if_turn_halt(ctx)

    assert ctx.turn_halt is None
    assert any(event.site == "turn_halt" for event in ctx.gate_precedence_conflict_events)
