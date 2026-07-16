from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    criteria_from_json,
    criteria_to_json,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import RunEvidenceSnapshot, evaluate_completion_criteria
from skyvern.forge.sdk.copilot.request_policy import (
    _accept_request_slot_anchor_correction,
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

P9_REQUEST = "Return completion status, the visible path label, and whether the path is login-only."
CONFIRMATION_NUMBER_REQUEST = "Turn on international roaming and return the confirmation number."
EXAMPLE_WATER_SERVICE_REQUEST = (
    "I need a reusable workflow that starts a commercial water service request in the mock Example Waterworks "
    "portal at https://water.example.test/. It should use the business name, contact email, "
    "service address, and desired start date as reusable inputs. For this eval run use Example Property Labs Inc, "
    "utilities@example.test, 1842 Example Avenue, Example City, EX 00000, and 2026-06-22. The workflow should "
    "submit the request when the reviewable form is complete, then return a safe result with the confirmation number, "
    "account number, selected start date, deposit amount, and next owner. If the site only exposes an email/manual-"
    "service path instead of an online form, report that as the blocker rather than inventing values."
)


def _synthetic_anchor_only_payload() -> dict[str, Any]:
    # The original classifier packet was not retained. This synthetic reconstruction
    # is limited to the anchor-only shape diagnosed in the live canary's backend.log:59.
    return {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation number.",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "confirmation number",
            },
            {
                "outcome": "The workflow returns the account number.",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "account number",
            },
            {
                "outcome": "The workflow returns the selected start date.",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "selected start date",
            },
            {
                "outcome": "The workflow returns the deposit amount.",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "deposit amount",
            },
        ],
    }


def _anchor_only_envelope(
    *,
    pinability: RequestSlotPinability = RequestSlotPinability.SHAPELESS_VALID,
) -> RequestSlotEnvelopeV1:
    return RequestSlotEnvelopeV1(
        version="1",
        slots=tuple(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=pinability,
            )
            for source_quote in ("confirmation number", "account number", "selected start date", "deposit amount")
        ),
    )


_EXAMPLE_WATER_SERVICE_SLOT_OUTCOMES = (
    ("The workflow returns the confirmation number.", "confirmation number"),
    ("The workflow returns the account number.", "account number"),
    ("The workflow returns the selected start date.", "selected start date"),
    ("The workflow returns the deposit amount.", "deposit amount"),
    ("The workflow returns the next owner.", "next owner"),
    ("The workflow reports an email or manual-service path as the blocker.", "email/manual-service path"),
)


def _synthetic_eight_row_anchor_payload(*, valid_anchors: bool) -> dict[str, Any]:
    # The live classifier packet was not retained. This labeled reconstruction mirrors
    # the observed eight-row decision and six degraded request-slot claims without
    # inventing output values or evidence shapes.
    return {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "opaque_provider_metadata": {"source": "original"},
        "completion_criteria": [
            {"outcome": "The workflow uses the declared reusable inputs."},
            {"outcome": "The workflow submits the commercial water service request."},
            *[
                {
                    "outcome": outcome,
                    "request_slot_source_id": "u0" if valid_anchors else "u9",
                    "request_slot_source_quote": source_quote,
                    "opaque_provider_metadata": "original",
                }
                for outcome, source_quote in _EXAMPLE_WATER_SERVICE_SLOT_OUTCOMES
            ],
        ],
    }


def _semantically_identical_anchor_correction() -> dict[str, Any]:
    corrected = _synthetic_eight_row_anchor_payload(valid_anchors=True)
    corrected.update(
        {
            "authoring_intent": None,
            "credential_refs": None,
            "login_page_urls": None,
            "completion_contract": None,
            "raw_secret_evidence": None,
            "raw_secret_handling": None,
            "clarification_reason": None,
            "opaque_provider_metadata": {"source": "corrected"},
        }
    )
    for item in corrected["completion_criteria"]:
        item.update(
            {
                "contingent_on": None,
                "contingent_antecedent_output_path": None,
                "deliverable_kind": None,
                "implicit": None,
                "method_mandated": None,
                "level": None,
                "output_path": None,
                "expected_output_value": None,
                "expected_output_shape": None,
                "requested_output_evidence_source": None,
                "kind": None,
                "terminal_action_family": None,
                "classification_output_key": None,
                "expected_classification": None,
                "judgment_predicate": None,
                "judgment_polarity_when_holds": None,
                "opaque_provider_metadata": "corrected",
            }
        )
    return corrected


def _example_water_service_envelope() -> RequestSlotEnvelopeV1:
    return RequestSlotEnvelopeV1(
        version="1",
        slots=tuple(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
            )
            for _outcome, source_quote in _EXAMPLE_WATER_SERVICE_SLOT_OUTCOMES
        ),
    )


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
                source_quote="completion status",
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
                "output_path": "output.completion_status",
                "expected_output_value": "complete",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "completion status",
            },
            {
                "outcome": "The workflow exposes the visible path label.",
                "output_path": "output.visible_path_label",
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
        "output.completion_status",
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
    assert shapeless.floor_rekeyed_from_path == "output.visible_path_label"
    assert unpinnable.kind == "outcome"
    assert unpinnable.expected_classification is None
    assert unpinnable.expected_output_value is None
    assert unpinnable.mint_disposition == "degraded"
    assert unpinnable.mint_degrade == "undecidable_judgment"
    assert unpinnable.requested_output_floor_rekeyed is True
    assert unpinnable.floor_rekeyed_from_path == "output.login_only"
    aliases = schema_output_path_aliases_from_criteria(policy.completion_criteria)
    assert aliases == {
        "completion status": "output.completion_status",
        "completion": "output.completion_status",
        "status": "output.completion_status",
    }
    assert all("request slot" not in alias for alias in aliases)


def test_fresh_contract_rejects_source_valid_wrong_datum_binding() -> None:
    request = _request()
    contract = canonicalize_request_slots(request=request, envelope=_envelope())
    payload = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the visible path label.",
                "output_path": "output.visible_path_label",
                "expected_output_shape": "status_label",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "completion status",
            }
        ]
    }

    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    rejected = next(
        criterion
        for criterion in policy.completion_criteria
        if criterion.floor_rekeyed_from_path == "output.visible_path_label"
    )
    assert rejected.request_slot_id is None
    assert rejected.mint_disposition == "degraded"
    assert rejected.mint_degrade == "undecidable_judgment"


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
    assert pinned.floor_rekeyed_from_path == "output.completion_status"


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
async def test_anchor_only_rows_bind_canonical_paths() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _anchor_only_envelope().model_dump(mode="json")
        return _synthetic_anchor_only_payload()

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", REQUEST_SLOT_PROMPT_NAME, REQUEST_SLOT_PROMPT_NAME]
    assert policy.request_slot_failure_kind is None
    assert len(policy.completion_criteria) == 4
    assert all(criterion.level == "run" for criterion in policy.completion_criteria)
    assert all(criterion.kind == "outcome" for criterion in policy.completion_criteria)
    assert all(criterion.pinability == "shapeless_valid" for criterion in policy.completion_criteria)
    assert all(criterion.output_path is None for criterion in policy.completion_criteria)
    assert all(criterion.mint_disposition == "decidable" for criterion in policy.completion_criteria)
    assert all(criterion.mint_degrade is None for criterion in policy.completion_criteria)
    canonical_paths = [criterion.floor_rekeyed_from_path for criterion in policy.completion_criteria]
    assert all(path is not None and path.startswith("output.request_slot_") for path in canonical_paths)
    assert len(set(canonical_paths)) == 4
    assert policy.to_trace_data()["mint_degraded_criterion_count"] == 0


@pytest.mark.asyncio
async def test_invalid_anchor_only_claim_gets_one_correction() -> None:
    calls: list[str] = []
    classifier_prompts: list[str] = []
    invalid = _synthetic_anchor_only_payload()
    invalid["completion_criteria"] = [
        {
            "outcome": "The workflow returns the confirmation number.",
            "request_slot_source_id": "u9",
            "request_slot_source_quote": "confirmation number",
        }
    ]
    corrected = _synthetic_anchor_only_payload()
    corrected["completion_criteria"] = [corrected["completion_criteria"][0]]
    envelope = RequestSlotEnvelopeV1(version="1", slots=(_anchor_only_envelope().slots[0],))

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        classifier_prompts.append(prompt)
        return invalid if calls.count("workflow-copilot-request-policy") == 1 else corrected

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    assert len(classifier_prompts) == 2
    assert classifier_prompts[1].count('"source_id":"u0"') == 1
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind is None
    assert criterion.pinability == "shapeless_valid"
    assert criterion.mint_disposition == "decidable"
    assert criterion.floor_rekeyed_from_path is not None
    assert criterion.floor_rekeyed_from_path.startswith("output.request_slot_")


@pytest.mark.asyncio
async def test_invalid_anchor_only_correction_fails_closed() -> None:
    invalid = _synthetic_anchor_only_payload()
    invalid["completion_criteria"] = [
        {
            "outcome": "The workflow returns the confirmation number.",
            "request_slot_source_id": "u9",
            "request_slot_source_quote": "confirmation number",
        }
    ]
    drifted = _synthetic_anchor_only_payload()
    drifted["completion_criteria"] = [
        {
            **drifted["completion_criteria"][0],
            "outcome": "The workflow returns a guessed confirmation number.",
        }
    ]
    calls = 0

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return invalid if calls == 1 else drifted

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == 2
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"
    assert policy.completion_criteria[0].mint_disposition == "degraded"
    assert policy.completion_criteria[0].mint_degrade == "undecidable_judgment"


def test_anchor_correction_semantic_validation_does_not_emit_mint_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        request_policy_module,
        "LOG",
        SimpleNamespace(info=lambda event, **_: events.append(event)),
    )
    original = {
        "completion_criteria": [
            {
                "outcome": "The workflow confirms whether the request succeeded.",
                "output_path": "output.request_succeeded",
                "expected_output_value": True,
                "expected_output_shape": "goal_judgment_boolean",
                "judgment_predicate": "login_gate_blocks_target",
                "judgment_polarity_when_holds": True,
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "whether the request succeeded",
            },
            {
                "outcome": "The workflow downloads the receipt.",
                "deliverable_kind": "registered_download",
                "expected_output_shape": "status_label",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "downloads the receipt",
            },
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **criterion,
                "request_slot_source_id": "u0",
                "request_slot_source_quote": source_quote,
            }
            for criterion, source_quote in zip(
                original["completion_criteria"],
                ("whether the request succeeded", "downloads the receipt"),
                strict=True,
            )
        ]
    }

    accepted = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request="Return whether the request succeeded and downloads the receipt.",
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert accepted is not None
    assert events == []


@pytest.mark.asyncio
async def test_eight_row_anchor_correction_accepts_representation_drift_only() -> None:
    original = _synthetic_eight_row_anchor_payload(valid_anchors=False)
    corrected = _semantically_identical_anchor_correction()
    accepted = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request=EXAMPLE_WATER_SERVICE_REQUEST,
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert accepted is not None
    assert accepted["opaque_provider_metadata"] == {"source": "original"}
    accepted_criteria = accepted["completion_criteria"]
    assert len(accepted_criteria) == 8
    assert "kind" not in accepted_criteria[2]
    assert accepted_criteria[2]["opaque_provider_metadata"] == "original"
    assert [item.get("request_slot_source_id") for item in accepted_criteria[2:]] == ["u0"] * 6

    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _example_water_service_envelope().model_dump(mode="json")
        return original if calls.count("workflow-copilot-request-policy") == 1 else corrected

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    assert policy.request_slot_failure_kind is None
    assert len(policy.completion_criteria) == 8
    assert sum(criterion.request_slot_id is not None for criterion in policy.completion_criteria) == 6
    assert all(criterion.mint_disposition == "decidable" for criterion in policy.completion_criteria)
    assert policy.to_trace_data()["mint_degraded_criterion_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("correction_kind", ["semantic_drift", "reordered_rows", "claim_expansion"])
async def test_eight_row_anchor_correction_drift_fails_closed(correction_kind: str) -> None:
    original = _synthetic_eight_row_anchor_payload(valid_anchors=False)
    corrected = _semantically_identical_anchor_correction()
    corrected_criteria = corrected["completion_criteria"]
    if correction_kind == "semantic_drift":
        corrected_criteria[2]["outcome"] = "The workflow returns a guessed confirmation number."
    elif correction_kind == "reordered_rows":
        corrected_criteria[2], corrected_criteria[3] = corrected_criteria[3], corrected_criteria[2]
    else:
        corrected_criteria[0]["request_slot_source_id"] = "u0"
        corrected_criteria[0]["request_slot_source_quote"] = "reusable workflow"
    calls = 0

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original if calls == 1 else corrected

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == 2
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"
    assert sum(criterion.mint_disposition == "degraded" for criterion in policy.completion_criteria) == 6
    assert all(
        criterion.mint_degrade == "undecidable_judgment"
        for criterion in policy.completion_criteria
        if criterion.mint_disposition == "degraded"
    )


@pytest.mark.asyncio
async def test_recorded_confirmation_runs_reach_same_run_plane_terminal() -> None:
    recorded_packets = (
        {
            "workflow_run_id": "wr_synthetic_recorded_1",
            "confirmation_number": "WTR-1842-DEMO",
            "account_number": "100245",
            "selected_start_date": "2026-06-22",
        },
        {
            "workflow_run_id": "wr_synthetic_recorded_3",
            "confirmation_number": "WTR-1842-DEMO",
            "account_number": "100245",
            "selected_start_date": "2026-06-22",
        },
    )
    terminal_states: list[tuple[bool, bool, frozenset[str | None], int]] = []

    for packet in recorded_packets:
        original = _synthetic_eight_row_anchor_payload(valid_anchors=False)
        original["completion_criteria"] = original["completion_criteria"][2:5]
        corrected = _semantically_identical_anchor_correction()
        corrected["completion_criteria"] = corrected["completion_criteria"][2:5]
        envelope = _example_water_service_envelope()
        recorded_envelope = RequestSlotEnvelopeV1(version="1", slots=envelope.slots[:3])
        request_policy_calls = 0

        async def classify_handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
            nonlocal request_policy_calls
            if prompt_name == REQUEST_SLOT_PROMPT_NAME:
                return recorded_envelope.model_dump(mode="json")
            request_policy_calls += 1
            return original if request_policy_calls == 1 else corrected

        policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", classify_handler)
        reloaded_criteria = list(criteria_from_json(criteria_to_json(policy.completion_criteria)))
        evidence_label = f"recorded_{packet['workflow_run_id']}"

        async def verification_handler(**_: object) -> dict[str, Any]:
            return {
                "verdicts": [
                    {
                        "criterion_id": criterion.id,
                        "satisfied": True,
                        "reason_code": "evidence_confirms",
                        "evidence_ref": f"block_outputs:{evidence_label}",
                    }
                    for criterion in reloaded_criteria
                ]
            }

        verification = await evaluate_completion_criteria(
            reloaded_criteria,
            RunEvidenceSnapshot(
                workflow_run_id=packet["workflow_run_id"],
                block_outputs={evidence_label: packet},
                block_output_sources={evidence_label: "runtime_output"},
                run_terminal_status="completed",
            ),
            verification_handler,
        )

        assert policy.to_trace_data()["mint_degraded_criterion_count"] == 0
        terminal_states.append(
            (
                verification.is_fully_satisfied(),
                verification.no_gradeable_run_plane,
                frozenset(verdict.evidence_source for verdict in verification.verdicts),
                len(verification.criterion_ids),
            )
        )

    assert terminal_states == [(True, False, frozenset({"runtime_output"}), 3)] * 2


@pytest.mark.asyncio
async def test_anchor_only_slot_producer_failure_degrades() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return {"invalid": "request-slot payload"}
        return _synthetic_anchor_only_payload()

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", *([REQUEST_SLOT_PROMPT_NAME] * 4)]
    assert policy.request_slot_failure_kind == "invalid_output"
    assert all(criterion.mint_disposition == "degraded" for criterion in policy.completion_criteria)
    assert all(criterion.mint_degrade == "undecidable_judgment" for criterion in policy.completion_criteria)


@pytest.mark.asyncio
async def test_anchor_only_unpinnable_slots_degrade() -> None:
    calls: list[str] = []
    envelope = _anchor_only_envelope(pinability=RequestSlotPinability.UNPINNABLE)

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        return _synthetic_anchor_only_payload()

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", REQUEST_SLOT_PROMPT_NAME, REQUEST_SLOT_PROMPT_NAME]
    assert policy.request_slot_failure_kind is None
    assert all(criterion.pinability == "unpinnable" for criterion in policy.completion_criteria)
    assert all(criterion.mint_disposition == "degraded" for criterion in policy.completion_criteria)
    assert all(criterion.mint_degrade == "undecidable_judgment" for criterion in policy.completion_criteria)


@pytest.mark.asyncio
async def test_unanchored_requested_output_with_invalid_correction_fails_closed() -> None:
    calls: list[str] = []
    unanchored = {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "status_label",
            }
        ],
    }

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        return unanchored

    policy = await _classify_request(CONFIRMATION_NUMBER_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", "workflow-copilot-request-policy"]
    criterion = policy.completion_criteria[0]
    assert criterion.level == "run"
    assert criterion.kind == "outcome"
    assert criterion.pinability is None
    assert criterion.output_path is None
    assert criterion.expected_output_shape is None
    assert criterion.floor_rekeyed_from_path == "output.confirmation_number"
    assert criterion.mint_disposition == "degraded"
    assert criterion.mint_degrade == "undecidable_judgment"
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected_quote", ["account number", "number"])
async def test_unanchored_correction_must_match_its_structured_datum(corrected_quote: str) -> None:
    calls: list[str] = []
    request = "Return the confirmation code beside the adjacent account number."
    unanchored = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation code beside the account number.",
                "output_path": "output.confirmation_code",
                "expected_output_shape": "reference_code",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **unanchored["completion_criteria"][0],
                "request_slot_source_id": "u0",
                "request_slot_source_quote": corrected_quote,
            }
        ]
    }

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        return unanchored if len(calls) == 1 else corrected

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", "workflow-copilot-request-policy"]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"
    assert criterion.output_path is None
    assert criterion.expected_output_shape is None
    assert criterion.mint_disposition == "degraded"
    assert criterion.mint_degrade == "undecidable_judgment"


@pytest.mark.asyncio
async def test_unanchored_correction_tied_to_its_structured_datum_mints_decidable() -> None:
    calls: list[str] = []
    request = "Return the confirmation code."
    unanchored = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation code.",
                "output_path": "output.confirmation_code",
                "expected_output_shape": "reference_code",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **unanchored["completion_criteria"][0],
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "confirmation code",
            }
        ]
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="confirmation code",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        return unanchored if calls.count("workflow-copilot-request-policy") == 1 else corrected

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind is None
    assert criterion.floor_rekeyed_from_path == "output.confirmation_code"
    assert criterion.mint_disposition == "decidable"
    assert criterion.mint_degrade is None


@pytest.mark.asyncio
async def test_source_valid_wrong_datum_anchor_gets_one_correction_and_fails_closed() -> None:
    calls: list[str] = []
    request = "Return the confirmation ID and the adjacent account number."
    wrong_datum = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation ID.",
                "output_path": "output.confirmation_id",
                "expected_output_shape": "reference_code",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "account number",
            }
        ]
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="account number",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        return wrong_datum

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", "workflow-copilot-request-policy"]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"
    assert criterion.request_slot_id is None
    assert criterion.mint_disposition == "degraded"
    assert criterion.mint_degrade == "undecidable_judgment"


@pytest.mark.asyncio
async def test_anchor_correction_preserves_original_criterion_datum_quote() -> None:
    calls: list[str] = []
    request = "Return the confirmation code."
    original = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation code.",
                "output_path": "output.confirmation_code",
                "expected_output_shape": "reference_code",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "confirmation code",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **original["completion_criteria"][0],
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "Return the confirmation code",
            }
        ]
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="confirmation code",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        return original if calls.count("workflow-copilot-request-policy") == 1 else corrected

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind is None
    assert criterion.floor_rekeyed_from_path == "output.confirmation_code"
    assert criterion.mint_disposition == "decidable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "correction_kind",
    ["semantic_drift", "duplicate_quote", "unknown_source", "adjacent_datum", "bare_shared_token"],
)
async def test_invalid_anchor_correction_fails_closed(correction_kind: str) -> None:
    calls: list[str] = []
    if correction_kind == "duplicate_quote":
        request = "Return status and status."
    elif correction_kind in {"adjacent_datum", "bare_shared_token"}:
        request = "Return the confirmation ID and the adjacent account number."
    else:
        request = CONFIRMATION_NUMBER_REQUEST
    unanchored = {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "status_label",
            }
        ],
    }
    if correction_kind in {"adjacent_datum", "bare_shared_token"}:
        unanchored["completion_criteria"][0]["outcome"] = (
            "The workflow returns the confirmation number beside the account data."
        )
    original_quote = (
        "status"
        if correction_kind == "duplicate_quote"
        else "confirmation ID"
        if correction_kind in {"adjacent_datum", "bare_shared_token"}
        else "return the confirmation number"
    )
    unanchored["completion_criteria"][0]["request_slot_source_id"] = "u8"
    unanchored["completion_criteria"][0]["request_slot_source_quote"] = original_quote
    corrected_item = {
        **unanchored["completion_criteria"][0],
        "request_slot_source_id": "u9" if correction_kind == "unknown_source" else "u0",
        "request_slot_source_quote": (
            "status"
            if correction_kind == "duplicate_quote"
            else "account number"
            if correction_kind == "adjacent_datum"
            else "number"
            if correction_kind == "bare_shared_token"
            else original_quote
        ),
    }
    if correction_kind == "semantic_drift":
        corrected_item["outcome"] = "The workflow returns a guessed confirmation."
    corrected = {**unanchored, "completion_criteria": [corrected_item]}

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        return unanchored if len(calls) == 1 else corrected

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", "workflow-copilot-request-policy"]
    criterion = policy.completion_criteria[0]
    assert criterion.output_path is None
    assert criterion.expected_output_shape is None
    assert criterion.mint_disposition == "degraded"
    assert criterion.mint_degrade == "undecidable_judgment"
    assert policy.request_slot_failure_kind == "invalid_anchor_correction"


@pytest.mark.asyncio
async def test_anchor_correction_still_requires_slot_producer_agreement() -> None:
    calls: list[str] = []
    unanchored = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation number.",
                "output_path": "output.confirmation_number",
                "expected_output_shape": "status_label",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "return the confirmation number",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **unanchored["completion_criteria"][0],
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "return the confirmation number",
            }
        ]
    }

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return {"invalid": "request-slot payload"}
        return unanchored if calls.count("workflow-copilot-request-policy") == 1 else corrected

    policy = await _classify_request(CONFIRMATION_NUMBER_REQUEST, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        "workflow-copilot-request-policy",
        *([REQUEST_SLOT_PROMPT_NAME] * 4),
    ]
    assert policy.request_slot_failure_kind == "invalid_output"
    assert policy.completion_criteria[0].mint_disposition == "degraded"
    assert policy.completion_criteria[0].mint_degrade == "undecidable_judgment"


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
