from __future__ import annotations

from typing import Any

import pytest

from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.request_policy import (
    _classification_from_raw,
    _classify_request,
    schema_output_path_aliases_from_criteria,
)
from skyvern.forge.sdk.copilot.request_slots import PROMPT_NAME as REQUEST_SLOT_PROMPT_NAME
from skyvern.forge.sdk.copilot.request_slots import (
    RequestSlotDeclarationV1,
    RequestSlotEnvelopeV1,
    RequestSlotPinability,
    RequestSlotPlane,
    RequestSlotProducerInputV1,
    canonicalize_request_slots,
)

P9_REQUEST = "Return status must equal complete, the visible path label, and whether the path is login-only."


def _request() -> RequestSlotProducerInputV1:
    return RequestSlotProducerInputV1(
        version="1",
        latest_request=P9_REQUEST,
        workflow_context="",
        earliest_user_turn="",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
    )


def _envelope() -> RequestSlotEnvelopeV1:
    return RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="status must equal complete",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.PINNED,
            ),
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="the visible path label",
                plane=RequestSlotPlane.DEFINITION,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
            ),
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="whether the path is login-only",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.UNPINNABLE,
            ),
        ),
    )


def _fresh_payload() -> dict[str, Any]:
    return {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The returned status is complete.",
                "output_path": "output.model_owned_status",
                "expected_output_value": "complete",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "status must equal complete",
            },
            {
                "outcome": "The workflow exposes the visible path label.",
                "output_path": "output.model_owned_label",
                "expected_output_value": "guessed label",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "visible path label",
            },
            {
                "outcome": "The run classifies whether the path is login-only.",
                "kind": "validation_classification",
                "classification_output_key": "login_only",
                "expected_classification": True,
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "whether the path is login-only",
            },
        ],
    }


def test_fresh_contract_consumes_declared_pinability_without_guessed_polarity() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())

    policy = _classification_from_raw(
        _fresh_payload(),
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert [criterion.request_slot_id for criterion in policy.completion_criteria] == [
        slot.slot_id for slot in contract.slots
    ]
    assert [criterion.output_path for criterion in policy.completion_criteria] == [
        "output.model_owned_status",
        None,
        None,
    ]
    assert all(
        not (criterion.output_path or "").startswith("output.request_slot_") for criterion in policy.completion_criteria
    )
    assert [criterion.level for criterion in policy.completion_criteria] == ["run", "definition", "run"]
    assert [criterion.pinability for criterion in policy.completion_criteria] == [
        "pinned",
        "shapeless_valid",
        "unpinnable",
    ]

    pinned, shapeless, unpinnable = policy.completion_criteria
    assert pinned.expected_output_value == "complete"
    assert pinned.mint_degrade is None
    assert shapeless.expected_output_value is None
    assert shapeless.expected_output_shape is None
    assert shapeless.mint_degrade is None
    assert shapeless.floor_rekeyed_from_path == "output.model_owned_label"
    assert unpinnable.kind == "outcome"
    assert unpinnable.expected_classification is None
    assert unpinnable.expected_output_value is None
    assert unpinnable.mint_disposition == "degraded"
    assert unpinnable.mint_degrade == "undecidable_judgment"
    assert unpinnable.requested_output_floor_rekeyed is True
    assert unpinnable.floor_rekeyed_from_path == "output.login_only"
    aliases = schema_output_path_aliases_from_criteria(policy.completion_criteria)
    assert aliases == {
        "model owned status": "output.model_owned_status",
        "model": "output.model_owned_status",
        "owned": "output.model_owned_status",
        "status": "output.model_owned_status",
    }
    assert all("request slot" not in alias for alias in aliases)


def test_fresh_pinned_has_exact_value_fallthrough_degrades_instead_of_guessing() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())
    payload = _fresh_payload()
    payload["completion_criteria"][0].pop("expected_output_value")

    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    pinned = policy.completion_criteria[0]
    assert pinned.expected_output_value is None
    assert pinned.expected_output_shape is None
    assert pinned.mint_disposition == "degraded"
    assert pinned.mint_degrade == "undecidable_judgment"
    assert pinned.output_path is None
    assert pinned.floor_rekeyed_from_path == "output.model_owned_status"


def test_producer_only_slots_are_authoritative_contract_members() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())
    payload = _fresh_payload()
    payload["completion_criteria"] = payload["completion_criteria"][:1]

    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert [criterion.request_slot_id for criterion in policy.completion_criteria] == [
        slot.slot_id for slot in contract.slots
    ]
    assert [criterion.outcome for criterion in policy.completion_criteria[1:]] == [
        "the visible path label",
        "whether the path is login-only",
    ]
    assert [criterion.pinability for criterion in policy.completion_criteria] == [
        "pinned",
        "shapeless_valid",
        "unpinnable",
    ]


def test_duplicate_classifier_bindings_keep_one_criterion_per_producer_slot() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())
    payload = _fresh_payload()
    duplicate = dict(payload["completion_criteria"][0])
    duplicate["outcome"] = "A duplicate alias must not replace the first binding."
    payload["completion_criteria"].insert(1, duplicate)

    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert len(policy.completion_criteria) == contract.count
    assert policy.completion_criteria[0].outcome == "The returned status is complete."
    assert len({criterion.request_slot_id for criterion in policy.completion_criteria}) == contract.count


def test_fresh_contract_keeps_whole_list_caps_dedup_and_download_rules() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())
    payload = _fresh_payload()
    payload["completion_criteria"] = [
        *payload["completion_criteria"],
        {
            "outcome": "The first requested download is returned.",
            "deliverable_kind": "registered_download",
        },
        {
            "outcome": "The second requested download is returned.",
            "deliverable_kind": "registered_download",
        },
        *({"outcome": f"Additional outcome {index}."} for index in range(8)),
        {"outcome": "Additional outcome 0."},
    ]

    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert len(policy.completion_criteria) == 8
    downloads = [criterion for criterion in policy.completion_criteria if criterion.deliverable_kind]
    assert len(downloads) == 2
    assert all(criterion.output_path is None for criterion in downloads)
    assert all(criterion.mint_degrade is None for criterion in downloads)
    assert len({criterion.outcome for criterion in policy.completion_criteria}) == 8


def test_followup_source_digest_change_adopts_stored_semantic_criteria() -> None:
    first_request = _request()
    followup_request = first_request.model_copy(update={"latest_request": f"{P9_REQUEST} Please continue."})
    first_contract = canonicalize_request_slots(request=first_request, envelope=_envelope())
    followup_contract = canonicalize_request_slots(request=followup_request, envelope=_envelope())
    first = _classification_from_raw(
        _fresh_payload(), request_slot_request=first_request, request_slot_contract=first_contract
    )
    followup = _classification_from_raw(
        _fresh_payload(), request_slot_request=followup_request, request_slot_contract=followup_contract
    )

    assert [criterion.request_slot_id for criterion in first.completion_criteria] != [
        criterion.request_slot_id for criterion in followup.completion_criteria
    ]
    stored = StoredCriteriaSet(set_id="set-1", goal_epoch=1, criteria=tuple(first.completion_criteria))
    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        followup.completion_criteria,
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.superseded_set_id is None


def test_unbound_typed_criteria_fail_safe_without_entering_legacy_minting() -> None:
    payload = _fresh_payload()

    policy = _classification_from_raw(payload)

    assert policy.request_slot_failure_kind == "missing_request_slot_contract"
    assert all(criterion.output_path is None for criterion in policy.completion_criteria)
    assert all(criterion.expected_output_value is None for criterion in policy.completion_criteria)
    assert all(criterion.expected_classification is None for criterion in policy.completion_criteria)
    assert all(criterion.mint_degrade == "undecidable_judgment" for criterion in policy.completion_criteria)


def test_fresh_slot_failure_preserves_non_output_outcome() -> None:
    policy = _classification_from_raw(
        {
            "completion_criteria": [{"outcome": "The application is submitted."}],
        },
        request_slot_failure_kind="invalid_output",
    )

    assert policy.request_slot_failure_kind == "invalid_output"
    assert policy.completion_criteria[0].outcome == "The application is submitted."
    assert policy.completion_criteria[0].mint_degrade is None


@pytest.mark.asyncio
async def test_classifier_seam_replays_one_prompt_with_identical_consumed_mint_sets() -> None:
    identities: list[tuple[tuple[str, str, str, str], ...]] = []

    for _ in range(3):
        calls: list[str] = []

        async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
            calls.append(prompt_name)
            if prompt_name == REQUEST_SLOT_PROMPT_NAME:
                return _envelope().model_dump(mode="json")
            return _fresh_payload()

        policy = await _classify_request(P9_REQUEST, "", [], "", handler)
        identities.append(
            tuple(
                (
                    criterion.request_slot_id or "",
                    criterion.output_path or "",
                    criterion.level,
                    criterion.pinability or "",
                )
                for criterion in policy.completion_criteria
            )
        )
        assert calls.count(REQUEST_SLOT_PROMPT_NAME) == 2

    assert identities == [identities[0], identities[0], identities[0]]
    assert len(identities[0]) == 3


@pytest.mark.asyncio
async def test_request_slot_producer_failure_degrades_without_legacy_guessing() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return {"invalid": "request-slot payload"}
        return _fresh_payload()

    policy = await _classify_request(P9_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", *([REQUEST_SLOT_PROMPT_NAME] * 4)]
    assert policy.request_slot_failure_kind == "invalid_output"
    assert all(criterion.output_path is None for criterion in policy.completion_criteria)
    assert all(criterion.expected_output_value is None for criterion in policy.completion_criteria)
    assert all(criterion.expected_classification is None for criterion in policy.completion_criteria)
    assert all(criterion.mint_degrade == "undecidable_judgment" for criterion in policy.completion_criteria)


@pytest.mark.asyncio
async def test_classifier_payload_without_request_slots_does_not_invoke_request_slot_producer() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        return {
            "testing_intent": "require_test",
            "credential_input_kind": "none",
            "requires_user_clarification": False,
            "completion_criteria": [{"outcome": "The requested workflow is complete."}],
        }

    await _classify_request("Build the workflow.", "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy"]


@pytest.mark.asyncio
async def test_fresh_non_output_request_does_not_invoke_request_slot_producer() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        return {
            "testing_intent": "require_test",
            "credential_input_kind": "none",
            "requires_user_clarification": False,
            "completion_criteria": [{"outcome": "The application is submitted."}],
        }

    policy = await _classify_request("Submit the application.", "", [], "", handler)

    assert policy.request_slot_failure_kind is None
    assert policy.completion_criteria[0].outcome == "The application is submitted."
    assert policy.completion_criteria[0].mint_degrade is None
    assert calls == ["workflow-copilot-request-policy"]
