"""Storage-aware response formatting for MCP browser storage results.

Storage values can contain authentication material. This module intentionally
emits no logs and keeps transformation metadata limited to tier, completeness,
and non-value fallback reasons.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from skyvern.cli.mcp_tools.response_distillation import (
    _MAX_KEYS,
    TransformResult,
    TransformTier,
    distill_value,
)

# Leave room for the central compactor's two map-level metadata fields. This
# prevents a second generic pass from dropping additional previews or replacing
# the exact omitted count.
_MAX_STORAGE_PREVIEWS = _MAX_KEYS - 2
_MIN_STRUCTURED_VALUE_CHARS = 240
_SUMMARY_FIELDS = frozenset({"_key_count", "_omitted_keys"})
_UNSAFE_FALLBACK_REASONS = frozenset(
    {
        "aliased_or_recursive",
        "ambiguous_duplicate_key",
        "parse_failed",
        "structure_too_deep",
        "unsafe_non_finite_number",
        "unsafe_non_string_key",
        "unsafe_value_type",
    }
)

# Reasons that make the value's *content* ambiguous — compacting any part of a
# map alongside such a value risks misrepresenting it, so the whole map fails
# closed. Opaque-but-harmless values (a JWT or base64 blob that parses as
# neither JSON nor YAML: parse_failed and friends) are kept verbatim instead
# while their siblings still compact.
_MAP_FAIL_CLOSED_REASONS = frozenset(
    {
        "aliased_or_recursive",
        "ambiguous_duplicate_key",
    }
)


def _serialized_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return sys.maxsize


def _format_storage_value(value: Any) -> tuple[Any, TransformTier, bool, str | None, bool]:
    """Return a bounded value plus provenance and whether the value changed.

    The final boolean distinguishes an accepted compact candidate from an
    unchanged value. Values with ambiguous structure are returned as marked
    passthroughs and cause the map formatter to fail closed. Opaque values that
    merely fail parsing are retained verbatim while sibling values continue to
    compact.
    """
    transformed = distill_value(value)
    value_chars = _serialized_chars(value)

    if transformed.tier is TransformTier.PASSTHROUGH:
        reason = transformed.fallback_reason
        if reason in _UNSAFE_FALLBACK_REASONS or value_chars > _MIN_STRUCTURED_VALUE_CHARS:
            return value, transformed.tier, transformed.complete, reason, False
        return value, TransformTier.STRUCTURED, True, None, False

    if value_chars <= _MIN_STRUCTURED_VALUE_CHARS:
        return value, TransformTier.STRUCTURED, True, None, False
    if _serialized_chars(transformed.value) >= value_chars:
        return value, TransformTier.STRUCTURED, True, None, False
    return (
        transformed.value,
        transformed.tier,
        transformed.complete,
        transformed.fallback_reason,
        True,
    )


def _format_storage_map(items: dict[Any, Any]) -> TransformResult[dict[Any, Any]]:
    if not all(isinstance(key, str) for key in items):
        return TransformResult(
            value=items,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="unsafe_non_string_key",
        )

    ordered_keys = sorted(items)
    summarize_map = len(ordered_keys) > _MAX_STORAGE_PREVIEWS
    if summarize_map:
        previewable_keys = [key for key in ordered_keys if key not in _SUMMARY_FIELDS]
        selected_keys = previewable_keys[:_MAX_STORAGE_PREVIEWS]
    else:
        selected_keys = ordered_keys

    previews: dict[str, Any] = {}
    complete = not summarize_map
    tier = TransformTier.STRUCTURED
    fallback_reason = "content_summarized" if summarize_map else None
    changed = summarize_map
    protected_paths: list[tuple[str, ...]] = []

    for key in selected_keys:
        value, value_tier, value_complete, value_reason, value_changed = _format_storage_value(items[key])
        if value_tier is TransformTier.PASSTHROUGH:
            if value_reason in _MAP_FAIL_CLOSED_REASONS:
                return TransformResult(
                    value=items,
                    tier=TransformTier.PASSTHROUGH,
                    complete=True,
                    fallback_reason=value_reason,
                )
            previews[key] = items[key]
            protected_paths.append((key,))
            continue
        previews[key] = value
        changed = changed or value_changed
        complete = complete and value_complete
        if value_tier is TransformTier.DEGRADED:
            tier = TransformTier.DEGRADED
        if not value_complete and fallback_reason is None:
            fallback_reason = value_reason or "content_summarized"

    if not changed:
        return TransformResult(value=items, tier=TransformTier.STRUCTURED, complete=True)

    if summarize_map:
        previews["_key_count"] = len(items)
        previews["_omitted_keys"] = len(items) - len(selected_keys)
    return TransformResult(
        value=previews,
        tier=tier,
        complete=complete,
        fallback_reason=fallback_reason,
        protected_paths=tuple(protected_paths),
    )


def format_storage_response(response: Any) -> TransformResult[Any]:
    """Compact known session/local-storage maps without mutating the response.

    Live session-storage reads use ``data.items``. ``local_storage`` and
    ``session_storage`` fields are also recognized at the top level or under
    ``data`` so representative state-shaped fixtures can exercise the same
    formatter without adding a local-storage read tool.
    """
    if not isinstance(response, dict):
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="unrecognized_storage_shape",
        )

    data = response.get("data")
    targets: list[tuple[str | None, str, dict[Any, Any]]] = []
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, dict):
            targets.append(("data", "items", items))
        for field in ("local_storage", "session_storage"):
            storage_map = data.get(field)
            if isinstance(storage_map, dict):
                targets.append(("data", field, storage_map))
    for field in ("local_storage", "session_storage"):
        storage_map = response.get(field)
        if isinstance(storage_map, dict):
            targets.append((None, field, storage_map))

    if not targets:
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="unrecognized_storage_shape",
        )

    formatted_targets: list[tuple[str | None, str, TransformResult[dict[Any, Any]]]] = []
    complete = True
    tier = TransformTier.STRUCTURED
    fallback_reason: str | None = None
    changed = False
    protected_paths: list[tuple[str, ...]] = []
    for parent, field, storage_map in targets:
        formatted = _format_storage_map(storage_map)
        if formatted.tier is TransformTier.PASSTHROUGH:
            return TransformResult(
                value=response,
                tier=TransformTier.PASSTHROUGH,
                complete=True,
                fallback_reason=formatted.fallback_reason,
            )
        formatted_targets.append((parent, field, formatted))
        prefix = (field,) if parent is None else ("data", field)
        protected_paths.extend((*prefix, *path) for path in formatted.protected_paths)
        changed = changed or formatted.value is not storage_map
        complete = complete and formatted.complete
        if formatted.tier is TransformTier.DEGRADED:
            tier = TransformTier.DEGRADED
        if not formatted.complete and fallback_reason is None:
            fallback_reason = formatted.fallback_reason or "content_summarized"

    if not changed:
        return TransformResult(value=response, tier=TransformTier.STRUCTURED, complete=True)

    candidate = dict(response)
    candidate_data = dict(data) if isinstance(data, dict) else None
    for parent, field, formatted in formatted_targets:
        if parent == "data":
            assert candidate_data is not None
            candidate_data[field] = formatted.value
        else:
            candidate[field] = formatted.value
    if candidate_data is not None:
        candidate["data"] = candidate_data

    if _serialized_chars(candidate) >= _serialized_chars(response):
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="candidate_not_smaller",
        )
    return TransformResult(
        value=candidate,
        tier=tier,
        complete=complete,
        fallback_reason=fallback_reason,
        protected_paths=tuple(protected_paths),
    )


__all__ = ["format_storage_response"]
