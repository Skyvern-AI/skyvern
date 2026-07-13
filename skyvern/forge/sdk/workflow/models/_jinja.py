"""Standalone Jinja environment shared by block modules to avoid import cycles."""

from __future__ import annotations

import json
from typing import Any, Callable, cast

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter

# Sentinel marker for native JSON type injection via | json filter.
_JSON_TYPE_MARKER = "__SKYVERN_RAW_JSON__"


def _json_type_filter(value: Any) -> str:
    """Jinja filter that marks a value for native JSON type injection.

    Usage in templates: {{ some_bool | json }}

    The filter serializes the value to JSON and wraps it with sentinel markers.
    When _render_templates_in_json() detects these markers, it unwraps and
    parses the JSON to get the native typed value (bool, int, list, etc.).

    Uses default=str to handle non-JSON-serializable types (datetime, Enum, etc.)
    """
    return f"{_JSON_TYPE_MARKER}{json.dumps(value, default=str)}{_JSON_TYPE_MARKER}"


def _json_finalize(value: Any) -> Any:
    """Jinja finalize hook: JSON-serialize dict/list values so `{{ var }}` yields valid JSON
    instead of Python repr. Strings (including `| tojson` output) pass through unchanged."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def render_templates_in_json_value(value: object, render_string: Callable[[str], str]) -> object:
    """Recursively render Jinja templates in nested JSON-like structures, honoring the
    `{{ expr | json }}` filter for type-preserving JSON injection."""
    if isinstance(value, str):
        rendered = render_string(value)
        if rendered.startswith(_JSON_TYPE_MARKER) and rendered.endswith(_JSON_TYPE_MARKER):
            json_str = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                raise FailedToFormatJinjaStyleParameter(value, f"Raw JSON filter produced invalid JSON: {json_str}")
        if _JSON_TYPE_MARKER in rendered:
            raise FailedToFormatJinjaStyleParameter(
                value,
                "The '| json' filter can only be used for complete value replacement. "
                "It cannot be combined with other text (e.g., 'prefix-{{ val | json }}'). "
                "Remove the surrounding text or remove the '| json' filter.",
            )
        return rendered
    if isinstance(value, list):
        return [render_templates_in_json_value(item, render_string) for item in value]
    if isinstance(value, dict):
        return {
            cast(str, render_templates_in_json_value(key, render_string)): render_templates_in_json_value(
                val, render_string
            )
            for key, val in value.items()
        }
    return value


jinja_json_finalize_env = SandboxedEnvironment(finalize=_json_finalize)
jinja_json_finalize_env.filters["json"] = _json_type_filter

if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
    jinja_json_finalize_strict_env = SandboxedEnvironment(undefined=StrictUndefined, finalize=_json_finalize)
else:
    jinja_json_finalize_strict_env = SandboxedEnvironment(finalize=_json_finalize)
jinja_json_finalize_strict_env.filters["json"] = _json_type_filter
