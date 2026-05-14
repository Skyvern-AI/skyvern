"""Generic helpers for shaping model-facing agent context.

These helpers are intentionally mechanical. Caller-owned product policy,
including reviewer-specific persist validation and Copilot enforcement rules,
stays with the caller.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Collection, Mapping
from typing import Any

import structlog

DEFAULT_TRUNCATION_SUFFIX = "\n... [truncated]"
ReplacementValue = object

LOG = structlog.get_logger()


def get_agent_message_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def replace_agent_message_field(item: Any, name: str, value: Any) -> Any:
    if isinstance(item, dict):
        return {**item, name: value}
    try:
        updated = copy.copy(item)
        setattr(updated, name, value)
        return updated
    except (AttributeError, TypeError) as exc:
        LOG.warning(
            "Failed to rewrite agent message field; leaving item as-is",
            field=name,
            item_type=type(item).__name__,
            error=str(exc),
        )
        return item


def compact_agent_messages_for_llm(
    messages: list[Any],
    *,
    keep_recent_tool_outputs: int,
    max_recent_tool_output_chars: int,
    summarize_tool_output: Callable[[str], str] | None = None,
    summarize_tool_arguments: Callable[[str], str] | None = None,
    tool_output_truncation_suffix: str = DEFAULT_TRUNCATION_SUFFIX,
    token_budget: int | None = None,
    estimate_tokens: Callable[[list[Any]], int] | None = None,
    is_synthetic_message: Callable[[Any], bool] | None = None,
    synthetic_message_placeholder: Any | Callable[[Any], Any] | None = None,
    emergency_tool_output_char_cap: int | None = None,
    aggressive_prune: Callable[[list[Any]], list[Any]] | None = None,
) -> list[Any]:
    """Compact model-facing messages without depending on one agent runtime."""
    _validate_non_negative("keep_recent_tool_outputs", keep_recent_tool_outputs)
    _validate_non_negative("max_recent_tool_output_chars", max_recent_tool_output_chars)
    if token_budget is not None:
        _validate_non_negative("token_budget", token_budget)
        if estimate_tokens is None:
            raise ValueError("estimate_tokens is required when token_budget is set")
    if emergency_tool_output_char_cap is not None:
        _validate_non_negative("emergency_tool_output_char_cap", emergency_tool_output_char_cap)

    items = _compact_tool_payloads(
        list(messages),
        keep_recent_tool_outputs=keep_recent_tool_outputs,
        max_recent_tool_output_chars=max_recent_tool_output_chars,
        summarize_tool_output=summarize_tool_output,
        summarize_tool_arguments=summarize_tool_arguments,
        suffix=tool_output_truncation_suffix,
    )
    if token_budget is None:
        return items

    assert estimate_tokens is not None
    if estimate_tokens(items) <= token_budget:
        return items

    if is_synthetic_message is not None and synthetic_message_placeholder is not None:
        synthetic_indices = [i for i, item in enumerate(items) if is_synthetic_message(item)]
        drop_indices = set(synthetic_indices[:-1])
        items = [
            _replacement(synthetic_message_placeholder, item) if i in drop_indices else item
            for i, item in enumerate(items)
        ]
        if estimate_tokens(items) <= token_budget:
            return items

    if emergency_tool_output_char_cap is not None:
        items = [
            _truncate_tool_output_item(item, emergency_tool_output_char_cap, tool_output_truncation_suffix)
            for item in items
        ]
        if estimate_tokens(items) <= token_budget:
            return items

    return aggressive_prune(items) if aggressive_prune is not None else items


def sanitize_agent_tool_result_for_llm(
    tool_name: str,
    result: Mapping[str, Any],
    *,
    drop_top_level_keys: Collection[str] | None = None,
    drop_data_keys: Collection[str] | None = None,
    replacement_fields: Mapping[str, ReplacementValue] | None = None,
    large_fields: Collection[str] | None = None,
    max_chars: int = 4000,
    truncation_suffix: str = DEFAULT_TRUNCATION_SUFFIX,
) -> dict[str, Any]:
    """Return a model-facing copy of a tool result shaped by caller config."""
    _validate_non_negative("max_chars", max_chars)
    top_drops = set(drop_top_level_keys or ())
    data_drops = set(drop_data_keys or ())
    replacements = replacement_fields or {}
    large_field_names = set(large_fields or ())

    sanitized: dict[str, Any] = {}
    for key, value in result.items():
        if key in top_drops:
            continue
        if key == "data" and isinstance(value, Mapping):
            value = {data_key: data_value for data_key, data_value in value.items() if data_key not in data_drops}
        sanitized[key] = _sanitize_tool_value(
            value,
            field_name=key,
            replacements=replacements,
            large_fields=large_field_names,
            max_chars=max_chars,
            suffix=truncation_suffix,
        )
    return sanitized


def _compact_tool_payloads(
    items: list[Any],
    *,
    keep_recent_tool_outputs: int,
    max_recent_tool_output_chars: int,
    summarize_tool_output: Callable[[str], str] | None,
    summarize_tool_arguments: Callable[[str], str] | None,
    suffix: str,
) -> list[Any]:
    output_indices = [i for i, item in enumerate(items) if _tool_output_field(item) is not None]
    call_indices = [i for i, item in enumerate(items) if get_agent_message_field(item, "type") == "function_call"]
    recent_outputs = set(output_indices[-keep_recent_tool_outputs:]) if keep_recent_tool_outputs > 0 else set()
    recent_calls = set(call_indices[-keep_recent_tool_outputs:]) if keep_recent_tool_outputs > 0 else set()

    compacted: list[Any] = []
    for i, item in enumerate(items):
        output_field = _tool_output_field(item)
        if output_field is not None:
            output = get_agent_message_field(item, output_field)
            if isinstance(output, str):
                new_output = (
                    _truncate_text(output, max_recent_tool_output_chars, suffix)
                    if i in recent_outputs
                    else _summarize_or_truncate(output, summarize_tool_output, max_recent_tool_output_chars, suffix)
                )
                item = replace_agent_message_field(item, output_field, new_output) if new_output != output else item
        elif get_agent_message_field(item, "type") == "function_call" and i not in recent_calls:
            arguments = get_agent_message_field(item, "arguments")
            if isinstance(arguments, str):
                new_arguments = _summarize_or_truncate(
                    arguments,
                    summarize_tool_arguments,
                    max_recent_tool_output_chars,
                    suffix,
                )
                item = (
                    replace_agent_message_field(item, "arguments", new_arguments)
                    if new_arguments != arguments
                    else item
                )
        compacted.append(item)
    return compacted


def _tool_output_field(item: Any) -> str | None:
    if get_agent_message_field(item, "type") == "function_call_output":
        return "output"
    if get_agent_message_field(item, "role") == "tool":
        return "content"
    return None


def _truncate_tool_output_item(item: Any, max_chars: int, suffix: str) -> Any:
    output_field = _tool_output_field(item)
    if output_field is None:
        return item
    output = get_agent_message_field(item, output_field)
    if not isinstance(output, str):
        return item
    new_output = _truncate_text(output, max_chars, suffix)
    return replace_agent_message_field(item, output_field, new_output) if new_output != output else item


def _sanitize_tool_value(
    value: Any,
    *,
    field_name: str | None,
    replacements: Mapping[str, ReplacementValue],
    large_fields: set[str],
    max_chars: int,
    suffix: str,
) -> Any:
    if field_name is not None and field_name in replacements:
        return _replacement(replacements[field_name], value)
    if field_name is not None and field_name in large_fields:
        return _truncate_large_value(value, max_chars, suffix)
    if isinstance(value, Mapping):
        return {
            key: _sanitize_tool_value(
                nested_value,
                field_name=str(key),
                replacements=replacements,
                large_fields=large_fields,
                max_chars=max_chars,
                suffix=suffix,
            )
            for key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_tool_value(
                item,
                field_name=None,
                replacements=replacements,
                large_fields=large_fields,
                max_chars=max_chars,
                suffix=suffix,
            )
            for item in value
        ]
    return value


def _truncate_large_value(value: Any, max_chars: int, suffix: str) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, max_chars, suffix)
    try:
        serialized = json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return _truncate_text(str(value), max_chars, suffix)
    return copy.deepcopy(value) if len(serialized) <= max_chars else _truncate_text(serialized, max_chars, suffix)


def _summarize_or_truncate(
    value: str,
    summarizer: Callable[[str], str] | None,
    max_chars: int,
    suffix: str,
) -> str:
    return summarizer(value) if summarizer is not None else _truncate_text(value, max_chars, suffix)


def _truncate_text(value: str, max_chars: int, suffix: str) -> str:
    return value[:max_chars] + suffix if len(value) > max_chars else value


def _replacement(replacement: ReplacementValue, original: Any) -> Any:
    return replacement(original) if callable(replacement) else copy.deepcopy(replacement)


def _validate_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
