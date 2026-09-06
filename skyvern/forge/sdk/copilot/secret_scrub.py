"""Exact-string scrubbing of filled credential values from tool results.

A value filled in one turn persists in the page's input.value on the cross-turn
debug session, so registered values are also kept in a session-keyed registry to
stay scrubbed on readbacks in later turns whose per-turn context is empty.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import structlog

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"

_SESSION_SCRUB_VALUES: dict[str, list[str]] = {}
# No deterministic per-session teardown exists, so FIFO-evict to bound worker memory.
_MAX_SCRUB_SESSIONS = 1024

# Substring-replacing a short value into stored content corrupts it: a six-digit OTP occurs by
# chance inside ports, counts, and ids. Rewrites of the stored workflow apply this floor — both the
# persisted row and the draft an edit anchors against, which have to stay the same string. Log
# redaction does not, because over-redacting a log line is cheap and leaking a secret into one is not.
MIN_PERSISTED_REDACTION_LENGTH = 8

_ALL_VALUES_CACHE: tuple[tuple[tuple[str, tuple[str, ...]], ...], list[str]] | None = None


def _registry_fingerprint() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Cheap key that changes on any registry mutation, including a direct one.

    Keyed by each session's values so clearing and reusing a session ID with the same value count
    cannot serve stale credentials from the prior session lifetime.

    Deriving the key from the data rather than a hand-maintained counter means a caller that
    mutates the dict directly cannot be served a stale list.
    """
    return tuple(sorted((session_id, tuple(values)) for session_id, values in _SESSION_SCRUB_VALUES.items()))


def _session_id(ctx: AgentContext) -> str | None:
    session_id = getattr(ctx, "browser_session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


# encodeURIComponent leaves these literal where Python's quote does not.
_ENCODE_URI_COMPONENT_SAFE = "-_.!~*'()"


def encoded_secret_variants(value: str) -> list[str]:
    """The forms a page can echo a value in: a URL query, escaped markup, a JSON or Python literal."""
    try:
        forms = [
            value,
            html.escape(value, quote=False),
            html.escape(value, quote=True),
            quote(value),
            quote(value, safe=""),
            quote(value, safe=_ENCODE_URI_COMPONENT_SAFE),
            quote_plus(value, safe=""),
            json.dumps(value, ensure_ascii=True)[1:-1],
            repr(value)[1:-1],
            value.encode("unicode_escape").decode("ascii"),
        ]
    except UnicodeError:
        forms = [value]
    return list(dict.fromkeys(form for form in forms if form))


def register_secret_scrub_value(ctx: AgentContext, value: str | None) -> None:
    """Register a value and every encoded form of it, so a page that echoes the value URL- or
    HTML-encoded is scrubbed by the same exact-string pass."""
    if not isinstance(value, str) or not value:
        return
    values = getattr(ctx, "secret_scrub_values", None)
    session_id = _session_id(ctx)
    new_session = session_id is not None and session_id not in _SESSION_SCRUB_VALUES
    for variant in encoded_secret_variants(value):
        if isinstance(values, list) and variant not in values:
            values.append(variant)
        if session_id is not None:
            session_values = _SESSION_SCRUB_VALUES.setdefault(session_id, [])
            if variant not in session_values:
                session_values.append(variant)
    if new_session:
        while len(_SESSION_SCRUB_VALUES) > _MAX_SCRUB_SESSIONS:
            _SESSION_SCRUB_VALUES.pop(next(iter(_SESSION_SCRUB_VALUES)))


def register_secret_scrub_values_from_structure(ctx: AgentContext, obj: Any) -> None:
    """Register string leaves without treating field names as secret values."""
    if isinstance(obj, str):
        register_secret_scrub_value(ctx, obj)
        return
    if isinstance(obj, Mapping):
        for value in obj.values():
            register_secret_scrub_values_from_structure(ctx, value)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for value in obj:
            register_secret_scrub_values_from_structure(ctx, value)


def matching_origin_run_redaction_parameters(ctx: AgentContext, run_id: str | None = None) -> dict[str, Any] | None:
    """Return a mutable copy of the complete, run-bound model-disclosure scrub set."""
    expected_run_id = run_id or getattr(ctx, "last_run_blocks_workflow_run_id", None)
    registry = getattr(ctx, "origin_run_redaction_registry", None)
    if (
        not isinstance(expected_run_id, str)
        or not expected_run_id
        or registry is None
        or registry.workflow_run_id != expected_run_id
        or not registry.contains_sensitive_values
        or not registry.contains_all_sensitive_values
    ):
        return None

    def mutable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: mutable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [mutable(item) for item in value]
        return value

    return mutable(registry.parameters)


def register_matching_origin_run_redaction_values(ctx: AgentContext, run_id: str | None = None) -> bool:
    """Bind the terminal origin run's known values to the existing exact-value scrubber."""
    parameters = matching_origin_run_redaction_parameters(ctx, run_id)
    if parameters is None:
        return False
    register_secret_scrub_values_from_structure(ctx, parameters)
    bound_run_id = run_id or getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if isinstance(bound_run_id, str) and bound_run_id:
        bound_run_ids = getattr(ctx, "origin_runs_bound_to_scrubber", None)
        if not isinstance(bound_run_ids, set):
            bound_run_ids = set()
            ctx.origin_runs_bound_to_scrubber = bound_run_ids
        bound_run_ids.add(bound_run_id)
    return True


def origin_runs_bound_to_scrubber(ctx: AgentContext) -> set[str]:
    """Runs whose complete registry has been registered with this context's scrubber."""
    bound_run_ids = getattr(ctx, "origin_runs_bound_to_scrubber", None)
    return bound_run_ids if isinstance(bound_run_ids, set) else set()


def clear_session_scrub_values(session_id: str | None) -> None:
    if isinstance(session_id, str):
        _SESSION_SCRUB_VALUES.pop(session_id, None)


def all_registered_secret_values() -> list[str]:
    """Every credential value registered by any session in this process, longest first.

    The log seam has no ``AgentContext`` to scope against — a credential value must never reach
    log output regardless of which session filled it. Callers that rewrite persisted content must
    use ``registered_scrub_values`` instead: replacing across sessions there would let one session's
    short value corrupt another's stored data.
    """
    global _ALL_VALUES_CACHE
    fingerprint = _registry_fingerprint()
    cached = _ALL_VALUES_CACHE
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    merged: set[str] = set()
    for values in _SESSION_SCRUB_VALUES.values():
        merged.update(value for value in values if isinstance(value, str) and value)
    computed = sorted(merged, key=len, reverse=True)
    _ALL_VALUES_CACHE = (fingerprint, computed)
    return computed


def registered_scrub_values(ctx: AgentContext) -> list[str]:
    """This turn's and this session's registered values, longest first."""
    return _registered_scrub_values(ctx)


def _registered_scrub_values(ctx: AgentContext) -> list[str]:
    merged: list[str] = []
    values = getattr(ctx, "secret_scrub_values", None)
    if isinstance(values, list):
        merged.extend(value for value in values if isinstance(value, str) and value)
    session_id = _session_id(ctx)
    if session_id is not None:
        merged.extend(value for value in _SESSION_SCRUB_VALUES.get(session_id, []) if isinstance(value, str) and value)
    # Longest first so an overlapping shorter value never splits a longer one.
    return sorted(set(merged), key=len, reverse=True)


def scrub_secrets_from_text(ctx: AgentContext, text: str) -> str:
    for value in _registered_scrub_values(ctx):
        text = text.replace(value, REDACTED_SECRET_PLACEHOLDER)
    return text


def scrub_all_registered_from_text(text: str) -> str:
    """Session-agnostic counterpart to ``scrub_secrets_from_text`` for reporting seams.

    Exception text is serialized where no ``AgentContext`` is in scope, so this scrubs against
    every session's values for the same reason ``all_registered_secret_values`` does.
    """
    for value in all_registered_secret_values():
        text = text.replace(value, REDACTED_SECRET_PLACEHOLDER)
    return text


def scrub_secrets_from_structure(ctx: AgentContext, obj: Any) -> Any:
    return _scrub_structure(_registered_scrub_values(ctx), obj)


def _scrub_structure(values: list[str], obj: Any) -> Any:
    # Lazy: output_utils costs ~7.7s to import, and this module is on the logging and span
    # exception paths, where that would be paid by whichever request raises first.
    from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64  # noqa: PLC0415

    if not values:
        return obj
    replacements = 0

    def walk(node: Any) -> Any:
        nonlocal replacements
        if isinstance(node, str):
            # A short alphanumeric secret (an OTP code) can occur inside image
            # base64 by coincidence; replacing it would corrupt the image.
            if is_valid_image_base64(node):
                return node
            for value in values:
                if value in node:
                    replacements += node.count(value)
                    node = node.replace(value, REDACTED_SECRET_PLACEHOLDER)
            return node
        if isinstance(node, dict):
            return {walk(key): walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, tuple):
            return tuple(walk(item) for item in node)
        return node

    scrubbed = walk(obj)
    if replacements:
        LOG.info("Scrubbed registered secret values from a tool result", replacements=replacements)
    return scrubbed
