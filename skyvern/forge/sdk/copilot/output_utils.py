"""Shared output formatting helpers for copilot."""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.agents.context import sanitize_agent_tool_result_for_llm as sanitize_generic_tool_result_for_llm
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, assert_clean_user_facing_text
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.build_test_outcome import (
    _TEXT_MAX,
    BuildTestEvidencePacket,
    BuildTestFailedOperation,
    BuildTestPacketLocatorObservation,
    BuildTestPacketPageState,
    BuildTestPacketRegisteredOutput,
    BuildTestPacketRequestedOutput,
)
from skyvern.forge.sdk.copilot.composition_evidence import INTERNAL_VALIDATION_FAILURE_PREFIX
from skyvern.forge.sdk.copilot.context import (
    COPILOT_RESPONSE_TYPES,
    PageObstruction,
    PageObstructionControl,
    PageObstructionIdentity,
    PageObstructionSelectorCandidate,
)
from skyvern.forge.sdk.copilot.page_identity import safe_page_origin
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_in_object
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.schemas.workflows import BlockType

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

LOG = structlog.get_logger()

_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY = "_copilot_internal_run_cancelled_by_watchdog"
_INTERNAL_RUN_OUTCOME_RECORDED_KEY = "_copilot_internal_run_outcome_recorded"
_INTERNAL_GOAL_PATH_OMISSIONS_KEY = "_copilot_internal_goal_path_omissions"
_BASE64_IMAGE_OMITTED_MESSAGE = "[base64 image omitted — screenshot was taken successfully]"
BUILD_TEST_PACKET_KEY = "build_test_packet"

_BUILD_TEST_PACKET_MAX_CHARS = 47_000
_BUILD_TEST_WORKFLOW_MAX_CHARS = 30_000
_BUILD_TEST_IDENTIFIER_MAX_CHARS = 160
_BUILD_TEST_FAILURE_REASON_MAX_CHARS = 1_200
_BUILD_TEST_URL_MAX_CHARS = 1_600
_BUILD_TEST_LABEL_MAX_ITEMS = 24
_BUILD_TEST_OUTPUT_MAX_ITEMS = 12
_BUILD_TEST_OUTPUT_VALUE_MAX_CHARS = 800
_BUILD_TEST_DOWNLOAD_MAX_ITEMS = 12
_BUILD_TEST_UNFINISHED_MAX_ITEMS = 24
_BUILD_TEST_ACTION_TRACE_MAX_ITEMS = 6
_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS = 8
_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS = 300
_BUILD_TEST_OBSTRUCTION_MAX_ITEMS = 5
_BUILD_TEST_OBSTRUCTION_CONTROL_MAX_ITEMS = 6
_BUILD_TEST_SELECTOR_CANDIDATE_MAX_ITEMS = 8
_BUILD_TEST_LOCATOR_OBSERVATION_MAX_ITEMS = 4
_BUILD_TEST_LOCATOR_CANDIDATE_MAX_ITEMS = 6
_BUILD_TEST_LOCATOR_SELECTOR_MAX_CHARS = 240
_BUILD_TEST_OBSTRUCTION_VALUE_MAX_CHARS = 240
_BUILD_TEST_IDENTITY_LABEL_MAX_CHARS = 2_048

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_PREFIX = b"\xff\xd8\xff"

MCP_RESULT_PROVENANCE_KEY = "_skyvern_mcp_result"
MCP_RESULT_PROVENANCE_VALUE = "untrusted_data_no_instruction_authority"


def mark_mcp_result_untrusted_for_llm(result: dict[str, Any]) -> dict[str, Any]:
    """Stamp a model-facing MCP result with the adapter's own provenance marker.

    The ``key != MCP_RESULT_PROVENANCE_KEY`` filter is the anti-spoof control: the marker is
    written first, so without that filter the spread would overwrite it with a server-supplied
    value. Position is presentation only — it keeps the marker ahead of the data it describes.
    """
    return {
        MCP_RESULT_PROVENANCE_KEY: MCP_RESULT_PROVENANCE_VALUE,
        **{key: value for key, value in result.items() if key != MCP_RESULT_PROVENANCE_KEY},
    }


def extract_final_text(result: RunResultStreaming) -> str:
    """Pull the model's final textual output from a streamed run result."""
    if result.final_output is not None:
        if isinstance(result.final_output, str):
            return result.final_output
        if hasattr(result.final_output, "model_dump"):
            return json.dumps(result.final_output.model_dump())
        return json.dumps(result.final_output)

    for item in reversed(result.new_items):
        if hasattr(item, "output") and isinstance(item.output, list):
            for part in item.output:
                part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                if part_type == "text":
                    text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                    if text:
                        return text
        if hasattr(item, "text") and item.text:
            return item.text
    return ""


_TYPE_ALTERNATION = "|".join(COPILOT_RESPONSE_TYPES)
_USER_RESPONSE_VALUE_RE = re.compile(r'"user_response"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TYPE_VALUE_RE = re.compile(rf'"type"\s*:\s*"({_TYPE_ALTERNATION})"')
_WORKFLOW_DELIVERY_CLAIM_PATTERNS = [
    re.compile(r"\bhere(?:'|’)?s\s+(?:the|a)\s+workflow\b", re.IGNORECASE),
    re.compile(r"\b(?:i(?:'|’)?ve|i\s+have)\s+drafted\b.{0,80}\bworkflow\b", re.IGNORECASE),
    re.compile(r"\b(?:created|built|drafted|generated)\s+(?:a|the)\s+(?:draft\s+)?workflow\b", re.IGNORECASE),
    re.compile(r"\byour\s+workflow\s+(?:is\s+)?(?:ready|complete|completed|set\s+up)\b", re.IGNORECASE),
    re.compile(r"\bworkflow\s+(?:is\s+)?(?:ready|complete|completed)\b", re.IGNORECASE),
]


def _try_loads_dict(text: str) -> dict[str, Any] | None:
    # strict=False allows literal control characters in string values (SKY-9189)
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _strip_markdown_code_fence(text: str) -> str:
    cleaned = text.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.removesuffix("```")
    return cleaned.strip()


def _strip_structured_response_label(text: str) -> str | None:
    text_upper = text.upper()
    for response_type in sorted(COPILOT_RESPONSE_TYPES, key=len, reverse=True):
        if not text_upper.startswith(response_type):
            continue
        remainder = text[len(response_type) :]
        if not remainder:
            continue
        stripped = remainder.lstrip()
        if not stripped:
            continue
        if stripped[0] in {":", ","}:
            stripped = stripped[1:].lstrip()
        elif not remainder[0].isspace():
            continue
        candidate = _strip_markdown_code_fence(stripped)
        if candidate.startswith("{"):
            return candidate
    return None


def _looks_like_envelope(parsed: dict[str, Any]) -> bool:
    if "user_response" in parsed:
        return True
    # bare {"type": "object"} (a JSON schema in prose) is not an envelope
    type_value = parsed.get("type")
    return isinstance(type_value, str) and type_value.upper() in COPILOT_RESPONSE_TYPES


def _text_looks_envelope_shaped(text: str) -> bool:
    # require leading `{` so prose that merely quotes the field names (e.g.,
    # "I see \"type\": \"REPLY\" but cannot find \"user_response\"") falls
    # through to the plain-text tier instead of degrading to "Done."
    return text.startswith("{") and '"user_response"' in text and bool(_TYPE_VALUE_RE.search(text))


def _sniff_response_type(text: str) -> str:
    # REPLACE_WORKFLOW is demoted to REPLY: recovery cannot extract a usable
    # workflow_yaml, and announcing an update without one is worse than silent.
    match = _TYPE_VALUE_RE.search(text)
    if match and match.group(1).upper() == "ASK_QUESTION":
        return "ASK_QUESTION"
    return "REPLY"


def parse_final_response(text: str) -> dict[str, Any]:
    """Parse the agent's final JSON envelope, tolerating markdown code fences,
    leading action labels (``REPLY {...}``), prose preambles, and literal
    control characters in string values. Falls back to regex-extracting
    ``user_response`` from envelope-shaped text so a malformed envelope never
    reaches the chat bubble."""
    cleaned = _strip_markdown_code_fence(text)

    parsed = _try_loads_dict(cleaned)
    if parsed is not None:
        return parsed

    label_stripped = _strip_structured_response_label(cleaned)
    if label_stripped is not None:
        parsed = _try_loads_dict(label_stripped)
        if parsed is not None:
            return parsed
        cleaned = label_stripped

    first = cleaned.find("{")
    last = cleaned.rfind("}")
    # skip when the slice equals the full string — _try_loads_dict above already tried it
    if first != -1 and last > first and not (first == 0 and last == len(cleaned) - 1):
        parsed = _try_loads_dict(cleaned[first : last + 1])
        if parsed is not None and _looks_like_envelope(parsed):
            return parsed

    if _text_looks_envelope_shaped(cleaned):
        sniffed_type = _sniff_response_type(cleaned)
        match = _USER_RESPONSE_VALUE_RE.search(cleaned)
        if match:
            try:
                value = json.loads(f'"{match.group(1)}"', strict=False)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, str):
                return {"type": sniffed_type, "user_response": value}
        return {"type": sniffed_type, "user_response": "Done."}

    return {"type": "REPLY", "user_response": text}


def looks_like_workflow_delivery_claim(text: Any) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    return any(pattern.search(text) for pattern in _WORKFLOW_DELIVERY_CLAIM_PATTERNS)


# A `block_type:` line whose value is a real BlockType, or a `workflow_definition:`
# line — both keyed to canonical identifiers and anchored at line start, so inline
# prose ("the block_type field") cannot trip them. The optional quote group also
# matches the JSON serialization (`"block_type": "navigation"`).
_BLOCK_TYPE_LINE_RE = re.compile(
    r'^\s*-?\s*["\']?block_type["\']?\s*:\s*["\']?(?:' + "|".join(re.escape(bt.value) for bt in BlockType) + r")\b",
    re.MULTILINE,
)
_WORKFLOW_DEFINITION_LINE_RE = re.compile(r'^\s*["\']?workflow_definition["\']?\s*:', re.MULTILINE)


def looks_like_workflow_yaml_in_chat(text: Any) -> bool:
    """Return True when ``text`` contains serialized Skyvern workflow YAML/JSON."""
    if not isinstance(text, str):
        return False
    if "block_type" not in text and "workflow_definition" not in text:
        return False
    return bool(_WORKFLOW_DEFINITION_LINE_RE.search(text) or _BLOCK_TYPE_LINE_RE.search(text))


def extract_screenshot_b64(result: dict[str, Any]) -> str | None:
    """Extract screenshot_base64 from a copilot result dict, if present."""
    data = result.get("data")
    if isinstance(data, dict):
        return data.get("screenshot_base64")
    return None


def is_valid_image_base64(value: str | None) -> bool:
    """Return True if value looks like valid base64-encoded PNG or JPEG data."""
    if not value or not isinstance(value, str) or len(value) < 100:
        return False
    try:
        header = base64.b64decode(value[:24], validate=True)
        return header[:8] == _PNG_SIGNATURE or header[:3] == _JPEG_PREFIX
    except (binascii.Error, ValueError):
        return False


def _summarize_extracted_data(extracted: Any) -> str:
    """Summarize extracted data to prevent the LLM from echoing raw values."""
    if isinstance(extracted, list):
        if not extracted:
            return "Extracted empty list."
        if isinstance(extracted[0], dict):
            keys = sorted(extracted[0].keys())
            return f"Extracted {len(extracted)} items. Keys: {', '.join(keys)}"
        return f"Extracted list with {len(extracted)} items."
    if isinstance(extracted, dict):
        keys = sorted(extracted.keys())
        return f"Extracted object with keys: {', '.join(keys)}"
    if isinstance(extracted, str):
        return f"Extracted text ({len(extracted)} chars)."
    return "Extracted data present."


def _append_omission(notices: list[str], notice: str) -> None:
    if notice not in notices:
        notices.append(notice)


def _bounded_packet_string(
    value: str | None,
    *,
    field_name: str,
    max_chars: int,
    notices: list[str],
) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    _append_omission(notices, f"{field_name} shortened at {max_chars} characters.")
    return value[: max_chars - 3] + "..."


def _bounded_packet_strings(
    values: list[str],
    *,
    field_name: str,
    max_items: int,
    max_chars: int | None,
    notices: list[str],
) -> list[str]:
    bounded = values[:max_items]
    if len(values) > max_items:
        _append_omission(notices, f"{field_name} shortened: {len(values) - max_items} item(s) omitted.")
    if max_chars is None:
        return bounded
    rendered: list[str] = []
    shortened = 0
    for value in bounded:
        if len(value) <= max_chars:
            rendered.append(value)
            continue
        rendered.append(value[: max_chars - 3] + "...")
        shortened += 1
    if shortened:
        _append_omission(notices, f"{field_name} shortened: {shortened} text value(s) clipped.")
    return rendered


def _bounded_failed_operation(
    operation: BuildTestFailedOperation | None,
    notices: list[str],
) -> BuildTestFailedOperation | None:
    if operation is None:
        return None
    return operation.model_copy(
        update={
            "workflow_run_id": _bounded_packet_string(
                operation.workflow_run_id,
                field_name="failure.failed_operation.workflow_run_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "workflow_run_block_id": _bounded_packet_string(
                operation.workflow_run_block_id,
                field_name="failure.failed_operation.workflow_run_block_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "block_label": _bounded_packet_string(
                operation.block_label,
                field_name="failure.failed_operation.block_label",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
        }
    )


def _bounded_connect_failure(
    failure: BuildTestConnectFailure | None,
    notices: list[str],
) -> BuildTestConnectFailure | None:
    if failure is None:
        return None
    return failure.model_copy(
        update={
            "workflow_run_id": _bounded_packet_string(
                failure.workflow_run_id,
                field_name="failure.connect_failure.workflow_run_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "workflow_run_block_id": _bounded_packet_string(
                failure.workflow_run_block_id,
                field_name="failure.connect_failure.workflow_run_block_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "task_id": _bounded_packet_string(
                failure.task_id,
                field_name="failure.connect_failure.task_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "browser_session_id": _bounded_packet_string(
                failure.browser_session_id,
                field_name="failure.connect_failure.browser_session_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "occupier_run_id": _bounded_packet_string(
                failure.occupier_run_id,
                field_name="failure.connect_failure.occupier_run_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
        }
    )


def _bounded_obstruction_control(
    control: PageObstructionControl,
    *,
    obstruction_index: int,
    control_index: int,
    page_prefix: str,
    notices: list[str],
) -> PageObstructionControl:
    field_prefix = f"{page_prefix}.obstructions[{obstruction_index}].visible_controls[{control_index}]"
    candidates = control.selector_candidates[:_BUILD_TEST_SELECTOR_CANDIDATE_MAX_ITEMS]
    if len(control.selector_candidates) > _BUILD_TEST_SELECTOR_CANDIDATE_MAX_ITEMS:
        _append_omission(
            notices,
            f"{field_prefix}.selector_candidates shortened: "
            f"{len(control.selector_candidates) - _BUILD_TEST_SELECTOR_CANDIDATE_MAX_ITEMS} item(s) omitted.",
        )
    bounded_candidates = [
        PageObstructionSelectorCandidate(
            selector=_bounded_packet_string(
                candidate.selector,
                field_name=f"{field_prefix}.selector_candidates[].selector",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            )
            or "",
            source=_bounded_packet_string(
                candidate.source,
                field_name=f"{field_prefix}.selector_candidates[].source",
                max_chars=40,
                notices=notices,
            )
            or "",
        )
        for candidate in candidates
    ]
    identity = control.identity
    if identity is not None:
        identity = PageObstructionIdentity(
            tag=_bounded_packet_string(
                identity.tag,
                field_name=f"{field_prefix}.identity.tag",
                max_chars=40,
                notices=notices,
            )
            or "",
            role=_bounded_packet_string(
                identity.role,
                field_name=f"{field_prefix}.identity.role",
                max_chars=40,
                notices=notices,
            )
            or "",
            label_context=_bounded_packet_string(
                identity.label_context,
                field_name=f"{field_prefix}.identity.label_context",
                max_chars=_BUILD_TEST_IDENTITY_LABEL_MAX_CHARS,
                notices=notices,
            )
            or "",
        )
    values = control.model_dump(mode="json", exclude_none=True)
    for key, value in tuple(values.items()):
        if key in {"selector_candidates", "identity"} or not isinstance(value, str):
            continue
        max_chars = _BUILD_TEST_IDENTIFIER_MAX_CHARS if key == "selector" else _BUILD_TEST_OBSTRUCTION_VALUE_MAX_CHARS
        values[key] = _bounded_packet_string(
            value,
            field_name=f"{field_prefix}.{key}",
            max_chars=max_chars,
            notices=notices,
        )
    values["selector_candidates"] = bounded_candidates
    values["identity"] = identity
    return PageObstructionControl.model_validate(values)


def _bounded_page_obstructions(
    obstructions: list[PageObstruction], notices: list[str], *, page_prefix: str
) -> list[PageObstruction]:
    bounded_obstructions = obstructions[:_BUILD_TEST_OBSTRUCTION_MAX_ITEMS]
    if len(obstructions) > _BUILD_TEST_OBSTRUCTION_MAX_ITEMS:
        _append_omission(
            notices,
            f"{page_prefix}.obstructions shortened: "
            f"{len(obstructions) - _BUILD_TEST_OBSTRUCTION_MAX_ITEMS} item(s) omitted.",
        )
    projected: list[PageObstruction] = []
    for obstruction_index, obstruction in enumerate(bounded_obstructions):
        field_prefix = f"{page_prefix}.obstructions[{obstruction_index}]"
        controls = obstruction.visible_controls[:_BUILD_TEST_OBSTRUCTION_CONTROL_MAX_ITEMS]
        if len(obstruction.visible_controls) > _BUILD_TEST_OBSTRUCTION_CONTROL_MAX_ITEMS:
            _append_omission(
                notices,
                f"{field_prefix}.visible_controls shortened: "
                f"{len(obstruction.visible_controls) - _BUILD_TEST_OBSTRUCTION_CONTROL_MAX_ITEMS} item(s) omitted.",
            )
        values = obstruction.model_dump(mode="json", exclude_none=True)
        for key, value in tuple(values.items()):
            if key in {"visible_controls", "underlying_page_blocked"} or not isinstance(value, str):
                continue
            max_chars = (
                _BUILD_TEST_IDENTIFIER_MAX_CHARS if key == "selector" else _BUILD_TEST_OBSTRUCTION_VALUE_MAX_CHARS
            )
            values[key] = _bounded_packet_string(
                value,
                field_name=f"{field_prefix}.{key}",
                max_chars=max_chars,
                notices=notices,
            )
        values["visible_controls"] = [
            _bounded_obstruction_control(
                control,
                obstruction_index=obstruction_index,
                control_index=control_index,
                page_prefix=page_prefix,
                notices=notices,
            )
            for control_index, control in enumerate(controls)
        ]
        projected.append(PageObstruction.model_validate(values))
    return projected


def _bounded_locator_observations(
    observations: list[BuildTestPacketLocatorObservation], notices: list[str]
) -> list[BuildTestPacketLocatorObservation]:
    kept = observations[:_BUILD_TEST_LOCATOR_OBSERVATION_MAX_ITEMS]
    if len(observations) > len(kept):
        _append_omission(
            notices,
            f"failure.locator_observations shortened: {len(observations) - len(kept)} item(s) omitted.",
        )
    bounded = []
    for observation in kept:
        observed_candidates = observation.observed_candidates or []
        candidates = observed_candidates[:_BUILD_TEST_LOCATOR_CANDIDATE_MAX_ITEMS]
        if len(observed_candidates) > len(candidates):
            _append_omission(
                notices,
                "failure.locator_observations[].observed_candidates shortened: "
                f"{len(observed_candidates) - len(candidates)} item(s) omitted.",
            )
        bounded.append(
            observation.model_copy(
                update={
                    # A silently clipped selector reads as an exact observed identity and gets
                    # authored back as a broken locator, so truncation is announced.
                    "authored_selector": _bounded_packet_string(
                        observation.authored_selector,
                        field_name="failure.locator_observations[].authored_selector",
                        max_chars=_BUILD_TEST_LOCATOR_SELECTOR_MAX_CHARS,
                        notices=notices,
                    ),
                    "observed_candidates": (
                        [
                            _bounded_packet_string(
                                candidate,
                                field_name="failure.locator_observations[].observed_candidates[]",
                                max_chars=_BUILD_TEST_LOCATOR_SELECTOR_MAX_CHARS,
                                notices=notices,
                            )
                            for candidate in candidates
                        ]
                        if observation.observed_candidates is not None
                        else None
                    ),
                }
            )
        )
    return bounded


def _bounded_packet_page_state(
    page_state: BuildTestPacketPageState | None, notices: list[str], *, field_prefix: str
) -> BuildTestPacketPageState | None:
    if page_state is None:
        return None
    updates = {
        "current_origin": _bounded_packet_string(
            page_state.current_origin,
            field_name=f"{field_prefix}.current_origin",
            max_chars=_BUILD_TEST_URL_MAX_CHARS,
            notices=notices,
        ),
        "current_url": _bounded_packet_string(
            page_state.current_url,
            field_name=f"{field_prefix}.current_url",
            max_chars=_BUILD_TEST_URL_MAX_CHARS,
            notices=notices,
        ),
        "title": _bounded_packet_string(
            page_state.title,
            field_name=f"{field_prefix}.title",
            max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
            notices=notices,
        ),
        "evidence_source": _bounded_packet_string(
            page_state.evidence_source,
            field_name=f"{field_prefix}.evidence_source",
            max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
            notices=notices,
        ),
        "rendered_value_excerpt": _bounded_packet_string(
            page_state.rendered_value_excerpt,
            field_name=f"{field_prefix}.rendered_value_excerpt",
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "form_summaries": _bounded_packet_strings(
            page_state.form_summaries,
            field_name=f"{field_prefix}.form_summaries",
            max_items=_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "result_summaries": _bounded_packet_strings(
            page_state.result_summaries,
            field_name=f"{field_prefix}.result_summaries",
            max_items=_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "action_summaries": _bounded_packet_strings(
            page_state.action_summaries,
            field_name=f"{field_prefix}.action_summaries",
            max_items=_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "challenge_summaries": _bounded_packet_strings(
            page_state.challenge_summaries,
            field_name=f"{field_prefix}.challenge_summaries",
            max_items=_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "obstruction_summaries": _bounded_packet_strings(
            page_state.obstruction_summaries,
            field_name=f"{field_prefix}.obstruction_summaries",
            max_items=_BUILD_TEST_PAGE_SUMMARY_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        ),
        "obstructions": _bounded_page_obstructions(page_state.obstructions, notices, page_prefix=field_prefix),
    }
    return page_state.model_copy(update=updates)


def _compact_packet_for_aggregate_limit(
    packet: BuildTestEvidencePacket,
    notices: list[str],
) -> BuildTestEvidencePacket:
    _append_omission(
        notices,
        f"repeated packet facts shortened further to keep the packet under {_BUILD_TEST_PACKET_MAX_CHARS} characters.",
    )
    outputs: list[BuildTestPacketRegisteredOutput] = []
    for output in packet.registered_outputs[:6]:
        rendered = json.dumps(output.output, ensure_ascii=False, separators=(",", ":"))
        outputs.append(
            output.model_copy(
                update={
                    "output": rendered[:197] + "..." if len(rendered) > 200 else output.output,
                    "value_complete": output.value_complete and len(rendered) <= 200,
                }
            )
        )

    def compact_page_state(
        page_state: BuildTestPacketPageState | None, *, field_prefix: str
    ) -> BuildTestPacketPageState | None:
        if page_state is None:
            return None
        had_obstructions = bool(page_state.obstructions)

        def compact_summaries(values: list[str]) -> list[str]:
            return [value[:117] + "..." if len(value) > 120 else value for value in values[:2]]

        compacted = page_state.model_copy(
            update={
                "form_summaries": compact_summaries(page_state.form_summaries),
                "result_summaries": compact_summaries(page_state.result_summaries),
                "action_summaries": compact_summaries(page_state.action_summaries),
                "challenge_summaries": compact_summaries(page_state.challenge_summaries),
                "obstruction_summaries": compact_summaries(page_state.obstruction_summaries),
                "obstructions": [],
            }
        )
        if had_obstructions:
            _append_omission(notices, f"{field_prefix}.obstructions omitted at the aggregate packet limit.")
        return compacted

    failure = packet.failure
    if failure is not None:
        page_state = compact_page_state(failure.page_state, field_prefix="failure.page_state")
        dropped_observations = max(len(failure.locator_observations) - 2, 0)
        failure = failure.model_copy(
            update={
                "action_trace": [
                    value[:117] + "..." if len(value) > 120 else value for value in failure.action_trace[:2]
                ],
                "page_state": page_state,
                "locator_observations": failure.locator_observations[:2],
            }
        )
        if dropped_observations:
            _append_omission(
                notices,
                f"failure.locator_observations shortened at the aggregate packet limit: "
                f"{dropped_observations} item(s) omitted.",
            )
    return packet.model_copy(
        update={
            "canonical_workflow_yaml": None,
            "canonical_workflow_yaml_complete": False,
            "attempted_block_labels": packet.attempted_block_labels[:12],
            "executed_block_labels": packet.executed_block_labels[:12],
            "action_observations": [
                value[:117] + "..." if len(value) > 120 else value for value in packet.action_observations[:2]
            ],
            "failure": failure,
            "page_state": compact_page_state(packet.page_state, field_prefix="page_state"),
            "requested_outputs": packet.requested_outputs,
            "registered_outputs": outputs,
            "downloads": packet.downloads[:6],
            "unfinished_items": packet.unfinished_items[:12],
            "omission_notices": notices,
        }
    )


def project_build_test_packet_for_llm(packet: BuildTestEvidencePacket) -> BuildTestEvidencePacket:
    """Return the one bounded model projection of a factual build-test packet."""
    notices = list(packet.omission_notices)
    workflow_yaml = packet.canonical_workflow_yaml
    workflow_complete = packet.canonical_workflow_yaml_complete
    if workflow_yaml is not None and len(workflow_yaml) > _BUILD_TEST_WORKFLOW_MAX_CHARS:
        workflow_yaml = workflow_yaml[: _BUILD_TEST_WORKFLOW_MAX_CHARS - 3] + "..."
        workflow_complete = False
        _append_omission(
            notices,
            "canonical_workflow_yaml shortened at 30000 characters; "
            "use the persisted workflow readback for full bytes.",
        )

    attempted = _bounded_packet_strings(
        packet.attempted_block_labels,
        field_name="attempted_block_labels",
        max_items=_BUILD_TEST_LABEL_MAX_ITEMS,
        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
        notices=notices,
    )
    executed = _bounded_packet_strings(
        packet.executed_block_labels,
        field_name="executed_block_labels",
        max_items=_BUILD_TEST_LABEL_MAX_ITEMS,
        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
        notices=notices,
    )
    action_observations = _bounded_packet_strings(
        packet.action_observations,
        field_name="action_observations",
        max_items=_BUILD_TEST_ACTION_TRACE_MAX_ITEMS,
        max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
        notices=notices,
    )

    requested_outputs: list[BuildTestPacketRequestedOutput] = []
    for output in packet.requested_outputs[:_BUILD_TEST_OUTPUT_MAX_ITEMS]:
        requested_outputs.append(
            output.model_copy(
                update={
                    "workflow_run_id": _bounded_packet_string(
                        output.workflow_run_id,
                        field_name="requested_outputs[].workflow_run_id",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "output_parameter_id": _bounded_packet_string(
                        output.output_parameter_id,
                        field_name="requested_outputs[].output_parameter_id",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "output_parameter_key": _bounded_packet_string(
                        output.output_parameter_key,
                        field_name="requested_outputs[].output_parameter_key",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "description": _bounded_packet_string(
                        output.description,
                        field_name="requested_outputs[].description",
                        max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
                        notices=notices,
                    ),
                    "block_label": _bounded_packet_string(
                        output.block_label,
                        field_name="requested_outputs[].block_label",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "block_type": _bounded_packet_string(
                        output.block_type,
                        field_name="requested_outputs[].block_type",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                }
            )
        )
    if len(packet.requested_outputs) > _BUILD_TEST_OUTPUT_MAX_ITEMS:
        _append_omission(
            notices,
            "requested_outputs shortened: "
            f"{len(packet.requested_outputs) - _BUILD_TEST_OUTPUT_MAX_ITEMS} item(s) omitted.",
        )

    registered_outputs: list[BuildTestPacketRegisteredOutput] = []
    for output in packet.registered_outputs[:_BUILD_TEST_OUTPUT_MAX_ITEMS]:
        rendered_output = json.dumps(output.output, ensure_ascii=False, separators=(",", ":"))
        block_output = output.output
        value_complete = output.value_complete
        if len(rendered_output) > _BUILD_TEST_OUTPUT_VALUE_MAX_CHARS:
            block_output = rendered_output[: _BUILD_TEST_OUTPUT_VALUE_MAX_CHARS - 3] + "..."
            value_complete = False
            _append_omission(
                notices,
                f"registered output {output.label or '(unlabeled)'} shortened.",
            )
        registered_outputs.append(
            output.model_copy(
                update={
                    "label": _bounded_packet_string(
                        output.label,
                        field_name="registered_outputs[].label",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "status": _bounded_packet_string(
                        output.status,
                        field_name="registered_outputs[].status",
                        max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                        notices=notices,
                    ),
                    "output": block_output,
                    "value_complete": value_complete,
                }
            )
        )
    if len(packet.registered_outputs) > _BUILD_TEST_OUTPUT_MAX_ITEMS:
        _append_omission(
            notices,
            "registered_outputs shortened: "
            f"{len(packet.registered_outputs) - _BUILD_TEST_OUTPUT_MAX_ITEMS} item(s) omitted.",
        )

    failure = packet.failure
    if failure is not None:
        action_trace = _bounded_packet_strings(
            failure.action_trace,
            field_name="failure.action_trace",
            max_items=_BUILD_TEST_ACTION_TRACE_MAX_ITEMS,
            max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
            notices=notices,
        )
        failure = failure.model_copy(
            update={
                "locator_observations": _bounded_locator_observations(failure.locator_observations, notices),
                "workflow_run_block_id": _bounded_packet_string(
                    failure.workflow_run_block_id,
                    field_name="failure.workflow_run_block_id",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "task_id": _bounded_packet_string(
                    failure.task_id,
                    field_name="failure.task_id",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "step_id": _bounded_packet_string(
                    failure.step_id,
                    field_name="failure.step_id",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "block_label": _bounded_packet_string(
                    failure.block_label,
                    field_name="failure.block_label",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "block_type": _bounded_packet_string(
                    failure.block_type,
                    field_name="failure.block_type",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "block_status": _bounded_packet_string(
                    failure.block_status,
                    field_name="failure.block_status",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "reason": _bounded_packet_string(
                    failure.reason,
                    field_name="failure.reason",
                    max_chars=_BUILD_TEST_FAILURE_REASON_MAX_CHARS,
                    notices=notices,
                ),
                "failed_operation": _bounded_failed_operation(failure.failed_operation, notices),
                "connect_failure": _bounded_connect_failure(failure.connect_failure, notices),
                "action_trace": action_trace,
                "page_state": _bounded_packet_page_state(
                    failure.page_state, notices, field_prefix="failure.page_state"
                ),
            }
        )

    downloads = [
        download.model_copy(
            update={
                "artifact_id": _bounded_packet_string(
                    download.artifact_id,
                    field_name="downloads[].artifact_id",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                "file_name": _bounded_packet_string(
                    download.file_name,
                    field_name="downloads[].file_name",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
            }
        )
        for download in packet.downloads[:_BUILD_TEST_DOWNLOAD_MAX_ITEMS]
    ]
    if len(packet.downloads) > _BUILD_TEST_DOWNLOAD_MAX_ITEMS:
        _append_omission(
            notices,
            f"downloads shortened: {len(packet.downloads) - _BUILD_TEST_DOWNLOAD_MAX_ITEMS} item(s) omitted.",
        )
    unfinished = [
        item.model_copy(
            update={
                "label": _bounded_packet_string(
                    item.label,
                    field_name="unfinished_items[].label",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
                # A declared goal path is copied back verbatim by the model, so it is bounded at
                # the ceiling the facts carry rather than the shorter identifier one.
                "output_path": _bounded_packet_string(
                    item.output_path,
                    field_name="unfinished_items[].output_path",
                    max_chars=max(_BUILD_TEST_IDENTIFIER_MAX_CHARS, _TEXT_MAX),
                    notices=notices,
                ),
                "reason_code": _bounded_packet_string(
                    item.reason_code,
                    field_name="unfinished_items[].reason_code",
                    max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                    notices=notices,
                ),
            }
        )
        for item in packet.unfinished_items[:_BUILD_TEST_UNFINISHED_MAX_ITEMS]
    ]
    if len(packet.unfinished_items) > _BUILD_TEST_UNFINISHED_MAX_ITEMS:
        _append_omission(
            notices,
            "unfinished_items shortened: "
            f"{len(packet.unfinished_items) - _BUILD_TEST_UNFINISHED_MAX_ITEMS} item(s) omitted.",
        )

    projected = packet.model_copy(
        update={
            "workflow_permanent_id": _bounded_packet_string(
                packet.workflow_permanent_id,
                field_name="workflow_permanent_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "canonical_workflow_yaml": workflow_yaml,
            "canonical_workflow_yaml_complete": workflow_complete,
            "attempted_block_labels": attempted,
            "executed_block_labels": executed,
            "action_observations": action_observations,
            "failure": failure,
            "page_state": _bounded_packet_page_state(packet.page_state, notices, field_prefix="page_state"),
            "requested_outputs": requested_outputs,
            "registered_outputs": registered_outputs,
            "downloads": downloads,
            "unfinished_items": unfinished,
            "omission_notices": notices,
        }
    )
    run = projected.run.model_copy(
        update={
            "workflow_run_id": _bounded_packet_string(
                projected.run.workflow_run_id,
                field_name="run.workflow_run_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "browser_session_id": _bounded_packet_string(
                projected.run.browser_session_id,
                field_name="run.browser_session_id",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
            "status": _bounded_packet_string(
                projected.run.status,
                field_name="run.status",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            ),
        }
    )
    screenshot = projected.screenshot.model_copy(
        update={
            "provenance": _bounded_packet_string(
                projected.screenshot.provenance,
                field_name="screenshot.provenance",
                max_chars=_BUILD_TEST_IDENTIFIER_MAX_CHARS,
                notices=notices,
            )
        }
    )
    projected = projected.model_copy(update={"run": run, "screenshot": screenshot, "omission_notices": notices})

    serialized = json.dumps(projected.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    if len(serialized) > _BUILD_TEST_PACKET_MAX_CHARS and projected.canonical_workflow_yaml is not None:
        excess = len(serialized) - _BUILD_TEST_PACKET_MAX_CHARS
        retained_chars = max(0, len(projected.canonical_workflow_yaml) - excess - 200)
        shortened_workflow = projected.canonical_workflow_yaml[:retained_chars]
        if retained_chars >= 3:
            shortened_workflow = shortened_workflow[:-3] + "..."
        _append_omission(
            notices,
            f"canonical_workflow_yaml shortened further to keep the packet under {_BUILD_TEST_PACKET_MAX_CHARS} characters.",
        )
        projected = projected.model_copy(
            update={
                "canonical_workflow_yaml": shortened_workflow or None,
                "canonical_workflow_yaml_complete": False,
                "omission_notices": notices,
            }
        )
    serialized = json.dumps(projected.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    if len(serialized) > _BUILD_TEST_PACKET_MAX_CHARS:
        projected = _compact_packet_for_aggregate_limit(projected, notices)
    # Every consumer of this projection renders it to a model, so the packet leaves here already
    # safe. Redacting the parsed structure keeps the typed facts; the text redactor, run over
    # serialized JSON, can consume a delimiter and take the whole document with it (SKY-13986).
    # Keep None-valued fields: several are required, so dropping them fails revalidation.
    redacted = redact_raw_secrets_in_object(projected.model_dump(mode="json"))
    return BuildTestEvidencePacket.model_validate(redacted)


def project_direct_test_handoff_packet_for_llm(packet: BuildTestEvidencePacket) -> BuildTestEvidencePacket:
    notices: list[str] = []
    if packet.canonical_workflow_yaml is None:
        notices.append("canonical_workflow_yaml omitted: no persisted workflow readback was recorded.")
    if packet.run.workflow_run_id is None:
        notices.append("run.workflow_run_id omitted: no workflow run was recorded for this result.")
    if packet.run.status is None:
        notices.append("run.status omitted: no recorded run status exists for this result.")
    if not packet.attempted_block_labels:
        notices.append("attempted_block_labels omitted: no block run attempt was recorded.")
    if not packet.executed_block_labels:
        notices.append("executed_block_labels omitted: no block execution was recorded.")
    if not packet.action_observations:
        notices.append("action_observations empty: no same-run typed action observation was recorded.")
    if not packet.registered_outputs:
        notices.append(
            "registered_outputs empty: no workflow run block output or registered output-parameter value was recorded."
        )
    redacted_output_count = sum(
        REDACTED_SECRET_PLACEHOLDER in json.dumps(output.output, ensure_ascii=False)
        for output in packet.registered_outputs
    )
    if redacted_output_count:
        notices.append(
            f"registered_outputs redacted {redacted_output_count} item(s) containing registered secret values."
        )
    if not packet.downloads:
        notices.append("downloads empty: no registered download artifact was recorded.")
    if not packet.screenshot.present:
        notices.append("screenshot omitted: no final or failed-block screenshot was recorded.")
    if not packet.unfinished_items:
        notices.append("unfinished_items empty: recorded outcome and workflow evidence identify none.")

    failure = packet.failure
    if failure is None:
        notices.append("failure omitted: no failed run or failed block was recorded.")
    else:
        source_page_state = failure.page_state
        page_state = None
        if source_page_state is not None:
            current_origin = safe_page_origin(source_page_state.current_origin) or safe_page_origin(
                source_page_state.current_url
            )
            if current_origin is not None or source_page_state.observed_after_workflow_run:
                page_state = source_page_state.model_copy(
                    update={
                        "current_origin": current_origin,
                        "current_url": None,
                        "title": None,
                        "evidence_source": None,
                        "rendered_value_excerpt": None,
                        "form_summaries": [],
                        "result_summaries": [],
                        "action_summaries": [],
                        "challenge_summaries": [],
                        "obstruction_summaries": [],
                        "obstructions": [],
                    }
                )
        notices.append(
            "failure diagnostic prose omitted from the direct test handoff; typed status, labels, counts, "
            "failing line, and URL-reduced origin remain when recorded."
        )
        failure = failure.model_copy(
            update={
                "reason": None,
                "error_codes": [],
                "action_trace": [],
                "page_state": page_state,
                "locator_observations": [],
            }
        )

    return project_build_test_packet_for_llm(
        packet.model_copy(
            update={
                "failure": failure,
                "omission_notices": notices,
            }
        )
    )


def _remove_registered_output_copies(data: dict[str, Any]) -> dict[str, Any]:
    """Keep the projected packet as the sole provider-visible registered-output surface."""

    registered_output_locations: set[tuple[str, str]] = set()
    for field_name in ("registered_output_parameter_values", "workflow_run_output_parameters"):
        raw_outputs = data.pop(field_name, None)
        if not isinstance(raw_outputs, list):
            continue
        for output in raw_outputs:
            if not isinstance(output, dict):
                continue
            block_label = output.get("block_label")
            output_key = output.get("output_parameter_key")
            if isinstance(block_label, str) and isinstance(output_key, str):
                registered_output_locations.add((block_label, output_key))

    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return data
    sanitized_blocks: list[Any] = []
    for raw_block in blocks:
        if not isinstance(raw_block, dict):
            sanitized_blocks.append(raw_block)
            continue
        block = dict(raw_block)
        recorded_output_present = "output" in block
        block.pop("output", None)
        block_label = block.get("label")
        extracted_data = block.get("extracted_data")
        if recorded_output_present:
            block.pop("extracted_data", None)
        elif isinstance(block_label, str) and isinstance(extracted_data, dict):
            registered_keys = {
                output_key
                for registered_label, output_key in registered_output_locations
                if registered_label == block_label
            }
            block["extracted_data"] = {
                key: value for key, value in extracted_data.items() if key not in registered_keys
            }
        sanitized_blocks.append(block)
    data["blocks"] = sanitized_blocks
    return data


def sanitize_tool_result_for_llm(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Strip large/binary fields from tool results before sending to the LLM."""
    sanitized = sanitize_generic_tool_result_for_llm(
        tool_name,
        result,
        drop_top_level_keys=(
            "action",
            "browser_context",
            "artifacts",
            "timing_ms",
            "_workflow",
            _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
            _INTERNAL_RUN_OUTCOME_RECORDED_KEY,
            _INTERNAL_GOAL_PATH_OMISSIONS_KEY,
        ),
        drop_data_keys=("sdk_equivalent", "authored_locator_observations"),
        replacement_fields={"screenshot_base64": _BASE64_IMAGE_OMITTED_MESSAGE},
    )

    data = sanitized.get("data")
    if isinstance(data, dict):
        data = dict(data)
        had_build_test_packet = isinstance(data.get(BUILD_TEST_PACKET_KEY), dict)
        if had_build_test_packet:
            data = _remove_registered_output_copies(data)
        if "schema" in data and isinstance(data["schema"], dict):
            schema_str = json.dumps(data["schema"])
            # 2000 chars ~= 500 LLM tokens — enough for the model to see the
            # overall shape without consuming a meaningful slice of the prompt
            # budget. Over this, point the model at get_block_schema instead.
            # get_block_schema is exempt: the schema is what it was called for, and the steering
            # message names the call that produced it, so truncating here leaves the model no route
            # to the fields it asked for.
            if len(schema_str) > 2000 and tool_name != "get_block_schema":
                data["schema"] = {
                    "_truncated": True,
                    "message": (
                        f"Schema too large ({len(schema_str)} chars). Use get_block_schema for the specific block type."
                    ),
                }
        data.pop("sdk_equivalent", None)
        if tool_name in {"run_blocks_and_collect_debug", "edit_block_and_run"}:
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {**block, "extracted_data": _summarize_extracted_data(block["extracted_data"])}
                    if isinstance(block, dict) and "extracted_data" in block
                    else block
                    for block in blocks
                ]
        if tool_name in {"get_run_results", "run_blocks_and_collect_debug", "edit_block_and_run"}:
            # _attach_failed_block_screenshots puts base64 bytes on each failed block. They would
            # otherwise flow straight into the LLM context as raw image data — strip them while
            # preserving the existence signal. The image itself reaches the model through
            # data.screenshot_base64; leaving the bytes here also crowds out later fields such as
            # final_url under downstream truncation.
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {
                        **{
                            key: value
                            for key, value in block.items()
                            if key not in {"action_trace", "reasoning", "element"}
                        },
                        **({"screenshot_b64": _BASE64_IMAGE_OMITTED_MESSAGE} if "screenshot_b64" in block else {}),
                    }
                    if isinstance(block, dict)
                    else block
                    for block in blocks
                ]
        if tool_name == "get_run_results" and not had_build_test_packet:
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {**block, "output": truncate_output(block["output"])}
                    if isinstance(block, dict) and "output" in block
                    else block
                    for block in blocks
                ]
        repair_context = data.get("authoring_repair_context")
        if isinstance(repair_context, dict):
            repair_context = dict(repair_context)
            repair_context.pop("page_obstructions", None)
            repair_context.pop("page_obstruction_omission_notices", None)
            data["authoring_repair_context"] = repair_context
        raw_packet = data.get(BUILD_TEST_PACKET_KEY)
        if isinstance(raw_packet, dict):
            try:
                packet = BuildTestEvidencePacket.model_validate(raw_packet)
            except ValueError:
                data.pop(BUILD_TEST_PACKET_KEY, None)
                data["build_test_packet_omitted"] = "The internal packet failed typed validation."
            else:
                try:
                    projected_packet = project_build_test_packet_for_llm(packet).model_dump(
                        mode="json", exclude_none=True
                    )
                except Exception:
                    LOG.exception("copilot build test packet projection failed")
                    data.pop(BUILD_TEST_PACKET_KEY, None)
                    data["build_test_packet_omitted"] = "The internal packet projection failed."
                else:
                    data = {
                        BUILD_TEST_PACKET_KEY: projected_packet,
                        **{key: value for key, value in data.items() if key != BUILD_TEST_PACKET_KEY},
                    }
        if had_build_test_packet:
            # The bounded typed packet is the sole provider-visible action-observation
            # surface. If projection fails, omit the raw copies along with the invalid
            # packet: they can contain target-derived responses, descriptions, elements,
            # and values registered for secret scrubbing during finalization.
            data.pop("action_observations", None)
            data.pop("action_trace_summary", None)
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {
                        key: value
                        for key, value in block.items()
                        if key not in {"action_trace", "reasoning", "element", "response", "description"}
                    }
                    if isinstance(block, dict)
                    else block
                    for block in blocks
                ]
            sanitized = {"data": data, **{key: value for key, value in sanitized.items() if key != "data"}}
        else:
            # Without a packet the summary is the only action surface left, and nothing upstream
            # bounds it; hold it to the same per-line budget the packet projection applies.
            summary = data.get("action_trace_summary")
            if isinstance(summary, list):
                data["action_trace_summary"] = _bounded_packet_strings(
                    [line for line in summary if isinstance(line, str)],
                    field_name="action_trace_summary",
                    max_items=_BUILD_TEST_ACTION_TRACE_MAX_ITEMS,
                    max_chars=_BUILD_TEST_PAGE_SUMMARY_MAX_CHARS,
                    notices=[],
                )
            sanitized["data"] = data
    return sanitized


def iter_failure_reasons(result: dict[str, Any]) -> Iterator[str]:
    """Yield non-empty failure_reason strings from a copilot tool result:
    run-level ``data.failure_reason`` first, then each block's ``failure_reason``
    in order. Callers that only need the first match should wrap with ``next``."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return
    run_level = data.get("failure_reason")
    if isinstance(run_level, str) and run_level:
        yield run_level
    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            reason = block.get("failure_reason")
            if isinstance(reason, str) and reason:
                yield reason


_UNKNOWN_ERROR_SENTINEL = "Unknown error"
_USER_FACING_SUMMARY_KEYS: tuple[str, ...] = ("user_facing_summary", "user_facing_reason")
_STRUCTURED_UNSAFE_FALLBACK = "Couldn't complete that step."


def _extract_failure_message(result: dict[str, Any]) -> str:
    """Prefer top-level ``error`` over nested failure_reason fields. Defense
    in depth: _run_blocks_and_collect_debug now populates ``error`` on
    failure, but other tool return shapes may still omit it."""
    top = result.get("error")
    if isinstance(top, str) and top:
        return top
    return next(iter_failure_reasons(result), _UNKNOWN_ERROR_SENTINEL)


def _result_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _clean_structured_user_facing_text(value: Any, *, blocked_tool: str | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    # This branch returns straight to the caller without passing through
    # _sanitize_failure_text, so it needs its own credential-id redaction.
    cleaned = " ".join(_CREDENTIAL_ID_RE.sub("[credential]", value).split())
    if not cleaned:
        return None
    try:
        assert_clean_user_facing_text(cleaned, blocked_tool=blocked_tool)
    except ValueError:
        return None
    return cleaned


def _blocker_signal_matches_result(signal: CopilotToolBlockerSignal, result: dict[str, Any]) -> bool:
    error = result.get("error")
    if not isinstance(error, str) or not error:
        return False
    steering = signal.agent_steering_text
    if error == steering or steering in error:
        return True
    return False


def _iter_blocker_signals(
    blocker_signal: CopilotToolBlockerSignal | Iterable[CopilotToolBlockerSignal] | None,
) -> Iterator[CopilotToolBlockerSignal]:
    if isinstance(blocker_signal, CopilotToolBlockerSignal):
        yield blocker_signal
        return
    if blocker_signal is None:
        return
    for signal in blocker_signal:
        if isinstance(signal, CopilotToolBlockerSignal):
            yield signal


def _structured_failure_summary_for_user(
    result: dict[str, Any],
    *,
    blocker_signal: CopilotToolBlockerSignal | Iterable[CopilotToolBlockerSignal] | None = None,
    blocked_tool: str | None = None,
) -> str | None:
    if result.get("ok", False):
        return None

    for signal in _iter_blocker_signals(blocker_signal):
        if _blocker_signal_matches_result(signal, result):
            return (
                _clean_structured_user_facing_text(
                    signal.user_facing_reason,
                    blocked_tool=signal.blocked_tool or blocked_tool,
                )
                or _STRUCTURED_UNSAFE_FALLBACK
            )

    data = _result_data(result)
    saw_structured_summary = False
    for container in (result, data):
        for key in _USER_FACING_SUMMARY_KEYS:
            raw_value = container.get(key)
            if isinstance(raw_value, str) and raw_value.strip():
                saw_structured_summary = True
            summary = _clean_structured_user_facing_text(raw_value, blocked_tool=blocked_tool)
            if summary is not None:
                return summary
    if saw_structured_summary:
        return _STRUCTURED_UNSAFE_FALLBACK

    return None


# Blocker kinds where the tool was redirected before it ran (a precondition/authority
# gate), not a real failure. `tool_error` and `loop_detected` keep failure affect —
# something actually broke or the agent is stuck.
_NEUTRAL_REDIRECT_BLOCKER_KINDS = frozenset({"authority_denied"})


def user_facing_success(
    result: dict[str, Any],
    *,
    blocker_signal: CopilotToolBlockerSignal | Iterable[CopilotToolBlockerSignal] | None = None,
) -> bool:
    """Whether a tool result should render without failure affect in the user-facing
    activity stream. A raw ``ok=False`` still counts as success here when it's explained
    by a precondition/authority blocker signal — the agent was redirected, not broken."""
    if result.get("ok", True):
        return True
    # A run waiting on a human approval is the designed outcome of a human_interaction block, not a
    # break, so it must not stream with failure affect.
    data = result.get("data")
    if isinstance(data, dict) and (data.get("control_signal") or {}).get("kind") == "watchdog_paused":
        return True
    return any(
        signal.blocker_kind in _NEUTRAL_REDIRECT_BLOCKER_KINDS and _blocker_signal_matches_result(signal, result)
        for signal in _iter_blocker_signals(blocker_signal)
    )


_HEADERS_BLOB_RE = re.compile(r"\s*headers:\s*\{[^{}]*\}\s*", re.IGNORECASE)
_LARGE_DICT_BLOB_RE = re.compile(r"\{[^{}]{40,}\}")
# Every credential-shaped id prefix in skyvern/forge/sdk/db/id.py, so a new one does
# not quietly become renderable: cred, the vault/parameter kinds, folders, OAuth,
# and run credential selections.
_CREDENTIAL_ID_RE = re.compile(
    r"`?\b(?:cred|cp|cfld|blc|bccd|bsi|opp|azcp|asp|goac|moac|wrcs)_[A-Za-z0-9][A-Za-z0-9_-]*`?"
)
# Punctuation an LLM-authored block label may keep. Everything outside this set and
# the letter/digit categories becomes a space: a whitelist, because any quote-class
# codepoint left in a label lets it visually close the quoting around it and append a
# fabricated verdict to the row. Modifier letters (Lm) are excluded despite being
# alphanumeric — U+02EE and friends are quote look-alikes.
_LABEL_ALLOWED_PUNCTUATION = frozenset(" -_./")
_LABEL_ALLOWED_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lo", "Nd"})


def sanitize_block_label_for_display(label: str, max_chars: int = 40) -> str:
    """Make an LLM-authored block label safe to interpolate into a feed row."""
    cleaned = "".join(
        char if unicodedata.category(char) in _LABEL_ALLOWED_CATEGORIES or char in _LABEL_ALLOWED_PUNCTUATION else " "
        for char in label
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


_MAX_JOINED_BLOCK_LABELS = 5


def _joined_block_labels(labels: list[Any], render: Callable[[object], str], *, cap: bool) -> str:
    """Render a comma list, capped for display so a many-block run stays one row.
    Uncapped for agent state, which reads the full set back."""
    shown = [render(label) for label in (labels[:_MAX_JOINED_BLOCK_LABELS] if cap else labels)]
    remaining = len(labels) - len(shown)
    joined = ", ".join(label for label in shown if label)
    return f"{joined} (+{remaining} more)" if remaining > 0 else joined


def _sanitize_failure_text(text: str, max_chars: int = 120) -> str:
    """Strip dict/HTTP-header dumps and cap a failure message for chat display.

    The chat activity bullet is a fact, not a data dump — we never want raw
    response headers or large JSON-looking blobs to flow into the SSE
    payload. Short, capitalised technical tokens (``ERR_NAME_NOT_RESOLVED``)
    must pass through unchanged."""
    text = _HEADERS_BLOB_RE.sub(" ", text)
    text = _LARGE_DICT_BLOB_RE.sub("{...}", text)
    text = _CREDENTIAL_ID_RE.sub("[credential]", text)
    text = " ".join(text.split())
    if not text:
        return "(no details)"
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def _describe_value_shape(value: Any) -> str:
    """Describe the shape of a JS evaluation result without echoing values.

    Distinct from ``_summarize_extracted_data``: that helper shapes data for
    the LLM context (different verb, different audience). This one phrases
    the shape for a chat activity bullet."""
    if isinstance(value, list):
        if not value:
            return "empty list"
        if isinstance(value[0], dict):
            keys = sorted(value[0].keys())
            return f"list of {len(value)} items, keys: {', '.join(keys)}"
        return f"list of {len(value)} items"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return f"object with keys: {', '.join(keys)}"
    if isinstance(value, str):
        return f"text ({len(value)} chars)"
    return "value"


def _workflow_write_phrase(data: dict[str, Any]) -> str:
    """A write stages a proposal — only the end of the turn can persist it — so the summary frame
    must not say the workflow was updated when the tool result it summarizes says ``staged``."""
    return "Staged a workflow draft" if data.get("persistence") else "Workflow updated"


def summarize_tool_result(tool_name: str, result: dict[str, Any], *, for_display: bool = False) -> str:
    """Summarize a tool result. ``for_display`` clamps LLM-authored block labels for
    the activity feed; the default leaves them verbatim because this string is parsed
    back into agent state by ``context.merge_turn_summary``."""

    def block_label(value: object) -> str:
        return sanitize_block_label_for_display(str(value)) if for_display else str(value)

    if not result.get("ok", False):
        return f"Failed: {_sanitize_failure_text(_extract_failure_message(result))}"

    raw_data = result.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}

    if tool_name == "update_workflow":
        return f"{_workflow_write_phrase(data)} ({data.get('block_count', '?')} blocks)"
    if tool_name == "update_and_run_blocks" or (tool_name == "edit_block_and_run" and data.get("skipped_run")):
        if not isinstance(raw_data, dict):
            return "OK"
        if data.get("skipped_run"):
            return f"{_workflow_write_phrase(data)} ({data.get('block_count', '?')} blocks); browser run skipped"
        # Non-skip result is run-blocks-shaped (overall_status, no block_count).
        ran = "Staged a workflow draft and ran it" if data.get("persistence") else "Updated the workflow and ran it"
        status = data.get("overall_status") or data.get("status")
        if status:
            return f"{ran}: {status}"
        return ran
    if tool_name == "list_credentials":
        if data.get("status") == "resolved":
            credential = data.get("credential")
            if isinstance(credential, dict):
                name = credential.get("name")
                if isinstance(name, str) and name:
                    safe_name = sanitize_block_label_for_display(name)
                    if safe_name:
                        return f"Found 1 credential: {safe_name}"
                return "Found 1 credential(s)"
        return f"Found {data.get('count', 0)} credential(s)"
    if tool_name == "list_integrations":
        return f"Found {data.get('count', 0)} connected integration(s)"
    if tool_name == "get_block_schema":
        if "block_types" in data:
            return f"Listed {data.get('count', '?')} block types"
        return f"Schema for {data.get('block_type', '?')}"
    if tool_name == "validate_block":
        if data.get("valid"):
            return f"Block '{block_label(data.get('label', '?'))}' is valid"
        return "Block validation failed"
    if tool_name in {"run_blocks_and_collect_debug", "edit_block_and_run"}:
        if not isinstance(raw_data, dict):
            return "Run debug completed"
        raw_executed = data.get("executed_block_labels") or [b.get("label", "?") for b in data.get("blocks", [])]
        executed = _joined_block_labels(raw_executed, block_label, cap=for_display)
        status = data.get("overall_status", "?")
        requested = data.get("requested_block_labels") or []
        if requested and raw_executed and list(raw_executed) != list(requested):
            skipped = _joined_block_labels(
                [label for label in requested if label not in set(raw_executed)], block_label, cap=for_display
            )
            suffix = f" (skipped prefix from cache: {skipped})" if skipped else ""
            return f"Run {executed}: {status}{suffix}"
        return f"Run {executed}: {status}"
    if tool_name == "get_browser_screenshot":
        url = data.get("url")
        return f"Screenshot taken ({url[:80]})" if url else "Screenshot taken"
    if tool_name == "navigate_browser":
        url = result.get("url") or data.get("url", "?")
        return f"Navigated to {url[:80]}"
    if tool_name == "evaluate":
        result_val = data.get("result")
        if result_val is None:
            return "Evaluated JavaScript"
        return f"Evaluated JavaScript — returned {_describe_value_shape(result_val)}"
    if tool_name == "click":
        target = (
            data.get("effective_target")
            or data.get("selector")
            or data.get("executed_selector")
            or data.get("resolved_selector")
            or "?"
        )
        return f"Clicked '{target}'"
    if tool_name == "type_text":
        length = data.get("typed_length") or data.get("text_length", "?")
        target = data.get("effective_target") or data.get("selector") or data.get("executed_selector") or "?"
        return f"Typed {length} chars into '{target}'"
    if tool_name == "scroll":
        return f"Scrolled {data.get('direction', '?')}"
    if tool_name == "console_messages":
        count = data.get("count", 0)
        return f"Read {count} console message(s)"
    if tool_name == "select_option":
        return f"Selected '{data.get('value', '?')}'"
    if tool_name == "press_key":
        return f"Pressed '{data.get('key', '?')}'"
    if tool_name == "discover_workflow_entrypoint":
        candidate_url = data.get("candidate_url")
        if isinstance(candidate_url, str) and candidate_url:
            return f"Found the entry page: {candidate_url[:80]}"
        return "No entry page found"
    if tool_name == "inspect_page_for_composition":
        raw_forms = data.get("forms")
        forms = raw_forms if isinstance(raw_forms, list) else []
        field_count = sum(len(f.get("fields") or []) for f in forms if isinstance(f, dict))
        if field_count:
            return f"Inspected the page ({field_count} form field(s))"
        return "Inspected the page"
    return "OK"


def build_run_blocks_response(run_ok: bool, result_data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a run-blocks result, promoting the first failure reason to a top-level ``error``."""
    response: dict[str, Any] = {"ok": run_ok, "data": result_data}
    if not run_ok:
        response["error"] = next(iter_failure_reasons(response), "Unknown error (no failure reason provided)")
    return response


def summarize_tool_result_detail(
    result: dict[str, Any],
    max_chars: int = 800,
    *,
    tool_name: str | None = None,
    blocker_signal: CopilotToolBlockerSignal | Iterable[CopilotToolBlockerSignal] | None = None,
    success: bool | None = None,
) -> str | None:
    """Tooltip-grade failure detail (longer cap than ``summarize_tool_result``); None on success.

    ``success`` lets a caller pass the already-reclassified value (e.g. a phase/authority
    redirect that ``user_facing_success`` upgraded from raw ``ok: false``) so this field
    doesn't contradict the row's own success flag — same override shape as
    ``narration.extract_tool_details``.
    """
    if result.get("ok", False) if success is None else success:
        return None
    structured = _structured_failure_summary_for_user(result, blocker_signal=blocker_signal, blocked_tool=tool_name)
    if structured is not None:
        return structured
    failure_message = _extract_failure_message(result)
    # Same internal-validator convention _translate_failure_for_user maps to the generic
    # summary — the tooltip-grade detail must not leak the raw text either.
    if any(marker in failure_message.lower() for marker in _INTERNAL_VALIDATION_MARKERS):
        return _USER_FACING_GENERIC_FAILURE
    return _sanitize_failure_text(failure_message, max_chars=max_chars)


_JINJA_ERROR_MARKERS: tuple[str, ...] = ("Failed to format jinja", "Jinja style parameter")
# Markers are matched against a lower-cased copy of the error.
_ENGINE_INSTRUCTION_MARKERS: tuple[str, ...] = (
    "invalid selector:",
    "do not use ",
    "jquery pseudo-selectors",
    "tool will not run again",
    "locator(",
    "call log:",
    "waiting for locator",
)
_USE_TOOL_NAME_RE = re.compile(r"use the ['\"]?[a-z_][a-z0-9_]*['\"]? tool", re.IGNORECASE)
# Shared prefix for internal workflow-authoring validator rejects (stale block metadata,
# banned block types, missing observation evidence, raw YAML/pydantic errors). Validators
# import this constant rather than hand-typing the prefix, so a future validator can't
# silently bypass the leak-suppression below by phrasing its reject text differently.
# The full text is written for the agent to self-correct, never for the user.
_INTERNAL_VALIDATION_MARKERS: tuple[str, ...] = (INTERNAL_VALIDATION_FAILURE_PREFIX.strip(": ").lower(),)

_USER_FACING_JINJA_MESSAGE = "A workflow parameter could not be filled in."
_USER_FACING_GENERIC_FAILURE = _STRUCTURED_UNSAFE_FALLBACK

_USER_FACING_EMPTY_SUCCESS_TOOLS: frozenset[str] = frozenset(
    {
        "click",
        "type_text",
        "evaluate",
        "select_option",
        "list_credentials",
        "request_credential",
        # The server-authored display label already names the operation and its
        # target block; a bare "OK" summary would render instead of it.
        "edit_block",
        "add_block",
        "delete_block",
    }
)


def _translate_failure_for_user(error_text: str) -> str:
    if any(marker in error_text for marker in _JINJA_ERROR_MARKERS):
        return _USER_FACING_JINJA_MESSAGE
    lowered = error_text.lower()
    if any(marker in lowered for marker in _INTERNAL_VALIDATION_MARKERS):
        return _USER_FACING_GENERIC_FAILURE
    if any(marker in lowered for marker in _ENGINE_INSTRUCTION_MARKERS):
        return _USER_FACING_GENERIC_FAILURE
    if _USE_TOOL_NAME_RE.search(error_text):
        return _USER_FACING_GENERIC_FAILURE
    if error_text.strip() == _UNKNOWN_ERROR_SENTINEL:
        return _USER_FACING_GENERIC_FAILURE
    return f"Failed: {_sanitize_failure_text(error_text)}"


def format_tool_result_for_user(
    tool_name: str,
    result: dict[str, Any],
    *,
    blocker_signal: CopilotToolBlockerSignal | Iterable[CopilotToolBlockerSignal] | None = None,
) -> str:
    """SSE-bound counterpart to summarize_tool_result; do not mix the two.

    summarize_tool_result is parsed by context.merge_turn_summary for state
    extraction — rewriting it would corrupt agent state.
    """
    if not result.get("ok", False):
        structured = _structured_failure_summary_for_user(result, blocker_signal=blocker_signal, blocked_tool=tool_name)
        if structured is not None:
            return structured
        return _translate_failure_for_user(_extract_failure_message(result))
    if tool_name in _USER_FACING_EMPTY_SUCCESS_TOOLS:
        return ""
    return summarize_tool_result(tool_name, result, for_display=True)


def truncate_output(output: Any, max_chars: int = 2000) -> str | None:
    if output is None:
        return None

    if isinstance(output, str):
        text = output
    else:
        try:
            text = json.dumps(output, default=str)
        except (TypeError, ValueError):
            text = str(output)

    if len(text) > max_chars:
        return text[:max_chars] + "\n... [truncated]"
    return text
