"""Contract-owned JIT structural-read plans for requested Copilot outputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeGuard


class LiveReadKind(StrEnum):
    KEY_VALUE = "key_value"
    TABLE_COLUMN = "table_column"


@dataclass(frozen=True, slots=True)
class RevealAnchor:
    selector: str = ""
    role: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if bool(self.selector) == bool(self.role and self.name):
            raise ValueError("Reveal anchor must contain exactly one selector or role/name pair")


@dataclass(frozen=True, slots=True)
class LiveReadBinding:
    output_path: str
    kind: LiveReadKind
    selector: str
    selector_count: int
    selector_index: int
    child_index: int = 0
    child_count: int = 0
    row_selector: str = ""
    row_count: int = 0
    column_index: int = 0
    relation_label: str = ""
    headers: tuple[str, ...] = ()
    row_cell_counts: tuple[int, ...] = ()
    row_identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestedOutputExtractionPlan:
    requested_output_paths: tuple[str, ...]
    observation_step: int
    observation_identity: str
    reveal: RevealAnchor
    live_reads: tuple[LiveReadBinding, ...]
    identity: str


@dataclass(frozen=True, slots=True)
class FrozenRequestedOutputExtractionCandidate:
    plan_identity: str
    observation_identity: str
    requested_output_paths: tuple[str, ...]
    reveal: RevealAnchor
    interaction_code: str
    extraction_code: str
    source: str
    admission_result: str
    fingerprint: str


def _stable_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _leaf_paths(paths: set[str]) -> set[str]:
    return {
        path
        for path in paths
        if path.startswith("output.")
        and not any(
            other != path and (other.startswith(f"{path}.") or other.startswith(f"{path}[]")) for other in paths
        )
    }


def output_path_segments(path: str) -> tuple[tuple[str, bool], ...]:
    segments: list[tuple[str, bool]] = []
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        is_array = "[]" in part
        name = part.replace("[]", "")
        if name:
            segments.append((name, is_array))
    return tuple(segments)


def _exact_path(label: str, labels_by_path: dict[str, tuple[str, ...]]) -> str | None:
    matches = [path for path, labels in labels_by_path.items() if label in labels]
    return matches[0] if len(matches) == 1 else None


def _key_value_bindings(packet: dict[str, Any], labels_by_path: dict[str, tuple[str, ...]]) -> list[LiveReadBinding]:
    relations = packet.get("key_value_relations")
    if not isinstance(relations, list):
        return []
    bindings: list[LiveReadBinding] = []
    for relation in relations:
        if (
            not isinstance(relation, dict)
            or relation.get("visible") is not True
            or relation.get("value_visible") is not True
        ):
            continue
        label = relation.get("key_text")
        selector = relation.get("container_selector")
        match_count = relation.get("container_match_count")
        position = relation.get("container_position")
        child_index = relation.get("value_child_index")
        child_count = relation.get("direct_child_count")
        if not isinstance(label, str) or not isinstance(selector, str):
            continue
        if not _is_int(match_count) or not _is_int(position) or not _is_int(child_index) or not _is_int(child_count):
            continue
        if match_count <= position or position < 0 or child_index < 0 or child_count <= child_index:
            continue
        output_path = _exact_path(label, labels_by_path)
        if output_path is None:
            continue
        bindings.append(
            LiveReadBinding(
                output_path,
                LiveReadKind.KEY_VALUE,
                selector,
                match_count,
                position,
                child_index,
                child_count,
                relation_label=label,
            )
        )
    return bindings


def _table_bindings(packet: dict[str, Any], labels_by_path: dict[str, tuple[str, ...]]) -> list[LiveReadBinding]:
    containers = packet.get("result_containers")
    if not isinstance(containers, list):
        return []
    bindings: list[LiveReadBinding] = []
    for container in containers:
        if (
            not isinstance(container, dict)
            or container.get("visible") is not True
            or container.get("span_free") is not True
            or container.get("nested_table_free") is not True
        ):
            continue
        selector, row_selector = container.get("selector"), container.get("row_selector")
        match_count, row_count = container.get("selector_match_count"), container.get("row_count")
        headers, rows, sample_rows = container.get("headers"), container.get("rows"), container.get("sample_rows")
        if (
            not isinstance(selector, str)
            or not isinstance(row_selector, str)
            or not _is_int(match_count)
            or match_count != 1
        ):
            continue
        if not _is_int(row_count) or row_count <= 0 or container.get("rows_truncated") is not False:
            continue
        if not isinstance(headers, list) or not isinstance(rows, list) or len(rows) != row_count:
            continue
        if (
            not isinstance(sample_rows, list)
            or len(sample_rows) != row_count
            or not all(isinstance(value, str) for value in sample_rows)
        ):
            continue
        header_label_list: list[str] = []
        for header in headers:
            if isinstance(header, dict) and isinstance(header.get("text"), str):
                header_label_list.append(header["text"])
        header_labels = tuple(header_label_list)
        if len(header_labels) != len(headers):
            continue
        row_cell_counts: list[int] = []
        for row_index, row in enumerate(rows):
            if (
                not isinstance(row, dict)
                or row.get("row_index") != row_index
                or row.get("visible") is not True
                or row.get("has_row_header") is not False
            ):
                break
            cells = row.get("cells")
            if (
                not isinstance(cells, list)
                or len(cells) != len(headers)
                or any(
                    not isinstance(cell, dict)
                    or cell.get("column_index") != column_index
                    or cell.get("visible") is not True
                    for column_index, cell in enumerate(cells)
                )
            ):
                break
            row_cell_counts.append(len(cells))
        if len(row_cell_counts) != row_count:
            continue
        for header in headers:
            column_index = header.get("column_index") if isinstance(header, dict) else None
            if not isinstance(header, dict) or not isinstance(header.get("text"), str) or not _is_int(column_index):
                continue
            output_path = _exact_path(header["text"], labels_by_path)
            if output_path is None:
                continue
            bindings.append(
                LiveReadBinding(
                    output_path,
                    LiveReadKind.TABLE_COLUMN,
                    selector,
                    match_count,
                    0,
                    row_selector=row_selector,
                    row_count=row_count,
                    column_index=column_index,
                    relation_label=header["text"],
                    headers=header_labels,
                    row_cell_counts=tuple(row_cell_counts),
                    row_identities=tuple(sample_rows),
                )
            )
    return bindings


def _array_prefix(path: str) -> tuple[tuple[str, bool], ...]:
    segments = output_path_segments(path)
    for index, (_, is_array) in enumerate(segments):
        if is_array:
            return segments[: index + 1]
    return ()


def _plan_from_entry(
    entry: dict[str, Any], *, labels_by_path: dict[str, tuple[str, ...]]
) -> RequestedOutputExtractionPlan | None:
    if entry.get("reached_via") != "interaction" or entry.get("had_bounded_schema") is not True:
        return None
    step, packet = entry.get("step"), entry.get("evidence")
    if not _is_int(step) or not isinstance(packet, dict) or packet.get("source_tool") != "scout_interaction":
        return None
    if (
        packet.get("result_containers_truncated") is not False
        or packet.get("key_value_relations_truncated") is not False
    ):
        return None
    if isinstance(packet.get("inspection_warnings"), list) and packet["inspection_warnings"]:
        return None
    selector, role, name = (
        packet.get("interaction_selector"),
        packet.get("interaction_role"),
        packet.get("interaction_accessible_name"),
    )
    if isinstance(selector, str) and selector:
        reveal = RevealAnchor(selector=selector)
    elif isinstance(role, str) and role and isinstance(name, str) and name:
        reveal = RevealAnchor(role=role, name=name)
    else:
        return None
    leaf_paths = _leaf_paths(set(labels_by_path))
    live_reads = _key_value_bindings(packet, labels_by_path) + _table_bindings(packet, labels_by_path)
    by_path: dict[str, list[LiveReadBinding]] = {}
    for binding in live_reads:
        by_path.setdefault(binding.output_path, []).append(binding)
    if any(len(by_path.get(path, [])) != 1 for path in leaf_paths):
        return None
    ordered_reads = tuple(by_path[path][0] for path in sorted(leaf_paths))
    tables_by_array: dict[tuple[tuple[str, bool], ...], set[tuple[str, int]]] = {}
    for binding in ordered_reads:
        prefix = _array_prefix(binding.output_path)
        if prefix:
            tables_by_array.setdefault(prefix, set()).add((binding.selector, binding.selector_index))
    if any(len(tables) != 1 for tables in tables_by_array.values()):
        return None
    # The step is provenance, not structure: an identical re-observation must not
    # invalidate the candidate frozen from the earlier offer.
    observation_identity = _stable_identity(repr((reveal, ordered_reads)))
    identity = _stable_identity(repr((tuple(sorted(labels_by_path)), observation_identity)))
    return RequestedOutputExtractionPlan(
        tuple(sorted(labels_by_path)), step, observation_identity, reveal, ordered_reads, identity
    )


def derive_requested_output_extraction_plan(
    *, flow_evidence: list[dict[str, Any]], labels_by_path: dict[str, tuple[str, ...]]
) -> RequestedOutputExtractionPlan | None:
    """Derive from one rollback-owned packet; never combine partial observations."""
    if not labels_by_path:
        return None
    for entry in reversed(flow_evidence):
        if isinstance(entry, dict) and entry.get("reached_via") == "interaction":
            return _plan_from_entry(entry, labels_by_path=labels_by_path)
    return None
