from __future__ import annotations

import ast
import json
import re
import textwrap
from collections.abc import Mapping
from typing import Any, Literal, Protocol

import yaml

from skyvern.forge.sdk.copilot.completion_verification import CriterionVerdict, EvidenceSourceKind, RunEvidenceSnapshot
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    ExpectedOutputValue,
    RequestedOutputEvidenceSource,
    _is_judgment_boolean_criterion,
    lookup_requested_output_path_alias,
    schema_output_path_aliases_from_criteria,
)
from skyvern.utils.yaml_loader import safe_load_no_dates

_GOAL_PATH_INDEX_PATTERN = re.compile(r"\[\d+\]")
_REQUESTED_OUTPUT_PREFIX = "output."
_STRUCTURAL_ABSTENTION_REASON_CODE = "structurally_abstained"
# Ignored at any requested-output traversal depth, including schema-declared nested fields.
_IGNORED_RUNTIME_FIELD_NAMES = frozenset({"evidence_text"})
_INDEPENDENT_EVIDENCE_SOURCES: frozenset[EvidenceSourceKind] = frozenset(
    {"independent_page_evidence", "registered_output_parameter", "registered_artifact_content"}
)
_POST_RUN_PAGE_OBSERVATION_LABEL = "post_run_page_observation"
_REGISTERED_ARTIFACT_OBSERVATION_LABEL = "registered_artifact_observation"
_MIN_CARRIER_VALUE_CHARS = 4
_MAX_CARRIER_TEXT_CHARS = 20_000
_PAGE_EVIDENCE_STAMP_KEYS = frozenset({"workflow_run_id", "observed_after_workflow_run"})


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
    raw_authored_paths_by_label = _authored_output_contract_paths_by_label(copilot_ctx)
    authored_paths_by_label = _authored_output_contract_paths_by_label(copilot_ctx, requested_path_aliases)
    declared_judgment_output_paths = _producer_declared_judgment_output_paths(copilot_ctx.code_artifact_metadata)
    verdicts: list[CriterionVerdict] = []
    for criterion in criteria:
        path = _normalize_output_path(criterion.output_path)
        # A carrier verdict is already sourced from independent evidence and only returned on a
        # positive confirmation, so the early return skips the self-emission veto path that no
        # longer applies; abstentions fall through to normal authored-path grading.
        carrier_verdict = _carrier_confirmation(criterion, path, snapshot)
        if carrier_verdict is not None:
            verdicts.append(carrier_verdict)
            continue
        accepted_labels = (
            {label for label, paths in authored_paths_by_label.items() if path in paths} if path else set()
        )
        if not path or not accepted_labels:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code="unproducible",
                    missing_evidence=f"accepted code artifact metadata does not declare output.{path or ''}",
                )
            )
            continue
        independent_labels = {
            label for label, source in snapshot.block_output_sources.items() if source in _INDEPENDENT_EVIDENCE_SOURCES
        }
        accepted_runtime_outputs = dict(
            _accepted_runtime_outputs(snapshot.block_outputs, accepted_labels, independent_labels)
        )
        accepted_output_sources = {
            label: source
            for label, source in snapshot.block_output_sources.items()
            if label in accepted_runtime_outputs
        }
        accepted_authored_paths = set().union(*(authored_paths_by_label[label] for label in accepted_labels))
        accepted_raw_authored_paths = set().union(
            *(raw_authored_paths_by_label.get(label, set()) for label in accepted_labels)
        )
        projection_roots = _runtime_projection_roots(accepted_authored_paths | accepted_raw_authored_paths)
        expected_value = criterion.expected_output_value
        expected_shape = criterion.expected_output_shape
        grounding_mode = _criterion_grounding_mode(criterion)
        requires_declared_independent_evidence = bool(path) and path in declared_judgment_output_paths
        effective_evidence_source: RequestedOutputEvidenceSource = (
            "independent_run_evidence"
            if requires_declared_independent_evidence
            else criterion.requested_output_evidence_source
        )
        judgment_grounding_bars_self_emission = (
            _is_judgment_boolean_criterion(criterion) or requires_declared_independent_evidence
        )
        trace_fields = _criterion_trace_fields(criterion, grounding_mode, effective_evidence_source)
        if expected_value is not None:
            refutation = _independent_evidence_text_refutation(
                accepted_runtime_outputs,
                accepted_output_sources,
                path,
                expected_value,
                projection_roots,
            )
            if refutation is not None:
                refutation_evidence_ref, evidence_source = refutation
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id,
                        state="unsatisfied",
                        reason_code="evidence_contradicts",
                        evidence_ref=refutation_evidence_ref,
                        missing_evidence=f"independent page evidence refuted emitted output.{path}",
                        evidence_source=evidence_source,
                        **trace_fields,
                    )
                )
                continue
            match_state, evidence_ref = _runtime_output_path_match(
                accepted_runtime_outputs, path, expected_value, projection_roots
            )
        elif expected_shape is not None:
            match_state, evidence_ref = _runtime_output_path_presence(accepted_runtime_outputs, path, projection_roots)
        else:
            match_state, evidence_ref = _runtime_output_path_presence(accepted_runtime_outputs, path, projection_roots)
            if match_state == "present" and evidence_ref is not None:
                present_source = _evidence_source_for_ref(accepted_output_sources, evidence_ref)
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id,
                        state="unsatisfied",
                        reason_code=_STRUCTURAL_ABSTENTION_REASON_CODE,
                        evidence_ref=evidence_ref,
                        missing_evidence=(
                            "requested-output field is present, but the criterion lacks typed expected_output_value "
                            "or expected_output_shape to prove the value"
                        ),
                        evidence_source=present_source,
                        self_emitted_judgment_not_independent=(
                            judgment_grounding_bars_self_emission
                            and present_source not in _INDEPENDENT_EVIDENCE_SOURCES
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
            present_source = _evidence_source_for_ref(accepted_output_sources, evidence_ref)
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
                    evidence_source=present_source,
                    self_emitted_judgment_not_independent=(
                        judgment_grounding_bars_self_emission and present_source not in _INDEPENDENT_EVIDENCE_SOURCES
                    ),
                    **trace_fields,
                )
            )
            continue
        if match_state == "satisfied" and evidence_ref is not None:
            satisfied_source = _evidence_source_for_ref(accepted_output_sources, evidence_ref)
            requires_independent_evidence = (
                effective_evidence_source == "independent_run_evidence" or _is_judgment_boolean_criterion(criterion)
            )
            if requires_independent_evidence and satisfied_source not in _INDEPENDENT_EVIDENCE_SOURCES:
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id,
                        state="unsatisfied",
                        reason_code=_STRUCTURAL_ABSTENTION_REASON_CODE,
                        evidence_ref=evidence_ref,
                        missing_evidence=(
                            "self-emitted requested output is not independent evidence for this criterion"
                        ),
                        evidence_source=satisfied_source,
                        self_emitted_judgment_not_independent=True,
                        **trace_fields,
                    )
                )
                continue
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="evidence_confirms",
                    evidence_ref=evidence_ref,
                    **trace_fields,
                    evidence_source=_evidence_source_for_ref(accepted_output_sources, evidence_ref),
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
                    evidence_source=_evidence_source_for_ref(accepted_output_sources, evidence_ref),
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


def _carrier_confirmation(
    criterion: CompletionCriterion, path: str, snapshot: RunEvidenceSnapshot
) -> CriterionVerdict | None:
    if _is_judgment_boolean_criterion(criterion):
        return None
    value_text = _carrier_scalar_text(criterion.expected_output_value)
    if value_text is None or len(value_text) < _MIN_CARRIER_VALUE_CHARS:
        return None
    if _carrier_independent_refutation(snapshot, path, criterion.expected_output_value) is not None:
        return None
    trace_fields = _criterion_trace_fields(
        criterion, _criterion_grounding_mode(criterion), criterion.requested_output_evidence_source
    )
    artifact_text = _carrier_surface_text(
        snapshot, _REGISTERED_ARTIFACT_OBSERVATION_LABEL, "registered_artifact_content"
    )
    if artifact_text is not None and _boundary_delimited_present(value_text, artifact_text):
        return CriterionVerdict(
            criterion_id=criterion.id,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref=f"block_outputs:{_REGISTERED_ARTIFACT_OBSERVATION_LABEL}",
            evidence_source="registered_artifact_content",
            **trace_fields,
        )
    page_text = _carrier_surface_text(snapshot, _POST_RUN_PAGE_OBSERVATION_LABEL, "independent_page_evidence")
    if page_text is not None and _boundary_delimited_present(value_text, page_text):
        pre_run_text = snapshot.pre_run_page_reference_text
        if pre_run_text is None:
            return None
        if not _boundary_delimited_present(value_text, _normalized_expected_text(pre_run_text)):
            return CriterionVerdict(
                criterion_id=criterion.id,
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref=f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}",
                evidence_source="independent_page_evidence",
                **trace_fields,
            )
    return None


def _carrier_scalar_text(value: ExpectedOutputValue | None) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        return _normalized_expected_text(value) or None
    return None


def _carrier_surface_text(snapshot: RunEvidenceSnapshot, label: str, expected_source: EvidenceSourceKind) -> str | None:
    if snapshot.block_output_sources.get(label) != expected_source:
        return None
    payload = snapshot.block_outputs.get(label)
    if not isinstance(payload, Mapping):
        return None
    return _normalized_expected_text(page_evidence_prose_text(payload))


def _carrier_independent_refutation(
    snapshot: RunEvidenceSnapshot, path: str, expected_value: ExpectedOutputValue | None
) -> tuple[str, EvidenceSourceKind] | None:
    if not path or expected_value is None:
        return None
    independent_outputs = {
        label: payload
        for label, payload in snapshot.block_outputs.items()
        if snapshot.block_output_sources.get(label) == "independent_page_evidence"
    }
    if not independent_outputs:
        return None
    delegated = _independent_evidence_text_refutation(
        independent_outputs, snapshot.block_output_sources, path, expected_value, set()
    )
    if delegated is not None:
        return delegated
    # Refute when the page packet carries a value AT the requested path that mismatches the
    # expected scalar. The shared refutation only reaches this comparison behind an
    # ``evidence_text`` field the page packet never carries, so it is checked directly here
    # (SKY-11868); a free-text-only contradiction remains unmaskable by design.
    if isinstance(expected_value, bool):
        return None
    parts = _path_parts(path)
    for label, payload in independent_outputs.items():
        if not isinstance(payload, Mapping):
            continue
        values, source_path = _runtime_path_values(payload, parts, path, set())
        if values and not any(_value_matches_expected(value, expected_value) for value in values):
            return f"block_outputs:{label}.{source_path}", "independent_page_evidence"
    return None


def page_evidence_prose_text(evidence: Mapping[str, Any]) -> str:
    parts: list[str] = []
    total = 0
    for key, value in evidence.items():
        if key in _PAGE_EVIDENCE_STAMP_KEYS:
            continue
        for scalar in _iter_prose_scalars(value):
            parts.append(scalar)
            total += len(scalar) + 1
            if total >= _MAX_CARRIER_TEXT_CHARS:
                return " ".join(parts)[:_MAX_CARRIER_TEXT_CHARS]
    return " ".join(parts)


def _iter_prose_scalars(value: Any) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, Mapping):
        collected: list[str] = []
        for item in value.values():
            collected.extend(_iter_prose_scalars(item))
        return collected
    if isinstance(value, list):
        collected = []
        for item in value:
            collected.extend(_iter_prose_scalars(item))
        return collected
    return []


def _boundary_delimited_present(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    length = len(needle)
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return False
        before_ok = idx == 0 or not haystack[idx - 1].isalnum()
        after_index = idx + length
        after_ok = after_index == len(haystack) or not haystack[after_index].isalnum()
        if before_ok and after_ok:
            return True
        start = idx + 1


def _evidence_source_for_ref(
    output_sources: Mapping[str, EvidenceSourceKind],
    evidence_ref: str | None,
) -> EvidenceSourceKind | None:
    label = _evidence_ref_label(evidence_ref)
    return output_sources.get(label) if label else None


def _evidence_ref_label(evidence_ref: str | None) -> str | None:
    if not evidence_ref or not evidence_ref.startswith("block_outputs:"):
        return None
    return evidence_ref.removeprefix("block_outputs:").split(".", 1)[0]


def _independent_evidence_text_refutation(
    block_outputs: Mapping[str, Any],
    output_sources: Mapping[str, EvidenceSourceKind],
    path: str,
    expected_value: ExpectedOutputValue,
    projection_roots: set[str],
) -> tuple[str, EvidenceSourceKind] | None:
    parts = _path_parts(path)
    for label, payload in block_outputs.items():
        source = output_sources.get(label)
        if source != "independent_page_evidence" or not isinstance(payload, Mapping):
            continue
        if isinstance(expected_value, bool):
            values, source_path = _runtime_path_values(payload, parts, path, projection_roots)
            grounded_booleans = [_bool_canonical(value) for value in values]
            if (
                grounded_booleans
                and all(value is not None for value in grounded_booleans)
                and expected_value not in grounded_booleans
            ):
                return f"block_outputs:{label}.{source_path}", source
            continue
        evidence_text = payload.get("evidence_text")
        if not isinstance(evidence_text, str) or not _expected_value_in_text(evidence_text, expected_value):
            continue
        values, _source_path = _runtime_path_values(payload, parts, path, projection_roots)
        if values and not any(_value_matches_expected(value, expected_value) for value in values):
            return f"block_outputs:{label}.evidence_text", source
    return None


def _expected_value_in_text(text: str, expected: str) -> bool:
    observed = _normalized_expected_text(text)
    target = _normalized_expected_text(expected)
    return bool(target) and (target in observed or _compact_expected_text(target) in _compact_expected_text(observed))


def _criterion_grounding_mode(
    criterion: CompletionCriterion,
) -> Literal["exact_value", "shape", "missing", "judgment_boolean"]:
    if _is_judgment_boolean_criterion(criterion):
        return "judgment_boolean"
    if criterion.expected_output_value is not None:
        return "exact_value"
    if criterion.expected_output_shape is not None:
        return "shape"
    return "missing"


def _criterion_trace_fields(
    criterion: CompletionCriterion,
    grounding_mode: str,
    evidence_source: RequestedOutputEvidenceSource | None = None,
) -> dict[str, Any]:
    return {
        "output_path": criterion.output_path,
        "grounding_mode": grounding_mode,
        "expected_output_shape": criterion.expected_output_shape,
        "has_exact_value": criterion.expected_output_value is not None,
        "requested_output_evidence_source": evidence_source or criterion.requested_output_evidence_source,
    }


def _producer_declared_judgment_output_paths(metadata: object) -> set[str]:
    if not isinstance(metadata, Mapping):
        return set()
    paths: set[str] = set()
    for artifact in metadata.values():
        if not isinstance(artifact, Mapping):
            continue
        declared_criteria = artifact.get("completion_criteria")
        if isinstance(declared_criteria, list):
            for declared in declared_criteria:
                if not isinstance(declared, Mapping):
                    continue
                if declared.get("requested_output_evidence_source") != "independent_run_evidence":
                    continue
                normalized = _normalize_output_path(declared.get("output_path"))
                if normalized:
                    paths.add(normalized)
        for row_group in (artifact.get("claimed_outcomes"), artifact.get("terminal_verifier_expectations")):
            rows = [row for row in row_group if isinstance(row, Mapping)] if isinstance(row_group, list) else []
            for row in rows:
                paths.update(_schema_boolean_output_paths(_parse_extraction_schema(row.get("extraction_schema"))))
    return paths


def _schema_boolean_output_paths(schema: Mapping[str, Any] | None, prefix: str = "") -> set[str]:
    if not isinstance(schema, Mapping):
        return set()
    if schema.get("type") == "array":
        items = schema.get("items")
        array_prefix = f"{prefix}[]" if prefix else ""
        return _schema_boolean_output_paths(items, array_prefix) if isinstance(items, Mapping) else set()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        if schema.get("type") == "boolean" and prefix:
            return {_normalize_output_path(prefix)}
        return set()
    paths: set[str] = set()
    for raw_name, child_schema in properties.items():
        name = str(raw_name).strip()
        if not name or not isinstance(child_schema, Mapping):
            continue
        child_prefix = f"{prefix}.{name}" if prefix else name
        if child_schema.get("type") == "boolean":
            paths.add(_normalize_output_path(child_prefix))
        paths.update(_schema_boolean_output_paths(child_schema, child_prefix))
    return paths


def _authored_output_contract_paths_by_label(
    copilot_ctx: _GroundingCtx, aliases: dict[str, str] | None = None
) -> dict[str, set[str]]:
    metadata = copilot_ctx.code_artifact_metadata
    code_by_label = _code_blocks_by_label(copilot_ctx)
    paths_by_label: dict[str, set[str]] = {}
    if isinstance(metadata, Mapping):
        for label, artifact in metadata.items():
            if not isinstance(label, str) or not isinstance(artifact, Mapping):
                continue
            paths = set(_artifact_contract_paths(artifact))
            code = code_by_label.get(label)
            if code:
                paths.update(_static_return_paths(code))
            canonical_paths = _canonical_output_paths(paths, aliases)
            if canonical_paths:
                paths_by_label[label] = canonical_paths
    for label, code in code_by_label.items():
        static_paths = _canonical_output_paths(_static_return_paths(code), aliases)
        if static_paths:
            paths_by_label.setdefault(label, set()).update(static_paths)
    return paths_by_label


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


def _runtime_projection_roots(authored_paths: set[str]) -> set[str]:
    return {parts[0] for path in authored_paths if len(parts := _path_parts(path)) > 1}


def _accepted_runtime_outputs(
    block_outputs: Mapping[str, Any], accepted_labels: set[str], independent_labels: set[str]
) -> list[tuple[str, Any]]:
    if not accepted_labels and not independent_labels:
        return []
    accepted_output_keys = {f"{label}_output" for label in accepted_labels}
    return [
        (label, payload)
        for label, payload in block_outputs.items()
        if label in accepted_labels or label in accepted_output_keys or label in independent_labels
    ]


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
    block_outputs: Mapping[str, Any],
    path: str,
    expected_value: ExpectedOutputValue | None,
    projection_roots: set[str] | None = None,
) -> tuple[str, str | None]:
    parts = _path_parts(path)
    if not parts or expected_value is None:
        return "missing", None
    contradicted_ref: str | None = None
    satisfied_ref: str | None = None
    for label, payload in block_outputs.items():
        values, source_path = _runtime_path_values(payload, parts, path, projection_roots or set())
        if not values:
            continue
        evidence_ref = f"block_outputs:{label}.{source_path}"
        if any(not _value_matches_expected(value, expected_value) for value in values):
            contradicted_ref = contradicted_ref or evidence_ref
            continue
        if any(_value_matches_expected(value, expected_value) for value in values):
            satisfied_ref = satisfied_ref or evidence_ref
    if contradicted_ref is not None:
        return "contradicted", contradicted_ref
    if satisfied_ref is not None:
        return "satisfied", satisfied_ref
    return "missing", None


def _runtime_output_path_presence(
    block_outputs: Mapping[str, Any], path: str, projection_roots: set[str] | None = None
) -> tuple[str, str | None]:
    parts = _path_parts(path)
    if not parts:
        return "missing", None
    for label, payload in block_outputs.items():
        values, source_path = _runtime_present_path_values(payload, parts, path, projection_roots or set())
        if not values:
            continue
        evidence_ref = f"block_outputs:{label}.{source_path}"
        return "present", evidence_ref
    return "missing", None


def _runtime_path_values(
    payload: Any, parts: list[str], path: str, projection_roots: set[str]
) -> tuple[list[Any], str]:
    canonical_values = _path_values(payload, parts)
    if canonical_values:
        filtered_canonical_values = [value for value in canonical_values if _is_meaningful_requested_value(value)]
        return filtered_canonical_values or canonical_values, path
    wrapped_parts = ["output", *parts]
    wrapped_path = ".".join(wrapped_parts)
    wrapped_values = [value for value in _path_values(payload, wrapped_parts) if _is_meaningful_requested_value(value)]
    if wrapped_values:
        return wrapped_values, wrapped_path
    for root in sorted(projection_roots):
        projected_parts = [root, *parts]
        projected_path = ".".join(projected_parts)
        projected_values = [
            value for value in _path_values(payload, projected_parts) if _is_meaningful_requested_value(value)
        ]
        if projected_values:
            return projected_values, projected_path
    return [], wrapped_path


def _runtime_present_path_values(
    payload: Any, parts: list[str], path: str, projection_roots: set[str]
) -> tuple[list[Any], str]:
    raw_canonical_values = _path_values(payload, parts)
    if raw_canonical_values:
        canonical_values = [value for value in raw_canonical_values if _is_meaningful_requested_value(value)]
        return canonical_values, path
    wrapped_parts = ["output", *parts]
    wrapped_path = ".".join(wrapped_parts)
    wrapped_values = [value for value in _path_values(payload, wrapped_parts) if _is_meaningful_requested_value(value)]
    if wrapped_values:
        return wrapped_values, wrapped_path
    for root in sorted(projection_roots):
        projected_parts = [root, *parts]
        projected_path = ".".join(projected_parts)
        projected_values = [
            value for value in _path_values(payload, projected_parts) if _is_meaningful_requested_value(value)
        ]
        if projected_values:
            return projected_values, projected_path
    return [], wrapped_path


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


def _bool_canonical(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        collapsed = value.strip().casefold()
        if collapsed == "true":
            return True
        if collapsed == "false":
            return False
    return None


def _value_matches_expected(value: Any, expected: ExpectedOutputValue) -> bool:
    if isinstance(expected, bool):
        canonical = _bool_canonical(value)
        return canonical is not None and canonical == expected
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
