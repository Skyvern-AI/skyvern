from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    criteria_from_json,
    criteria_to_json,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    RequestPolicy,
    _apply_requested_output_completion_criteria,
    _classify_request,
    _parse_completion_criteria,
    _render_active_criteria_for_prompt,
)


async def _policy_for_message(
    user_message: str,
    criteria: list[dict[str, Any]],
    *,
    config: CopilotConfig | None = None,
) -> RequestPolicy:
    async def _handler(**_: Any) -> dict[str, Any]:
        return {
            "testing_intent": "require_test",
            "credential_input_kind": "none",
            "requires_user_clarification": False,
            "completion_criteria": criteria,
        }

    return await _classify_request(
        user_message,
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        handler=_handler,
        config=config,
    )


def _stored(*criteria: CompletionCriterion) -> StoredCriteriaSet:
    return StoredCriteriaSet(set_id="wccs_existing", goal_epoch=1, criteria=tuple(criteria))


def _criterion(
    cid: str,
    outcome: str,
    *,
    level: str = "run",
    output_path: str | None = None,
    method_mandated: bool = False,
    contingent_on: str | None = None,
    contingent_antecedent_output_path: str | None = None,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=cid,
        outcome=outcome,
        level=level,  # type: ignore[arg-type]
        output_path=output_path,
        method_mandated=method_mandated,
        contingent_on=contingent_on,
        contingent_antecedent_output_path=contingent_antecedent_output_path,
    )


def _outcomes(policy: RequestPolicy) -> list[str]:
    return [criterion.outcome for criterion in policy.completion_criteria]


def _criteria_for_path(policy: RequestPolicy, output_path: str) -> list[CompletionCriterion]:
    return [criterion for criterion in policy.completion_criteria if criterion.output_path == output_path]


def _criteria_fingerprint(criteria: list[CompletionCriterion]) -> str:
    payload = json.dumps(criteria_to_json(criteria), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _requested_output_subset(policy: RequestPolicy, requested_output_paths: set[str]) -> list[CompletionCriterion]:
    return [
        criterion
        for criterion in policy.completion_criteria
        if criterion.level == "run"
        and not criterion.method_mandated
        and criterion.output_path in requested_output_paths
    ]


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
async def test_unbound_requested_output_narrative_is_replaced_but_unrelated_run_gate_remains() -> None:
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


def test_active_criteria_rendering_includes_contingent_on() -> None:
    rendered = _render_active_criteria_for_prompt(
        [
            _criterion(
                "c0",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
            )
        ]
    )

    assert json.loads(rendered) == [
        {
            "outcome": "A provider blocker is reported to the user.",
            "implicit": False,
            "method_mandated": False,
            "level": "run",
            "kind": "outcome",
            "terminal_action_family": None,
            "contingent_on": "the provider site blocks online submission",
        }
    ]


def test_active_criteria_rendering_includes_contingent_antecedent_output_path() -> None:
    rendered = _render_active_criteria_for_prompt(
        [
            _criterion(
                "c0",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
                contingent_antecedent_output_path="output.blocker",
            )
        ]
    )

    assert json.loads(rendered) == [
        {
            "outcome": "A provider blocker is reported to the user.",
            "implicit": False,
            "method_mandated": False,
            "level": "run",
            "kind": "outcome",
            "terminal_action_family": None,
            "contingent_on": "the provider site blocks online submission",
            "contingent_antecedent_output_path": "output.blocker",
        }
    ]


def test_criteria_json_round_trips_contingent_on() -> None:
    criteria = (
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
        ),
    )

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored == criteria


def test_criteria_json_round_trips_contingent_antecedent_output_path() -> None:
    criteria = (
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        ),
    )

    restored = criteria_from_json(criteria_to_json(criteria))

    assert restored == criteria


def test_reconcile_keeps_conditional_and_unconditional_same_outcome_distinct() -> None:
    stored = _stored(_criterion("s0", "A provider blocker is reported to the user."))
    fresh = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
        )
    ]

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        fresh,
        actionable=True,
    )

    assert decision.action == "create"
    assert decision.criteria == tuple(fresh)


def test_reconcile_keeps_structural_conditional_and_unconditional_same_outcome_distinct() -> None:
    stored = _stored(_criterion("s0", "A provider blocker is reported to the user."))
    fresh = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]

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
