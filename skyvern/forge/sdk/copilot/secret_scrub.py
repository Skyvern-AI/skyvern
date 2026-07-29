"""Exact-string scrubbing of filled credential values from tool results.

A value filled in one turn persists in the page's input.value on the cross-turn
debug session, so registered values are also kept in a session-keyed registry to
stay scrubbed on readbacks in later turns whose per-turn context is empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"

_SESSION_SCRUB_VALUES: dict[str, list[str]] = {}
# No deterministic per-session teardown exists, so FIFO-evict to bound worker memory.
_MAX_SCRUB_SESSIONS = 1024

# Substring-replacing a short value into stored content corrupts it: a six-digit OTP occurs by
# chance inside ports, counts, and ids. Rewrites that persist apply this floor; log redaction does
# not, because over-redacting a log line is cheap and leaking a secret into one is not.
MIN_PERSISTED_REDACTION_LENGTH = 8

_ALL_VALUES_CACHE: tuple[tuple[tuple[str, int], ...], list[str]] | None = None


def _registry_fingerprint() -> tuple[tuple[str, int], ...]:
    """Cheap key that changes on any registry mutation, including a direct one.

    Keyed per session rather than on totals: aggregate counts alone collide whenever one session is
    dropped and another registers the same number of values, which served the dropped session's
    values as the live list and left the new session's credential unscrubbed. Values are
    append-only within a session, so a per-session count moves on every addition.

    Deriving the key from the data rather than a hand-maintained counter means a caller that
    mutates the dict directly cannot be served a stale list.
    """
    return tuple(sorted((session_id, len(values)) for session_id, values in _SESSION_SCRUB_VALUES.items()))


def _session_id(ctx: AgentContext) -> str | None:
    session_id = getattr(ctx, "browser_session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def register_secret_scrub_value(ctx: AgentContext, value: str | None) -> None:
    if not isinstance(value, str) or not value:
        return
    values = getattr(ctx, "secret_scrub_values", None)
    if isinstance(values, list) and value not in values:
        values.append(value)
    session_id = _session_id(ctx)
    if session_id is not None:
        new_session = session_id not in _SESSION_SCRUB_VALUES
        session_values = _SESSION_SCRUB_VALUES.setdefault(session_id, [])
        if value not in session_values:
            session_values.append(value)
        if new_session:
            while len(_SESSION_SCRUB_VALUES) > _MAX_SCRUB_SESSIONS:
                _SESSION_SCRUB_VALUES.pop(next(iter(_SESSION_SCRUB_VALUES)))


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
    # Lazy: output_utils costs ~7.7s to import, and this module is on the logging and span
    # exception paths, where that would be paid by whichever request raises first.
    from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64  # noqa: PLC0415

    values = _registered_scrub_values(ctx)
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
