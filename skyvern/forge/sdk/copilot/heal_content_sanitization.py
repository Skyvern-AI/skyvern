"""Heal-content sanitization.

Primary masking is write-time and value-based via ``mask_heal_text`` and
``mask_heal_steps`` with a live agent context. ``sanitize_heal_content`` is a
read-time, shape-based backstop and is not sufficient on its own.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure, scrub_secrets_from_text
from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX
from skyvern.schemas.self_heal import HealEpisode, HealEpisodeDetail

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

TRUNCATION_MARKER = "\n…[truncated]"
_PLACEHOLDER_SECRET_RE = re.compile(rf"\b{re.escape(RANDOM_SECRET_ID_PREFIX)}[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")


def _truncate_sanitized_text(text: str, *, max_length: int) -> str:
    if max_length < 0:
        max_length = 0

    if len(text) <= max_length:
        return text

    if text.endswith(TRUNCATION_MARKER) and len(text) == max_length + len(TRUNCATION_MARKER):
        return text

    return f"{text[:max_length]}{TRUNCATION_MARKER}"


def mask_heal_text(ctx: AgentContext, text: str | None, *, max_length: int = 20000) -> str | None:
    if text is None:
        return None

    sanitized = scrub_secrets_from_text(ctx, text)
    sanitized = redact_raw_secrets_for_prompt(sanitized)
    sanitized = _PLACEHOLDER_SECRET_RE.sub("[REDACTED_SECRET]", sanitized)
    return _truncate_sanitized_text(sanitized, max_length=max_length)


def mask_heal_steps(ctx: AgentContext, steps: Any) -> Any:
    return scrub_secrets_from_structure(ctx, steps)


def sanitize_heal_content(text: str | None, *, max_length: int = 20000) -> str | None:
    if text is None:
        return None

    sanitized = redact_raw_secrets_for_prompt(text)
    sanitized = _PLACEHOLDER_SECRET_RE.sub("[REDACTED_SECRET]", sanitized)
    return _truncate_sanitized_text(sanitized, max_length=max_length)


def _sanitize_string_leaves(node: Any) -> Any:
    if isinstance(node, str):
        return sanitize_heal_content(node)
    if isinstance(node, list):
        return [_sanitize_string_leaves(item) for item in node]
    if isinstance(node, tuple):
        return tuple(_sanitize_string_leaves(item) for item in node)
    if isinstance(node, dict):
        return {key: _sanitize_string_leaves(value) for key, value in node.items()}
    return node


def build_heal_episode_detail(episode: HealEpisode) -> HealEpisodeDetail:
    return HealEpisodeDetail(
        **episode.model_dump(exclude={"block_code", "block_prompt", "failure_message", "block_steps"}),
        sanitized_block_code=sanitize_heal_content(episode.block_code),
        sanitized_block_prompt=sanitize_heal_content(episode.block_prompt),
        sanitized_failure_message=sanitize_heal_content(episode.failure_message),
        block_steps=_sanitize_string_leaves(episode.block_steps),
    )
