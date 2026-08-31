from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from typing import Any, Literal

from jinja2 import Environment, nodes
from typing_extensions import NotRequired, TypedDict

from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps_in_yaml
from skyvern.forge.sdk.copilot.data_write_defaults import DATA_WRITE_BLOCK_TYPES
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.services import google_drive_service
from skyvern.schemas.google_sheets import extract_a1_sheet_prefix, extract_spreadsheet_id
from skyvern.schemas.workflows import BlockType, FileStorageType

ReviewChange = Literal["added", "changed", "unchanged", "removed"]
BlockCoverage = Literal["current_source", "different_source", "never_run", "unknown"]
ParameterDefault = str | int | float | bool
DestinationIdentity = tuple[str, ...]
DestinationAdapter = Callable[[dict[str, Any], Mapping[str, ParameterDefault]], DestinationIdentity | None]

_JINJA_ENV = Environment()
_PARAMETER_IDENTITY_PREFIX = "\0workflow-parameter\0"
_IGNORED_FINGERPRINT_KEYS = frozenset({"label"})
_IGNORED_COMPARISON_KEYS = frozenset({"label", "next_block_label"})


class NarrativeReviewBlock(TypedDict):
    label: str
    blockType: str
    change: ReviewChange
    neverTested: NotRequired[bool]
    coverage: NotRequired[BlockCoverage]


class NarrativeDuplicateWrite(TypedDict):
    blockType: str
    blockLabels: list[str]


class NarrativeReviewProjection(TypedDict):
    blocks: list[NarrativeReviewBlock]
    duplicateWrites: list[NarrativeDuplicateWrite]


def _block_type(block: Mapping[str, Any]) -> str:
    return str(block.get("block_type") or "").strip().lower()


def _parameter_defaults(parsed: Mapping[str, Any]) -> dict[str, ParameterDefault]:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return {}
    parameters = definition.get("parameters")
    if not isinstance(parameters, list):
        return {}
    defaults: dict[str, ParameterDefault] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        value = parameter.get("default_value")
        if value is None and parameter.get("parameter_type") == "credential":
            value = parameter.get("credential_id")
        if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
            defaults[key] = value
    return defaults


def _resolved_identity_value(
    raw: object,
    defaults: Mapping[str, ParameterDefault],
    *,
    blank_value: str = "",
) -> str | None:
    if raw is None:
        return blank_value
    if not isinstance(raw, (str, int, float, bool)):
        return None
    if not isinstance(raw, str):
        return str(raw)
    if "{{" not in raw and "{%" not in raw and "{#" not in raw:
        return raw.strip()
    try:
        parsed = _JINJA_ENV.parse(raw)
    except Exception:
        return None
    # Destination comparison only needs the workflow parameter form used by
    # runtime identities (``{{ account }}``). Inspect the Jinja AST without
    # executing model-authored expressions on the terminal response path.
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], nodes.Output):
        return None
    output_nodes = parsed.body[0].nodes
    if len(output_nodes) != 1 or not isinstance(output_nodes[0], nodes.Name):
        return None
    parameter_name = output_nodes[0].name
    if parameter_name not in defaults:
        # Two fields bound to the same required runtime parameter necessarily
        # resolve to the same value even though that value is not known yet.
        return f"{_PARAMETER_IDENTITY_PREFIX}{parameter_name}"
    return str(defaults[parameter_name]).strip()


def _google_sheets_resource(reference: str) -> str | None:
    if reference.startswith(_PARAMETER_IDENTITY_PREFIX):
        return reference
    try:
        return extract_spreadsheet_id(reference)
    except ValueError:
        return None


def _google_sheets_destination(
    block: dict[str, Any], defaults: Mapping[str, ParameterDefault]
) -> DestinationIdentity | None:
    spreadsheet_url = _resolved_identity_value(block.get("spreadsheet_url"), defaults)
    account = _resolved_identity_value(block.get("credential_id"), defaults)
    sheet_name = _resolved_identity_value(block.get("sheet_name"), defaults)
    cell_range = _resolved_identity_value(block.get("range"), defaults)
    if not spreadsheet_url or not account or sheet_name is None or cell_range is None:
        return None
    resource = _google_sheets_resource(spreadsheet_url)
    if resource is None:
        return None
    tab = sheet_name or extract_a1_sheet_prefix(cell_range)
    if not tab:
        return None
    return account, resource, tab


def _google_drive_destination(
    block: dict[str, Any], defaults: Mapping[str, ParameterDefault]
) -> DestinationIdentity | None:
    storage_type = _resolved_identity_value(block.get("storage_type"), defaults)
    if storage_type != FileStorageType.GOOGLE_DRIVE.value:
        return None
    account = _resolved_identity_value(block.get("google_credential_id"), defaults)
    folder_reference = _resolved_identity_value(block.get("google_drive_folder_id"), defaults)
    if not account or folder_reference is None:
        return None
    try:
        folder = google_drive_service.extract_folder_id(folder_reference)
    except ValueError:
        return None
    return account, folder or "my-drive-root"


def _file_upload_destination(
    block: dict[str, Any], defaults: Mapping[str, ParameterDefault]
) -> DestinationIdentity | None:
    storage_type = _resolved_identity_value(block.get("storage_type"), defaults, blank_value=FileStorageType.S3.value)
    if storage_type == FileStorageType.GOOGLE_DRIVE.value:
        destination = _google_drive_destination(block, defaults)
        return (storage_type, *destination) if destination is not None else None
    if storage_type == FileStorageType.S3.value:
        account = _resolved_identity_value(block.get("aws_access_key_id"), defaults)
        bucket = _resolved_identity_value(block.get("s3_bucket"), defaults)
        region = _resolved_identity_value(block.get("region_name"), defaults)
        folder = _resolved_identity_value(block.get("path"), defaults, blank_value="run-root")
        return (
            (storage_type, account, bucket, region, folder)
            if account and bucket and region is not None and folder is not None
            else None
        )
    if storage_type == FileStorageType.AZURE.value:
        account = _resolved_identity_value(block.get("azure_storage_account_name"), defaults)
        container = _resolved_identity_value(block.get("azure_blob_container_name"), defaults)
        folder = _resolved_identity_value(block.get("path"), defaults, blank_value="run-root")
        return (storage_type, account, container, folder) if account and container and folder is not None else None
    if storage_type == FileStorageType.SFTP.value:
        host = _resolved_identity_value(block.get("sftp_host"), defaults)
        port = _resolved_identity_value(block.get("sftp_port"), defaults, blank_value="22")
        username = _resolved_identity_value(block.get("sftp_username"), defaults)
        remote_path = _resolved_identity_value(block.get("sftp_remote_path"), defaults)
        return (
            (storage_type, host, port, username, remote_path)
            if host and port and username and remote_path is not None
            else None
        )
    return None


def _not_comparable(_block: dict[str, Any], _defaults: Mapping[str, ParameterDefault]) -> DestinationIdentity | None:
    return None


_COMPARABLE_DESTINATION_ADAPTERS: dict[str, DestinationAdapter] = {
    BlockType.GOOGLE_SHEETS_WRITE.value: _google_sheets_destination,
    BlockType.FILE_UPLOAD.value: _file_upload_destination,
}
DESTINATION_ADAPTERS: dict[str, DestinationAdapter] = {
    block_type: _COMPARABLE_DESTINATION_ADAPTERS.get(block_type, _not_comparable)
    for block_type in DATA_WRITE_BLOCK_TYPES
}


def _comparison_value(block: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if key not in _IGNORED_COMPARISON_KEYS}


def _workflow_execution_inputs(parsed: Mapping[str, Any]) -> dict[str, Any]:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return {}
    parameters = definition.get("parameters")
    normalized_parameters = (
        [
            {key: value for key, value in parameter.items() if key != "description"}
            for parameter in parameters
            if isinstance(parameter, dict)
        ]
        if isinstance(parameters, list)
        else []
    )
    normalized_parameters.sort(key=lambda parameter: json.dumps(parameter, sort_keys=True, default=str))
    return {
        "workflow": {
            key: value
            for key, value in parsed.items()
            if key not in {"title", "description", "status", "is_saved_task", "folder_id", "workflow_definition"}
        },
        "definition": {key: value for key, value in definition.items() if key not in {"blocks", "parameters"}},
        "parameters": normalized_parameters,
    }


def _block_fingerprint(block: Mapping[str, Any], workflow_execution_inputs: Mapping[str, Any]) -> str:
    # Ignore only the indexed block's own label. Nested labels and control-flow
    # links remain part of the exact version that a run exercised.
    value = {
        "block": {key: item for key, item in block.items() if key not in _IGNORED_FINGERPRINT_KEYS},
        "workflowExecutionInputs": workflow_execution_inputs,
    }
    serialized = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _next_common_label(
    block: Mapping[str, Any],
    blocks_by_label: Mapping[str, Mapping[str, Any]],
    common_labels: set[str],
) -> str | None:
    next_label = block.get("next_block_label")
    visited: set[str] = set()
    while isinstance(next_label, str) and next_label and next_label not in visited:
        if next_label in common_labels:
            return next_label
        visited.add(next_label)
        next_block = blocks_by_label.get(next_label)
        if next_block is None:
            return None
        next_label = next_block.get("next_block_label")
    return None


def workflow_block_fingerprints(workflow_yaml: str) -> dict[str, set[str]]:
    if not isinstance(workflow_yaml, str):
        return {}
    parsed = parse_workflow_yaml(derive_code_block_steps_in_yaml(workflow_yaml))
    if not isinstance(parsed, dict):
        return {}
    indexed = _labeled_blocks(parsed)
    if indexed is None:
        return {}
    blocks, _ = indexed
    execution_inputs = _workflow_execution_inputs(parsed)
    return {block["label"]: {_block_fingerprint(block, execution_inputs)} for block in blocks}


def serialize_execution_receipts(receipts: Mapping[str, Collection[str]]) -> dict[str, list[str]]:
    return {
        label: sorted({fingerprint for fingerprint in fingerprints if isinstance(fingerprint, str) and fingerprint})
        for label, fingerprints in receipts.items()
        if isinstance(label, str) and label
    }


def parse_execution_receipts(raw: object) -> dict[str, set[str]]:
    if not isinstance(raw, dict):
        return {}
    receipts: dict[str, set[str]] = {}
    for label, fingerprints in raw.items():
        if not isinstance(label, str) or not label or not isinstance(fingerprints, list):
            continue
        parsed = {fingerprint for fingerprint in fingerprints if isinstance(fingerprint, str) and fingerprint}
        if parsed:
            receipts[label] = parsed
    return receipts


def _labeled_blocks(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]] | None:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict) or not isinstance(definition.get("blocks"), list):
        return None
    ordered: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}
    for block in workflow_blocks(parsed):
        label = block.get("label")
        if not isinstance(label, str) or not label or label in by_label:
            return None
        ordered.append(block)
        by_label[label] = block
    return ordered, by_label


def _duplicate_writes(
    staged_blocks: list[dict[str, Any]], defaults: Mapping[str, ParameterDefault]
) -> list[NarrativeDuplicateWrite]:
    labels_by_destination: dict[tuple[str, ...], list[str]] = {}
    for block in staged_blocks:
        block_type = _block_type(block)
        adapter = DESTINATION_ADAPTERS.get(block_type)
        if adapter is None:
            continue
        destination = adapter(block, defaults)
        label = block.get("label")
        if destination is None or not isinstance(label, str):
            continue
        labels_by_destination.setdefault((block_type, *destination), []).append(label)
    return [
        {"blockType": identity[0], "blockLabels": labels}
        for identity, labels in labels_by_destination.items()
        if len(labels) > 1
    ]


def build_review_projection(
    persisted_workflow_yaml: str,
    staged_workflow_yaml: str,
    executed_blocks: Mapping[str, Collection[str] | str] | set[str],
) -> NarrativeReviewProjection | None:
    if not isinstance(persisted_workflow_yaml, str) or not isinstance(staged_workflow_yaml, str):
        return None
    persisted = parse_workflow_yaml(derive_code_block_steps_in_yaml(persisted_workflow_yaml))
    staged = parse_workflow_yaml(derive_code_block_steps_in_yaml(staged_workflow_yaml))
    if not isinstance(persisted, dict) or not isinstance(staged, dict):
        return None
    persisted_index = _labeled_blocks(persisted)
    staged_index = _labeled_blocks(staged)
    if persisted_index is None or staged_index is None:
        return None
    persisted_blocks, persisted_by_label = persisted_index
    staged_blocks, staged_by_label = staged_index
    persisted_common = [block["label"] for block in persisted_blocks if block["label"] in staged_by_label]
    staged_common = [block["label"] for block in staged_blocks if block["label"] in persisted_by_label]
    persisted_positions = {label: index for index, label in enumerate(persisted_common)}
    staged_positions = {label: index for index, label in enumerate(staged_common)}
    common_labels = set(persisted_common)
    persisted_execution_inputs = _workflow_execution_inputs(persisted)
    staged_execution_inputs = _workflow_execution_inputs(staged)
    execution_inputs_unchanged = persisted_execution_inputs == staged_execution_inputs
    staged_fingerprints = {
        block["label"]: _block_fingerprint(block, staged_execution_inputs) for block in staged_blocks
    }

    if isinstance(executed_blocks, Mapping):
        receipts: dict[str, set[str]] = {
            label: {value} if isinstance(value, str) else set(value) for label, value in executed_blocks.items()
        }
        source_bound = True
    else:
        receipts = {label: set() for label in executed_blocks}
        source_bound = False
    # A block's own label is excluded from its fingerprint, so a receipt stranded under a
    # label the staged workflow no longer has still identifies source this turn ran.
    orphan_fingerprints = {
        fingerprint
        for label, recorded in receipts.items()
        if label not in staged_fingerprints
        for fingerprint in recorded
    }

    review_blocks: list[NarrativeReviewBlock] = []
    for block in staged_blocks:
        label = block["label"]
        prior = persisted_by_label.get(label)
        if prior is None:
            change: ReviewChange = "added"
        elif (
            execution_inputs_unchanged
            and _comparison_value(prior) == _comparison_value(block)
            and persisted_positions[label] == staged_positions[label]
            and _next_common_label(prior, persisted_by_label, common_labels)
            == _next_common_label(block, staged_by_label, common_labels)
        ):
            change = "unchanged"
        else:
            change = "changed"
        recorded = receipts.get(label)
        if recorded is None:
            coverage: BlockCoverage = "unknown" if staged_fingerprints[label] in orphan_fingerprints else "never_run"
        elif not source_bound or staged_fingerprints[label] in recorded:
            coverage = "current_source"
        else:
            coverage = "different_source"
        review_blocks.append(
            {
                "label": label,
                "blockType": _block_type(block),
                "change": change,
                "neverTested": coverage != "current_source",
                "coverage": coverage,
            }
        )
    for block in persisted_blocks:
        label = block["label"]
        if label not in staged_by_label:
            review_blocks.append({"label": label, "blockType": _block_type(block), "change": "removed"})

    return {
        "blocks": review_blocks,
        "duplicateWrites": _duplicate_writes(staged_blocks, _parameter_defaults(staged)),
    }
