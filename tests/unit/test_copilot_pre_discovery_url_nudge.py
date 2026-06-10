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

from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES,
    PRE_DISCOVERY_URL_QUESTION_NUDGE,
    _pre_discovery_url_question_nudge,
    _response_coverage_nudge,
)


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


_URL_ASK = {
    "type": "ASK_QUESTION",
    "user_response": "I can build that, but I need the exact URL to use as the entry point. Please provide the URL.",
}


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


# ---------------------------------------------------------------------------
# No over-suppression — legitimate INITIAL asks are let through.
# ---------------------------------------------------------------------------


def test_credential_clarification_passes_through() -> None:
    ctx = _Ctx()
    ctx.request_policy = SimpleNamespace(clarification_reason="credential_name_unresolved")
    ask = {
        "type": "ASK_QUESTION",
        "user_response": "Which saved credential should I use for the login page?",
    }
    assert _pre_discovery_url_question_nudge(ctx, ask) is None


def test_loop_clarification_passes_through() -> None:
    ctx = _Ctx()
    ctx.request_policy = SimpleNamespace(clarification_reason="ambiguous_loop_edit")
    ask = {"type": "ASK_QUESTION", "user_response": "Which loop block on the page should I edit?"}
    assert _pre_discovery_url_question_nudge(ctx, ask) is None


def test_conditional_clarification_passes_through() -> None:
    ctx = _Ctx()
    ctx.request_policy = SimpleNamespace(clarification_reason="missing_conditional_condition")
    ask = {"type": "ASK_QUESTION", "user_response": "What condition should gate this site visit?"}
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
