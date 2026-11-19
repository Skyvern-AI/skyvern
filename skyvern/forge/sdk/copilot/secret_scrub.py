"""Agent-context scrubbing of filled credential values from tool results, on the shared registry in
``skyvern.utils.secret_scrub``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.utils.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    encoded_secret_variants,
    read_session_scrub_values,
    register_session_scrub_values,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()


def _session_id(ctx: AgentContext) -> str | None:
    session_id = getattr(ctx, "browser_session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def register_secret_scrub_value(ctx: AgentContext, value: str | None) -> None:
    """Register a value and every encoded form of it, so a page that echoes the value URL- or
    HTML-encoded is scrubbed by the same exact-string pass."""
    if not isinstance(value, str) or not value:
        return
    values = getattr(ctx, "secret_scrub_values", None)
    variants = encoded_secret_variants(value)
    if isinstance(values, list):
        for variant in variants:
            if variant not in values:
                values.append(variant)
    register_session_scrub_values(_session_id(ctx), variants)


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
        merged.extend(read_session_scrub_values(session_id))
    # Longest first so an overlapping shorter value never splits a longer one.
    return sorted(set(merged), key=len, reverse=True)


def scrub_secrets_from_text(ctx: AgentContext, text: str) -> str:
    for value in _registered_scrub_values(ctx):
        text = text.replace(value, REDACTED_SECRET_PLACEHOLDER)
    return text


def is_registered_scrub_value(ctx: AgentContext, value: object) -> bool:
    """True when this exact string is one of the values registered for scrubbing.

    A caller that preserves a machine token across a scrub asks this first: the token itself may be
    the secret, and writing it back would undo the scrub for the one value that needed it.
    """
    return isinstance(value, str) and value in _registered_scrub_values(ctx)


def scrub_secrets_from_structure(ctx: AgentContext, obj: Any) -> Any:
    return _scrub_structure(_registered_scrub_values(ctx), obj)


def _scrub_structure(values: list[str], obj: Any) -> Any:
    # Lazy: output_utils costs ~7.7s to import, which callers that only register or read values should not pay.
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
