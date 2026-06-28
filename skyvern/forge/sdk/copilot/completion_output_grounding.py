from __future__ import annotations

import ast
import json
import re
import textwrap
from collections.abc import Mapping
from typing import Any, Protocol

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
        evidence_ref = _runtime_output_path_evidence_ref(snapshot.block_outputs, path)
        if evidence_ref is not None:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="evidence_confirms",
                    evidence_ref=evidence_ref,
                )
            )
            continue
        verdicts.append(
            CriterionVerdict(
                criterion_id=criterion.id,
                state="unsatisfied",
                reason_code="missing_exact_field",
                missing_evidence=f"run output did not include exact structured field output.{path}",
            )
        )
    return verdicts


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


def _runtime_output_path_evidence_ref(block_outputs: Mapping[str, Any], path: str) -> str | None:
    parts = _path_parts(path)
    if not parts:
        return None
    for label, payload in block_outputs.items():
        if _path_has_meaningful_value(payload, parts):
            return f"block_outputs:{label}.{path}"
    return None


def _path_has_meaningful_value(value: Any, parts: list[str]) -> bool:
    if not parts:
        return _is_meaningful_requested_value(value)
    part = parts[0]
    if part == "[]":
        if not isinstance(value, list):
            return False
        return any(_path_has_meaningful_value(item, parts[1:]) for item in value)
    expects_array = part.endswith("[]")
    key = part[:-2] if expects_array else part
    if key in _IGNORED_RUNTIME_FIELD_NAMES:
        return False
    if isinstance(value, Mapping):
        if key not in value:
            return False
        child = value[key]
        if expects_array:
            if not isinstance(child, list):
                return False
            return any(_path_has_meaningful_value(item, parts[1:]) for item in child)
        return _path_has_meaningful_value(child, parts[1:])
    if isinstance(value, list):
        return any(_path_has_meaningful_value(item, parts) for item in value)
    return False


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
