"""Contract-owned JIT structural-read plans for requested Copilot outputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeGuard


class LiveReadKind(StrEnum):
    KEY_VALUE = "key_value"
    TABLE_COLUMN = "table_column"


class ValueShape(StrEnum):
    NUMERIC_ID = "numeric_id"
    POSTAL_ADDRESS = "postal_address"
    CATEGORICAL_TOKEN = "categorical_token"
    DATE = "date"
    FREE_TEXT = "free_text"


class ValueCardinality(StrEnum):
    SCALAR = "scalar"
    COLUMN = "column"


@dataclass(frozen=True, slots=True)
class ShapeExpectation:
    shape: ValueShape
    cardinality: ValueCardinality
    id_digit_length: int | None = None


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
    # Where the label proving this value's identity lives, when it is not the value's sibling.
    label_selector: str = ""
    # Which direct child carries that label; a tile may print its figure first.
    label_child_index: int = 0
    # Whether the label is what identified this element. A value-witnessed binding chose the element
    # by the value it showed, so re-reading a heading proves nothing about that choice — and a page
    # with no heading has none to re-read.
    identified_by_label: bool = True


@dataclass(frozen=True, slots=True)
class RequestedOutputExtractionPlan:
    requested_output_paths: tuple[str, ...]
    observation_step: int
    observation_identity: str
    # None when the bindings were read off the navigated page; the generated block then replays no
    # reveal click, because there was none to replay.
    reveal: RevealAnchor | None
    live_reads: tuple[LiveReadBinding, ...]
    identity: str


@dataclass(frozen=True, slots=True)
class FrozenRequestedOutputExtractionCandidate:
    plan_identity: str
    observation_identity: str
    requested_output_paths: tuple[str, ...]
    reveal: RevealAnchor | None
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
    # Binds only on exact label==page-label equality; disjoint goal and page vocabularies bind nothing.
    # A content value-witness binder would be the non-lexical upgrade.
    matches = [path for path, labels in labels_by_path.items() if label in labels]
    return matches[0] if len(matches) == 1 else None


def _key_value_bindings(packet: dict[str, Any], labels_by_path: dict[str, tuple[str, ...]]) -> list[LiveReadBinding]:
    relations = packet.get("key_value_relations")
    if not isinstance(relations, list):
        return []
    # A requested-output label is minted case-folded while the page renders the tile heading
    # capitalised, so exact equality is tried first and a symmetric fold only rescues its misses.
    folded_labels_by_path = {
        path: tuple(entry.casefold() for entry in labels) for path, labels in labels_by_path.items()
    }
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
        output_path = _exact_path(label, labels_by_path) or _exact_path(label.casefold(), folded_labels_by_path)
        if output_path is None:
            continue
        label_selector = relation.get("label_selector")
        label_selector = label_selector if isinstance(label_selector, str) else ""
        label_child_index = _relation_label_child_index(relation)
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
                label_selector=label_selector,
                label_child_index=label_child_index,
                identified_by_label=label_child_index >= 0 or bool(label_selector),
            )
        )
    return bindings


def _singly_walked_key_value_bindings(
    packet: dict[str, Any], labels_by_path: dict[str, tuple[str, ...]], leaf_paths: set[str]
) -> list[LiveReadBinding]:
    """Binds a path whose label the extractor's walk saw exactly once.

    Truncation says there is more beyond the capture, which matters only because an unseen relation
    could share a captured label and make a bind ambiguous. A label the walk met once cannot be that
    among the relations it walked, so it binds from a truncated channel; everything else still waits
    for a complete one. Only a single-alias path qualifies: two aliases for one path each counted
    once would still produce two bindings.

    The count covers the light DOM the extractor walked, not the page: a duplicate inside a shadow
    root, a cross-origin frame, an unmounted virtualized row, or markup rendered after the capture
    is not in it, so this is uniqueness among what was seen rather than proof of uniqueness.

    Paths are counted one at a time, which is what counting per label needs and is also blind to the
    binder's own cross-path check, so two paths naming the same label would each bind that one
    relation and report one tile as two different outputs; a label more than one path claims binds
    for none of them.
    """
    claimed: dict[str, list[LiveReadBinding]] = {}
    for path in leaf_paths:
        labels = labels_by_path.get(path) or ()
        if len({label.casefold() for label in labels}) != 1:
            continue
        candidates = _key_value_bindings(packet, {path: labels})
        if len(candidates) != 1:
            continue
        relation_count = _walked_count_for_label(packet, candidates[0].relation_label)
        if relation_count != 1:
            continue
        claimed.setdefault(candidates[0].relation_label.casefold(), []).append(candidates[0])
    bindings = [found[0] for found in claimed.values() if len(found) == 1]
    return bindings


def _walked_count_for_label(packet: dict[str, Any], label: str) -> int:
    relations = packet.get("key_value_relations")
    if not isinstance(relations, list):
        return 0
    folded = label.casefold()
    for relation in relations:
        if isinstance(relation, dict) and str(relation.get("key_text") or "").casefold() == folded:
            count = relation.get("key_text_walked_count")
            return count if _is_int(count) else 0
    return 0


def _relation_label_child_index(relation: dict[str, Any]) -> int:
    """Which direct child carries the label, or -1 when the label is not the value's sibling.

    A tile whose figure is re-anchored onto its own row leaves the heading outside that row, so no
    child index names it and a read proving one would assert the figure against the heading text.
    """
    index = relation.get("label_child_index")
    if not _is_int(index):
        return 0
    return index if index >= -1 else 0


def _value_witness_bindings(
    packet: dict[str, Any], witnessed_by_path: dict[str, str], *, channel_intact: bool = True
) -> list[LiveReadBinding]:
    """Bind a path to the one relation still showing the value the scout read for it.

    This corroborates one quantity observed through two channels rather than matching a goal word
    against a page word, so a page whose vocabulary shares nothing with the request still binds.
    A value that appears in no relation, or in more than one, binds nothing.

    A truncated channel says there is more beyond the capture, so uniqueness among what was recorded
    is not uniqueness on the page. There the relation must also carry the walk's own page-wide count
    of its value, and that count must be one; a relation that never carried the count proves nothing
    and binds nothing. One relation answers at most one requested output, so two paths witnessing the
    same value describe one observation twice and neither binds.
    """
    relations = packet.get("key_value_relations")
    if not witnessed_by_path or not isinstance(relations, list):
        return []
    resolved: list[tuple[int, LiveReadBinding]] = []
    for output_path, witnessed in witnessed_by_path.items():
        matches = [
            relation
            for relation in relations
            if isinstance(relation, dict)
            and relation.get("visible") is True
            and relation.get("value_visible") is True
            and relation.get("value_truncated") is not True
            and isinstance(relation.get("value_text"), str)
            and relation["value_text"].strip() == witnessed
        ]
        if len(matches) != 1:
            continue
        relation = matches[0]
        if not channel_intact and relation.get("value_text_walked_count") != 1:
            continue
        selector = relation.get("container_selector")
        match_count = relation.get("container_match_count")
        position = relation.get("container_position")
        child_index = relation.get("value_child_index")
        child_count = relation.get("direct_child_count")
        if not isinstance(selector, str) or not selector:
            continue
        if not _is_int(match_count) or not _is_int(position) or not _is_int(child_index) or not _is_int(child_count):
            continue
        if match_count <= position or position < 0 or child_index < 0 or child_count <= child_index:
            continue
        resolved.append(
            (
                id(relation),
                LiveReadBinding(
                    output_path,
                    LiveReadKind.KEY_VALUE,
                    selector,
                    match_count,
                    position,
                    child_index,
                    child_count,
                    relation_label=str(relation.get("key_text") or ""),
                    label_selector=str(relation.get("label_selector") or ""),
                    label_child_index=_relation_label_child_index(relation),
                    identified_by_label=False,
                ),
            )
        )
    claimed_once = {
        relation_id
        for relation_id in {rid for rid, _ in resolved}
        if [rid for rid, _ in resolved].count(relation_id) == 1
    }
    return [binding for relation_id, binding in resolved if relation_id in claimed_once]


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


def array_parent_path(path: str) -> str | None:
    prefix = _array_prefix(path)
    if not prefix:
        return None
    return ".".join(name for name, _ in prefix)


_DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"),
)
_ZIP_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")
_STATE_TOKEN_PATTERN = re.compile(r"^[A-Z]{2}$")


def _is_numeric_id(text: str, digit_length: int | None) -> bool:
    if digit_length is None or digit_length <= 0:
        return False
    compact = text.replace(" ", "")
    return compact.isdigit() and len(compact) == digit_length


def _is_date(text: str) -> bool:
    stripped = text.strip()
    return any(pattern.match(stripped) for pattern in _DATE_PATTERNS)


def _is_postal_address(text: str) -> bool:
    tokens = text.split()
    if len(tokens) < 3 or not tokens[0][:1].isdigit():
        return False
    if sum(1 for token in tokens if token.isalpha()) < 2:
        return False
    return any(_ZIP_PATTERN.match(token) for token in tokens) or any(
        _STATE_TOKEN_PATTERN.match(token) for token in tokens
    )


def _is_categorical_token(text: str) -> bool:
    stripped = text.strip()
    if "," in stripped:
        return False
    tokens = stripped.split()
    return 1 <= len(tokens) <= 3 and all(token.isalpha() for token in tokens)


def value_matches_shape(text: str, expectation: ShapeExpectation) -> bool:
    if expectation.shape == ValueShape.NUMERIC_ID:
        return _is_numeric_id(text, expectation.id_digit_length)
    if expectation.shape == ValueShape.POSTAL_ADDRESS:
        return _is_postal_address(text)
    if expectation.shape == ValueShape.CATEGORICAL_TOKEN:
        return _is_categorical_token(text)
    if expectation.shape == ValueShape.DATE:
        return _is_date(text)
    return False


def _column_values_match_shape(values: list[str], expectation: ShapeExpectation) -> bool:
    if not values or not all(value_matches_shape(value, expectation) for value in values):
        return False
    if expectation.shape == ValueShape.CATEGORICAL_TOKEN:
        return len(set(values)) < len(values)
    return True


def resolve_shape_expectations_by_path(
    paths: set[str], registry: dict[str, ShapeExpectation] | None
) -> dict[str, ShapeExpectation]:
    if not registry:
        return {}
    resolved: dict[str, ShapeExpectation] = {}
    for path in _leaf_paths(paths):
        segments = output_path_segments(path)
        if not segments:
            continue
        expectation = registry.get(segments[-1][0])
        if expectation is None:
            continue
        is_column = bool(_array_prefix(path))
        if is_column != (expectation.cardinality == ValueCardinality.COLUMN):
            continue
        resolved[path] = expectation
    return resolved


def _key_value_shape_bindings(
    packet: dict[str, Any], shape_expectations_by_path: dict[str, ShapeExpectation]
) -> list[LiveReadBinding]:
    scalar_paths = {
        path: expectation
        for path, expectation in shape_expectations_by_path.items()
        if expectation.cardinality == ValueCardinality.SCALAR
    }
    relations = packet.get("key_value_relations")
    if not scalar_paths or not isinstance(relations, list):
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
        value_text = relation.get("value_text")
        if not isinstance(label, str) or not isinstance(selector, str):
            continue
        if not _is_int(match_count) or not _is_int(position) or not _is_int(child_index) or not _is_int(child_count):
            continue
        if match_count <= position or position < 0 or child_index < 0 or child_count <= child_index:
            continue
        if not isinstance(value_text, str) or not value_text.strip():
            continue
        for path, expectation in scalar_paths.items():
            if value_matches_shape(value_text, expectation):
                bindings.append(
                    LiveReadBinding(
                        path,
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


def _table_shape_bindings(
    packet: dict[str, Any], shape_expectations_by_path: dict[str, ShapeExpectation]
) -> list[LiveReadBinding]:
    column_paths = {
        path: expectation
        for path, expectation in shape_expectations_by_path.items()
        if expectation.cardinality == ValueCardinality.COLUMN
    }
    containers = packet.get("result_containers")
    if not column_paths or not isinstance(containers, list):
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
        header_label_list = [
            header["text"] for header in headers if isinstance(header, dict) and isinstance(header.get("text"), str)
        ]
        header_labels = tuple(header_label_list)
        if len(header_labels) != len(headers):
            continue
        row_cell_counts: list[int] = []
        column_texts: dict[int, list[str]] = {}
        valid = True
        for row_index, row in enumerate(rows):
            if (
                not isinstance(row, dict)
                or row.get("row_index") != row_index
                or row.get("visible") is not True
                or row.get("has_row_header") is not False
            ):
                valid = False
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
                valid = False
                break
            row_cell_counts.append(len(cells))
            for column_index, cell in enumerate(cells):
                cell_text = cell.get("text")
                column_texts.setdefault(column_index, []).append(cell_text if isinstance(cell_text, str) else "")
        if not valid or len(row_cell_counts) != row_count:
            continue
        for header in headers:
            header_column_index = header.get("column_index") if isinstance(header, dict) else None
            if (
                not isinstance(header, dict)
                or not isinstance(header.get("text"), str)
                or not _is_int(header_column_index)
            ):
                continue
            values = column_texts.get(header_column_index, [])
            for path, expectation in column_paths.items():
                if _column_values_match_shape(values, expectation):
                    bindings.append(
                        LiveReadBinding(
                            path,
                            LiveReadKind.TABLE_COLUMN,
                            selector,
                            match_count,
                            0,
                            row_selector=row_selector,
                            row_count=row_count,
                            column_index=header_column_index,
                            relation_label=header["text"],
                            headers=header_labels,
                            row_cell_counts=tuple(row_cell_counts),
                            row_identities=tuple(sample_rows),
                        )
                    )
    return bindings


# Content a click revealed is unreproducible without replaying that click, so an interaction-reached
# packet still owes its anchor. Content already rendered on the page owes nothing: requiring an anchor
# there leaves the plan underived on every deep-linked dashboard (SKY-13226). `navigate` is the stamp for
# an inspection that went to the requested URL itself, which is exactly the deep-link case; `post_run` is
# excluded because it observes the state a run left behind rather than the page as authored.
_BINDABLE_REACHED_VIA: frozenset[str] = frozenset({"interaction", "current_page", "navigate"})
_PAGE_EVIDENCE_TOOLS: frozenset[str] = frozenset({"inspect_page_for_composition", "evaluate"})
_BINDABLE_EVIDENCE_TOOLS: dict[str, frozenset[str]] = {
    "interaction": frozenset({"scout_interaction"}),
    "current_page": _PAGE_EVIDENCE_TOOLS,
    "navigate": _PAGE_EVIDENCE_TOOLS,
}


def _reveal_anchor_from_packet(packet: dict[str, Any]) -> RevealAnchor | None:
    selector, role, name = (
        packet.get("interaction_selector"),
        packet.get("interaction_role"),
        packet.get("interaction_accessible_name"),
    )
    if isinstance(selector, str) and selector:
        return RevealAnchor(selector=selector)
    if isinstance(role, str) and role and isinstance(name, str) and name:
        return RevealAnchor(role=role, name=name)
    return None


def _entry_is_bindable(entry: object) -> bool:
    """Whether an entry is one derivation can spend its single attempt on.

    The binder needs a bounded schema, so an entry without one carries nothing to bind and choosing
    it only spends the attempt; selecting on `reached_via` alone let that happen. An observation whose
    every relation belongs to a dialog in front of the page is the same kind of non-packet: it is
    freshest, so it wins the single attempt and shadows the tile captured before the dialog opened.
    """
    return _entry_observed_the_page(entry) and entry.get("had_bounded_schema") is True


def _entry_observed_the_page(entry: object) -> TypeGuard[dict[str, Any]]:
    """Whether the entry is an observation of the page itself, so a walk may select it at all.

    Three walks each pick "the freshest bindable packet", so a dialog-only capture excluded from one
    of them is still chosen by the other two; they share this test rather than repeating its terms.
    """
    return (
        isinstance(entry, dict)
        and entry.get("reached_via") in _BINDABLE_REACHED_VIA
        and entry.get("obstructed") is not True
    )


def _intact_binding_channels(packet: dict[str, Any]) -> tuple[bool, bool]:
    """Whether the key/value and result-container channels each survived the capture caps.

    A capture that hit one cap says nothing about the other, and a key/value bind reads only
    relations while a table bind reads only containers, so vetoing the whole packet withholds a
    binding whose own evidence is complete. Truncation still voids the channel it happened in,
    where an unseen duplicate could have made the bind ambiguous.
    """
    return (
        packet.get("key_value_relations_truncated") is False,
        packet.get("result_containers_truncated") is False,
    )


def _channel_bindings(
    packet: dict[str, Any],
    labels_by_path: dict[str, tuple[str, ...]],
    *,
    key_values_intact: bool,
    containers_intact: bool,
) -> list[LiveReadBinding]:
    bindings: list[LiveReadBinding] = []
    if key_values_intact:
        bindings += _key_value_bindings(packet, labels_by_path)
    if containers_intact:
        bindings += _table_bindings(packet, labels_by_path)
    return bindings


def _plan_from_entry(
    entry: dict[str, Any],
    *,
    labels_by_path: dict[str, tuple[str, ...]],
    witnessed_by_path: dict[str, str] | None = None,
    requested_paths: set[str] | None = None,
) -> RequestedOutputExtractionPlan | None:
    reached_via = entry.get("reached_via")
    if reached_via not in _BINDABLE_REACHED_VIA or entry.get("had_bounded_schema") is not True:
        return None
    step, packet = entry.get("step"), entry.get("evidence")
    if not _is_int(step) or not isinstance(packet, dict):
        return None
    if packet.get("source_tool") not in _BINDABLE_EVIDENCE_TOOLS[reached_via]:
        return None
    key_values_intact, containers_intact = _intact_binding_channels(packet)
    if isinstance(packet.get("inspection_warnings"), list) and packet["inspection_warnings"]:
        return None
    reveal = _reveal_anchor_from_packet(packet)
    if reveal is None and reached_via == "interaction":
        return None
    requested_scope = set(requested_paths) if requested_paths else set(labels_by_path)
    leaf_paths = _leaf_paths(requested_scope)
    live_reads = _channel_bindings(
        packet,
        labels_by_path,
        key_values_intact=key_values_intact,
        containers_intact=containers_intact,
    )
    if not key_values_intact:
        # The channel stays truncated: a label counted once page-wide binds, and nothing else does.
        live_reads = live_reads + _singly_walked_key_value_bindings(packet, labels_by_path, leaf_paths)
    # The witness joins on the value, and refuses a value the page shows more than once, so it carries
    # its own ambiguity proof rather than borrowing the channel's. Requiring an intact channel switched
    # it off for every dashboard rich enough to trip the relation cap.
    if witnessed_by_path:
        label_bound = {binding.output_path for binding in live_reads}
        live_reads = live_reads + _value_witness_bindings(
            packet,
            {
                path: value
                for path, value in witnessed_by_path.items()
                if path in leaf_paths and path not in label_bound
            },
            channel_intact=key_values_intact,
        )
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
    # The scope the plan was derived for, not the labels that happened to bind it: a witness binds
    # without a label, and keying either field on labels leaves the plan claiming no requested path.
    requested = tuple(sorted(requested_scope))
    identity = _stable_identity(repr((requested, observation_identity)))
    return RequestedOutputExtractionPlan(requested, step, observation_identity, reveal, ordered_reads, identity)


def value_designation_probe_expression(value_text: str, label: str) -> str:
    """In-browser probe resolving a value the model read off the page into a pinned element.

    The model designates what it can see — the rendered value, and the label it sits under — and the
    page resolves that to a selector. Asking the model for markup instead made it author selectors it
    had no way to verify (SKY-13226).
    """
    target = json.dumps(value_text)
    anchor = json.dumps(label)
    return (
        "(() => { const target = " + target + "; const label = " + anchor + ";"
        " const visible = (el) => { const r = el.getBoundingClientRect();"
        " return r.width > 0 && r.height > 0; };"
        " let matches = Array.from(document.querySelectorAll('body *')).filter((el) => {"
        " if (!visible(el)) return false;"
        " if ((el.innerText || '').trim() !== target) return false;"
        " return !Array.from(el.children).some((c) => (c.innerText || '').trim() === target); });"
        " if (!matches.length) return { error: 'text-not-found' };"
        " if (matches.length > 1 && label) { const scoped = matches.filter((el) => {"
        " let node = el.parentElement, hops = 0;"
        " while (node && hops < 4) { if ((node.innerText || '').includes(label)) return true;"
        " node = node.parentElement; hops++; } return false; });"
        " if (scoped.length) matches = scoped; }"
        " if (matches.length > 1) return { error: 'text-ambiguous', visible_count: matches.length,"
        " text: target, url: location.href };"
        " const chosen = matches[0];"
        # Identity before position: a positional path silently reads the wrong element once a sibling
        # is inserted between designation and the block running.
        " const unique = (sel) => { try { const found = document.querySelectorAll(sel);"
        " return found.length === 1 && found[0] === chosen; } catch (e) { return false; } };"
        " const tag = chosen.tagName.toLowerCase();"
        " const candidates = [];"
        " if (chosen.id) candidates.push('#' + CSS.escape(chosen.id));"
        " const classes = Array.from(chosen.classList || []).slice(0, 3)"
        ".map((c) => '.' + CSS.escape(c)).join('');"
        " if (classes) candidates.push(tag + classes);"
        " let selector = '';"
        " for (const candidate of candidates) { if (unique(candidate)) { selector = candidate; break; } }"
        " if (!selector) { const parts = []; let cur = chosen;"
        " while (cur && cur.nodeType === 1 && cur !== document.body) {"
        " const parent = cur.parentElement; if (!parent) break;"
        " const index = Array.prototype.indexOf.call(parent.children, cur) + 1;"
        " parts.unshift(cur.tagName.toLowerCase() + ':nth-child(' + index + ')');"
        " if (parent.id) { parts.unshift('#' + CSS.escape(parent.id));"
        " cur = null; break; } cur = parent; }"
        " selector = parts[0] && parts[0].charAt(0) === '#'"
        " ? parts.join(' > ') : 'body > ' + parts.join(' > '); }"
        " const all = Array.from(document.querySelectorAll(selector));"
        " const position = all.indexOf(chosen);"
        " if (position < 0) return { error: 'path-unstable', text: target, url: location.href };"
        " return { selector: selector, match_count: all.length, position: position, text: target,"
        " url: location.href }; })()"
    )


def plan_from_designations(
    designations: list[dict[str, Any]], requested_paths: set[str]
) -> RequestedOutputExtractionPlan | None:
    """Build the extraction plan from model-designated, probe-validated value elements.

    Designation replaces shape inference as the chooser: the page pins and compiles what the model
    saw, rather than the model authoring a path to an element it can only see rendered (SKY-13226).
    Independent of packet completeness — validation ran against the live page, not a truncatable
    relation list.
    """
    leaf_paths = _leaf_paths(requested_paths)
    if not leaf_paths:
        return None
    by_path: dict[str, LiveReadBinding] = {}
    for designation in designations:
        path = designation.get("output_path")
        selector = designation.get("selector")
        match_count = designation.get("match_count")
        position = designation.get("position")
        if not isinstance(path, str) or path not in leaf_paths or path in by_path:
            return None
        # A designation the page saw but could not pin to one element carries no selector. It still
        # names the value, which the witness channel binds; here it simply leaves the path uncovered.
        if not isinstance(selector, str) or not selector:
            continue
        if not _is_int(match_count) or not _is_int(position) or match_count <= position or position < 0:
            return None
        by_path[path] = LiveReadBinding(path, LiveReadKind.KEY_VALUE, selector, match_count, position)
    if set(by_path) != leaf_paths:
        return None
    ordered_reads = tuple(by_path[path] for path in sorted(leaf_paths))
    observation_identity = _stable_identity(repr(("designated", ordered_reads)))
    requested = tuple(sorted(requested_paths))
    identity = _stable_identity(repr((requested, observation_identity)))
    return RequestedOutputExtractionPlan(requested, 0, observation_identity, None, ordered_reads, identity)


def derivation_bail_reason(
    *,
    flow_evidence: list[dict[str, Any]],
    labels_by_path: dict[str, tuple[str, ...]],
    witnessed_by_path: dict[str, str] | None = None,
    requested_paths: set[str] | None = None,
) -> str:
    """Which guard stopped derivation, for the unavailable-plan log.

    Ten live runs produced `derived=0` with the tile visibly on screen, and each cause took a
    ~15-minute run plus a guess to distinguish; the bail point makes the next run name it. A missing
    label stopped being terminal once a witness could bind without one, so the label channel and the
    witness channel report separately rather than both as "no-labels".
    """
    scope = set(requested_paths) if requested_paths else set(labels_by_path)
    if not scope:
        return "no-authoritative-paths"
    # Selected the way derivation selects, or the reason names an entry derivation never looked at.
    entry = next((candidate for candidate in reversed(flow_evidence) if _entry_is_bindable(candidate)), None)
    if entry is None:
        return "no-bindable-entry"
    reached_via = str(entry.get("reached_via"))
    if entry.get("had_bounded_schema") is not True:
        return f"entry-unbounded-schema[{reached_via}]"
    packet = entry.get("evidence")
    if not _is_int(entry.get("step")) or not isinstance(packet, dict):
        return f"entry-malformed[{reached_via}]"
    source_tool = str(packet.get("source_tool"))
    if source_tool not in _BINDABLE_EVIDENCE_TOOLS[reached_via]:
        return f"packet-source-tool[{reached_via}:{source_tool}]"
    key_values_intact, containers_intact = _intact_binding_channels(packet)
    voided_channels = [
        name
        for name, intact in (
            ("key_value_relations", key_values_intact),
            ("result_containers", containers_intact),
        )
        if not intact
    ]
    # Truncation stopped being terminal once a witness could bind through it, so it is reported as
    # context on the per-path detail below rather than as the answer.
    if isinstance(packet.get("inspection_warnings"), list) and packet["inspection_warnings"]:
        return f"packet-inspection-warnings[{reached_via}]"
    if reached_via == "interaction" and _reveal_anchor_from_packet(packet) is None:
        return f"missing-reveal-anchor[{source_tool}]"
    leaf_paths = _leaf_paths(scope)
    counts = {path: 0 for path in leaf_paths}
    for binding in _channel_bindings(
        packet,
        labels_by_path,
        key_values_intact=key_values_intact,
        containers_intact=containers_intact,
    ):
        if binding.output_path in counts:
            counts[binding.output_path] += 1
    for binding in _value_witness_bindings(packet, dict(witnessed_by_path or {}), channel_intact=key_values_intact):
        if binding.output_path in counts:
            counts[binding.output_path] += 1
    unbound = sorted(path for path, count in counts.items() if count == 0)
    ambiguous = sorted(f"{path}(n={count})" for path, count in counts.items() if count > 1)
    if unbound:
        # An unbound path names why its witness channel could not answer, because a vocabulary miss,
        # an undeclared read and an unproved count want three different fixes.
        witnessed = dict(witnessed_by_path or {})
        relations = [rel for rel in (packet.get("key_value_relations") or []) if isinstance(rel, dict)]
        detail = []
        for path in unbound:
            value = witnessed.get(path)
            if not value:
                detail.append(f"{path}:witness-not-declared")
                continue
            matches = [rel for rel in relations if str(rel.get("value_text") or "").strip() == value]
            if not matches:
                detail.append(f"{path}:witness-not-present")
            elif len(matches) > 1:
                detail.append(f"{path}:witness-ambiguous[n={len(matches)}]")
            elif not key_values_intact and matches[0].get("value_text_walked_count") != 1:
                detail.append(f"{path}:witness-uniqueness-unproved-after-cap")
            else:
                detail.append(f"{path}:requested-path-unbound")
        truncated = f" truncated={voided_channels}" if voided_channels else ""
        return f"bindings[{' '.join(detail)}{(' ambiguous=' + str(ambiguous)) if ambiguous else ''}{truncated}]"
    if ambiguous:
        truncated = f" truncated={voided_channels}" if voided_channels else ""
        return f"bindings[ambiguous={ambiguous}{truncated}]"
    return "table-consistency-or-derived"


def bindable_candidate_headings(flow_evidence: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    """Key texts of the freshest bindable packet, for the unavailable-plan log.

    The binder compares minted labels against these; without both sides in the log, a live no-bind
    cannot say whether the label or the page vocabulary missed.
    """
    for entry in reversed(flow_evidence):
        if not _entry_observed_the_page(entry):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            return []
        relations = packet.get("key_value_relations")
        if not isinstance(relations, list):
            return []
        return [
            str(relation.get("key_text"))
            for relation in relations[:limit]
            if isinstance(relation, dict) and relation.get("key_text")
        ]
    return []


def unbound_candidate_relations(flow_evidence: list[dict[str, Any]], *, limit: int = 8) -> list[tuple[str, str]]:
    """Label/value pairs the freshest bindable packet is offering.

    The binder joins a minted label to a page label, so a page that names the requested quantity in
    its own words binds nothing however plainly it displays it. Handing the loop what the page does
    offer lets it read one of these into the requested path, after which the value witness binds the
    relation still showing that exact value — a join on the quantity rather than on the wording.
    """
    for entry in reversed(flow_evidence):
        if not _entry_observed_the_page(entry):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            return []
        relations = packet.get("key_value_relations")
        if not isinstance(relations, list):
            return []
        # A dialog beside readable content keeps the packet composable, but its own dismiss controls
        # are not quantities the page is showing; offering them puts a button next to the number as
        # if they were alternatives for the same requested output.
        dismiss_texts = set(entry.get("dismiss_texts") or ())
        offered: list[tuple[str, str]] = []
        for relation in relations:
            if not isinstance(relation, dict) or relation.get("visible") is not True:
                continue
            if relation.get("value_visible") is not True or relation.get("value_truncated") is True:
                continue
            label = str(relation.get("key_text") or "").strip()
            value = str(relation.get("value_text") or "").strip()
            if label in dismiss_texts or value in dismiss_texts:
                continue
            if label and value and (label, value) not in offered:
                offered.append((label, value))
            if len(offered) >= limit:
                break
        return offered
    return []


def value_shown_in_selectable_evidence(flow_evidence: list[dict[str, Any]], value: str) -> bool:
    """Whether an observation selection may consider still shows this exact value."""
    needle = value.strip()
    if not needle:
        return False
    for entry in reversed(flow_evidence):
        if not _entry_is_bindable(entry):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            continue
        for relation in packet.get("key_value_relations") or []:
            if isinstance(relation, dict) and str(relation.get("value_text") or "").strip() == needle:
                return True
    return False


def _witnessed_packet_entry(
    flow_evidence: list[dict[str, Any]], witnessed_by_path: dict[str, str]
) -> dict[str, Any] | None:
    """Newest selectable entry whose packet still shows one of the witnessed values."""
    witnessed = {value.strip() for value in witnessed_by_path.values() if value and value.strip()}
    if not witnessed:
        return None
    for entry in reversed(flow_evidence):
        if not _entry_is_bindable(entry):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            continue
        for relation in packet.get("key_value_relations") or []:
            if isinstance(relation, dict) and str(relation.get("value_text") or "").strip() in witnessed:
                return entry
    return None


def derive_requested_output_extraction_plan(
    *,
    flow_evidence: list[dict[str, Any]],
    labels_by_path: dict[str, tuple[str, ...]],
    witnessed_by_path: dict[str, str] | None = None,
    requested_paths: set[str] | None = None,
) -> RequestedOutputExtractionPlan | None:
    """Derive from one rollback-owned packet; never combine partial observations.

    The requested paths define what the plan owes; labels are one channel for meeting it. Scoping the
    plan by labels instead made a label-free witness depend on a label, so a page whose wording the
    request never uses had no route at all (SKY-13226).
    """
    scope = set(requested_paths) if requested_paths else set(labels_by_path)
    if not scope:
        return None
    # Freshest bindable packet wins, whether it was reached by a click or was already on the page: a
    # login-gated dashboard stamps its post-login captures `current_page`, so scanning for `interaction`
    # alone walks back past the metric tiles into the login form and derives nothing. Falling back to a
    # staler packet on failure is the partial-observation combining this function refuses; the one other
    # entry ever tried is the packet still showing a witnessed value, because a live counter ticks
    # between the read and the freshest capture, and the packet showing what the read saw is that
    # read's own contemporaneous observation, not a stale substitute. Either way one packet decides.
    for entry in reversed(flow_evidence):
        if _entry_is_bindable(entry):
            plan = _plan_from_entry(
                entry, labels_by_path=labels_by_path, witnessed_by_path=witnessed_by_path, requested_paths=scope
            )
            if plan is not None or not witnessed_by_path:
                return plan
            witness_entry = _witnessed_packet_entry(flow_evidence, witnessed_by_path)
            if witness_entry is not None and witness_entry is not entry:
                return _plan_from_entry(
                    witness_entry,
                    labels_by_path=labels_by_path,
                    witnessed_by_path=witnessed_by_path,
                    requested_paths=scope,
                )
            return None
    return None
