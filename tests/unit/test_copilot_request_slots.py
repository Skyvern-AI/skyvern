from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel, ValidationError

from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat
from skyvern.forge.sdk.copilot import request_slots as request_slots_module
from skyvern.forge.sdk.copilot.request_slots import (
    CanonicalRequestSlotV1,
    RequestSlotAntecedentFamily,
    RequestSlotContractV1,
    RequestSlotDeclarationV1,
    RequestSlotEnvelopeV1,
    RequestSlotPinability,
    RequestSlotPlane,
    RequestSlotProducerFailureKind,
    RequestSlotProducerInputV1,
    RequestSlotProducerResult,
    RequestSlotSourceV1,
    canonicalize_request_slots,
    produce_request_slots,
    request_slot_contracts_agree,
    request_slot_source_text,
    request_slot_sources,
)


def _input(**overrides: object) -> RequestSlotProducerInputV1:
    values: dict[str, object] = {
        "version": "1",
        "latest_request": "Return whether a public form exists and the recommended next action.",
        "workflow_context": "name: public path validation",
        "earliest_user_turn": "User requested a validation-only workflow.",
        "latest_prior_user_turn": "Keep the workflow reusable.",
        "latest_assistant_turn": "I can validate the public path.",
        "retained_history": ("user: Do not submit anything.",),
        "global_context": '{"user_goal": "validate the public path"}',
    }
    values.update(overrides)
    return RequestSlotProducerInputV1.model_validate(values)


def _source_id(request: RequestSlotProducerInputV1, quote: str) -> str:
    matches = [source.source_id for source in request_slot_sources(request) if quote in source.text]
    assert len(matches) == 1, (quote, matches)
    return matches[0]


def _response(
    request: RequestSlotProducerInputV1,
    *slots: tuple[str, str, str],
) -> dict[str, object]:
    return {
        "version": "1",
        "slots": [
            {
                "source_id": _source_id(request, quote),
                "source_quote": quote,
                "plane": plane,
                "pinability": pinability,
                "antecedent_family": "unconditional",
            }
            for quote, plane, pinability in slots
        ],
    }


def _envelope(
    request: RequestSlotProducerInputV1,
    *slots: tuple[str, str, str],
) -> RequestSlotEnvelopeV1:
    return RequestSlotEnvelopeV1.model_validate_json(json.dumps(_response(request, *slots)))


def _identity(contract: RequestSlotContractV1) -> tuple[tuple[str, str, str, str, str], ...]:
    return tuple(
        (
            slot.slot_id,
            slot.canonical_path,
            slot.plane.value,
            slot.pinability.value,
            slot.antecedent_family.value,
        )
        for slot in contract.slots
    )


def _anchor_texts(request: RequestSlotProducerInputV1, contract: RequestSlotContractV1) -> list[str]:
    return [request_slot_source_text(request, slot) for slot in contract.slots]


def test_request_slot_models_are_strict_bounded_and_model_cannot_name_paths() -> None:
    request = _input()
    source_id = _source_id(request, "public form")
    valid_declaration = {
        "source_id": source_id,
        "source_quote": "public form",
        "plane": "run",
        "pinability": "shapeless_valid",
        "antecedent_family": "unconditional",
    }

    with pytest.raises(ValidationError):
        RequestSlotProducerInputV1.model_validate({"version": "2", "latest_request": "Build it"})
    with pytest.raises(ValidationError):
        RequestSlotProducerInputV1.model_validate({"version": "1", "latest_request": "x" * 16_385})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "path_segments": ["output", "public_form"]})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate(
            {key: value for key, value in valid_declaration.items() if key != "pinability"}
        )
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate(
            {key: value for key, value in valid_declaration.items() if key != "antecedent_family"}
        )
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "source_id": "latest_request"})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "source_quote": ""})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "plane": "unknown"})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "pinability": "maybe"})
    with pytest.raises(ValidationError):
        RequestSlotDeclarationV1.model_validate({**valid_declaration, "antecedent_family": "conditional"})
    with pytest.raises(ValidationError):
        RequestSlotSourceV1.model_validate({"source_id": "u1", "order": 0, "text": "Return status."})
    empty = RequestSlotEnvelopeV1.model_validate_json('{"version":"1","slots":[]}')
    assert empty.slots == ()
    with pytest.raises(ValidationError):
        RequestSlotProducerResult.model_validate({"status": "success", "attempts": 1})


def test_request_slot_input_requires_explicit_context_fields_but_allows_empty_values() -> None:
    payload = _input().model_dump()
    for field_name in (
        "latest_request",
        "workflow_context",
        "earliest_user_turn",
        "latest_prior_user_turn",
        "latest_assistant_turn",
        "retained_history",
        "global_context",
    ):
        with pytest.raises(ValidationError):
            RequestSlotProducerInputV1.model_validate(
                {key: value for key, value in payload.items() if key != field_name}
            )

    empty = RequestSlotProducerInputV1.model_validate(
        {
            "version": "1",
            "latest_request": "",
            "workflow_context": "",
            "earliest_user_turn": "",
            "latest_prior_user_turn": "",
            "latest_assistant_turn": "",
            "retained_history": (),
            "global_context": "",
        }
    )
    assert request_slot_sources(empty) == ()


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (RequestSlotProducerInputV1, {"version": "1", "latest_request": b"Build it"}),
        (
            RequestSlotDeclarationV1,
            {"source_id": "u0", "source_quote": b"status", "plane": "run", "pinability": "pinned"},
        ),
        (
            RequestSlotSourceV1,
            {"source_id": "u0", "order": 0.0, "text": "Return status."},
        ),
        (
            RequestSlotContractV1,
            {"version": "1", "request_digest": "0" * 64, "slots": (), "count": 1.0},
        ),
        (
            RequestSlotProducerResult,
            {"status": "failure", "attempts": 1.0, "failure_kind": RequestSlotProducerFailureKind.INVALID_OUTPUT},
        ),
    ],
)
def test_public_v1_models_reject_coercible_values(model: type[BaseModel], payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_request_sources_are_user_owned_chronological_deduplicated_and_server_named() -> None:
    request = _input(
        earliest_user_turn="Return invoice_number.",
        retained_history=("Return invoice_number.", "Return due_date."),
        latest_prior_user_turn="Return invoice_number.",
        latest_request="Also return status.",
        workflow_context="output.workflow_decoy",
        latest_assistant_turn="Return output.assistant_decoy",
        global_context='{"output":"global_decoy"}',
    )

    sources = request_slot_sources(request)

    assert [source.source_id for source in sources] == ["u0", "u1", "u2"]
    assert [source.text for source in sources] == [
        "Return due_date.",
        "Return invoice_number.",
        "Also return status.",
    ]
    assert all("decoy" not in source.text for source in sources)


def test_canonicalization_derives_server_owned_paths_ids_count_and_source_spans() -> None:
    request = _input(latest_request="Return the public form status and recommended next action.")
    envelope = _envelope(
        request,
        ("recommended next action", "run", "shapeless_valid"),
        ("public form status", "definition", "pinned"),
    )

    contract = canonicalize_request_slots(request=request, envelope=envelope)

    assert contract.count == 2
    assert [slot.ordinal for slot in contract.slots] == [0, 1]
    assert [slot.canonical_path.rsplit("_", 1)[-1] for slot in contract.slots] == ["00", "01"]
    assert all(slot.canonical_path.startswith("output.request_slot_") for slot in contract.slots)
    assert all(len(slot.slot_id) == 64 for slot in contract.slots)
    assert [slot.plane for slot in contract.slots] == [RequestSlotPlane.DEFINITION, RequestSlotPlane.RUN]
    assert [slot.pinability for slot in contract.slots] == [
        RequestSlotPinability.PINNED,
        RequestSlotPinability.SHAPELESS_VALID,
    ]
    assert all(slot.antecedent_family == RequestSlotAntecedentFamily.UNCONDITIONAL for slot in contract.slots)
    assert _anchor_texts(request, contract) == ["public form status", "recommended next action"]
    assert contract == RequestSlotContractV1.model_validate(contract.model_dump())


@pytest.mark.parametrize(
    ("latest_request", "quotes", "error"),
    [
        ("Return status.", ("missing",), "not present"),
        ("Return status and status.", ("status",), "not unique"),
        ("Return public form exists.", ("public form exists", "form exists"), "overlap"),
    ],
)
def test_canonicalization_rejects_missing_ambiguous_and_overlapping_anchors(
    latest_request: str,
    quotes: tuple[str, ...],
    error: str,
) -> None:
    request = _input(latest_request=latest_request)
    source_id = request_slot_sources(request)[-1].source_id
    envelope = RequestSlotEnvelopeV1.model_validate_json(
        json.dumps(
            {
                "version": "1",
                "slots": [
                    {
                        "source_id": source_id,
                        "source_quote": quote,
                        "plane": "run",
                        "pinability": "shapeless_valid",
                        "antecedent_family": "unconditional",
                    }
                    for quote in quotes
                ],
            }
        )
    )

    with pytest.raises(ValueError, match=error):
        canonicalize_request_slots(request=request, envelope=envelope)


def test_canonicalization_rejects_unknown_source_and_context_only_anchor() -> None:
    request = _input(latest_request="Return status.", workflow_context="output.workflow_decoy")
    for source_id, source_quote in (("u99", "status"), (request_slot_sources(request)[-1].source_id, "workflow_decoy")):
        envelope = RequestSlotEnvelopeV1.model_validate_json(
            json.dumps(
                {
                    "version": "1",
                    "slots": [
                        {
                            "source_id": source_id,
                            "source_quote": source_quote,
                            "plane": "run",
                            "pinability": "shapeless_valid",
                            "antecedent_family": "unconditional",
                        }
                    ],
                }
            )
        )
        with pytest.raises(ValueError):
            canonicalize_request_slots(request=request, envelope=envelope)


def test_response_order_and_quote_boundary_variation_do_not_change_identity() -> None:
    request = _input(
        latest_request=(
            "Return a structured summary showing whether a public form exists, whether the path is login-only, "
            "the visible page/path label, and the recommended next action."
        )
    )
    response_shapes = [
        (
            ("whether a public form exists", "run", "shapeless_valid"),
            ("whether the path is login-only", "run", "shapeless_valid"),
            ("the visible page/path label", "run", "shapeless_valid"),
            ("the recommended next action", "run", "shapeless_valid"),
        ),
        (
            ("recommended next action", "run", "shapeless_valid"),
            ("page/path label", "run", "shapeless_valid"),
            ("path is login-only", "run", "shapeless_valid"),
            ("a public form exists", "run", "shapeless_valid"),
        ),
        (
            ("visible page/path label", "run", "shapeless_valid"),
            ("public form exists", "run", "shapeless_valid"),
            ("recommended next action", "run", "shapeless_valid"),
            ("login-only", "run", "shapeless_valid"),
        ),
    ]

    contracts = [
        canonicalize_request_slots(request=request, envelope=_envelope(request, *response_shape))
        for response_shape in response_shapes
    ]

    assert contracts[0].count == contracts[1].count == contracts[2].count == 4
    assert _identity(contracts[0]) == _identity(contracts[1]) == _identity(contracts[2])
    for contract in contracts:
        anchors = _anchor_texts(request, contract)
        assert any("public form" in anchor for anchor in anchors)
        assert any("login-only" in anchor for anchor in anchors)
        assert any("page/path label" in anchor for anchor in anchors)
        assert any("recommended next action" in anchor for anchor in anchors)


def test_contract_agreement_detects_membership_that_opaque_identity_does_not_encode() -> None:
    request = _input(latest_request="Return alpha and beta.")
    alpha = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("alpha", "run", "shapeless_valid")),
    )
    beta = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("beta", "run", "shapeless_valid")),
    )

    assert _identity(alpha) == _identity(beta)
    assert not request_slot_contracts_agree(alpha, beta)


def test_contract_agreement_requires_antecedent_family_consensus() -> None:
    request = _input(latest_request="Report the blocker if online submission is unavailable.")
    unconditional_envelope = _envelope(request, ("blocker", "run", "shapeless_valid"))
    blocker_envelope = unconditional_envelope.model_copy(
        update={
            "slots": (
                unconditional_envelope.slots[0].model_copy(
                    update={"antecedent_family": RequestSlotAntecedentFamily.BLOCKER}
                ),
            )
        }
    )
    unconditional = canonicalize_request_slots(request=request, envelope=unconditional_envelope)
    blocker = canonicalize_request_slots(request=request, envelope=blocker_envelope)

    assert unconditional.slots[0].slot_id == blocker.slots[0].slot_id
    assert not request_slot_contracts_agree(unconditional, blocker)


@pytest.mark.asyncio
async def test_producer_accepts_two_agreeing_empty_contracts() -> None:
    calls = 0

    async def handler(*, prompt: str, prompt_name: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"version": "1", "slots": []}

    result = await produce_request_slots(request=_input(latest_request="Submit the form."), handler=handler)

    assert result.status == "success"
    assert result.attempts == 2
    assert result.contract is not None
    assert result.contract.count == 0
    assert calls == 2


def test_pinability_is_not_identity_but_plane_is() -> None:
    request = _input(latest_request="Return status.")
    pinned = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("status", "run", "pinned")),
    ).slots[0]
    unpinnable = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("status", "run", "unpinnable")),
    ).slots[0]
    definition = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("status", "definition", "pinned")),
    ).slots[0]

    assert pinned.canonical_path == unpinnable.canonical_path == definition.canonical_path
    assert pinned.slot_id == unpinnable.slot_id
    assert pinned.slot_id != definition.slot_id
    assert pinned.pinability != unpinnable.pinability


def test_canonical_models_reject_tampered_server_fields() -> None:
    request = _input(latest_request="Return status.")
    contract = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("status", "run", "shapeless_valid")),
    )
    slot = contract.slots[0]

    with pytest.raises(ValidationError):
        CanonicalRequestSlotV1.model_validate({**slot.model_dump(), "canonical_path": "output.login_only"})
    with pytest.raises(ValidationError):
        RequestSlotContractV1.model_validate({**contract.model_dump(), "count": 99})


@pytest.mark.asyncio
async def test_producer_retries_invalid_fresh_payload_then_succeeds() -> None:
    request = _input(latest_request="Return status.")
    responses = [
        {
            "version": "1",
            "slots": [{"source_id": _source_id(request, "status"), "source_quote": "status", "plane": "run"}],
        },
        _response(request, ("status", "run", "shapeless_valid")),
        _response(request, ("status", "run", "shapeless_valid")),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        assert prompt_name == "workflow-copilot-request-slots"
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    assert result.attempts == 3
    assert result.contract is not None
    assert result.contract.slots[0].pinability == RequestSlotPinability.SHAPELESS_VALID
    assert responses == []


@pytest.mark.asyncio
async def test_producer_requires_source_overlap_agreement_before_success() -> None:
    request = _input(latest_request="Return whether a public form exists.")
    responses = [
        _response(request, ("whether a public form exists", "run", "shapeless_valid")),
        _response(request, ("public form exists", "run", "shapeless_valid")),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    assert result.attempts == 2
    assert result.contract is not None
    assert request_slot_source_text(request, result.contract.slots[0]) == "whether a public form exists"
    assert responses == []


@pytest.mark.asyncio
async def test_producer_retries_disagreement_until_consecutive_contracts_agree() -> None:
    request = _input(latest_request="Return alpha and beta.")
    responses = [
        _response(request, ("alpha", "run", "shapeless_valid")),
        _response(
            request,
            ("alpha", "run", "shapeless_valid"),
            ("beta", "run", "shapeless_valid"),
        ),
        _response(
            request,
            ("alpha", "run", "shapeless_valid"),
            ("beta", "run", "shapeless_valid"),
        ),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    assert result.attempts == 3
    assert result.contract is not None
    assert result.contract.count == 2
    assert responses == []


@pytest.mark.asyncio
async def test_producer_requires_plane_and_pinability_agreement() -> None:
    request = _input(latest_request="Return status.")
    responses = [
        _response(request, ("status", "definition", "pinned")),
        _response(request, ("status", "run", "shapeless_valid")),
        _response(request, ("status", "run", "shapeless_valid")),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    assert result.attempts == 3
    assert result.contract is not None
    assert result.contract.slots[0].plane == RequestSlotPlane.RUN
    assert result.contract.slots[0].pinability == RequestSlotPinability.SHAPELESS_VALID
    assert responses == []


@pytest.mark.asyncio
async def test_producer_can_recover_from_an_a_b_a_sequence_without_majority_minting() -> None:
    request = _input(latest_request="Return alpha and beta.")
    responses = [
        _response(request, ("alpha", "run", "shapeless_valid")),
        _response(
            request,
            ("alpha", "run", "shapeless_valid"),
            ("beta", "run", "shapeless_valid"),
        ),
        _response(request, ("alpha", "run", "shapeless_valid")),
        _response(request, ("alpha", "run", "shapeless_valid")),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    assert result.attempts == 4
    assert result.contract is not None
    assert result.contract.count == 1
    assert responses == []


@pytest.mark.asyncio
async def test_producer_degrades_when_only_nonconsecutive_valid_shapes_agree() -> None:
    request = _input(latest_request="Return alpha and beta.")
    responses = [
        _response(request, ("alpha", "run", "shapeless_valid")),
        _response(
            request,
            ("alpha", "run", "shapeless_valid"),
            ("beta", "run", "shapeless_valid"),
        ),
        _response(request, ("alpha", "run", "shapeless_valid")),
        _response(
            request,
            ("alpha", "run", "shapeless_valid"),
            ("beta", "run", "shapeless_valid"),
        ),
    ]

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        return responses.pop(0)

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result == RequestSlotProducerResult.failure(
        RequestSlotProducerFailureKind.INCONSISTENT_OUTPUT,
        attempts=4,
    )
    assert responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "failure_kind"),
    [
        (EmptyLLMResponseError(""), RequestSlotProducerFailureKind.EMPTY_OUTPUT),
        (InvalidLLMResponseFormat("bad"), RequestSlotProducerFailureKind.INVALID_OUTPUT),
    ],
)
async def test_producer_maps_common_handler_response_errors_into_typed_retry(
    exception: Exception,
    failure_kind: RequestSlotProducerFailureKind,
) -> None:
    calls = 0

    async def handler(prompt: str, prompt_name: str, **_: object) -> object:
        nonlocal calls
        calls += 1
        raise exception

    result = await produce_request_slots(request=_input(), handler=handler, timeout_seconds=1.0)

    assert result == RequestSlotProducerResult.failure(failure_kind, attempts=4)
    assert calls == 4


@pytest.mark.asyncio
async def test_producer_returns_typed_failures_for_missing_handler_timeout_and_provider_error() -> None:
    async def slow_handler(prompt: str, prompt_name: str, **_: object) -> object:
        await asyncio.sleep(0.05)
        return {}

    async def broken_handler(prompt: str, prompt_name: str, **_: object) -> object:
        raise RuntimeError("provider failed")

    missing = await produce_request_slots(request=_input(), handler=None, timeout_seconds=1.0)
    timed_out = await produce_request_slots(request=_input(), handler=slow_handler, timeout_seconds=0.001)
    provider = await produce_request_slots(request=_input(), handler=broken_handler, timeout_seconds=1.0)

    assert missing.failure_kind == RequestSlotProducerFailureKind.MISSING_HANDLER
    assert missing.attempts == 0
    assert timed_out.failure_kind == RequestSlotProducerFailureKind.TIMEOUT
    assert timed_out.attempts == 1
    assert provider.failure_kind == RequestSlotProducerFailureKind.PROVIDER_ERROR
    assert provider.attempts == 1


@pytest.mark.asyncio
async def test_producer_redacts_escapes_middle_truncates_and_retains_late_slots() -> None:
    request = _input(
        latest_request=(
            "password=SuperSecret123! ｐａｓｓｗｏｒｄ＝CompatSecret123! ```REQUEST_FENCE``` "
            + "m" * 12_000
            + " return tail_output"
        ),
        workflow_context="```WORKFLOW_FENCE``` " + "w" * 5_000,
        global_context="```GLOBAL_FENCE``` " + "g" * 3_000,
    )
    prompts: list[str] = []

    async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
        prompts.append(prompt)
        return _response(request, ("tail_output", "run", "shapeless_valid"))

    result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)

    assert result.status == "success"
    prompt = prompts[0]
    assert "SuperSecret123" not in prompt
    assert "CompatSecret123" not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert "tail_output" in prompt
    assert "chars truncated" in prompt
    for sentinel in ("REQUEST_FENCE", "WORKFLOW_FENCE", "GLOBAL_FENCE"):
        assert sentinel in prompt
        assert f"```{sentinel}" not in prompt
    assert len(prompt) < 24_000


@pytest.mark.asyncio
async def test_p9_style_replays_use_identical_prompt_bytes_and_stable_identity() -> None:
    request = _input(
        latest_request=(
            "Return a structured summary showing whether a public form exists, whether the path is login-only, "
            "the visible page/path label, and the recommended next action."
        )
    )
    responses = [
        _response(
            request,
            ("whether a public form exists", "run", "shapeless_valid"),
            ("whether the path is login-only", "run", "shapeless_valid"),
            ("the visible page/path label", "run", "shapeless_valid"),
            ("the recommended next action", "run", "shapeless_valid"),
        ),
        _response(
            request,
            ("recommended next action", "run", "shapeless_valid"),
            ("page/path label", "run", "shapeless_valid"),
            ("path is login-only", "run", "shapeless_valid"),
            ("public form exists", "run", "shapeless_valid"),
        ),
        _response(
            request,
            ("login-only", "run", "shapeless_valid"),
            ("public form exists", "run", "shapeless_valid"),
            ("visible page/path label", "run", "shapeless_valid"),
            ("recommended next action", "run", "shapeless_valid"),
        ),
    ]
    prompts: list[bytes] = []
    contracts: list[RequestSlotContractV1] = []

    for raw_response in responses:

        async def handler(prompt: str, prompt_name: str, **_: object) -> dict[str, object]:
            prompts.append(prompt.encode())
            return raw_response

        result = await produce_request_slots(request=request, handler=handler, timeout_seconds=1.0)
        assert result.contract is not None
        contracts.append(result.contract)

    assert all(prompt == prompts[0] for prompt in prompts)
    assert len(prompts) == 6
    assert _identity(contracts[0]) == _identity(contracts[1]) == _identity(contracts[2])
    assert [contract.count for contract in contracts] == [4, 4, 4]
    for contract in contracts:
        anchors = _anchor_texts(request, contract)
        assert sum("public form" in anchor for anchor in anchors) == 1
        assert sum("login-only" in anchor for anchor in anchors) == 1
        assert sum("page/path label" in anchor for anchor in anchors) == 1
        assert sum("recommended next action" in anchor for anchor in anchors) == 1


def test_contract_namespace_never_collides_with_legacy_semantic_paths() -> None:
    request = _input(latest_request="Return whether the path is login-only.")
    contract = canonicalize_request_slots(
        request=request,
        envelope=_envelope(request, ("path is login-only", "run", "shapeless_valid")),
    )

    assert contract.slots[0].canonical_path != "output.login_only"
    assert contract.slots[0].canonical_path != "output.path_is_login_only"
    assert contract.slots[0].canonical_path.startswith("output.request_slot_")
    assert request_slots_module.PROMPT_NAME == "workflow-copilot-request-slots"
