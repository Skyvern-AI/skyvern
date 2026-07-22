from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
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
    _anchor_correction_rejection_capture,
    _apply_request_slot_datum_bindings,
    _classification_from_raw,
    _classify_request,
    schema_output_path_aliases_from_criteria,
)
from skyvern.forge.sdk.copilot.request_slots import PROMPT_NAME as REQUEST_SLOT_PROMPT_NAME
from skyvern.forge.sdk.copilot.request_slots import (
    RequestSlotAntecedentFamily,
    RequestSlotDatumBindingDeclarationV1,
    RequestSlotDatumDeclineDeclarationV1,
    RequestSlotDatumTargetV1,
    RequestSlotDeclarationV1,
    RequestSlotEnvelopeV1,
    RequestSlotPinability,
    RequestSlotPlane,
    RequestSlotProducerInputV1,
    canonicalize_request_slots,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
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
P8_QUICKCONNECT_REQUEST = (
    "I need a reusable workflow that creates or verifies a gas QuickConnect request in the mock Peach State Gas "
    "GasConnect Hub at http://localhost:8906/utility_services/peach_gas/. Sign in with the saved credential named "
    "mock-portal-login. The service address parts, desired start date, and business name should be reusable inputs; "
    "for this run use 77 Gaslight Way, Decatur, GA 30030, 2026-06-24, and Example Realty Labs Inc. After sign-in, "
    "the workflow should open the QuickConnect create flow, use only the sourced address/date/business data, submit "
    "only when the exact address is visible, then read the My QuickConnects table and output the request id, "
    "provider-captured address, requested date, status, and whether the request was newly submitted or already present."
)
P8_QUICKCONNECT_REQUEST_SHA256 = "87440c6781124905ac58113b2fdc4629415ef883e69fdc479fc23ba380d3e353"
P8_QUICKCONNECT_PRIOR_TURN = (
    "The prior QuickConnect result included the request id, provider-captured address, requested date, and status."
)
P8_QUICKCONNECT_WORKFLOW_CONTEXT = (
    "Existing workflow context also mentions request id, provider-captured address, requested date, and status."
)
P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "copilot" / "request_policy" / "sky_12671_p8_noop_anchor_correction_pair.json"
)
P8_LIVE_ORIGINAL_PAYLOAD_FIXTURE = (
    Path(__file__).parent / "fixtures" / "copilot" / "request_policy" / "sky_12671_p8_original_payload.json"
)
P8_CLEAN_RETRY_CUSTODY_GAP_FIXTURE = (
    Path(__file__).parent / "fixtures" / "copilot" / "request_policy" / "sky_12671_p8_clean_retry_custody_gap.json"
)


def _recorded_p8_original_payload_bytes() -> bytes:
    # The fixture stores backend.log:1316's original_payload_json verbatim; the
    # repository text-file newline is not part of the recorded JSON field.
    return P8_LIVE_ORIGINAL_PAYLOAD_FIXTURE.read_bytes().removesuffix(b"\n")


def test_clean_retry_custody_gap_is_explicit_and_never_substituted_with_a_synthetic_replay() -> None:
    custody = json.loads(P8_CLEAN_RETRY_CUSTODY_GAP_FIXTURE.read_text())

    assert custody["workflow_permanent_id"] == "wpid_553704674845352182"
    assert custody["original_payload_sha256"] == ("9f3a7ee247f6c15f677c68402930a7a5c3d23f334b5719442198127724c00cab")
    assert custody["predicate"] == "target_coverage_mismatch"
    assert custody["raw_original_payload_retained"] is False
    assert custody["raw_correction_payload_retained"] is False
    assert custody["custody_status"] == "exact_replay_unavailable"


def _request_slot_envelope_with_target_bindings(
    prompt: str,
    envelope: RequestSlotEnvelopeV1,
    *,
    anchors_by_index: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    marker = "Datum targets from the immutable request-policy payload:\n```"
    assert marker in prompt
    targets = json.loads(prompt.split(marker, 1)[1].split("```", 1)[0])
    payload = envelope.model_dump(mode="json")
    payload["datum_bindings"] = [
        {
            "criterion_index": target["criterion_index"],
            "datum_field": target["datum_field"],
            "declined": False,
            "source_id": anchors_by_index[target["criterion_index"]][0],
            "source_quote": anchors_by_index[target["criterion_index"]][1],
        }
        for target in targets
    ]
    return payload


def _p8_quickconnect_classifier_fixture(*, source_id: str) -> dict[str, Any]:
    # Only P8_QUICKCONNECT_REQUEST is copied from the admitted backend logs. The
    # classifier packet was not retained, so this response is deliberately fixture-labeled.
    return {
        "testing_intent": "require_test",
        "credential_input_kind": "credential_name",
        "credential_refs": ["mock-portal-login"],
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "A matching QuickConnect request is created or verified.",
                "kind": "terminal_action",
                "terminal_action_family": "request",
                "request_slot_source_id": source_id,
                "request_slot_source_quote": "creates or verifies a gas QuickConnect request",
            },
            {
                "outcome": "Only the sourced address, date, and business data are used.",
                "request_slot_source_id": source_id,
                "request_slot_source_quote": "use only the sourced address/date/business data",
            },
            {
                "outcome": "Submission occurs only when the exact address is visible.",
                "request_slot_source_id": source_id,
                "request_slot_source_quote": "submit only when the exact address is visible",
            },
            *[
                {
                    "outcome": outcome,
                    "output_path": output_path,
                    "expected_output_shape": "status_label",
                    "request_slot_source_id": source_id,
                    "request_slot_source_quote": source_quote,
                }
                for outcome, output_path, source_quote in (
                    ("The request id is returned.", "output.request_id", "request id"),
                    (
                        "The provider-captured address is returned.",
                        "output.provider_captured_address",
                        "provider-captured address",
                    ),
                    ("The requested date is returned.", "output.requested_date", "requested date"),
                    ("The request status is returned.", "output.status", "status"),
                )
            ],
        ],
    }


def _p8_quickconnect_envelope(*, source_id: str = "u0") -> RequestSlotEnvelopeV1:
    return RequestSlotEnvelopeV1(
        version="1",
        slots=tuple(
            RequestSlotDeclarationV1(
                source_id=source_id,
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            )
            for source_quote in ("request id", "provider-captured address", "requested date", "status")
        ),
    )


def _p8_payload_with_typed_state_binding(
    payload: dict[str, Any], *, binding_overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    enriched = json.loads(json.dumps(payload))
    criterion_index = 6
    criterion = enriched["completion_criteria"][criterion_index]
    criterion["request_slot_datum_binding"] = {
        "version": "1",
        "criterion_index": criterion_index,
        "datum_field": "output_path",
        "datum_value": "output.request_submission_state",
        "source_id": "u0",
        "source_quote": "whether the request was newly submitted or already present",
        **(binding_overrides or {}),
    }
    return enriched


def _request_slot_input(latest_request: str) -> RequestSlotProducerInputV1:
    return RequestSlotProducerInputV1(
        version="1",
        latest_request=latest_request,
        workflow_context="",
        earliest_user_turn="",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
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
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
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


@pytest.mark.asyncio
async def test_independent_request_slot_producer_recovers_primary_classifier_omission() -> None:
    source_quote = "email/manual-service path"
    primary = {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [],
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.BLOCKER,
            ),
        ),
    )
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        return primary

    policy = await _classify_request(EXAMPLE_WATER_SERVICE_REQUEST, "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", REQUEST_SLOT_PROMPT_NAME, REQUEST_SLOT_PROMPT_NAME]
    assert len(policy.completion_criteria) == 1
    criterion = policy.completion_criteria[0]
    assert criterion.outcome == source_quote
    assert criterion.antecedent_family == "blocker"
    assert criterion.request_slot_id is not None


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
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
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
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="the visible path label",
                plane=RequestSlotPlane.DEFINITION,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="whether the path is login-only",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.UNPINNABLE,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
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
    assert [criterion.antecedent_family for criterion in policy.completion_criteria] == [
        "unconditional",
        "unconditional",
        "unconditional",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_text", "source_quote", "antecedent_family"),
    [
        (
            "Submit online. If the site only exposes a manual path, report that as the blocker.",
            "report that as the blocker",
            RequestSlotAntecedentFamily.BLOCKER,
        ),
        (
            "Always return the blocker field as part of the audit record.",
            "blocker field",
            RequestSlotAntecedentFamily.UNCONDITIONAL,
        ),
    ],
)
async def test_independent_producer_binds_family_when_primary_classifier_emits_zero_contingent_signal(
    request_text: str,
    source_quote: str,
    antecedent_family: RequestSlotAntecedentFamily,
) -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return {
                "version": "1",
                "slots": [
                    {
                        "source_id": "u0",
                        "source_quote": source_quote,
                        "plane": "run",
                        "pinability": "shapeless_valid",
                        "antecedent_family": antecedent_family.value,
                    }
                ],
            }
        return {
            "testing_intent": "require_test",
            "credential_input_kind": "none",
            "requires_user_clarification": False,
            "completion_criteria": [
                {
                    "outcome": "The blocker is reported.",
                    "output_path": "output.blocker",
                    "request_slot_source_id": "u0",
                    "request_slot_source_quote": source_quote,
                }
            ],
        }

    policy = await _classify_request(request_text, "", [], "", handler)

    criterion = policy.completion_criteria[0]
    trace = policy.to_trace_data()
    assert criterion.contingent_on is None
    assert criterion.contingent_antecedent_output_path is None
    assert criterion.mint_degrade is None
    assert criterion.antecedent_family == antecedent_family.value
    assert trace["antecedent_family_criterion_0_id"] == criterion.id
    assert trace["antecedent_family_criterion_0_antecedent_family"] == antecedent_family.value
    assert calls.count("workflow-copilot-request-policy") == 1
    assert calls.count(REQUEST_SLOT_PROMPT_NAME) == 2


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
    assert all(criterion.antecedent_family == "undecidable" for criterion in policy.completion_criteria)


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
async def test_classifier_payload_without_request_slots_requires_agreed_empty_producer_contract() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return RequestSlotEnvelopeV1(version="1", slots=()).model_dump(mode="json")
        return {
            "testing_intent": "require_test",
            "credential_input_kind": "none",
            "requires_user_clarification": False,
            "completion_criteria": [{"outcome": "The requested workflow is complete."}],
        }

    await _classify_request("Build the workflow.", "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy", REQUEST_SLOT_PROMPT_NAME, REQUEST_SLOT_PROMPT_NAME]


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

    decision = _accept_request_slot_anchor_correction(
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

    assert decision.predicate == "accepted"
    assert events == []


@pytest.mark.asyncio
async def test_eight_row_anchor_correction_accepts_representation_drift_only() -> None:
    original = _synthetic_eight_row_anchor_payload(valid_anchors=False)
    corrected = _semantically_identical_anchor_correction()
    decision = _accept_request_slot_anchor_correction(
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

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    accepted = decision.accepted_payload
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


@pytest.mark.parametrize("recorded_run", ["run1", "run2", "run3"])
def test_p8_custody_triplet_source_bytes_accepts_fixture_labeled_anchor_correction(recorded_run: str) -> None:
    assert recorded_run in {"run1", "run2", "run3"}
    assert hashlib.sha256(P8_QUICKCONNECT_REQUEST.encode()).hexdigest() == P8_QUICKCONNECT_REQUEST_SHA256
    original = _p8_quickconnect_classifier_fixture(source_id="u9")
    corrected = _p8_quickconnect_classifier_fixture(source_id="u0")

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request=P8_QUICKCONNECT_REQUEST,
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    accepted = decision.accepted_payload
    assert [item["outcome"] for item in accepted["completion_criteria"]] == [
        item["outcome"] for item in original["completion_criteria"]
    ]
    assert [item.get("request_slot_source_quote") for item in accepted["completion_criteria"]] == [
        item.get("request_slot_source_quote") for item in original["completion_criteria"]
    ]
    assert [item.get("request_slot_source_id") for item in accepted["completion_criteria"]] == ["u0"] * 7


@pytest.mark.asyncio
async def test_p8_admitted_source_fixture_mints_ordered_decidable_canonical_slots() -> None:
    assert hashlib.sha256(P8_QUICKCONNECT_REQUEST.encode()).hexdigest() == P8_QUICKCONNECT_REQUEST_SHA256
    original = _p8_quickconnect_classifier_fixture(source_id="u9")
    envelope = _p8_quickconnect_envelope(source_id="u1")
    calls: list[str] = []
    request_slot_envelopes: list[RequestSlotEnvelopeV1] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            response = _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={
                    3: ("u1", "request id"),
                    4: ("u1", "provider-captured address"),
                    5: ("u1", "requested date"),
                    6: ("u1", "status"),
                },
            )
            request_slot_envelopes.append(RequestSlotEnvelopeV1.model_validate_json(json.dumps(response)))
            return response
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("two-pass producer consensus must not be echoed through a third model call")
        if "TERMINAL ACTION RECONCILIATION MODE" in prompt:
            return {"version": "1", "criterion_id": "c0", "terminal_action_family": "request"}
        return original

    policy = await _classify_request(
        P8_QUICKCONNECT_REQUEST,
        P8_QUICKCONNECT_WORKFLOW_CONTEXT,
        [
            WorkflowCopilotChatHistoryMessage(
                sender=WorkflowCopilotChatSender.USER,
                content=P8_QUICKCONNECT_PRIOR_TURN,
                created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
        ],
        "",
        handler,
    )

    assert calls == [
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
        "workflow-copilot-request-policy",
    ]
    assert policy.request_slot_failure_kind is None
    assert [criterion.outcome for criterion in policy.completion_criteria] == [
        "The request id is returned.",
        "The provider-captured address is returned.",
        "The requested date is returned.",
        "The request status is returned.",
        "A matching QuickConnect request is created or verified.",
        "Only the sourced address, date, and business data are used.",
        "Submission occurs only when the exact address is visible.",
    ]
    expected_request = RequestSlotProducerInputV1(
        version="1",
        latest_request=P8_QUICKCONNECT_REQUEST,
        workflow_context=P8_QUICKCONNECT_WORKFLOW_CONTEXT,
        earliest_user_turn=P8_QUICKCONNECT_PRIOR_TURN,
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
        datum_targets=tuple(
            RequestSlotDatumTargetV1(
                criterion_index=index,
                datum_field="output_path",
                datum_value=original["completion_criteria"][index]["output_path"],
                criterion_outcome_sha256=hashlib.sha256(
                    original["completion_criteria"][index]["outcome"].encode()
                ).hexdigest(),
            )
            for index in range(3, 7)
        ),
    )
    assert [criterion.request_slot_id for criterion in policy.completion_criteria[:4]] == [
        slot.slot_id
        for slot in canonicalize_request_slots(
            request=expected_request,
            envelope=request_slot_envelopes[0],
        ).slots
    ]
    assert all(criterion.mint_disposition == "decidable" for criterion in policy.completion_criteria[:4])
    assert all(criterion.mint_degrade == "undecidable_judgment" for criterion in policy.completion_criteria[4:])
    assert sum(criterion.kind == "terminal_action" for criterion in policy.completion_criteria) == 1
    trace_data = policy.to_trace_data()
    assert "request_slot_failure_kind" not in trace_data
    assert trace_data["mint_degraded_criterion_count"] == 3


def test_live_p8_noop_pair_rejects_unbound_and_accepts_typed_datum_binding() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    original_payload_bytes = _recorded_p8_original_payload_bytes()
    assert hashlib.sha256(original_payload_bytes).hexdigest() == packet["source"]["original_sha256"]
    recorded_original = json.loads(original_payload_bytes)
    assert recorded_original == packet["original"]
    capture = _anchor_correction_rejection_capture(recorded_original, packet["corrected"])

    assert capture is not None
    assert capture["original_sha256"] == packet["source"]["original_sha256"]
    assert capture["corrected_sha256"] == packet["source"]["corrected_sha256"]
    assert capture["pair_sha256"] == packet["source"]["pair_sha256"]
    assert capture["pair_sha256"] == "e5be31a1cc861dd40c26a9c4df5d0240e6fabe42ca37bc1c5e2bcedbe56fbac5"

    request_slot_request = RequestSlotProducerInputV1(
        version="1",
        latest_request=packet["latest_request"],
        workflow_context="",
        earliest_user_turn="",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
    )
    legacy = _accept_request_slot_anchor_correction(
        recorded_original,
        packet["corrected"],
        request_slot_request=request_slot_request,
    )
    assert legacy.predicate == packet["source"]["predicate"] == "original_quote_not_admissible"
    assert legacy.criterion_index == packet["source"]["criterion_index"] == 6

    enriched_original = _p8_payload_with_typed_state_binding(packet["original"])
    enriched_corrected = _p8_payload_with_typed_state_binding(packet["corrected"])
    accepted = _accept_request_slot_anchor_correction(
        enriched_original,
        enriched_corrected,
        request_slot_request=request_slot_request,
    )
    assert accepted.predicate == "accepted"
    assert accepted.accepted_payload == enriched_original


def test_p8_producer_consensus_joins_only_the_enumerated_datum() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    request = _request_slot_input(packet["latest_request"])
    outcome_sha256 = hashlib.sha256(packet["original"]["completion_criteria"][6]["outcome"].encode()).hexdigest()
    request = request.model_copy(
        update={
            "datum_targets": (
                RequestSlotDatumTargetV1(
                    criterion_index=6,
                    datum_field="output_path",
                    datum_value="output.request_submission_state",
                    criterion_outcome_sha256=outcome_sha256,
                ),
            )
        }
    )
    contract = canonicalize_request_slots(
        request=request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote="whether the request was newly submitted or already present",
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.SHAPELESS_VALID,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                ),
            ),
            datum_bindings=(
                RequestSlotDatumBindingDeclarationV1(
                    criterion_index=6,
                    datum_field="output_path",
                    declined=False,
                    source_id="u0",
                    source_quote="whether the request was newly submitted or already present",
                ),
            ),
        ),
    )
    decision = _apply_request_slot_datum_bindings(
        packet["original"],
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    assert decision.accepted_payload["completion_criteria"][:6] == packet["original"]["completion_criteria"][:6]
    assert decision.accepted_payload["completion_criteria"][6]["request_slot_datum_binding"] == {
        "version": "1",
        "criterion_index": 6,
        "datum_field": "output_path",
        "datum_value": "output.request_submission_state",
        "criterion_outcome_sha256": outcome_sha256,
        "source_id": "u0",
        "source_quote": "whether the request was newly submitted or already present",
    }
    assert decision.trusted_bindings == (
        request_policy_module.TrustedRequestSlotDatumBindingV1(
            version="1",
            criterion_index=6,
            datum_field="output_path",
            datum_value="output.request_submission_state",
            criterion_outcome_sha256=outcome_sha256,
            source_id="u0",
            source_quote="whether the request was newly submitted or already present",
            slot_id=contract.datum_bindings[0].slot_id,
        ),
    )


@pytest.mark.asyncio
async def test_producer_decline_degrades_junk_without_discarding_legitimate_sibling() -> None:
    sibling_quote = "whether submission was new or existing"
    junk_quote = "account number"
    original = {
        "completion_criteria": [
            {
                "outcome": "Submission state is returned.",
                "output_path": "output.submission_state",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": sibling_quote,
            },
            {
                "outcome": "Confirmation ID is returned.",
                "output_path": "output.confirmation_id",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": junk_quote,
            },
        ]
    }
    request_text = "Return whether submission was new or existing and the adjacent account number."
    slots = tuple(
        RequestSlotDeclarationV1(
            source_id="u0",
            source_quote=quote,
            plane=RequestSlotPlane.RUN,
            pinability=RequestSlotPinability.SHAPELESS_VALID,
            antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
        )
        for quote in dict.fromkeys((sibling_quote, "account number" if junk_quote != sibling_quote else sibling_quote))
    )
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            marker = "Datum targets from the immutable request-policy payload:\n```"
            targets = json.loads(prompt.split(marker, 1)[1].split("```", 1)[0])
            return {
                "version": "1",
                "slots": [slot.model_dump(mode="json") for slot in slots],
                "datum_bindings": [
                    {
                        "criterion_index": targets[0]["criterion_index"],
                        "datum_field": targets[0]["datum_field"],
                        "declined": False,
                        "source_id": "u0",
                        "source_quote": sibling_quote,
                    },
                    {"criterion_index": 1, "datum_field": "output_path", "declined": True},
                ],
            }
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("producer consensus must not be echoed through a correction call")
        return original

    policy = await _classify_request(request_text, "", [], "", handler)

    assert calls.count(REQUEST_SLOT_PROMPT_NAME) == 2
    assert policy.request_slot_failure_kind is None
    by_path = {criterion.floor_rekeyed_from_path: criterion for criterion in policy.completion_criteria}
    assert by_path["output.submission_state"].mint_disposition == "decidable"
    assert by_path["output.submission_state"].request_slot_id is not None
    assert by_path["output.confirmation_id"].mint_disposition == "degraded"
    assert by_path["output.confirmation_id"].mint_degrade == "undecidable_judgment"


@pytest.mark.parametrize("producer_declines", [False, True])
def test_server_applies_consensus_binding_or_local_decline(producer_declines: bool) -> None:
    original = {
        "completion_criteria": [
            {
                "outcome": "Submission state is returned.",
                "output_path": "output.submission_state",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "whether submission was new or existing",
            }
        ]
    }
    target = RequestSlotDatumTargetV1(
        criterion_index=0,
        datum_field="output_path",
        datum_value="output.submission_state",
        criterion_outcome_sha256=hashlib.sha256(b"Submission state is returned.").hexdigest(),
    )
    request = _request_slot_input("Return whether submission was new or existing.").model_copy(
        update={"datum_targets": (target,)}
    )
    producer_resolution: RequestSlotDatumBindingDeclarationV1 | RequestSlotDatumDeclineDeclarationV1
    if producer_declines:
        producer_resolution = RequestSlotDatumDeclineDeclarationV1(
            criterion_index=0,
            datum_field="output_path",
            declined=True,
        )
    else:
        producer_resolution = RequestSlotDatumBindingDeclarationV1(
            criterion_index=0,
            datum_field="output_path",
            declined=False,
            source_id="u0",
            source_quote="whether submission was new or existing",
        )
    contract = canonicalize_request_slots(
        request=request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote="whether submission was new or existing",
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.SHAPELESS_VALID,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                ),
            ),
            datum_bindings=(producer_resolution,),
        ),
    )
    decision = _apply_request_slot_datum_bindings(
        original,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    if producer_declines:
        assert decision.accepted_payload == original
        assert decision.trusted_bindings == ()
    else:
        assert decision.accepted_payload["completion_criteria"][0]["request_slot_source_quote"] == (
            "whether submission was new or existing"
        )
        assert decision.trusted_bindings[0].slot_id == contract.slots[0].slot_id


@pytest.mark.parametrize(
    ("contract_mutation", "producer_declines"),
    [
        ("missing_resolution", False),
        ("duplicate_binding", False),
        ("stale_binding_value", False),
        ("stale_decline_hash", True),
    ],
)
def test_server_rejects_malformed_or_stale_consensus_contracts_without_mutating_payload(
    contract_mutation: str,
    producer_declines: bool,
) -> None:
    original = {
        "completion_criteria": [
            {
                "outcome": "Submission state is returned.",
                "output_path": "output.submission_state",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "whether submission was new or existing",
            }
        ]
    }
    target = RequestSlotDatumTargetV1(
        criterion_index=0,
        datum_field="output_path",
        datum_value="output.submission_state",
        criterion_outcome_sha256=hashlib.sha256(b"Submission state is returned.").hexdigest(),
    )
    request = _request_slot_input("Return whether submission was new or existing.").model_copy(
        update={"datum_targets": (target,)}
    )
    resolution: RequestSlotDatumBindingDeclarationV1 | RequestSlotDatumDeclineDeclarationV1
    if producer_declines:
        resolution = RequestSlotDatumDeclineDeclarationV1(
            criterion_index=0,
            datum_field="output_path",
            declined=True,
        )
    else:
        resolution = RequestSlotDatumBindingDeclarationV1(
            criterion_index=0,
            datum_field="output_path",
            declined=False,
            source_id="u0",
            source_quote="whether submission was new or existing",
        )
    contract = canonicalize_request_slots(
        request=request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote="whether submission was new or existing",
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.SHAPELESS_VALID,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                ),
            ),
            datum_bindings=(resolution,),
        ),
    )

    if contract_mutation == "missing_resolution":
        malformed_contract = contract.model_copy(update={"datum_bindings": (), "datum_declines": ()})
    elif contract_mutation == "duplicate_binding":
        malformed_contract = contract.model_copy(update={"datum_bindings": contract.datum_bindings * 2})
    elif contract_mutation == "stale_binding_value":
        stale_binding = contract.datum_bindings[0].model_copy(update={"datum_value": "output.other_state"})
        malformed_contract = contract.model_copy(update={"datum_bindings": (stale_binding,)})
    else:
        stale_decline = contract.datum_declines[0].model_copy(update={"criterion_outcome_sha256": "0" * 64})
        malformed_contract = contract.model_copy(update={"datum_declines": (stale_decline,)})

    decision = _apply_request_slot_datum_bindings(
        original,
        request_slot_request=request,
        request_slot_contract=malformed_contract,
    )

    assert decision.predicate == "invalid_contract"
    assert decision.accepted_payload is None
    assert decision.trusted_bindings == ()
    assert original["completion_criteria"][0]["request_slot_source_quote"] == ("whether submission was new or existing")
    assert "request_slot_datum_binding" not in original["completion_criteria"][0]


def test_server_rejects_binding_contract_minted_for_different_request() -> None:
    original = {
        "completion_criteria": [
            {
                "outcome": "Submission state is returned.",
                "output_path": "output.submission_state",
            }
        ]
    }
    target = RequestSlotDatumTargetV1(
        criterion_index=0,
        datum_field="output_path",
        datum_value="output.submission_state",
        criterion_outcome_sha256=hashlib.sha256(b"Submission state is returned.").hexdigest(),
    )
    current_request = _request_slot_input("Return whether submission was new or existing.").model_copy(
        update={"datum_targets": (target,)}
    )
    foreign_request = _request_slot_input(
        "Return whether submission was new or existing. Do not submit a replacement."
    ).model_copy(update={"datum_targets": (target,)})
    foreign_contract = canonicalize_request_slots(
        request=foreign_request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote="whether submission was new or existing",
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.SHAPELESS_VALID,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                ),
            ),
            datum_bindings=(
                RequestSlotDatumBindingDeclarationV1(
                    criterion_index=0,
                    datum_field="output_path",
                    declined=False,
                    source_id="u0",
                    source_quote="whether submission was new or existing",
                ),
            ),
        ),
    )

    decision = _apply_request_slot_datum_bindings(
        original,
        request_slot_request=current_request,
        request_slot_contract=foreign_contract,
    )

    assert decision.predicate == "invalid_contract"
    assert decision.accepted_payload is None
    assert "request_slot_datum_binding" not in original["completion_criteria"][0]


def test_trusted_binding_rejects_non_string_outcome_without_coercion() -> None:
    item = {
        "outcome": 123,
        "output_path": "output.submission_state",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": "submission state",
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "output_path",
            "datum_value": "output.submission_state",
            "criterion_outcome_sha256": hashlib.sha256(b"123").hexdigest(),
            "source_id": "u0",
            "source_quote": "submission state",
        },
    }
    request = _request_slot_input("Return the submission state.")
    binding = request_policy_module.TrustedRequestSlotDatumBindingV1(
        version="1",
        criterion_index=0,
        datum_field="output_path",
        datum_value="output.submission_state",
        criterion_outcome_sha256=hashlib.sha256(b"123").hexdigest(),
        source_id="u0",
        source_quote="submission state",
        slot_id="slot-submission-state",
    )

    assert not request_policy_module._trusted_request_slot_datum_binding_is_valid(
        item,
        binding=binding,
        criterion_index=0,
        request_slot_request=request,
    )


@pytest.mark.asyncio
async def test_datum_target_index_overflow_degrades_through_typed_failure_path() -> None:
    raw = {
        "completion_criteria": [
            {
                "outcome": f"Datum {index} is returned.",
                "output_path": f"output.datum_{index}",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "all requested data",
            }
            for index in range(65)
        ]
    }
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        del prompt
        calls.append(prompt_name)
        return raw

    policy = await _classify_request("Return all requested data.", "", [], "", handler)

    assert calls == ["workflow-copilot-request-policy"]
    assert policy.request_slot_failure_kind == "invalid_output"
    assert policy.completion_criteria
    assert all(criterion.mint_disposition == "degraded" for criterion in policy.completion_criteria)


@pytest.mark.parametrize(
    "binding_overrides",
    [
        {"version": "2"},
        {"criterion_index": 5},
        {"datum_field": "outcome"},
        {"datum_value": "output.other"},
        {"source_id": "u1"},
        {"source_quote": "status"},
        {"unknown": "value"},
    ],
)
def test_live_p8_invalid_typed_binding_preserves_existing_rejection(binding_overrides: dict[str, Any]) -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    original = _p8_payload_with_typed_state_binding(packet["original"], binding_overrides=binding_overrides)
    corrected = _p8_payload_with_typed_state_binding(packet["corrected"], binding_overrides=binding_overrides)

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request=packet["latest_request"],
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert decision.predicate == "original_quote_not_admissible"
    assert decision.criterion_index == 6


@pytest.mark.asyncio
async def test_live_p8_typed_binding_mints_state_criterion_through_production_path() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    original_payload_bytes = _recorded_p8_original_payload_bytes()
    assert hashlib.sha256(original_payload_bytes).hexdigest() == packet["source"]["original_sha256"]
    raw = json.loads(original_payload_bytes)
    assert raw == packet["original"]
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=tuple(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            )
            for source_quote in (
                "output the request id",
                "provider-captured address",
                "requested date",
                "status",
                "whether the request was newly submitted or already present",
            )
        ),
    )
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={
                    6: ("u0", "whether the request was newly submitted or already present"),
                },
            )
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("two-pass producer consensus must not be echoed through a third model call")
        if "TERMINAL ACTION RECONCILIATION MODE" in prompt:
            return {"version": "1", "criterion_id": None, "terminal_action_family": None}
        return raw

    policy = await _classify_request(packet["latest_request"], "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
        "workflow-copilot-request-policy",
    ]
    assert policy.request_slot_failure_kind is None
    state = next(
        criterion
        for criterion in policy.completion_criteria
        if criterion.floor_rekeyed_from_path == "output.request_submission_state"
    )
    assert state.request_slot_id is not None
    assert state.mint_disposition == "decidable"
    assert state.mint_degrade is None


@pytest.mark.asyncio
async def test_primary_self_asserted_binding_is_replaced_by_two_pass_producer_consensus() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    raw = _p8_payload_with_typed_state_binding(packet["original"])
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="whether the request was newly submitted or already present",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={
                    6: ("u0", "whether the request was newly submitted or already present"),
                },
            )
        return raw

    policy = await _classify_request(packet["latest_request"], "", [], "", handler)

    state = next(
        criterion
        for criterion in policy.completion_criteria
        if criterion.floor_rekeyed_from_path == "output.request_submission_state"
    )
    assert policy.request_slot_failure_kind is None
    assert state.request_slot_id is not None
    assert state.mint_disposition == "decidable"


@pytest.mark.parametrize("binding", [None, {"version": "2"}])
def test_invalid_typed_binding_uses_unchanged_legacy_admissibility(binding: Any) -> None:
    criterion = {
        "outcome": "The request id is returned.",
        "output_path": "output.request_id",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": "request id",
        "request_slot_datum_binding": binding,
    }
    payload = {"completion_criteria": [criterion]}

    decision = _accept_request_slot_anchor_correction(
        payload,
        payload,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request="Return the request id.",
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert decision.predicate == "accepted"


def test_typed_classification_binding_accepts_nonlexical_datum_name() -> None:
    criterion = {
        "outcome": "Whether the request was approved or denied is returned.",
        "kind": "validation_classification",
        "classification_output_key": "decision_state",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": "whether the request was approved or denied",
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "classification_output_key",
            "datum_value": "decision_state",
            "source_id": "u0",
            "source_quote": "whether the request was approved or denied",
        },
    }
    payload = {"completion_criteria": [criterion]}

    decision = _accept_request_slot_anchor_correction(
        payload,
        payload,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request="Return whether the request was approved or denied.",
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert decision.predicate == "accepted"


def test_anchor_correction_rejects_typed_binding_drift() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    original = _p8_payload_with_typed_state_binding(packet["original"])
    corrected = _p8_payload_with_typed_state_binding(
        packet["corrected"],
        binding_overrides={"datum_value": "output.other"},
    )

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=RequestSlotProducerInputV1(
            version="1",
            latest_request=packet["latest_request"],
            workflow_context="",
            earliest_user_turn="",
            latest_prior_user_turn="",
            latest_assistant_turn="",
            retained_history=(),
            global_context="",
        ),
    )

    assert decision.predicate == "criterion_semantics_changed"
    assert decision.criterion_index == 6


@pytest.mark.parametrize(
    ("latest_request", "source_id"),
    [
        ("Return whether the request was approved or denied.", "u9"),
        (
            "Return whether the request was approved or denied and repeat whether the request was approved or denied.",
            "u0",
        ),
    ],
)
def test_typed_binding_requires_unique_span_in_nominated_source(latest_request: str, source_id: str) -> None:
    source_quote = "whether the request was approved or denied"
    criterion = {
        "outcome": "Whether the request was approved or denied is returned.",
        "output_path": "output.decision_state",
        "request_slot_source_id": source_id,
        "request_slot_source_quote": source_quote,
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "output_path",
            "datum_value": "output.decision_state",
            "source_id": source_id,
            "source_quote": source_quote,
        },
    }
    payload = {"completion_criteria": [criterion]}

    decision = _accept_request_slot_anchor_correction(
        payload,
        payload,
        request_slot_request=_request_slot_input(latest_request),
    )

    assert decision.predicate == "original_quote_not_admissible"
    assert decision.criterion_index == 0


@pytest.mark.parametrize("binding", [[], {"version": "1"}])
@pytest.mark.asyncio
async def test_malformed_binding_uses_legacy_path_through_classifier(binding: Any) -> None:
    source_quote = "request id"
    raw = {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "The request id is returned.",
                "output_path": "output.request_id",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": source_quote,
                "request_slot_datum_binding": binding,
            }
        ],
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        if "REQUEST SLOT ANCHOR CORRECTION" in prompt:
            pytest.fail("malformed binding must not disable legacy admissibility")
        return raw

    policy = await _classify_request("Return the request id.", "", [], "", handler)

    criterion = policy.completion_criteria[0]
    assert criterion.request_slot_id is not None
    assert criterion.mint_disposition == "decidable"
    assert criterion.mint_degrade is None


@pytest.mark.asyncio
async def test_typed_classification_binding_mints_through_independent_producer() -> None:
    source_quote = "whether the request was approved or denied"
    raw = {
        "testing_intent": "require_test",
        "credential_input_kind": "none",
        "requires_user_clarification": False,
        "completion_criteria": [
            {
                "outcome": "Whether the request was approved or denied is returned.",
                "kind": "validation_classification",
                "classification_output_key": "decision_state",
                "expected_classification": "approved",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": source_quote,
                "request_slot_datum_binding": {
                    "version": "1",
                    "criterion_index": 0,
                    "datum_field": "classification_output_key",
                    "datum_value": "decision_state",
                    "source_id": "u0",
                    "source_quote": source_quote,
                },
            }
        ],
    }
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.PINNED,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={0: ("u0", source_quote)},
            )
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("producer consensus must not be echoed through a correction call")
        return raw

    policy = await _classify_request(
        "Return whether the request was approved or denied.",
        "",
        [],
        "",
        handler,
    )

    criterion = policy.completion_criteria[0]
    assert criterion.request_slot_id is not None
    assert criterion.classification_output_key == "decision_state"
    assert criterion.expected_classification == "approved"
    assert criterion.mint_disposition == "decidable"


@pytest.mark.parametrize("corrected_binding", [None, {}])
def test_anchor_correction_rejects_removed_or_malformed_binding_on_one_side(corrected_binding: Any) -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    original = _p8_payload_with_typed_state_binding(packet["original"])
    corrected = _p8_payload_with_typed_state_binding(packet["corrected"])
    corrected_criterion = corrected["completion_criteria"][6]
    if corrected_binding is None:
        corrected_criterion.pop("request_slot_datum_binding")
    else:
        corrected_criterion["request_slot_datum_binding"] = corrected_binding

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=_request_slot_input(packet["latest_request"]),
    )

    assert decision.predicate == "criterion_semantics_changed"
    assert decision.criterion_index == 6


@pytest.mark.parametrize("mutated_index", [False, 0.0])
def test_anchor_correction_compares_binding_values_type_strictly(mutated_index: Any) -> None:
    source_quote = "whether the request was approved or denied"
    criterion = {
        "outcome": "Whether the request was approved or denied is returned.",
        "output_path": "output.decision_state",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": source_quote,
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "output_path",
            "datum_value": "output.decision_state",
            "source_id": "u0",
            "source_quote": source_quote,
        },
    }
    original = {"completion_criteria": [criterion]}
    corrected = json.loads(json.dumps(original))
    corrected["completion_criteria"][0]["request_slot_datum_binding"]["criterion_index"] = mutated_index

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=_request_slot_input(f"Return {source_quote}."),
    )

    assert decision.predicate == "criterion_semantics_changed"
    assert decision.criterion_index == 0


def test_validation_classification_binding_cannot_certify_inactive_output_path() -> None:
    source_quote = "whether the request was approved or denied"
    criterion = {
        "outcome": "Whether the request was approved or denied is returned.",
        "kind": "validation_classification",
        "output_path": "output.zzz",
        "classification_output_key": "unrelated_key",
        "expected_classification": "approved",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": source_quote,
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "output_path",
            "datum_value": "output.zzz",
            "source_id": "u0",
            "source_quote": source_quote,
        },
    }
    payload = {"completion_criteria": [criterion]}

    decision = _accept_request_slot_anchor_correction(
        payload,
        payload,
        request_slot_request=_request_slot_input(f"Return {source_quote}."),
    )

    assert decision.predicate == "original_quote_not_admissible"
    assert decision.criterion_index == 0


def test_typed_binding_cannot_certify_classification_key_rejected_by_parser() -> None:
    source_quote = "whether the request was approved or denied"
    criterion = {
        "outcome": "Whether the request was approved or denied is returned.",
        "kind": "validation_classification",
        "classification_output_key": "bad.key",
        "expected_classification": "approved",
        "request_slot_source_id": "u0",
        "request_slot_source_quote": source_quote,
        "request_slot_datum_binding": {
            "version": "1",
            "criterion_index": 0,
            "datum_field": "classification_output_key",
            "datum_value": "bad.key",
            "source_id": "u0",
            "source_quote": source_quote,
        },
    }
    payload = {"completion_criteria": [criterion]}

    request = _request_slot_input(f"Return {source_quote}.")
    decision = _accept_request_slot_anchor_correction(
        payload,
        payload,
        request_slot_request=request,
    )

    assert decision.predicate == "original_quote_not_admissible"
    assert decision.criterion_index == 0

    contract = canonicalize_request_slots(
        request=request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote=source_quote,
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.PINNED,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                ),
            ),
        ),
    )
    policy = _classification_from_raw(
        payload,
        request_slot_request=request,
        request_slot_contract=contract,
    )
    parsed = next(criterion for criterion in policy.completion_criteria if criterion.request_slot_id is None)
    assert parsed.request_slot_id is None
    assert parsed.classification_output_key is None
    assert parsed.mint_disposition == "degraded"
    assert parsed.mint_degrade == "undecidable_judgment"


@pytest.mark.asyncio
async def test_valid_typed_binding_degrades_when_independent_producer_disagrees() -> None:
    packet = json.loads(P8_LIVE_NOOP_ANCHOR_CORRECTION_FIXTURE.read_text())
    raw = _p8_payload_with_typed_state_binding(packet["original"])
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=tuple(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote=source_quote,
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            )
            for source_quote in ("output the request id", "provider-captured address", "requested date", "status")
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return envelope.model_dump(mode="json")
        if "REQUEST SLOT ANCHOR CORRECTION" in prompt:
            pytest.fail("valid typed binding must not enter correction")
        if "TERMINAL ACTION RECONCILIATION MODE" in prompt:
            return {"version": "1", "criterion_id": None, "terminal_action_family": None}
        return raw

    policy = await _classify_request(packet["latest_request"], "", [], "", handler)

    state = next(
        criterion
        for criterion in policy.completion_criteria
        if criterion.floor_rekeyed_from_path == "output.request_submission_state"
    )
    assert state.request_slot_id is None
    assert state.mint_disposition == "degraded"
    assert state.mint_degrade == "undecidable_judgment"


def test_anchor_correction_accepts_quote_unique_in_nominated_source_with_cross_source_overlap() -> None:
    request = RequestSlotProducerInputV1(
        version="1",
        latest_request="Return the request id for the current QuickConnect.",
        workflow_context="",
        earliest_user_turn="Return the request id from the prior QuickConnect.",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
    )
    original = {
        "completion_criteria": [
            {
                "outcome": "The request id is returned.",
                "output_path": "output.request_id",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "request id",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **original["completion_criteria"][0],
                "request_slot_source_id": "u1",
            }
        ]
    }

    decision = _accept_request_slot_anchor_correction(
        original,
        corrected,
        request_slot_request=request,
    )

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    accepted = decision.accepted_payload
    assert accepted["completion_criteria"][0]["request_slot_source_id"] == "u1"


def test_anchor_correction_rejection_names_exact_failed_predicate() -> None:
    request = RequestSlotProducerInputV1(
        version="1",
        latest_request="Return the request id.",
        workflow_context="",
        earliest_user_turn="",
        latest_prior_user_turn="",
        latest_assistant_turn="",
        retained_history=(),
        global_context="",
    )
    original = {
        "completion_criteria": [
            {
                "outcome": "The request id is returned.",
                "output_path": "output.request_id",
                "request_slot_source_id": "u9",
                "request_slot_source_quote": "request id",
            }
        ]
    }
    corrected = {
        "completion_criteria": [
            {
                **original["completion_criteria"][0],
                "request_slot_source_id": "u1",
                "request_slot_source_quote": "request",
            }
        ]
    }

    decision = _accept_request_slot_anchor_correction(original, corrected, request_slot_request=request)

    assert decision.predicate == "original_quote_not_admissible"
    assert decision.criterion_index == 0
    assert decision.accepted_payload is None


def test_anchor_correction_rejection_capture_is_redacted_and_content_addressed() -> None:
    original = {
        "password": "unredacted-password",
        "raw_secret_evidence": "bare-credential-value-1234567890",
        "completion_criteria": [
            {
                "outcome": "Return the request id after password=another-secret",
                "request_slot_source_id": "u9",
            }
        ],
    }
    corrected = {
        "api_key": "sk-abcdefghijklmnop",
        "completion_criteria": [
            {
                "outcome": "Return the request id after password=another-secret",
                "request_slot_source_id": "u0",
            }
        ],
    }

    capture = _anchor_correction_rejection_capture(original, corrected)

    assert capture is not None
    serialized_capture = json.dumps(capture, sort_keys=True)
    assert "unredacted-password" not in serialized_capture
    assert "bare-credential-value-1234567890" not in serialized_capture
    assert "another-secret" not in serialized_capture
    assert "sk-abcdefghijklmnop" not in serialized_capture
    assert "****" in capture["original_payload_json"]
    assert json.loads(capture["original_payload_json"])["raw_secret_evidence"] == "[REDACTED_SECRET]"
    assert "[REDACTED_SECRET]" in capture["corrected_payload_json"]
    assert hashlib.sha256(capture["original_payload_json"].encode()).hexdigest() == capture["original_sha256"]
    assert hashlib.sha256(capture["corrected_payload_json"].encode()).hexdigest() == capture["corrected_sha256"]
    pair_json = json.dumps(
        {
            "corrected": json.loads(capture["corrected_payload_json"]),
            "original": json.loads(capture["original_payload_json"]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert hashlib.sha256(pair_json.encode()).hexdigest() == capture["pair_sha256"]


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
async def test_unanchored_requested_output_uses_producer_consensus_and_mints_decidable() -> None:
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
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="confirmation code",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={0: ("u0", "confirmation code")},
            )
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("producer consensus must not be echoed through a correction call")
        return unanchored

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == [
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
async def test_source_valid_wrong_primary_anchor_is_replaced_by_producer_consensus() -> None:
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
                source_quote="confirmation ID",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="account number",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={0: ("u0", "confirmation ID")},
            )
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("producer consensus must not be echoed through a correction call")
        return wrong_datum

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind is None
    assert criterion.request_slot_id is not None
    assert criterion.mint_disposition == "decidable"
    assert criterion.mint_degrade is None


def test_producer_consensus_replaces_primary_anchor_with_agreed_datum_slot() -> None:
    latest_request = "Return the confirmation ID and the adjacent account number."
    original = {
        "completion_criteria": [
            {
                "outcome": "The workflow returns the confirmation ID beside the account number.",
                "output_path": "output.confirmation_id",
                "request_slot_source_id": "u0",
                "request_slot_source_quote": "account number",
            }
        ]
    }
    outcome_sha256 = hashlib.sha256(original["completion_criteria"][0]["outcome"].encode()).hexdigest()
    request = _request_slot_input(latest_request).model_copy(
        update={
            "datum_targets": (
                RequestSlotDatumTargetV1(
                    criterion_index=0,
                    datum_field="output_path",
                    datum_value="output.confirmation_id",
                    criterion_outcome_sha256=outcome_sha256,
                ),
            )
        }
    )
    contract = canonicalize_request_slots(
        request=request,
        envelope=RequestSlotEnvelopeV1(
            version="1",
            slots=tuple(
                RequestSlotDeclarationV1(
                    source_id="u0",
                    source_quote=quote,
                    plane=RequestSlotPlane.RUN,
                    pinability=RequestSlotPinability.SHAPELESS_VALID,
                    antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
                )
                for quote in ("confirmation ID", "account number")
            ),
            datum_bindings=(
                RequestSlotDatumBindingDeclarationV1(
                    criterion_index=0,
                    datum_field="output_path",
                    declined=False,
                    source_id="u0",
                    source_quote="confirmation ID",
                ),
            ),
        ),
    )
    decision = _apply_request_slot_datum_bindings(
        original,
        request_slot_request=request,
        request_slot_contract=contract,
    )

    assert decision.predicate == "accepted"
    assert decision.accepted_payload is not None
    assert decision.accepted_payload["completion_criteria"][0]["request_slot_source_quote"] == "confirmation ID"
    assert decision.trusted_bindings == (
        request_policy_module.TrustedRequestSlotDatumBindingV1(
            version="1",
            criterion_index=0,
            datum_field="output_path",
            datum_value="output.confirmation_id",
            criterion_outcome_sha256=outcome_sha256,
            source_id="u0",
            source_quote="confirmation ID",
            slot_id=contract.datum_bindings[0].slot_id,
        ),
    )


@pytest.mark.asyncio
async def test_producer_consensus_replaces_invalid_primary_source_identity() -> None:
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
    envelope = RequestSlotEnvelopeV1(
        version="1",
        slots=(
            RequestSlotDeclarationV1(
                source_id="u0",
                source_quote="confirmation code",
                plane=RequestSlotPlane.RUN,
                pinability=RequestSlotPinability.SHAPELESS_VALID,
                antecedent_family=RequestSlotAntecedentFamily.UNCONDITIONAL,
            ),
        ),
    )

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return _request_slot_envelope_with_target_bindings(
                prompt,
                envelope,
                anchors_by_index={0: ("u0", "confirmation code")},
            )
        if "REQUEST SLOT DATUM BINDING" in prompt:
            pytest.fail("producer consensus must not be echoed through a correction call")
        return original

    policy = await _classify_request(request, "", [], "", handler)

    assert calls == [
        "workflow-copilot-request-policy",
        REQUEST_SLOT_PROMPT_NAME,
        REQUEST_SLOT_PROMPT_NAME,
    ]
    criterion = policy.completion_criteria[0]
    assert policy.request_slot_failure_kind is None
    assert criterion.floor_rekeyed_from_path == "output.confirmation_code"
    assert criterion.request_slot_id is not None
    assert criterion.mint_disposition == "decidable"


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
        *([REQUEST_SLOT_PROMPT_NAME] * 4),
    ]
    assert policy.request_slot_failure_kind == "invalid_output"
    assert policy.completion_criteria[0].mint_disposition == "degraded"
    assert policy.completion_criteria[0].mint_degrade == "undecidable_judgment"


@pytest.mark.asyncio
async def test_fresh_non_output_request_accepts_agreed_empty_request_slot_contract() -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        calls.append(prompt_name)
        if prompt_name == REQUEST_SLOT_PROMPT_NAME:
            return RequestSlotEnvelopeV1(version="1", slots=()).model_dump(mode="json")
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
    assert calls == ["workflow-copilot-request-policy", REQUEST_SLOT_PROMPT_NAME, REQUEST_SLOT_PROMPT_NAME]
