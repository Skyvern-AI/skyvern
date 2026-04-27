"""Standalone Jinja environment shared by block modules to avoid import cycles."""

from __future__ import annotations

import json
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

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


jinja_json_finalize_env = SandboxedEnvironment(finalize=_json_finalize)
jinja_json_finalize_env.filters["json"] = _json_type_filter
