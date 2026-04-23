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

_TRUNCATION_HINT = (
    "Response exceeded the ~150k-char Claude tool-result limit. "
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


def truncate_response(data: Any, *, max_chars: int = MCP_MAX_RESPONSE_CHARS) -> Any:
    """Return `data` unchanged if under `max_chars`; otherwise wrap in a truncation envelope.

    Envelope shape (explicit so the model cannot miss truncation):
        {
          "_truncated": True,
          "_original_chars": <serialized size>,
          "_max_chars": <limit>,
          "_hint": "...",
          "ok": <original "ok" if present>,
          "error": <original "error" if present, bounded to 2k chars>,
          "<key>_id": <preserved identifier field if present>,
          ...
        }

    Non-dict responses that overflow are wrapped too (rare, but possible if a
    tool returns a list or string). From dict payloads, top-level keys
    `ok`/`error` plus any `*_id` identifier fields (bounded to 10 keys of up to
    256 chars each) are preserved; the oversized data is otherwise dropped.
    Identifier preservation keeps workflow/run/session IDs in the envelope so
    the caller can re-query or paginate without losing its place.

    Contract: the returned payload is always `<= max_chars` when serialized.
    Pathological inputs (e.g. a `error` field containing a full stack trace or
    HTML dump larger than the cap) are defended against by (a) capping `error`
    at `_MAX_PRESERVED_ERROR_CHARS` and (b) a final re-measure that falls back
    to a metadata-only envelope if the assembled payload is still over.
    """
    size = _response_size(data)
    if size <= max_chars:
        return data

    envelope: dict[str, Any] = {
        "_truncated": True,
        "_original_chars": size,
        "_max_chars": max_chars,
        "_hint": _TRUNCATION_HINT,
    }
    if isinstance(data, dict):
        if "ok" in data:
            envelope["ok"] = data["ok"]
        if "error" in data:
            envelope["error"] = _bound_error_value(data["error"])
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

    # Final safety net: if the preserved fields combined still blow past the
    # cap (extremely unlikely given the per-field caps above, but possible if
    # a caller passes a tiny `max_chars`), drop them and emit a metadata-only
    # envelope. Guarantees the module's "always under max_chars" contract.
    if _response_size(envelope) > max_chars:
        return {
            "_truncated": True,
            "_original_chars": size,
            "_max_chars": max_chars,
            "_hint": _TRUNCATION_HINT,
            "_envelope_rewrapped": True,
        }
    return envelope


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
    "MCP_MAX_RESPONSE_CHARS",
    "size_capped",
    "truncate_response",
]
