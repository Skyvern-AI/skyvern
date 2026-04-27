"""Shared output formatting helpers for copilot."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

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


def parse_final_response(text: str) -> dict[str, Any]:
    """Parse the agent's final JSON envelope, stripping markdown code fences.

    Tolerant of literal control characters (newlines/tabs/CR) inside string
    values. Some model outputs wrap long `user_response` prose across real
    newlines instead of `\\n` escapes; strict `json.loads` rejects those, and
    without this fallback the frontend renders the full raw JSON object as
    the user-visible reply (see SKY-9189 test-2 regression).
    """
    cleaned = text.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    for strict in (True, False):
        try:
            parsed = json.loads(cleaned, strict=strict)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    return {"type": "REPLY", "user_response": text}


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
    sanitized = dict(result)
    for key in ("action", "browser_context", "artifacts", "timing_ms"):
        sanitized.pop(key, None)

    data = sanitized.get("data")
    if isinstance(data, dict):
        data = dict(data)
        if "screenshot_base64" in data:
            data["screenshot_base64"] = "[base64 image omitted — screenshot was taken successfully]"
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
                    {**block, "screenshot_b64": "[base64 image omitted — screenshot was taken successfully]"}
                    if isinstance(block, dict) and "screenshot_b64" in block
                    else block
                    for block in blocks
                ]
        sanitized["data"] = data
    sanitized.pop("_workflow", None)
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


def _extract_failure_message(result: dict[str, Any]) -> str:
    """Prefer top-level ``error`` over nested failure_reason fields. Defense
    in depth: _run_blocks_and_collect_debug now populates ``error`` on
    failure, but other tool return shapes may still omit it."""
    top = result.get("error")
    if isinstance(top, str) and top:
        return top
    return next(iter_failure_reasons(result), "Unknown error")


_HEADERS_BLOB_RE = re.compile(r"\s*headers:\s*\{[^{}]*\}\s*", re.IGNORECASE)
_LARGE_DICT_BLOB_RE = re.compile(r"\{[^{}]{40,}\}")


def _sanitize_failure_text(text: str) -> str:
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
    if len(text) > 120:
        text = text[:117] + "..."
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

    data = result.get("data") or {}

    if tool_name == "update_workflow":
        return f"Workflow updated ({data.get('block_count', '?')} blocks)"
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
        if not isinstance(data, dict):
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
        return f"Clicked '{data.get('selector', '?')}'"
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
