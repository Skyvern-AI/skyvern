"""Sanitized loaded-result evidence shared by Copilot steering and blockers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
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
_MAX_LOADED_RESULT_TARGETS = 3
_MAX_SAMPLE_ROWS = 3
_MAX_SAMPLE_ROW_CHARS = 160
_MAX_TEXT_EXCERPT_CHARS = 240
_MAX_SELECTOR_CHARS = 240
_COMPOSITION_TARGET_SUMMARY_CHAR_BUDGET = 900


@dataclass(frozen=True)
class LoadedResultCompositionTarget:
    selector: str = ""
    is_table: bool = False
    row_selector: str = ""
    row_count: int | None = None
    sample_rows: tuple[str, ...] = ()
    text_excerpt: str = ""
    structure_signature: str = ""
    evidence_source: str = ""
    observation_id: str = ""


@dataclass(frozen=True)
class LoadedResultCompositionEvidence:
    result_container_count: int
    table_result_container_count: int
    targets: tuple[LoadedResultCompositionTarget, ...] = ()
    structure_signature: str = ""


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


def _bounded_text(value: object, max_chars: int) -> str:
    text = str(value).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _bounded_optional_text(value: object, max_chars: int) -> str:
    return _bounded_text(value, max_chars) if isinstance(value, str) else ""


def _sample_row_text(value: object) -> str:
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return str(value)
    return str(value)


def _bounded_sample_rows(container: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("sample_rows", "rows", "items"):
        raw_rows = container.get(key)
        if not isinstance(raw_rows, list):
            continue
        rows = tuple(
            _bounded_text(_sample_row_text(row), _MAX_SAMPLE_ROW_CHARS)
            for row in raw_rows[:_MAX_SAMPLE_ROWS]
            if _is_meaningful_data(row)
        )
        if rows:
            return rows
    return ()


def _safe_row_count(value: object) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _target_signature_payload(target: LoadedResultCompositionTarget) -> dict[str, object]:
    return {
        "is_table": target.is_table,
        "row_count": target.row_count,
    }


def _structure_signature(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def loaded_result_target_structure_signature(*, is_table: bool, row_count: int | None) -> str:
    return _structure_signature(
        {
            "is_table": is_table,
            "row_count": row_count,
        }
    )


def _loaded_result_composition_target(container: Mapping[str, Any]) -> LoadedResultCompositionTarget:
    target = LoadedResultCompositionTarget(
        selector=_bounded_optional_text(container.get("selector"), _MAX_SELECTOR_CHARS),
        is_table=_result_container_is_table(container),
        row_selector=_bounded_optional_text(container.get("row_selector"), _MAX_SELECTOR_CHARS),
        row_count=_safe_row_count(container.get("row_count")),
        sample_rows=_bounded_sample_rows(container),
        text_excerpt=_bounded_optional_text(
            container.get("content_excerpt")
            or container.get("sample_text")
            or container.get("text")
            or container.get("text_excerpt")
            or container.get("visible_results_evidence"),
            _MAX_TEXT_EXCERPT_CHARS,
        ),
        evidence_source=_bounded_optional_text(container.get("evidence_source"), _MAX_SELECTOR_CHARS),
        observation_id=_bounded_optional_text(container.get("observation_id"), _MAX_SELECTOR_CHARS),
    )
    return LoadedResultCompositionTarget(
        selector=target.selector,
        is_table=target.is_table,
        row_selector=target.row_selector,
        row_count=target.row_count,
        sample_rows=target.sample_rows,
        text_excerpt=target.text_excerpt,
        structure_signature=loaded_result_target_structure_signature(
            is_table=target.is_table,
            row_count=target.row_count,
        ),
        evidence_source=target.evidence_source,
        observation_id=target.observation_id,
    )


def _target_summary(target: LoadedResultCompositionTarget, *, include_samples: bool = True) -> dict[str, object]:
    summary: dict[str, object] = {
        "selector": target.selector,
        "is_table": target.is_table,
    }
    if target.row_selector:
        summary["row_selector"] = target.row_selector
    if target.row_count is not None:
        summary["row_count"] = target.row_count
    if include_samples and target.sample_rows:
        summary["sample_rows"] = list(target.sample_rows)
    if include_samples and target.text_excerpt:
        summary["text_excerpt"] = target.text_excerpt
    summary["structure_signature"] = target.structure_signature
    if target.evidence_source:
        summary["evidence_source"] = target.evidence_source
    if target.observation_id:
        summary["observation_id"] = target.observation_id
    return summary


def _summary_with_targets(
    evidence: LoadedResultCompositionEvidence, targets: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "result_container_count": evidence.result_container_count,
        "table_result_container_count": evidence.table_result_container_count,
        "targets": targets,
        "structure_signature": evidence.structure_signature,
    }


def _summary_over_budget(summary: dict[str, object]) -> bool:
    return len(json.dumps(summary, default=str, separators=(",", ":"))) > _COMPOSITION_TARGET_SUMMARY_CHAR_BUDGET


def _summary_len(summary: dict[str, object]) -> int:
    return len(json.dumps(summary, default=str, separators=(",", ":")))


def _truncate_target_text_field_to_budget(
    summary: dict[str, object], target: dict[str, object], field_name: str
) -> None:
    value = target.get(field_name)
    if not isinstance(value, str) or not value:
        return
    while _summary_over_budget(summary) and len(value) > 8:
        overage = _summary_len(summary) - _COMPOSITION_TARGET_SUMMARY_CHAR_BUDGET
        trim_by = max(overage + 3, max(len(value) // 4, 1))
        next_len = max(8, len(value) - trim_by)
        value = value[:next_len].rstrip()
        target[field_name] = value + "..."


def _cap_loaded_result_composition_summary(summary: dict[str, object]) -> dict[str, object]:
    capped = deepcopy(summary)
    if not _summary_over_budget(capped):
        return capped
    targets = capped.get("targets")
    if not isinstance(targets, list):
        return capped
    for target in targets:
        if isinstance(target, dict):
            target.pop("text_excerpt", None)
    if not _summary_over_budget(capped):
        return capped
    for target in targets:
        if isinstance(target, dict):
            target.pop("sample_rows", None)
    if not _summary_over_budget(capped):
        return capped
    for target in targets:
        if isinstance(target, dict):
            target.pop("evidence_source", None)
            target.pop("observation_id", None)
    if not _summary_over_budget(capped):
        return capped
    while len(targets) > 1 and _summary_over_budget(capped):
        targets.pop()
    if not targets or not isinstance(targets[0], dict) or not _summary_over_budget(capped):
        return capped
    first_target = targets[0]
    first_target.pop("row_selector", None)
    if not _summary_over_budget(capped):
        return capped
    _truncate_target_text_field_to_budget(capped, first_target, "selector")
    return capped


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
    targets = tuple(
        _loaded_result_composition_target(container) for container in meaningful[:_MAX_LOADED_RESULT_TARGETS]
    )
    return LoadedResultCompositionEvidence(
        result_container_count=len(meaningful),
        table_result_container_count=sum(1 for container in meaningful if _result_container_is_table(container)),
        targets=targets,
        structure_signature=_structure_signature([_target_signature_payload(target) for target in targets]),
    )


def loaded_result_composition_target_summary(evidence: LoadedResultCompositionEvidence) -> dict[str, object]:
    summary = _summary_with_targets(evidence, [_target_summary(target) for target in evidence.targets])
    return _cap_loaded_result_composition_summary(summary)


COVERAGE_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_VALUE_BEARING_LIST_KEYS = ("sample_rows", "rows", "items")
_VALUE_BEARING_TEXT_KEYS = ("content_excerpt", "sample_text", "text", "text_excerpt", "visible_results_evidence")


def _result_container_value_tokens(container: Mapping[str, Any]) -> set[str]:
    parts: list[str] = []
    for key in _VALUE_BEARING_LIST_KEYS:
        value = container.get(key)
        if isinstance(value, list):
            parts.extend(_sample_row_text(item) for item in value if _is_meaningful_data(item))
    for key in _VALUE_BEARING_TEXT_KEYS:
        value = container.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    joined = " ".join(parts).lower()
    return {token for token in COVERAGE_TOKEN_RE.findall(joined) if not token.isdigit()}


def covered_output_paths_in_result_containers(
    raw_containers: object, coverage_tokens_by_path: Mapping[str, frozenset[str]]
) -> set[str]:
    """Credit a requested output path only when one of its identifying tokens appears in a
    value-bearing surface of a content-bearing container. Selector/row_selector are never
    consulted, so an empty result shell with matching selector tokens stays uncovered."""
    if not isinstance(raw_containers, list) or not coverage_tokens_by_path:
        return set()
    covered: set[str] = set()
    for container in raw_containers:
        if not isinstance(container, Mapping) or not _result_container_has_content(container):
            continue
        value_tokens = _result_container_value_tokens(container)
        if not value_tokens:
            continue
        for path, tokens in coverage_tokens_by_path.items():
            if path not in covered and tokens and (tokens & value_tokens):
                covered.add(path)
    return covered
