from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuthoringParameterBindingMatchBasis = Literal[
    "exact_authored_selector",
    "grounded_input_correspondence",
    "unique_ephemeral_value",
]
AuthoringParameterBindingTerminalTool = Literal["click", "press_key"]


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
    authored = authored_selector_parameter_bindings(code, declared_keys)
    if authored is None:
        return False
    return all(
        binding.declared_key in authored.get(binding.field_selector, set()) for binding in snapshot.field_bindings
    )
