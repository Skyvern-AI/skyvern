from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import enforcement as enforcement_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    _criterion_reconcile_key,
    criteria_from_json,
    criteria_to_json,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import (
    REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
    CompletionCriterion,
    JudgmentTruthCondition,
    RequestPolicy,
    _apply_classifier_typed_requested_output_corroborators,
    _apply_requested_output_completion_criteria,
    _apply_validation_classification_completion_criteria,
    _classifier_fallback_policy,
    _criterion_grounding_mode,
    _degrade_pathless_contingent_criteria,
    _parse_completion_criteria,
    _render_active_criteria_for_prompt,
    build_classifier_fallback_floor,
    is_fallback_floor_base_criterion,
    request_policy_has_present_completion_contract,
)
from skyvern.forge.sdk.copilot.request_slots import PROMPT_NAME as REQUEST_SLOT_PROMPT_NAME
from skyvern.forge.sdk.copilot.tools import workflow_update as workflow_update_module
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode
from tests.unit.conftest import make_copilot_context
from tests.unit.copilot_test_helpers import make_completion_criterion as _criterion

_TERMINAL_ACTION_CREATE_OUTCOMES = (
    "A new service request is submitted.",
    "The requested service setup is created.",
    "The account's start-service request is created.",
)
_TERMINAL_ACTION_ABSTAIN = {"version": "1", "criterion_id": None, "terminal_action_family": None}


def _terminal_action_packet(*outcomes: str) -> dict[str, object]:
    return {
        "testing_intent": "require_test",
        "credential_input_kind": "credential_name",
        "credential_refs": ["Portal Login"],
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": outcome,
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
            }
            for outcome in outcomes
        ],
    }


async def _classify_with_terminal_action_reconciliation(
    user_message: str,
    packet: dict[str, object],
    reconciliation: dict[str, object],
) -> tuple[RequestPolicy, list[str]]:
    prompts: list[str] = []

    async def handler(prompt: str, prompt_name: str) -> dict[str, object]:
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return {"version": "1", "slots": []}
        assert prompt_name == request_policy_module.PROMPT_NAME
        prompts.append(prompt)
        if "TERMINAL ACTION RECONCILIATION MODE" in prompt:
            return reconciliation
        return packet

    policy = await request_policy_module._classify_request(user_message, "", [], "", handler)
    return policy, prompts


def _terminal_action_enforcement_ctx(criteria: tuple[CompletionCriterion, ...]) -> CopilotContext:
    ctx = make_copilot_context()
    ctx.turn_intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.completion_criteria_turn_state = SimpleNamespace(decision=SimpleNamespace(criteria=criteria))
    ctx.scout_trajectory = [
        {
            "tool_name": "fill_credential_field",
            "credential_id": "credential_ref",
            "credential_field": "username",
            "trajectory_index": 0,
        },
        {"tool_name": "click", "accessible_name": "Sign in", "trajectory_index": 1},
    ]
    ctx.synthesized_block_offered = True
    ctx.synthesized_block_offered_trajectory_len = len(ctx.scout_trajectory)
    ctx.synthesized_block_offered_goal_complete = enforcement_module.synthesized_trajectory_is_goal_complete(ctx)
    return ctx


async def _policy_for_message(
    user_message: str,
    criteria: list[dict[str, Any]],
    *,
    config: CopilotConfig | None = None,
) -> RequestPolicy:
    """Exercise the retained deterministic fallback canonicalizers directly.

    Fresh classifier responses use typed request-slot binding instead; those integration
    semantics are covered by test_copilot_request_policy_pinability_consumer.py.
    """
    policy = RequestPolicy(
        testing_intent="require_test",
        classifier_status="success",
        classifier_failure_kind="none",
        completion_criteria=_parse_completion_criteria(criteria),
    )
    aliases = config.requested_output_path_aliases if config is not None else {}
    _apply_requested_output_completion_criteria(policy, user_message, aliases)
    _apply_validation_classification_completion_criteria(policy)
    _apply_classifier_typed_requested_output_corroborators(policy)
    _degrade_pathless_contingent_criteria(policy)
    policy.classifier_non_runtime_requested_output_evidence_sources = sorted(
        {
            criterion.requested_output_evidence_source
            for criterion in policy.completion_criteria
            if criterion.output_path is not None and criterion.requested_output_evidence_source != "runtime_output"
        }
    )
    policy.completion_contract_status = "present" if policy.graded_completion_criteria() else "absent"
    return policy


def _stored(*criteria: CompletionCriterion) -> StoredCriteriaSet:
    return StoredCriteriaSet(set_id="wccs_existing", goal_epoch=1, criteria=tuple(criteria))


def _outcomes(policy: RequestPolicy) -> list[str]:
    return [criterion.outcome for criterion in policy.completion_criteria]


def _criteria_for_path(policy: RequestPolicy, output_path: str) -> list[CompletionCriterion]:
    return [criterion for criterion in policy.completion_criteria if criterion.output_path == output_path]


def _criteria_fingerprint(criteria: list[CompletionCriterion]) -> str:
    payload = json.dumps(criteria_to_json(criteria), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("outcome", _TERMINAL_ACTION_CREATE_OUTCOMES)
@pytest.mark.asyncio
async def test_credentialed_create_omission_is_reconciled_before_custody_and_foreclosure(outcome: str) -> None:
    policy, prompts = await _classify_with_terminal_action_reconciliation(
        "Sign in with the saved credential and create the requested service setup.",
        _terminal_action_packet(outcome),
        {"version": "1", "criterion_id": "c0", "terminal_action_family": "request"},
    )

    assert len(prompts) == 2
    assert '"id":"c0"' in prompts[1] and '"kind":"outcome"' in prompts[1]
    stored = criteria_from_json(criteria_to_json(policy.completion_criteria))
    assert [
        (
            item.id,
            item.outcome,
            item.kind,
            item.terminal_action_family,
            item.terminal_action_verification_mode,
        )
        for item in stored
    ] == [("c0", outcome, "terminal_action", "request", "semantic_outcome_v1")]
    assert stored[0].method_mandated is False
    ctx = _terminal_action_enforcement_ctx(stored)
    assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is False
    assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is False
    assert enforcement_module._should_block_mutating_tool_after_synthesized_offer(ctx, "click") is False

    ctx.update_workflow_called = True
    with capture_logs() as logs:
        workflow_update_module._log_imposition_skipped_after_update(ctx)
    (event,) = (entry for entry in logs if entry["event"] == "copilot_imposition_skipped_after_update")
    assert (event["reaches_goal"], event["goal_complete"]) == (False, False)


@pytest.mark.parametrize(
    ("user_message", "outcome"),
    [
        ("Sign in with the saved credential named Portal Login.", "The user is authenticated."),
        ("Sign in with the saved credential and complete MFA.", "Authentication including MFA completes."),
    ],
)
@pytest.mark.asyncio
async def test_login_only_reconciliation_abstains_and_keeps_generic_goal_floor(user_message: str, outcome: str) -> None:
    with capture_logs() as logs:
        policy, prompts = await _classify_with_terminal_action_reconciliation(
            user_message, _terminal_action_packet(outcome), _TERMINAL_ACTION_ABSTAIN
        )

    assert len(prompts) == 2
    assert [(item.id, item.outcome, item.kind) for item in policy.completion_criteria] == [("c0", outcome, "outcome")]
    assert [
        entry["reason"] for entry in logs if entry["event"] == "copilot_terminal_action_reconciliation_omitted"
    ] == ["model_abstained"]
    ctx = _terminal_action_enforcement_ctx(tuple(policy.completion_criteria))
    assert enforcement_module.synthesized_trajectory_reaches_goal(ctx) is True
    assert enforcement_module.synthesized_trajectory_is_goal_complete(ctx) is True


@pytest.mark.asyncio
async def test_ambiguous_terminal_action_reconciliation_abstains_without_rekeying() -> None:
    malformed = {"version": "1", "criterion_id": ["c0", "c1"], "terminal_action_family": "request"}
    with capture_logs() as logs:
        policy, _ = await _classify_with_terminal_action_reconciliation(
            "Sign in and create a service request.",
            _terminal_action_packet("A service request is created.", "The created request is visible."),
            malformed,
        )

    assert [(item.id, item.kind) for item in policy.completion_criteria] == [("c0", "outcome"), ("c1", "outcome")]
    assert [
        entry["reason"] for entry in logs if entry["event"] == "copilot_terminal_action_reconciliation_omitted"
    ] == ["malformed_response"]


@pytest.mark.asyncio
async def test_request_slot_failure_precedes_later_transient_reconciliation_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "testing_intent": "require_test",
        "credential_input_kind": "credential_name",
        "credential_refs": ["Portal Login"],
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The requested service setup is created.",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "create the requested service setup",
            }
        ],
    }
    drifted = {
        **original,
        "completion_criteria": [
            {
                **original["completion_criteria"][0],
                "outcome": "A guessed service setup is created.",
            }
        ],
    }
    classifier_calls = 0

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, object]:
        nonlocal classifier_calls
        assert prompt_name == request_policy_module.PROMPT_NAME
        classifier_calls += 1
        return original if classifier_calls == 1 else drifted

    run_classifier = request_policy_module._run_request_policy_classifier

    async def run_with_transient_reconciliation(
        handler: Any,
        prompt: str,
        *,
        deadline: float | None = None,
    ) -> tuple[Any | None, str, int]:
        if "TERMINAL ACTION RECONCILIATION MODE" in prompt:
            return None, "transient_error", 0
        return await run_classifier(handler, prompt, deadline=deadline)

    monkeypatch.setattr(request_policy_module, "_run_request_policy_classifier", run_with_transient_reconciliation)

    with capture_logs() as logs:
        policy = await request_policy_module._classify_request(
            "Sign in and create the requested service setup.", "", [], "", handler
        )

    assert policy.request_slot_failure_kind == "invalid_anchor_correction"
    (event,) = (entry for entry in logs if entry["event"] == "copilot_terminal_action_reconciliation_omitted")
    assert event["reason"] == "request_slot_failure"
    assert event["failure_kind"] == "request_slot_failure"
    assert event["retryable"] is False
    assert event["request_slot_failure_kind"] == "invalid_anchor_correction"


def test_unknown_request_slot_failure_kind_fails_closed_at_omission_boundary() -> None:
    policy = RequestPolicy(
        credential_input_kind="credential_name",
        request_slot_failure_kind="future_request_slot_failure",
    )

    with capture_logs() as logs:
        request_policy_module._log_terminal_action_reconciliation_omitted(
            policy,
            reason="classifier_failed",
            failure_kind="transient_error",
        )

    (event,) = (entry for entry in logs if entry["event"] == "copilot_terminal_action_reconciliation_omitted")
    assert event["reason"] == "request_slot_failure"
    assert event["failure_kind"] == "request_slot_failure"
    assert event["retryable"] is False
    assert event["request_slot_failure_kind"] == "future_request_slot_failure"


@pytest.mark.asyncio
async def test_terminal_action_reconciliation_gets_its_own_classifier_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []
    # The initial classification may be followed by request-slot correction and
    # production work that legitimately outlives its budget. Reconciliation is
    # a distinct classifier stage and must not inherit that expired deadline.
    monotonic_values = iter((100.0, 101.0, 112.0, 113.0))

    async def bounded_wait_for(awaitable: Awaitable[object], *, timeout: float) -> object:
        timeouts.append(timeout)
        return await awaitable

    timeout_error = request_policy_module.asyncio.TimeoutError
    monkeypatch.setattr(request_policy_module.settings, "COPILOT_REQUEST_POLICY_CLASSIFIER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(request_policy_module, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr(
        request_policy_module,
        "asyncio",
        SimpleNamespace(wait_for=bounded_wait_for, TimeoutError=timeout_error),
    )

    policy, prompts = await _classify_with_terminal_action_reconciliation(
        "Sign in with the saved credential and create the requested service setup.",
        _terminal_action_packet("The requested service setup is created."),
        {"version": "1", "criterion_id": "c0", "terminal_action_family": "request"},
    )

    assert len(prompts) == 2
    assert [(criterion.id, criterion.kind) for criterion in policy.completion_criteria] == [("c0", "terminal_action")]
    assert timeouts == [9.0, 9.0]


def test_p9_exact_value_unpinnable_output_mint_degrades() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c0",
                outcome="The visible page path label is returned.",
                output_path="output.visible_page_path_label",
                expected_output_value="Public start-service path",
                expected_output_shape="status_label",
                pinability="unpinnable",
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return visible_page_path_label.")

    criterion = _criteria_for_path(policy, "output.visible_page_path_label")[0]
    assert criterion.mint_disposition == "degraded"
    assert criterion.mint_degrade == "undecidable_judgment"


def test_p9_classifier_drift_normalizes_to_one_contract_shape() -> None:
    prompt = "Return public_form_exists, visible_page_path_label, and recommended_next_action."
    classifier_shapes = [
        [
            CompletionCriterion(id="c0", outcome="a public form exists for starting service"),
            CompletionCriterion(id="c1", outcome="the visible page path label is returned"),
        ],
        [
            CompletionCriterion(
                id="c0",
                outcome="a public form exists for starting service",
                output_path="output.public_form_exists",
            ),
            CompletionCriterion(id="c1", outcome="the recommended next action is returned"),
        ],
        [
            CompletionCriterion(id="c0", outcome="a public form exists for starting service"),
            CompletionCriterion(id="c1", outcome="the visible page path label is returned"),
            CompletionCriterion(
                id="c2",
                outcome="the public form result is returned",
                output_path="output.public_form_exists",
            ),
            CompletionCriterion(id="c3", outcome="the recommended next action is returned"),
        ],
    ]
    policies = [RequestPolicy(completion_criteria=criteria) for criteria in classifier_shapes]

    for policy in policies:
        _apply_requested_output_completion_criteria(policy, prompt)

    serialized = [criteria_to_json(policy.completion_criteria) for policy in policies]
    assert [len(policy.completion_criteria) for policy in policies] == [3, 3, 3]
    assert serialized == [serialized[0], serialized[0], serialized[0]]


def _requested_output_subset(policy: RequestPolicy, requested_output_paths: set[str]) -> list[CompletionCriterion]:
    return [
        criterion
        for criterion in policy.completion_criteria
        if criterion.level == "run"
        and not criterion.method_mandated
        and criterion.output_path in requested_output_paths
    ]


def _run_outcome_corroborators(policy: RequestPolicy) -> list[CompletionCriterion]:
    return [
        criterion
        for criterion in policy.completion_criteria
        if criterion.level == "run"
        and criterion.kind == "outcome"
        and not criterion.method_mandated
        and criterion.output_path is None
    ]


def test_present_completion_contract_helper_accepts_present_status() -> None:
    policy = RequestPolicy(completion_contract_status="present")

    assert request_policy_has_present_completion_contract(policy) is True


def test_present_completion_contract_helper_accepts_criteria_backed_policy() -> None:
    policy = RequestPolicy(
        completion_contract_status="absent",
        completion_criteria=[CompletionCriterion(id="record_id", outcome="The returned record includes an id.")],
    )

    assert request_policy_has_present_completion_contract(policy) is True


def test_present_completion_contract_helper_rejects_absent_policy() -> None:
    policy = RequestPolicy(completion_contract_status="absent", completion_criteria=[])

    assert request_policy_has_present_completion_contract(policy) is False


def test_completion_criteria_preserve_requested_output_evidence_source() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record selects the best option.",
                "output_path": "output.best_option_selected",
                "expected_output_value": "true",
                "requested_output_evidence_source": "independent_run_evidence",
            },
            {
                "outcome": "The returned record includes status.",
                "output_path": "output.status",
                "expected_output_value": "Active",
            },
            {
                "outcome": "The registered output parameter includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_value": "WTR-1842-DEMO",
                "requested_output_evidence_source": "registered_output_parameter",
            },
            {
                "outcome": "The registered artifact content includes the statement total.",
                "output_path": "output.statement_total",
                "expected_output_value": "$41.00",
                "requested_output_evidence_source": "registered_artifact_content",
            },
            {
                "outcome": "The classifier drifted.",
                "output_path": "output.drifted",
                "requested_output_evidence_source": "output_path_name_heuristic",
            },
        ]
    )

    assert [criterion.requested_output_evidence_source for criterion in criteria] == [
        "independent_run_evidence",
        "runtime_output",
        "registered_output_parameter",
        "registered_artifact_content",
        "runtime_output",
    ]

    round_tripped = criteria_from_json(criteria_to_json(criteria))
    assert [criterion.requested_output_evidence_source for criterion in round_tripped] == [
        "independent_run_evidence",
        "runtime_output",
        "registered_output_parameter",
        "registered_artifact_content",
        "runtime_output",
    ]

    policy = RequestPolicy(completion_criteria=list(round_tripped))
    trace = policy.to_trace_data()
    assert trace["requested_output_criterion_0_evidence_source"] == "independent_run_evidence"
    assert trace["requested_output_criterion_1_evidence_source"] == "runtime_output"
    assert trace["requested_output_criterion_2_evidence_source"] == "registered_output_parameter"
    assert trace["requested_output_criterion_3_evidence_source"] == "registered_artifact_content"


def test_requested_output_canonicalization_preserves_independent_evidence_source() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "The returned record selects the best option.",
                output_path="output.best_option",
                expected_output_value="true",
                requested_output_evidence_source="independent_run_evidence",
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with best option.")

    requested = _criteria_for_path(policy, "output.best_option")
    assert requested
    assert requested[0].requested_output_evidence_source == "independent_run_evidence"


def test_requested_output_canonicalization_inherits_single_independent_selection_source() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "The highest-priority returned record is selected.",
                expected_output_value="true",
                requested_output_evidence_source="independent_run_evidence",
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with document name.")

    requested = _criteria_for_path(policy, "output.document_name")
    assert requested
    assert requested[0].requested_output_evidence_source == "independent_run_evidence"


@pytest.mark.asyncio
async def test_classifier_requested_output_selection_source_survives_canonicalization() -> None:
    policy = await _policy_for_message(
        "Return a final record with document name.",
        [
            {
                "outcome": "The highest-priority returned document is selected.",
                "expected_output_value": "true",
                "requested_output_evidence_source": "independent_run_evidence",
            }
        ],
    )

    requested = _criteria_for_path(policy, "output.document_name")
    trace = policy.to_trace_data()

    assert requested
    assert requested[0].requested_output_evidence_source == "independent_run_evidence"
    assert trace["classifier_non_runtime_requested_output_evidence_source_count"] == 1
    assert trace["classifier_non_runtime_requested_output_evidence_sources"] == ["independent_run_evidence"]


def test_request_policy_trace_exposes_classifier_fallback_status() -> None:
    policy = RequestPolicy(
        classifier_status="fallback",
        classifier_failure_kind="timeout",
        classifier_retry_count=2,
        completion_criteria=[
            _criterion(
                "c0",
                "The returned record includes status.",
                output_path="output.status",
            )
        ],
    )

    trace = policy.to_trace_data()

    assert trace["classifier_status"] == "fallback"
    assert trace["classifier_failure_kind"] == "timeout"
    assert trace["classifier_retry_count"] == 2
    assert trace["classifier_non_runtime_requested_output_evidence_source_count"] == 0
    assert trace["classifier_non_runtime_requested_output_evidence_sources"] == []
    assert trace["requested_output_criterion_0_evidence_source"] == "runtime_output"


@pytest.mark.asyncio
async def test_classifier_output_is_augmented_with_generic_requested_outputs() -> None:
    policy = await _policy_for_message(
        "Build a registry lookup. Return a final result record with customer name, record id, and status.",
        [{"outcome": "The profile details are captured."}],
    )

    rendered = "\n".join(_outcomes(policy))
    assert "customer name" in rendered
    assert "record id" in rendered
    assert "status" in rendered
    assert _criteria_for_path(policy, "output.customer_name")
    assert _criteria_for_path(policy, "output.record_id")
    assert _criteria_for_path(policy, "output.status")


@pytest.mark.asyncio
async def test_lowercase_record_id_is_augmented_as_requested_output() -> None:
    policy = await _policy_for_message(
        "Return a final record with record id.",
        [],
    )

    assert _outcomes(policy) == ["The returned record includes record id."]
    assert policy.completion_criteria[0].output_path == "output.record_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_token",
    [
        pytest.param("`field_name`", id="backtick"),
        pytest.param('"field_name"', id="double_quote"),
        pytest.param("'field_name'", id="single_quote"),
    ],
)
async def test_delimited_named_output_field_is_augmented_as_requested_output(field_token: str) -> None:
    policy = await _policy_for_message(
        f"Return a final record with output field named {field_token}.",
        [],
    )

    criteria = _criteria_for_path(policy, "output.field_name")
    trace = policy.to_trace_data()

    assert len(criteria) == 1
    assert criteria[0].output_path == "output.field_name"
    assert trace["requested_output_criteria_count"] == 1


@pytest.mark.asyncio
async def test_delimited_named_output_field_dedupes_with_bare_field() -> None:
    policy = await _policy_for_message(
        "Return a final record with output field named field_name and output field named `field_name`.",
        [],
    )

    assert len(_criteria_for_path(policy, "output.field_name")) == 1
    assert policy.to_trace_data()["requested_output_criteria_count"] == 1


@pytest.mark.asyncio
async def test_leading_output_verb_does_not_enter_requested_output_slug() -> None:
    policy = await _policy_for_message(
        "Return a final record. Capture the identifier.",
        [],
    )

    assert _outcomes(policy) == ["The returned record includes identifier."]
    assert policy.completion_criteria[0].output_path == "output.identifier"


@pytest.mark.asyncio
async def test_possessive_requested_output_slug_is_canonicalized() -> None:
    policy = await _policy_for_message(
        "Return a final record with each location's status.",
        [],
    )

    assert _outcomes(policy) == ["The returned record includes location status."]
    assert policy.completion_criteria[0].output_path == "output.location_status"


@pytest.mark.asyncio
async def test_combined_classifier_output_splits_requested_output_grounding() -> None:
    policy = await _policy_for_message(
        "Return a final record with record id and status.",
        [{"outcome": "The returned record includes record id and status."}],
    )

    assert _criteria_for_path(policy, "output.record_id")
    assert _criteria_for_path(policy, "output.status")


@pytest.mark.asyncio
async def test_schema_derived_output_path_precedes_config_alias_and_slug_fallback() -> None:
    policy = await _policy_for_message(
        "Return a final record with tracking number.",
        [
            {
                "outcome": "The returned record includes tracking number.",
                "output_path": "output.shipment.tracking_number",
            }
        ],
        config=CopilotConfig(requested_output_path_aliases={"tracking number": "output.tracking_number"}),
    )

    assert _criteria_for_path(policy, "output.shipment.tracking_number")
    assert not _criteria_for_path(policy, "output.tracking_number")


@pytest.mark.asyncio
async def test_config_alias_maps_generic_requested_output_field() -> None:
    policy = await _policy_for_message(
        "Return a final record with reference number.",
        [],
        config=CopilotConfig(requested_output_path_aliases={"reference number": "output.reference_number"}),
    )

    assert _outcomes(policy) == ["The returned record includes reference number."]
    assert policy.completion_criteria[0].output_path == "output.reference_number"


@pytest.mark.asyncio
async def test_classifier_drift_canonicalizes_to_stable_generic_requested_outputs() -> None:
    prompt = "Return a final record with record id, status, and contact email."
    classifier_shapes = [
        [
            {"outcome": "The complete profile details are visible."},
            {"outcome": "The final evidence text includes the record id.", "output_path": "output.evidence_text"},
            {"outcome": "The final record includes a contact email."},
            {"outcome": "The workflow accepts lookup inputs.", "level": "definition"},
            {
                "outcome": "The workflow runs to its intended end state with the expected output.",
                "method_mandated": True,
            },
        ],
        [
            {"outcome": "The extracted profile contains record id, status, and contact email."},
            {"outcome": "The reusable inputs are defined.", "level": "definition"},
        ],
        [
            {
                "outcome": "Record id is copied from the search result evidence text.",
                "output_path": "output.evidence_text",
            },
            {"outcome": "The returned record includes record id.", "output_path": "output.record_id"},
            {"outcome": "The returned record includes status."},
            {"outcome": "A different unbound narrative run criterion is present."},
        ],
    ]

    policies = [await _policy_for_message(prompt, criteria) for criteria in classifier_shapes]
    requested_output_paths = {
        "output.record_id",
        "output.status",
        "output.contact_email",
    }
    canonical_subsets = [_requested_output_subset(policy, requested_output_paths) for policy in policies]
    canonical_json = [criteria_to_json(subset) for subset in canonical_subsets]
    fingerprints = [_criteria_fingerprint(subset) for subset in canonical_subsets]

    assert canonical_json == [canonical_json[0], canonical_json[0], canonical_json[0]]
    assert fingerprints == [fingerprints[0], fingerprints[0], fingerprints[0]]
    assert {criterion.output_path for criterion in canonical_subsets[0]} == requested_output_paths
    assert len(canonical_subsets[0]) == 3
    assert all(_criteria_for_path(policy, "output.evidence_text") == [] for policy in policies)
    assert any(criterion.level == "definition" for criterion in policies[0].completion_criteria)
    assert any(
        criterion.outcome == "A different unbound narrative run criterion is present."
        for criterion in policies[2].completion_criteria
    )


@pytest.mark.asyncio
async def test_requested_output_criteria_preserve_stable_paths_without_derived_carriers() -> None:
    prompt = "Return a final record with service address 1234 Sample Utility Way and requested start date 2026-06-22."
    classifier_shapes = [
        [
            {
                "outcome": "The returned record includes requested start date 2026-06-22.",
                "output_path": "output.requested_start_date",
            },
            {
                "outcome": "The returned record includes service address 1234 Sample Utility Way.",
                "output_path": "output.service_address",
            },
        ],
        [
            {"outcome": "The returned record includes service address.", "output_path": "output.service_address"},
            {
                "outcome": "The returned record includes requested start date.",
                "output_path": "output.requested_start_date",
            },
            {"outcome": "The workflow runs to an expected result.", "method_mandated": True},
        ],
    ]

    policies = [await _policy_for_message(prompt, criteria) for criteria in classifier_shapes]
    requested_output_paths = {"output.service_address", "output.requested_start_date"}
    canonical_subsets = [_requested_output_subset(policy, requested_output_paths) for policy in policies]
    canonical_json = [criteria_to_json(subset) for subset in canonical_subsets]

    assert canonical_json == [canonical_json[0], canonical_json[0]]
    assert [criterion.id for criterion in canonical_subsets[0]] == [
        "__copilot_requested_output__output_service_address",
        "__copilot_requested_output__output_requested_start_date",
    ]
    assert [criterion.output_path for criterion in canonical_subsets[0]] == [
        "output.service_address",
        "output.requested_start_date",
    ]
    assert [criterion.outcome for criterion in canonical_subsets[0]] == [
        "The returned record includes service address.",
        "The returned record includes requested start date.",
    ]
    assert [criterion.expected_output_value for criterion in canonical_subsets[0]] == [
        None,
        None,
    ]
    assert [criterion.expected_output_shape for criterion in canonical_subsets[0]] == [None, None]


@pytest.mark.asyncio
async def test_resale_docs_requested_output_uses_explicit_document_name_and_blocks_terminal_record_fields() -> None:
    policy = await _policy_for_message(
        (
            "I need a reusable workflow that retrieves the resale demand document name from the mock ResaleDocs Hub "
            "order-status page. The confirmation number should be a reusable input; for this eval run use "
            "DEMO-RESALE-1842. The workflow should look up the order, open the order-level View / Download document "
            "list, choose the highest-priority HOA demand or resale statement document row, and output one "
            "requested-output field named document_name containing the selected row's visible document name. Do not "
            "include an exact expected value for document_name in the workflow output contract, and do not output "
            "confirmation_number, order_id, request_id, submission_id, or a terminal-record identifier."
        ),
        [
            {
                "outcome": "The returned record includes visible document name.",
                "output_path": "output.visible_document_name",
            },
            {
                "outcome": "The returned record includes terminal record identifier.",
                "output_path": "output.terminal_record_identifier",
            },
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
            },
            {
                "outcome": "The returned record includes order id.",
                "output_path": "output.order_id",
            },
        ],
    )

    requested_outputs = [criterion for criterion in policy.completion_criteria if criterion.output_path is not None]
    assert [criterion.output_path for criterion in requested_outputs] == ["output.document_name"]
    assert requested_outputs[0].expected_output_value is None
    assert requested_outputs[0].expected_output_shape is None
    assert _run_outcome_corroborators(policy)[0].requested_output_corroborator is True


@pytest.mark.asyncio
async def test_named_customer_prose_does_not_become_requested_output_field_name() -> None:
    policy = await _policy_for_message(
        "Return a final record with customer named Acme and status.",
        [],
    )

    assert not _criteria_for_path(policy, "output.acme")
    assert _criteria_for_path(policy, "output.status")


@pytest.mark.asyncio
async def test_requested_output_canonicalization_preserves_output_corroborator() -> None:
    policy = await _policy_for_message(
        "Extract the first 3 quotes and authors, then return JSON with quotes.",
        [
            {
                "id": "c_quotes",
                "outcome": "The run extracts the first 3 quotes and authors into the returned quotes JSON.",
                "output_path": "output.quotes",
            }
        ],
    )

    requested = _requested_output_subset(policy, {"output.quotes"})
    corroborators = _run_outcome_corroborators(policy)

    assert len(requested) == 1
    assert requested[0].id == "__copilot_requested_output__output_quotes"
    assert len(corroborators) == 1
    assert corroborators[0].requested_output_corroborator is True
    assert corroborators[0].outcome == "The run extracts the first 3 quotes and authors into the returned quotes JSON."


@pytest.mark.asyncio
async def test_classifier_typed_requested_output_adds_distinct_corroborator() -> None:
    policy = await _policy_for_message(
        "Run the read-only extraction.",
        [
            {
                "outcome": "The returned JSON contains the first three public quotes and authors.",
                "output_path": "output.quotes",
            }
        ],
    )

    requested = _requested_output_subset(policy, {"output.quotes"})
    corroborators = _run_outcome_corroborators(policy)

    assert len(requested) == 1
    assert requested[0].id == "c0"
    assert requested[0].output_path == "output.quotes"
    assert requested[0].requested_output_corroborator is False
    assert len(corroborators) == 1
    assert corroborators[0].id != requested[0].id
    assert corroborators[0].id == "c0__requested_output_corroborator"
    assert corroborators[0].output_path is None
    assert corroborators[0].expected_output_value is None
    assert corroborators[0].expected_output_shape is None
    assert corroborators[0].requested_output_corroborator is True
    assert corroborators[0].outcome == requested[0].outcome


@pytest.mark.asyncio
async def test_plain_requested_output_text_does_not_fabricate_corroborator() -> None:
    policy = await _policy_for_message("Return a final record with record id.", [])

    requested = _requested_output_subset(policy, {"output.record_id"})

    assert len(requested) == 1
    assert requested[0].id == "__copilot_requested_output__output_record_id"
    assert _run_outcome_corroborators(policy) == []


def test_requested_output_canonicalization_preserves_fallback_floor_base_when_it_is_source() -> None:
    policy = RequestPolicy(completion_criteria=build_classifier_fallback_floor([]))

    _apply_requested_output_completion_criteria(policy, "Return a final record with record id.")

    assert len(_requested_output_subset(policy, {"output.record_id"})) == 1
    fallback = [
        criterion for criterion in _run_outcome_corroborators(policy) if is_fallback_floor_base_criterion(criterion)
    ]
    assert len(fallback) == 1
    assert fallback[0].requested_output_corroborator is True


def test_requested_output_canonicalization_does_not_promote_method_mandated_only_corroborator() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="method_floor",
                outcome="The workflow runs to its intended end state with the expected output.",
                method_mandated=True,
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with record id.")

    assert len(_requested_output_subset(policy, {"output.record_id"})) == 1
    assert _run_outcome_corroborators(policy) == []


def test_requested_output_corroborator_respects_completion_criteria_cap() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id=f"specific_{index}", outcome=f"Specific retained outcome {index}.")
            for index in range(7)
        ]
        + [
            CompletionCriterion(
                id="quotes",
                outcome="The run extracts the requested record id into the returned JSON.",
                output_path="output.record_id",
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with record id and status.")

    assert len(policy.completion_criteria) == 8
    assert {
        criterion.output_path for criterion in _requested_output_subset(policy, {"output.record_id", "output.status"})
    } == {
        "output.record_id",
        "output.status",
    }


def test_requested_output_canonicalization_drops_redundant_corroborator_when_outputs_exceed_cap() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="name_source",
                outcome="The run extracts the requested name into the returned JSON.",
                output_path="output.name",
            )
        ]
    )

    _apply_requested_output_completion_criteria(
        policy,
        "Return a final record with name, record id, status, phone, email, license, taxonomy, specialty, and date.",
    )

    requested_output_paths = {
        "output.name",
        "output.record_id",
        "output.status",
        "output.phone",
        "output.email",
        "output.license",
        "output.taxonomy",
        "output.specialty",
        "output.date",
    }
    corroborators = _run_outcome_corroborators(policy)

    assert {
        criterion.output_path for criterion in _requested_output_subset(policy, requested_output_paths)
    } == requested_output_paths
    assert corroborators == []


@pytest.mark.asyncio
async def test_requested_output_criteria_do_not_infer_shapes_from_generated_provider_field_names() -> None:
    policy = await _policy_for_message(
        "Return a final record with provider captured address, account number, requested date, status, "
        "confirmation number, deposit amount, and next owner.",
        [],
    )

    requested_output_paths = {
        "output.provider_captured_address",
        "output.account_number",
        "output.requested_date",
        "output.status",
        "output.confirmation_number",
        "output.deposit_amount",
        "output.next_owner",
    }

    assert {
        criterion.output_path: criterion.expected_output_shape
        for criterion in _requested_output_subset(policy, requested_output_paths)
    } == {
        "output.provider_captured_address": None,
        "output.account_number": None,
        "output.requested_date": None,
        "output.status": None,
        "output.confirmation_number": None,
        "output.deposit_amount": None,
        "output.next_owner": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "output_path", "_expected_value"),
    [
        (
            "Return a final record with service URL https://example.com/a:b.",
            "output.service_url",
            "https://example.com/a:b",
        ),
        ("Return a final record with tax rate 1.25.", "output.tax_rate", "1.25"),
        ("Return a final record with company domain example.com.", "output.company_domain", "example.com"),
        (
            "Return a final record with department name Research and Development.",
            "output.department_name",
            "Research and Development",
        ),
    ],
)
async def test_requested_output_expected_values_are_not_synthesized_from_user_message_regex(
    prompt: str, output_path: str, _expected_value: str
) -> None:
    policy = await _policy_for_message(prompt, [])

    criteria = _criteria_for_path(policy, output_path)
    assert len(criteria) == 1
    assert (
        criteria[0].outcome == f"The returned record includes {output_path.removeprefix('output.').replace('_', ' ')}."
    )
    assert criteria[0].expected_output_value is None


@pytest.mark.asyncio
async def test_message_text_cannot_override_classifier_typed_expected_output_value() -> None:
    policy = await _policy_for_message(
        "Return a final record with service address 999 Wrong Way.",
        [
            {
                "outcome": "The returned record includes service address.",
                "output_path": "output.service_address",
                "expected_output_value": "1234 Sample Utility Way",
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.service_address")
    assert len(criteria) == 1
    assert criteria[0].expected_output_value == "1234 Sample Utility Way"


@pytest.mark.asyncio
async def test_classifier_typed_expected_output_value_is_preserved_without_outcome_carrier() -> None:
    policy = await _policy_for_message(
        "Return a final record with service address.",
        [
            {
                "outcome": "The returned record includes service address.",
                "output_path": "output.service_address",
                "expected_output_value": "1234 Sample Utility Way",
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.service_address")
    assert len(criteria) == 1
    assert criteria[0].outcome == "The returned record includes service address."
    assert criteria[0].expected_output_value == "1234 Sample Utility Way"


@pytest.mark.asyncio
async def test_classifier_typed_expected_output_shape_is_preserved_without_outcome_carrier() -> None:
    policy = await _policy_for_message(
        "Return a final record with confirmation number.",
        [
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "reference_code",
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.confirmation_number")
    assert len(criteria) == 1
    assert criteria[0].expected_output_shape == "reference_code"


@pytest.mark.asyncio
async def test_classifier_invalid_expected_output_shape_is_ignored() -> None:
    policy = await _policy_for_message(
        "Return a final record with custom value.",
        [
            {
                "outcome": "The returned record includes custom value.",
                "output_path": "output.custom_value",
                "expected_output_shape": "anything_non_empty",
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.custom_value")
    assert len(criteria) == 1
    assert criteria[0].expected_output_shape is None


def test_parse_completion_criteria_accepts_only_declared_expected_output_shapes() -> None:
    valid, invalid = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "reference_code",
            },
            {
                "outcome": "The returned record includes custom value.",
                "output_path": "output.custom_value",
                "expected_output_shape": "anything_non_empty",
            },
        ]
    )

    assert valid.expected_output_shape == "reference_code"
    assert invalid.expected_output_shape is None


def test_parse_completion_criteria_preserves_validation_classification_contract() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "validation_classification",
                "classification_output_key": "login_gated",
                "expected_classification": True,
            },
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "outcome",
                "classification_output_key": "login_gated",
                "expected_classification": True,
            },
        ]
    )

    assert len(parsed) == 2
    assert parsed[0].kind == "validation_classification"
    assert parsed[0].classification_output_key == "login_gated"
    assert parsed[0].expected_classification is None
    assert parsed[0].mint_disposition == "pending"
    assert parsed[1].kind == "outcome"


def test_parse_completion_criteria_drops_requested_output_fields_for_validation_classification() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "validation_classification",
                "output_path": "output.path_classification",
                "expected_output_value": "login_gated",
                "expected_output_shape": "status_label",
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].kind == "validation_classification"
    assert parsed[0].output_path is None
    assert parsed[0].expected_output_value is None
    assert parsed[0].expected_output_shape is None


def test_parse_completion_criteria_dedupes_validation_classification_by_target_and_output_key() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "validation_classification",
                "classification_output_key": "path_classification",
                "expected_classification": "login_gated",
            },
            {
                "outcome": "Duplicate phrasing should not matter for the same classification target.",
                "kind": "validation_classification",
                "classification_output_key": "path_classification",
                "expected_classification": "login_gated",
            },
            {
                "outcome": "A different expected classification stays distinct.",
                "kind": "validation_classification",
                "classification_output_key": "path_classification",
                "expected_classification": "public",
            },
        ]
    )

    assert [(criterion.classification_output_key, criterion.expected_classification) for criterion in parsed] == [
        ("path_classification", "login_gated"),
        ("path_classification", "public"),
    ]


@pytest.mark.asyncio
async def test_request_policy_promotes_login_only_output_to_validation_classification_contract() -> None:
    policy = await _policy_for_message(
        (
            "I need a reusable validation-only workflow that checks whether Riverbend Gas has a public path for "
            "starting gas service from http://localhost:8900/utility_services/riverbend_gas/. It should look for a "
            "start, connect, new service, or move-in path without logging in, creating an account, entering identity "
            "details, entering payment details, accepting terms, or submitting anything. For this eval run, classify "
            "the safest reachable next step and return a structured summary showing whether a public form exists, "
            "whether the path is login-only, the visible page/path label, and the recommended next action."
        ),
        [
            {
                "outcome": "The returned record includes public path exists.",
                "output_path": "output.public_path_exists",
            },
            {
                "outcome": "The returned record includes safest reachable next step.",
                "output_path": "output.safest_reachable_next_step",
            },
            {
                "outcome": "The returned record includes login only.",
                "output_path": "output.login_only",
            },
        ],
    )

    validation_criteria = [
        criterion for criterion in policy.completion_criteria if criterion.kind == "validation_classification"
    ]
    assert len(validation_criteria) == 1
    assert validation_criteria[0].classification_output_key == "login_only"
    assert validation_criteria[0].expected_classification is True
    assert validation_criteria[0].output_path is None
    assert validation_criteria[0].expected_output_value is None
    assert not _criteria_for_path(policy, "output.login_only")


def test_prompt_summary_includes_validation_classification_output_contract() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_login_only",
                "The run classifies whether the path is login-only.",
                kind="validation_classification",
                classification_output_key="login_only",
                expected_classification=True,
            )
        ]
    )

    summary = policy.prompt_summary()

    assert "validation_classification_output_contracts:" in summary
    assert "criterion_id: c_login_only" in summary
    assert "return_key: login_only" in summary
    assert "expected_value: true" in summary
    assert "return_location: top_level_block_output" in summary


@pytest.mark.asyncio
async def test_request_policy_promotes_path_classification_when_classifier_supplies_target() -> None:
    policy = await _policy_for_message(
        "Classify whether the service path is login-gated and return path classification.",
        [
            {
                "outcome": "The returned record includes path classification.",
                "output_path": "output.path_classification",
                "expected_output_value": "login-gated",
            }
        ],
    )

    assert len(policy.completion_criteria) == 1
    criterion = policy.completion_criteria[0]
    assert criterion.kind == "validation_classification"
    assert criterion.classification_output_key == "path_classification"
    assert criterion.expected_classification == "login-gated"
    assert criterion.output_path is None
    assert criterion.expected_output_value is None


@pytest.mark.asyncio
async def test_requested_output_canonicalization_preserves_classifier_shape_first() -> None:
    policy = await _policy_for_message(
        "Return a final record with confirmation number.",
        [
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "numeric_identifier",
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.confirmation_number")
    assert len(criteria) == 1
    assert criteria[0].expected_output_shape == "numeric_identifier"


@pytest.mark.asyncio
async def test_classifier_non_string_expected_output_value_is_ignored() -> None:
    policy = await _policy_for_message(
        "Return a final record with service address.",
        [
            {
                "outcome": "The returned record includes service address.",
                "output_path": "output.service_address",
                "expected_output_value": {"value": "1234 Sample Utility Way"},
            }
        ],
    )

    criteria = _criteria_for_path(policy, "output.service_address")
    assert len(criteria) == 1
    assert criteria[0].expected_output_value is None


@pytest.mark.asyncio
async def test_method_and_setup_text_do_not_become_completion_criteria() -> None:
    policy = await _policy_for_message(
        "Build a lookup. Open Show Details, click Search, choose a plan, set the location, then output profile details.",
        [],
    )

    rendered = "\n".join(_outcomes(policy)).lower()
    assert "show details" not in rendered
    assert "click search" not in rendered
    assert "choose a plan" not in rendered
    assert "location" not in rendered
    assert policy.completion_criteria == []


@pytest.mark.asyncio
async def test_reusable_input_id_does_not_cover_requested_output_id() -> None:
    policy = await _policy_for_message(
        "Accept record id as a reusable input, search by that value, and return a final record with record id.",
        [{"outcome": "The workflow accepts record id as a reusable input.", "level": "definition"}],
    )

    id_criteria = [criterion for criterion in policy.completion_criteria if "record id" in criterion.outcome]
    assert [criterion.level for criterion in id_criteria] == ["definition", "run"]
    assert [criterion.output_path for criterion in id_criteria] == [None, "output.record_id"]


@pytest.mark.asyncio
async def test_unbound_requested_output_narrative_is_replaced_but_specific_run_gate_remains() -> None:
    policy = await _policy_for_message(
        "Return a final record with record id, status, and phone.",
        [
            {"outcome": "The returned record narrative includes record id, status, and phone."},
            {"outcome": "The portal session reaches the submitted results screen."},
        ],
    )

    assert "The returned record narrative includes record id, status, and phone." not in _outcomes(policy)
    assert "The portal session reaches the submitted results screen." in _outcomes(policy)
    assert {
        criterion.output_path
        for criterion in _requested_output_subset(
            policy,
            {"output.record_id", "output.status", "output.phone"},
        )
    } == {"output.record_id", "output.status", "output.phone"}


@pytest.mark.asyncio
async def test_generic_ungrounded_run_gate_drops_after_requested_output_canonicalization() -> None:
    policy = await _policy_for_message(
        "Return a final record with record id.",
        [{"outcome": "The workflow runs to its intended end state with the expected output."}],
    )

    assert "The workflow runs to its intended end state with the expected output." not in _outcomes(policy)
    assert _criteria_for_path(policy, "output.record_id")


@pytest.mark.asyncio
async def test_terminal_action_run_gate_survives_requested_output_canonicalization() -> None:
    policy = await _policy_for_message(
        "Submit the request and return a final record with confirmation number.",
        [
            {
                "outcome": "The request submission action is completed.",
                "kind": "terminal_action",
                "terminal_action_family": "request",
            },
        ],
    )

    assert "The request submission action is completed." in _outcomes(policy)
    assert _criteria_for_path(policy, "output.confirmation_number")


@pytest.mark.asyncio
async def test_requested_output_coverage_uses_whole_token_sequence_matching() -> None:
    policy = await _policy_for_message(
        "Return a final record with id.",
        [{"outcome": "The returned record includes customer name."}],
    )

    rendered = "\n".join(_outcomes(policy)).lower()
    assert "customer name" in rendered
    assert "includes id" in rendered
    assert _criteria_for_path(policy, "output.id")


@pytest.mark.asyncio
async def test_fresh_augmented_requested_output_supersedes_incomplete_stored_generic_criteria() -> None:
    stored = _stored(_criterion("s0", "The profile details are captured."))
    policy = await _policy_for_message(
        "Return a final result record with customer name and record id.",
        [{"outcome": "The profile details are captured."}],
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        policy.completion_criteria,
        actionable=True,
    )

    assert decision.action == "create"
    assert decision.superseded_set_id == stored.set_id
    assert any(criterion.output_path == "output.record_id" for criterion in decision.criteria)


def test_stored_complete_requested_output_survives_narrowed_generic_fresh_criteria() -> None:
    stored = _stored(
        _criterion("s0", "The profile details are captured."),
        CompletionCriterion(id="s1", outcome="The returned record includes record id.", output_path="output.record_id"),
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c0", "The profile details are captured.")],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.criteria == stored.criteria


def test_requested_output_canonicalization_preserves_contingent_metadata() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "conditional_npi",
                "The returned record includes NPI.",
                output_path="output.npi",
                contingent_on="the provider site allows online lookup",
                contingent_antecedent_output_path="output.provider_lookup_available",
            )
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with NPI.")

    criteria = _requested_output_subset(policy, {"output.npi"})
    assert len(criteria) == 1
    assert criteria[0].outcome == "The returned record includes NPI."
    assert criteria[0].contingent_on == "the provider site allows online lookup"
    assert criteria[0].contingent_antecedent_output_path == "output.provider_lookup_available"


def test_classifier_parse_preserves_contingent_on_without_inference() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
            },
            {"outcome": "The request is submitted unless the provider site blocks online submission."},
            {"outcome": "Ignored empty contingent value.", "contingent_on": "   "},
        ]
    )

    assert criteria[0].outcome == "A provider blocker is reported to the user."
    assert criteria[0].contingent_on == "the provider site blocks online submission"
    assert criteria[1].contingent_on is None
    assert criteria[2].contingent_on is None


def test_classifier_parse_preserves_contingent_antecedent_output_path_without_inference() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
                "contingent_antecedent_output_path": "output.blocker",
            },
            {
                "outcome": "Rejected transcript path.",
                "contingent_on": "the transcript mentions a blocker",
                "contingent_antecedent_output_path": "transcript.blocker",
            },
            {
                "outcome": "Rejected nested path.",
                "contingent_antecedent_output_path": "output.blocker.reason",
            },
            {"outcome": "No regex inference when prose mentions output.blocker."},
        ]
    )

    assert criteria[0].contingent_antecedent_output_path == "output.blocker"
    assert criteria[1].contingent_antecedent_output_path is None
    assert criteria[2].contingent_antecedent_output_path is None
    assert criteria[3].contingent_antecedent_output_path is None


def test_classifier_parse_preserves_only_registered_download_deliverable_kind() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The requested download is returned.",
                "deliverable_kind": "registered_download",
            },
            {
                "outcome": "Unknown deliverable is ignored.",
                "deliverable_kind": "download",
            },
            {
                "outcome": "Absent deliverable stays empty.",
            },
        ]
    )

    assert [criterion.deliverable_kind for criterion in criteria] == ["registered_download", None, None]


def test_requested_output_canonicalization_drops_marker_for_output_id() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "The returned record includes output id.",
                output_path="output.output_id",
                deliverable_kind="registered_download",
            )
        ]
    )

    _apply_requested_output_completion_criteria(
        policy,
        "Return a final record with output id.",
        aliases={"output id": "output.output_id"},
    )

    criteria = _requested_output_subset(policy, {"output.output_id"})
    assert len(criteria) == 1
    assert criteria[0].id == "__copilot_requested_output__output_output_id"
    assert criteria[0].deliverable_kind is None


def test_requested_output_canonicalization_drops_marker_for_npi() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes NPI.",
                "output_path": "output.npi",
                "deliverable_kind": "registered_download",
            }
        ]
    )
    assert parsed[0].deliverable_kind == "registered_download"
    policy = RequestPolicy(completion_criteria=parsed)

    _apply_requested_output_completion_criteria(
        policy,
        "Return a final record with NPI.",
        aliases={"NPI": "output.npi"},
    )

    criteria = _requested_output_subset(policy, {"output.npi"})
    assert len(criteria) == 1
    assert criteria[0].id == "__copilot_requested_output__output_npi"
    assert criteria[0].deliverable_kind is None


def test_requested_output_canonicalization_preserves_marker_for_approved_download_path() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "The returned record includes download artifact.",
                output_path="output.output_id",
                deliverable_kind="registered_download",
            )
        ]
    )

    _apply_requested_output_completion_criteria(
        policy,
        "Return a final record with download artifact.",
        aliases={"download artifact": "output.downloaded_file_artifact_ids"},
    )

    criteria = _requested_output_subset(policy, {"output.downloaded_file_artifact_ids"})
    assert len(criteria) == 1
    assert criteria[0].id == "__copilot_requested_output__output_downloaded_file_artifact_ids"
    assert criteria[0].deliverable_kind == "registered_download"


@pytest.mark.asyncio
async def test_classifier_typed_download_preserves_registered_download_requested_output() -> None:
    policy = await _policy_for_message(
        "Build a workflow that downloads the May 2026 bill.",
        [
            {
                "outcome": "The requested download artifact ids include the May 2026 bill.",
                "output_path": "output.downloaded_file_artifact_ids",
                "expected_output_value": "May 2026",
                "expected_output_shape": "date",
                "requested_output_evidence_source": "registered_artifact_content",
                "deliverable_kind": "registered_download",
            }
        ],
    )

    criteria = _requested_output_subset(policy, {"output.downloaded_file_artifact_ids"})
    trace = policy.to_trace_data()

    assert len(criteria) == 1
    assert criteria[0].output_path == "output.downloaded_file_artifact_ids"
    assert criteria[0].deliverable_kind == "registered_download"
    assert criteria[0].expected_output_value == "May 2026"
    assert criteria[0].expected_output_shape == "date"
    assert criteria[0].requested_output_evidence_source == "registered_artifact_content"
    assert trace["requested_output_criteria_count"] == 1


@pytest.mark.asyncio
async def test_download_prose_without_typed_classifier_output_does_not_mint_requested_output() -> None:
    policy = await _policy_for_message(
        "Build a workflow that downloads the bill for May 2026.",
        [],
    )

    criteria = _requested_output_subset(policy, {"output.downloaded_file_artifact_ids"})

    assert criteria == []
    assert policy.to_trace_data()["requested_output_criteria_count"] == 0


@pytest.mark.asyncio
async def test_explicit_download_alias_can_mint_registered_download_requested_output() -> None:
    policy = await _policy_for_message(
        "Build a workflow that returns the May 2026 download artifact.",
        [
            {
                "outcome": "The returned record includes the download artifact.",
                "output_path": "output.downloaded_file_artifact_ids",
                "expected_output_value": "May 2026",
                "expected_output_shape": "date",
                "requested_output_evidence_source": "registered_artifact_content",
                "deliverable_kind": "registered_download",
            }
        ],
        config=CopilotConfig(
            requested_output_path_aliases={"download artifact": "output.downloaded_file_artifact_ids"}
        ),
    )

    criteria = _requested_output_subset(policy, {"output.downloaded_file_artifact_ids"})

    assert len(criteria) == 1
    assert criteria[0].expected_output_value == "May 2026"
    assert criteria[0].expected_output_shape == "date"
    assert criteria[0].requested_output_evidence_source == "registered_artifact_content"
    assert criteria[0].deliverable_kind == "registered_download"


@pytest.mark.asyncio
async def test_date_qualified_download_preserves_classifier_typed_value_and_shape() -> None:
    policy = await _policy_for_message(
        "Build a workflow that downloads the May 2026 bill.",
        [
            {
                "outcome": "The requested download is returned.",
                "output_path": "output.downloaded_file_artifact_ids",
                "expected_output_value": "April 2026",
                "expected_output_shape": "date",
                "deliverable_kind": "registered_download",
            }
        ],
    )

    criteria = _requested_output_subset(policy, {"output.downloaded_file_artifact_ids"})

    assert len(criteria) == 1
    assert criteria[0].expected_output_value == "April 2026"
    assert criteria[0].expected_output_shape == "date"


@pytest.mark.parametrize(
    ("raw_criteria", "expected_count"),
    [
        pytest.param(
            [
                {
                    "outcome": "The returned record includes the requested bill.",
                    "output_path": "output.downloaded_file_artifact_ids",
                    "expected_output_value": "May 2026",
                    "expected_output_shape": "date",
                    "deliverable_kind": "registered_download",
                }
            ],
            1,
            id="typed_value_single_download_stays_pathed",
        ),
        pytest.param(
            [
                {"outcome": "The first requested download is returned.", "deliverable_kind": "registered_download"},
                {"outcome": "The second requested download is returned.", "deliverable_kind": "registered_download"},
            ],
            0,
            id="multi_file_download_stays_pathless",
        ),
        pytest.param(
            [{"outcome": "The task completes successfully."}],
            0,
            id="incidental_no_ask_earns_no_requested_output",
        ),
        pytest.param(
            [{"outcome": "The requested download is returned.", "deliverable_kind": "registered_download"}],
            1,
            id="lone_presence_only_download_mints_requested_output",
        ),
    ],
)
def test_registered_download_shape_matrix_stays_fail_closed(
    raw_criteria: list[dict[str, Any]], expected_count: int
) -> None:
    policy = RequestPolicy(completion_criteria=_parse_completion_criteria(raw_criteria))

    trace = policy.to_trace_data()

    assert trace["requested_output_criteria_count"] == expected_count
    if expected_count == 0:
        assert all(criterion.output_path is None for criterion in policy.completion_criteria)


def test_lone_presence_only_download_defaults_downloaded_files_path() -> None:
    criteria = _parse_completion_criteria(
        [{"outcome": "The requested download is returned.", "deliverable_kind": "registered_download"}]
    )

    assert len(criteria) == 1
    assert criteria[0].output_path == "output.downloaded_files"
    assert criteria[0].expected_output_value is None


@pytest.mark.parametrize(
    ("criterion_kwargs", "expected"),
    [
        pytest.param(
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
            },
            {
                "outcome": "A provider blocker is reported to the user.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
                "contingent_on": "the provider site blocks online submission",
            },
            id="contingent_on",
        ),
        pytest.param(
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
                "contingent_antecedent_output_path": "output.blocker",
            },
            {
                "outcome": "A provider blocker is reported to the user.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
                "contingent_on": "the provider site blocks online submission",
                "contingent_antecedent_output_path": "output.blocker",
            },
            id="contingent_antecedent_output_path",
        ),
        pytest.param(
            {
                "outcome": "The requested download is returned.",
                "output_path": "output.output_id",
                "deliverable_kind": "registered_download",
            },
            {
                "outcome": "The requested download is returned.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
                "deliverable_kind": "registered_download",
                "output_path": "output.output_id",
            },
            id="deliverable_kind",
        ),
        pytest.param(
            {
                "outcome": "The returned record includes service address.",
                "output_path": "output.service_address",
                "expected_output_value": "1234 Sample Utility Way",
            },
            {
                "outcome": "The returned record includes service address.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
                "output_path": "output.service_address",
                "expected_output_value": "1234 Sample Utility Way",
            },
            id="expected_output_value",
        ),
        pytest.param(
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "reference_code",
            },
            {
                "outcome": "The returned record includes confirmation number.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "outcome",
                "terminal_action_family": None,
                "output_path": "output.confirmation_number",
                "expected_output_shape": "reference_code",
            },
            id="expected_output_shape",
        ),
        pytest.param(
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "validation_classification",
                "classification_output_key": "login_gated",
                "expected_classification": True,
            },
            {
                "outcome": "The run classifies whether the path is login gated.",
                "implicit": False,
                "method_mandated": False,
                "level": "run",
                "kind": "validation_classification",
                "terminal_action_family": None,
                "classification_output_key": "login_gated",
                "expected_classification": True,
            },
            id="validation_classification_contract",
        ),
    ],
)
def test_active_criteria_rendering_includes_optional_fields(
    criterion_kwargs: dict[str, Any], expected: dict[str, Any]
) -> None:
    rendered = _render_active_criteria_for_prompt([_criterion("c0", **criterion_kwargs)])

    assert json.loads(rendered) == [expected]


@pytest.mark.parametrize(
    "criterion_kwargs",
    [
        pytest.param(
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
                "mint_degrade": "contingent_missing_antecedent",
            },
            id="contingent_on",
        ),
        pytest.param(
            {
                "outcome": "The returned record includes service address.",
                "output_path": "output.service_address",
                "expected_output_value": "1234 Sample Utility Way",
            },
            id="expected_output_value",
        ),
        pytest.param(
            {
                "outcome": "The returned record includes confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "reference_code",
            },
            id="expected_output_shape",
        ),
        pytest.param(
            {
                "outcome": "The run classifies whether the path is login gated.",
                "kind": "validation_classification",
                "classification_output_key": "path_classification",
                "expected_classification": "login_gated",
            },
            id="validation_classification_contract",
        ),
        pytest.param(
            {
                "outcome": "A provider blocker is reported to the user.",
                "contingent_on": "the provider site blocks online submission",
                "contingent_antecedent_output_path": "output.blocker",
            },
            id="contingent_antecedent_output_path",
        ),
    ],
)
def test_criteria_json_round_trips(criterion_kwargs: dict[str, Any]) -> None:
    criteria = (_criterion("c0", **criterion_kwargs),)

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored == criteria


def test_criteria_json_rehydrates_validation_classification_without_requested_output_fields() -> None:
    criteria = (
        _criterion(
            "c0",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            output_path="output.path_classification",
            expected_output_value="login_gated",
            expected_output_shape="status_label",
        ),
    )

    restored = criteria_from_json(criteria_to_json(criteria))

    assert len(restored) == 1
    assert restored[0].kind == "validation_classification"
    assert restored[0].output_path is None
    assert restored[0].expected_output_value is None
    assert restored[0].expected_output_shape is None


def _minted_download_criterion() -> CompletionCriterion:
    return _criterion(
        "c0",
        "The finished workflow produces the downloaded file.",
        output_path="output.downloaded_files",
        deliverable_kind="registered_download",
        requested_output_path_mint_source="classifier_default",
    )


def test_store_round_trip_preserves_requested_output_path_mint_source() -> None:
    criteria = (_minted_download_criterion(),)

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored[0].requested_output_path_mint_source == "classifier_default"
    assert restored == criteria


def test_criteria_from_json_without_mint_source_key_loads_none() -> None:
    restored = criteria_from_json(
        [
            {
                "id": "c0",
                "outcome": "The finished workflow produces the downloaded file.",
                "output_path": "output.downloaded_files",
                "deliverable_kind": "registered_download",
            }
        ]
    )

    assert len(restored) == 1
    assert restored[0].requested_output_path_mint_source is None


def test_requested_output_path_mint_source_excluded_from_reconcile_key() -> None:
    minted = _minted_download_criterion()
    persisted_twin = replace(minted, requested_output_path_mint_source=None)

    assert _criterion_reconcile_key(minted) == _criterion_reconcile_key(persisted_twin)


def test_classifier_declared_registered_download_path_mints_declared_source() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The requested statement download is returned.",
                "output_path": "output.downloaded_file_artifact_ids",
                "deliverable_kind": "registered_download",
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].output_path == "output.downloaded_file_artifact_ids"
    assert parsed[0].requested_output_path_mint_source == "classifier_declared"


def test_classifier_declared_non_registered_download_path_mints_no_source() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes the statement id.",
                "output_path": "output.statement_id",
                "deliverable_kind": "registered_download",
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].output_path == "output.statement_id"
    assert parsed[0].requested_output_path_mint_source is None


def test_store_round_trip_preserves_classifier_declared_mint_source() -> None:
    criteria = (replace(_minted_download_criterion(), requested_output_path_mint_source="classifier_declared"),)

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored[0].requested_output_path_mint_source == "classifier_declared"
    assert restored == criteria


def test_exact_value_download_ask_does_not_mint_classifier_declared_source() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The requested statement download is returned.",
                "output_path": "output.downloaded_files",
                "deliverable_kind": "registered_download",
                "expected_output_value": "statement-2026-05.pdf",
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].expected_output_value == "statement-2026-05.pdf"
    assert parsed[0].requested_output_path_mint_source is None


def test_plain_outcome_keeps_canonical_deliverable_confirmation_association() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The requested document is selected and downloaded.",
                "deliverable_confirmation_criterion_id": REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].deliverable_confirmation_criterion_id == REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID


def test_non_canonical_deliverable_confirmation_association_is_cleared() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The requested document is selected and downloaded.",
                "deliverable_confirmation_criterion_id": "c1",
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].deliverable_confirmation_criterion_id is None


@pytest.mark.parametrize(
    "ineligible_fields",
    [
        {"level": "definition"},
        {"kind": "terminal_action", "terminal_action_family": "request"},
        {"output_path": "output.downloaded_files", "deliverable_kind": "registered_download"},
        {"method_mandated": True},
    ],
)
def test_ineligible_criteria_cannot_carry_deliverable_confirmation_association(
    ineligible_fields: dict[str, Any],
) -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The requested document is selected and downloaded.",
                "deliverable_confirmation_criterion_id": REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
                **ineligible_fields,
            }
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].deliverable_confirmation_criterion_id is None


def test_store_round_trip_preserves_deliverable_confirmation_association() -> None:
    criteria = (
        _criterion(
            "c0",
            "The requested document is selected and downloaded.",
            deliverable_confirmation_criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
        ),
    )

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored == criteria
    assert restored[0].deliverable_confirmation_criterion_id == REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID


def test_store_load_clears_association_on_ineligible_row() -> None:
    row = criteria_to_json(
        (
            _criterion(
                "c0",
                "The requested document is selected and downloaded.",
                deliverable_confirmation_criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            ),
        )
    )
    row[0]["method_mandated"] = True

    restored = criteria_from_json(row)

    assert restored[0].deliverable_confirmation_criterion_id is None


def test_deliverable_confirmation_association_changes_reconcile_identity() -> None:
    associated = _criterion(
        "c0",
        "The requested document is selected and downloaded.",
        deliverable_confirmation_criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
    )
    unassociated = replace(associated, deliverable_confirmation_criterion_id=None)

    assert _criterion_reconcile_key(associated) != _criterion_reconcile_key(unassociated)


def test_request_policy_trace_exposes_requested_output_grounding_contract_without_values() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_address",
                "The returned record includes service address.",
                output_path="output.service_address",
                expected_output_value="1234 Sample Utility Way",
            ),
            _criterion(
                "c_confirmation",
                "The returned record includes confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="reference_code",
            ),
            _criterion("c_status", "The returned record includes status.", output_path="output.status"),
        ]
    )

    trace = policy.to_trace_data()

    assert trace["requested_output_criteria_count"] == 3
    assert trace["requested_output_criterion_0_id"] == "c_address"
    assert trace["requested_output_criterion_0_output_path"] == "output.service_address"
    assert trace["requested_output_criterion_0_grounding_mode"] == "exact_value"
    assert trace["requested_output_criterion_0_has_exact_value"] is True
    assert "requested_output_criterion_0_expected_output_shape" not in trace
    assert trace["requested_output_criterion_1_id"] == "c_confirmation"
    assert trace["requested_output_criterion_1_output_path"] == "output.confirmation_number"
    assert trace["requested_output_criterion_1_grounding_mode"] == "shape"
    assert trace["requested_output_criterion_1_expected_output_shape"] == "reference_code"
    assert trace["requested_output_criterion_1_has_exact_value"] is False
    assert trace["requested_output_criterion_2_id"] == "c_status"
    assert trace["requested_output_criterion_2_output_path"] == "output.status"
    assert trace["requested_output_criterion_2_grounding_mode"] == "missing"
    assert trace["requested_output_criterion_2_has_exact_value"] is False
    assert "1234 Sample Utility Way" not in repr(trace)


@pytest.mark.parametrize(
    ("stored_criterion", "fresh_criterion"),
    [
        pytest.param(
            _criterion(
                "s0",
                "The returned record includes service address.",
                output_path="output.service_address",
                expected_output_value="1234 Sample Utility Way",
            ),
            _criterion(
                "c0",
                "The returned record includes service address.",
                output_path="output.service_address",
                expected_output_value="7890 Changed Ave",
            ),
            id="changed-expected-output-value",
        ),
        pytest.param(
            _criterion(
                "s0",
                "The returned record includes confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="reference_code",
            ),
            _criterion(
                "c0",
                "The returned record includes confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="numeric_identifier",
            ),
            id="changed-expected-output-shape",
        ),
        pytest.param(
            _criterion(
                "s0",
                "The run classifies whether the path is login gated.",
                kind="validation_classification",
                classification_output_key="path_classification",
                expected_classification="login_gated",
            ),
            _criterion(
                "c0",
                "The run classifies whether the path is public.",
                kind="validation_classification",
                classification_output_key="path_classification",
                expected_classification="public",
            ),
            id="changed-classification-target",
        ),
        pytest.param(
            _criterion("s0", "A provider blocker is reported to the user."),
            _criterion(
                "c0",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
            ),
            id="added-contingent-on",
        ),
        pytest.param(
            _criterion("s0", "A provider blocker is reported to the user."),
            _criterion(
                "c0",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
                contingent_antecedent_output_path="output.blocker",
            ),
            id="added-structural-contingent-on",
        ),
    ],
)
def test_reconcile_supersedes_or_keeps_distinct(
    stored_criterion: CompletionCriterion, fresh_criterion: CompletionCriterion
) -> None:
    stored = _stored(stored_criterion)
    fresh = [fresh_criterion]

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        fresh,
        actionable=True,
    )

    assert decision.action == "create"
    assert decision.criteria == tuple(fresh)


@pytest.mark.asyncio
async def test_requested_output_criteria_survive_cap_with_existing_and_generic_criteria() -> None:
    criteria = [{"outcome": f"Specific retained outcome {index}."} for index in range(7)]
    criteria.append(
        {
            "outcome": "The workflow runs to its intended end state with the expected output.",
            "method_mandated": True,
        }
    )

    policy = await _policy_for_message("Return a final record with record id.", criteria)

    assert len(policy.completion_criteria) == 8
    rendered = "\n".join(_outcomes(policy))
    assert "record id" in rendered
    assert "Specific retained outcome" in rendered
    assert "intended end state" in rendered
    assert _criteria_for_path(policy, "output.record_id")

    criteria = [{"outcome": f"Specific retained outcome {index}."} for index in range(7)]
    criteria.append({"outcome": "The returned record includes record id."})

    policy = await _policy_for_message("Return a final record with record id and status.", criteria)

    assert len(policy.completion_criteria) == 8
    rendered = "\n".join(_outcomes(policy))
    assert "record id" in rendered
    assert "status" in rendered
    assert "Specific retained outcome" in rendered
    assert _criteria_for_path(policy, "output.record_id")
    assert _criteria_for_path(policy, "output.status")


def test_requested_output_criteria_survive_cap_when_already_present_before_augmentation() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id=f"c{index}", outcome=f"Specific retained outcome {index}.") for index in range(7)
        ]
        + [
            CompletionCriterion(id="record_id", outcome="The returned record includes record id."),
            CompletionCriterion(
                id="floor",
                outcome="The workflow runs to its intended end state with the expected output.",
                method_mandated=True,
            ),
        ]
    )

    _apply_requested_output_completion_criteria(policy, "Return a final record with record id.")

    assert len(policy.completion_criteria) == 8
    assert [criterion.output_path for criterion in _requested_output_subset(policy, {"output.record_id"})] == [
        "output.record_id"
    ]
    assert any("intended end state" in criterion.outcome for criterion in policy.completion_criteria)


def test_requested_output_criteria_can_exceed_cap_without_dropping_requested_fields() -> None:
    policy = RequestPolicy()

    _apply_requested_output_completion_criteria(
        policy,
        "Return a final record with name, record id, status, phone, email, license, taxonomy, specialty, and date.",
    )

    assert len(policy.completion_criteria) == 9
    assert {criterion.output_path for criterion in policy.completion_criteria} == {
        "output.name",
        "output.record_id",
        "output.status",
        "output.phone",
        "output.email",
        "output.license",
        "output.taxonomy",
        "output.specialty",
        "output.date",
    }


def test_parser_preserves_boolean_expected_value_and_promotes_independent_source() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record selects the highest-priority document.",
                "output_path": "output.selected_highest_priority",
                "expected_output_value": True,
            }
        ]
    )

    assert len(criteria) == 1
    assert criteria[0].expected_output_value is True
    assert criteria[0].requested_output_evidence_source == "independent_run_evidence"
    assert _criterion_grounding_mode(criteria[0]) == "judgment_boolean"


def test_parser_promotes_goal_judgment_boolean_shape_without_explicit_value() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record reports whether the highest-priority item was selected.",
                "output_path": "output.selected_highest_priority",
                "expected_output_shape": "goal_judgment_boolean",
            }
        ]
    )

    assert criteria[0].expected_output_shape == "goal_judgment_boolean"
    assert criteria[0].requested_output_evidence_source == "independent_run_evidence"
    assert _criterion_grounding_mode(criteria[0]) == "judgment_boolean"


def test_parser_mints_login_gate_judgment_truth_condition_from_classifier_fields() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record reports whether the login gate blocks the target.",
                "output_path": "output.login_gate_blocks_target",
                "expected_output_shape": "goal_judgment_boolean",
                "requested_output_evidence_source": "independent_run_evidence",
                "judgment_predicate": "login_gate_blocks_target",
                "judgment_polarity_when_holds": True,
            }
        ]
    )

    assert criteria[0].judgment_truth_condition == JudgmentTruthCondition(
        predicate="login_gate_blocks_target", polarity_when_holds=True
    )


def test_backticked_named_output_mints_login_gate_requested_output_truth_condition() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            replace(
                _criterion(
                    "c_login_gate",
                    "The returned record reports login gate blocks target.",
                    expected_output_shape="goal_judgment_boolean",
                    requested_output_evidence_source="independent_run_evidence",
                ),
                judgment_truth_condition=JudgmentTruthCondition(
                    predicate="login_gate_blocks_target", polarity_when_holds=True
                ),
            )
        ]
    )

    _apply_requested_output_completion_criteria(
        policy,
        "Create a validation workflow and return the named output `login_gate_blocks_target`.",
    )

    criteria = _criteria_for_path(policy, "output.login_gate_blocks_target")
    assert len(criteria) == 1
    assert criteria[0].expected_output_shape == "goal_judgment_boolean"
    assert criteria[0].requested_output_evidence_source == "independent_run_evidence"
    assert criteria[0].judgment_truth_condition == JudgmentTruthCondition(
        predicate="login_gate_blocks_target", polarity_when_holds=True
    )


def test_parser_dedup_keeps_runtime_string_and_judgment_boolean_on_same_path() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes the selection label.",
                "output_path": "output.selection",
                "expected_output_value": "true",
            },
            {
                "outcome": "The returned record reports the selection judgment.",
                "output_path": "output.selection",
                "expected_output_value": True,
            },
        ]
    )

    assert len(criteria) == 2
    modes = {_criterion_grounding_mode(criterion) for criterion in criteria}
    assert modes == {"exact_value", "judgment_boolean"}


def test_declared_judgment_coerces_stringy_boolean_to_typed_bool() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record reports the highest-priority selection judgment.",
                "output_path": "output.selected_highest_priority",
                "expected_output_value": "true",
                "requested_output_evidence_source": "independent_run_evidence",
            }
        ]
    )

    assert criteria[0].expected_output_value is True
    assert _criterion_grounding_mode(criteria[0]) == "judgment_boolean"


def test_bare_stringy_boolean_without_judgment_signal_stays_exact_value() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes the selection label.",
                "output_path": "output.selection",
                "expected_output_value": "true",
            }
        ]
    )

    assert criteria[0].expected_output_value == "true"
    assert _criterion_grounding_mode(criteria[0]) == "exact_value"


def test_store_round_trip_preserves_boolean_expected_value_and_source() -> None:
    criteria = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record reports the false judgment.",
                "output_path": "output.is_duplicate",
                "expected_output_value": False,
            }
        ]
    )

    round_tripped = criteria_from_json(criteria_to_json(list(criteria)))

    assert round_tripped[0].expected_output_value is False
    assert round_tripped[0].requested_output_evidence_source == "independent_run_evidence"
    assert _criterion_grounding_mode(round_tripped[0]) == "judgment_boolean"


def test_store_round_trip_preserves_judgment_truth_condition() -> None:
    criterion = replace(
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        ),
        judgment_truth_condition=JudgmentTruthCondition(predicate="login_gate_blocks_target", polarity_when_holds=True),
    )

    round_tripped = criteria_from_json(criteria_to_json([criterion]))

    assert round_tripped[0].judgment_truth_condition == JudgmentTruthCondition(
        predicate="login_gate_blocks_target", polarity_when_holds=True
    )
    assert _criterion_reconcile_key(round_tripped[0]) == _criterion_reconcile_key(criterion)


def test_expected_false_survives_active_criteria_rendering() -> None:
    criterion = _criterion(
        "c_bool",
        "The returned record reports the false judgment.",
        output_path="output.is_duplicate",
        expected_output_value=False,
        requested_output_evidence_source="independent_run_evidence",
    )

    rendered = json.loads(_render_active_criteria_for_prompt([criterion]))

    assert rendered[0]["expected_output_value"] is False


def test_reconcile_key_distinguishes_false_from_absent_and_string_false() -> None:
    false_bool = _criterion(
        "c_false_bool",
        "outcome",
        output_path="output.flag",
        expected_output_value=False,
        requested_output_evidence_source="independent_run_evidence",
    )
    absent = _criterion("c_absent", "outcome", output_path="output.flag")
    string_false = _criterion(
        "c_string_false",
        "outcome",
        output_path="output.flag",
        expected_output_value="false",
    )

    keys = {
        _criterion_reconcile_key(false_bool),
        _criterion_reconcile_key(absent),
        _criterion_reconcile_key(string_false),
    }
    assert len(keys) == 3


def test_criteria_from_json_coerces_stored_stringy_judgment_boolean_to_typed_bool() -> None:
    raw_outcome = "The returned record reports the highest-priority selection judgment."
    raw_criterion = {
        "outcome": raw_outcome,
        "output_path": "output.selected_highest_priority",
        "expected_output_value": "true",
        "requested_output_evidence_source": "independent_run_evidence",
    }
    restored = criteria_from_json([{"id": "c0", **raw_criterion}])
    parsed = _parse_completion_criteria([raw_criterion])

    assert restored[0].expected_output_value is True
    assert _criterion_reconcile_key(restored[0]) == _criterion_reconcile_key(parsed[0])


def test_scope_boundary_boolean_on_login_only_rekinds_and_drops_judgment_invariant() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_login_only",
                "The workflow only reaches the login page.",
                output_path="output.login_only",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            )
        ]
    )

    _apply_validation_classification_completion_criteria(policy)

    promoted = policy.completion_criteria[0]
    assert promoted.kind == "validation_classification"
    assert promoted.classification_output_key == "login_only"
    assert promoted.expected_output_value is None
    assert promoted.requested_output_evidence_source == "independent_run_evidence"
    assert promoted.mint_disposition == "pending"


def test_scope_boundary_boolean_on_non_enumerated_path_keeps_judgment_invariant() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_judgment",
                "The returned record reports the selection judgment.",
                output_path="output.selected_highest_priority",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            )
        ]
    )

    _apply_validation_classification_completion_criteria(policy)

    kept = policy.completion_criteria[0]
    assert kept.kind == "outcome"
    assert kept.expected_output_value is True
    assert kept.requested_output_evidence_source == "independent_run_evidence"


@pytest.mark.asyncio
async def test_classifier_success_path_degrades_pathless_contingent_criterion() -> None:
    policy = await _policy_for_message(
        "Submit the request and report any blocker.",
        [
            {"outcome": "A blocker is reported to the user.", "contingent_on": "the site blocks submission"},
            {
                "outcome": "A blocker is reported with evidence.",
                "contingent_on": "the site blocks submission with evidence",
                "contingent_antecedent_output_path": "output.blocker",
            },
        ],
    )

    by_contingent = {c.contingent_on: c for c in policy.completion_criteria if c.contingent_on}
    assert by_contingent["the site blocks submission"].mint_degrade == "contingent_missing_antecedent"
    assert by_contingent["the site blocks submission with evidence"].mint_degrade is None


@pytest.mark.asyncio
async def test_canonicalization_carry_of_pathless_contingent_is_mint_degraded() -> None:
    policy = await _policy_for_message(
        "Return a final record with NPI.",
        [
            {
                "outcome": "The returned record includes NPI.",
                "output_path": "output.npi",
                "contingent_on": "the provider site allows online lookup",
            }
        ],
    )

    (canonical,) = (c for c in policy.completion_criteria if c.output_path == "output.npi")
    assert canonical.contingent_on == "the provider site allows online lookup"
    assert canonical.contingent_antecedent_output_path is None
    assert canonical.mint_degrade == "contingent_missing_antecedent"


def test_fallback_policy_upholds_contingent_mint_invariant() -> None:
    policy = _classifier_fallback_policy(
        [],
        raw_secret_present=False,
        failure_kind="provider_error",
        user_message="return the status for the record",
    )

    assert policy.completion_criteria
    assert all(
        criterion.contingent_antecedent_output_path is not None or criterion.mint_degrade is not None
        for criterion in policy.completion_criteria
        if criterion.contingent_on
    )


def test_degrade_sweep_targets_only_pathless_contingent_criteria() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion("c0", "A blocker is reported to the user.", contingent_on="the site blocks submission"),
            _criterion(
                "c1",
                "A blocker is reported with evidence.",
                contingent_on="the site blocks submission with evidence",
                contingent_antecedent_output_path="output.blocker",
            ),
            _criterion("c2", "The request is submitted."),
        ]
    )

    _degrade_pathless_contingent_criteria(policy)

    degrades = {criterion.id: criterion.mint_degrade for criterion in policy.completion_criteria}
    assert degrades == {"c0": "contingent_missing_antecedent", "c1": None, "c2": None}


def test_degrade_sweep_preserves_existing_mint_degrade() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            replace(
                _criterion("c0", "return the status", contingent_on="the site blocks submission"),
                mint_degrade="turn_unsatisfiable_fallback",
            )
        ]
    )

    _degrade_pathless_contingent_criteria(policy)

    assert policy.completion_criteria[0].mint_degrade == "turn_unsatisfiable_fallback"


def test_to_trace_data_reports_mint_degraded_criteria_without_output_path() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            replace(
                _criterion("c0", "A blocker is reported to the user.", contingent_on="the site blocks submission"),
                mint_degrade="contingent_missing_antecedent",
            ),
            _criterion("c1", "The request is submitted."),
        ]
    )

    data = policy.to_trace_data()

    assert data["mint_degraded_criterion_count"] == 1
    assert data["mint_degraded_criterion_0_id"] == "c0"
    assert data["mint_degraded_criterion_0_mint_degrade"] == "contingent_missing_antecedent"
