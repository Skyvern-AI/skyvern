"""Sanitized loaded-result evidence shared by Copilot steering and blockers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Browser UI chrome, not Google Chrome. Keep this to generic empty-state and control text.
_RESULT_CONTAINER_UI_CHROME_TOKENS = frozenset(
    {
        "and",
        "are",
        "bot",
        "button",
        "captcha",
        "challenge",
        "clear",
        "control",
        "disabled",
        "enter",
        "first",
        "form",
        "found",
        "full",
        "human",
        "last",
        "lookup",
        "matching",
        "name",
        "record",
        "records",
        "reset",
        "result",
        "results",
        "search",
        "select",
        "state",
        "submit",
        "verification",
        "verify",
        "you",
        "your",
    }
)
_EMPTY_RESULT_TEXT_PATTERNS = (
    re.compile(r"\bno\s+(?:matching\s+)?(?:results?|records?)(?:\s+found)?\b"),
    re.compile(r"\b0\s+(?:matching\s+)?(?:results?|records?)(?:\s+found)?\b"),
)
_TRANSIENT_RESULT_TEXT_PATTERNS = (
    re.compile(r"(?:loading|please\s+wait|searching|fetching|processing)(?:\s+(?:results?|records?|items?))?\W*"),
    re.compile(
        r"(?:showing|displaying)\s+\d+\s*(?:-|to|through)\s*\d+\s+(?:of|/)\s+\d+(?:\s+(?:results?|records?|items?))?\W*"
    ),
)
_MAX_MEANINGFUL_DATA_DEPTH = 10


@dataclass(frozen=True)
class LoadedResultCompositionEvidence:
    result_container_count: int
    table_result_container_count: int


def _is_meaningful_data(value: Any, *, _depth: int = 0) -> bool:
    if _depth > _MAX_MEANINGFUL_DATA_DEPTH:
        return False
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return bool(value)
    if isinstance(value, Mapping):
        return any(_is_meaningful_data(item, _depth=_depth + 1) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_is_meaningful_data(item, _depth=_depth + 1) for item in value)
    return True


def _result_container_has_content(container: Mapping[str, Any]) -> bool:
    row_count = container.get("row_count")
    if isinstance(row_count, int) and row_count > 0:
        return True
    for key in ("sample_rows", "rows", "items"):
        value = container.get(key)
        if isinstance(value, list) and _is_meaningful_data(value):
            return True
    for key in ("content_excerpt", "sample_text", "text", "text_excerpt", "visible_results_evidence"):
        value = container.get(key)
        if isinstance(value, str):
            if _text_has_non_ui_chrome_tokens(value):
                return True
            continue
        if value:
            return True
    return False


def _text_has_non_ui_chrome_tokens(value: str) -> bool:
    normalized = value.strip().lower()
    if any(pattern.search(normalized) for pattern in _EMPTY_RESULT_TEXT_PATTERNS):
        return False
    if any(pattern.fullmatch(normalized) for pattern in _TRANSIENT_RESULT_TEXT_PATTERNS):
        return False
    tokens = {token for token in re.findall(r"[a-z0-9]{2,}", normalized) if not token.isdigit()}
    return bool(tokens - _RESULT_CONTAINER_UI_CHROME_TOKENS)


def _result_container_is_table(container: Mapping[str, Any]) -> bool:
    tag = container.get("tag")
    return (
        (isinstance(tag, str) and tag.lower() == "table")
        or container.get("is_table") is True
        or "row_selector" in container
    )


def loaded_result_composition_evidence_from_page(
    evidence: Mapping[str, Any],
) -> LoadedResultCompositionEvidence | None:
    raw_containers = evidence.get("result_containers")
    if not isinstance(raw_containers, list):
        return None
    containers = [container for container in raw_containers if isinstance(container, Mapping)]
    meaningful = [container for container in containers if _result_container_has_content(container)]
    if not meaningful:
        return None
    return LoadedResultCompositionEvidence(
        result_container_count=len(meaningful),
        table_result_container_count=sum(1 for container in meaningful if _result_container_is_table(container)),
    )


def loaded_result_composition_target_summary(evidence: LoadedResultCompositionEvidence) -> dict[str, int]:
    return {
        "result_container_count": evidence.result_container_count,
        "table_result_container_count": evidence.table_result_container_count,
    }
