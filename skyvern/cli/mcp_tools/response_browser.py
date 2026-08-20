"""Tool-aware response formatting for browser evaluation and extraction."""

from __future__ import annotations

import json
import sys
from typing import Any

from skyvern.cli.mcp_tools.response_distillation import TransformResult, TransformTier, distill_value

_TARGET_KEYS = ("result", "extracted")
_SMALL_STRUCTURED_STRING_CHARS = 240


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return sys.maxsize


def _should_use_candidate(original: Any, transformed: TransformResult[Any]) -> bool:
    """Keep small values intact and never expand a targeted payload."""
    if transformed.tier is TransformTier.PASSTHROUGH:
        return False
    if transformed.complete and isinstance(original, (dict, list)):
        return False
    if transformed.complete and isinstance(original, str) and len(original) <= _SMALL_STRUCTURED_STRING_CHARS:
        return False
    return _serialized_size(transformed.value) < _serialized_size(original)


def format_browser_response(response: Any) -> TransformResult[Any]:
    """Compact only evaluate/extract values in an MCP result envelope.

    The formatter is intentionally pure: envelope metadata, screenshot data,
    artifacts, and other non-target fields are retained unchanged. The central
    response hook adds its standard recovery marker when this formatter reports
    an incomplete structured or degraded result.
    """
    if not isinstance(response, dict):
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="unrecognized_browser_response",
        )
    data = response.get("data")
    if not isinstance(data, dict):
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason="unrecognized_browser_response",
        )

    replacements: dict[str, Any] = {}
    selected_results: list[TransformResult[Any]] = []
    passthrough_reason: str | None = None
    for key in _TARGET_KEYS:
        if key not in data:
            continue
        transformed = distill_value(data[key])
        if _should_use_candidate(data[key], transformed):
            replacements[key] = transformed.value
            selected_results.append(transformed)
        elif passthrough_reason is None:
            passthrough_reason = transformed.fallback_reason

    if not replacements:
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason=passthrough_reason or "no_browser_payload_target",
        )

    formatted_data = dict(data)
    formatted_data.update(replacements)
    formatted_response = dict(response)
    formatted_response["data"] = formatted_data
    tier = (
        TransformTier.DEGRADED
        if any(result.tier is TransformTier.DEGRADED for result in selected_results)
        else TransformTier.STRUCTURED
    )
    complete = all(result.complete for result in selected_results)
    fallback_reason = next(
        (result.fallback_reason for result in selected_results if result.fallback_reason is not None),
        None,
    )
    protected_paths = tuple(
        path
        for path in (("data", "url"), ("data", "screenshot"), ("artifacts",))
        if _path_present(formatted_response, path)
    )
    return TransformResult(
        value=formatted_response,
        tier=tier,
        complete=complete,
        fallback_reason=fallback_reason,
        protected_paths=protected_paths,
    )


def _path_present(value: Any, path: tuple[str, ...]) -> bool:
    node = value
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


__all__ = ["format_browser_response"]
