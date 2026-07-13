"""Shared output formatting helpers for copilot."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from skyvern.forge.sdk.agents.context import sanitize_agent_tool_result_for_llm as sanitize_generic_tool_result_for_llm
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, assert_clean_user_facing_text
from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES
from skyvern.forge.sdk.copilot.failure_tracking import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
)
from skyvern.forge.sdk.copilot.loop_detection import LOOP_DETECTED_MARKER
from skyvern.schemas.workflows import BlockType

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY = "_copilot_internal_run_cancelled_by_watchdog"
_BASE64_IMAGE_OMITTED_MESSAGE = "[base64 image omitted — screenshot was taken successfully]"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_PREFIX = b"\xff\xd8\xff"


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
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
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
        ),
        drop_data_keys=("sdk_equivalent",),
        replacement_fields={"screenshot_base64": _BASE64_IMAGE_OMITTED_MESSAGE},
    )

    data = sanitized.get("data")
    if isinstance(data, dict):
        data = dict(data)
        if "schema" in data and isinstance(data["schema"], dict):
            schema_str = json.dumps(data["schema"])
            # 2000 chars ~= 500 LLM tokens — enough for the model to see the
            # overall shape without consuming a meaningful slice of the prompt
            # budget. Over this, point the model at get_block_schema instead.
            if len(schema_str) > 2000:
                data["schema"] = {
                    "_truncated": True,
                    "message": (
                        f"Schema too large ({len(schema_str)} chars). Use get_block_schema for the specific block type."
                    ),
                }
        data.pop("sdk_equivalent", None)
        if tool_name == "run_blocks_and_collect_debug":
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {**block, "extracted_data": _summarize_extracted_data(block["extracted_data"])}
                    if isinstance(block, dict) and "extracted_data" in block
                    else block
                    for block in blocks
                ]
        if tool_name == "get_run_results":
            # _attach_failed_block_screenshots puts base64 bytes on each failed
            # block. They would otherwise flow straight into the LLM context as
            # raw image data — strip them while preserving the existence signal.
            blocks = data.get("blocks")
            if isinstance(blocks, list):
                data["blocks"] = [
                    {**block, "screenshot_b64": _BASE64_IMAGE_OMITTED_MESSAGE}
                    if isinstance(block, dict) and "screenshot_b64" in block
                    else block
                    for block in blocks
                ]
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
    cleaned = " ".join(value.split())
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
    return signal.internal_reason_code == ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE and _has_failure_category(
        result, ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY
    )


def _failure_categories(result: dict[str, Any]) -> list[Any]:
    data = _result_data(result)
    categories = data.get("failure_categories")
    return categories if isinstance(categories, list) else []


def _has_failure_category(result: dict[str, Any], category: str) -> bool:
    for item in _failure_categories(result):
        if isinstance(item, dict) and item.get("category") == category:
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

    if _has_failure_category(result, PER_TOOL_BUDGET_FAILURE_CATEGORY):
        # An explicit but empty failure_reason still means the structured
        # watchdog path fired; use the generic safe copy rather than raw error
        # fallback.
        return _clean_structured_user_facing_text(data.get("failure_reason"), blocked_tool=blocked_tool) or (
            _STRUCTURED_UNSAFE_FALLBACK if isinstance(data.get("failure_reason"), str) else None
        )

    return None


# Blocker kinds where the tool was redirected before it ran (a precondition/authority
# gate), not a real failure. `tool_error` and `loop_detected` keep failure affect —
# something actually broke or the agent is stuck.
_NEUTRAL_REDIRECT_BLOCKER_KINDS = frozenset({"phase_gated", "missing_required_context", "authority_denied"})


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
    return any(
        signal.blocker_kind in _NEUTRAL_REDIRECT_BLOCKER_KINDS and _blocker_signal_matches_result(signal, result)
        for signal in _iter_blocker_signals(blocker_signal)
    )


_HEADERS_BLOB_RE = re.compile(r"\s*headers:\s*\{[^{}]*\}\s*", re.IGNORECASE)
_LARGE_DICT_BLOB_RE = re.compile(r"\{[^{}]{40,}\}")


def _sanitize_failure_text(text: str, max_chars: int = 120) -> str:
    """Strip dict/HTTP-header dumps and cap a failure message for chat display.

    The chat activity bullet is a fact, not a data dump — we never want raw
    response headers or large JSON-looking blobs to flow into the SSE
    payload. Short, capitalised technical tokens (``ERR_NAME_NOT_RESOLVED``)
    must pass through unchanged."""
    text = _HEADERS_BLOB_RE.sub(" ", text)
    text = _LARGE_DICT_BLOB_RE.sub("{...}", text)
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


def summarize_tool_result(tool_name: str, result: dict[str, Any]) -> str:
    """Create a brief human-readable summary of a tool result."""
    if not result.get("ok", False):
        return f"Failed: {_sanitize_failure_text(_extract_failure_message(result))}"

    raw_data = result.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}

    if tool_name == "update_workflow":
        return f"Workflow updated ({data.get('block_count', '?')} blocks)"
    if tool_name == "update_and_run_blocks" and data.get("skipped_run"):
        return f"Workflow updated ({data.get('block_count', '?')} blocks); browser run skipped"
    if tool_name == "list_credentials":
        return f"Found {data.get('count', 0)} credential(s)"
    if tool_name == "get_block_schema":
        if "block_types" in data:
            return f"Listed {data.get('count', '?')} block types"
        return f"Schema for {data.get('block_type', '?')}"
    if tool_name == "validate_block":
        if data.get("valid"):
            return f"Block '{data.get('label', '?')}' is valid"
        return "Block validation failed"
    if tool_name == "run_blocks_and_collect_debug":
        if not isinstance(raw_data, dict):
            return "Run debug completed"
        executed = data.get("executed_block_labels") or [b.get("label", "?") for b in data.get("blocks", [])]
        status = data.get("overall_status", "?")
        requested = data.get("requested_block_labels") or []
        if requested and executed and list(executed) != list(requested):
            skipped = [label for label in requested if label not in set(executed)]
            suffix = f" (skipped prefix from cache: {', '.join(skipped)})" if skipped else ""
            return f"Run {', '.join(executed)}: {status}{suffix}"
        return f"Run {', '.join(executed)}: {status}"
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
        target = data.get("effective_target") or data.get("selector") or data.get("resolved_selector") or "?"
        return f"Clicked '{target}'"
    if tool_name == "type_text":
        length = data.get("typed_length") or data.get("text_length", "?")
        return f"Typed {length} chars into '{data.get('selector', '?')}'"
    if tool_name == "scroll":
        return f"Scrolled {data.get('direction', '?')}"
    if tool_name == "console_messages":
        count = data.get("count", 0)
        return f"Read {count} console message(s)"
    if tool_name == "select_option":
        return f"Selected '{data.get('value', '?')}'"
    if tool_name == "press_key":
        return f"Pressed '{data.get('key', '?')}'"
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
INTERNAL_VALIDATION_FAILURE_PREFIX = "Workflow validation failed: "
_INTERNAL_VALIDATION_MARKERS: tuple[str, ...] = (INTERNAL_VALIDATION_FAILURE_PREFIX.strip(": ").lower(),)

_USER_FACING_LOOP_MESSAGE = "The agent got stuck retrying the same step — moving on."
_USER_FACING_JINJA_MESSAGE = "A workflow parameter could not be filled in."
_USER_FACING_GENERIC_FAILURE = _STRUCTURED_UNSAFE_FALLBACK

_USER_FACING_EMPTY_SUCCESS_TOOLS: frozenset[str] = frozenset({"click", "type_text", "evaluate", "select_option"})


def _translate_failure_for_user(error_text: str) -> str:
    if LOOP_DETECTED_MARKER in error_text:
        return _USER_FACING_LOOP_MESSAGE
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
    return summarize_tool_result(tool_name, result)


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
