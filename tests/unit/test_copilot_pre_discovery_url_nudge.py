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

import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.build_phase import DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD, BuildPhase
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES,
    PRE_DISCOVERY_URL_QUESTION_NUDGE,
    PRESENT_COMPLETION_CONTRACT_ASK_RETRY,
    _check_enforcement,
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


_REQUESTED_OUTPUT_PATHS = ("output.number_of_new_signups", "output.number_of_website_visitors")


def _output_path_contract_policy(**overrides: object) -> RequestPolicy:
    defaults = dict(
        user_response_policy="proceed",
        clarification_reason="none",
        completion_contract_status="present",
        completion_criteria=[
            CompletionCriterion(
                id="visitors", outcome="The website visitor count is returned.", output_path=_REQUESTED_OUTPUT_PATHS[1]
            ),
            CompletionCriterion(
                id="signups", outcome="The new signup count is returned.", output_path=_REQUESTED_OUTPUT_PATHS[0]
            ),
        ],
    )
    defaults.update(overrides)
    return RequestPolicy(**defaults)


def _output_schema_ask(refs: list[str]) -> dict[str, object]:
    return {
        "type": "ASK_QUESTION",
        "user_response": "Should the output be website visitors and new signups as integers?",
        "ask_subject": "output_schema",
        "refs": refs,
    }


def test_output_schema_ask_with_covering_refs_auto_answers() -> None:
    ctx = _present_contract_ctx(request_policy=_output_path_contract_policy())

    with capture_logs() as logs:
        nudge = _response_coverage_nudge(ctx, _output_schema_ask(list(_REQUESTED_OUTPUT_PATHS)))

    assert nudge is not None
    assert nudge != PRESENT_COMPLETION_CONTRACT_ASK_RETRY
    for path in _REQUESTED_OUTPUT_PATHS:
        assert path in nudge
    events = [entry for entry in logs if entry["event"] == "copilot_ask_subject_auto_answered"]
    assert len(events) == 1
    assert events[0]["subject"] == "output_schema"
    assert set(events[0]["resolved_refs"]) == set(_REQUESTED_OUTPUT_PATHS)


@pytest.mark.parametrize(
    "refs",
    [
        pytest.param([], id="empty"),
        pytest.param(["output.number_of_website_visitors", "output.unknown_field"], id="partial"),
        pytest.param(["output.unknown_field"], id="unresolvable"),
    ],
)
def test_output_schema_ask_without_full_coverage_passes_through(refs: list[str]) -> None:
    ctx = _mid_build_ctx(_output_path_contract_policy())

    with capture_logs() as logs:
        nudge = _response_coverage_nudge(ctx, _output_schema_ask(refs))

    assert nudge is None
    assert not [entry for entry in logs if entry["event"] == "copilot_ask_subject_auto_answered"]


_CREDENTIALS_ASK = {
    "type": "ASK_QUESTION",
    "user_response": "Which saved credential should I use?",
    "ask_subject": "credentials",
}


def test_non_output_schema_subject_ask_passes_through() -> None:
    ctx = _mid_build_ctx(_output_path_contract_policy())

    assert _response_coverage_nudge(ctx, _CREDENTIALS_ASK) is None


@pytest.mark.parametrize(
    "ask",
    [
        pytest.param(_CREDENTIALS_ASK, id="credentials"),
        pytest.param({**_CREDENTIALS_ASK, "ask_subject": "other"}, id="other"),
        pytest.param(_output_schema_ask(["output.unknown_field"]), id="output_schema_unresolvable"),
    ],
)
def test_typed_subject_ask_before_a_genuine_attempt_keeps_the_legacy_retry(ask: dict[str, object]) -> None:
    """A typed subject the contract cannot answer must not buy the ask an early exit past the
    build-first retry; only a resolved auto-answer skips it."""
    ctx = _present_contract_ctx(request_policy=_output_path_contract_policy())

    assert _response_coverage_nudge(ctx, ask) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY


@pytest.mark.parametrize(
    ("ctx_factory", "expected_outcome"),
    [
        pytest.param(
            lambda: _present_contract_ctx(request_policy=_output_path_contract_policy()),
            "build_first_retry",
            id="retried",
        ),
        pytest.param(
            lambda: _mid_build_ctx(_output_path_contract_policy()),
            "reached_user",
            id="reached_user",
        ),
    ],
)
def test_unresolved_typed_ask_logs_which_outcome_it_got(ctx_factory: Callable[[], _Ctx], expected_outcome: str) -> None:
    with capture_logs() as logs:
        _response_coverage_nudge(ctx_factory(), _CREDENTIALS_ASK)

    events = [entry for entry in logs if entry["event"] == "copilot_ask_subject_passed_through"]
    assert len(events) == 1
    assert events[0]["outcome"] == expected_outcome


def test_absent_subject_present_contract_ask_keeps_legacy_retry() -> None:
    ctx = _present_contract_ctx(request_policy=_output_path_contract_policy())

    assert _response_coverage_nudge(ctx, _OUTPUT_CONFIRMATION_ASK) == PRESENT_COMPLETION_CONTRACT_ASK_RETRY


def test_definition_level_output_path_does_not_count_as_coverage() -> None:
    policy = _output_path_contract_policy(
        completion_criteria=[
            CompletionCriterion(
                id="def_visitors",
                outcome="The website visitor count is defined.",
                output_path="output.number_of_website_visitors",
                level="definition",
            ),
            CompletionCriterion(
                id="signups", outcome="The new signup count is returned.", output_path="output.number_of_new_signups"
            ),
        ],
    )
    ctx = _mid_build_ctx(policy)

    assert _response_coverage_nudge(ctx, _output_schema_ask(["output.number_of_website_visitors"])) is None


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


def _rekeyed_criterion(criterion_id: str, outcome: str, path: str, **overrides: object) -> CompletionCriterion:
    defaults = dict(
        id=criterion_id,
        outcome=outcome,
        output_path=None,
        requested_output_floor_rekeyed=True,
        floor_rekeyed_from_path=path,
        kind="outcome",
        level="run",
        pinability="shapeless_valid",
        requested_output_evidence_source="runtime_output",
    )
    defaults.update(overrides)
    return CompletionCriterion(**defaults)


def _rekeyed_contract_policy(**overrides: object) -> RequestPolicy:
    return _output_path_contract_policy(
        completion_criteria=[
            _rekeyed_criterion("visitors", "The website visitor count is returned.", _REQUESTED_OUTPUT_PATHS[1]),
            _rekeyed_criterion("signups", "The new signup count is returned.", _REQUESTED_OUTPUT_PATHS[0]),
        ],
        **overrides,
    )


def _mid_build_ctx(policy: RequestPolicy) -> _Ctx:
    return _present_contract_ctx(request_policy=policy, update_workflow_called=True)


def test_output_schema_ask_auto_answers_after_genuine_attempt() -> None:
    ctx = _mid_build_ctx(_output_path_contract_policy())

    nudge = _response_coverage_nudge(ctx, _output_schema_ask(list(_REQUESTED_OUTPUT_PATHS)))

    assert nudge is not None
    assert nudge != PRESENT_COMPLETION_CONTRACT_ASK_RETRY
    for path in _REQUESTED_OUTPUT_PATHS:
        assert path in nudge


def test_floor_rekeyed_contract_covers_output_schema_ask() -> None:
    ctx = _mid_build_ctx(_rekeyed_contract_policy())

    with capture_logs() as logs:
        nudge = _response_coverage_nudge(ctx, _output_schema_ask(list(_REQUESTED_OUTPUT_PATHS)))

    assert nudge is not None
    events = [entry for entry in logs if entry["event"] == "copilot_ask_subject_auto_answered"]
    assert len(events) == 1
    assert set(events[0]["resolved_refs"]) == set(_REQUESTED_OUTPUT_PATHS)


def test_floor_rekeyed_paths_are_quotable_from_the_prompt_summary() -> None:
    summary = _rekeyed_contract_policy().prompt_summary()

    assert "requested_output_paths:" in summary
    for path in _REQUESTED_OUTPUT_PATHS:
        assert f"- {path}" in summary


def test_credentials_ask_after_genuine_attempt_passes_through() -> None:
    ctx = _mid_build_ctx(_rekeyed_contract_policy())
    ask = {
        "type": "ASK_QUESTION",
        "user_response": "Which saved credential should I use?",
        "ask_subject": "credentials",
    }

    assert _response_coverage_nudge(ctx, ask) is None


def test_uncovered_output_schema_ask_after_genuine_attempt_passes_through() -> None:
    ctx = _mid_build_ctx(_rekeyed_contract_policy())

    with capture_logs() as logs:
        nudge = _response_coverage_nudge(ctx, _output_schema_ask(["output.unknown_field"]))

    assert nudge is None
    assert not [entry for entry in logs if entry["event"] == "copilot_ask_subject_auto_answered"]


def test_definition_level_rekeyed_path_does_not_count_as_coverage() -> None:
    policy = _output_path_contract_policy(
        completion_criteria=[
            _rekeyed_criterion(
                "visitors",
                "The website visitor count is defined.",
                _REQUESTED_OUTPUT_PATHS[1],
                level="definition",
            ),
        ],
    )
    ctx = _mid_build_ctx(policy)

    assert _response_coverage_nudge(ctx, _output_schema_ask([_REQUESTED_OUTPUT_PATHS[1]])) is None


def test_clarification_reason_keeps_output_schema_ask_with_the_user() -> None:
    ctx = _mid_build_ctx(_rekeyed_contract_policy(clarification_reason="credentials"))

    assert _response_coverage_nudge(ctx, _output_schema_ask(list(_REQUESTED_OUTPUT_PATHS))) is None


def _enforcement_ctx(policy: RequestPolicy) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,
    )
    ctx.build_phase = BuildPhase.COMPOSING
    ctx.request_policy = policy
    ctx.turn_intent = _authoring_intent()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.discovery_calls_this_turn = 1
    return ctx


def test_enforcement_auto_answers_rekeyed_output_schema_ask_mid_build() -> None:
    ctx = _enforcement_ctx(_rekeyed_contract_policy())
    result = SimpleNamespace(final_output=json.dumps(_output_schema_ask(list(_REQUESTED_OUTPUT_PATHS))))

    nudge = _check_enforcement(ctx, result)

    assert nudge is not None
    for path in _REQUESTED_OUTPUT_PATHS:
        assert path in nudge


def test_definition_level_rekeyed_path_stays_out_of_the_prompt_summary() -> None:
    policy = _output_path_contract_policy(
        completion_criteria=[
            _rekeyed_criterion(
                "visitors",
                "The website visitor count is defined.",
                _REQUESTED_OUTPUT_PATHS[1],
                level="definition",
            ),
        ],
    )

    assert "requested_output_paths:" not in policy.prompt_summary()
