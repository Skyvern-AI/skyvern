"""Shared output formatting helpers for copilot."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_PREFIX = b"\xff\xd8\xff"


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
        if "visible_elements_html" in data and data["visible_elements_html"]:
            html = data["visible_elements_html"]
            if len(html) > 3000:
                data["visible_elements_html"] = html[:3000] + "\n... [truncated]"
        if "schema" in data and isinstance(data["schema"], dict):
            schema_str = json.dumps(data["schema"])
            if len(schema_str) > 2000:
                data["schema"] = {
                    "_truncated": True,
                    "message": f"Schema too large ({len(schema_str)} chars). Use get_block_schema for the specific block type.",
                }
        data.pop("sdk_equivalent", None)
        sanitized["data"] = data
    sanitized.pop("_workflow", None)
    return sanitized


def summarize_tool_result(tool_name: str, result: dict[str, Any]) -> str:
    """Create a brief human-readable summary of a tool result."""
    if not result.get("ok", False):
        return f"Failed: {result.get('error', 'Unknown error')[:200]}"

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
        labels = [b.get("label", "?") for b in data.get("blocks", [])]
        return f"Run {', '.join(labels)}: {data.get('overall_status', '?')}"
    if tool_name == "get_browser_screenshot":
        return f"Screenshot taken ({data.get('url', '?')[:80]})"
    if tool_name == "navigate_browser":
        url = result.get("url") or data.get("url", "?")
        return f"Navigated to {url[:80]}"
    if tool_name == "evaluate":
        result_val = data.get("result")
        preview = str(result_val)[:100] if result_val is not None else "undefined"
        return f"JS result: {preview}"
    if tool_name == "click":
        return f"Clicked '{data.get('selector', '?')}'"
    if tool_name == "type_text":
        length = data.get("typed_length") or data.get("text_length", "?")
        return f"Typed {length} chars into '{data.get('selector', '?')}'"
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
