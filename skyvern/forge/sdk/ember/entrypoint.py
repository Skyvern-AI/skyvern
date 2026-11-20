"""Read-only helpers for recovering a workflow entrypoint URL."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import yaml

from skyvern.utils.yaml_loader import safe_load_no_dates

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"\)]+", re.IGNORECASE)
_ANCHOR_URL_TRAILING_PUNCTUATION = ",.;:!?)]}'\"`"
_ANCHOR_URL_MARKDOWN_WRAPPERS = frozenset("*_~")
_ANCHOR_TRUNCATION_SENTINEL = "…"


def extract_anchor_entry_url(text: str | None) -> str | None:
    """Extract a complete HTTP(S) URL from the earliest-turn transcript anchor."""
    if not text:
        return None
    for match in _URL_IN_TEXT_RE.finditer(text):
        start, end = match.span()
        if text[end : end + 1] == "<" or text[start - 1 : start] == ">":
            continue
        if _ANCHOR_TRUNCATION_SENTINEL in text[max(0, start - 2) : end + 2]:
            continue
        candidate = match.group(0).rstrip(_ANCHOR_URL_TRAILING_PUNCTUATION)
        preceding = text[start - 1 : start]
        if preceding in _ANCHOR_URL_MARKDOWN_WRAPPERS:
            candidate = candidate.rstrip(preceding)
        try:
            parsed = urlparse(candidate)
            port = parsed.port
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if "." in parsed.hostname or port is not None or parsed.hostname == "localhost":
            return candidate
    return None


def _first_yaml_target_url(workflow_yaml: str | None) -> str | None:
    if not workflow_yaml:
        return None
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return None
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return None
    for raw_block in blocks:
        block: dict[str, Any] = raw_block if isinstance(raw_block, dict) else {}
        if block.get("block_type") not in {"goto_url", "navigation"}:
            continue
        url = block.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def extract_in_turn_entry_url(user_message: str, agent_user_message: str, workflow_yaml: str | None) -> str | None:
    for text in (user_message, agent_user_message):
        match = _URL_IN_TEXT_RE.search(text or "")
        if match:
            candidate = match.group(0).rstrip(_ANCHOR_URL_TRAILING_PUNCTUATION)
            if candidate:
                return candidate
    return _first_yaml_target_url(workflow_yaml)


def anchor_recovers_entrypoint(
    user_message: str,
    agent_user_message: str,
    workflow_yaml: str | None,
    transcript_earliest_user_turn: str = "",
) -> str | None:
    """Return an anchor URL only when the current turn has no newer entrypoint."""
    if (
        _URL_IN_TEXT_RE.search(user_message or "")
        or _URL_IN_TEXT_RE.search(agent_user_message or "")
        or _first_yaml_target_url(workflow_yaml)
    ):
        return None
    return extract_anchor_entry_url(transcript_earliest_user_turn)


def resolve_turn_entrypoint_url(
    *,
    eval_entrypoint_url: str | None,
    in_turn_entrypoint: str | None,
    anchor_entrypoint: str | None,
    persisted_entrypoint_url: str | None,
    current_entrypoint_url: str | None,
) -> str | None:
    """The benchmark seed outranks in-turn extraction because an instruction that happens to name a
    domain would otherwise silently beat the dataset URL the run is supposed to start from."""
    if eval_entrypoint_url:
        return eval_entrypoint_url
    if in_turn_entrypoint is not None:
        return in_turn_entrypoint
    if current_entrypoint_url is None:
        return anchor_entrypoint or persisted_entrypoint_url
    return current_entrypoint_url
