"""Helpers for capping prompt inputs before templating.

Separate from prompt_engine.py so per-field caps can be applied at the call
boundary without reaching into the element-tree truncation logic. Reuses
skyvern.utils.token_counter.count_tokens for consistent measurement.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from skyvern.utils.token_counter import count_tokens, decode_tokens, encode_tokens

LOG = structlog.get_logger()

PREVIOUS_EXTRACTED_INFO_MAX_TOKENS = 20_000
EXTRACTION_SCHEMA_MAX_TOKENS = 10_000


def _crop_string(value: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    tokens = encode_tokens(value)
    if len(tokens) <= max_tokens:
        return value
    return decode_tokens(tokens[-max_tokens:])


def _crop_list(value: list[Any], max_tokens: int) -> list[Any]:
    """Greedy reverse-iteration: keep the most recent items that fit and stop
    at the first overshoot. Earlier items are dropped even if they would
    individually fit — recency over coverage is the intent."""
    result: list[Any] = []
    remaining = max_tokens
    for item in reversed(value):
        rendered = item if isinstance(item, str) else json.dumps(item, default=str)
        cost = count_tokens(rendered)
        if cost > remaining:
            break
        result.append(item)
        remaining -= cost
    result.reverse()
    return result


def _crop_dict(value: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    if not value:
        return value
    per_key = max(1, max_tokens // len(value))
    cropped: dict[str, Any] = {}
    for key, inner in value.items():
        inner_str = inner if isinstance(inner, str) else json.dumps(inner, default=str)
        if count_tokens(inner_str) <= per_key:
            # Preserve the original type when the value already fits — only
            # values that overshoot the per-key budget get coerced to a
            # truncated string.
            cropped[key] = inner
        else:
            cropped[key] = _crop_string(inner_str, per_key)
    return cropped


def truncate_previous_extracted_information(
    value: Any,
    max_tokens: int = PREVIOUS_EXTRACTED_INFO_MAX_TOKENS,
) -> Any:
    """Cap the prompt contribution of `previous_extracted_information`."""
    if value is None:
        return None

    rendered_before = value if isinstance(value, str) else json.dumps(value, default=str)
    before_tokens = count_tokens(rendered_before)

    if isinstance(value, str):
        result: Any = _crop_string(value, max_tokens)
    elif isinstance(value, list):
        cropped = _crop_list(value, max_tokens)
        result = cropped if cropped else _crop_string(json.dumps(value, default=str), max_tokens)
    elif isinstance(value, dict):
        result = _crop_dict(value, max_tokens)
    else:
        result = _crop_string(json.dumps(value, default=str), max_tokens)

    rendered_after = result if isinstance(result, str) else json.dumps(result, default=str)
    after_tokens = count_tokens(rendered_after)
    if after_tokens < before_tokens:
        LOG.warning(
            "Truncated previous_extracted_information",
            value_kind=type(value).__name__,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            max_tokens=max_tokens,
        )
    return result


def truncate_extraction_schema(
    schema: Any,
    max_tokens: int = EXTRACTION_SCHEMA_MAX_TOKENS,
) -> Any:
    """Cap a customer-provided JSONSchema when it blows the prompt budget.

    Passes through unchanged when under budget. Otherwise replaces the body
    with a placeholder that preserves top-level shape (object/array) and tells
    the LLM to fall back to general extraction.
    """
    if schema is None:
        return None

    top_level: Any = None
    if isinstance(schema, str):
        before_tokens = count_tokens(schema)
        if before_tokens <= max_tokens:
            return schema
        # Truncating a JSON string tail-first breaks brace/bracket pairing; try
        # to recover the top-level type from a parse attempt, otherwise fall
        # through to the object placeholder below.
        try:
            parsed = json.loads(schema)
            if isinstance(parsed, dict):
                top_level = parsed.get("type")
        except (ValueError, TypeError):
            pass
    else:
        rendered = json.dumps(schema, default=str)
        before_tokens = count_tokens(rendered)
        if before_tokens <= max_tokens:
            return schema
        if isinstance(schema, dict):
            top_level = schema.get("type")

    placeholder = {
        "type": top_level if top_level in ("object", "array") else "object",
        "_skyvern_schema_truncated": True,
        "_skyvern_schema_hint": (
            "The full extraction schema exceeded the prompt budget and was replaced with this placeholder. "
            "Extract all relevant information from the page as structured data; do not assume specific field names."
        ),
    }
    LOG.warning(
        "Truncated extraction schema to placeholder",
        before_tokens=before_tokens,
        max_tokens=max_tokens,
        placeholder_top_level=placeholder["type"],
    )
    return placeholder
