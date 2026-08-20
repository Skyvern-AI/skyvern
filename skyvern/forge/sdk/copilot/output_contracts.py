from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml
from skyvern.forge.sdk.workflow.models.parameter import is_sensitive_workflow_parameter
from skyvern.schemas.workflows import BlockType

_ORDERED_CHILD_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_ORDERED_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")


@dataclass(frozen=True)
class CodeBlockOutputContract:
    label: str
    code: str
    parameter_keys: tuple[str, ...]
    declared_workflow_parameter_keys: tuple[str, ...]
    available_binding_keys: tuple[str, ...]
    available_output_keys: tuple[str, ...]


def _enum_or_string_name(value: object) -> str:
    name = getattr(value, "value", value)
    return str(name or "")


def _block_output_key(block: Mapping[str, Any]) -> str | None:
    label = str(block.get("label") or "").strip()
    return f"{label}_output" if label else None


def _is_credential_parameter(parameter: Mapping[str, Any]) -> bool:
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    return parameter_type == "credential" or (
        parameter_type == "workflow" and workflow_parameter_type == "credential_id"
    )


def declared_string_workflow_parameter_keys(parsed: Mapping[str, Any]) -> set[str]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, Mapping):
        return set()
    parameters = workflow_definition.get("parameters")
    if not isinstance(parameters, list):
        return set()
    keys: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            continue
        key = str(parameter.get("key") or "").strip()
        if not key or _is_credential_parameter(parameter) or is_sensitive_workflow_parameter(dict(parameter)):
            continue
        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type and parameter_type != "workflow":
            continue
        if workflow_parameter_type and workflow_parameter_type != "string":
            continue
        keys.add(key)
    return keys


def declared_workflow_parameter_keys(parsed: Mapping[str, Any]) -> set[str]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, Mapping):
        return set()
    parameters = workflow_definition.get("parameters")
    if not isinstance(parameters, list):
        return set()
    return {
        key
        for parameter in parameters
        if isinstance(parameter, Mapping)
        for key in [str(parameter.get("key") or "").strip()]
        if key
    }


def code_block_parameter_keys(block: Mapping[str, Any]) -> frozenset[str]:
    raw_keys = block.get("parameter_keys")
    keys = {key for key in raw_keys if isinstance(key, str) and key} if isinstance(raw_keys, list) else set()
    raw_parameters = block.get("parameters")
    if isinstance(raw_parameters, list):
        keys.update(
            str(parameter.get("key") or "").strip()
            for parameter in raw_parameters
            if isinstance(parameter, Mapping) and str(parameter.get("key") or "").strip()
        )
    return frozenset(keys)


def code_block_available_contracts_by_label(workflow_yaml: str | None) -> dict[str, CodeBlockOutputContract]:
    if workflow_yaml is None:
        return {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return {}
    contracts: dict[str, CodeBlockOutputContract] = {}
    declared_parameter_keys = tuple(sorted(declared_workflow_parameter_keys(parsed)))

    def visit_branch(
        branch: Mapping[str, Any], available_binding_keys: set[str], available_output_keys: set[str]
    ) -> None:
        for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
            visit_blocks(branch.get(key), set(available_binding_keys), set(available_output_keys))
        for branch_key in _ORDERED_BRANCH_LIST_KEYS:
            branches = branch.get(branch_key)
            if not isinstance(branches, list):
                continue
            for nested_branch in branches:
                if isinstance(nested_branch, Mapping):
                    visit_branch(nested_branch, set(available_binding_keys), set(available_output_keys))

    def visit_blocks(blocks: Any, available_binding_keys: set[str], available_output_keys: set[str]) -> set[str]:
        if not isinstance(blocks, list):
            return available_binding_keys
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            label = str(block.get("label") or "").strip()
            if label and _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
                contracts[label] = CodeBlockOutputContract(
                    label=label,
                    code=str(block.get("code") or ""),
                    parameter_keys=tuple(sorted(code_block_parameter_keys(block))),
                    declared_workflow_parameter_keys=declared_parameter_keys,
                    available_binding_keys=tuple(sorted(available_binding_keys)),
                    available_output_keys=tuple(sorted(available_output_keys)),
                )
            for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
                visit_blocks(block.get(key), set(available_binding_keys), set(available_output_keys))
            for branch_key in _ORDERED_BRANCH_LIST_KEYS:
                branches = block.get(branch_key)
                if not isinstance(branches, list):
                    continue
                for branch in branches:
                    if isinstance(branch, Mapping):
                        visit_branch(branch, set(available_binding_keys), set(available_output_keys))
            output_key = _block_output_key(block)
            if output_key:
                available_binding_keys.add(output_key)
                available_output_keys.add(output_key)
        return available_binding_keys

    workflow_definition = parsed.get("workflow_definition")
    blocks = workflow_definition.get("blocks") if isinstance(workflow_definition, Mapping) else None
    visit_blocks(blocks, declared_string_workflow_parameter_keys(parsed), set())
    return contracts


def code_block_available_binding_keys_by_label(workflow_yaml: str | None) -> dict[str, list[str]]:
    return {
        label: list(contract.available_binding_keys)
        for label, contract in code_block_available_contracts_by_label(workflow_yaml).items()
    }
