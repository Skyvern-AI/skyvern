"""MCP response distillation and size enforcement.

Claude has two hard limits on tool responses:
- Claude.ai / Desktop: ~150,000 characters. `MCP_MAX_RESPONSE_CHARS` (140k)
  targets this ceiling, leaving headroom for FastMCP's jsonrpc wrapper and
  content-block metadata.
- Claude Code: roughly 25,000 tokens (~100,000 characters at English density),
  configurable upward via the client-side `MAX_MCP_OUTPUT_TOKENS` environment
  variable. A 140k-char payload can therefore still overflow Claude Code.

The five extraction-class tools default to full-fidelity responses. Operators
can set server-side `SKYVERN_MCP_EXTRACTION_DEFAULT_VERBOSITY=summary` before
tool registration to roll those defaults back; accepted values are `full`
(the default) and `summary`. Explicit per-call verbosity still wins, and every
response remains subject to both the character and UTF-8 byte caps.

Oversize payloads are wrapped in an explicit truncation envelope so the model
knows to paginate or narrow the request.
"""

from __future__ import annotations

import bisect
import copy
import functools
import hashlib
import inspect
import json
import os
import re
import sys
import threading
import time
from array import array
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Awaitable, Callable, Literal, ParamSpec, TypeVar
from urllib.parse import parse_qsl, urlsplit

import structlog

from skyvern.cli.mcp_tools.response_distillation import (
    _MAX_PREFIX_CHARS,
    _NO_RECOVERABLE_ANCHOR_SOURCE,
    TransformResult,
    TransformTier,
    _DuplicateKeyError,
    _reject_duplicate_keys,
    distill_value,
)

LOG = structlog.get_logger(__name__)

# Cap slightly under Claude.ai's 150k-char hard limit. Leaves headroom for
# the MCP envelope (jsonrpc wrapper, content-block metadata) that the FastMCP
# serializer adds on top of our dict.
MCP_MAX_RESPONSE_CHARS = 140_000
MCP_MAX_RESPONSE_BYTES = 140_000

EXTRACTION_TOOL_NAMES = frozenset(
    {
        "skyvern_extract",
        "skyvern_evaluate",
        "skyvern_extract_and_screenshot",
        "skyvern_evaluate_and_screenshot",
        "skyvern_navigate_extract_and_screenshot",
    }
)
COMPACT_TOOL_NAMES = frozenset(
    {
        "skyvern_network_requests",
        "skyvern_network_request_detail",
        "skyvern_har_stop",
        "skyvern_get_session_storage",
        "skyvern_workflow_run",
        "skyvern_workflow_status",
    }
)
_EXTRACTION_VERBOSITY_ENV = "SKYVERN_MCP_EXTRACTION_DEFAULT_VERBOSITY"


def extraction_default_verbosity(environ: Mapping[str, str] = os.environ) -> Literal["summary", "full"]:
    value = environ.get(_EXTRACTION_VERBOSITY_ENV, "full").strip().lower()
    if value == "full":
        return "full"
    if value == "summary":
        return "summary"
    raise ValueError(f"{_EXTRACTION_VERBOSITY_ENV} must be 'full' or 'summary', got {value!r}")


EXTRACTION_DEFAULT_VERBOSITY = extraction_default_verbosity()
FULL_DEFAULT_TOOL_NAMES = EXTRACTION_TOOL_NAMES if EXTRACTION_DEFAULT_VERBOSITY == "full" else frozenset()
SUMMARY_DEFAULT_TOOL_NAMES = COMPACT_TOOL_NAMES | (
    EXTRACTION_TOOL_NAMES if EXTRACTION_DEFAULT_VERBOSITY == "summary" else frozenset()
)

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
_MAX_ANCHOR_SCAN_CHARS = _MAX_PREFIX_CHARS
_MAX_RESPONSE_ANCHOR_VALUE_CHARS = _MAX_PRESERVED_IDENTIFIER_VALUE_CHARS
_MAX_RESPONSE_ANCHOR_SIDECAR_CHARS = 8_000
_MIN_RESPONSE_ANCHOR_SIDECAR_CHARS = 2_048
_ANCHOR_SIDECAR_BODY_MULTIPLIER = 3
_ANCHOR_VALUE_TRUNCATION = "… [truncated from {length} chars]"
_ANCHOR_KEY_VALUE_RE = re.compile(
    r"(?<![\w.-])[\"']?(?P<key>[A-Za-z_][\w.-]*)[\"']?\s*[:=]\s*"
    r"(?P<value>https?://[^\s,;\"']+|[^\s,;]+)"
)
_URL_RE = re.compile(r"https?://[^\s,;\"']+")
_CONTAINER_START_RE = re.compile(r"[\[{]")

_TRUNCATION_HINT = (
    "Response exceeded the ~150k-char Claude tool-result limit. "
    "Narrow the query (add filters, reduce page size, request specific fields) or paginate."
)
_TRUNCATION_BYTE_HINT = (
    "Response exceeded the 140k-byte aggregate MCP response limit. "
    "Narrow the query (add filters, reduce page size, request specific fields) or paginate."
)
_CONTINUATION_TTL_SECONDS = 15 * 60
_CONTINUATION_CACHE_SIZE = 8
_CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_CONTINUATION_HINT_SUFFIX = (
    " Continuations are served from a 15-minute in-process cache without re-executing the tool; "
    "request each next slice promptly. If the cache expires, restart with response_offset_chars=0."
)
_CONTINUATION_CHAR_HINT = (
    "Response exceeded the ~150k-char Claude tool-result limit. "
    "Retry with response_offset_chars set to _next_offset_chars to continue."
    f"{_CONTINUATION_HINT_SUFFIX}"
)
_CONTINUATION_BYTE_HINT = (
    "Response exceeded the 140k-byte aggregate MCP response limit. "
    "Retry with response_offset_chars set to _next_offset_chars to continue."
    f"{_CONTINUATION_HINT_SUFFIX}"
)
_CONTINUATION_BOTH_HINT = (
    "Response exceeded both the ~150k-char Claude tool-result limit and the 140k-byte aggregate MCP response limit. "
    "Retry with response_offset_chars set to _next_offset_chars to continue."
    f"{_CONTINUATION_HINT_SUFFIX}"
)
_CONTINUATION_CACHE: OrderedDict[str, tuple[float, str, int, str, dict[str, Any]]] = OrderedDict()
_CONTINUATION_CACHE_LOCK = threading.Lock()
_MEMORY_ADDRESS_RE = re.compile(r" at 0x[0-9a-fA-F]+")

P = ParamSpec("P")
R = TypeVar("R")


def _stable_cache_fallback(value: Any) -> dict[str, str]:
    """Represent non-JSON call arguments without process-unstable addresses."""
    return {
        "__type__": f"{type(value).__module__}.{type(value).__qualname__}",
        "__repr__": _MEMORY_ADDRESS_RE.sub(" at 0x…", repr(value)),
    }


def _continuation_scope_identity() -> tuple[str | None, tuple[str, str | None] | None]:
    from skyvern.forge import app

    from ._session import get_current_session

    state = get_current_session()
    try:
        agent_function = app.AGENT_FUNCTION
    except RuntimeError:
        organization_id = state.organization_id
    else:
        try:
            organization_id = agent_function.get_mcp_request_organization_id() or state.organization_id
        except ValueError:
            organization_id = state.organization_id
            if organization_id is None:
                raise ValueError("organization identity is required for request-scoped continuations") from None
    context = state.context
    if context is None:
        return organization_id, None
    resolved_id = context.session_id if context.mode == "cloud_session" else context.cdp_url
    return organization_id, (context.mode, resolved_id)


def _continuation_cache_key(
    fn: Callable[..., Any],
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    bound.arguments.pop("response_offset_chars", None)
    canonical = json.dumps(
        {
            "tool": f"{fn.__module__}.{fn.__qualname__}",
            "scope": _continuation_scope_identity(),
            "arguments": bound.arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_stable_cache_fallback,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _continuation_cache_delete(key: str) -> None:
    with _CONTINUATION_CACHE_LOCK:
        _CONTINUATION_CACHE.pop(key, None)


def _continuation_cache_get(key: str) -> tuple[str, int, str, dict[str, Any]] | None:
    now = time.monotonic()
    with _CONTINUATION_CACHE_LOCK:
        for cached_key, (expires_at, _, _, _, _) in list(_CONTINUATION_CACHE.items()):
            if expires_at <= now:
                del _CONTINUATION_CACHE[cached_key]
        cached = _CONTINUATION_CACHE.pop(key, None)
        if cached is None:
            return None
        _, serialized, total_bytes, snapshot_id, preserved = cached
        _CONTINUATION_CACHE[key] = cached
        return serialized, total_bytes, snapshot_id, preserved


def _continuation_cache_put(
    key: str,
    serialized: str,
    total_bytes: int,
    snapshot_id: str,
    preserved: dict[str, Any],
) -> None:
    with _CONTINUATION_CACHE_LOCK:
        _CONTINUATION_CACHE.pop(key, None)
        _CONTINUATION_CACHE[key] = (
            time.monotonic() + _CONTINUATION_TTL_SECONDS,
            serialized,
            total_bytes,
            snapshot_id,
            preserved,
        )
        while len(_CONTINUATION_CACHE) > _CONTINUATION_CACHE_SIZE:
            _CONTINUATION_CACHE.popitem(last=False)


def _response_size(data: Any) -> int:
    """Return JSON-serialized size in characters.

    Fail-closed: if serialization raises (e.g. circular reference hits
    ``ValueError``), return ``sys.maxsize`` so ``truncate_response`` wraps the
    payload in the truncation envelope rather than passing through unchanged.
    An unmeasurable payload is never "small".
    """
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        return sys.maxsize


def _is_anchor_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized == "id"
        or normalized.endswith(("_id", "_ids", "_status", "_code"))
        or normalized in {"status", "code"}
        or normalized == "count"
        or normalized.endswith(("_count", "_counts"))
        or normalized == "url"
        or normalized.endswith(("_url", "_urls"))
    )


def _is_sensitive_anchor_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return (
        normalized in {"authorization", "cookie", "cookies", "set_cookie", "api_key"}
        or "token" in normalized
        or "secret" in normalized
        or "password" in normalized
        or "credential" in normalized
    )


def _is_sensitive_anchor_value(key: str, value: Any) -> bool:
    if _is_sensitive_anchor_name(key):
        return True
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return False
    parsed = urlsplit(value)
    return parsed.password is not None or any(_is_sensitive_anchor_name(name) for name, _ in parse_qsl(parsed.query))


def _is_anchor_scalar(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def _bounded_anchor_value(value: Any) -> Any:
    if not isinstance(value, str) or len(value) <= _MAX_RESPONSE_ANCHOR_VALUE_CHARS:
        return value
    suffix = _ANCHOR_VALUE_TRUNCATION.format(length=len(value))
    return f"{value[: _MAX_RESPONSE_ANCHOR_VALUE_CHARS - len(suffix)]}{suffix}"


def _structured_container(value: str) -> tuple[list[Any], str]:
    scanned = value[:_MAX_ANCHOR_SCAN_CHARS].strip()
    if not scanned:
        return [], ""

    containers: list[Any] = []
    remainder: list[str] = []
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
    cursor = 0
    while cursor < len(scanned):
        match = _CONTAINER_START_RE.search(scanned, cursor)
        if match is None:
            remainder.append(scanned[cursor:])
            break
        start = match.start()
        if start > cursor:
            remainder.append(scanned[cursor:start])
        try:
            parsed, end = decoder.raw_decode(scanned, start)
        except _DuplicateKeyError:
            return [], scanned
        except (json.JSONDecodeError, RecursionError):
            remainder.append(scanned[start : start + 1])
            cursor = start + 1
            continue
        if isinstance(parsed, (dict, list)):
            containers.append(parsed)
        else:
            remainder.append(scanned[start:end])
        cursor = end

    return containers, "".join(remainder).strip()


def _response_anchors(value: Any) -> dict[str, Any]:
    keys: dict[str, bool] = {}
    values: list[list[Any]] = []
    entries: list[list[Any]] = []
    active_containers: set[int] = set()

    def append_value(key: str, item: Any, path: tuple[str, ...]) -> None:
        if _is_sensitive_anchor_value(key, item):
            return
        pair = [key, _bounded_anchor_value(item)]
        values.append(pair)
        entries.append([list(path), *pair])

    def harvest_text(text: str, nearest_key: str, path: tuple[str, ...]) -> None:
        scanned = text[:_MAX_ANCHOR_SCAN_CHARS]
        url_matches = list(_URL_RE.finditer(scanned))
        claimed_url_spans = [match.span() for match in url_matches]
        consumed_url_spans: list[tuple[int, int]] = []
        for match in _ANCHOR_KEY_VALUE_RE.finditer(scanned):
            if match.start() and scanned[match.start() - 1] == "\\":
                continue
            if any(start <= match.start("key") < end for start, end in claimed_url_spans):
                continue
            key = match.group("key")
            if not _is_anchor_key(key):
                continue
            item = match.group("value").strip("\"'").rstrip(".,)]}")
            append_value(key, item, path)
            consumed_url_spans.append(match.span("value"))
        if not (_is_anchor_key(nearest_key) or nearest_key in {"body", "content", "raw", "result", "text"}):
            return
        for match in url_matches:
            if any(start <= match.start() < end for start, end in consumed_url_spans):
                continue
            append_value(nearest_key, match.group(0).rstrip(".,)]}"), path)

    def walk(
        node: Any,
        *,
        anchor_list: bool = False,
        terminal_key: str = "_",
        path: tuple[str, ...] = (),
    ) -> None:
        if isinstance(node, dict):
            if id(node) in active_containers:
                return
            active_containers.add(id(node))
            for key, item in node.items():
                key_text = str(key)
                item_path = (*path, key_text)
                item_is_anchor = _is_anchor_key(key_text) and not _is_sensitive_anchor_name(key_text)
                if item_is_anchor:
                    keys[key_text] = True
                appended_scalar = (item_is_anchor or _is_anchor_scalar(item)) and not isinstance(item, (dict, list))
                if appended_scalar:
                    append_value(key_text, item, item_path)
                if not _is_anchor_scalar(item):
                    walk(
                        item,
                        anchor_list=item_is_anchor and isinstance(item, list),
                        terminal_key=key_text,
                        path=item_path,
                    )
            active_containers.remove(id(node))
            return
        if isinstance(node, list):
            if id(node) in active_containers:
                return
            active_containers.add(id(node))
            for index, item in enumerate(node):
                item_path = (*path, str(index))
                appended_scalar = (anchor_list or _is_anchor_scalar(item)) and not isinstance(item, (dict, list))
                if appended_scalar:
                    append_value(terminal_key, item, item_path)
                if not _is_anchor_scalar(item):
                    walk(
                        item,
                        anchor_list=anchor_list and isinstance(item, list),
                        terminal_key=terminal_key,
                        path=item_path,
                    )
            active_containers.remove(id(node))
            return
        if isinstance(node, str):
            containers, remainder = _structured_container(node)
            for parsed in containers:
                walk(parsed, terminal_key=terminal_key, path=(*path, "<parsed>"))
            if remainder:
                harvest_text(remainder, terminal_key, path)

    walk(value)
    return {"keys": keys, "values": values, "entries": entries}


def _response_size_bytes(data: Any) -> int:
    """Return the UTF-8 byte length of the JSON-serialized response."""
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str).encode())
    except Exception:
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
    except Exception:
        try:
            preview = str(error)
        except Exception:
            preview = "<unserializable error payload>"
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
    limit_error = {
        "code": "RESPONSE_TOO_LARGE",
        "message": "The response exceeded the MCP response size limit and was not returned in full.",
        "hint": hint,
    }
    base_envelope: dict[str, Any] = {
        "ok": False,
        "error": limit_error,
        "_truncated": True,
        original_key: size,
        max_key: max_size,
        "_hint": hint,
    }
    envelope = dict(base_envelope)
    if isinstance(data, dict):
        try:
            if data.get("ok") is not True and data.get("error") is not None:
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
                envelope["error"] = {
                    "code": "RESPONSE_TOO_LARGE",
                    "message": "The inline screenshot exceeded the MCP response size limit and was not returned.",
                    "hint": "Use the saved screenshot artifact path, or retry with inline=false.",
                }
        except Exception:
            envelope = dict(base_envelope)

    if size_fn(envelope) > max_size:
        return {
            **base_envelope,
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
    try:
        serialized_bytes = json.dumps(data, ensure_ascii=False, default=str).encode()
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "RESPONSE_ENCODING_ERROR",
                "message": "The response could not be encoded as UTF-8 JSON.",
                "hint": "Narrow the request or retry without invalid text data.",
            },
            "_truncated": True,
            "_size_unavailable": "bytes",
            "_hint": _TRUNCATION_BYTE_HINT,
        }
    measured_bytes = len(serialized_bytes)
    return _truncate_response_to_limit(
        data,
        size_fn=lambda value: measured_bytes if value is data else _response_size_bytes(value),
        max_size=max_bytes,
        unit="bytes",
        hint=_TRUNCATION_BYTE_HINT,
    )


def _response_offset_chars(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> int:
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return 0
    bound.apply_defaults()
    return bound.arguments.get("response_offset_chars", 0)


def _continuation_preserved_fields(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    preserved: dict[str, Any] = {}
    if "ok" in data:
        preserved["ok"] = data["ok"]
    if "error" in data:
        preserved["error"] = _bound_error_value(data["error"])
    screenshot_artifact = _preserved_screenshot_artifact(data)
    if screenshot_artifact:
        preserved["artifacts"] = [screenshot_artifact]
    for key, value in data.items():
        if len(preserved) >= _MAX_PRESERVED_IDENTIFIER_FIELDS:
            break
        if not isinstance(key, str) or not key.endswith("_id") or key in preserved:
            continue
        if value is None or isinstance(value, int):
            preserved[key] = value
        elif isinstance(value, str) and len(value) <= _MAX_PRESERVED_IDENTIFIER_VALUE_CHARS:
            preserved[key] = value
    return preserved


def _full_response_continuation(
    serialized: str,
    total_bytes: int,
    snapshot_id: str,
    preserved: dict[str, Any],
    offset: int,
) -> dict[str, Any]:
    total_chars = len(serialized)
    chars_over = total_chars > MCP_MAX_RESPONSE_CHARS
    bytes_over = total_bytes > MCP_MAX_RESPONSE_BYTES
    if not chars_over and not bytes_over:
        raise ValueError("Continuation snapshot does not exceed either MCP response cap")

    size_fields: dict[str, int] = {}
    if chars_over:
        size_fields.update(
            {
                "_original_chars": total_chars,
                "_max_chars": MCP_MAX_RESPONSE_CHARS,
            }
        )
    if bytes_over:
        size_fields.update(
            {
                "_original_bytes": total_bytes,
                "_max_bytes": MCP_MAX_RESPONSE_BYTES,
            }
        )
    hint = (
        _CONTINUATION_BOTH_HINT
        if chars_over and bytes_over
        else _CONTINUATION_CHAR_HINT
        if chars_over
        else _CONTINUATION_BYTE_HINT
    )

    def envelope(content: str, next_offset: int | None) -> dict[str, Any]:
        return {
            **preserved,
            "ok": False,
            "error": {
                "code": "RESPONSE_TRUNCATED",
                "message": "The response is incomplete and must be continued.",
                "hint": hint,
            },
            "_truncated": True,
            **size_fields,
            "_snapshot_id": snapshot_id,
            "_hint": hint,
            "_content_slice": content,
            "_offset_chars": offset,
            "_next_offset_chars": next_offset,
            "_total_chars": total_chars,
        }

    if offset >= total_chars:
        return envelope("", None)

    # The empty-slice envelope is serialized once to establish fixed overhead.
    # Both possible next-offset encodings are measured so the chosen slice also
    # fits when the last page switches the field to JSON null.
    empty_continuing = envelope("", total_chars)
    empty_finished = envelope("", None)
    overhead_chars = max(_response_size(empty_continuing), _response_size(empty_finished))
    overhead_bytes = max(_response_size_bytes(empty_continuing), _response_size_bytes(empty_finished))
    char_budget = max(0, MCP_MAX_RESPONSE_CHARS - overhead_chars)
    byte_budget = max(0, MCP_MAX_RESPONSE_BYTES - overhead_bytes)

    # A raw character costs at least one serialized character and one UTF-8
    # byte. Precompute escaped prefix costs only for the bounded candidate
    # window, then binary-search those arrays instead of serializing the full
    # response for every probe.
    candidate = serialized[offset : offset + min(char_budget, byte_budget)]
    escaped_chars = array("I", [0])
    escaped_bytes = array("I", [0])
    for character in candidate:
        if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            char_cost = byte_cost = 2
        elif ord(character) < 0x20:
            char_cost = byte_cost = 6
        else:
            char_cost = 1
            byte_cost = len(character.encode())
        escaped_chars.append(escaped_chars[-1] + char_cost)
        escaped_bytes.append(escaped_bytes[-1] + byte_cost)

    relative_end = min(
        bisect.bisect_right(escaped_chars, char_budget) - 1,
        bisect.bisect_right(escaped_bytes, byte_budget) - 1,
    )
    end = offset + relative_end
    return envelope(serialized[offset:end], end if end < total_chars else None)


def _full_verbosity(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return False
    bound.apply_defaults()
    return bound.arguments.get("verbosity") == "full"


def _get_path(value: Any, path: tuple[str, ...]) -> tuple[Any, bool]:
    node = value
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


def _restore_protected_paths(formatted: TransformResult[Any], generic_value: Any) -> tuple[Any, bool]:
    """Copy the formatter's verbatim-retained subtrees back over the generically
    compacted value. Returns (value, ok); ok=False means a protected subtree's
    location was destroyed by compaction, so the formatter output must win."""
    if not formatted.protected_paths:
        return generic_value, True
    restored = copy.deepcopy(generic_value)
    for path in formatted.protected_paths:
        subtree, present = _get_path(formatted.value, path)
        if not present:
            continue
        node = restored
        for key in path[:-1]:
            if not isinstance(node, dict) or not isinstance(node.get(key), dict):
                return formatted.value, False
            node = node[key]
        if not isinstance(node, dict):
            return formatted.value, False
        node[path[-1]] = subtree
    return restored, True


def _combine_formatter_result(
    original: Any,
    formatter: Callable[[Any], TransformResult[Any] | Any] | None,
) -> TransformResult[Any]:
    if formatter is None:
        return distill_value(original)

    formatted = formatter(original)
    if not isinstance(formatted, TransformResult):
        return replace(
            distill_value(formatted),
            recoverable_anchor_source=formatted,
        )
    formatted = replace(formatted, recoverable_anchor_source=formatted.value)
    if formatted.tier is TransformTier.PASSTHROUGH:
        return formatted

    generic = distill_value(formatted.value)
    if generic.tier is TransformTier.PASSTHROUGH:
        # The generic pass could not (or refused to) improve the formatter's
        # output — e.g. it flagged an unsafe structure. The formatter's already
        # compacted result still stands; discarding it here would ship the full
        # oversized original to the cap for a defect in the *envelope*, not in
        # the formatter's work.
        return formatted
    # The generic compactor must not mangle fields the formatter deliberately
    # retained verbatim (inline screenshot base64, HAR log): restore those
    # subtrees, and if compaction destroyed their location, the formatter's
    # output wins outright. Restoring a verbatim subtree can also inflate the
    # combined value past the formatter's own output (generic scaffolding plus
    # the restored payload), in which case the formatter output wins on size.
    # The hard cap remains the final backstop.
    if formatted.protected_paths:
        value, restored_ok = _restore_protected_paths(formatted, generic.value)
        if not restored_ok or _response_size_bytes(value) >= _response_size_bytes(formatted.value):
            return formatted
    else:
        value = generic.value
    tier = (
        TransformTier.DEGRADED if TransformTier.DEGRADED in {formatted.tier, generic.tier} else TransformTier.STRUCTURED
    )
    return TransformResult(
        value=value,
        tier=tier,
        complete=formatted.complete and generic.complete,
        fallback_reason=formatted.fallback_reason or generic.fallback_reason,
        owns_completeness_marker=formatted.owns_completeness_marker,
        recoverable_anchor_source=formatted.recoverable_anchor_source,
    )


def _with_response_anchors(original: Any, transformed: TransformResult[Any]) -> TransformResult[Any]:
    if transformed.complete or transformed.tier is TransformTier.PASSTHROUGH or not isinstance(transformed.value, dict):
        return transformed
    original_anchors = _response_anchors(original)
    output_anchors = _response_anchors(transformed.value)
    remaining_entries = Counter(
        json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) for entry in output_anchors["entries"]
    )
    remaining_values = Counter(
        json.dumps(pair, ensure_ascii=False, sort_keys=True, default=str) for pair in output_anchors["values"]
    )
    original_pair_totals = Counter(
        json.dumps(entry[1:], ensure_ascii=False, sort_keys=True, default=str) for entry in original_anchors["entries"]
    )
    omitted_values: list[Any] = []
    for entry in original_anchors["entries"]:
        pair = entry[1:]
        entry_marker = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        pair_marker = json.dumps(pair, ensure_ascii=False, sort_keys=True, default=str)
        if remaining_entries[entry_marker]:
            remaining_entries[entry_marker] -= 1
            remaining_values[pair_marker] -= 1
        elif remaining_values[pair_marker] and original_pair_totals[pair_marker] == 1:
            remaining_values[pair_marker] -= 1
        else:
            omitted_values.append(pair)

    unique_values: list[Any] = []
    value_counts: dict[str, int] = {}
    for pair in omitted_values:
        marker = json.dumps(pair, ensure_ascii=False, sort_keys=True, default=str)
        if marker not in value_counts:
            unique_values.append(pair)
            value_counts[marker] = 0
        value_counts[marker] += 1
    if not unique_values:
        return transformed

    counted_values: list[Any] = []
    for pair in unique_values:
        count = value_counts[json.dumps(pair, ensure_ascii=False, sort_keys=True, default=str)]
        counted_values.append(pair if count == 1 else [*pair, count])
    key_order = list(dict.fromkeys(str(pair[0]) for pair in counted_values))
    representatives: list[Any] = []
    remainder: list[Any] = []
    seen_keys: set[str] = set()
    for pair in counted_values:
        key = str(pair[0])
        if key in seen_keys:
            remainder.append(pair)
        else:
            seen_keys.add(key)
            representatives.append(pair)

    budget = min(
        _MAX_RESPONSE_ANCHOR_SIDECAR_CHARS,
        max(
            _MIN_RESPONSE_ANCHOR_SIDECAR_CHARS,
            _ANCHOR_SIDECAR_BODY_MULTIPLIER * _response_size(transformed.value),
        ),
    )
    sidecar: dict[str, Any] = {
        "keys": {},
        "values": [],
        "omitted_key_count": len(key_order),
        "omitted_value_count": len(counted_values),
    }
    for key in key_order:
        candidate = {
            **sidecar,
            "keys": {**sidecar["keys"], key: None},
            "omitted_key_count": len(key_order) - len(sidecar["keys"]) - 1,
        }
        if _response_size(candidate) <= budget:
            sidecar = candidate
    for pair in [*representatives, *remainder]:
        if str(pair[0]) not in sidecar["keys"]:
            continue
        candidate_values = [*sidecar["values"], pair]
        candidate = {
            **sidecar,
            "values": candidate_values,
            "omitted_value_count": len(counted_values) - len(candidate_values),
        }
        if _response_size(candidate) <= budget:
            sidecar = candidate
    if sidecar["omitted_key_count"] == 0:
        sidecar.pop("omitted_key_count")
    if sidecar["omitted_value_count"] == 0:
        sidecar.pop("omitted_value_count")

    value = dict(transformed.value)
    value["_response_anchors"] = sidecar
    return TransformResult(
        value=value,
        tier=transformed.tier,
        complete=False,
        fallback_reason=transformed.fallback_reason,
        protected_paths=transformed.protected_paths,
        owns_completeness_marker=transformed.owns_completeness_marker,
        recoverable_anchor_source=transformed.recoverable_anchor_source,
    )


def _with_completeness_marker(transformed: TransformResult[Any], recovery_hint: str | None) -> TransformResult[Any]:
    if transformed.complete or transformed.tier is TransformTier.PASSTHROUGH:
        return transformed
    if transformed.owns_completeness_marker:
        return transformed
    marker = {
        "complete": False,
        "tier": transformed.tier.value,
        "recovery_hint": recovery_hint
        or "Narrow the request or paginate to recover content omitted from this summary.",
    }
    if transformed.fallback_reason is not None:
        marker["fallback_reason"] = transformed.fallback_reason
    if isinstance(transformed.value, dict):
        value = dict(transformed.value)
        value["_response_distillation"] = marker
    else:
        value = {"data": transformed.value, "_response_distillation": marker}
    return TransformResult(
        value=value,
        tier=transformed.tier,
        complete=False,
        fallback_reason=transformed.fallback_reason,
        owns_completeness_marker=True,
        recoverable_anchor_source=transformed.recoverable_anchor_source,
    )


def response_transformed(
    formatter: Callable[[Any], TransformResult[Any] | Any] | None = None,
    recovery_hint: str | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[Any]]]:
    """Distill an async tool response, then enforce the existing hard cap.

    A tool-aware formatter, when supplied, runs first. Unless it returns a
    passthrough result, the generic compactor then processes its output and
    restores any formatter-protected paths. Without a formatter, the generic
    compactor runs on the original response. A selected ``verbosity="full"``
    (explicitly or by the tool's default) bypasses transformation. A full response
    from a tool that declares ``response_offset_chars`` is cached for bounded,
    side-effect-free continuation when it exceeds either cap. A transform failure
    never breaks a successful tool call: the raw response falls through to the cap.
    """

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[Any]]:
        signature = inspect.signature(fn)
        continuation_capable = "response_offset_chars" in signature.parameters

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            response_offset_chars = _response_offset_chars(signature, args, kwargs)
            if not isinstance(response_offset_chars, int) or isinstance(response_offset_chars, bool):
                return {
                    "ok": False,
                    "error": {
                        "code": "INVALID_OFFSET",
                        "message": "response_offset_chars must be an integer.",
                        "hint": "re-issue the same call with response_offset_chars=0",
                    },
                }
            if response_offset_chars < 0:
                return {
                    "ok": False,
                    "error": {
                        "code": "INVALID_OFFSET",
                        "message": "response_offset_chars must be non-negative.",
                        "hint": "re-issue the same call with response_offset_chars=0",
                    },
                }

            cache_key: str | None = None
            serialized: str | None = None
            snapshot_id: str | None = None
            if continuation_capable and response_offset_chars > 0:
                try:
                    cache_key = _continuation_cache_key(fn, signature, args, kwargs)
                except Exception:
                    LOG.warning("mcp_response_continuation_key_failed", tool=fn.__name__, exc_info=True)
                    return {
                        "ok": False,
                        "error": {
                            "code": "CONTINUATION_UNAVAILABLE",
                            "message": "Continuation scope could not be resolved.",
                            "hint": "re-issue the same call with response_offset_chars=0",
                        },
                    }
                snapshot = _continuation_cache_get(cache_key)
                if snapshot is None:
                    return {
                        "ok": False,
                        "error": {
                            "code": "CONTINUATION_EXPIRED",
                            "message": "Continuation snapshot is missing or expired.",
                            "hint": "re-issue the same call with response_offset_chars=0",
                        },
                    }
                serialized, total_bytes, snapshot_id, preserved = snapshot
                capped = _full_response_continuation(
                    serialized,
                    total_bytes,
                    snapshot_id,
                    preserved,
                    response_offset_chars,
                )
                LOG.info(
                    "mcp_response_distilled",
                    tool=fn.__name__,
                    tier=TransformTier.PASSTHROUGH.value,
                    original_chars=len(serialized),
                    output_chars=_response_size(capped),
                    savings_percentage=round(
                        max(0.0, (len(serialized) - _response_size(capped)) * 100 / len(serialized)),
                        2,
                    ),
                    fallback_reason="verbosity_full_continuation_cache",
                    snapshot_id=snapshot_id,
                )
                return capped

            original = await fn(*args, **kwargs)
            if continuation_capable:
                try:
                    cache_key = _continuation_cache_key(fn, signature, args, kwargs)
                except Exception:
                    LOG.warning("mcp_response_continuation_key_failed", tool=fn.__name__, exc_info=True)
                else:
                    _continuation_cache_delete(cache_key)
            try:
                serialized = json.dumps(original, ensure_ascii=False, default=str)
                original_bytes = len(serialized.encode())
            except Exception:
                serialized = None
                original_chars = sys.maxsize
                original_bytes = sys.maxsize
            else:
                original_chars = len(serialized)
            tier = TransformTier.PASSTHROUGH
            fallback_reason: str | None = None

            full_verbosity = _full_verbosity(signature, args, kwargs)
            if full_verbosity:
                selected = original
                fallback_reason = "verbosity_full"
            else:
                try:
                    transformed = _combine_formatter_result(original, formatter)
                    if transformed.tier is not TransformTier.PASSTHROUGH:
                        anchor_source = (
                            original
                            if transformed.recoverable_anchor_source is _NO_RECOVERABLE_ANCHOR_SOURCE
                            else transformed.recoverable_anchor_source
                        )
                        transformed = _with_response_anchors(anchor_source, transformed)
                        transformed = _with_completeness_marker(transformed, recovery_hint)
                except Exception:
                    LOG.warning("mcp_response_transform_failed", tool=fn.__name__, exc_info=True)
                    transformed = TransformResult(
                        value=original,
                        tier=TransformTier.PASSTHROUGH,
                        complete=True,
                        fallback_reason="transform_error",
                    )
                if transformed.tier is TransformTier.PASSTHROUGH:
                    selected = original
                    fallback_reason = transformed.fallback_reason
                elif (
                    _response_size_bytes(transformed.value) < original_bytes
                    and _response_size(transformed.value) <= original_chars
                ):
                    selected = transformed.value
                    tier = transformed.tier
                    fallback_reason = transformed.fallback_reason
                else:
                    selected = original
                    fallback_reason = "candidate_not_smaller"

            response_exceeds_cap = original_chars > MCP_MAX_RESPONSE_CHARS or original_bytes > MCP_MAX_RESPONSE_BYTES
            if (
                full_verbosity
                and continuation_capable
                and cache_key is not None
                and serialized is not None
                and original_bytes <= _CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES
                and response_exceeds_cap
            ):
                preserved = _continuation_preserved_fields(original)
                snapshot_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:8]
                _continuation_cache_put(cache_key, serialized, original_bytes, snapshot_id, preserved)
                capped = _full_response_continuation(serialized, original_bytes, snapshot_id, preserved, 0)
            elif (
                full_verbosity
                and continuation_capable
                and serialized is not None
                and original_bytes > _CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES
                and response_exceeds_cap
            ):
                preserved = _continuation_preserved_fields(original)
                capped = {
                    **preserved,
                    "ok": False,
                    "error": {
                        "code": "RESPONSE_TOO_LARGE_FOR_CONTINUATION",
                        "message": "The response exceeds the continuation snapshot limit and was not returned.",
                        "hint": "Narrow the request or request fewer fields.",
                    },
                    "_truncated": True,
                    "_original_chars": original_chars,
                    "_original_bytes": original_bytes,
                    "_max_snapshot_bytes": _CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES,
                }
                fallback_reason = "continuation_snapshot_too_large"
            else:
                capped = truncate_response_bytes(truncate_response(selected))
            output_chars = _response_size(capped)
            savings_percentage = (
                round(max(0.0, (original_chars - output_chars) * 100 / original_chars), 2)
                if 0 < original_chars < sys.maxsize
                else 0.0
            )
            LOG.info(
                "mcp_response_distilled",
                tool=fn.__name__,
                tier=tier.value,
                original_chars=original_chars,
                output_chars=output_chars,
                savings_percentage=savings_percentage,
                fallback_reason=fallback_reason,
                **({"snapshot_id": snapshot_id} if snapshot_id is not None else {}),
            )
            return capped

        return wrapper

    return decorator


def size_capped(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[Any]]:
    """Decorator: enforce character and UTF-8 byte caps on a tool's return value.

    Applies to async tool functions returning any JSON-serializable payload.
    Emits a structured ``mcp_response_truncated`` warning whenever the envelope
    fires so operators can see which tools are hitting the cap and tune the
    limit (or paginate upstream) rather than having the signal hidden in the
    tool response alone.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        result = await fn(*args, **kwargs)
        capped = truncate_response_bytes(truncate_response(result))
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
    "COMPACT_TOOL_NAMES",
    "EXTRACTION_DEFAULT_VERBOSITY",
    "EXTRACTION_TOOL_NAMES",
    "FULL_DEFAULT_TOOL_NAMES",
    "MCP_MAX_RESPONSE_BYTES",
    "MCP_MAX_RESPONSE_CHARS",
    "SUMMARY_DEFAULT_TOOL_NAMES",
    "extraction_default_verbosity",
    "response_transformed",
    "size_capped",
    "truncate_response",
    "truncate_response_bytes",
]
