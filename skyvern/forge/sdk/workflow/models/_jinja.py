"""Standalone Jinja environment shared by block modules to avoid import cycles."""

from __future__ import annotations

import io
import json
import tokenize
from typing import Any, Callable, cast

import structlog
from jinja2 import ChainableUndefined, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter

LOG = structlog.get_logger()

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


_MASKED_COMMENT_SENTINEL = "# __SKYVERN_MASKED_COMMENT_{index}__"
_JINJA_DELIMITERS = ("{{", "{%", "{#")


def mask_jinja_in_python_comments(source: str) -> tuple[str, dict[str, str]]:
    """Hide Jinja delimiters inside Python `#` comments behind inert sentinel comments.

    A comment never executes, so template syntax in one must not be able to fail the render
    (a bare `{{ }}` is a Jinja syntax error) or splice a rendered value into the source. Returns
    the masked code plus the sentinel -> original comment map for restore_jinja_masked_comments.
    Any source Python cannot tokenize is returned unchanged, preserving the previous behavior.
    """
    if not any(delimiter in source for delimiter in _JINJA_DELIMITERS):
        return source, {}

    try:
        comments = [
            token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT and any(d in token.string for d in _JINJA_DELIMITERS)
        ]
    except (IndentationError, SyntaxError, tokenize.TokenError, UnicodeDecodeError, ValueError) as exc:
        LOG.warning("Could not tokenize code to mask Jinja in its comments; rendering it as-is", error=str(exc))
        return source, {}

    if not comments:
        return source, {}

    lines = source.splitlines(keepends=True)
    masked: dict[str, str] = {}
    # Reverse order so an earlier comment's replacement cannot shift a later one's columns.
    for index, token in reversed(list(enumerate(comments))):
        sentinel = _MASKED_COMMENT_SENTINEL.format(index=index)
        masked[sentinel] = token.string
        row = token.start[0] - 1
        line = lines[row]
        lines[row] = line[: token.start[1]] + sentinel + line[token.end[1] :]

    return "".join(lines), masked


def restore_jinja_masked_comments(rendered: str, masked: dict[str, str]) -> str:
    for sentinel, comment in masked.items():
        rendered = rendered.replace(sentinel, comment)
    return rendered


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


class RequiredBindingUndefined(ChainableUndefined):
    """Raises where an undefined is rendered directly into the payload.

    StrictUndefined would also break `{% if %}` guards, `{% for %}` over an absent collection, and
    `| default(...)` on a producer block that never ran — all legitimate ways to author a conditional
    payload — so only `__str__` fails. The trade-off is that filters consuming an undefined as a
    sequence (`join`, `length`) still emit, because raising on iteration would take `{% for %}` with it.
    """

    __slots__ = ()

    def __str__(self) -> str:
        self._fail_with_undefined_error()
        raise AssertionError("unreachable")


def _json_finalize_required_binding(value: Any) -> Any:
    # A producer that returned null said "no value"; rendering it into a quoted JSON slot would write
    # the text "None". An omitted binding is the loud case and is handled by the undefined type above.
    if value is None:
        return ""
    return _json_finalize(value)


# A payload a block sends to an external system cannot distinguish "the producer omitted this" from
# "the user wanted an empty value" once an undefined reference renders as "". Rendering one under this
# environment raises instead, and stays strict regardless of WORKFLOW_TEMPLATING_STRICTNESS.
jinja_json_finalize_required_binding_env = SandboxedEnvironment(
    undefined=RequiredBindingUndefined, finalize=_json_finalize_required_binding
)
jinja_json_finalize_required_binding_env.filters["json"] = _json_type_filter

if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
    jinja_json_finalize_strict_env = SandboxedEnvironment(undefined=StrictUndefined, finalize=_json_finalize)
else:
    jinja_json_finalize_strict_env = SandboxedEnvironment(finalize=_json_finalize)
jinja_json_finalize_strict_env.filters["json"] = _json_type_filter
