from __future__ import annotations

import ast
import json
import re
import textwrap
from collections.abc import Mapping
from typing import Any, Literal, Protocol

import yaml

from skyvern.forge.sdk.copilot.completion_verification import CriterionVerdict, RunEvidenceSnapshot
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    lookup_requested_output_path_alias,
    schema_output_path_aliases_from_criteria,
)
from skyvern.utils.yaml_loader import safe_load_no_dates

_GOAL_PATH_INDEX_PATTERN = re.compile(r"\[\d+\]")
_REQUESTED_OUTPUT_PREFIX = "output."
_STRUCTURAL_ABSTENTION_REASON_CODE = "structurally_abstained"
# Ignored at any requested-output traversal depth, including schema-declared nested fields.
_IGNORED_RUNTIME_FIELD_NAMES = frozenset({"evidence_text"})


class _GroundingCtx(Protocol):
    code_artifact_metadata: object
    last_workflow_yaml: str | None
    workflow_yaml: str | None


def split_requested_output_criteria(
    criteria: list[CompletionCriterion],
) -> tuple[list[CompletionCriterion], list[CompletionCriterion]]:
    requested: list[CompletionCriterion] = []
    remaining: list[CompletionCriterion] = []
    for criterion in criteria:
        if (
            criterion.output_path
            and criterion.level != "definition"
            and not criterion.method_mandated
            and criterion.kind != "validation_classification"
            and _normalize_output_path(criterion.output_path)
        ):
            requested.append(criterion)
        else:
            remaining.append(criterion)
    return requested, remaining


def grade_requested_output_criteria(
    copilot_ctx: _GroundingCtx,
    criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
) -> list[CriterionVerdict]:
    requested_path_aliases = schema_output_path_aliases_from_criteria(criteria)
    authored_paths = _authored_output_contract_paths(copilot_ctx, requested_path_aliases)
    verdicts: list[CriterionVerdict] = []
    for criterion in criteria:
        path = _normalize_output_path(criterion.output_path)
        if not path or path not in authored_paths:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code="unproducible",
                    missing_evidence=f"accepted code artifact metadata does not declare output.{path or ''}",
                )
            )
            continue
        expected_value = criterion.expected_output_value
        expected_shape = criterion.expected_output_shape
        grounding_mode = _criterion_grounding_mode(criterion)
        trace_fields = _criterion_trace_fields(criterion, grounding_mode)
        if expected_value is not None:
            match_state, evidence_ref = _runtime_output_path_match(snapshot.block_outputs, path, expected_value)
        elif expected_shape is not None:
            match_state, evidence_ref = _runtime_output_path_presence(snapshot.block_outputs, path)
        else:
            match_state, evidence_ref = _runtime_output_path_presence(snapshot.block_outputs, path)
            if match_state == "present" and evidence_ref is not None:
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id,
                        state="unsatisfied",
                        reason_code=_STRUCTURAL_ABSTENTION_REASON_CODE,
                        evidence_ref=evidence_ref,
                        missing_evidence=(
                            "requested-output field is present, but the criterion has no typed expected_output_value "
                            "that can prove or refute the value"
                        ),
                        **trace_fields,
                    )
                )
                continue
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code="no_evidence",
                    missing_evidence=(
                        "requested-output criterion lacks typed expected_output_value or expected_output_shape; "
                        "presence-only output cannot confirm value-grounded criterion"
                    ),
                    **trace_fields,
                )
            )
            continue
        if match_state == "present" and evidence_ref is not None:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code=_STRUCTURAL_ABSTENTION_REASON_CODE,
                    evidence_ref=evidence_ref,
                    missing_evidence=(
                        "requested-output field is present with a typed expected_output_shape, but no exact "
                        "expected_output_value can prove or refute the value"
                    ),
                    **trace_fields,
                )
            )
            continue
        if match_state == "satisfied" and evidence_ref is not None:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="evidence_confirms",
                    evidence_ref=evidence_ref,
                    **trace_fields,
                )
            )
            continue
        if match_state == "contradicted":
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code="evidence_contradicts",
                    evidence_ref=evidence_ref,
                    missing_evidence=(
                        f"run output included output.{path} but it did not match the expected value"
                        if expected_value is not None
                        else f"run output included output.{path} but it could not be value-grounded"
                    ),
                    **trace_fields,
                )
            )
            continue
        verdicts.append(
            CriterionVerdict(
                criterion_id=criterion.id,
                state="unsatisfied",
                reason_code="missing_exact_field",
                missing_evidence=f"run output did not include exact structured field output.{path}",
                **trace_fields,
            )
        )
    return verdicts


def _criterion_grounding_mode(criterion: CompletionCriterion) -> Literal["exact_value", "shape", "missing"]:
    if criterion.expected_output_value is not None:
        return "exact_value"
    if criterion.expected_output_shape is not None:
        return "shape"
    return "missing"


def _criterion_trace_fields(criterion: CompletionCriterion, grounding_mode: str) -> dict[str, Any]:
    return {
        "output_path": criterion.output_path,
        "grounding_mode": grounding_mode,
        "expected_output_shape": criterion.expected_output_shape,
        "has_exact_value": criterion.expected_output_value is not None,
    }


def _authored_output_contract_paths(copilot_ctx: _GroundingCtx, aliases: dict[str, str] | None = None) -> set[str]:
    metadata = copilot_ctx.code_artifact_metadata
    if not isinstance(metadata, Mapping):
        return set()
    code_by_label = _code_blocks_by_label(copilot_ctx)
    paths: set[str] = set()
    for label, artifact in metadata.items():
        if not isinstance(label, str) or not isinstance(artifact, Mapping):
            continue
        paths.update(_artifact_contract_paths(artifact))
        code = code_by_label.get(label)
        if code:
            paths.update(_static_return_paths(code))
    return _canonical_output_paths(paths, aliases)


def _canonical_output_paths(paths: set[str], aliases: dict[str, str] | None) -> set[str]:
    canonical: set[str] = set()
    for path in paths:
        normalized = _normalize_output_path(path)
        if not normalized:
            continue
        alias_path = lookup_requested_output_path_alias(normalized, aliases)
        canonical.add(_normalize_output_path(alias_path) if alias_path else normalized)
    return canonical


def _artifact_contract_paths(artifact: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    for row_group in (artifact.get("claimed_outcomes"), artifact.get("terminal_verifier_expectations")):
        rows = [row for row in row_group if isinstance(row, Mapping)] if isinstance(row_group, list) else []
        for row in rows:
            paths.update(_metadata_goal_value_paths(row.get("goal_value_paths")))
            paths.update(_schema_output_paths(_parse_extraction_schema(row.get("extraction_schema"))))
    return paths


def _metadata_goal_value_paths(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        path = _normalize_output_path(item)
        if path and not path.casefold().startswith("<fill"):
            paths.add(path)
    return paths


def _parse_extraction_schema(value: Any) -> Mapping[str, Any] | None:
    # Metadata schemas are stored as JSON text or already-decoded mappings.
    if isinstance(value, Mapping):
        return value or None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in {"null", "none"} or text.casefold().startswith("<fill"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) and parsed else None


def _schema_output_paths(schema: Mapping[str, Any] | None, prefix: str = "") -> set[str]:
    if not isinstance(schema, Mapping):
        return set()
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, Mapping):
            # Runtime code blocks return top-level arrays directly; [] mirrors that list-wrapped traversal.
            array_prefix = f"{prefix}[]" if prefix else ""
            return _schema_output_paths(items, array_prefix)
        return {prefix} if prefix else set()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {prefix} if prefix else set()
    paths: set[str] = set()
    for raw_name, child_schema in properties.items():
        name = str(raw_name).strip()
        if not name:
            continue
        child_prefix = f"{prefix}.{name}" if prefix else name
        paths.add(child_prefix)
        if isinstance(child_schema, Mapping):
            paths.update(_schema_output_paths(child_schema, child_prefix))
    return paths


def _code_blocks_by_label(copilot_ctx: _GroundingCtx) -> dict[str, str]:
    workflow_yaml = copilot_ctx.last_workflow_yaml
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        workflow_yaml = copilot_ctx.workflow_yaml
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return {}
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, Mapping):
        return {}
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return {}
    by_label: dict[str, str] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        label = block.get("label")
        code = block.get("code")
        if isinstance(label, str) and label and isinstance(code, str) and code.strip():
            by_label[label] = code
    return by_label


def _static_return_paths(code: str) -> set[str]:
    return {key for key in (_top_level_return_dict_keys(code) or set()) if key}


def _top_level_return_dict_keys(code: str) -> set[str] | None:
    wrapped = "async def __copilot_block__():\n" + textwrap.indent(textwrap.dedent(code).strip() or "pass", "    ")
    try:
        tree = ast.parse(wrapped)
    except SyntaxError:
        return None
    function = next((node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)), None)
    if function is None:
        return None
    keys: set[str] = set()
    found_return = False
    # Only top-level returns define the code block's authored output contract.
    for node in function.body:
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        found_return = True
        unwrapped = node.value.value if isinstance(node.value, ast.Await) else node.value
        if isinstance(unwrapped, ast.Dict):
            keys.update(_dict_keys(unwrapped))
        elif isinstance(unwrapped, ast.List) and len(unwrapped.elts) == 1 and isinstance(unwrapped.elts[0], ast.Dict):
            keys.update(f"[].{key}" for key in _dict_keys(unwrapped.elts[0]))
        else:
            return None
    return keys if found_return else None


def _dict_keys(node: ast.Dict) -> set[str]:
    return {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}


def _runtime_output_path_match(
    block_outputs: Mapping[str, Any], path: str, expected_value: str | None
) -> tuple[str, str | None]:
    parts = _path_parts(path)
    if not parts or expected_value is None:
        return "missing", None
    contradicted_ref: str | None = None
    for label, payload in block_outputs.items():
        values, source_path = _runtime_path_values(payload, parts, path)
        if not values:
            continue
        evidence_ref = f"block_outputs:{label}.{source_path}"
        if any(_value_matches_expected(value, expected_value) for value in values):
            return "satisfied", evidence_ref
        contradicted_ref = contradicted_ref or evidence_ref
    if contradicted_ref is not None:
        return "contradicted", contradicted_ref
    return "missing", None


def _runtime_output_path_presence(block_outputs: Mapping[str, Any], path: str) -> tuple[str, str | None]:
    parts = _path_parts(path)
    if not parts:
        return "missing", None
    for label, payload in block_outputs.items():
        values, source_path = _runtime_present_path_values(payload, parts, path)
        if not values:
            continue
        evidence_ref = f"block_outputs:{label}.{source_path}"
        return "present", evidence_ref
    return "missing", None


def _runtime_path_values(payload: Any, parts: list[str], path: str) -> tuple[list[Any], str]:
    canonical_values = _path_values(payload, parts)
    if canonical_values:
        filtered_canonical_values = [value for value in canonical_values if _is_meaningful_requested_value(value)]
        return filtered_canonical_values or canonical_values, path
    wrapped_parts = ["output", *parts]
    wrapped_path = ".".join(wrapped_parts)
    wrapped_values = [value for value in _path_values(payload, wrapped_parts) if _is_meaningful_requested_value(value)]
    return wrapped_values, wrapped_path


def _runtime_present_path_values(payload: Any, parts: list[str], path: str) -> tuple[list[Any], str]:
    raw_canonical_values = _path_values(payload, parts)
    if raw_canonical_values:
        canonical_values = [value for value in raw_canonical_values if _is_meaningful_requested_value(value)]
        return canonical_values, path
    wrapped_parts = ["output", *parts]
    wrapped_path = ".".join(wrapped_parts)
    wrapped_values = [value for value in _path_values(payload, wrapped_parts) if _is_meaningful_requested_value(value)]
    return wrapped_values, wrapped_path


def _path_values(value: Any, parts: list[str]) -> list[Any]:
    if not parts:
        return [value]
    part = parts[0]
    if part == "[]":
        if not isinstance(value, list):
            return []
        return [matched for item in value for matched in _path_values(item, parts[1:])]
    expects_array = part.endswith("[]")
    key = part[:-2] if expects_array else part
    if key in _IGNORED_RUNTIME_FIELD_NAMES:
        return []
    if isinstance(value, Mapping):
        if key not in value:
            return []
        child = value[key]
        if expects_array:
            if not isinstance(child, list):
                return []
            return [matched for item in child for matched in _path_values(item, parts[1:])]
        return _path_values(child, parts[1:])
    if isinstance(value, list):
        return [matched for item in value for matched in _path_values(item, parts)]
    return []


def _is_meaningful_requested_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_is_meaningful_requested_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_is_meaningful_requested_value(item) for item in value)
    return True


def _value_matches_expected(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_value_matches_expected(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_value_matches_expected(item, expected) for item in value)
    observed = _normalized_expected_text(value)
    target = _normalized_expected_text(expected)
    if not observed or not target:
        return False
    return observed == target or _compact_expected_text(observed) == _compact_expected_text(target)


def _normalized_expected_text(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def _compact_expected_text(value: str) -> str:
    return "".join(char for char in value if char.isalnum())


def _normalize_output_path(path: str | None) -> str:
    if not isinstance(path, str):
        return ""
    normalized = path.strip()
    if normalized.startswith(_REQUESTED_OUTPUT_PREFIX):
        normalized = normalized[len(_REQUESTED_OUTPUT_PREFIX) :]
    if normalized == "$":
        return ""
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$["):
        normalized = normalized[1:]
    normalized = normalized.replace("[*]", "[]")
    normalized = _GOAL_PATH_INDEX_PATTERN.sub("[]", normalized)
    return ".".join(part for part in normalized.split(".") if part)


def _path_parts(path: str) -> list[str]:
    return [part for part in path.split(".") if part]
