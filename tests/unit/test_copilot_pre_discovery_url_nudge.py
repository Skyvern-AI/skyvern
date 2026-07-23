"""Tests for the pre-discovery entry-point-URL ASK gate in _check_enforcement.

A fresh BUILD turn that names a site but no exact URL enters BuildPhase.INITIAL.
The model is supposed to call discover_workflow_entrypoint, but at temperature=1
it occasionally samples straight to an ASK_QUESTION demanding the exact URL. The
gate fires on the structural triple (phase in DISCOVERY_PERMITTED_PHASES,
discovery_calls_this_turn == 0, default clarification_reason) with no text
matching, steering the model to discovery while leaving credential / loop /
conditional clarifications and the post-discovery could-not-resolve ask
untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.build_phase import DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD, BuildPhase
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES,
    PRE_DISCOVERY_URL_QUESTION_NUDGE,
    PRESENT_COMPLETION_CONTRACT_ASK_RETRY,
    _pre_discovery_url_question_nudge,
    _response_coverage_nudge,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode


class _Ctx:
    """Minimal stand-in for CopilotContext used in the pre-discovery gate."""

    def __init__(self) -> None:
        self.build_phase = BuildPhase.INITIAL
        self.discovery_calls_this_turn = 0
        self.pre_discovery_url_question_nudge_count = 0
        self.request_policy = None
        # Fields the post-discovery sibling reads when _response_coverage_nudge runs.
        self.resolved_discovery_entrypoint_url = None
        self.resolved_discovery_failure_reason = None
        self.update_workflow_called = False
        self.no_workflow_nudge_count = 0
        self.coverage_nudge_count = 0
        self.format_nudge_count = 0
        self.test_after_update_done = False
        self.workflow_persisted = False
        self.last_workflow = None
        self.last_update_block_count = None
        self.last_test_ok = None
        self.last_run_blocks_workflow_run_id = None
        self.last_successful_run_blocks_workflow_run_id = None
        self.last_outcome_gate_workflow_run_id = None

    def has_genuine_workflow_attempt(self) -> bool:
        return CopilotContext.has_genuine_workflow_attempt(self)  # type: ignore[arg-type]

    def genuine_attempt_parity_fields(self) -> dict[str, bool | int | str | None]:
        return CopilotContext.genuine_attempt_parity_fields(self)  # type: ignore[arg-type]


_URL_ASK = {
    "type": "ASK_QUESTION",
    "user_response": "I can build that, but I need the exact URL to use as the entry point. Please provide the URL.",
}

_OUTPUT_CONFIRMATION_ASK = {
    "type": "ASK_QUESTION",
    "user_response": "Please confirm the output fields before I build this workflow.",
}


def _authoring_intent(*, mode: TurnIntentMode = TurnIntentMode.BUILD, requires_user_input: bool = False) -> TurnIntent:
    return TurnIntent(
        mode=mode,
        authority=TurnIntentAuthority(
            may_update_workflow=mode in {TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY},
            may_run_blocks=mode in {TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY},
            requires_user_input=requires_user_input,
        ),
    )


def _present_contract_policy(**overrides: object) -> RequestPolicy:
    defaults = dict(
        user_response_policy="proceed",
        clarification_reason="none",
        completion_contract_status="present",
        completion_criteria=[
            CompletionCriterion(id="record_id", outcome="The returned record includes the requested id."),
        ],
    )
    defaults.update(overrides)
    return RequestPolicy(**defaults)


def _present_contract_ctx(**overrides: object) -> _Ctx:
    ctx = _Ctx()
    ctx.build_phase = BuildPhase.COMPOSING
    ctx.request_policy = _present_contract_policy()
    ctx.turn_intent = _authoring_intent()
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def test_pre_discovery_url_ask_in_initial_phase_steers_to_discovery() -> None:
    ctx = _Ctx()
    nudge = _pre_discovery_url_question_nudge(ctx, _URL_ASK)
    assert nudge == PRE_DISCOVERY_URL_QUESTION_NUDGE
    assert ctx.pre_discovery_url_question_nudge_count == 1


def test_pre_discovery_url_ask_fires_through_response_coverage_gate() -> None:
    ctx = _Ctx()
    assert _response_coverage_nudge(ctx, _URL_ASK) == PRE_DISCOVERY_URL_QUESTION_NUDGE


def test_pre_discovery_url_ask_in_discovering_phase_steers_to_discovery() -> None:
    ctx = _Ctx()
    ctx.build_phase = BuildPhase.DISCOVERING
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) == PRE_DISCOVERY_URL_QUESTION_NUDGE


def test_no_nudge_after_discovery_ran_this_turn() -> None:
    ctx = _Ctx()
    ctx.discovery_calls_this_turn = 1
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None


def test_no_nudge_outside_initial_or_discovering_phase() -> None:
    ctx = _Ctx()
    ctx.build_phase = BuildPhase.COMPOSING
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None


def test_no_nudge_for_non_ask_question() -> None:
    ctx = _Ctx()
    reply = {"type": "REPLY", "user_response": "Please provide the URL to start from."}
    assert _pre_discovery_url_question_nudge(ctx, reply) is None


def test_no_nudge_after_discovery_failure_escape() -> None:
    ctx = _Ctx()
    ctx.discovery_failure_streak_this_turn = DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None


def test_no_nudge_when_turn_halt_stashed() -> None:
    ctx = _Ctx()
    ctx.turn_halt = object()
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None


# ---------------------------------------------------------------------------
# No over-suppression — legitimate INITIAL asks are let through.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clarification_reason", "ask_text"),
    [
        pytest.param(
            "credential_name_unresolved",
            "Which saved credential should I use for the login page?",
            id="credential",
        ),
        pytest.param(
            "ambiguous_loop_edit",
            "Which loop block on the page should I edit?",
            id="loop",
        ),
        pytest.param(
            "missing_conditional_condition",
            "What condition should gate this site visit?",
            id="conditional",
        ),
    ],
)
def test_non_default_clarification_reason_passes_through(clarification_reason: str, ask_text: str) -> None:
    ctx = _Ctx()
    ctx.request_policy = SimpleNamespace(clarification_reason=clarification_reason)
    ask = {"type": "ASK_QUESTION", "user_response": ask_text}
    assert _pre_discovery_url_question_nudge(ctx, ask) is None


def test_post_discovery_could_not_resolve_site_name_ask_passes_through() -> None:
    # Discovery ran and failed to resolve a site → discovery_calls_this_turn > 0,
    # so the genuine "which URL?" ask is let through to the user.
    ctx = _Ctx()
    ctx.discovery_calls_this_turn = 1
    ctx.resolved_discovery_failure_reason = "could_not_resolve_site_name"
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None


def test_any_pre_discovery_ask_on_structural_triple_steers_to_discovery() -> None:
    # No text matching: any ASK_QUESTION on the structural triple (INITIAL/0
    # discovery / default clarification_reason) is steered to discovery, even
    # when the phrasing names neither a URL nor a site.
    ctx = _Ctx()
    ask = {"type": "ASK_QUESTION", "user_response": "Where should the agent begin?"}
    assert _pre_discovery_url_question_nudge(ctx, ask) == PRE_DISCOVERY_URL_QUESTION_NUDGE
    assert ctx.pre_discovery_url_question_nudge_count == 1


# ---------------------------------------------------------------------------
# Bounded — the (N+1)th ask is let through.
# ---------------------------------------------------------------------------


def test_pre_discovery_nudge_is_bounded() -> None:
    ctx = _Ctx()
    for expected_count in range(1, MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES + 1):
        assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) == PRE_DISCOVERY_URL_QUESTION_NUDGE
        assert ctx.pre_discovery_url_question_nudge_count == expected_count
    assert _pre_discovery_url_question_nudge(ctx, _URL_ASK) is None
    assert ctx.pre_discovery_url_question_nudge_count == MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES


def test_present_completion_contract_ask_returns_internal_retry() -> None:
    ctx = _present_contract_ctx()

    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY


def test_present_completion_contract_ask_has_no_per_rule_cap() -> None:
    ctx = _present_contract_ctx()

    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY
    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {
                "request_policy": _present_contract_policy(
                    user_response_policy="ask_clarification",
                    clarification_reason="credential_name_unresolved",
                ),
                "turn_intent": _authoring_intent(mode=TurnIntentMode.CLARIFY, requires_user_input=True),
            },
            id="request_policy_clarification",
        ),
        pytest.param(
            {"request_policy": _present_contract_policy(clarification_reason="credential_name_unresolved")},
            id="non_none_clarification_reason",
        ),
        pytest.param(
            {
                "turn_intent": TurnIntent(
                    mode=TurnIntentMode.CLARIFY,
                    authority=TurnIntentAuthority(requires_user_input=True),
                ),
            },
            id="clarify_turn_intent",
        ),
        pytest.param(
            {"turn_intent": _authoring_intent(requires_user_input=True)},
            id="turn_intent_requiring_user_input",
        ),
    ],
)
def test_present_completion_contract_ask_allows_clarification(overrides: dict[str, object]) -> None:
    ctx = _present_contract_ctx(**overrides)

    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) is None


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param({"update_workflow_called": True}, id="persisted_update"),
        pytest.param({"last_update_block_count": 1}, id="persisted_block_count"),
        pytest.param({"last_test_ok": False}, id="failed_build_test"),
        pytest.param({"last_run_blocks_workflow_run_id": "wr_test"}, id="run_id"),
        pytest.param(
            {"last_run_blocks_workflow_run_id": "wr_test", "last_test_ok": None},
            id="watchdog_softened_run_id",
        ),
    ],
)
def test_present_completion_contract_ask_suppressed_by_genuine_attempt(marker: dict[str, object]) -> None:
    ctx = _present_contract_ctx(**marker)

    assert ctx.has_genuine_workflow_attempt() is True
    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) is None


def test_present_completion_contract_ask_admits_after_scout_only_marker() -> None:
    ctx = _present_contract_ctx(test_after_update_done=True)

    assert ctx.has_genuine_workflow_attempt() is False
    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY


_PARITY_MARKER_STATES = [
    pytest.param({}, id="no_markers"),
    pytest.param({"test_after_update_done": True}, id="scout_only"),
    pytest.param({"update_workflow_called": True}, id="persisted_update"),
    pytest.param({"last_update_block_count": 0}, id="zero_block_count"),
    pytest.param({"last_test_ok": True}, id="passed_build_test"),
    pytest.param({"last_test_ok": False}, id="failed_build_test"),
    pytest.param({"last_run_blocks_workflow_run_id": "wr_1"}, id="run_id"),
    pytest.param({"last_outcome_gate_workflow_run_id": "wr_2"}, id="outcome_gate_run_id"),
    pytest.param({"test_after_update_done": True, "last_test_ok": True}, id="scout_and_genuine"),
]


@pytest.mark.parametrize("marker", _PARITY_MARKER_STATES)
def test_recycle_admission_is_superset_of_backstop_block(marker: dict[str, object]) -> None:
    ctx = _present_contract_ctx(**marker)
    workflow_attempted = ctx.has_genuine_workflow_attempt()
    recycle_admits = _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY

    backstop_would_fire = not workflow_attempted
    if backstop_would_fire:
        assert recycle_admits
    else:
        assert not recycle_admits
