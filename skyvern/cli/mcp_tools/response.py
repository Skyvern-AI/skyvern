"""MCP response size enforcement.

Claude has two hard limits on tool responses:
- Claude.ai / Desktop: ~150,000 characters. `MCP_MAX_RESPONSE_CHARS` (140k)
  targets this ceiling, leaving headroom for FastMCP's jsonrpc wrapper and
  content-block metadata.
- Claude Code: roughly 25,000 tokens (~100,000 characters at English density),
  configurable upward via the `MAX_MCP_OUTPUT_TOKENS` environment variable.
  A 140k-char payload that slips through our cap can still overflow Claude
  Code's lower token limit. Claude Code users who need oversize tool output
  should either raise `MAX_MCP_OUTPUT_TOKENS` on their side or lean on the
  truncation envelope's `_hint` (paginate / narrow the query).

Exceeding either cap either truncates silently or, worse, leaves Claude without
enough context to recover. This module enforces a hard cap and wraps oversize
payloads in an explicit truncation envelope so the model knows to paginate.
"""

from __future__ import annotations

import functools
import json
import sys
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

import structlog

LOG = structlog.get_logger(__name__)

# Cap slightly under Claude.ai's 150k-char hard limit. Leaves headroom for
# the MCP envelope (jsonrpc wrapper, content-block metadata) that the FastMCP
# serializer adds on top of our dict.
MCP_MAX_RESPONSE_CHARS = 140_000
MCP_MAX_RESPONSE_BYTES = 140_000

# When truncation envelope wraps an oversize payload, preserve identifier-like
# keys (`*_id`) from the original dict so the caller retains enough context to
# re-query or paginate. Bounded to avoid re-inflating the envelope past the cap
# if a tool unexpectedly puts a huge value behind a `_id`-suffixed key.
_MAX_PRESERVED_IDENTIFIER_FIELDS = 10
_MAX_PRESERVED_IDENTIFIER_VALUE_CHARS = 256

# Bound preserved `error` payloads. Stack traces / HTML dumps returned under
# `error` can themselves exceed the cap and blow the envelope past 140k chars.
# A 2k-char ceiling keeps the structured error informative while guaranteeing
# the envelope honors its "under max_chars" contract.
_MAX_PRESERVED_ERROR_CHARS = 2_000
_MAX_PRESERVED_ERROR_PREVIEW_CHARS = 500
_MAX_PRESERVED_SCREENSHOT_PATH_CHARS = 1_024

_TRUNCATION_HINT = (
    "Response exceeded the ~150k-char Claude tool-result limit. "
    "Narrow the query (add filters, reduce page size, request specific fields) or paginate."
)
_TRUNCATION_BYTE_HINT = (
    "Response exceeded the 140k-byte aggregate MCP response limit. "
    "Narrow the query (add filters, reduce page size, request specific fields) or paginate."
)

P = ParamSpec("P")
R = TypeVar("R")


def _response_size(data: Any) -> int:
    """Return JSON-serialized size in characters.

    Fail-closed: if serialization raises (e.g. circular reference hits
    ``ValueError``), return ``sys.maxsize`` so ``truncate_response`` wraps the
    payload in the truncation envelope rather than passing through unchanged.
    An unmeasurable payload is never "small".
    """
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return sys.maxsize


def _response_size_bytes(data: Any) -> int:
    """Return the UTF-8 byte length of the JSON-serialized response."""
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str).encode())
    except (TypeError, ValueError):
        return sys.maxsize


def _bound_error_value(error: Any) -> Any:
    """Cap a preserved `error` value so the envelope cannot re-inflate past the response cap."""
    size = _response_size(error)
    if size <= _MAX_PRESERVED_ERROR_CHARS:
        return error
    # Serialize to JSON so the preview is parseable by downstream log / alert
    # consumers. `str()` would emit Python repr (single quotes, non-JSON). Fall
    # back to `str()` only if JSON serialization itself fails — in that case
    # we already could not measure the payload and the preview is best-effort.
    try:
        preview = json.dumps(error, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        preview = str(error)
    if len(preview) > _MAX_PRESERVED_ERROR_PREVIEW_CHARS:
        preview = f"{preview[:_MAX_PRESERVED_ERROR_PREVIEW_CHARS]}... [truncated]"
    return {
        "_original_error_chars": size,
        "_error_preview": preview,
        "_hint": "error payload exceeded envelope size cap; check server logs for full context",
    }


def _contains_inline_image(value: Any, seen: set[int] | None = None) -> bool:
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if (
            value.get("inline") is True
            and str(value.get("mime", "")).startswith("image/")
            and isinstance(value.get("data"), str)
        ):
            return True
        return any(_contains_inline_image(item, seen) for item in value.values())
    if isinstance(value, list):
        if id(value) in seen:
            return False
        seen.add(id(value))
        return any(_contains_inline_image(item, seen) for item in value)
    return False


def _preserved_screenshot_artifact(value: Any) -> dict[str, Any] | None:
    artifacts = value.get("artifacts") if isinstance(value, dict) else None
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") != "screenshot":
            continue
        path = artifact.get("path")
        if not isinstance(path, str) or len(path) > _MAX_PRESERVED_SCREENSHOT_PATH_CHARS:
            continue
        return {"kind": "screenshot", "path": path, "mime": "image/png"}
    return None


def _truncate_response_to_limit(
    data: Any,
    *,
    size_fn: Callable[[Any], int],
    max_size: int,
    unit: str,
    hint: str,
) -> Any:
    size = size_fn(data)
    if size <= max_size:
        return data

    original_key = f"_original_{unit}"
    max_key = f"_max_{unit}"
    envelope: dict[str, Any] = {
        "_truncated": True,
        original_key: size,
        max_key: max_size,
        "_hint": hint,
    }
    if isinstance(data, dict):
        if "ok" in data:
            envelope["ok"] = data["ok"]
        if "error" in data:
            envelope["error"] = _bound_error_value(data["error"])
        screenshot_artifact = _preserved_screenshot_artifact(data)
        if screenshot_artifact:
            envelope["artifacts"] = [screenshot_artifact]
        preserved = 0
        for key, value in data.items():
            if preserved >= _MAX_PRESERVED_IDENTIFIER_FIELDS:
                break
            if key in envelope:
                continue
            if not isinstance(key, str) or not key.endswith("_id"):
                continue
            if value is None or isinstance(value, int):
                envelope[key] = value
                preserved += 1
                continue
            if isinstance(value, str) and len(value) <= _MAX_PRESERVED_IDENTIFIER_VALUE_CHARS:
                envelope[key] = value
                preserved += 1
        if data.get("ok") is True and _contains_inline_image(data):
            envelope["ok"] = False
            envelope["error"] = {
                "code": "RESPONSE_TOO_LARGE",
                "message": "The inline screenshot exceeded the MCP response size limit and was not returned.",
                "hint": "Use the saved screenshot artifact path, or retry with inline=false.",
            }

    if size_fn(envelope) > max_size:
        return {
            "_truncated": True,
            original_key: size,
            max_key: max_size,
            "_hint": hint,
            "_envelope_rewrapped": True,
        }
    return envelope


def truncate_response(data: Any, *, max_chars: int = MCP_MAX_RESPONSE_CHARS) -> Any:
    """Cap a response by JSON-serialized characters, preserving the existing envelope contract."""
    return _truncate_response_to_limit(
        data,
        size_fn=_response_size,
        max_size=max_chars,
        unit="chars",
        hint=_TRUNCATION_HINT,
    )


def truncate_response_bytes(data: Any, *, max_bytes: int = MCP_MAX_RESPONSE_BYTES) -> Any:
    """Cap a response by aggregate UTF-8 JSON bytes."""
    return _truncate_response_to_limit(
        data,
        size_fn=_response_size_bytes,
        max_size=max_bytes,
        unit="bytes",
        hint=_TRUNCATION_BYTE_HINT,
    )


def size_capped(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[Any]]:
    """Decorator: enforce `MCP_MAX_RESPONSE_CHARS` on a tool's return value.

    Applies to async tool functions returning any JSON-serializable payload.
    Emits a structured ``mcp_response_truncated`` warning whenever the envelope
    fires so operators can see which tools are hitting the cap and tune the
    limit (or paginate upstream) rather than having the signal hidden in the
    tool response alone.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        result = await fn(*args, **kwargs)
        capped = truncate_response(result)
        if capped is not result:
            original_chars: int | None = None
            if isinstance(capped, dict):
                raw_original = capped.get("_original_chars")
                if isinstance(raw_original, int):
                    original_chars = raw_original
            LOG.warning(
                "mcp_response_truncated",
                tool=fn.__name__,
                original_chars=original_chars,
                max_chars=MCP_MAX_RESPONSE_CHARS,
            )
        return capped

    return wrapper


__all__ = [
    "MCP_MAX_RESPONSE_BYTES",
    "MCP_MAX_RESPONSE_CHARS",
    "size_capped",
    "truncate_response",
    "truncate_response_bytes",
]
