from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml
from skyvern.forge.sdk.workflow.models.parameter import is_sensitive_workflow_parameter
from skyvern.schemas.workflows import BlockType

_ORDERED_CHILD_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_ORDERED_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")

OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE = "output_source_unobservable"
OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE = "actuation_exhausted"


class OutputContractBailFamily(StrEnum):
    STATIC_RETURN = "static_return"
    STRUCTURAL = "structural"


class OutputContractActuationKind(StrEnum):
    IMPOSED = "imposed"
    STRUCTURE_DIRECTIVE = "structure_directive"
    ADVISORY_RUN = "advisory_run"
    BLOCKED_TERMINAL = "blocked_terminal"


class OutputContractAdvisoryState(StrEnum):
    UNUSED = "unused"
    GRANTED = "granted"
    CONSUMED = "consumed"
    EXPIRED = "expired"


# A suffix whose sole defect is an un-keyable static return is STATIC_RETURN; every other split
# defect, and any unknown or mixed blocker set, is STRUCTURAL. The family is observability-only
# and no longer gates the advisory run, which is family-uniform and keyed on observable source.
_STATIC_RETURN_BLOCKERS = frozenset({"static_return_envelope_unavailable"})


def classify_output_contract_bail_family(blockers: Iterable[str]) -> OutputContractBailFamily:
    codes = {str(blocker).strip() for blocker in blockers if str(blocker).strip()}
    if codes and codes <= _STATIC_RETURN_BLOCKERS:
        return OutputContractBailFamily.STATIC_RETURN
    return OutputContractBailFamily.STRUCTURAL


@dataclass(frozen=True)
class OutputContractActuationEvidence:
    imposed_available: bool
    click_only_spine: bool
    observed_required_values: bool
    prior_actuation: bool
    prior_directive_unconsumed: bool
    advisory_state: OutputContractAdvisoryState = OutputContractAdvisoryState.UNUSED
    actuation_progress_exhausted: bool = False
    declick_attempt_failed: bool = False
    advisory_run_grantable: bool = False
    consumed_run_output_observed: bool = False
    consumed_run_bound_required_path: bool = False
    consumed_run_carried_page_extraction: bool = False
    loaded_result_source_producible: bool = False


@dataclass(frozen=True)
class OutputContractActuation:
    kind: OutputContractActuationKind
    family: OutputContractBailFamily
    reason_code: str = ""


def resolve_output_contract_actuation(
    *,
    family: OutputContractBailFamily,
    evidence: OutputContractActuationEvidence,
) -> OutputContractActuation:
    """Total lattice keyed on typed evidence, never reject counts: terminals require typed
    evidence (output_source_unobservable only for a click-only spine with zero observed values
    AND a failed de-click-only attempt; actuation_exhausted only after an advisory run was
    consumed), and any observable source — regardless of family — always reaches one adjudicating
    advisory run before any exhaustion terminal, so exhaustion count alone with a producible source
    never terminals and a lone flaky scout pass never terminals a producible click-only shape.
    A grantable advisory run (a producible separated-spine source whose imposition flaked this pass)
    preempts the no-source terminal until it is consumed, so arm D never fires while a run is still
    grantable. The exhaustion terminal keys on the executed run's observed output, never on draft
    shape: it requires that a consumed run's output was observed, that the run carried the imposed
    page-source extraction, and that it still bound no required path. A consumed run whose output was
    observed but bound nothing without a page-source extraction on board is not exhaustion evidence —
    it re-enters the ladder once so the stronger page-source imposition can bind the on-screen values a
    code static-return provably cannot key."""
    if evidence.imposed_available:
        return OutputContractActuation(OutputContractActuationKind.IMPOSED, family)
    grantable_source = (
        evidence.advisory_run_grantable and evidence.advisory_state != OutputContractAdvisoryState.CONSUMED
    )
    observable_source = (
        evidence.observed_required_values or evidence.loaded_result_source_producible or not evidence.click_only_spine
    )
    producible_source = observable_source or grantable_source
    progressed = evidence.actuation_progress_exhausted or evidence.prior_actuation
    if (
        evidence.click_only_spine
        and not evidence.observed_required_values
        and not evidence.loaded_result_source_producible
        and evidence.declick_attempt_failed
        and not grantable_source
    ):
        return OutputContractActuation(
            OutputContractActuationKind.BLOCKED_TERMINAL,
            family,
            OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        )
    if (
        producible_source
        and evidence.advisory_state in {OutputContractAdvisoryState.UNUSED, OutputContractAdvisoryState.GRANTED}
        and (evidence.actuation_progress_exhausted or evidence.prior_directive_unconsumed)
    ):
        return OutputContractActuation(OutputContractActuationKind.ADVISORY_RUN, family)
    if (
        evidence.advisory_state == OutputContractAdvisoryState.CONSUMED
        and progressed
        and evidence.consumed_run_output_observed
        and evidence.consumed_run_carried_page_extraction
        and not evidence.consumed_run_bound_required_path
    ):
        return OutputContractActuation(
            OutputContractActuationKind.BLOCKED_TERMINAL,
            family,
            OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
        )
    return OutputContractActuation(OutputContractActuationKind.STRUCTURE_DIRECTIVE, family)


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
