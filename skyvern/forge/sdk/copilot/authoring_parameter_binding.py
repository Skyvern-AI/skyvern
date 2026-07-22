from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuthoringParameterBindingMatchBasis = Literal[
    "exact_authored_selector",
    "grounded_input_correspondence",
    "unique_ephemeral_value",
    "scouted_selection_value",
    "scouted_option_value",
]
AuthoringParameterBindingTerminalTool = Literal["click", "press_key", "select_option"]

_SELECTION_MATCH_BASES: frozenset[AuthoringParameterBindingMatchBasis] = frozenset(
    {"scouted_selection_value", "scouted_option_value"}
)

SameMonthFileMatchFormat = Literal["identity", "iso_date_to_year_month"]


@dataclass(frozen=True, slots=True)
class SameMonthFileMatchHole:
    declared_keys: tuple[str, ...]
    matched_literal: str
    position: int
    format_id: SameMonthFileMatchFormat
    source_values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SameMonthFileMatchTransform:
    selector: str
    holes: tuple[SameMonthFileMatchHole, ...]
    date_keys: tuple[str, str]
    expected_declared_keys: tuple[str, ...]
    provenance_fingerprint: str
    date_format_id: Literal["iso_date_to_year_month"] = "iso_date_to_year_month"


def same_month_file_match_transform_fingerprint(transform: SameMonthFileMatchTransform) -> str:
    payload = {
        "selector": transform.selector,
        "holes": [
            {
                "declared_keys": hole.declared_keys,
                "matched_literal": hole.matched_literal,
                "position": hole.position,
                "format_id": hole.format_id,
                "source_values": hole.source_values,
            }
            for hole in transform.holes
        ],
        "date_keys": transform.date_keys,
        "expected_declared_keys": transform.expected_declared_keys,
        "date_format_id": transform.date_format_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _iso_date_parts(value: str) -> tuple[int, int, int] | None:
    parts = value.split("-")
    if len(parts) != 3 or tuple(map(len, parts)) != (4, 2, 2) or any(not part.isdigit() for part in parts):
        return None
    year, month, day = (int(part) for part in parts)
    if year < 1 or month < 1 or month > 12:
        return None
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return (year, month, day) if 1 <= day <= days[month - 1] else None


def _quoted_content_spans(value: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    quote = ""
    start = -1
    index = 0
    while index < len(value):
        character = value[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                spans.append((start, index))
                quote = ""
        elif character in ("'", '"'):
            quote = character
            start = index + 1
        index += 1
    return tuple(spans)


def _quoted_boundary_positions(
    selector: str,
    literal: str,
    quoted_spans: Sequence[tuple[int, int]],
) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while literal:
        position = selector.find(literal, start)
        if position < 0:
            break
        end = position + len(literal)
        left_boundary = position == 0 or not selector[position - 1].isalnum()
        right_boundary = end == len(selector) or not selector[end].isalnum()
        inside_quote = any(span_start <= position and end <= span_end for span_start, span_end in quoted_spans)
        if left_boundary and right_boundary and inside_quote:
            positions.append(position)
        start = position + 1
    return tuple(positions)


def same_month_file_match_transform_is_valid(transform: SameMonthFileMatchTransform) -> bool:
    if (
        transform.provenance_fingerprint != same_month_file_match_transform_fingerprint(transform)
        or not transform.expected_declared_keys
        or transform.expected_declared_keys != tuple(sorted(set(transform.expected_declared_keys)))
        or len(set(transform.date_keys)) != 2
        or not set(transform.date_keys).issubset(transform.expected_declared_keys)
    ):
        return False
    quoted_spans = _quoted_content_spans(transform.selector)
    covered_keys: set[str] = set()
    cursor = 0
    date_holes = 0
    for hole in transform.holes:
        if (
            hole.position < cursor
            or not hole.matched_literal
            or not hole.declared_keys
            or any(not key or key in covered_keys for key in hole.declared_keys)
            or transform.selector[hole.position : hole.position + len(hole.matched_literal)] != hole.matched_literal
            or _quoted_boundary_positions(transform.selector, hole.matched_literal, quoted_spans) != (hole.position,)
        ):
            return False
        covered_keys.update(hole.declared_keys)
        cursor = hole.position + len(hole.matched_literal)
        if hole.format_id == "identity":
            if len(hole.declared_keys) != 1 or hole.source_values != (hole.matched_literal,):
                return False
            continue
        if hole.format_id != "iso_date_to_year_month" or hole.declared_keys != transform.date_keys:
            return False
        if len(hole.source_values) != 2:
            return False
        date_parts = tuple(_iso_date_parts(value) for value in hole.source_values)
        if any(parts is None for parts in date_parts):
            return False
        start_parts, end_parts = date_parts
        if start_parts is None or end_parts is None or start_parts[:2] != end_parts[:2]:
            return False
        if hole.matched_literal != f"{start_parts[0]:04d}-{start_parts[1]:02d}":
            return False
        date_holes += 1
    return date_holes == 1 and covered_keys == set(transform.expected_declared_keys)


def derive_same_month_file_match_transform(
    *,
    selector: str,
    parameter_values: Mapping[str, str],
    identity_correspondences: Sequence[Mapping[str, object]],
) -> SameMonthFileMatchTransform | None:
    if not selector or not parameter_values or any(not key or not value for key, value in parameter_values.items()):
        return None
    quoted_spans = _quoted_content_spans(selector)
    identity_holes: dict[str, SameMonthFileMatchHole] = {}
    duplicate_identity_keys: set[str] = set()
    for correspondence in identity_correspondences:
        key = correspondence.get("input_key")
        literal = correspondence.get("matched_literal")
        position = correspondence.get("position")
        if (
            not isinstance(key, str)
            or not isinstance(literal, str)
            or not isinstance(position, int)
            or correspondence.get("surface") != "selector"
            or correspondence.get("transform") != "identity"
            or correspondence.get("parameter_value") != parameter_values.get(key)
            or literal != parameter_values.get(key)
            or _quoted_boundary_positions(selector, literal, quoted_spans) != (position,)
        ):
            continue
        if key in identity_holes:
            duplicate_identity_keys.add(key)
        identity_holes[key] = SameMonthFileMatchHole((key,), literal, position, "identity", (literal,))
    if duplicate_identity_keys:
        return None

    parsed_dates = [
        (key, value, parts) for key, value in parameter_values.items() if (parts := _iso_date_parts(value)) is not None
    ]
    candidates: list[SameMonthFileMatchTransform] = []
    for left_index, (left_key, left_value, left_parts) in enumerate(parsed_dates):
        for right_key, right_value, right_parts in parsed_dates[left_index + 1 :]:
            if left_parts[:2] != right_parts[:2]:
                continue
            month_literal = f"{left_parts[0]:04d}-{left_parts[1]:02d}"
            month_positions = _quoted_boundary_positions(selector, month_literal, quoted_spans)
            if len(month_positions) != 1:
                continue
            date_records = sorted(
                ((left_key, left_value, left_parts), (right_key, right_value, right_parts)),
                key=lambda record: (record[2], record[0]),
            )
            date_keys = (date_records[0][0], date_records[1][0])
            if any(key in identity_holes for key in date_keys):
                continue
            holes = [hole for key, hole in identity_holes.items() if key not in date_keys]
            holes.append(
                SameMonthFileMatchHole(
                    declared_keys=date_keys,
                    matched_literal=month_literal,
                    position=month_positions[0],
                    format_id="iso_date_to_year_month",
                    source_values=(date_records[0][1], date_records[1][1]),
                )
            )
            holes.sort(key=lambda hole: hole.position)
            covered_keys = {key for hole in holes for key in hole.declared_keys}
            if any(left.position + len(left.matched_literal) > right.position for left, right in zip(holes, holes[1:])):
                continue
            candidate = SameMonthFileMatchTransform(
                selector=selector,
                holes=tuple(holes),
                date_keys=date_keys,
                expected_declared_keys=tuple(sorted(covered_keys)),
                provenance_fingerprint="",
            )
            candidates.append(
                SameMonthFileMatchTransform(
                    selector=candidate.selector,
                    holes=candidate.holes,
                    date_keys=candidate.date_keys,
                    expected_declared_keys=candidate.expected_declared_keys,
                    provenance_fingerprint=same_month_file_match_transform_fingerprint(candidate),
                )
            )
    unique = {candidate: candidate for candidate in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


class AuthoringParameterFieldBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_key: str
    field_selector: str
    field_trajectory_index: int | None = Field(default=None, ge=0)
    match_basis: AuthoringParameterBindingMatchBasis


class AuthoringParameterTerminalBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: AuthoringParameterBindingTerminalTool
    trajectory_index: int = Field(ge=0)
    selector: str = ""
    key: str = ""


class AuthoringParameterBindingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structural_key: str
    source_origin: str
    field_bindings: tuple[AuthoringParameterFieldBinding, ...]
    terminal: AuthoringParameterTerminalBinding
    fingerprint: str


class AuthoringParameterBindingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_key: str
    field_selector: str


class AuthoringParameterBindingDirective(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structural_key: str
    source_origin: str
    candidates: tuple[AuthoringParameterBindingCandidate, ...]
    fingerprint: str


def authoring_parameter_binding_fingerprint(
    *,
    structural_key: str,
    source_origin: str,
    field_bindings: Sequence[AuthoringParameterFieldBinding],
    terminal: AuthoringParameterTerminalBinding,
) -> str:
    payload = {
        "structural_key": structural_key,
        "source_origin": source_origin,
        "field_bindings": [
            {
                "declared_key": binding.declared_key,
                "field_selector": binding.field_selector,
                "field_trajectory_index": binding.field_trajectory_index,
                "match_basis": binding.match_basis,
            }
            for binding in field_bindings
        ],
        "terminal": terminal.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_authoring_parameter_binding_snapshot(
    *,
    structural_key: str,
    source_origin: str,
    field_bindings: Sequence[AuthoringParameterFieldBinding],
    terminal: AuthoringParameterTerminalBinding,
) -> AuthoringParameterBindingSnapshot:
    ordered = tuple(sorted(field_bindings, key=lambda binding: binding.declared_key))
    return AuthoringParameterBindingSnapshot(
        structural_key=structural_key,
        source_origin=source_origin,
        field_bindings=ordered,
        terminal=terminal,
        fingerprint=authoring_parameter_binding_fingerprint(
            structural_key=structural_key,
            source_origin=source_origin,
            field_bindings=ordered,
            terminal=terminal,
        ),
    )


def build_authoring_parameter_binding_directive(
    *,
    structural_key: str,
    source_origin: str,
    candidates: Sequence[AuthoringParameterBindingCandidate],
) -> AuthoringParameterBindingDirective:
    unique = {(candidate.declared_key, candidate.field_selector): candidate for candidate in candidates}
    ordered = tuple(unique[key] for key in sorted(unique))
    payload = {
        "structural_key": structural_key,
        "source_origin": source_origin,
        "candidates": [candidate.model_dump(mode="json") for candidate in ordered],
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return AuthoringParameterBindingDirective(
        structural_key=structural_key,
        source_origin=source_origin,
        candidates=ordered,
        fingerprint=fingerprint,
    )


def _literal_locator_selector(node: ast.AST) -> str:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "locator":
        return ""
    if not isinstance(node.func.value, ast.Name) or node.func.value.id != "page" or len(node.args) != 1:
        return ""
    argument = node.args[0]
    return argument.value if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else ""


def _fill_parameter_key(node: ast.Call) -> str:
    if not node.args:
        return ""
    argument = node.args[0]
    if isinstance(argument, ast.Name):
        return argument.id
    if (
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id == "str"
        and len(argument.args) == 1
        and isinstance(argument.args[0], ast.Name)
    ):
        return argument.args[0].id
    return ""


def authored_selector_parameter_bindings(code: str, declared_keys: set[str]) -> dict[str, set[str]] | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    bindings: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"fill", "type"}:
            continue
        selector = _literal_locator_selector(node.func.value)
        parameter_key = _fill_parameter_key(node)
        if selector and parameter_key in declared_keys:
            bindings.setdefault(selector, set()).add(parameter_key)
    return bindings


def _formatted_value_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call) and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
        return node.args[0].id
    return None


def _templated_locator_declared_key(receiver: ast.AST, declared_keys: set[str]) -> str | None:
    if not isinstance(receiver, ast.Call) or not isinstance(receiver.func, ast.Attribute):
        return None
    if not isinstance(receiver.func.value, ast.Name) or receiver.func.value.id != "page":
        return None
    joined: ast.AST | None = None
    if receiver.func.attr == "locator" and len(receiver.args) == 1:
        joined = receiver.args[0]
    elif receiver.func.attr == "get_by_role":
        for keyword_arg in receiver.keywords:
            if keyword_arg.arg == "name":
                joined = keyword_arg.value
    if not isinstance(joined, ast.JoinedStr):
        return None
    keys: set[str] = set()
    for value in joined.values:
        if not isinstance(value, ast.FormattedValue):
            continue
        name = _formatted_value_name(value.value)
        if name is None:
            return None
        if name in declared_keys:
            keys.add(name)
    if len(keys) != 1:
        return None
    return next(iter(keys))


def authored_selection_parameter_bindings(code: str, declared_keys: set[str]) -> dict[str, set[str]] | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    bindings: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "click":
            key = _templated_locator_declared_key(node.func.value, declared_keys)
            if key is None:
                continue
            bindings.setdefault(ast.unparse(node.func.value), set()).add(key)
        elif node.func.attr == "select_option":
            selector = _literal_locator_selector(node.func.value)
            parameter_key = _fill_parameter_key(node)
            if selector and parameter_key in declared_keys:
                bindings.setdefault(selector, set()).add(parameter_key)
    return bindings


def authoring_parameter_binding_directive_consumed(
    directive: AuthoringParameterBindingDirective,
    snapshot: AuthoringParameterBindingSnapshot,
    *,
    code: str,
    parameter_keys: Sequence[str],
) -> bool:
    if directive.structural_key != snapshot.structural_key or directive.source_origin != snapshot.source_origin:
        return False
    snapshot_pairs = {(binding.declared_key, binding.field_selector) for binding in snapshot.field_bindings}
    directive_pairs = {(candidate.declared_key, candidate.field_selector) for candidate in directive.candidates}
    if not directive_pairs or not snapshot_pairs.issubset(directive_pairs):
        return False
    if {candidate.declared_key for candidate in directive.candidates} != {
        binding.declared_key for binding in snapshot.field_bindings
    }:
        return False
    declared_keys = {binding.declared_key for binding in snapshot.field_bindings}
    if not declared_keys.issubset(parameter_keys):
        return False
    fill_authored = authored_selector_parameter_bindings(code, declared_keys)
    if fill_authored is None:
        return False
    selection_needed = any(binding.match_basis in _SELECTION_MATCH_BASES for binding in snapshot.field_bindings)
    selection_authored = authored_selection_parameter_bindings(code, declared_keys) if selection_needed else {}
    if selection_authored is None:
        return False
    return all(
        binding.declared_key
        in (selection_authored if binding.match_basis in _SELECTION_MATCH_BASES else fill_authored).get(
            binding.field_selector, set()
        )
        for binding in snapshot.field_bindings
    )
