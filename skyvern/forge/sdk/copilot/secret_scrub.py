"""Exact-string scrubbing of filled credential values from tool results.

A value filled in one turn persists in the page's input.value on the cross-turn
debug session, so registered values are also kept in a session-keyed registry to
stay scrubbed on readbacks in later turns whose per-turn context is empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"

_SESSION_SCRUB_VALUES: dict[str, list[str]] = {}
# No deterministic per-session teardown exists, so FIFO-evict to bound worker memory.
_MAX_SCRUB_SESSIONS = 1024


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


def scrub_secrets_from_structure(ctx: AgentContext, obj: Any) -> Any:
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
