from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from enum import StrEnum
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat
from skyvern.forge.sdk.copilot.context import sanitize_global_llm_context_for_prompt
from skyvern.forge.sdk.copilot.llm_errors import is_retriable_llm_error
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()
PROMPT_NAME = "workflow-copilot-request-slots"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_REQUEST_PROMPT_CHARS = 8_192
_MAX_WORKFLOW_PROMPT_CHARS = 4_096
_MAX_TRANSCRIPT_ANCHOR_PROMPT_CHARS = 1_000
_MAX_RETAINED_HISTORY_PROMPT_ITEMS = 4
_MAX_GLOBAL_CONTEXT_PROMPT_CHARS = 2_048
_MAX_SOURCE_QUOTE_CHARS = 1_024
_MAX_CLASSIFIER_ATTEMPTS = 4
_SOURCE_ID_PATTERN = re.compile(r"^u(?:0|[1-9][0-9]?)$")
_PATH_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_SLOT_PATH_PATTERN = re.compile(r"^request_slot_[0-9a-f]{48}_[0-9]{2}$")


class RequestSlotPlane(StrEnum):
    DEFINITION = "definition"
    RUN = "run"


class RequestSlotPinability(StrEnum):
    PINNED = "pinned"
    SHAPELESS_VALID = "shapeless_valid"
    UNPINNABLE = "unpinnable"


class RequestSlotAntecedentFamily(StrEnum):
    UNCONDITIONAL = "unconditional"
    BLOCKER = "blocker"
    UNDECIDABLE = "undecidable"


class RequestSlotProducerFailureKind(StrEnum):
    MISSING_HANDLER = "missing_handler"
    PROMPT_RENDER_ERROR = "prompt_render_error"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    EMPTY_OUTPUT = "empty_output"
    INVALID_OUTPUT = "invalid_output"
    INCONSISTENT_OUTPUT = "inconsistent_output"


class RequestSlotProducerInputV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"]
    latest_request: str = Field(max_length=16_384)
    workflow_context: str = Field(max_length=32_768)
    earliest_user_turn: str = Field(max_length=4_096)
    latest_prior_user_turn: str = Field(max_length=4_096)
    latest_assistant_turn: str = Field(max_length=4_096)
    retained_history: tuple[str, ...] = Field(max_length=8)
    global_context: str = Field(max_length=32_768)

    @field_validator("retained_history")
    @classmethod
    def _validate_retained_history(cls, entries: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() or len(entry) > 4_096 for entry in entries):
            raise ValueError("retained history entries must be non-empty and at most 4096 characters")
        return entries


class RequestSlotSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str = Field(min_length=2, max_length=3, pattern=_SOURCE_ID_PATTERN.pattern)
    order: int = Field(ge=0, le=63)
    text: str = Field(min_length=1, max_length=_MAX_REQUEST_PROMPT_CHARS)

    @model_validator(mode="after")
    def _validate_server_owned_identity(self) -> RequestSlotSourceV1:
        if self.source_id != f"u{self.order}":
            raise ValueError("source_id must derive from source order")
        return self


class RequestSlotDeclarationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str = Field(min_length=2, max_length=3, pattern=_SOURCE_ID_PATTERN.pattern)
    source_quote: str = Field(min_length=1, max_length=_MAX_SOURCE_QUOTE_CHARS)
    plane: RequestSlotPlane
    pinability: RequestSlotPinability
    antecedent_family: RequestSlotAntecedentFamily

    @field_validator("source_quote")
    @classmethod
    def _validate_source_quote(cls, quote: str) -> str:
        if not quote.strip():
            raise ValueError("source_quote must contain non-whitespace request text")
        return quote


class RequestSlotEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"]
    slots: tuple[RequestSlotDeclarationV1, ...] = Field(max_length=64)


def _request_digest(version: str, sources: tuple[RequestSlotSourceV1, ...]) -> str:
    encoded = json.dumps(
        [version, [[source.source_id, source.text] for source in sources]],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_path(request_digest: str, ordinal: int) -> tuple[str, tuple[str, str]]:
    leaf = f"request_slot_{request_digest[:48]}_{ordinal:02d}"
    return f"output.{leaf}", ("output", leaf)


def _slot_id(version: str, request_digest: str, ordinal: int, plane: RequestSlotPlane) -> str:
    encoded = json.dumps(
        [version, request_digest, ordinal, plane.value],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class CanonicalRequestSlotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    slot_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    canonical_path: str = Field(min_length=1, max_length=71)
    path_segments: tuple[str, str]
    ordinal: int = Field(ge=0, le=63)
    source_id: str = Field(min_length=2, max_length=3, pattern=_SOURCE_ID_PATTERN.pattern)
    source_start: int = Field(ge=0, le=_MAX_REQUEST_PROMPT_CHARS)
    source_end: int = Field(ge=1, le=_MAX_REQUEST_PROMPT_CHARS)
    plane: RequestSlotPlane
    pinability: RequestSlotPinability
    antecedent_family: RequestSlotAntecedentFamily

    @field_validator("path_segments")
    @classmethod
    def _validate_path_segments(cls, segments: tuple[str, str]) -> tuple[str, str]:
        if any(_PATH_SEGMENT_PATTERN.fullmatch(segment) is None for segment in segments):
            raise ValueError("path segments must be lowercase identifiers of at most 64 characters")
        if segments[0] != "output" or _REQUEST_SLOT_PATH_PATTERN.fullmatch(segments[1]) is None:
            raise ValueError("canonical request-slot paths must use the server-owned namespace")
        return segments

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> CanonicalRequestSlotV1:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        if self.canonical_path != ".".join(self.path_segments):
            raise ValueError("canonical_path must derive from path_segments")
        return self


class RequestSlotContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"]
    request_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    slots: tuple[CanonicalRequestSlotV1, ...] = Field(max_length=64)
    count: int = Field(ge=0, le=64)

    @model_validator(mode="after")
    def _validate_derived_membership(self) -> RequestSlotContractV1:
        if self.count != len(self.slots):
            raise ValueError("count must derive from slot membership")
        if [slot.ordinal for slot in self.slots] != list(range(self.count)):
            raise ValueError("slot ordinals must be contiguous and in canonical order")
        if len({slot.canonical_path for slot in self.slots}) != self.count:
            raise ValueError("slot membership contains a canonical path collision")
        if len({slot.slot_id for slot in self.slots}) != self.count:
            raise ValueError("slot membership contains a canonical slot identity collision")

        previous: CanonicalRequestSlotV1 | None = None
        for slot in self.slots:
            expected_path, expected_segments = _canonical_path(self.request_digest, slot.ordinal)
            if slot.canonical_path != expected_path or slot.path_segments != expected_segments:
                raise ValueError("canonical path must derive from request digest and ordinal")
            if slot.slot_id != _slot_id(self.version, self.request_digest, slot.ordinal, slot.plane):
                raise ValueError("slot_id must derive from version, request digest, ordinal, and plane")
            if previous is not None:
                previous_order = int(previous.source_id[1:])
                current_order = int(slot.source_id[1:])
                if (current_order, slot.source_start, slot.source_end) < (
                    previous_order,
                    previous.source_start,
                    previous.source_end,
                ):
                    raise ValueError("slot membership must be in request-source order")
                if current_order == previous_order and slot.source_start < previous.source_end:
                    raise ValueError("slot membership contains overlapping source anchors")
            previous = slot
        return self


class RequestSlotProducerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["success", "failure"]
    attempts: int = Field(ge=0, le=_MAX_CLASSIFIER_ATTEMPTS)
    contract: RequestSlotContractV1 | None = None
    failure_kind: RequestSlotProducerFailureKind | None = None

    @model_validator(mode="after")
    def _validate_exclusive_result(self) -> RequestSlotProducerResult:
        if self.status == "success":
            if self.contract is None or self.failure_kind is not None or self.attempts == 0:
                raise ValueError("success requires a contract, at least one attempt, and no failure")
        elif self.contract is not None or self.failure_kind is None:
            raise ValueError("failure requires a failure kind and no contract")
        return self

    @classmethod
    def success(cls, contract: RequestSlotContractV1, *, attempts: int) -> RequestSlotProducerResult:
        return cls(status="success", attempts=attempts, contract=contract)

    @classmethod
    def failure(
        cls,
        failure_kind: RequestSlotProducerFailureKind,
        *,
        attempts: int,
    ) -> RequestSlotProducerResult:
        return cls(status="failure", attempts=attempts, failure_kind=failure_kind)


def _middle_truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    keep = max(cap - 32, 16)
    head_len = keep // 2
    tail_len = keep - head_len
    omitted = len(text) - keep
    return f"{text[:head_len]}<…{omitted} chars truncated…>{text[-tail_len:]}"


def _safe_prompt_text(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    bounded = normalized if len(normalized) <= limit * 4 else normalized[: limit * 2] + normalized[-limit * 2 :]
    escaped = escape_code_fences(redact_raw_secrets_for_prompt(bounded))
    return _middle_truncate(escaped, limit)


def request_slot_sources(request: RequestSlotProducerInputV1) -> tuple[RequestSlotSourceV1, ...]:
    candidates = [
        _safe_prompt_text(request.earliest_user_turn, _MAX_TRANSCRIPT_ANCHOR_PROMPT_CHARS),
        *(
            _safe_prompt_text(entry, _MAX_TRANSCRIPT_ANCHOR_PROMPT_CHARS)
            for entry in request.retained_history[:_MAX_RETAINED_HISTORY_PROMPT_ITEMS]
        ),
        _safe_prompt_text(request.latest_prior_user_turn, _MAX_TRANSCRIPT_ANCHOR_PROMPT_CHARS),
        _safe_prompt_text(request.latest_request, _MAX_REQUEST_PROMPT_CHARS),
    ]
    retained_reversed: list[str] = []
    seen: set[str] = set()
    for text in reversed(candidates):
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        retained_reversed.append(text)
    retained = tuple(reversed(retained_reversed))
    return tuple(
        RequestSlotSourceV1(source_id=f"u{order}", order=order, text=text) for order, text in enumerate(retained)
    )


def request_slot_source_text(request: RequestSlotProducerInputV1, slot: CanonicalRequestSlotV1) -> str:
    source = next(
        (candidate for candidate in request_slot_sources(request) if candidate.source_id == slot.source_id), None
    )
    if source is None or slot.source_end > len(source.text):
        raise ValueError("canonical slot does not dereference against the request source set")
    return source.text[slot.source_start : slot.source_end]


def canonicalize_request_slots(
    *,
    request: RequestSlotProducerInputV1,
    envelope: RequestSlotEnvelopeV1,
) -> RequestSlotContractV1:
    if request.version != envelope.version:
        raise ValueError("request and response contract versions must match")
    sources = request_slot_sources(request)
    source_by_id = {source.source_id: source for source in sources}
    resolved: list[tuple[RequestSlotDeclarationV1, RequestSlotSourceV1, int, int]] = []
    for declaration in envelope.slots:
        source = source_by_id.get(declaration.source_id)
        if source is None:
            raise ValueError(f"unknown request source: {declaration.source_id}")
        start = source.text.find(declaration.source_quote)
        if start < 0:
            raise ValueError(f"source quote is not present in {declaration.source_id}")
        if source.text.find(declaration.source_quote, start + 1) >= 0:
            raise ValueError(f"source quote is not unique in {declaration.source_id}")
        resolved.append((declaration, source, start, start + len(declaration.source_quote)))

    resolved.sort(key=lambda item: (item[1].order, item[2], item[3]))
    previous: tuple[RequestSlotDeclarationV1, RequestSlotSourceV1, int, int] | None = None
    for item in resolved:
        if previous is not None and item[1].source_id == previous[1].source_id and item[2] < previous[3]:
            raise ValueError(f"source anchors overlap in {item[1].source_id}")
        previous = item

    digest = _request_digest(envelope.version, sources)
    canonical_slots: list[CanonicalRequestSlotV1] = []
    seen_paths: set[str] = set()
    seen_slot_ids: set[str] = set()
    for ordinal, (declaration, source, start, end) in enumerate(resolved):
        canonical_path, path_segments = _canonical_path(digest, ordinal)
        slot_id = _slot_id(envelope.version, digest, ordinal, declaration.plane)
        if canonical_path in seen_paths:
            raise ValueError(f"canonical request-slot path collision: {canonical_path}")
        if slot_id in seen_slot_ids:
            raise ValueError(f"canonical request-slot identity collision: {canonical_path}")
        seen_paths.add(canonical_path)
        seen_slot_ids.add(slot_id)
        canonical_slots.append(
            CanonicalRequestSlotV1(
                slot_id=slot_id,
                canonical_path=canonical_path,
                path_segments=path_segments,
                ordinal=ordinal,
                source_id=source.source_id,
                source_start=start,
                source_end=end,
                plane=declaration.plane,
                pinability=declaration.pinability,
                antecedent_family=declaration.antecedent_family,
            )
        )
    return RequestSlotContractV1(
        version=envelope.version,
        request_digest=digest,
        slots=tuple(canonical_slots),
        count=len(canonical_slots),
    )


def _render_prompt(request: RequestSlotProducerInputV1) -> str:
    safe_global_context = sanitize_global_llm_context_for_prompt(request.global_context)
    sources = request_slot_sources(request)
    return prompt_engine.load_prompt(
        template=PROMPT_NAME,
        request_sources=json.dumps(
            [source.model_dump(mode="json") for source in sources],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        workflow_context=_safe_prompt_text(request.workflow_context, _MAX_WORKFLOW_PROMPT_CHARS),
        latest_assistant_turn=_safe_prompt_text(request.latest_assistant_turn, _MAX_TRANSCRIPT_ANCHOR_PROMPT_CHARS),
        global_context=_safe_prompt_text(safe_global_context, _MAX_GLOBAL_CONTEXT_PROMPT_CHARS),
    )


def _is_empty_output(raw: object) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _parse_envelope(raw: object) -> RequestSlotEnvelopeV1 | None:
    try:
        payload = raw if isinstance(raw, str) else json.dumps(raw)
        return RequestSlotEnvelopeV1.model_validate_json(payload)
    except (TypeError, ValueError, ValidationError):
        return None


def request_slot_contracts_agree(
    first: RequestSlotContractV1,
    second: RequestSlotContractV1,
) -> bool:
    if first.version != second.version or first.request_digest != second.request_digest or first.count != second.count:
        return False
    return all(
        first_slot.ordinal == second_slot.ordinal
        and first_slot.source_id == second_slot.source_id
        and first_slot.plane == second_slot.plane
        and first_slot.pinability == second_slot.pinability
        and first_slot.antecedent_family == second_slot.antecedent_family
        and first_slot.source_start < second_slot.source_end
        and second_slot.source_start < first_slot.source_end
        for first_slot, second_slot in zip(first.slots, second.slots, strict=True)
    )


async def produce_request_slots(
    *,
    request: RequestSlotProducerInputV1,
    handler: LLMAPIHandler | None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RequestSlotProducerResult:
    if handler is None:
        return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.MISSING_HANDLER, attempts=0)
    try:
        prompt = _render_prompt(request)
    except Exception as exc:
        LOG.warning("request-slot prompt render failed", error=str(exc))
        return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.PROMPT_RENDER_ERROR, attempts=0)

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    last_failure = RequestSlotProducerFailureKind.INVALID_OUTPUT
    candidate_contract: RequestSlotContractV1 | None = None
    for attempt in range(1, _MAX_CLASSIFIER_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.TIMEOUT, attempts=attempt - 1)
        try:
            raw = await asyncio.wait_for(
                handler(prompt=prompt, prompt_name=PROMPT_NAME),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.TIMEOUT, attempts=attempt)
        except EmptyLLMResponseError:
            last_failure = RequestSlotProducerFailureKind.EMPTY_OUTPUT
            continue
        except InvalidLLMResponseFormat:
            last_failure = RequestSlotProducerFailureKind.INVALID_OUTPUT
            continue
        except Exception as exc:
            if attempt < _MAX_CLASSIFIER_ATTEMPTS and is_retriable_llm_error(exc):
                continue
            if time.monotonic() >= deadline:
                return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.TIMEOUT, attempts=attempt)
            LOG.warning("request-slot classifier provider failed", error=str(exc), attempt=attempt)
            return RequestSlotProducerResult.failure(RequestSlotProducerFailureKind.PROVIDER_ERROR, attempts=attempt)

        if _is_empty_output(raw):
            last_failure = RequestSlotProducerFailureKind.EMPTY_OUTPUT
            continue
        envelope = _parse_envelope(raw)
        if envelope is None:
            last_failure = RequestSlotProducerFailureKind.INVALID_OUTPUT
            continue
        try:
            contract = canonicalize_request_slots(request=request, envelope=envelope)
        except ValueError:
            last_failure = RequestSlotProducerFailureKind.INVALID_OUTPUT
            continue
        if candidate_contract is not None and request_slot_contracts_agree(candidate_contract, contract):
            return RequestSlotProducerResult.success(candidate_contract, attempts=attempt)
        candidate_contract = contract

    if candidate_contract is not None:
        last_failure = RequestSlotProducerFailureKind.INCONSISTENT_OUTPUT
    return RequestSlotProducerResult.failure(last_failure, attempts=_MAX_CLASSIFIER_ATTEMPTS)
