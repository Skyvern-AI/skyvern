"""Deterministic, conservative compaction for MCP tool responses.

This module deliberately knows nothing about individual tools. Tool-specific
formatters can select and shape useful content first; :func:`distill_value`
then applies bounded, JSON-safe previews without guessing at scalar text.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

import yaml

from skyvern.utils.yaml_loader import NoDatesSafeLoader

_MAX_DEPTH = 4
_MAX_KEYS = 24
_MAX_LIST_EXAMPLES = 5
_MAX_STRING_PREVIEW = 240
_MAX_PREFIX_CHARS = 64_000
_NO_RECOVERABLE_ANCHOR_SOURCE = object()

T = TypeVar("T")


class TransformTier(StrEnum):
    """Confidence level for the structure supplied to the compactor."""

    STRUCTURED = "structured"
    DEGRADED = "degraded"
    PASSTHROUGH = "passthrough"


@dataclass(frozen=True, slots=True)
class TransformResult(Generic[T]):
    """The output and provenance of one response transformation."""

    value: T
    tier: TransformTier
    complete: bool
    fallback_reason: str | None = None
    protected_paths: tuple[tuple[str, ...], ...] = ()
    """Key paths in ``value`` the formatter deliberately retained verbatim
    (e.g. inline screenshot base64, artifact listings). Downstream generic
    compaction must restore these subtrees untouched."""
    owns_completeness_marker: bool = False
    recoverable_anchor_source: Any = field(
        default=_NO_RECOVERABLE_ANCHOR_SOURCE,
        repr=False,
        compare=False,
    )
    """Formatter-stage source for recoverable anchors; omitted from wire output."""


class _DuplicateKeyError(ValueError):
    """Raised when JSON object pairs would lose ambiguous duplicate data."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _invalid_structure_reason(value: Any) -> str | None:
    """Return why a value cannot be treated as unambiguous JSON structure."""
    seen: set[int] = set()
    active: set[int] = set()

    def visit(item: Any) -> str | None:
        if isinstance(item, dict):
            item_id = id(item)
            if item_id in active or item_id in seen:
                return "aliased_or_recursive"
            seen.add(item_id)
            active.add(item_id)
            for key, child in item.items():
                if not isinstance(key, str):
                    return "unsafe_non_string_key"
                reason = visit(child)
                if reason is not None:
                    return reason
            active.remove(item_id)
            return None
        if isinstance(item, list):
            item_id = id(item)
            if item_id in active or item_id in seen:
                return "aliased_or_recursive"
            seen.add(item_id)
            active.add(item_id)
            for child in item:
                reason = visit(child)
                if reason is not None:
                    return reason
            active.remove(item_id)
            return None
        if item is None or isinstance(item, (str, bool, int)):
            return None
        if isinstance(item, float):
            return None if math.isfinite(item) else "unsafe_non_finite_number"
        return "unsafe_value_type"

    return visit(value)


class _PlainScalarYamlLoader(NoDatesSafeLoader):
    """YAML loader for distilling free-form text: structure only, scalars verbatim.

    PyYAML implements YAML 1.1, whose implicit resolvers coerce ``10:30`` to 630,
    ``yes`` to True, ``007`` to 7, and ``null`` or ``~`` to None. A distilled
    summary of page text must never present coerced values as extracted truth,
    so those resolvers are removed and the scalars stay plain strings. Explicit
    empty values remain empty strings. JSON input never reaches this loader;
    ``json.loads`` runs first and keeps its native types.
    """


_COERCING_SCALAR_TAGS = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:null",
}
_PlainScalarYamlLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag not in _COERCING_SCALAR_TAGS]
    for key, resolvers in NoDatesSafeLoader.yaml_implicit_resolvers.items()
}


def _construct_plain_scalar(loader: _PlainScalarYamlLoader, node: yaml.nodes.ScalarNode) -> str:
    return loader.construct_scalar(node)


for _tag in _COERCING_SCALAR_TAGS:
    _PlainScalarYamlLoader.add_constructor(_tag, _construct_plain_scalar)


def _safe_load_plain_scalars(stream: str) -> Any:
    loader = _PlainScalarYamlLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def _yaml_has_alias(value: str) -> bool:
    try:
        return any(
            isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(value, Loader=_PlainScalarYamlLoader)
        )
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):
        return False


def _yaml_strips_content(value: str) -> bool:
    """Detect YAML comment syntax that would silently drop free-form text.

    YAML treats ``#`` at line start or after whitespace as a comment, so parsing
    prose like ``Address: 500 Main St #204`` would truncate the scalar mid-value
    while reporting the result complete. Such input is ambiguous: pass it
    through verbatim instead of presenting truncated values as extracted truth.
    """
    return any(line.lstrip().startswith("#") or " #" in line or "\t#" in line for line in value.splitlines())


def _yaml_has_duplicate_keys(value: str) -> bool:
    """Detect duplicate scalar mapping keys before SafeLoader discards one.

    Key fingerprints MUST be computed with the same loader that later parses
    the value (`_PlainScalarYamlLoader`): with the coercing resolvers stripped,
    ``10:`` and ``"10":`` both resolve to the string key ``"10"`` and would
    silently overwrite each other if fingerprinted under the default resolvers.
    """
    try:
        root = yaml.compose(value, Loader=_PlainScalarYamlLoader)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):
        return False

    def visit(node: yaml.nodes.Node | None) -> bool:
        if isinstance(node, yaml.nodes.MappingNode):
            keys: set[tuple[str, str]] = set()
            for key, child in node.value:
                if isinstance(key, yaml.nodes.ScalarNode):
                    fingerprint = (key.tag, key.value)
                    if fingerprint in keys:
                        return True
                    keys.add(fingerprint)
                if visit(key) or visit(child):
                    return True
            return False
        if isinstance(node, yaml.nodes.SequenceNode):
            return any(visit(child) for child in node.value)
        return False

    try:
        return visit(root)
    except RecursionError:
        return False


def _ordered_keys(value: dict[str, Any]) -> list[str]:
    anchors = [key for key in value if key in {"ok", "error"} or key.endswith("_id")]
    anchor_set = set(anchors)
    return sorted(anchors, key=lambda key: (key not in {"ok", "error"}, key)) + [
        key for key in value if key not in anchor_set
    ]


def _preview_string(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_STRING_PREVIEW:
        return value, True
    omitted = len(value) - _MAX_STRING_PREVIEW
    return f"{value[:_MAX_STRING_PREVIEW]}… [{omitted} chars omitted]", False


def _depth_summary(value: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            "_type": "array",
            "_length": len(value),
            "_omitted_items": len(value),
        }

    keys = _ordered_keys(value)
    selected = keys[:_MAX_KEYS]
    scalar_preview: dict[str, Any] = {}
    array_lengths: dict[str, int] = {}
    for key in selected:
        child = value[key]
        if isinstance(child, list):
            array_lengths[key] = len(child)
        elif not isinstance(child, dict):
            scalar_preview[key] = _preview_string(child)[0] if isinstance(child, str) else child
    summary: dict[str, Any] = {
        "_type": "object",
        "_key_count": len(keys),
        "_keys": selected,
        "_omitted_keys": max(0, len(keys) - len(selected)),
    }
    if scalar_preview:
        summary["_scalar_preview"] = scalar_preview
    if array_lengths:
        summary["_array_lengths"] = array_lengths
    return summary


def _compact(value: Any, *, depth: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _preview_string(value)
    if not isinstance(value, (dict, list)):
        return value, True
    if depth >= _MAX_DEPTH:
        return _depth_summary(value), False

    if isinstance(value, list):
        compacted_examples: list[Any] = []
        complete = len(value) <= _MAX_LIST_EXAMPLES
        for item in value[:_MAX_LIST_EXAMPLES]:
            compacted, child_complete = _compact(item, depth=depth + 1)
            compacted_examples.append(compacted)
            complete = complete and child_complete
        if len(value) <= _MAX_LIST_EXAMPLES:
            return compacted_examples, complete
        return (
            {
                "_type": "array",
                "_length": len(value),
                "_examples": compacted_examples,
                "_omitted_items": len(value) - len(compacted_examples),
            },
            False,
        )

    keys = _ordered_keys(value)
    selected = keys[:_MAX_KEYS]
    complete = len(selected) == len(keys)
    compacted_mapping: dict[str, Any] = {}
    for key in selected:
        compacted, child_complete = _compact(value[key], depth=depth + 1)
        compacted_mapping[key] = compacted
        complete = complete and child_complete
    if len(selected) != len(keys):
        key_count_field = "_key_count"
        while key_count_field in compacted_mapping:
            key_count_field = f"_{key_count_field}"
        omitted_keys_field = "_omitted_keys"
        while omitted_keys_field in compacted_mapping:
            omitted_keys_field = f"_{omitted_keys_field}"
        compacted_mapping[key_count_field] = len(keys)
        compacted_mapping[omitted_keys_field] = len(keys) - len(selected)
    return compacted_mapping, complete


def _structured_result(value: dict[str, Any] | list[Any], tier: TransformTier) -> TransformResult[Any]:
    try:
        reason = _invalid_structure_reason(value)
    except RecursionError:
        reason = "structure_too_deep"
    if reason is not None:
        return TransformResult(value=value, tier=TransformTier.PASSTHROUGH, complete=True, fallback_reason=reason)
    compacted, complete = _compact(value, depth=0)
    fallback_reason = None if complete else "content_summarized"
    return TransformResult(value=compacted, tier=tier, complete=complete, fallback_reason=fallback_reason)


def _parsed_structure_result(
    original: str, parsed: dict[str, Any] | list[Any], tier: TransformTier
) -> TransformResult[Any]:
    """Compact a parsed string, preserving the string when its contents are unsafe."""
    result = _structured_result(parsed, tier)
    if result.tier is not TransformTier.PASSTHROUGH:
        return result
    return TransformResult(
        value=original,
        tier=TransformTier.PASSTHROUGH,
        complete=True,
        fallback_reason=result.fallback_reason,
    )


def _degraded_json_prefix(value: str) -> TransformResult[Any] | None:
    stripped = value.lstrip()
    if not stripped:
        return None
    try:
        parsed, end = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys).raw_decode(
            stripped[:_MAX_PREFIX_CHARS]
        )
    except _DuplicateKeyError:
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="ambiguous_duplicate_key",
        )
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(parsed, (dict, list)) or not stripped[end:].strip():
        return None
    result = _parsed_structure_result(value, parsed, TransformTier.DEGRADED)
    if result.tier is TransformTier.PASSTHROUGH:
        return result
    return TransformResult(
        value=result.value,
        tier=TransformTier.DEGRADED,
        complete=False,
        fallback_reason="trailing_content_after_json_prefix",
    )


def distill_value(value: T) -> TransformResult[Any]:
    """Parse and compact a response without mutating the input.

    Dicts and lists are known structured values. Strings must parse fully as a
    JSON/YAML mapping or sequence; the sole exception is a bounded JSON prefix,
    which is explicitly reported as degraded and incomplete. Scalars and
    ambiguous/unsafe values are returned unchanged.
    """
    if isinstance(value, (dict, list)):
        return _structured_result(value, TransformTier.STRUCTURED)
    if not isinstance(value, str):
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="scalar_or_unsupported_value",
        )
    if not value.strip():
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="empty_string",
        )

    parsed: Any
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError:
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="ambiguous_duplicate_key",
        )
    except (json.JSONDecodeError, RecursionError):
        parsed = None
    else:
        if isinstance(parsed, (dict, list)):
            return _parsed_structure_result(value, parsed, TransformTier.STRUCTURED)
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="scalar_only_parse",
        )

    if _yaml_has_alias(value):
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="aliased_or_recursive",
        )
    if _yaml_has_duplicate_keys(value):
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="ambiguous_duplicate_key",
        )
    if _yaml_strips_content(value):
        return TransformResult(
            value=value,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="yaml_comment_ambiguity",
        )
    try:
        parsed = _safe_load_plain_scalars(value)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _parsed_structure_result(value, parsed, TransformTier.STRUCTURED)

    degraded = _degraded_json_prefix(value)
    if degraded is not None:
        return degraded
    return TransformResult(
        value=value,
        tier=TransformTier.PASSTHROUGH,
        complete=True,
        fallback_reason="scalar_only_parse" if parsed is not None else "parse_failed",
    )


__all__ = [
    "TransformResult",
    "TransformTier",
    "distill_value",
]
