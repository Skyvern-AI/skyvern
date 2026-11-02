from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from typing import Any, Literal, get_args

import structlog
from jinja2 import Environment, nodes
from typing_extensions import NotRequired, TypedDict

from skyvern.exceptions import InvalidWorkflowParameter
from skyvern.forge.sdk.copilot.data_write_defaults import DATA_WRITE_BLOCK_TYPES
from skyvern.forge.sdk.copilot.input_references import block_read_keys
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.copilot.workflow_yaml import canonical_copilot_workflow
from skyvern.forge.sdk.services import google_drive_service
from skyvern.forge.sdk.workflow.block_notes import annotate_notes_read, definition_step_notes, strip_inert_block_fields
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType
from skyvern.schemas.google_sheets import extract_a1_sheet_prefix, extract_spreadsheet_id
from skyvern.schemas.workflows import PARAMETER_YAML_SUBCLASSES, BlockType, FileStorageType

ReviewChange = Literal["added", "changed", "unchanged", "removed"]
InputChange = Literal["added", "changed", "removed"]
BlockCoverage = Literal["current_source", "different_source", "never_run", "unknown"]
ParameterDefault = str | int | float | bool
DestinationIdentity = tuple[str, ...]
DestinationAdapter = Callable[[dict[str, Any], Mapping[str, ParameterDefault]], DestinationIdentity | None]

LOG = structlog.get_logger()

_JINJA_ENV = Environment()
_PARAMETER_IDENTITY_PREFIX = "\0workflow-parameter\0"
_IGNORED_FINGERPRINT_KEYS = frozenset({"label"})
_IGNORED_COMPARISON_KEYS = frozenset({"label", "next_block_label"})
_PARAMETER_FIELDS_NOT_BINDINGS = frozenset(
    {"key", "parameter_type", "workflow_parameter_type", "default_value", "description"}
)


class NarrativeReviewBlock(TypedDict):
    label: str
    blockType: str
    change: ReviewChange
    neverTested: NotRequired[bool]
    coverage: NotRequired[BlockCoverage]


class NarrativeDuplicateWrite(TypedDict):
    blockType: str
    blockLabels: list[str]


class NarrativeReviewInput(TypedDict):
    key: str
    change: InputChange


class NarrativeReviewProjection(TypedDict):
    blocks: list[NarrativeReviewBlock]
    duplicateWrites: list[NarrativeDuplicateWrite]
    inputs: list[NarrativeReviewInput]
    keptNotes: NotRequired[list[str]]
    keptDescriptions: NotRequired[list[str]]


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


CODE_BLOCK_NON_IDENTITY_KEYS = frozenset({"steps", "user_owned_goal", "goal_needs_regeneration", "code_edited_by_hand"})


def _without_code_steps(value: Any) -> Any:
    """Drop code-derived `steps` and the Goal-ownership facts at every nesting depth; neither is execution identity."""
    if isinstance(value, dict):
        is_code = _block_type(value) == BlockType.CODE.value
        return {
            key: _without_code_steps(item)
            for key, item in value.items()
            if not (is_code and key in CODE_BLOCK_NON_IDENTITY_KEYS)
        }
    if isinstance(value, list):
        return [_without_code_steps(item) for item in value]
    return value


def _execution_view(block: Mapping[str, Any], notes_by_label: Mapping[str, str]) -> dict[str, Any]:
    """The block as a run sees it: without the people-only description, with the notes it reads."""
    view = strip_inert_block_fields(block)
    annotate_notes_read([view], notes_by_label)
    return view


def _workflow_step_notes(parsed: Mapping[str, Any]) -> dict[str, str]:
    definition = parsed.get("workflow_definition")
    return definition_step_notes(definition) if isinstance(definition, dict) else {}


def _comparison_value(block: Mapping[str, Any], notes_by_label: Mapping[str, str]) -> dict[str, Any]:
    return {
        key: value
        for key, value in _without_code_steps(_execution_view(block, notes_by_label)).items()
        if key not in _IGNORED_COMPARISON_KEYS
    }


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


_YAML_PARAMETER_FIELDS: dict[str, frozenset[str]] = {
    parameter_model.model_fields["parameter_type"].default: frozenset(parameter_model.model_fields)
    for parameter_model in get_args(PARAMETER_YAML_SUBCLASSES)
}


def _comparable_parameter(parameter: Mapping[str, Any]) -> dict[str, Any]:
    declared_type = parameter.get("parameter_type")
    schema_fields = _YAML_PARAMETER_FIELDS.get(declared_type) if isinstance(declared_type, str) else None
    fields = {
        field: value
        for field, value in parameter.items()
        if value is not None and (schema_fields is None or field in schema_fields)
    }
    if not fields.get("description"):
        fields.pop("description", None)
    workflow_parameter_type = fields.get("workflow_parameter_type")
    if (
        fields.get("parameter_type") == ParameterType.WORKFLOW
        and "default_value" in fields
        and isinstance(workflow_parameter_type, str)
    ):
        try:
            # Coerced as the save converter does, so a typed default and the YAML it was saved from compare equal.
            fields["default_value"] = WorkflowParameterType(workflow_parameter_type).convert_default_value(
                fields["default_value"]
            )
        except (ValueError, TypeError, InvalidWorkflowParameter):
            pass
    return fields


def _parameters_by_key(parsed: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    definition = parsed.get("workflow_definition")
    parameters = definition.get("parameters") if isinstance(definition, dict) else None
    if not isinstance(parameters, list):
        return {}
    return {
        parameter["key"]: _comparable_parameter(parameter)
        for parameter in parameters
        if isinstance(parameter, dict)
        and isinstance(parameter.get("key"), str)
        and parameter.get("parameter_type") != ParameterType.OUTPUT
    }


def _parameter_signature(parameter: Mapping[str, Any], ignored_field: str | None = None) -> str:
    return json.dumps(
        {field: value for field, value in parameter.items() if field != ignored_field}, sort_keys=True, default=str
    )


def _input_changes(persisted: Mapping[str, Any], staged: Mapping[str, Any]) -> list[NarrativeReviewInput]:
    before = _parameters_by_key(persisted)
    after = _parameters_by_key(staged)
    rows: list[NarrativeReviewInput] = []
    for key, parameter in after.items():
        if key not in before:
            rows.append({"key": key, "change": "added"})
        elif _parameter_signature(before[key]) != _parameter_signature(parameter):
            rows.append({"key": key, "change": "changed"})
    rows.extend({"key": key, "change": "removed"} for key in before if key not in after)
    return rows


def _changed_input_keys(
    persisted: Mapping[str, Any], staged: Mapping[str, Any], input_rows: list[NarrativeReviewInput]
) -> set[str]:
    """The changed inputs, plus every parameter bound to one of them, transitively."""
    before = _parameters_by_key(persisted)
    after = _parameters_by_key(staged)
    # Nothing at run time reads a description, so editing one leaves the blocks that read the input unchanged.
    changed = {
        row["key"]
        for row in input_rows
        if row["change"] != "changed"
        or _parameter_signature(before[row["key"]], "description")
        != _parameter_signature(after[row["key"]], "description")
    }
    bindings = [
        (key, {field: value for field, value in parameter.items() if field not in _PARAMETER_FIELDS_NOT_BINDINGS})
        for side in (before, after)
        for key, parameter in side.items()
    ]
    grew = bool(changed)
    while grew:
        grew = False
        for key, binding in bindings:
            if key not in changed and block_read_keys(binding, changed):
                changed.add(key)
                grew = True
    return changed


def _workflow_settings(parsed: Mapping[str, Any]) -> dict[str, Any]:
    settings = {key: value for key, value in _workflow_execution_inputs(parsed).items() if key != "parameters"}
    # The definition version only says how blocks link, which the position and next-block checks compare.
    settings["definition"] = {key: value for key, value in settings["definition"].items() if key != "version"}
    return settings


def _loop_children_of_changed_loops(
    blocks: list[dict[str, Any]], notes_by_label: Mapping[str, str], keys: set[str]
) -> set[str]:
    """Labels nested in a loop whose own fields read one of ``keys``: what they iterate over changed."""
    labels: set[str] = set()
    for block in blocks:
        children = block.get("loop_blocks")
        if not isinstance(children, list):
            continue
        own_fields = {
            key: value for key, value in _comparison_value(block, notes_by_label).items() if key != "loop_blocks"
        }
        if block_read_keys(own_fields, keys):
            labels.update(
                child["label"]
                for child in workflow_blocks({"workflow_definition": {"blocks": children}})
                if isinstance(child.get("label"), str)
            )
    return labels


def _comparison_documents(
    persisted_workflow_yaml: str,
    staged_workflow_yaml: str,
    persisted: dict[str, Any],
    staged: dict[str, Any],
    candidate_settings: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both sides as the save path would write them, so only meaning, not spelling, can differ."""
    try:
        canonical_persisted = canonical_copilot_workflow(persisted_workflow_yaml, bind_parameters=False)
        canonical_staged = canonical_copilot_workflow(
            staged_workflow_yaml, inherited={**canonical_persisted, **candidate_settings}
        )
    except Exception as exc:  # noqa: BLE001 - the card falls back to the written YAML rather than failing the turn.
        # No exc_info: a validation error quotes workflow values, which can include input defaults.
        LOG.warning("copilot_review_canonical_comparison_failed", error_type=type(exc).__name__)
        return persisted, staged
    for raw, canonical in ((persisted, canonical_persisted), (staged, canonical_staged)):
        raw_index, canonical_index = _labeled_blocks(raw), _labeled_blocks(canonical)
        if (
            raw_index is None
            or canonical_index is None
            or [block["label"] for block in raw_index[0]] != [block["label"] for block in canonical_index[0]]
        ):
            return persisted, staged
    return canonical_persisted, canonical_staged


def _block_fingerprint(
    block: Mapping[str, Any], workflow_execution_inputs: Mapping[str, Any], notes_by_label: Mapping[str, str]
) -> str:
    # Ignore only the indexed block's own label. Nested labels and control-flow
    # links remain part of the exact version that a run exercised.
    value = {
        "block": {
            key: item
            for key, item in _without_code_steps(_execution_view(block, notes_by_label)).items()
            if key not in _IGNORED_FINGERPRINT_KEYS
        },
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
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return {}
    indexed = _labeled_blocks(parsed)
    if indexed is None:
        return {}
    blocks, _ = indexed
    execution_inputs = _workflow_execution_inputs(parsed)
    notes_by_label = _workflow_step_notes(parsed)
    return {block["label"]: {_block_fingerprint(block, execution_inputs, notes_by_label)} for block in blocks}


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
        receipts[label] = {fingerprint for fingerprint in fingerprints if isinstance(fingerprint, str) and fingerprint}
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
    candidate_workflow_yaml: str | None = None,
) -> NarrativeReviewProjection | None:
    """``candidate_workflow_yaml`` is the processed proposal as saved; it supplies the settings the staged YAML
    omits, which the save path takes from the draft rather than the persisted row."""
    if not isinstance(persisted_workflow_yaml, str) or not isinstance(staged_workflow_yaml, str):
        return None
    persisted = parse_workflow_yaml(persisted_workflow_yaml)
    staged = parse_workflow_yaml(staged_workflow_yaml)
    candidate = parse_workflow_yaml(candidate_workflow_yaml) if candidate_workflow_yaml else None
    candidate_settings = (
        {key: value for key, value in candidate.items() if key != "workflow_definition"}
        if isinstance(candidate, dict)
        else {}
    )
    if not isinstance(persisted, dict) or not isinstance(staged, dict):
        return None
    persisted_index = _labeled_blocks(persisted)
    staged_index = _labeled_blocks(staged)
    if persisted_index is None or staged_index is None:
        return None
    persisted_blocks, persisted_by_label = persisted_index
    staged_blocks, staged_by_label = staged_index
    persisted_compared, staged_compared = _comparison_documents(
        persisted_workflow_yaml, staged_workflow_yaml, persisted, staged, candidate_settings
    )
    compared_persisted_blocks, compared_persisted_by_label = _labeled_blocks(persisted_compared) or persisted_index
    compared_staged_blocks, compared_staged_by_label = _labeled_blocks(staged_compared) or staged_index
    persisted_common = [block["label"] for block in compared_persisted_blocks if block["label"] in staged_by_label]
    staged_common = [block["label"] for block in compared_staged_blocks if block["label"] in persisted_by_label]
    persisted_positions = {label: index for index, label in enumerate(persisted_common)}
    staged_positions = {label: index for index, label in enumerate(staged_common)}
    common_labels = set(persisted_common)
    staged_execution_inputs = _workflow_execution_inputs(staged)
    persisted_notes = _workflow_step_notes(persisted_compared)
    staged_notes = _workflow_step_notes(staged_compared)
    input_rows = _input_changes(persisted, staged)
    changed_inputs = _changed_input_keys(persisted, staged, input_rows)
    persisted_settings = _workflow_settings(persisted_compared)
    staged_settings = _workflow_settings(staged_compared)
    settings_unchanged = persisted_settings == staged_settings and not (
        changed_inputs
        and (block_read_keys(persisted_settings, changed_inputs) or block_read_keys(staged_settings, changed_inputs))
    )
    loop_children_changed = (
        _loop_children_of_changed_loops(compared_persisted_blocks, persisted_notes, changed_inputs)
        | _loop_children_of_changed_loops(compared_staged_blocks, staged_notes, changed_inputs)
        if changed_inputs
        else set()
    )
    staged_written_notes = _workflow_step_notes(staged)
    staged_fingerprints = {
        block["label"]: _block_fingerprint(block, staged_execution_inputs, staged_written_notes)
        for block in staged_blocks
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
        if label not in persisted_by_label:
            change: ReviewChange = "added"
        else:
            prior_value = _comparison_value(compared_persisted_by_label[label], persisted_notes)
            staged_value = _comparison_value(compared_staged_by_label[label], staged_notes)
            reads_changed_input = bool(changed_inputs) and bool(
                block_read_keys(prior_value, changed_inputs) | block_read_keys(staged_value, changed_inputs)
            )
            unchanged = (
                settings_unchanged
                and not reads_changed_input
                and label not in loop_children_changed
                and prior_value == staged_value
                and persisted_positions[label] == staged_positions[label]
                and _next_common_label(compared_persisted_by_label[label], compared_persisted_by_label, common_labels)
                == _next_common_label(compared_staged_by_label[label], compared_staged_by_label, common_labels)
            )
            change = "unchanged" if unchanged else "changed"
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
        "inputs": input_rows,
    }
