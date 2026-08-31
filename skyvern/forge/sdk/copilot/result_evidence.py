"""Bounded browser-observation facts used by Copilot verification."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.copilot.composition_evidence_size import size_compaction_omits
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    LiveReadBinding,
    LiveReadKind,
    ShapeExpectation,
    _key_value_bindings,
    _key_value_shape_bindings,
    _table_bindings,
    _table_shape_bindings,
    array_parent_path,
)

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
_SCOUT_OBSERVATION_SOURCE_TOOL = "evaluate"
_SCOUT_INTERACTION_SOURCE_TOOL = "scout_interaction"
_SCOUT_WITNESS_KEY_VALUE = "capture_nonempty_value"
_SCOUT_WITNESS_TABLE = "cell_text_present"
_SCOUT_VALUE_WITNESSES = frozenset({_SCOUT_WITNESS_KEY_VALUE, _SCOUT_WITNESS_TABLE})
_SHAPE_CHANNEL = "shape"
_LEXICAL_CHANNEL = "lexical"


@dataclass(frozen=True)
class ScoutObservedBinding:
    output_path: str
    kind: str
    selector: str
    selector_index: int
    matched_label: str
    value_witness: str


@dataclass(frozen=True)
class ScoutObservationContract:
    bindings: tuple[ScoutObservedBinding, ...]
    source_tool: str
    source_url: str
    has_bounded_page_schema: bool
    structure_signature: str
    workflow_run_id: str | None = None
    observed_after_workflow_run: bool = False


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


def _sample_row_text(value: object) -> str:
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return str(value)
    return str(value)


def _structure_signature(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _scout_binding_signature_payload(binding: ScoutObservedBinding) -> dict[str, object]:
    return {
        "output_path": binding.output_path,
        "kind": binding.kind,
        "selector": binding.selector,
        "selector_index": binding.selector_index,
        "matched_label": binding.matched_label,
        "value_witness": binding.value_witness,
    }


def _scout_observation_structure_signature(
    bindings: tuple[ScoutObservedBinding, ...],
    *,
    source_url: str,
    workflow_run_id: str | None,
    observed_after_workflow_run: bool,
) -> str:
    return _structure_signature(
        {
            "bindings": [_scout_binding_signature_payload(binding) for binding in bindings],
            "source_tool": _SCOUT_OBSERVATION_SOURCE_TOOL,
            "source_url": source_url,
            "workflow_run_id": workflow_run_id,
            "observed_after_workflow_run": observed_after_workflow_run,
        }
    )


def scout_observation_contract_valid(contract: ScoutObservationContract | None) -> bool:
    if contract is None or contract.source_tool != _SCOUT_OBSERVATION_SOURCE_TOOL or not contract.source_url.strip():
        return False
    if not contract.bindings:
        return False
    for binding in contract.bindings:
        if not binding.output_path.startswith("output."):
            return False
        if binding.kind not in (LiveReadKind.KEY_VALUE, LiveReadKind.TABLE_COLUMN):
            return False
        if binding.value_witness not in _SCOUT_VALUE_WITNESSES:
            return False
    expected = _scout_observation_structure_signature(
        contract.bindings,
        source_url=contract.source_url,
        workflow_run_id=contract.workflow_run_id,
        observed_after_workflow_run=contract.observed_after_workflow_run,
    )
    return contract.structure_signature == expected


def scout_observation_bound_paths(contract: ScoutObservationContract | None) -> set[str]:
    if not scout_observation_contract_valid(contract):
        return set()
    assert contract is not None
    paths = {binding.output_path for binding in contract.bindings}
    parents = {parent for path in paths if (parent := array_parent_path(path)) is not None}
    return paths | parents


def _table_binding_value_witnessed(packet: Mapping[str, Any], binding: LiveReadBinding) -> bool:
    containers = packet.get("result_containers")
    if not isinstance(containers, list):
        return False
    for container in containers:
        if not isinstance(container, Mapping) or container.get("selector") != binding.selector:
            continue
        if not _result_container_has_content(container):
            return False
        rows = container.get("rows")
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            cells = row.get("cells")
            if not isinstance(cells, list) or binding.column_index >= len(cells):
                continue
            cell = cells[binding.column_index]
            if isinstance(cell, Mapping) and cell.get("has_text") is True:
                return True
        return False
    return False


def _binding_carrier(binding: LiveReadBinding) -> tuple[str, str, int, int]:
    return (str(binding.kind), binding.selector, binding.selector_index, binding.column_index)


def _kv_relation_for_binding(packet: Mapping[str, Any], binding: LiveReadBinding) -> Mapping[str, Any] | None:
    relations = packet.get("key_value_relations")
    if not isinstance(relations, list):
        return None
    for relation in relations:
        if (
            isinstance(relation, Mapping)
            and relation.get("container_selector") == binding.selector
            and relation.get("container_position") == binding.selector_index
            and relation.get("key_text") == binding.relation_label
        ):
            return relation
    return None


def _kv_binding_value_text_dropped(packet: Mapping[str, Any], binding: LiveReadBinding) -> bool:
    relation = _kv_relation_for_binding(packet, binding)
    if relation is None or "value_text" not in relation:
        return False
    value_text = relation.get("value_text")
    return not isinstance(value_text, str) or not value_text.strip()


def mint_scout_observation_contract(
    page_evidence: Mapping[str, Any],
    *,
    labels_by_path: dict[str, tuple[str, ...]],
    url: str,
    has_bounded_page_schema: bool,
    workflow_run_id: str | None = None,
    observed_after_workflow_run: bool = False,
    shape_expectations_by_path: dict[str, ShapeExpectation] | None = None,
) -> ScoutObservationContract | None:
    """Bind requested output paths from one page capture via two additive channels: a lexical label-match
    channel and a non-lexical value-shape channel that activates on any capture carrying binder-consumable
    witnessed value content (bounded schema or a witnessed value), regardless of interaction ordinal.
    Lexical candidates take precedence per path, surviving shape bindings require a >=2 distinct-path
    quorum, and truncated or warned captures void the mint."""
    if not isinstance(page_evidence, Mapping) or not url.strip() or not labels_by_path:
        return None
    packet = dict(page_evidence)
    if size_compaction_omits(
        packet,
        {
            "key_value_relations",
            "result_containers",
            "result_containers.rows",
            "result_containers.sample_rows",
        },
    ):
        return None
    if page_evidence.get("result_containers_truncated") is not False:
        return None
    if page_evidence.get("key_value_relations_truncated") is not False:
        return None
    warnings = page_evidence.get("inspection_warnings")
    if isinstance(warnings, list) and warnings:
        return None
    lexical_by_path: dict[str, list[LiveReadBinding]] = {}
    for binding in _key_value_bindings(packet, labels_by_path):
        lexical_by_path.setdefault(binding.output_path, []).append(binding)
    for binding in _table_bindings(packet, labels_by_path):
        lexical_by_path.setdefault(binding.output_path, []).append(binding)

    # Local import: composition_evidence pulls in the output_utils -> context -> result_evidence
    # chain, so importing it at module scope would form a circular import.
    from skyvern.forge.sdk.copilot.composition_evidence import has_witnessed_value_content

    shape_by_path: dict[str, list[LiveReadBinding]] = {}
    if shape_expectations_by_path and (has_bounded_page_schema or has_witnessed_value_content(packet)):
        for binding in _key_value_shape_bindings(packet, shape_expectations_by_path):
            shape_by_path.setdefault(binding.output_path, []).append(binding)
        for binding in _table_shape_bindings(packet, shape_expectations_by_path):
            shape_by_path.setdefault(binding.output_path, []).append(binding)
        for path, shape_bindings in shape_by_path.items():
            lexical_carriers = {_binding_carrier(binding) for binding in lexical_by_path.get(path, [])}
            if lexical_carriers:
                shape_by_path[path] = [
                    binding for binding in shape_bindings if _binding_carrier(binding) not in lexical_carriers
                ]

    resolved: list[tuple[LiveReadBinding, str]] = []
    for output_path in sorted(set(lexical_by_path) | set(shape_by_path)):
        lexical = lexical_by_path.get(output_path, [])
        shape = shape_by_path.get(output_path, [])
        if len(lexical) == 1:
            resolved.append((lexical[0], _LEXICAL_CHANNEL))
        elif not lexical and len(shape) == 1:
            resolved.append((shape[0], _SHAPE_CHANNEL))

    gated: list[tuple[LiveReadBinding, str]] = []
    for binding, channel in resolved:
        if binding.kind == LiveReadKind.TABLE_COLUMN:
            if not _table_binding_value_witnessed(packet, binding):
                continue
        elif _kv_binding_value_text_dropped(packet, binding):
            continue
        gated.append((binding, channel))

    carrier_indices: dict[tuple[str, str, int, int], list[int]] = {}
    for index, (binding, _channel) in enumerate(gated):
        carrier_indices.setdefault(_binding_carrier(binding), []).append(index)
    dropped: set[int] = set()
    for indices in carrier_indices.values():
        if len(indices) < 2 or all(gated[index][1] != _SHAPE_CHANNEL for index in indices):
            continue
        if any(gated[index][1] == _LEXICAL_CHANNEL for index in indices):
            dropped.update(index for index in indices if gated[index][1] == _SHAPE_CHANNEL)
        else:
            dropped.update(indices)
    survivors = [entry for index, entry in enumerate(gated) if index not in dropped]

    shape_paths = {binding.output_path for binding, channel in survivors if channel == _SHAPE_CHANNEL}
    if len(shape_paths) < 2:
        survivors = [(binding, channel) for binding, channel in survivors if channel != _SHAPE_CHANNEL]

    bindings: list[ScoutObservedBinding] = []
    for binding, _channel in survivors:
        witness = _SCOUT_WITNESS_TABLE if binding.kind == LiveReadKind.TABLE_COLUMN else _SCOUT_WITNESS_KEY_VALUE
        bindings.append(
            ScoutObservedBinding(
                output_path=binding.output_path,
                kind=str(binding.kind),
                selector=binding.selector,
                selector_index=binding.selector_index,
                matched_label=binding.relation_label,
                value_witness=witness,
            )
        )
    if not bindings:
        return None
    frozen_bindings = tuple(bindings)
    return ScoutObservationContract(
        bindings=frozen_bindings,
        source_tool=_SCOUT_OBSERVATION_SOURCE_TOOL,
        source_url=url,
        has_bounded_page_schema=has_bounded_page_schema,
        structure_signature=_scout_observation_structure_signature(
            frozen_bindings,
            source_url=url,
            workflow_run_id=workflow_run_id,
            observed_after_workflow_run=observed_after_workflow_run,
        ),
        workflow_run_id=workflow_run_id,
        observed_after_workflow_run=observed_after_workflow_run,
    )
