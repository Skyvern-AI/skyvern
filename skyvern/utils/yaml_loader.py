"""YAML loading helpers.

PyYAML's default ``SafeLoader`` resolves ISO 8601 strings into Python
``datetime`` / ``date`` objects via the ``tag:yaml.org,2002:timestamp``
implicit resolver. That breaks downstream JSON serialization when users
embed datetime-like strings inside free-form JSON parameter defaults
(e.g. ``"created_at": "2023-10-27T10:00:00Z"``).

``NoDatesSafeLoader`` is a ``SafeLoader`` subclass with the timestamp
resolver removed so such strings stay as plain ``str`` values.
"""

from typing import Any

import yaml


class NoDatesSafeLoader(yaml.SafeLoader):
    """SafeLoader that does not auto-convert ISO 8601 strings to datetime."""


# Remove the implicit timestamp resolver from this loader only, leaving
# the global yaml.SafeLoader untouched.
NoDatesSafeLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def safe_load_no_dates(stream: Any) -> Any:
    """``yaml.safe_load`` variant that keeps ISO 8601 strings as strings.

    Implemented by constructing ``NoDatesSafeLoader`` directly (the same
    pattern ``yaml.safe_load`` uses internally) instead of calling
    ``yaml.load(...)``. ``NoDatesSafeLoader`` is a ``SafeLoader`` subclass,
    so this is just as safe — but avoiding ``yaml.load`` keeps SAST
    scanners from flagging a false-positive unsafe-deserialization.
    """
    loader = NoDatesSafeLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def format_yaml_error(exc: yaml.YAMLError) -> str:
    """Build a user-actionable detail string from a PyYAML parse error.

    ``MarkedYAMLError`` (parent of ScannerError/ParserError/ComposerError) carries
    a ``problem`` description plus 1-indexed-friendly line/column marks. Plain
    ``yaml.YAMLError`` instances fall back to ``str(exc)``.
    """
    if isinstance(exc, yaml.MarkedYAMLError):
        problem = (exc.problem or "could not parse YAML").strip()
        parts = [f"Invalid YAML: {problem}"]
        if exc.problem_mark is not None:
            parts.append(f"at line {exc.problem_mark.line + 1}, column {exc.problem_mark.column + 1}")
        if exc.context and exc.context_mark is not None:
            parts.append(
                f"({exc.context.strip()} at line {exc.context_mark.line + 1}, column {exc.context_mark.column + 1})"
            )
        return " ".join(parts)
    message = str(exc).strip()
    return f"Invalid YAML: {message}" if message else "Invalid YAML"
