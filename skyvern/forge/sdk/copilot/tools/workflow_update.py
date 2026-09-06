from __future__ import annotations

import ast
import copy
import hashlib
import json
import keyword
import os
import re
import textwrap
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Any, Literal, NamedTuple, cast

import structlog
import yaml
from jinja2 import TemplateSyntaxError
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.copilot.author_time_block import (
    BANNED_BLOCKS_BLOCK_ID,
    CODE_SAFETY_BLOCK_ID,
    CREDENTIAL_SCOUT_BLOCK_ID,
    AuthorTimeBlock,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    clear_active_run_evidence_on_workflow_edit,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestOutcomeReasonCode,
    CodeSafetyRejectionFact,
    RecordedBuildTestOutcome,
    authored_structure_signature_from_workflow,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
)
from skyvern.forge.sdk.copilot.code_block_preflight import (
    advisory_code_block_diagnostics,
    scanner_advisory_diagnostics,
)
from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError, author_time_code_security_errors
from skyvern.forge.sdk.copilot.code_block_steps import bind_referenced_parameters_in_yaml
from skyvern.forge.sdk.copilot.code_block_synthesis import wrapped_code_ast as _wrapped_code_ast
from skyvern.forge.sdk.copilot.code_write_diff import CodeWriteDiff, build_code_write_diffs
from skyvern.forge.sdk.copilot.completion_verification import grade_definition_criteria
from skyvern.forge.sdk.copilot.composition_evidence import (
    normalize_block_observation_refs,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import (
    CodeAuthoringRepairContext,
    CopilotContext,
)
from skyvern.forge.sdk.copilot.credential_fill_fields import CredentialFillField
from skyvern.forge.sdk.copilot.google_connection_notice import (
    collect_google_connection_notices,
    google_sheet_connection_bindings,
    retain_notices_after_lookup_failure,
    write_google_connection_notice_capture,
)
from skyvern.forge.sdk.copilot.narration import CODE_REPAIR_PROGRESS_SURFACE_KIND, CODE_REPAIR_PROGRESS_TEXT
from skyvern.forge.sdk.copilot.output_contracts import (
    declared_string_workflow_parameter_keys,
    declared_workflow_parameter_keys,
)
from skyvern.forge.sdk.copilot.output_policy import (
    demote_author_time_steer_reasons,
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
)
from skyvern.forge.sdk.copilot.output_utils import INTERNAL_VALIDATION_FAILURE_PREFIX
from skyvern.forge.sdk.copilot.reached_download_target import (
    REGISTERED_DOWNLOAD_OUTPUT_KEYS,
    code_uses_download_claim,
)
from skyvern.forge.sdk.copilot.request_policy import (
    REQUESTED_OUTPUT_PATH_MINT_SOURCES,
    CompletionCriterion,
    JudgmentPredicate,
    RequestedOutputEvidenceSource,
    _coerce_requested_output_evidence_source,
    _is_judgment_boolean_criterion,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    ScoutedInteraction,
)
from skyvern.forge.sdk.copilot.schema_incompatibility import (
    SCHEMA_INCOMPATIBILITY_REASON_CODE,
    SchemaIncompatibility,
    merge_schema_incompatibilities,
    render_schema_incompatibility_agent_steer,
    render_schema_incompatibility_user_reason,
)
from skyvern.forge.sdk.copilot.secret_scrub import registered_scrub_values, scrub_secrets_from_text
from skyvern.forge.sdk.copilot.streaming_adapter import emit_workflow_draft, maybe_emit_design_end
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    parse_workflow_yaml,
    workflow_blocks,
)
from skyvern.forge.sdk.copilot.workflow_yaml import (
    _normalize_copilot_yaml,
    _process_workflow_yaml,
    dump_workflow_yaml,
    reconcile_workflow_completion_contract,
    redact_credentials_in_workflow_yaml,
    runner_code_block_associations,
)
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.runtime_completion import contract_from_code_artifact_metadata
from skyvern.schemas.proxy_location import runtime_proxy_location
from skyvern.schemas.workflows import BlockType
from skyvern.utils.templating import get_missing_variables
from skyvern.utils.url_validators import validate_webhook_url

from ._shared import (
    _enum_or_string_name,
    _proxy_location_trace_value,
    _raw_yaml_proxy_location,
)
from .banned_blocks import (
    _banned_block_reject_message,
    _copilot_banned_block_types,
    _copilot_block_authoring_policy,
    _detect_new_banned_blocks,
    _record_banned_block_reject_span,
    _task_v3_pure_policy_violations,
    _task_v3_pure_reject_message,
)
from .credentials import (
    _credential_id_misbinding_findings,
    _credential_reference_validation_error,
    canonicalize_named_google_sheet_bindings,
)
from .frontier import (
    _get_prior_workflow,
    _invalidate_verified_state_on_edit,
)
from .guardrails import _authority_tool_error

LOG = structlog.get_logger()


class BlockObservationRef(BaseModel):
    label: str
    observation_step: Annotated[int, Field(ge=0)]


ArtifactEvidenceStatus = Literal["satisfied", "missing", "diagnostic_only", "observed_not_verified"]
ExtractionSchemaProvenance = Literal["self_authored", "user_edited"]
SelectedExtractionMetadataDisposition = Literal[
    "none",
    "self_authored_extraction_preserved",
    "sibling_or_suffix_extraction_preserved",
]


class CodeArtifactClaimedOutcome(BaseModel):
    id: str = ""
    scope: str = ""
    text: str = ""
    status: ArtifactEvidenceStatus = "observed_not_verified"
    depends_on: list[str] = Field(default_factory=list, description="Page-dependency ids this claim relies on.")
    covered_criteria: list[str] = Field(default_factory=list, description="Completion-criterion ids this claim covers.")
    criteria_ids: list[str] = Field(default_factory=list)
    goal_value_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Output JSON paths that carry the goal values for this claim, for example "
            "`records[].number` or `records[].expiration_date`."
        ),
    )
    extraction_schema: str | None = Field(
        default=None,
        description=(
            "JSON Schema the user confirmed for this claim's extraction shape, serialized as a JSON "
            'string (an object, or `{"type":"array","items":{...}}` for repeated records). Named '
            "fields, types, and nesting the `goal_value_paths` index into; the block return is conformed "
            "and validated against it. Same dialect as the legacy `data_schema` lever."
        ),
    )
    extraction_schema_provenance: ExtractionSchemaProvenance | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    observation_refs: list[str] = Field(default_factory=list)
    required_tokens: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)


class CodeArtifactPageDependency(BaseModel):
    id: str = ""
    scope: str = ""
    status: ArtifactEvidenceStatus = "observed_not_verified"
    url_hint: str | None = None
    page_state_hint: str | None = None
    required_affordances: list[str] = Field(default_factory=list)
    required_outcomes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, description="Dependency-scoped evidence_ref ids.")
    observation_refs: list[str] = Field(default_factory=list, description="Dependency-scoped observation_ref ids.")


class CodeArtifactCompletionCriterion(BaseModel):
    id: str = ""
    text: str = ""
    level: Literal["terminal", "outcome", "prefix", "method"] = "terminal"
    outcome: str | None = None
    terminal: bool | None = None
    output_path: str | None = None
    requested_output_evidence_source: RequestedOutputEvidenceSource | None = None
    deliverable_kind: Literal["registered_download"] | None = Field(
        default=None,
        description=(
            "Set to `registered_download` only when this criterion promises the user a downloaded file. "
            "Run finalization then verifies execution-layer file registration; authored code output cannot satisfy it."
        ),
    )
    judgment_predicate: JudgmentPredicate | None = Field(
        default=None,
        description=(
            "For a judgment-boolean criterion, the closed-vocabulary page-evidence predicate the "
            "independent post-run packet decides this boolean by (e.g. `login_gate_blocks_target`)."
        ),
    )
    judgment_polarity_when_holds: bool | None = Field(
        default=None,
        description="The emitted boolean value that corresponds to `judgment_predicate` holding on the packet.",
    )


class CodeArtifactScopedRef(BaseModel):
    claim_id: str | None = None
    dependency_id: str | None = None
    criterion_id: str | None = None
    evidence_ref: str | None = None
    observation_ref: str | None = None
    status: ArtifactEvidenceStatus = Field(
        default="observed_not_verified", validation_alias=AliasChoices("status", "evidence_status")
    )
    source_tool: str | None = None
    observation_step: Annotated[int, Field(ge=0)] | None = None
    run_sample_id: str | None = None
    current_url: str | None = None
    source_label: str | None = None
    checkpoint_next_mode: Literal["advance", "stop"] | None = None


class CodeArtifactTerminalVerifierExpectation(BaseModel):
    id: str = ""
    text: str = ""
    criteria_ids: list[str] = Field(default_factory=list)
    claimed_outcome_ids: list[str] = Field(default_factory=list)
    goal_value_paths: list[str] = Field(
        default_factory=list,
        description="Output JSON paths terminal verification should treat as goal-value evidence.",
    )
    extraction_schema: str | None = Field(
        default=None,
        description="JSON Schema (serialized JSON string) of the confirmed extraction shape terminal verification expects.",
    )
    extraction_schema_provenance: ExtractionSchemaProvenance | None = None


class CodeArtifactExplorationObservation(BaseModel):
    id: str = ""
    text: str = ""
    status: Literal["observed_not_verified"] = Field(
        default="observed_not_verified",
        validation_alias=AliasChoices("status", "evidence_status"),
    )
    observation_ref: str | None = None
    source_tool: str | None = None
    observation_step: Annotated[int, Field(ge=0)] | None = None
    current_url: str | None = None
    source_label: str | None = None
    checkpoint_next_mode: Literal["advance", "stop"] | None = None


class CodeArtifactInputBinding(BaseModel):
    """An explicit model-owned workflow-key to factual scout-input binding."""

    parameter_key: str
    input_id: str | None = None
    credential_id: str | None = None
    credential_field: CredentialFillField | None = None


class CodeArtifactMetadata(BaseModel):
    artifact_id: str | None = Field(
        default=None,
        description="Stable artifact id supplied by the author; never generated or replaced by the server.",
    )
    block_label: str | None = Field(
        default=None, description="Label of the authored `code` block this artifact describes."
    )
    block_id: str | None = None
    declared_goal: str = Field(default="", description="The durable goal this block accomplishes; model-owned.")
    claimed_outcomes: list[CodeArtifactClaimedOutcome] = Field(
        default_factory=list,
        description=(
            "Outcomes this block claims. Each claim links `depends_on` page-dependency ids, covered "
            "criterion ids, and claim-scoped observation/evidence refs; a `satisfied` claim additionally "
            "requires claim-scoped `evidence_refs`. Every link is supplied by the author."
        ),
    )
    page_dependencies: list[CodeArtifactPageDependency] = Field(
        default_factory=list,
        description=(
            "Pages or states the code depends on; non-`missing` rows carry scoped evidence or observation refs."
        ),
    )
    completion_criteria: list[CodeArtifactCompletionCriterion] = Field(
        default_factory=list,
        description="Completion criteria; include at least one `terminal` criterion.",
    )
    evidence_refs: list[CodeArtifactScopedRef] = Field(
        default_factory=list,
        description=(
            "Artifact-level refs: each entry carries its ref id, a scoped id (claim/dependency/criterion), "
            "and `source_tool` unless status is `missing`."
        ),
    )
    observation_refs: list[CodeArtifactScopedRef] = Field(
        default_factory=list,
        description=("Artifact-level observation refs; same shape rules as `evidence_refs`."),
    )
    terminal_verifier_expectations: list[CodeArtifactTerminalVerifierExpectation] = Field(
        default_factory=list,
        description="What terminal verification must observe; link `criteria_ids` or `claimed_outcome_ids`.",
    )
    exploration_observations: list[CodeArtifactExplorationObservation] = Field(
        default_factory=list,
        description="Scout-time observations; status stays `observed_not_verified` until verification passes.",
    )
    input_bindings: list[CodeArtifactInputBinding] = Field(
        default_factory=list,
        description=(
            "Explicit parameter-key bindings to an ordinary same-turn input_id or an exact "
            "credential_id/credential_field identity from the scout facts."
        ),
    )


_CODE_ARTIFACT_REQUIRED_LIST_FIELDS = (
    "claimed_outcomes",
    "page_dependencies",
    "completion_criteria",
    "terminal_verifier_expectations",
)


def _code_artifact_metadata_as_tool_argument(
    metadata: list[CodeArtifactMetadata] | None,
) -> list[dict[str, Any]]:
    if not metadata:
        return []
    return [item.model_dump(mode="json", exclude_none=True) for item in metadata]


def _format_code_artifact_violations(violations: list[str]) -> str:
    # Surface every contract violation at once so the agent fixes them in a single
    # update instead of round-tripping one error per `update_and_run_blocks` call.
    if len(violations) == 1:
        return violations[0]
    numbered = "\n".join(f"{index}. {message}" for index, message in enumerate(violations, start=1))
    return f"Artifact metadata has {len(violations)} contract violations; fix all of them in one update:\n{numbered}"


def _code_artifact_validation_error_message(exc: ValidationError) -> str:
    # Build from loc/type only; pydantic's str(exc) embeds input_value, which would
    # carry submitted metadata values onto the scrubbing-exempt durable span.
    parts = [
        f"{'.'.join(str(loc) for loc in err.get('loc', ()))}: {err.get('type', 'invalid')}" for err in exc.errors()
    ]
    detail = "; ".join(part for part in parts if part) or "schema validation failed"
    return f"Artifact metadata is malformed ({detail})."


class CodeArtifactNormalization(NamedTuple):
    normalized: dict[str, dict[str, Any]]
    error: str | None
    violations: list[str]
    offending_labels: list[str]
    schema_incompatibilities: list[SchemaIncompatibility] = []


def _normalize_code_artifact_metadata(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    scout_trajectory: list[ScoutedInteraction] | None = None,
    verified_runtime_output_paths_by_label: Mapping[str, set[str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    result = _normalize_code_artifact_metadata_detailed(
        raw_metadata,
        workflow_yaml,
        scout_trajectory=scout_trajectory,
        verified_runtime_output_paths_by_label=verified_runtime_output_paths_by_label,
    )
    return result.normalized, result.error


def _normalize_code_artifact_metadata_detailed(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    scout_trajectory: list[ScoutedInteraction] | None = None,
    verified_runtime_output_paths_by_label: Mapping[str, set[str]] | None = None,
) -> CodeArtifactNormalization:
    """Normalize submitted artifact metadata at the persist seam.

    Submitted metadata is validated without re-keying entries or minting identities or fields.
    Returns the per-violation list and offending labels alongside the batched error for telemetry."""
    if raw_metadata in (None, [], {}):
        return CodeArtifactNormalization({}, None, [], [])
    items = _code_artifact_metadata_items(raw_metadata)
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    violations: list[str] = []
    offending_labels: list[str] = []
    schema_incompatibilities: list[SchemaIncompatibility] = []
    anchored: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for raw_item in items:
        try:
            metadata = (
                raw_item
                if isinstance(raw_item, CodeArtifactMetadata)
                else CodeArtifactMetadata.model_validate(raw_item)
            )
        except ValidationError as exc:
            violations.append(_code_artifact_validation_error_message(exc))
            continue
        dumped = metadata.model_dump(mode="json", exclude_none=True)
        label = str(dumped.get("block_label") or "").strip()
        if not label or label not in code_blocks:
            violations.append(
                "Artifact metadata requires an exact `block_label` that names a submitted code block"
                + (f"; `{label}` is not present." if label else ".")
            )
            continue
        if label in seen_labels:
            violations.append(f"Artifact metadata for `{label}` is duplicated; submit exactly one row.")
            continue
        seen_labels.add(label)
        dumped["block_label"] = label
        anchored.append(dumped)

    normalized: dict[str, dict[str, Any]] = {}
    parsed_workflow = parse_workflow_yaml(workflow_yaml)
    declared_parameter_keys = (
        declared_workflow_parameter_keys(parsed_workflow) if isinstance(parsed_workflow, Mapping) else set()
    )
    for dumped in anchored:
        label = str(dumped["block_label"])
        artifact_id = str(dumped.get("artifact_id") or "").strip()
        identity_violations: list[str] = []
        if not artifact_id.startswith("code_artifact:"):
            identity_violations.append(
                f"Artifact metadata for `{label}` requires an explicit `artifact_id` beginning with `code_artifact:`."
            )
        submitted_block_id = str(dumped.get("block_id") or "").strip()
        workflow_block_id = str(code_blocks[label].get("block_id") or code_blocks[label].get("id") or "").strip()
        if submitted_block_id and workflow_block_id and submitted_block_id != workflow_block_id:
            identity_violations.append(
                f"Artifact metadata for `{label}` has `block_id` `{submitted_block_id}` which does not match the submitted block."
            )
        item_violations: list[str] = list(identity_violations)
        declared_goal = str(dumped.get("declared_goal") or "").strip()
        if not declared_goal:
            item_violations.append(f"Artifact metadata for `{label}` requires a non-empty `declared_goal`.")
        for field_name in _CODE_ARTIFACT_REQUIRED_LIST_FIELDS:
            value = dumped.get(field_name)
            if not isinstance(value, list) or not value:
                item_violations.append(f"Artifact metadata for `{label}` requires non-empty `{field_name}`.")
        if not dumped.get("evidence_refs") and not dumped.get("observation_refs"):
            item_violations.append(f"Artifact metadata for `{label}` requires `evidence_refs` or `observation_refs`.")
        raw_block_parameter_keys = code_blocks[label].get("parameter_keys")
        block_parameter_keys = (
            {str(key).strip() for key in raw_block_parameter_keys if isinstance(key, str) and str(key).strip()}
            if isinstance(raw_block_parameter_keys, list)
            else set()
        )
        item_violations.extend(
            _input_binding_violations(
                block_label=label,
                bindings=dumped.get("input_bindings"),
                declared_parameter_keys=declared_parameter_keys,
                block_parameter_keys=block_parameter_keys,
                scout_trajectory=scout_trajectory or [],
            )
        )
        item_violations.extend(
            _code_artifact_metadata_shape_errors(
                label,
                dumped,
                reject_unfilled_goal_value_paths=False,
            )
        )
        block_code = str(code_blocks[label].get("code") or "")
        return_shape_error = _extraction_return_shape_error(
            label,
            dumped,
            block_code,
            require_declared_output=False,
        )
        if return_shape_error is not None:
            item_violations.append(return_shape_error)
        schema_conformance_error = _extraction_schema_conformance_error(label, dumped, block_code)
        if schema_conformance_error is not None:
            item_violations.append(schema_conformance_error)
        schema_incompatibility = _extraction_schema_incompatibility(
            label,
            dumped,
            block_code,
            verified_runtime_output_paths=(
                verified_runtime_output_paths_by_label.get(label) if verified_runtime_output_paths_by_label else None
            ),
        )
        if schema_incompatibility is not None:
            schema_incompatibilities.append(schema_incompatibility)
            item_violations.append(render_schema_incompatibility_agent_steer(schema_incompatibility))
        descriptor_leak = _download_descriptor_leak_finding(label, block_code)
        if descriptor_leak is not None:
            item_violations.append(descriptor_leak)
        if item_violations:
            violations.extend(item_violations)
            offending_labels.append(label)
            continue
        normalized[label] = dumped
    if violations:
        return CodeArtifactNormalization(
            normalized,
            _format_code_artifact_violations(violations),
            violations,
            offending_labels,
            schema_incompatibilities,
        )
    return CodeArtifactNormalization(normalized, None, [], [], [])


def _input_binding_violations(
    *,
    block_label: str,
    bindings: object,
    declared_parameter_keys: set[str],
    block_parameter_keys: set[str],
    scout_trajectory: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate model-submitted identities exactly; never infer or repair a binding."""
    if not isinstance(bindings, list):
        return [f"Artifact metadata for `{block_label}` has malformed `input_bindings`."]
    # Carried entries are a prior turn's history seeded into the trajectory. Their identity
    # crosses so the model can read it, but the private value it names does not, so a binding
    # on one resolves to nothing at dispatch and the run reports success on a value nobody
    # demonstrated. Same-turn means same turn.
    ordinary_ids = {
        str(interaction.get("input_id") or "").strip()
        for interaction in scout_trajectory
        if str(interaction.get("input_id") or "").strip() and interaction.get("carried") is not True
    }
    credential_identities = {
        (
            str(interaction.get("credential_id") or "").strip(),
            str(interaction.get("credential_field") or "").strip(),
        )
        for interaction in scout_trajectory
        if str(interaction.get("credential_id") or "").strip()
        and str(interaction.get("credential_field") or "").strip()
    }
    violations: list[str] = []
    seen_parameter_keys: set[str] = set()
    for raw_binding in bindings:
        if not isinstance(raw_binding, Mapping):
            violations.append(f"Artifact metadata for `{block_label}` contains a malformed input binding.")
            continue
        parameter_key = str(raw_binding.get("parameter_key") or "").strip()
        input_id = str(raw_binding.get("input_id") or "").strip()
        credential_id = str(raw_binding.get("credential_id") or "").strip()
        credential_field = str(raw_binding.get("credential_field") or "").strip()
        has_ordinary_identity = bool(input_id)
        has_credential_identity = bool(credential_id or credential_field)
        if not parameter_key:
            violations.append(f"Artifact metadata for `{block_label}` has an input binding without `parameter_key`.")
            continue
        if parameter_key in seen_parameter_keys:
            violations.append(
                f"Artifact metadata for `{block_label}` binds parameter `{parameter_key}` more than once."
            )
        seen_parameter_keys.add(parameter_key)
        if parameter_key not in declared_parameter_keys:
            violations.append(
                f"Input binding parameter `{parameter_key}` for `{block_label}` is not declared in workflow parameters."
            )
        if parameter_key not in block_parameter_keys:
            violations.append(
                f"Input binding parameter `{parameter_key}` for `{block_label}` is not present in that block's parameter_keys."
            )
        if has_ordinary_identity == has_credential_identity:
            violations.append(
                f"Input binding for `{parameter_key}` must name exactly one input_id or credential identity."
            )
            continue
        if has_ordinary_identity and input_id not in ordinary_ids:
            violations.append(
                f"Input identity `{input_id}` for `{parameter_key}` is not present in same-turn scout facts."
            )
        if has_credential_identity:
            if not credential_id or not credential_field:
                violations.append(
                    f"Credential binding for `{parameter_key}` requires both credential_id and credential_field."
                )
            elif (credential_id, credential_field) not in credential_identities:
                violations.append(
                    f"Credential identity `{credential_id}` / `{credential_field}` for `{parameter_key}` is not present in same-turn scout facts."
                )
    return violations


def _artifact_mutable_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _requested_output_path_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or path == "$":
        return None
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$["):
        path = path[1:]
    if not path.startswith("output."):
        path = f"output.{path}"
    return path


def _metadata_requested_output_evidence_sources(
    code_artifact_metadata: object,
) -> dict[str, RequestedOutputEvidenceSource]:
    metadata = code_artifact_metadata if isinstance(code_artifact_metadata, Mapping) else {}
    sources: dict[str, RequestedOutputEvidenceSource] = {}
    for artifact in metadata.values():
        if not isinstance(artifact, Mapping):
            continue
        for criterion in _artifact_rows(artifact.get("completion_criteria")):
            if "requested_output_evidence_source" not in criterion:
                continue
            source = _coerce_requested_output_evidence_source(criterion.get("requested_output_evidence_source"))
            if source == "runtime_output":
                continue
            output_path = _requested_output_path_key(criterion.get("output_path"))
            if output_path:
                sources.setdefault(output_path, source)
    return sources


def _apply_code_artifact_requested_output_evidence_sources(ctx: AgentContext, code_artifact_metadata: object) -> None:
    sources = _metadata_requested_output_evidence_sources(code_artifact_metadata)
    if not sources:
        return
    policy = getattr(ctx, "request_policy", None)
    if policy is None:
        return
    criteria = getattr(policy, "completion_criteria", None)
    if not isinstance(criteria, list):
        return
    updated_criteria = []
    for criterion in criteria:
        output_path = _requested_output_path_key(getattr(criterion, "output_path", None))
        if output_path in sources:
            updated_criteria.append(replace(criterion, requested_output_evidence_source=sources[output_path]))
        else:
            updated_criteria.append(criterion)
    policy.completion_criteria = updated_criteria


def _code_artifact_metadata_items(raw_metadata: Any) -> list[Any]:
    if isinstance(raw_metadata, Mapping):
        items: list[Any] = []
        for block_label, value in raw_metadata.items():
            if isinstance(value, Mapping) and "block_label" not in value:
                items.append({"block_label": block_label, **value})
            else:
                items.append(value)
        return items
    if isinstance(raw_metadata, list):
        return raw_metadata
    return [raw_metadata]


def _workflow_yaml_code_blocks_by_label(workflow_yaml: str | None) -> dict[str, Mapping[str, Any]]:
    if workflow_yaml is None:
        return {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return {}
    blocks: dict[str, Mapping[str, Any]] = {}
    for block in workflow_blocks(parsed):
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
            label = block.get("label")
            if isinstance(label, str) and label:
                blocks[label] = block
    return blocks


# Headroom under enforcement._RECENT_TOOL_OUTPUT_CHAR_CAP: a result past that cap is head-truncated,
# which would leave the model a sliced, unparseable JSON payload instead of code it can re-anchor on.
_MAX_STORED_CODE_CHARS = 30_000


def _withheld_labels_within(labels: list[str], budget: int) -> list[str]:
    for shown_count in range(len(labels), 0, -1):
        shown = labels[:shown_count]
        if shown_count < len(labels):
            shown = shown + [f"... and {len(labels) - shown_count} more"]
        if len(json.dumps(shown)) <= budget:
            return shown
    exhausted = [f"... and {len(labels)} more"]
    return exhausted if len(json.dumps(exhausted)) <= budget else []


def _changed_code_blocks(prior_yaml: str | None, submitted_yaml: str, accepted_yaml: str) -> dict[str, str]:
    prior = _workflow_yaml_code_blocks_by_label(prior_yaml)
    submitted = _workflow_yaml_code_blocks_by_label(submitted_yaml)
    accepted = _workflow_yaml_code_blocks_by_label(accepted_yaml)
    changed: dict[str, str] = {}
    for label, block in accepted.items():
        code = block.get("code")
        if not isinstance(code, str):
            continue
        prior_block = prior.get(label)
        submitted_block = submitted.get(label)
        unchanged_since_prior = prior_block is not None and prior_block.get("code") == code
        matches_submission = submitted_block is None or submitted_block.get("code") == code
        if unchanged_since_prior and matches_submission:
            continue
        changed[label] = code
    return changed


def _advisory_labels_by_message(changed_code_blocks: Mapping[str, str]) -> dict[str, list[str]]:
    """Labels per advisory message, computed from every changed block rather than the
    budget-truncated ``stored_code`` so an oversized block still gets its note."""
    labels_by_message: dict[str, list[str]] = {}
    for label, code in sorted(changed_code_blocks.items()):
        for diagnostic in advisory_code_block_diagnostics(code):
            labels = labels_by_message.setdefault(diagnostic.message, [])
            if label not in labels:
                labels.append(label)
    return labels_by_message


async def _scanner_advisory_labels_by_message(
    changed_code_blocks: Mapping[str, str], organization_id: str | None
) -> dict[str, list[str]]:
    """Labels per scanner-advisory message; each scan is bounded and fail-open inside
    ``scanner_advisory_diagnostics``, so this never delays or fails the update."""
    labels_by_message: dict[str, list[str]] = {}
    for label, code in sorted(changed_code_blocks.items()):
        for diagnostic in await scanner_advisory_diagnostics(code, organization_id=organization_id):
            labels = labels_by_message.setdefault(diagnostic.message, [])
            if label not in labels:
                labels.append(label)
    return labels_by_message


def _accepted_code_delta(changed: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Code blocks the accepted submission changed, as stored after server-side rewrites, paired
    with the labels that did not fit the budget so an omission is named rather than silent. A block
    the server did not rewrite is returned too, because the anchor has to survive context
    compaction and a payload that carried only rewrites would make its own absence ambiguous."""
    if not changed:
        return {}, []

    ordered = sorted(changed, key=lambda label: len(changed[label]))
    stored: dict[str, str] = {}
    for index, label in enumerate(ordered):
        candidate = {**stored, label: changed[label]}
        remaining = ordered[index + 1 :]
        if len(json.dumps(candidate)) + len(json.dumps(remaining)) > _MAX_STORED_CODE_CHARS:
            break
        stored = candidate
    withheld = [label for label in ordered if label not in stored]
    if not withheld:
        return stored, []
    return stored, _withheld_labels_within(withheld, _MAX_STORED_CODE_CHARS - len(json.dumps(stored)))


def _code_block_safety_errors(workflow_yaml: str | None, prior_yaml: str | None) -> list[str | CodeBlockSecurityError]:
    """Run the sandbox's static safety rule on new/changed code blocks before any run.

    Label-scoped diff so legacy code blocks the model did not touch cannot wedge
    every subsequent update."""
    prior_blocks = _workflow_yaml_code_blocks_by_label(prior_yaml)
    workflow_blocks_by_label = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    errors: list[str | CodeBlockSecurityError] = []
    for label, block in workflow_blocks_by_label.items():
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        prior_block = prior_blocks.get(label)
        parameter_keys = _code_block_parameter_keys(block)
        if (
            prior_block is not None
            and str(prior_block.get("code") or "") == code
            and _code_block_parameter_keys(prior_block) == parameter_keys
        ):
            continue
        try:
            ast.parse(code)
        except SyntaxError as exc:
            errors.append(f"Code block `{label}` is not valid Python: {exc}")
            continue
        try:
            CodeBlock.is_safe_code(code)
        except SyntaxError as exc:
            errors.append(f"Code block `{label}` is not valid Python: {exc}")
            continue
        except InsecureCodeDetected as exc:
            errors.append(
                f"Code block `{label}` failed the sandbox safety check: {exc}. Rewrite without import "
                "statements, dunder access, or private attributes; the sandbox provides `page`, declared "
                "code-block parameter keys, `json`, `re`, `html`, `asyncio.sleep`, and its explicit safe helper "
                "namespace."
            )
        errors.extend(author_time_code_security_errors(label=label, code=code))
    return errors


def _human_facing_code_safety_errors(errors: list[str | CodeBlockSecurityError]) -> list[str | CodeBlockSecurityError]:
    preflight_reason_codes = {
        reason_code for error in errors if (reason_code := _generated_code_preflight_reason_code(error)) is not None
    }
    if not preflight_reason_codes:
        return errors
    return [
        error
        for error in errors
        if not isinstance(error, CodeBlockSecurityError) or error.reason_code not in preflight_reason_codes
    ]


def _generated_code_preflight_reason_code(error: str | CodeBlockSecurityError) -> str | None:
    if isinstance(error, CodeBlockSecurityError):
        return None
    marker = "failed the generated-code preflight check: "
    if marker not in error:
        return None
    detail = error.split(marker, 1)[1]
    reason_code = detail.split(":", 1)[0]
    if not reason_code.startswith("AUTHOR_PAGE_"):
        return None
    return reason_code


def _declared_string_workflow_parameter_keys(parsed: Mapping[str, Any]) -> set[str]:
    return declared_string_workflow_parameter_keys(parsed)


def _clear_code_authoring_repair_context(ctx: AgentContext) -> None:
    ctx.last_code_authoring_repair_context = None


def _record_author_time_reject_outcome(
    ctx: AgentContext,
    *,
    reason_code: BuildTestOutcomeReasonCode,
    summary: str,
    structural_payload: Mapping[str, object] | None = None,
    authored_structure_signature: str | None = None,
    block_labels: list[str] | None = None,
    missing_requested_output_facts: list[dict[str, object]] | None = None,
    code_safety_rejection_facts: list[CodeSafetyRejectionFact] | None = None,
) -> None:
    record_build_test_outcome(
        ctx,
        _build_author_time_reject_outcome(
            ctx,
            reason_code=reason_code,
            summary=summary,
            structural_payload=structural_payload,
            authored_structure_signature=authored_structure_signature,
            block_labels=block_labels,
            missing_requested_output_facts=missing_requested_output_facts,
            code_safety_rejection_facts=code_safety_rejection_facts,
        ),
    )


def _build_author_time_reject_outcome(
    ctx: AgentContext,
    *,
    reason_code: BuildTestOutcomeReasonCode,
    summary: str,
    structural_payload: Mapping[str, object] | None = None,
    authored_structure_signature: str | None = None,
    block_labels: list[str] | None = None,
    missing_requested_output_facts: list[dict[str, object]] | None = None,
    code_safety_rejection_facts: list[CodeSafetyRejectionFact] | None = None,
) -> RecordedBuildTestOutcome:
    prior_outcome = ctx.latest_recorded_build_test_outcome
    observed_page_value_excerpt = (
        prior_outcome.observed_page_value_excerpt if isinstance(prior_outcome, RecordedBuildTestOutcome) else ""
    )
    return recorded_outcome_from_author_time_reject(
        reason_code=reason_code,
        block_labels=block_labels or [],
        structural_payload=structural_payload,
        authored_structure_signature=authored_structure_signature,
        observed_evidence_summary=summary,
        observed_page_value_excerpt=observed_page_value_excerpt,
        missing_requested_output_facts=missing_requested_output_facts or [],
        code_safety_rejection_facts=code_safety_rejection_facts or [],
    )


def _code_safety_rejection_facts(
    errors: list[str | CodeBlockSecurityError],
    *,
    submission_ref: str | None,
) -> list[CodeSafetyRejectionFact]:
    facts: list[CodeSafetyRejectionFact] = []
    for error in errors:
        if isinstance(error, CodeBlockSecurityError):
            facts.append(
                CodeSafetyRejectionFact(
                    block_label=error.block_label,
                    reason_code=error.reason_code,
                    surface=error.surface,
                    submission_ref=submission_ref or "",
                )
            )
    return facts


def _code_safety_reject_payload(facts: list[CodeSafetyRejectionFact]) -> dict[str, list[dict[str, str]]] | None:
    if not facts:
        return None
    return {
        "code_safety_errors": [
            {
                "block_label": fact.block_label,
                "reason_code": fact.reason_code,
                "surface": fact.surface,
            }
            for fact in facts
        ]
    }


def _metadata_item_extraction_schema_paths(item: Mapping[str, Any]) -> set[str]:
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            schema = _parse_extraction_schema(row.get("extraction_schema"))
            if schema is not None:
                return _schema_property_paths(schema)
    return set()


def _schema_property_roots(schema: Mapping[str, object]) -> set[str]:
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        return {str(key) for key in properties if str(key)}
    items = schema.get("items")
    if isinstance(items, Mapping):
        return _schema_property_roots(items)
    return set()


def _schema_property_paths(schema: Mapping[str, object], *, prefix: str = "") -> set[str]:
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        paths: set[str] = set()
        for raw_key, child in properties.items():
            key = str(raw_key).strip()
            if not key:
                continue
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            if isinstance(child, Mapping):
                paths.update(_schema_property_paths(child, prefix=path))
        return paths
    items = schema.get("items")
    if isinstance(items, Mapping):
        array_prefix = f"{prefix}[]" if prefix else ""
        return _schema_property_paths(items, prefix=array_prefix)
    return set()


def _requested_output_child_paths(ctx: AgentContext) -> set[str]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return set()
    paths: set[str] = set()
    # Criteria at this seam are polymorphic (typed CompletionCriterion plus lighter duck-typed
    # shapes), so read fields via getattr — a non-model criterion must still contribute its path.
    for criterion in _active_completion_criteria(ctx):
        if isinstance(criterion, CompletionCriterion) and _is_judgment_boolean_criterion(criterion):
            continue
        if isinstance(criterion, CompletionCriterion) and criterion.antecedent_family == "blocker":
            continue
        if getattr(criterion, "level", None) == "definition":
            continue
        if getattr(criterion, "method_mandated", False):
            continue
        if getattr(criterion, "kind", None) == "validation_classification":
            continue
        if getattr(criterion, "mint_degrade", None) is not None:
            continue
        if getattr(criterion, "requested_output_path_mint_source", None) in REQUESTED_OUTPUT_PATH_MINT_SOURCES:
            continue
        path = _canonical_requested_output_path(getattr(criterion, "output_path", None))
        if path and _output_path_has_child(path):
            paths.add(path)
    return paths


def _contingent_antecedent_child_paths(ctx: AgentContext) -> set[str]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return set()
    paths: set[str] = set()
    for criterion in _active_completion_criteria(ctx):
        if not isinstance(criterion, CompletionCriterion):
            continue
        if criterion.mint_degrade is not None:
            continue
        if criterion.level == "definition" and not (
            criterion.output_path and _is_judgment_boolean_criterion(criterion)
        ):
            continue
        raw_paths = [criterion.contingent_antecedent_output_path]
        if criterion.antecedent_family == "blocker":
            raw_paths.append(criterion.output_path)
        for raw_path in raw_paths:
            path = _canonical_requested_output_path(raw_path)
            if path and _output_path_has_child(path):
                paths.add(path)
    return paths


def _active_completion_criteria(ctx: AgentContext) -> list[CompletionCriterion]:
    request_policy = ctx.request_policy
    if request_policy is None:
        return []
    return request_policy.graded_completion_criteria()


class _DefinitionPlaneReject(NamedTuple):
    criterion_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    unreferenced_parameter_keys: tuple[str, ...]


def _expression_parameter_sources(expression: ast.AST, bindings: Mapping[str, set[str]]) -> set[str]:
    if isinstance(expression, ast.Name) and isinstance(expression.ctx, ast.Load):
        return set(bindings.get(expression.id, set()))
    if isinstance(expression, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        local_bindings = {name: set(sources) for name, sources in bindings.items()}
        comprehension_sources: set[str] = set()
        for generator in expression.generators:
            comprehension_sources.update(_expression_parameter_sources(generator.iter, local_bindings))
            _bind_parameter_sources(generator.target, set(), local_bindings)
            for condition in generator.ifs:
                comprehension_sources.update(_expression_parameter_sources(condition, local_bindings))
        if isinstance(expression, ast.DictComp):
            comprehension_sources.update(_expression_parameter_sources(expression.key, local_bindings))
            comprehension_sources.update(_expression_parameter_sources(expression.value, local_bindings))
        else:
            comprehension_sources.update(_expression_parameter_sources(expression.elt, local_bindings))
        return comprehension_sources
    if isinstance(expression, ast.Lambda):
        local_bindings = {name: set(sources) for name, sources in bindings.items()}
        for argument in (*expression.args.posonlyargs, *expression.args.args, *expression.args.kwonlyargs):
            local_bindings[argument.arg] = set()
        if expression.args.vararg is not None:
            local_bindings[expression.args.vararg.arg] = set()
        if expression.args.kwarg is not None:
            local_bindings[expression.args.kwarg.arg] = set()
        return _expression_parameter_sources(expression.body, local_bindings)
    if isinstance(expression, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return set()
    sources: set[str] = set()
    for child in ast.iter_child_nodes(expression):
        sources.update(_expression_parameter_sources(child, bindings))
    return sources


def _awaited_parameter_sources(expression: ast.AST, bindings: Mapping[str, set[str]]) -> set[str]:
    if isinstance(expression, ast.Await):
        return _expression_parameter_sources(expression.value, bindings)
    if isinstance(expression, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return set()
    sources: set[str] = set()
    for child in ast.iter_child_nodes(expression):
        sources.update(_awaited_parameter_sources(child, bindings))
    return sources


def _bind_parameter_sources(target: ast.expr, sources: set[str], bindings: dict[str, set[str]]) -> None:
    if isinstance(target, ast.Name):
        bindings[target.id] = set(sources)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _bind_parameter_sources(element, sources, bindings)


def _merge_parameter_bindings(left: Mapping[str, set[str]], right: Mapping[str, set[str]]) -> dict[str, set[str]]:
    return {name: set(left.get(name, set())) | set(right.get(name, set())) for name in set(left) | set(right)}


def _loop_body_has_reachable_break(statements: Sequence[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, ast.Break):
            return True
        if isinstance(
            statement, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if not _statements_can_fall_through([statement]):
                return False
            continue
        if isinstance(statement, ast.If):
            if isinstance(statement.test, ast.Constant):
                selected_branch = statement.body if bool(statement.test.value) else statement.orelse
                if _loop_body_has_reachable_break(selected_branch):
                    return True
            elif _loop_body_has_reachable_break(statement.body) or _loop_body_has_reachable_break(statement.orelse):
                return True
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            if statement.finalbody:
                if _loop_body_has_reachable_break(statement.finalbody):
                    return True
                if not _statements_can_fall_through(statement.finalbody):
                    return False
            branches = [statement.body, statement.orelse]
            branches.extend(handler.body for handler in statement.handlers)
            if any(_loop_body_has_reachable_break(branch) for branch in branches):
                return True
        elif isinstance(statement, (ast.With, ast.AsyncWith)) and _loop_body_has_reachable_break(statement.body):
            return True
        elif isinstance(statement, ast.Match) and any(
            _loop_body_has_reachable_break(case.body) for case in statement.cases
        ):
            return True
        if not _statements_can_fall_through([statement]):
            return False
    return False


def _statements_can_fall_through(statements: Sequence[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return False
        if isinstance(statement, ast.If):
            if isinstance(statement.test, ast.Constant):
                selected_branch = statement.body if bool(statement.test.value) else statement.orelse
                if not _statements_can_fall_through(selected_branch):
                    return False
                continue
            if statement.orelse:
                if not _statements_can_fall_through(statement.body) and not _statements_can_fall_through(
                    statement.orelse
                ):
                    return False
        if (
            isinstance(statement, ast.While)
            and isinstance(statement.test, ast.Constant)
            and bool(statement.test.value)
            and not _loop_body_has_reachable_break(statement.body)
        ):
            return False
        if isinstance(statement, (ast.Try, ast.TryStar)):
            if statement.finalbody and not _statements_can_fall_through(statement.finalbody):
                return False
            normal_falls_through = _statements_can_fall_through(statement.body) and _statements_can_fall_through(
                statement.orelse
            )
            handler_falls_through = any(_statements_can_fall_through(handler.body) for handler in statement.handlers)
            if not normal_falls_through and not handler_falls_through:
                return False
    return True


def _statement_parameter_dataflow(
    statements: Sequence[ast.stmt], bindings: dict[str, set[str]]
) -> tuple[set[str], dict[str, set[str]]]:
    consumed: set[str] = set()
    current = {name: set(sources) for name, sources in bindings.items()}
    for statement in statements:
        if isinstance(statement, ast.Assign):
            consumed.update(_awaited_parameter_sources(statement.value, current))
            sources = _expression_parameter_sources(statement.value, current)
            for target in statement.targets:
                _bind_parameter_sources(target, sources, current)
            continue
        if isinstance(statement, ast.AnnAssign):
            value = statement.value
            sources = _expression_parameter_sources(value, current) if value is not None else set()
            if value is not None:
                consumed.update(_awaited_parameter_sources(value, current))
            _bind_parameter_sources(statement.target, sources, current)
            continue
        if isinstance(statement, ast.AugAssign):
            sources = _expression_parameter_sources(statement.target, current)
            sources.update(_expression_parameter_sources(statement.value, current))
            consumed.update(_awaited_parameter_sources(statement.value, current))
            _bind_parameter_sources(statement.target, sources, current)
            continue
        if isinstance(statement, ast.Expr):
            consumed.update(_awaited_parameter_sources(statement.value, current))
            continue
        if isinstance(statement, ast.Return):
            if statement.value is not None:
                consumed.update(_expression_parameter_sources(statement.value, current))
            break
        if isinstance(statement, ast.Raise):
            if statement.exc is not None:
                consumed.update(_expression_parameter_sources(statement.exc, current))
            break
        if isinstance(statement, ast.If):
            consumed.update(_expression_parameter_sources(statement.test, current))
            if isinstance(statement.test, ast.Constant) and statement.test.value is False:
                branch_consumed, branch_bindings = _statement_parameter_dataflow(statement.orelse, current)
                consumed.update(branch_consumed)
                current = branch_bindings
                continue
            if isinstance(statement.test, ast.Constant) and statement.test.value is True:
                branch_consumed, branch_bindings = _statement_parameter_dataflow(statement.body, current)
                consumed.update(branch_consumed)
                current = branch_bindings
                continue
            body_consumed, body_bindings = _statement_parameter_dataflow(statement.body, current)
            else_consumed, else_bindings = _statement_parameter_dataflow(statement.orelse, current)
            consumed.update(body_consumed)
            consumed.update(else_consumed)
            body_falls_through = _statements_can_fall_through(statement.body)
            else_falls_through = _statements_can_fall_through(statement.orelse)
            if body_falls_through and else_falls_through:
                current = _merge_parameter_bindings(body_bindings, else_bindings)
            elif body_falls_through:
                current = body_bindings
            elif else_falls_through:
                current = else_bindings
            else:
                break
            continue
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            consumed.update(_expression_parameter_sources(statement.iter, current))
            loop_bindings = {name: set(sources) for name, sources in current.items()}
            _bind_parameter_sources(statement.target, set(), loop_bindings)
            body_consumed, body_bindings = _statement_parameter_dataflow(statement.body, loop_bindings)
            else_consumed, else_bindings = _statement_parameter_dataflow(statement.orelse, current)
            consumed.update(body_consumed)
            consumed.update(else_consumed)
            current = _merge_parameter_bindings(current, _merge_parameter_bindings(body_bindings, else_bindings))
            continue
        if isinstance(statement, ast.While):
            consumed.update(_expression_parameter_sources(statement.test, current))
            if isinstance(statement.test, ast.Constant) and statement.test.value is False:
                else_consumed, current = _statement_parameter_dataflow(statement.orelse, current)
                consumed.update(else_consumed)
                continue
            body_consumed, body_bindings = _statement_parameter_dataflow(statement.body, current)
            else_consumed, else_bindings = _statement_parameter_dataflow(statement.orelse, current)
            consumed.update(body_consumed)
            consumed.update(else_consumed)
            current = _merge_parameter_bindings(current, _merge_parameter_bindings(body_bindings, else_bindings))
            continue
        if isinstance(statement, (ast.Try, ast.TryStar)):
            body_consumed, body_bindings = _statement_parameter_dataflow(statement.body, current)
            consumed.update(body_consumed)
            merged = body_bindings
            for handler in statement.handlers:
                handler_bindings = {name: set(sources) for name, sources in current.items()}
                if handler.name:
                    handler_bindings[handler.name] = set()
                handler_consumed, handler_bindings = _statement_parameter_dataflow(handler.body, handler_bindings)
                consumed.update(handler_consumed)
                merged = _merge_parameter_bindings(merged, handler_bindings)
            else_consumed, else_bindings = _statement_parameter_dataflow(statement.orelse, body_bindings)
            consumed.update(else_consumed)
            merged = _merge_parameter_bindings(merged, else_bindings)
            final_consumed, current = _statement_parameter_dataflow(statement.finalbody, merged)
            consumed.update(final_consumed)
            continue
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            with_bindings = {name: set(sources) for name, sources in current.items()}
            for item in statement.items:
                consumed.update(_expression_parameter_sources(item.context_expr, current))
                if item.optional_vars is not None:
                    _bind_parameter_sources(item.optional_vars, set(), with_bindings)
            body_consumed, current = _statement_parameter_dataflow(statement.body, with_bindings)
            consumed.update(body_consumed)
            continue
        if isinstance(statement, ast.Match):
            consumed.update(_expression_parameter_sources(statement.subject, current))
            merged = {name: set(sources) for name, sources in current.items()}
            for case in statement.cases:
                if case.guard is not None:
                    consumed.update(_expression_parameter_sources(case.guard, current))
                case_consumed, case_bindings = _statement_parameter_dataflow(case.body, current)
                consumed.update(case_consumed)
                merged = _merge_parameter_bindings(merged, case_bindings)
            current = merged
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_bindings = {name: set(sources) for name, sources in current.items()}
            for argument in (
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            ):
                function_bindings[argument.arg] = set()
            if statement.args.vararg is not None:
                function_bindings[statement.args.vararg.arg] = set()
            if statement.args.kwarg is not None:
                function_bindings[statement.args.kwarg.arg] = set()
            function_consumed, _ = _statement_parameter_dataflow(statement.body, function_bindings)
            current[statement.name] = function_consumed
            continue
        if isinstance(statement, ast.ClassDef):
            current[statement.name] = set()
            continue
        consumed.update(_expression_parameter_sources(statement, current))
    return consumed, current


def _code_runtime_parameter_sources(code: str, parameter_keys: set[str]) -> set[str] | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    initial_bindings = {key: {key} for key in parameter_keys}
    consumed, _ = _statement_parameter_dataflow(tree.body, initial_bindings)
    return consumed


def _value_template_parameter_sources(value: Any, parameter_keys: set[str], depth: int = 0) -> set[str]:
    if depth > 12:
        return set()
    if isinstance(value, str):
        try:
            return get_missing_variables(value, {}) & parameter_keys
        except TemplateSyntaxError:
            return set()
    if isinstance(value, Mapping):
        sources: set[str] = set()
        raw_keys = value.get("parameter_keys")
        if isinstance(raw_keys, list):
            sources.update(key for key in raw_keys if key in parameter_keys)
        for child in value.values():
            sources.update(_value_template_parameter_sources(child, parameter_keys, depth + 1))
        return sources
    if isinstance(value, list):
        list_sources: set[str] = set()
        for child in value:
            list_sources.update(_value_template_parameter_sources(child, parameter_keys, depth + 1))
        return list_sources
    return set()


def _non_code_runtime_parameter_sources(parsed: Mapping[str, Any], parameter_keys: set[str]) -> set[str]:
    definition = parsed.get("workflow_definition")
    blocks = definition.get("blocks") if isinstance(definition, Mapping) else None
    sources: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, Mapping):
            return
        is_block = "block_type" in value
        if is_block and _enum_or_string_name(value.get("block_type")) != BlockType.CODE.value:
            own_fields = {
                key: child
                for key, child in value.items()
                if key not in {*_ORDERED_CHILD_BLOCK_LIST_KEYS, *_ORDERED_BRANCH_LIST_KEYS}
            }
            sources.update(_value_template_parameter_sources(own_fields, parameter_keys))
        for key in (*_ORDERED_CHILD_BLOCK_LIST_KEYS, *_ORDERED_BRANCH_LIST_KEYS):
            visit(value.get(key))

    visit(blocks)
    return sources


def _workflow_runtime_parameter_sources(parsed: dict[str, Any]) -> set[str] | None:
    parameter_keys = _declared_string_workflow_parameter_keys(parsed)
    sources: set[str] = set()
    for block in _workflow_code_blocks(parsed):
        raw_parameter_keys = block.get("parameter_keys")
        block_parameter_keys = (
            {key for key in raw_parameter_keys if isinstance(key, str) and key}
            if isinstance(raw_parameter_keys, list)
            else set()
        )
        block_sources = _code_runtime_parameter_sources(str(block.get("code") or ""), block_parameter_keys)
        if block_sources is None:
            return None
        sources.update(block_sources)
    sources.update(_non_code_runtime_parameter_sources(parsed, parameter_keys))
    return sources


def _definition_plane_preflight_reject(
    ctx: AgentContext,
    workflow_yaml: str,
    *,
    enforce_untagged_declared_inputs: bool = False,
) -> _DefinitionPlaneReject | None:
    definition_criteria = [
        criterion for criterion in _active_completion_criteria(ctx) if criterion.level == "definition"
    ]
    code_only_browser = _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
    if not definition_criteria and not (code_only_browser and enforce_untagged_declared_inputs):
        return None
    unreferenced_parameter_keys: tuple[str, ...] = ()
    runtime_sources: set[str] | None = None
    if code_only_browser:
        parsed = parse_workflow_yaml(workflow_yaml)
        if isinstance(parsed, dict):
            runtime_sources = _workflow_runtime_parameter_sources(parsed)
            if runtime_sources is None:
                return None
            parameter_keys = _declared_string_workflow_parameter_keys(parsed)
            unreferenced_parameter_keys = tuple(sorted(parameter_keys - runtime_sources))
    unsatisfied = [
        verdict
        for verdict in grade_definition_criteria(definition_criteria, workflow_yaml)
        if verdict.state == "unsatisfied"
    ]
    if runtime_sources is not None and not unreferenced_parameter_keys:
        unsatisfied = [
            verdict for verdict in unsatisfied if verdict.reason_code != "definition_parameters_unreferenced"
        ]
    if not unsatisfied and not unreferenced_parameter_keys:
        return None
    return _DefinitionPlaneReject(
        criterion_ids=tuple(verdict.criterion_id for verdict in unsatisfied)
        or tuple(criterion.id for criterion in definition_criteria),
        reason_codes=tuple(verdict.reason_code for verdict in unsatisfied)
        or (("definition_parameters_unreferenced",) if unreferenced_parameter_keys else ()),
        unreferenced_parameter_keys=unreferenced_parameter_keys,
    )


def _definition_plane_reject_error(rejection: _DefinitionPlaneReject) -> str:
    if rejection.unreferenced_parameter_keys:
        keys = ", ".join(f"`{key}`" for key in rejection.unreferenced_parameter_keys)
        return f"The submitted workflow declares reusable parameters that no block references: {keys}."
    return "The submitted workflow does not satisfy its active definition-level requirements."


def _definition_plane_structural_payload(
    workflow_yaml: str,
    rejection: _DefinitionPlaneReject,
    code_artifact_metadata: object = None,
) -> tuple[dict[str, object], str | None]:
    authored_signature = authored_structure_signature_from_workflow(workflow_yaml, code_artifact_metadata)
    return (
        {
            "reason_code": "definition_contract_unsatisfied",
            "criterion_ids": rejection.criterion_ids,
            "definition_reason_codes": rejection.reason_codes,
            "unreferenced_parameter_keys": rejection.unreferenced_parameter_keys,
            "authored_structure_signature": authored_signature,
        },
        authored_signature,
    )


def _definition_plane_structural_key(
    workflow_yaml: str,
    rejection: _DefinitionPlaneReject,
    code_artifact_metadata: object = None,
) -> str:
    structural_payload, authored_signature = _definition_plane_structural_payload(
        workflow_yaml,
        rejection,
        code_artifact_metadata,
    )
    outcome = recorded_outcome_from_author_time_reject(
        reason_code="definition_contract_unsatisfied",
        block_labels=sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml)),
        structural_payload=structural_payload,
        authored_structure_signature=authored_signature,
        observed_evidence_summary=_definition_plane_reject_error(rejection),
    )
    return outcome.structural_key or authored_signature or ""


def _judgment_output_paths(ctx: AgentContext) -> set[str]:
    # Judgment-boolean paths are dropped from the producer's static-bind set, not moved to the
    # declaration lane; declaring one would fabricate a None where the run's own judgment belongs. The
    # requested-output gate reads the completion criteria directly rather than this bind set, so dropping
    # cannot pin it open.
    paths: set[str] = set()
    for criterion in _active_completion_criteria(ctx):
        if not isinstance(criterion, CompletionCriterion):
            continue
        if not _is_judgment_boolean_criterion(criterion):
            continue
        path = _canonical_requested_output_path(criterion.output_path)
        if path and _output_path_has_child(path):
            paths.add(path)
    return paths


def _canonical_requested_output_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    path = value.strip()
    if path == "$":
        return ""
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$["):
        path = path[1:]
    path = path.replace("[*]", "[]")
    path = re.sub(r"\[\d+\]", "[]", path)
    return ".".join(part for part in path.split(".") if part)


def _required_child_output_paths_for_authoring(ctx: AgentContext) -> tuple[set[str], str, str]:
    return (
        _requested_output_child_paths(ctx),
        "requested_output_contract",
        "requested_output_contract_missing_output_coverage",
    )


_DECLARATION_REQUIRED_VALUE_STATUS = "declaration_required_default_none"


def _missing_requested_output_facts(
    paths: Iterable[str],
    *,
    reason_code: str,
    declaration_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    declaration_paths = declaration_paths or set()
    return [
        {
            "output_path": path,
            "output_root": _output_path_root(path),
            "reason_code": reason_code,
            "value_status": (_DECLARATION_REQUIRED_VALUE_STATUS if path in declaration_paths else "no_typed_value"),
        }
        for path in sorted(paths)
    ]


def _single_repair_block_label(block_labels: list[str]) -> str:
    labels = [str(label).strip() for label in block_labels if str(label).strip()]
    return labels[0] if len(labels) == 1 else ""


def _normalized_repair_paths(paths: Iterable[str]) -> list[str]:
    return sorted(dict.fromkeys(str(path).strip() for path in paths if str(path).strip()))


def _declaration_repair_sentence(declaration_paths: Iterable[str]) -> str:
    declaration_text = ", ".join(_normalized_repair_paths(declaration_paths))
    if not declaration_text:
        return ""
    return (
        f" Declare {declaration_text} in the extraction_schema and the returned structure with value None "
        "unless the run actually hits that condition; never source it from the page."
    )


def _metadata_repair_contract(
    *,
    block_labels: list[str],
    required_paths: Iterable[str],
    source: str,
    reason_code: str,
    declaration_paths: Iterable[str] = (),
) -> dict[str, object] | None:
    goal_paths = _normalized_repair_paths(required_paths)
    union_paths = sorted(dict.fromkeys([*goal_paths, *_normalized_repair_paths(declaration_paths)]))
    block_label = _single_repair_block_label(block_labels)
    if not union_paths or not block_label:
        return None
    return {
        "block_label": block_label,
        "required_goal_value_paths": goal_paths,
        "required_extraction_schema_paths": union_paths,
        "required_code_return_paths": union_paths,
        "source": source,
        "reason_code": reason_code,
    }


def _metadata_output_repair_context(
    *,
    block_labels: list[str],
    required_paths: Iterable[str],
    coverage_reason_code: str,
    source: str,
    summary: str,
    declaration_paths: Iterable[str] = (),
) -> CodeAuthoringRepairContext | None:
    goal_paths = _normalized_repair_paths(required_paths)
    declaration = _normalized_repair_paths(declaration_paths)
    union_paths = sorted(dict.fromkeys([*goal_paths, *declaration]))
    block_label = _single_repair_block_label(block_labels)
    if not union_paths or not block_label:
        return None
    path_text = ", ".join(union_paths)
    return CodeAuthoringRepairContext(
        block_label=block_label,
        reason_code="metadata_reject",
        runtime_failure_class=coverage_reason_code,
        runtime_failure_reason=summary,
        required_goal_value_paths=goal_paths,
        required_extraction_schema_paths=union_paths,
        required_code_return_paths=union_paths,
        metadata_contract_source=source,
        metadata_contract_reason_code=coverage_reason_code,
        repair_instruction=(
            "Declare code_artifact_metadata goal_value_paths and extraction_schema for required output paths "
            f"{path_text}, make the code return those paths, then rerun update_and_run_blocks."
            + _declaration_repair_sentence(declaration)
        ),
    )


_METADATA_CONTRACT_REQUIRED_BEFORE_RUN_REASON_CODE = "metadata_contract_required_before_run"
_OUTPUT_CONTRACT_REJECT_REASON_CODE = "output_contract_required"
_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE = "value_bearing_output_required"
_OUTPUT_CONTRACT_UNDECLARED_SENTINEL_PATH = "output"
_VALUE_BEARING_ROOT_GUIDANCE_PATH = "output"
_VALUE_BEARING_PREARM_FINGERPRINT_PREFIX = "value-bearing:prearm:"
_VALUE_BEARING_GUIDANCE_FINGERPRINT_PREFIX = "value-bearing:guidance:"


@dataclass(frozen=True)
class _OutputContractEvaluation:
    block_label: str
    required_paths: set[str]
    observation_paths: set[str]
    declaration_paths: set[str]
    source: str
    reason_code: str
    missing_metadata_paths: list[str]
    missing_schema_paths: list[str]
    missing_return_paths: list[str]
    shape_violations: list[str]
    canonical_signature: str
    payload: dict[str, Any]
    repair_context: CodeAuthoringRepairContext | None

    @property
    def has_deficiencies(self) -> bool:
        return bool(
            self.missing_metadata_paths
            or self.missing_schema_paths
            or self.missing_return_paths
            or self.shape_violations
        )


@dataclass(frozen=True)
class _RuntimeOutputRepairContract:
    required_paths: set[str]
    facts: list[dict[str, Any]]
    workflow_run_id: str
    owner_labels: list[str]
    owner_labels_by_path: dict[str, list[str]]
    source: str = "runtime_output_repair"
    reason_code: str = "runtime_output_repair_required"


def _metadata_contract_required_paths(paths: Iterable[str]) -> set[str]:
    return {
        path
        for raw_path in paths
        for path in [_canonical_requested_output_path(str(raw_path))]
        if path and _output_path_has_child(path)
    }


def _path_segments(path: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        if not is_array and "[]" in name:
            name = name.replace("[]", "")
            is_array = True
        if name:
            segments.append((name, is_array))
    return segments


def _metadata_item_for_block_label(raw_metadata: object, block_label: str) -> Mapping[str, Any] | None:
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        if str(item.get("block_label") or "").strip() == block_label:
            return item
    return None


def _metadata_has_mapping_item(raw_metadata: object) -> bool:
    return any(_raw_metadata_item_mapping(item) is not None for item in _code_artifact_metadata_items(raw_metadata))


def _goal_paths_rooted_as_the_code_returns_them(required_paths: set[str], code: str) -> set[str]:
    """Goal paths named the way ``code`` names them.

    A goal path is written in the block's own namespace while a plan-derived extraction returns the
    requested-output namespace, so one calls the value `visitors` and the other
    `output.visitors`; compared raw, a value the code returns reads as absent. Re-rooting is applied
    only where it is what the code actually returns, so a block returning a bare mapping keeps naming
    its keys bare.
    """
    if not required_paths:
        return required_paths
    produced = _code_block_produced_output_paths(code)
    return {
        f"output.{path}" if path not in produced and f"output.{path}" in produced else path for path in required_paths
    }


def _metadata_item_goal_value_paths(item: Mapping[str, Any] | None) -> set[str]:
    if item is None:
        return set()
    paths: set[str] = set()
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            paths.update(
                path
                for raw_path in _artifact_goal_value_paths(row.get("goal_value_paths"))
                for path in [_canonical_requested_output_path(raw_path)]
                if path
            )
    return paths


def _metadata_item_effective_schema_text(item: Mapping[str, Any] | None, required_paths: set[str]) -> str:
    if item is None or not required_paths:
        return ""
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            schema_text = row.get("extraction_schema")
            schema = _parse_extraction_schema(schema_text)
            if schema is not None:
                return str(schema_text or "").strip() if required_paths <= _schema_property_paths(schema) else ""
    return ""


def _active_metadata_repair_block_label(ctx: AgentContext) -> str:
    repair_context = getattr(ctx, "last_code_authoring_repair_context", None)
    if not isinstance(repair_context, CodeAuthoringRepairContext):
        return ""
    if repair_context.reason_code != "metadata_reject":
        return ""
    return str(repair_context.block_label or "").strip()


def _output_metadata_owner_labels(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    required_paths: set[str],
) -> list[str]:
    if not required_paths:
        return []
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    owners: set[str] = set()
    for raw_item in _code_artifact_metadata_items(raw_code_artifact_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        label = str(item.get("block_label") or "").strip()
        if label in code_blocks and required_paths <= _metadata_item_goal_value_paths(item):
            owners.add(label)
    repair_label = _active_metadata_repair_block_label(ctx)
    if repair_label in code_blocks:
        owners.add(repair_label)
    for label, block in code_blocks.items():
        if required_paths <= _code_block_produced_output_paths(str(block.get("code") or "")):
            owners.add(label)
    return sorted(owners)


def _target_output_contract_block_label(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    required_paths: set[str],
) -> tuple[str, list[str]]:
    runtime_contract = _runtime_output_repair_contract_from_recorded_outcome(ctx)
    if runtime_contract is not None:
        code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
        runtime_owner_labels: set[str] = set()
        missing_owner = False
        ambiguous_owner = False
        for path in sorted(runtime_contract.required_paths):
            raw_path_owners = sorted(runtime_contract.owner_labels_by_path.get(path, []))
            path_owner_labels = sorted(label for label in raw_path_owners if label in code_blocks)
            # Ambiguity is judged before filtering: dropping stale labels must not resolve a
            # contested path down to a lone survivor.
            if len(raw_path_owners) > 1:
                ambiguous_owner = True
                runtime_owner_labels.update(path_owner_labels)
                continue
            if len(path_owner_labels) != 1:
                missing_owner = missing_owner or not path_owner_labels
                runtime_owner_labels.update(path_owner_labels)
                continue
            runtime_owner_labels.add(path_owner_labels[0])
        if missing_owner:
            return "", []
        current_owner_labels = sorted(runtime_owner_labels)
        if ambiguous_owner:
            return "", current_owner_labels
        if len(current_owner_labels) == 1:
            return current_owner_labels[0], current_owner_labels
        return "", current_owner_labels
    owner_labels = _output_metadata_owner_labels(ctx, workflow_yaml, raw_code_artifact_metadata, required_paths)
    if len(owner_labels) == 1:
        return owner_labels[0], owner_labels
    return "", owner_labels


def _runtime_output_repair_contract_from_recorded_outcome(ctx: AgentContext) -> _RuntimeOutputRepairContract | None:
    outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if not isinstance(outcome, RecordedBuildTestOutcome):
        return None
    if not (
        outcome.is_authoritative
        and outcome.phase == "persisted_block_run"
        and outcome.reason_code == "no_meaningful_output"
        and outcome.workflow_run_id
    ):
        return None
    facts: list[dict[str, Any]] = []
    required_paths: set[str] = set()
    owner_labels: set[str] = set()
    owner_labels_by_path: dict[str, set[str]] = {}
    for raw_fact in outcome.runtime_output_repair_facts:
        if not isinstance(raw_fact, Mapping):
            return None
        if str(raw_fact.get("workflow_run_id") or "").strip() != outcome.workflow_run_id:
            return None
        path = _canonical_requested_output_path(str(raw_fact.get("output_path") or ""))
        if not path or not _output_path_has_child(path):
            return None
        fact = dict(raw_fact)
        fact["output_path"] = path
        fact["output_root"] = _output_path_root(path)
        path_owner_labels = owner_labels_by_path.setdefault(path, set())
        raw_owner_labels = fact.get("owner_labels")
        if isinstance(raw_owner_labels, list):
            path_owner_labels.update(str(label).strip() for label in raw_owner_labels if str(label).strip())
        label = str(fact.get("block_label") or "").strip()
        if label:
            path_owner_labels.add(label)
        owner_labels.update(path_owner_labels)
        required_paths.add(path)
        facts.append(fact)
    if not facts or not required_paths:
        return None
    return _RuntimeOutputRepairContract(
        required_paths=required_paths,
        facts=sorted(facts, key=lambda item: str(item.get("output_path") or "")),
        workflow_run_id=outcome.workflow_run_id,
        owner_labels=sorted(owner_labels),
        owner_labels_by_path={path: sorted(labels) for path, labels in sorted(owner_labels_by_path.items())},
    )


class _OutputContractLiveness(StrEnum):
    ABSENT = "absent"
    VALUE_REQUIRED = "value_required"
    DEGRADED_EMPTY = "degraded_empty"


@dataclass(frozen=True)
class _DegradedRequestSlotDiagnostic:
    request_slot_id: str
    floor_rekeyed_from_path: str
    pinability: str
    mint_disposition: str
    mint_degrade: str
    request_slot_failure_kind: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.request_slot_id, self.floor_rekeyed_from_path

    def to_payload(self) -> dict[str, str]:
        return {
            "request_slot_id": self.request_slot_id,
            "floor_rekeyed_from_path": self.floor_rekeyed_from_path,
            "pinability": self.pinability,
            "mint_disposition": self.mint_disposition,
            "mint_degrade": self.mint_degrade,
            "request_slot_failure_kind": self.request_slot_failure_kind,
        }


@dataclass(frozen=True)
class _OutputContractRequiredPaths:
    """Two-lane contract: observation paths must be sourced from the page/run; declaration paths
    must only be declared in the returned structure (None when the contingency never fires)."""

    observation_paths: set[str]
    declaration_paths: set[str]
    source: str
    reason_code: str
    degraded_request_slots: tuple[_DegradedRequestSlotDiagnostic, ...] = ()

    @property
    def union(self) -> set[str]:
        return self.observation_paths | self.declaration_paths

    @property
    def liveness(self) -> _OutputContractLiveness:
        if self.observation_paths:
            return _OutputContractLiveness.VALUE_REQUIRED
        if self.degraded_request_slots:
            return _OutputContractLiveness.DEGRADED_EMPTY
        return _OutputContractLiveness.ABSENT


def _value_bearing_directive_paths(contract: _OutputContractRequiredPaths) -> set[str]:
    if not contract.observation_paths and (
        contract.declaration_paths or contract.liveness is _OutputContractLiveness.DEGRADED_EMPTY
    ):
        return {_VALUE_BEARING_ROOT_GUIDANCE_PATH}
    if contract.union:
        return set(contract.union)
    return set()


def _degraded_request_slot_diagnostics(ctx: AgentContext) -> tuple[_DegradedRequestSlotDiagnostic, ...]:
    diagnostics: list[_DegradedRequestSlotDiagnostic] = []
    request_policy = ctx.request_policy
    request_slot_failure_kind = request_policy.request_slot_failure_kind if request_policy is not None else None
    for criterion in _active_completion_criteria(ctx):
        if not isinstance(criterion, CompletionCriterion):
            continue
        if not (criterion.mint_disposition == "degraded" or criterion.mint_degrade is not None):
            continue
        if not criterion.request_slot_id and request_slot_failure_kind is None:
            continue
        diagnostics.append(
            _DegradedRequestSlotDiagnostic(
                request_slot_id=criterion.request_slot_id or "",
                floor_rekeyed_from_path=_canonical_requested_output_path(criterion.floor_rekeyed_from_path),
                pinability=str(criterion.pinability or ""),
                mint_disposition=criterion.mint_disposition,
                mint_degrade=str(criterion.mint_degrade or ""),
                request_slot_failure_kind=request_slot_failure_kind or "",
            )
        )
    return tuple(sorted(diagnostics, key=lambda item: item.identity))


def _output_contract_signature(
    *,
    ctx: AgentContext | None = None,
    scope_key: str = "",
    required_paths: set[str],
) -> str:
    """Stable output-safety identity that does not select or rewrite an author-owned block."""
    request_slot_identity = (
        tuple(diagnostic.identity for diagnostic in _degraded_request_slot_diagnostics(ctx)) if ctx is not None else ()
    )
    payload = {
        "scope": scope_key,
        "required_paths": sorted(required_paths),
        "request_slot_identity": sorted(request_slot_identity),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _runtime_output_contract_signature(runtime_contract: _RuntimeOutputRepairContract | None) -> str:
    if runtime_contract is None:
        return ""
    payload = {
        "workflow_run_id": runtime_contract.workflow_run_id,
        "required_paths": sorted(runtime_contract.required_paths),
        "owner_labels": runtime_contract.owner_labels,
        "owner_labels_by_path": runtime_contract.owner_labels_by_path,
        "facts": runtime_contract.facts,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _output_contract_required_paths_source(ctx: AgentContext) -> _OutputContractRequiredPaths:
    runtime_contract = _runtime_output_repair_contract_from_recorded_outcome(ctx)
    antecedent_paths = _contingent_antecedent_child_paths(ctx)
    degraded_request_slots = _degraded_request_slot_diagnostics(ctx)
    if runtime_contract is not None:
        runtime_observation_paths = runtime_contract.required_paths - _judgment_output_paths(ctx)
        return _OutputContractRequiredPaths(
            observation_paths=runtime_observation_paths,
            declaration_paths=antecedent_paths - runtime_observation_paths,
            source=runtime_contract.source,
            reason_code=runtime_contract.reason_code,
            degraded_request_slots=degraded_request_slots,
        )
    observation_paths, source, reason_code = _required_child_output_paths_for_authoring(ctx)
    observation_paths = observation_paths - _judgment_output_paths(ctx)
    repair_context = getattr(ctx, "last_code_authoring_repair_context", None)
    if (
        not observation_paths
        and isinstance(repair_context, CodeAuthoringRepairContext)
        and repair_context.reason_code == "metadata_reject"
    ):
        goal_paths = _metadata_contract_required_paths(repair_context.required_goal_value_paths)
        rehydrated = _metadata_contract_required_paths(
            [
                *repair_context.required_goal_value_paths,
                *repair_context.required_extraction_schema_paths,
                *repair_context.required_code_return_paths,
            ]
        )
        rehydrated -= _judgment_output_paths(ctx)
        # An antecedent the repair contract carried only in schema/return roles stays in the
        # declaration lane on rehydration; the goal role is the observation-lane record.
        observation_paths = rehydrated - (antecedent_paths - goal_paths)
        source = str(repair_context.metadata_contract_source or "").strip() or "metadata_reject"
        reason_code = (
            str(repair_context.metadata_contract_reason_code or "").strip()
            or str(repair_context.runtime_failure_class or "").strip()
            or "metadata_reject"
        )
    return _OutputContractRequiredPaths(
        observation_paths=observation_paths,
        declaration_paths=antecedent_paths - observation_paths,
        source=source,
        reason_code=reason_code,
        degraded_request_slots=degraded_request_slots,
    )


def _declaration_envelope_paths(declaration_paths: set[str]) -> set[str]:
    return declaration_paths | {_output_path_root(path) for path in declaration_paths}


def _mutation_root_name(expression: ast.expr) -> str:
    node = expression
    while isinstance(node, (ast.Attribute, ast.Starred, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _statement_mutation_root_names(statement: ast.stmt) -> set[str]:
    """Every name the statement could rebind or mutate, walking any target shape to its root
    name; method receivers and call arguments count because the callee may write through them."""
    names: set[str] = set()
    for node in ast.walk(statement):
        if isinstance(node, (ast.Name, ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if root := _mutation_root_name(node):
                names.add(root)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".", 1)[0] for alias in node.names)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.Call):
            receivers = [node.func.value] if isinstance(node.func, ast.Attribute) else []
            receivers.extend(node.args)
            receivers.extend(keyword.value for keyword in node.keywords)
            for receiver in receivers:
                if root := _mutation_root_name(receiver):
                    names.add(root)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            # Assigning into a subscript/attribute target aliases the RHS object into that
            # container, so a later mutation through the container mutates the RHS too. Taint every
            # RHS name so the alias is not resolved to its pre-mutation value (over-taint is fail-open).
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, (ast.Subscript, ast.Attribute)) for target in targets):
                names.update(child.id for child in ast.walk(node.value) if isinstance(child, ast.Name))
    return names


def _top_level_static_assignments(tree: ast.Module) -> tuple[dict[str, ast.expr], set[str]]:
    """Only a single-name top-level assignment resolves statically; every other write or
    mutation shape marks its root name uncertain, and uncertain is terminal (fail-open).
    Mutating an alias mutates the aliased object, so the taint spreads to every name the
    marked assignment references."""
    assignments: dict[str, ast.expr] = {}
    uncertain_names: set[str] = set()

    def mark_uncertain(names: set[str]) -> None:
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in uncertain_names:
                continue
            uncertain_names.add(name)
            assigned = assignments.pop(name, None)
            if assigned is not None:
                pending.extend(node.id for node in ast.walk(assigned) if isinstance(node, ast.Name))

    for node in tree.body:
        target_name = ""
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target_name, value = node.target.id, node.value
        written = _statement_mutation_root_names(node)
        if not target_name or value is None:
            mark_uncertain(written)
            continue
        mark_uncertain(written - {target_name})
        if target_name in assignments or target_name in uncertain_names:
            mark_uncertain({target_name})
        else:
            assignments[target_name] = value
    return assignments, uncertain_names


def _resolve_static_expression(
    expression: ast.expr,
    assignments: Mapping[str, ast.expr],
    uncertain_names: set[str],
    seen_names: frozenset[str] = frozenset(),
) -> ast.expr | None:
    if isinstance(expression, ast.Await):
        return _resolve_static_expression(expression.value, assignments, uncertain_names, seen_names)
    if not isinstance(expression, ast.Name):
        return expression
    if expression.id in uncertain_names or expression.id in seen_names:
        return None
    assigned = assignments.get(expression.id)
    if assigned is None:
        return None
    return _resolve_static_expression(assigned, assignments, uncertain_names, seen_names | {expression.id})


_RootOutputEnvelopeState = Literal["proven", "absent", "unknown"]


def _root_output_expression_state(
    expression: ast.expr,
    assignments: Mapping[str, ast.expr],
    uncertain_names: set[str],
) -> _RootOutputEnvelopeState:
    resolved = _resolve_static_expression(expression, assignments, uncertain_names)
    if resolved is None:
        return "unknown"
    if not isinstance(resolved, ast.Dict):
        return "absent" if isinstance(resolved, (ast.Constant, ast.List, ast.Set, ast.Tuple)) else "unknown"
    literal_keys: list[str] = []
    for key in resolved.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return "unknown"
        literal_keys.append(key.value)
    return "proven" if _VALUE_BEARING_ROOT_GUIDANCE_PATH in literal_keys else "absent"


def _root_output_envelope_state(code: str) -> _RootOutputEnvelopeState:
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
        return_nodes = [node for node in _iter_top_level_scope(tree.body) if isinstance(node, ast.Return)]
        if not return_nodes:
            return "absent"
        states: set[_RootOutputEnvelopeState] = set()
        for node in return_nodes:
            if node.value is None:
                states.add("absent")
                continue
            prior_body = [
                statement for statement in tree.body if (statement.end_lineno or statement.lineno) < node.lineno
            ]
            assignments, uncertain_names = _top_level_static_assignments(ast.Module(body=prior_body, type_ignores=[]))
            states.add(_root_output_expression_state(node.value, assignments, uncertain_names))
        if "absent" in states:
            return "absent"
        if "unknown" in states:
            return "unknown"
        return "proven"
    except (RecursionError, SyntaxError, ValueError):
        return "unknown"


_StaticOutputPathState = Literal["value", "empty", "absent", "unknown"]


def _static_leaf_value_state(expression: ast.expr) -> _StaticOutputPathState:
    if isinstance(expression, ast.Constant):
        if expression.value is None or (isinstance(expression.value, str) and not expression.value.strip()):
            return "empty"
        return "value"
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        element_states = {_static_leaf_value_state(element) for element in expression.elts}
        return "value" if element_states & {"value", "unknown"} else "empty"
    if isinstance(expression, ast.Dict):
        value_states = {_static_leaf_value_state(value) for value in expression.values}
        return "value" if value_states & {"value", "unknown"} else "empty"
    return "unknown"


def _static_output_path_state(
    expression: ast.expr,
    segments: list[tuple[str, bool]],
    assignments: Mapping[str, ast.expr],
    uncertain_names: set[str],
) -> _StaticOutputPathState:
    resolved = _resolve_static_expression(expression, assignments, uncertain_names)
    if resolved is None:
        return "unknown"
    if not segments:
        return _static_leaf_value_state(resolved)
    key, is_array = segments[0]
    if not isinstance(resolved, ast.Dict):
        return "unknown"
    matching_values: list[ast.expr] = []
    dynamic_key = False
    dynamic_key_after_match = False
    for raw_key, value in zip(resolved.keys, resolved.values):
        if isinstance(raw_key, ast.Constant) and raw_key.value == key:
            matching_values.append(value)
            dynamic_key_after_match = False
        elif not isinstance(raw_key, ast.Constant) or not isinstance(raw_key.value, str):
            dynamic_key = True
            dynamic_key_after_match = bool(matching_values)
    if not matching_values:
        return "unknown" if dynamic_key else "absent"
    # A dynamic entry after the last matching literal can shadow it at runtime.
    if dynamic_key_after_match:
        return "unknown"
    value = matching_values[-1]
    remaining = segments[1:]
    if not is_array:
        return _static_output_path_state(value, remaining, assignments, uncertain_names)
    resolved_value = _resolve_static_expression(value, assignments, uncertain_names)
    if not isinstance(resolved_value, (ast.List, ast.Tuple, ast.Set)):
        return "unknown"
    if not resolved_value.elts:
        return "empty"
    if not remaining:
        return "value"
    states = [
        _static_output_path_state(element, remaining, assignments, uncertain_names) for element in resolved_value.elts
    ]
    if "value" in states:
        return "value"
    return "unknown" if "unknown" in states else "empty"


def _statically_lacks_value_bearing_observation_paths(code: str, observation_paths: set[str]) -> bool:
    if not observation_paths:
        return False
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
        return_expressions = [
            node.value
            for node in _iter_top_level_scope(tree.body)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        if not return_expressions:
            return False
        assignments, uncertain_names = _top_level_static_assignments(tree)
        for expression in return_expressions:
            if not isinstance(_resolve_static_expression(expression, assignments, uncertain_names), ast.Dict):
                return False
            states = {
                _static_output_path_state(expression, _path_segments(path), assignments, uncertain_names)
                for path in observation_paths
            }
            if states & {"value", "unknown"}:
                return False
        return True
    except (RecursionError, SyntaxError, ValueError):
        return False


def _static_value_bearing_violations(code: str, observation_paths: set[str]) -> list[str]:
    if not _statically_lacks_value_bearing_observation_paths(code, observation_paths):
        return []
    return [
        "Unable to persist output contract: selected output extraction returns only statically "
        f"empty value(s) for required output path(s): {', '.join(sorted(observation_paths))}."
    ]


def _evaluate_output_contract_for_code_block(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    *,
    enforce_value_bearing_liveness: bool = False,
) -> _OutputContractEvaluation | None:
    """Evaluate factual metadata, schema, and return-path coverage for an authored code block."""
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    runtime_contract = _runtime_output_repair_contract_from_recorded_outcome(ctx)
    contract = _output_contract_required_paths_source(ctx)
    required_paths = _value_bearing_directive_paths(contract) if enforce_value_bearing_liveness else set(contract.union)
    observation_paths = contract.observation_paths
    declaration_paths = contract.declaration_paths
    source = contract.source
    reason_code = contract.reason_code
    if not required_paths and contract.liveness is not _OutputContractLiveness.DEGRADED_EMPTY:
        return None
    effective_metadata = raw_code_artifact_metadata
    if not _metadata_has_mapping_item(effective_metadata):
        existing_metadata = getattr(ctx, "code_artifact_metadata", None)
        if _metadata_has_mapping_item(existing_metadata):
            effective_metadata = existing_metadata
    block_label, owner_labels = _target_output_contract_block_label(
        ctx,
        workflow_yaml,
        effective_metadata,
        observation_paths,
    )
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    target_block = code_blocks.get(block_label) if block_label else None
    target_metadata = _metadata_item_for_block_label(effective_metadata, block_label) if block_label else None
    submitted_goal_paths = _metadata_item_goal_value_paths(target_metadata)
    submitted_schema_paths = _metadata_item_extraction_schema_paths(target_metadata) if target_metadata else set()
    target_code = str(target_block.get("code") or "") if target_block is not None else ""
    submitted_code_paths = _code_block_produced_output_paths(target_code)
    missing_metadata_paths = sorted(observation_paths - submitted_goal_paths)
    missing_schema_paths = sorted(required_paths - submitted_schema_paths)
    missing_return_paths = sorted(required_paths - submitted_code_paths)
    shape_violations: list[str] = []
    declaration_only_contract = bool(declaration_paths) and not observation_paths

    def _root_envelope_unproved(paths: set[str]) -> bool:
        if paths != {_VALUE_BEARING_ROOT_GUIDANCE_PATH}:
            return False
        return _root_output_envelope_state(target_code) != "proven"

    if enforce_value_bearing_liveness:
        if contract.liveness is _OutputContractLiveness.DEGRADED_EMPTY or declaration_only_contract:
            if (
                target_block is None
                or _root_envelope_unproved(required_paths)
                or _statically_lacks_value_bearing_observation_paths(target_code, required_paths)
            ):
                shape_violations.append(_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE)
        elif target_block is not None:
            if _root_envelope_unproved(observation_paths) or _statically_lacks_value_bearing_observation_paths(
                target_code, observation_paths
            ):
                shape_violations.append(_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE)
    if not block_label:
        shape_violations.append("ambiguous_output_owner" if owner_labels else "missing_output_owner")
    elif target_block is None:
        shape_violations.append("missing_output_block")

    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    effective_missing_return_paths = missing_return_paths
    runtime_signature = _runtime_output_contract_signature(runtime_contract)
    metadata_repair_contract = (
        _metadata_repair_contract(
            block_labels=[block_label],
            required_paths=observation_paths,
            source=source,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
        )
        if block_label
        else None
    )
    repair = _metadata_output_repair_context(
        block_labels=[block_label] if block_label else [],
        required_paths=observation_paths,
        coverage_reason_code=reason_code,
        source=source,
        summary="Submitted workflow does not satisfy the requested output contract.",
        declaration_paths=declaration_paths,
    )
    missing_paths = sorted(
        set(missing_metadata_paths)
        | set(missing_schema_paths)
        | set(effective_missing_return_paths)
        | (required_paths if shape_violations else set())
    )
    value_bearing_output_required = _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE in shape_violations
    payload: dict[str, Any] = {
        "reason_code": (
            _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE
            if value_bearing_output_required
            else _OUTPUT_CONTRACT_REJECT_REASON_CODE
        ),
        "block_label": block_label,
        "canonical_required_child_paths": sorted(required_paths),
        "declaration_only_child_paths": sorted(declaration_paths),
        "contract_liveness": contract.liveness.value,
        "degraded_request_slots": [slot.to_payload() for slot in contract.degraded_request_slots],
        "source": source,
        "metadata_contract_source": source,
        "metadata_contract_reason_code": reason_code,
        "missing_goal_value_paths": missing_metadata_paths,
        "missing_extraction_schema_paths": missing_schema_paths,
        "missing_code_return_paths": effective_missing_return_paths,
        "shape_violations": shape_violations,
        "reject_reason": (
            _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE
            if value_bearing_output_required
            else _OUTPUT_CONTRACT_REJECT_REASON_CODE
        ),
        "canonical_output_contract_signature": signature,
        "canonical_runtime_output_contract_signature": runtime_signature,
        "runtime_output_workflow_run_id": runtime_contract.workflow_run_id if runtime_contract is not None else "",
        "runtime_output_repair_facts": runtime_contract.facts if runtime_contract is not None else [],
        "output_owner_labels": owner_labels,
        "metadata_repair_contract": metadata_repair_contract,
        "missing_requested_output_facts": _missing_requested_output_facts(
            missing_paths,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
        ),
    }
    progress_data = _code_repair_progress_data(
        repair,
        missing_requested_output_facts=payload["missing_requested_output_facts"],
        metadata_repair_contract=metadata_repair_contract,
    )
    progress_data.update(payload)
    return _OutputContractEvaluation(
        block_label=block_label,
        required_paths=required_paths,
        observation_paths=observation_paths,
        declaration_paths=declaration_paths,
        source=source,
        reason_code=reason_code,
        missing_metadata_paths=missing_metadata_paths,
        missing_schema_paths=missing_schema_paths,
        missing_return_paths=effective_missing_return_paths,
        shape_violations=shape_violations,
        canonical_signature=signature,
        payload=progress_data,
        repair_context=repair,
    )


_METADATA_CONVERGENCE_DIRECTIVE_BLOCKER = "missing_code_artifact_metadata"


def _output_path_root(path: str) -> str:
    return path.split(".", 1)[0].split("[", 1)[0].strip()


def _output_path_has_child(path: str) -> bool:
    return "." in path or "[" in path


def _meaningful_runtime_output_roots(value: object, *, prefix: str = "") -> set[str]:
    roots: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not key or key == "evidence_text" or not _is_structural_runtime_output_key(key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            if _runtime_output_value_is_meaningful(child):
                roots.add(path.split(".", 1)[0])
                roots.update(_meaningful_runtime_output_roots(child, prefix=path))
        return roots
    if isinstance(value, list):
        for item in value:
            roots.update(_meaningful_runtime_output_roots(item, prefix=prefix))
    return roots


def _runtime_output_value_is_meaningful(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_runtime_output_value_is_meaningful(item) for item in value.values())
    if isinstance(value, list):
        return any(_runtime_output_value_is_meaningful(item) for item in value)
    return True


class _ProducedOutputRoots(NamedTuple):
    roots: set[str]
    abstained: bool = False


def _code_block_produced_output_paths(code: str) -> set[str]:
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return set()
    scope_statements = list(_iter_top_level_scope(tree.body))
    dict_assignments: dict[str, set[str]] = {}
    helper_return_paths = _helper_function_literal_return_paths(tree.body)
    returned_paths: set[str] = set()
    for node in scope_statements:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            dict_paths = _dict_literal_string_key_paths(node.value, dict_assignments)
            if dict_paths:
                dict_assignments[node.targets[0].id] = dict_paths
        elif isinstance(node, ast.Assign):
            _apply_literal_dict_key_assignment(dict_assignments, node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                dict_assignments[node.target.id] = _dict_literal_string_key_paths(node.value, dict_assignments)
            _apply_literal_dict_key_assignment(dict_assignments, node)
        elif isinstance(node, ast.AugAssign):
            _apply_literal_dict_key_assignment(dict_assignments, node)
        elif isinstance(node, ast.Return) and node.value is not None:
            returned_paths.update(_return_output_paths(node.value, dict_assignments, helper_return_paths))
    return returned_paths


def _output_path_direct_child(path: str, root: str) -> str:
    if not path.startswith(root + "."):
        return ""
    child = path[len(root) + 1 :]
    return re.split(r"[.\[]", child, maxsplit=1)[0].strip()


def _return_scaffold_name_is_safe(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _dict_subscript_target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        return target.value.id
    return None


def _literal_dict_key_assignment(target: ast.expr) -> tuple[str, str] | None:
    if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
        return None
    key_node = target.slice
    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str) and key_node.value.strip():
        return target.value.id, key_node.value.strip()
    return None


def _apply_literal_dict_key_assignment(
    dict_assignments: dict[str, set[str]],
    dynamic_dict_assignment_names_or_node: set[str] | ast.Assign | ast.AnnAssign | ast.AugAssign,
    node: ast.Assign | ast.AnnAssign | ast.AugAssign | None = None,
) -> None:
    dynamic_dict_assignment_names: set[str] | None
    if node is None:
        dynamic_dict_assignment_names = None
        node = cast(ast.Assign | ast.AnnAssign | ast.AugAssign, dynamic_dict_assignment_names_or_node)
    else:
        dynamic_dict_assignment_names = cast(set[str], dynamic_dict_assignment_names_or_node)
    targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        assignment = _literal_dict_key_assignment(target)
        target_name = _dict_subscript_target_name(target)
        if assignment is None:
            if dynamic_dict_assignment_names is not None and target_name in dict_assignments:
                dynamic_dict_assignment_names.add(target_name)
            continue
        name, key = assignment
        if name in dict_assignments:
            dict_assignments[name].add(key)


def _return_output_roots(
    node: ast.expr,
    dict_assignments: Mapping[str, set[str]],
    dynamic_dict_assignment_names: set[str],
    helper_return_roots: Mapping[str, _ProducedOutputRoots],
) -> _ProducedOutputRoots:
    if isinstance(node, ast.Await):
        return _return_output_roots(node.value, dict_assignments, dynamic_dict_assignment_names, helper_return_roots)
    if isinstance(node, ast.Name):
        if node.id in dict_assignments:
            return _ProducedOutputRoots(
                set(dict_assignments.get(node.id, set())), node.id in dynamic_dict_assignment_names
            )
        return _ProducedOutputRoots(set(), True)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return helper_return_roots.get(node.func.id, _ProducedOutputRoots(set(), True))
    if isinstance(node, ast.Dict):
        return _dict_literal_output_roots(node)
    if isinstance(node, ast.List):
        return _list_literal_output_roots(node)
    if isinstance(node, (ast.Constant, ast.Tuple, ast.Set)):
        return _ProducedOutputRoots(set(), False)
    return _ProducedOutputRoots(set(), True)


def _return_output_paths(
    node: ast.expr,
    dict_assignments: Mapping[str, set[str]],
    helper_return_paths: Mapping[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Await):
        return _return_output_paths(node.value, dict_assignments, helper_return_paths)
    if isinstance(node, ast.Name):
        return set(dict_assignments.get(node.id, set()))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return set(helper_return_paths.get(node.func.id, set()))
    return _dict_literal_string_key_paths(node, dict_assignments)


def _list_literal_output_roots(node: ast.List) -> _ProducedOutputRoots:
    if not node.elts:
        return _ProducedOutputRoots(set(), False)
    if all(isinstance(element, ast.Dict) for element in node.elts):
        roots: set[str] = set()
        abstained = False
        for element in node.elts:
            produced = _dict_literal_output_roots(cast(ast.Dict, element))
            roots.update(produced.roots)
            abstained = abstained or produced.abstained
        return _ProducedOutputRoots(roots, abstained)
    if all(isinstance(element, ast.Constant) for element in node.elts):
        return _ProducedOutputRoots(set(), False)
    return _ProducedOutputRoots(set(), True)


def _helper_function_literal_return_paths(statements: list[ast.stmt]) -> dict[str, set[str]]:
    helpers: dict[str, set[str]] = {}
    for statement in statements:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dict_assignments: dict[str, set[str]] = {}
        paths: set[str] = set()
        for helper_statement in _iter_top_level_scope(statement.body):
            if (
                isinstance(helper_statement, ast.Assign)
                and len(helper_statement.targets) == 1
                and isinstance(helper_statement.targets[0], ast.Name)
            ):
                dict_paths = _dict_literal_string_key_paths(helper_statement.value, dict_assignments)
                if isinstance(helper_statement.value, ast.Dict):
                    dict_assignments[helper_statement.targets[0].id] = dict_paths
            elif isinstance(helper_statement, ast.Assign):
                _apply_literal_dict_key_assignment(dict_assignments, helper_statement)
            elif isinstance(helper_statement, ast.AnnAssign):
                if isinstance(helper_statement.target, ast.Name) and isinstance(helper_statement.value, ast.Dict):
                    dict_assignments[helper_statement.target.id] = _dict_literal_string_key_paths(
                        helper_statement.value, dict_assignments
                    )
                _apply_literal_dict_key_assignment(dict_assignments, helper_statement)
            elif isinstance(helper_statement, ast.AugAssign):
                _apply_literal_dict_key_assignment(dict_assignments, helper_statement)
            elif isinstance(helper_statement, ast.Return) and helper_statement.value is not None:
                paths.update(_return_output_paths(helper_statement.value, dict_assignments, {}))
        if paths:
            helpers[statement.name] = paths
    return helpers


def _dict_literal_output_roots(node: ast.Dict) -> _ProducedOutputRoots:
    roots: set[str] = set()
    abstained = False
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.strip():
            roots.add(key.value.strip())
        else:
            abstained = True
    return _ProducedOutputRoots(roots, abstained)


def _dict_literal_string_key_paths(
    node: ast.expr,
    dict_assignments: Mapping[str, set[str]],
    *,
    prefix: str = "",
) -> set[str]:
    if isinstance(node, ast.List):
        array_prefix = f"{prefix}[]" if prefix else "[]"
        array_paths: set[str] = set()
        for item in node.elts:
            array_paths.update(_dict_literal_string_key_paths(item, dict_assignments, prefix=array_prefix))
        return array_paths
    if not isinstance(node, ast.Dict):
        return set()
    paths: set[str] = set()
    for key_node, value_node in zip(node.keys, node.values):
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str) and key_node.value.strip()):
            continue
        path = f"{prefix}.{key_node.value.strip()}" if prefix else key_node.value.strip()
        paths.add(path)
        if isinstance(value_node, ast.Dict):
            paths.update(_dict_literal_string_key_paths(value_node, dict_assignments, prefix=path))
        elif isinstance(value_node, ast.List):
            array_prefix = f"{path}[]"
            for item in value_node.elts:
                paths.update(_dict_literal_string_key_paths(item, dict_assignments, prefix=array_prefix))
        elif isinstance(value_node, ast.Name):
            for child_path in dict_assignments.get(value_node.id, set()):
                paths.add(f"{path}{child_path}" if child_path.startswith("[]") else f"{path}.{child_path}")
    return paths


def _locator_alias_selectors(tree: ast.AST) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}

    def record(target: ast.AST, value: ast.AST | None) -> None:
        if not isinstance(target, ast.Name) or value is None:
            return
        selectors = _locator_receiver_selectors(value, aliases)
        if selectors:
            aliases[target.id] = selectors

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target, node.value)
            continue
        if isinstance(node, ast.AnnAssign):
            record(node.target, node.value)
    return aliases


def _locator_receiver_selectors(node: ast.AST, aliases: Mapping[str, set[str]]) -> set[str]:
    while isinstance(node, ast.Attribute) and node.attr in {"first", "last"}:
        node = node.value
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, set()))
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return set()
    if node.func.attr in {"first", "last", "nth", "filter"}:
        return _locator_receiver_selectors(node.func.value, aliases)
    if node.func.attr != "locator" or not node.args:
        return set()
    receiver = node.func.value
    if not isinstance(receiver, ast.Name) or receiver.id != "page":
        return set()
    selector = node.args[0]
    if isinstance(selector, ast.Constant) and isinstance(selector.value, str):
        value = selector.value.strip()
        return {value} if value else set()
    return set()


def _call_chain_contains_method(node: ast.AST, method_name: str) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == method_name:
            return True
        return _call_chain_contains_method(node.func.value, method_name)
    if isinstance(node, ast.Attribute):
        return _call_chain_contains_method(node.value, method_name)
    return False


def _return_value_is_meaningful(
    node: ast.expr | None,
    helper_return_roots: Mapping[str, _ProducedOutputRoots],
) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Await):
        return _return_value_is_meaningful(node.value, helper_return_roots)
    if isinstance(node, ast.Constant) and node.value is None:
        return False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        produced = helper_return_roots.get(node.func.id)
        if produced is not None:
            return bool(produced.roots) or produced.abstained
        return True
    if (
        isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set))
        and not getattr(node, "elts", None)
        and not getattr(node, "keys", None)
    ):
        return False
    return True


def _code_block_parameter_keys(block: Mapping[str, Any]) -> frozenset[str]:
    raw_keys = block.get("parameter_keys")
    keys = {key for key in raw_keys if isinstance(key, str) and key} if isinstance(raw_keys, list) else set()
    # Synthesized blocks may submit full parameter rows before the persist seam
    # re-derives them into `parameter_keys`, so validate both sources.
    raw_parameters = block.get("parameters")
    if isinstance(raw_parameters, list):
        keys.update(
            str(parameter.get("key") or "").strip()
            for parameter in raw_parameters
            if isinstance(parameter, Mapping) and str(parameter.get("key") or "").strip()
        )
    return frozenset(keys)


def _conflict_marker_for_line(line: str) -> str | None:
    # Match marker text after incidental whitespace, while callers decide
    # whether indented markers are valid YAML string content for their surface.
    stripped = line.strip()
    if not stripped:
        return None
    if stripped == "=======":
        return stripped
    for prefix in ("<<<<<<<", ">>>>>>>"):
        if stripped == prefix or stripped.startswith(f"{prefix} "):
            return stripped
    return None


def _raw_workflow_yaml_conflict_marker_error(workflow_yaml: str) -> str | None:
    for line_number, line in enumerate(workflow_yaml.splitlines(), start=1):
        marker = _conflict_marker_for_line(line)
        if marker is not None and line == line.lstrip():
            return (
                f"Workflow YAML contains unresolved conflict marker `{marker}` on line {line_number}. "
                "Remove every git conflict marker line and submit valid workflow YAML before retrying."
            )
    return None


_ORDERED_CHILD_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_ORDERED_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")


def _code_repair_progress_data(
    repair_context: CodeAuthoringRepairContext | None = None,
    *,
    missing_requested_output_facts: list[dict[str, object]] | None = None,
    metadata_repair_contract: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Tag a code-authoring reject so the streaming adapter renders it as quiet de-duplicated progress."""
    data: dict[str, Any] = {
        "surface_kind": CODE_REPAIR_PROGRESS_SURFACE_KIND,
        "progress_text": CODE_REPAIR_PROGRESS_TEXT,
    }
    if repair_context is not None:
        data["authoring_repair_context"] = repair_context.model_dump(mode="json")
    if missing_requested_output_facts:
        data["missing_requested_output_facts"] = missing_requested_output_facts
    if metadata_repair_contract:
        data["metadata_repair_contract"] = metadata_repair_contract
    return data


def _code_seam_rejection_user_summary(*, metadata_rejected: bool, code_rejected: bool) -> str:
    if metadata_rejected and code_rejected:
        return "I need to adjust the workflow's code and its verification details before testing."
    if code_rejected:
        return "I need to adjust the workflow's code so it can run safely before testing."
    return "I need to adjust how the workflow verifies its results before testing."


def _compiled_authoring_user_summary() -> str:
    return "I need to bind the compiled browser-step code safely before saving this workflow."


def _workflow_code_blocks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for block in workflow_blocks(parsed)
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value
    ]


def _raw_metadata_item_mapping(raw_item: Any) -> Mapping[str, Any] | None:
    if isinstance(raw_item, CodeArtifactMetadata):
        return raw_item.model_dump(mode="json", exclude_none=True)
    if isinstance(raw_item, Mapping):
        return raw_item
    return None


def _raw_metadata_covers_output_label(raw_metadata: Any, label: str, *, candidate_labels: list[str]) -> bool:
    if not label:
        return False
    unlabeled_declares_goal_values = False
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        item_label = str(item.get("block_label") or "").strip()
        if item_label == label and _artifact_declares_goal_values(item):
            return True
        if not item_label and _artifact_declares_goal_values(item):
            unlabeled_declares_goal_values = True
    return len(candidate_labels) == 1 and unlabeled_declares_goal_values


def _raw_metadata_declares_goal_values_for_block(raw_metadata: Any, label: str) -> bool:
    if not label:
        return False
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        item_label = str(item.get("block_label") or "").strip()
        if item_label and item_label != label:
            continue
        if _artifact_declares_goal_values(item):
            return True
    return False


_FLAT_STRING_TEXT_METHODS = frozenset({"inner_text", "text_content", "inner_html", "content"})


def _expr_is_flat_string(node: ast.expr, string_locals: set[str]) -> bool:
    """True only for expressions that are unambiguously a single text blob: a string
    literal/f-string, a `.inner_text()/.text_content()/.inner_html()` read, a
    `str(...)` cast, a `str.join(...)`, string concatenation, or a local bound to one
    of those. Anything ambiguous returns False so the validator never false-positives."""
    if isinstance(node, ast.Await):
        return _expr_is_flat_string(node.value, string_locals)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Name):
        return node.id in string_locals
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _expr_is_flat_string(node.left, string_locals) or _expr_is_flat_string(node.right, string_locals)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _FLAT_STRING_TEXT_METHODS:
                return True
            if func.attr == "join":
                return True
            if func.attr in {"strip", "lstrip", "rstrip", "lower", "upper"}:
                return _expr_is_flat_string(func.value, string_locals)
        if isinstance(func, ast.Name) and func.id == "str":
            return True
    return False


def _expr_is_structured(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        return _expr_is_structured(node.value)
    return isinstance(node, (ast.Dict, ast.List, ast.DictComp, ast.ListComp, ast.SetComp, ast.Set, ast.Tuple))


_NESTED_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _iter_top_level_scope(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements in the snippet's own scope, descending into control flow
    (if/for/while/with/try) but never into nested function/class bodies."""
    for statement in statements:
        yield statement
        if isinstance(statement, _NESTED_SCOPE_NODES):
            continue
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                yield from _iter_top_level_scope([child])
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                yield from _iter_top_level_scope(child.body)


def _code_block_returns_flat_string(code: str) -> bool:
    """True when every top-level `return` in the snippet yields a flat text blob and
    none yields a structured value. Returns inside nested functions, and indeterminate
    or structured returns, are not flagged."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False

    scope_statements = list(_iter_top_level_scope(tree.body))
    string_locals: set[str] = set()
    for node in scope_statements:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if _expr_is_flat_string(node.value, string_locals):
                string_locals.add(name)
            else:
                string_locals.discard(name)

    returns = [node for node in scope_statements if isinstance(node, ast.Return) and node.value is not None]
    if not returns:
        return False
    if any(_expr_is_structured(node.value) for node in returns if node.value is not None):
        return False
    return all(_expr_is_flat_string(node.value, string_locals) for node in returns if node.value is not None)


def _function_body_has_structured_return(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    own_scope = list(_iter_top_level_scope(node.body))
    structured_locals: set[str] = set()
    for inner in own_scope:
        if isinstance(inner, ast.Assign) and len(inner.targets) == 1 and isinstance(inner.targets[0], ast.Name):
            name = inner.targets[0].id
            if _expr_is_structured(inner.value):
                structured_locals.add(name)
            else:
                structured_locals.discard(name)
    for inner in own_scope:
        if not isinstance(inner, ast.Return) or inner.value is None:
            continue
        if _expr_is_structured(inner.value):
            return True
        if isinstance(inner.value, ast.Name) and inner.value.id in structured_locals:
            return True
    return False


def _name_loaded_in(statements: list[ast.stmt], name: str, *, skip: ast.AST) -> bool:
    skip_nodes = set(ast.walk(skip))
    for statement in statements:
        for inner in ast.walk(statement):
            if inner in skip_nodes:
                continue
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id == name:
                return True
    return False


def _code_block_returns_uninvoked_structured_function(code: str) -> bool:
    """True when the snippet's only structured data lives in a nested function that the
    top-level scope never invokes, returns, or binds — the shared wrapper then captures
    the function object instead of its data. Top-level structured returns or structured
    local bindings (legit implicit capture) are not flagged, and anything indeterminate
    returns False."""
    # CodeBlock wraps the snippet and appends `return __capture_locals()`, so a nested
    # function defined-but-never-called is captured as a function object, not its data.
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False

    scope_statements = list(_iter_top_level_scope(tree.body))
    for statement in scope_statements:
        if isinstance(statement, ast.Return) and statement.value is not None and _expr_is_structured(statement.value):
            return False
        if isinstance(statement, ast.Assign) and _expr_is_structured(statement.value):
            return False

    structured_functions = [
        statement
        for statement in scope_statements
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _function_body_has_structured_return(statement)
    ]
    if not structured_functions:
        return False
    return all(not _name_loaded_in(scope_statements, function.name, skip=function) for function in structured_functions)


def _artifact_declares_goal_values(artifact: Mapping[str, Any]) -> bool:
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            if _artifact_goal_value_paths(row.get("goal_value_paths")):
                return True
    return False


def _artifact_goal_value_roots(artifact: Mapping[str, Any]) -> set[str]:
    roots: set[str] = set()
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            for path in _artifact_goal_value_paths(row.get("goal_value_paths")):
                root = path.split(".", 1)[0].split("[", 1)[0].strip()
                if root:
                    roots.add(root)
    return roots


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_target_names(element))
        return names
    return set()


def _pattern_bound_names(pattern: ast.pattern) -> set[str]:
    if isinstance(pattern, ast.MatchAs):
        bound_names = {pattern.name} if pattern.name else set()
        if pattern.pattern is not None:
            bound_names.update(_pattern_bound_names(pattern.pattern))
        return bound_names
    if isinstance(pattern, ast.MatchStar):
        return {pattern.name} if pattern.name else set()
    names: set[str] = set()
    for child in ast.iter_child_nodes(pattern):
        if isinstance(child, ast.pattern):
            names.update(_pattern_bound_names(child))
    return names


def _assigned_top_level_names(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in _iter_top_level_scope(statements):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_target_names(node.target))
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            names.update(_target_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_target_names(node.target))
    return names


def _submitted_scope_nodes(tree: ast.AST) -> list[ast.AST]:
    if (
        isinstance(tree, ast.Module)
        and len(tree.body) == 1
        and isinstance(tree.body[0], (ast.AsyncFunctionDef, ast.FunctionDef))
    ):
        roots: list[ast.AST] = list(tree.body[0].body)
    else:
        roots = [tree]
    nodes: list[ast.AST] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes


def _assigned_submitted_parameter_keys(code: str, parameter_keys: list[str]) -> list[str]:
    protected = {key for key in parameter_keys if key}
    if not protected:
        return []
    tree = _wrapped_code_ast(code)
    if tree is None:
        return []
    assigned: set[str] = set()
    for node in _submitted_scope_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assigned.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            assigned.update(_target_names(node.target))
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            assigned.update(_target_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            assigned.update(_target_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    assigned.update(_target_names(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            assigned.add(node.name)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                assigned.update(_pattern_bound_names(case.pattern))
    return sorted(assigned & protected)


def _missing_declared_output_roots(code: str, goal_roots: set[str]) -> set[str] | None:
    if not goal_roots:
        return None
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        # Code safety reports syntax errors first; avoid layering output-root errors onto invalid code.
        return None
    scope_statements = list(_iter_top_level_scope(tree.body))
    if any(isinstance(node, ast.Return) and node.value is not None for node in scope_statements):
        return None
    assigned_names = _assigned_top_level_names(tree.body)
    missing = goal_roots - assigned_names
    return missing or None


def _extraction_return_shape_error(
    label: str,
    artifact: Mapping[str, Any],
    code: str,
    *,
    require_declared_output: bool = False,
) -> str | None:
    """Reject an extraction-intent code block whose declared goal values do not reach
    the block output as a keyed structure: a flat text blob, or structured data trapped
    in an uninvoked nested function. Extraction-intent is the existing `goal_value_paths`
    signal; non-extraction blocks are never subject to this."""
    if not _artifact_declares_goal_values(artifact) or not code.strip():
        return None
    if _is_download_intent(artifact, code):
        return None
    if _code_block_returns_flat_string(code):
        return (
            f"Code block `{label}` declares `goal_value_paths` but `return`s a flat text blob "
            "(e.g. `page.inner_text(...)`/`text_content(...)`). Return a keyed structure instead: a dict, or an "
            "array of objects for repeated records, whose declared goal values resolve to named scalar fields "
            '(for example `return {"records": [{"number": "...", "expiration_date": "..."}]}`). A single value '
            'is fine as a keyed scalar (`{"<field>": value}`); do not array-wrap it.'
        )
    if _code_block_returns_uninvoked_structured_function(code):
        return (
            f"Code block `{label}` declares `goal_value_paths` but its structured `return` sits inside a nested "
            "function the top level never calls, so the block captures the function object instead of the data. "
            "Call that function and return its result (e.g. `return await run(page)`), or build the keyed "
            "structure at the top level so the declared goal values reach the block output."
        )
    if require_declared_output:
        missing_roots = _missing_declared_output_roots(code, _artifact_goal_value_roots(artifact))
        if missing_roots:
            missing = ", ".join(f"`{root}`" for root in sorted(missing_roots))
            return (
                f"Code block `{label}` declares `goal_value_paths` but does not return a keyed structure or leave "
                f"top-level output local(s) matching the declared path root(s): {missing}. Add an explicit "
                "structured `return` (a dict, or an array of objects for repeated records), or assign those "
                "top-level locals so the implicit code-block output contains the declared goal values."
            )
    return None


def _parse_extraction_schema(value: Any) -> dict[str, Any] | None:
    """Coerce a declared `extraction_schema` to a JSON-Schema object dict, or None when
    absent / disabled (`null`, empty) / still an unfilled `<fill...>` placeholder / not a
    parseable object. Accepts a JSON string (the tool-facing form) or an already-decoded
    dict (FE / direct callers)."""
    if isinstance(value, dict):
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
    return parsed if isinstance(parsed, dict) and parsed else None


def _artifact_extraction_schema_values(artifact: Mapping[str, Any]) -> list[Any]:
    return [value for value, _provenance in _artifact_extraction_schema_entries(artifact)]


def _artifact_extraction_schema_entries(artifact: Mapping[str, Any]) -> list[tuple[Any, ExtractionSchemaProvenance]]:
    values: list[tuple[Any, ExtractionSchemaProvenance]] = []
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            schema = row.get("extraction_schema")
            if schema is not None and not (isinstance(schema, str) and not schema.strip()):
                provenance: ExtractionSchemaProvenance = (
                    "self_authored" if row.get("extraction_schema_provenance") == "self_authored" else "user_edited"
                )
                values.append((schema, provenance))
    return values


def _schema_object_property_names(schema: Mapping[str, Any]) -> tuple[set[str], set[str]] | None:
    """Property names and required names for the record-level object of a data schema.

    Returns the (properties, required) name sets for an `object` schema, or for the
    `items` object of an `array` schema (records-style). Returns None when the schema
    declares no statically-readable record object, so reconciliation degrades to tolerant."""
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, Mapping):
            return _schema_object_property_names(items)
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    property_names = {str(name) for name in properties}
    required = schema.get("required")
    required_names = {str(name) for name in required} if isinstance(required, list) else set()
    return property_names, required_names & property_names


def _top_level_return_dict_keys(code: str) -> set[str] | None:
    """Top-level string keys of the snippet's returned dict literal (or the record
    objects inside a returned list literal). Returns None when no top-level dict/list
    literal return is statically determinable, so a dynamically-built return is never
    false-rejected and falls through to the runtime validate/fill pass."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return None

    return_values: list[ast.expr] = []
    for node in _iter_top_level_scope(tree.body):
        if isinstance(node, ast.Return) and node.value is not None:
            return_values.append(node.value)
    if not return_values:
        return None

    keys: set[str] = set()
    for value in return_values:
        unwrapped = value.value if isinstance(value, ast.Await) else value
        if isinstance(unwrapped, ast.Dict):
            keys |= _dict_keys(unwrapped)
        elif isinstance(unwrapped, ast.List):
            for element in unwrapped.elts:
                if isinstance(element, ast.Dict):
                    keys |= _dict_keys(element)
                else:
                    return None
        else:
            return None
    return keys


def _extraction_schema_conformance_error(label: str, artifact: Mapping[str, Any], code: str) -> str | None:
    """Enforce the confirmed `extraction_schema` against the authored return shape. Runs
    in addition to (never instead of) `_extraction_return_shape_error`. A declared-but-malformed
    schema is rejected; a top-level return dict literal whose keys omit a required schema field is
    rejected; a dynamically-built return cannot be statically reconciled and defers to the
    runtime validate/fill pass."""
    declared = _artifact_extraction_schema_values(artifact)
    schemas: list[dict[str, Any]] = []
    for value in declared:
        # An unfilled `<fill...>` slot is a not-yet-confirmed schema, not a malformed one;
        # leave it for the model to fill, the same way unfilled goal_value_paths are tolerated.
        if isinstance(value, str) and _is_unfilled_artifact_placeholder(value.strip()):
            continue
        parsed = _parse_extraction_schema(value)
        if parsed is None or not validate_schema(parsed):
            return (
                f"Code block `{label}` declares an `extraction_schema` that is not valid JSON Schema. "
                "Provide a JSON Schema (a JSON object with named fields and types, serialized as a string), or "
                "remove `extraction_schema` to fall back to `goal_value_paths` alone."
            )
        schemas.append(parsed)
    if not schemas:
        return None
    if _is_download_intent(artifact, code) or not code.strip():
        return None
    return_keys = _top_level_return_dict_keys(code)
    if return_keys is None:
        return None
    for schema in schemas:
        names = _schema_object_property_names(schema)
        if names is None:
            continue
        _property_names, required_names = names
        missing_required = sorted(required_names - return_keys)
        if missing_required:
            return (
                f"Code block `{label}` `return`s a keyed structure missing required field(s) "
                f"{', '.join(missing_required)} from the confirmed `extraction_schema`. Build the top-level "
                "return so every required schema field is a named key (a dict for one record, or an array of "
                "objects with those keys for repeated records)."
            )
    return None


def _top_level_path_segment(path: str) -> str:
    head = path.strip()
    for separator in (".", "[", "/"):
        index = head.find(separator)
        if index > 0:
            head = head[:index]
    return head.strip()


_STRUCTURAL_RUNTIME_OUTPUT_KEY_RE = re.compile(r"^[a-z]+(?:_[a-z]+)*(?:_[0-9])?$")
_SENSITIVE_RUNTIME_OUTPUT_KEY_TERMS = frozenset(
    {"api_key", "access_key", "password", "secret", "token", "credential", "email"}
)


def _is_structural_runtime_output_key(key: str) -> bool:
    return (
        _STRUCTURAL_RUNTIME_OUTPUT_KEY_RE.fullmatch(key) is not None
        and not keyword.iskeyword(key)
        and key not in _SENSITIVE_RUNTIME_OUTPUT_KEY_TERMS
        and not any(part in _SENSITIVE_RUNTIME_OUTPUT_KEY_TERMS for part in key.split("_"))
    )


def _verified_runtime_output_contract_paths(value: object, *, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not _is_structural_runtime_output_key(key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= _verified_runtime_output_contract_paths(child, prefix=path)
        return paths
    if isinstance(value, list):
        for item in value:
            paths |= _verified_runtime_output_contract_paths(item, prefix=prefix)
    return paths


def _verified_runtime_output_contract_paths_by_label(ctx: AgentContext, workflow_yaml: str) -> dict[str, set[str]]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return {}
    code_block_labels = set(_workflow_yaml_code_blocks_by_label(workflow_yaml))
    return {
        label: paths
        for label, output in ctx.verified_block_outputs.items()
        if label in code_block_labels and (paths := _verified_runtime_output_contract_paths(output))
    }


def _known_output_contract_paths(artifact: Mapping[str, Any], code: str) -> set[str]:
    """Top-level field names the block is known to produce: the snippet's return-dict
    keys plus the confirmed `goal_value_paths`' top-level segments. Empty when neither
    is statically determinable, so the incompatibility check stays tolerant."""
    paths: set[str] = set()
    return_keys = _top_level_return_dict_keys(code)
    if return_keys:
        paths |= return_keys
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            for path in _artifact_goal_value_paths(row.get("goal_value_paths")):
                segment = _top_level_path_segment(path)
                if segment:
                    paths.add(segment)
    return paths


def _schema_property_summary(schema: Mapping[str, Any]) -> str:
    names = _schema_object_property_names(schema)
    if names is None:
        return ""
    property_names, _required = names
    return ", ".join(sorted(property_names))


def _extraction_schema_incompatibility(
    label: str,
    artifact: Mapping[str, Any],
    code: str,
    *,
    verified_runtime_output_paths: set[str] | None = None,
) -> SchemaIncompatibility | None:
    """Detect an edited `extraction_schema` whose object property names overlap NONE of
    the block's known output contract. Unlike `_extraction_schema_conformance_error`,
    this fires even when `required` is empty: an optional-only field that maps to nothing
    the block produces is a non-repairable mismatch, not a tolerated gap. Stays tolerant
    when the contract or property names are not statically determinable."""
    if _is_download_intent(artifact, code) or not code.strip():
        return None
    known_paths = _known_output_contract_paths(artifact, code)
    if verified_runtime_output_paths:
        known_paths |= {_top_level_path_segment(path) for path in verified_runtime_output_paths}
    if not known_paths:
        return None
    incompatible: set[str] = set()
    summaries: list[str] = []
    for value, provenance in _artifact_extraction_schema_entries(artifact):
        if provenance != "user_edited":
            continue
        if isinstance(value, str) and _is_unfilled_artifact_placeholder(value.strip()):
            continue
        parsed = _parse_extraction_schema(value)
        if parsed is None or not validate_schema(parsed):
            continue
        names = _schema_object_property_names(parsed)
        if names is None:
            continue
        property_names, _required = names
        if not property_names or property_names & known_paths:
            continue
        incompatible |= property_names
        summary = _schema_property_summary(parsed)
        if summary and summary not in summaries:
            summaries.append(summary)
    if not incompatible:
        return None
    return SchemaIncompatibility(
        block_label=label,
        incompatible_paths=tuple(sorted(incompatible)),
        known_output_paths=tuple(sorted(known_paths)),
        edited_schema_summary="; ".join(summaries),
    )


_DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET = frozenset({"downloaded_file_path", "download_url"})


def _download_descriptor_leak_finding(label: str, code: str) -> str | None:
    """A run cannot reveal this: it succeeds, and a local filesystem path or raw download URL
    lands in workflow output. The execution layer owns those keys."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return None
    leak_key_locals: set[str] = set()
    leaked = False
    for node in _iter_top_level_scope(tree.body):
        if isinstance(node, ast.Return) and node.value is not None:
            if _dict_keys(node.value) & _DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET or (
                isinstance(node.value, ast.Name) and node.value.id in leak_key_locals
            ):
                leaked = True
                break
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if _dict_keys(node.value) & _DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET:
                leak_key_locals.add(node.targets[0].id)
    if not leaked:
        return None
    return (
        f"Code block `{label}` returns the raw descriptor keys "
        f"({', '.join(sorted(_DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET))}), which put a local filesystem path or download "
        "URL into workflow output. The execution layer owns those; return a small descriptor such as "
        '`{"saved_as": dl_info.value.suggested_filename}` instead.'
    )


def _dict_keys(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Await):
        return _dict_keys(node.value)
    if isinstance(node, ast.Dict):
        return {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    return set()


_REGISTERED_DOWNLOAD_OUTPUT_KEY_SET = frozenset(REGISTERED_DOWNLOAD_OUTPUT_KEYS)


def _code_returns_registration_keys(code: str) -> bool:
    """True when a top-level `return`/binding emits a dict literal carrying any
    execution-layer-owned registration key; writing those keys self-certifies a
    download the runtime never observed."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False
    for node in _iter_top_level_scope(tree.body):
        if isinstance(node, ast.Return) and node.value is not None:
            if _dict_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
        if isinstance(node, ast.Assign):
            if _dict_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
    return False


def _artifact_declares_registration_keys(artifact: Mapping[str, Any]) -> bool:
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            for path in _artifact_goal_value_paths(row.get("goal_value_paths")):
                head = path.split(".", 1)[0].split("[", 1)[0].strip()
                if head in _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                    return True
    return False


def _is_download_intent(artifact: Mapping[str, Any], code: str) -> bool:
    """Disjoint from extraction-intent (`goal_value_paths` on non-registration keys):
    a block is download-intent when it claims a download, self-asserts a registration key in a
    top-level dict, or declares a registration key as a goal path."""
    if not code.strip():
        return False
    return (
        code_uses_download_claim(code)
        or _code_returns_registration_keys(code)
        or _artifact_declares_registration_keys(artifact)
    )


def _code_artifact_metadata_shape_errors(
    label: str,
    artifact: Mapping[str, Any],
    *,
    reject_unfilled_goal_value_paths: bool = False,
) -> list[str]:
    """Return every shape violation for one artifact; the caller aggregates them."""
    errors: list[str] = []
    criteria_rows = _artifact_rows(artifact.get("completion_criteria"))
    terminal_criterion_ids = {
        str(row.get("id") or "").strip()
        for row in criteria_rows
        if row.get("terminal") is True or str(row.get("level") or "").strip() == "terminal"
    } - {""}
    # Populated while validating claimed outcomes, then used by terminal
    # verifier expectations below to require goal paths for terminal claims.
    terminal_claim_ids: set[str] = set()
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for index, row in enumerate(_artifact_rows(artifact.get(field_name))):
            schema = row.get("extraction_schema")
            if (
                isinstance(schema, str)
                and schema.strip()
                and not str(row.get("extraction_schema_provenance") or "").strip()
            ):
                errors.append(
                    f"Artifact metadata for `{label}` `{field_name}` entry {index} with `extraction_schema` "
                    "requires explicit `extraction_schema_provenance`."
                )
    for field_name, ref_key in (("evidence_refs", "evidence_ref"), ("observation_refs", "observation_ref")):
        for index, ref in enumerate(_artifact_rows(artifact.get(field_name))):
            if not str(ref.get(ref_key) or "").strip():
                errors.append(f"Artifact metadata for `{label}` `{field_name}` entry {index} requires `{ref_key}`.")
            if not any(str(ref.get(key) or "").strip() for key in ("claim_id", "dependency_id", "criterion_id")):
                errors.append(f"Artifact metadata for `{label}` `{field_name}` entry {index} requires a scoped id.")
            status = str(ref.get("status") or "").strip()
            if ref.get("checkpoint_next_mode") == "advance" and status != "diagnostic_only":
                errors.append(
                    f"Artifact metadata for `{label}` `{field_name}` entry {index} has "
                    "`checkpoint_next_mode=advance`; it must stay `diagnostic_only`."
                )
            if ref.get("checkpoint_next_mode") == "stop" and status not in {"observed_not_verified", "diagnostic_only"}:
                errors.append(
                    f"Artifact metadata for `{label}` `{field_name}` entry {index} has "
                    "`checkpoint_next_mode=stop`; it must remain `observed_not_verified` or `diagnostic_only`."
                )
            if status != "missing" and not str(ref.get("source_tool") or "").strip():
                errors.append(f"Artifact metadata for `{label}` `{field_name}` entry {index} requires `source_tool`.")

    for index, claim in enumerate(_artifact_rows(artifact.get("claimed_outcomes"))):
        claim_id = str(claim.get("id") or "").strip()
        if not _artifact_string_list(claim.get("depends_on")):
            errors.append(f"Artifact metadata claim `{claim_id or index}` for `{label}` requires `depends_on`.")
        claim_criteria = _artifact_string_list(claim.get("covered_criteria")) or _artifact_string_list(
            claim.get("criteria_ids")
        )
        claim_goal_value_paths = (
            _artifact_goal_value_paths(claim.get("goal_value_paths"))
            if reject_unfilled_goal_value_paths
            else _artifact_string_list(claim.get("goal_value_paths"))
        )
        if not claim_criteria:
            errors.append(f"Artifact metadata claim `{claim_id}` for `{label}` requires covered criterion ids.")
        if set(claim_criteria) & terminal_criterion_ids:
            if claim_id:
                terminal_claim_ids.add(claim_id)
            if reject_unfilled_goal_value_paths and _artifact_has_unfilled_goal_value_path(
                claim.get("goal_value_paths")
            ):
                errors.append(
                    f"Artifact metadata claim `{claim_id or index}` for `{label}` has unfilled "
                    "`goal_value_paths`; replace `<fill...>` placeholders with output JSON paths."
                )
            elif not claim_goal_value_paths:
                errors.append(
                    f"Artifact metadata claim `{claim_id or index}` for `{label}` covers a terminal criterion "
                    "and requires `goal_value_paths`."
                )
        claim_evidence_refs = _artifact_string_list(claim.get("evidence_refs"))
        claim_observation_refs = _artifact_string_list(claim.get("observation_refs"))
        if claim.get("status") == "satisfied" and not claim_evidence_refs:
            errors.append(
                f"Artifact metadata claim `{claim_id}` for `{label}` is `satisfied` but has no "
                "claim-scoped `evidence_refs`."
            )
        if claim.get("status") != "missing" and not claim_evidence_refs and not claim_observation_refs:
            errors.append(
                f"Artifact metadata claim `{claim_id}` for `{label}` requires claim-scoped "
                "`evidence_refs` or `observation_refs` unless status is `missing`."
            )

    for dependency in _artifact_rows(artifact.get("page_dependencies")):
        dependency_id = str(dependency.get("id") or "").strip()
        dependency_evidence_refs = _artifact_string_list(dependency.get("evidence_refs"))
        dependency_observation_refs = _artifact_string_list(dependency.get("observation_refs"))
        if dependency.get("status") == "satisfied" and not dependency_evidence_refs:
            errors.append(
                f"Artifact metadata dependency `{dependency_id}` for `{label}` is `satisfied` but has no "
                "dependency-scoped `evidence_refs`."
            )
        if dependency.get("status") != "missing" and not dependency_evidence_refs and not dependency_observation_refs:
            errors.append(
                f"Artifact metadata dependency `{dependency_id}` for `{label}` requires scoped "
                "`evidence_refs` or `observation_refs` unless status is `missing`."
            )

    for index, expectation in enumerate(_artifact_rows(artifact.get("terminal_verifier_expectations"))):
        expectation_id = str(expectation.get("id") or "").strip()
        expectation_criteria = _artifact_string_list(expectation.get("criteria_ids"))
        expectation_claims = _artifact_string_list(expectation.get("claimed_outcome_ids"))
        expectation_goal_value_paths = (
            _artifact_goal_value_paths(expectation.get("goal_value_paths"))
            if reject_unfilled_goal_value_paths
            else _artifact_string_list(expectation.get("goal_value_paths"))
        )
        if not expectation_criteria and not expectation_claims:
            errors.append(
                f"Artifact metadata terminal verifier expectation `{expectation_id or index}` for `{label}` "
                "requires `criteria_ids` or `claimed_outcome_ids`."
            )
        if set(expectation_criteria) & terminal_criterion_ids or set(expectation_claims) & terminal_claim_ids:
            if reject_unfilled_goal_value_paths and _artifact_has_unfilled_goal_value_path(
                expectation.get("goal_value_paths")
            ):
                errors.append(
                    f"Artifact metadata terminal verifier expectation `{expectation_id or index}` for `{label}` "
                    "has unfilled `goal_value_paths`; replace `<fill...>` placeholders with output JSON paths."
                )
            elif not expectation_goal_value_paths:
                errors.append(
                    f"Artifact metadata terminal verifier expectation `{expectation_id or index}` for `{label}` "
                    "requires `goal_value_paths` for terminal criteria."
                )

    for index, observation in enumerate(_artifact_rows(artifact.get("exploration_observations"))):
        if observation.get("status") != "observed_not_verified":
            errors.append(
                f"Artifact metadata for `{label}` exploration observation {index} must be marked "
                "`observed_not_verified` until authored execution and terminal verification pass."
            )
        if observation.get("checkpoint_next_mode") == "advance":
            errors.append(
                f"Artifact metadata for `{label}` exploration observation {index} cannot carry "
                "`checkpoint_next_mode=advance`; record that as `diagnostic_only` evidence instead."
            )
    return errors


def _artifact_rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _first_artifact_goal_value_paths(value: Any) -> list[str]:
    # Best-effort default propagation: preserve the first explicit contract
    # instead of inventing a union that may mix unrelated output shapes.
    for row in _artifact_rows(value):
        paths = _artifact_goal_value_paths(row.get("goal_value_paths"))
        if paths:
            return paths
    return []


def _artifact_goal_value_paths(value: Any) -> list[str]:
    # Keep in sync with blockers._metadata_goal_value_paths; duplicated locally
    # so authoring validation does not depend on runtime blocker helpers.
    return [path for path in _artifact_string_list(value) if not _is_unfilled_artifact_placeholder(path)]


def _artifact_has_unfilled_goal_value_path(value: Any) -> bool:
    return any(_is_unfilled_artifact_placeholder(path) for path in _artifact_string_list(value))


def _is_unfilled_artifact_placeholder(value: str) -> bool:
    return value.casefold().startswith("<fill")


def _artifact_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _missing_scouted_rung_violation_text(artifact: str) -> str:
    return "The persisted draft is missing scouted rung(s). " + artifact


PersistenceDisposition = Literal["staged", "staged_auto_apply"]

# Both values are staged: a write is never persisted at tool time, and the turn can still decline to
# apply it. ``staged_auto_apply`` reports only that the chat opted into auto-apply; an unknown setting
# is ``staged``.
_PERSISTENCE_MESSAGES: dict[PersistenceDisposition, str] = {
    "staged": (
        "Staged as a proposal for review. Accepting it makes this version the saved workflow; "
        "discarding it keeps the current one."
    ),
    "staged_auto_apply": (
        "Staged as a proposal. This chat accepts proposals automatically, so this one becomes the saved "
        "workflow at the end of the turn if the turn finishes cleanly and the change is fully verified. "
        "Otherwise it stays staged for review."
    ),
}


def persistence_disposition(ctx: AgentContext) -> PersistenceDisposition:
    return "staged_auto_apply" if ctx.auto_accept is True else "staged"


def carry_author_time_findings(update_result: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """The combined update-and-run tool returns the run's result, not the update's, so what the
    update said about the staged draft reaches the model only if it is carried across."""
    update_data = update_result.get("data")
    if not isinstance(update_data, dict):
        return result
    carried = {
        key: update_data[key]
        for key in ("findings", "stored_code", "stored_code_withheld", "persistence", "persistence_message")
        if update_data.get(key)
    }
    if not carried:
        return result
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    for key, value in carried.items():
        data.setdefault(key, value)
    return result


def _author_time_findings(
    *,
    schema_incompatibility: SchemaIncompatibility | None,
    metadata_violations: Sequence[str],
    code_block_diagnostics: Mapping[str, list[str]] | None = None,
    scanner_diagnostics: Mapping[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Non-blocking labels on a draft that persisted anyway. Each entry needs a reason a
    test-run would not surface it; anything a run reveals belongs in the run, not here."""
    findings: list[dict[str, Any]] = []
    # An edited schema field that binds to nothing does not fail the run: the run succeeds and
    # silently omits the field, so only the known output contract can say it can never bind.
    if schema_incompatibility is not None:
        findings.append(
            {
                "reason_code": SCHEMA_INCOMPATIBILITY_REASON_CODE,
                "summary": render_schema_incompatibility_user_reason(schema_incompatibility),
                "schema_incompatibility": schema_incompatibility.to_summary_dict(),
            }
        )
    # The normalizer rewrites what it can; the residue names metadata the authored artifact
    # never declared, which a run cannot report because it never had the field to produce.
    if metadata_violations:
        findings.append(
            {
                "reason_code": "code_artifact_metadata_incomplete",
                "summary": "\n".join(str(violation) for violation in metadata_violations),
            }
        )
    # A contentless readiness wait is intermittent by construction: it passes on every run where the
    # page happens to settle, so a green test-run cannot tell the author the wait encodes nothing.
    if code_block_diagnostics:
        findings.append(
            {
                "reason_code": "code_block_readiness_wait_advisory",
                "summary": "\n".join(
                    f"Code blocks {', '.join(f'`{label}`' for label in labels)}: {message}"
                    for message, labels in code_block_diagnostics.items()
                ),
            }
        )
    # A scanner-flagged pattern is invisible to a test-run by construction: the code runs and
    # succeeds — that is exactly what makes the flagged behavior worth a warning to the author.
    if scanner_diagnostics:
        findings.append(
            {
                "reason_code": "code_block_scanner_advisory",
                "summary": "\n".join(
                    f"Code blocks {', '.join(f'`{label}`' for label in labels)}: {message}"
                    for message, labels in scanner_diagnostics.items()
                ),
            }
        )
    return findings


async def restore_pending_workflow_proposal(ctx: CopilotContext) -> None:
    """Restore only the server's pending proposal, never a prior canonical YAML fallback."""
    if ctx.staged_workflow is not None or not ctx.workflow_copilot_chat_id:
        return
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=ctx.organization_id,
        workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
    )
    if chat is None or chat.workflow_permanent_id != ctx.workflow_permanent_id:
        return
    proposal = chat.proposed_workflow
    if not isinstance(proposal, dict):
        return
    workflow_yaml = proposal.get("_copilot_yaml")
    if not isinstance(workflow_yaml, str) or not workflow_yaml:
        return
    if ctx.workflow_yaml:
        # An explicit canvas edit remains the model's input to the normal update tool.
        # Only restore over the persisted canvas or the same pending proposal.
        try:
            submitted = _normalize_copilot_yaml(ctx.workflow_yaml)
            known_sources = [workflow_yaml]
            if ctx.persisted_workflow_yaml:
                known_sources.append(ctx.persisted_workflow_yaml)
            if all(submitted != _normalize_copilot_yaml(source) for source in known_sources):
                return
        except (yaml.YAMLError, ValidationError):
            return
    if "workflow_definition" in proposal:
        workflow = Workflow.model_validate(proposal)
        authored = _normalize_copilot_yaml(workflow_yaml)
        if "cdp_connect_headers" in authored.model_fields_set:
            # Workflow JSON masks these values for API disclosure; authored YAML retains
            # the submitted header binding, including an explicit clear.
            workflow.cdp_connect_headers = authored.cdp_connect_headers or {}
        if "max_elapsed_time_minutes" in authored.model_fields_set:
            workflow.max_elapsed_time_minutes = authored.max_elapsed_time_minutes
        inherits_headers = (
            "cdp_connect_headers" not in authored.model_fields_set and workflow.cdp_connect_headers is None
        )
        inherits_limit = (
            "max_elapsed_time_minutes" not in authored.model_fields_set and workflow.max_elapsed_time_minutes is None
        )
        if inherits_headers or inherits_limit:
            # Older proposal writers stored defaults rather than resolved omitted settings.
            saved = await app.DATABASE.workflows.get_workflow(
                workflow_id=ctx.workflow_id, organization_id=ctx.organization_id
            )
            if saved is not None:
                if inherits_headers:
                    workflow.cdp_connect_headers = saved.cdp_connect_headers
                if inherits_limit:
                    workflow.max_elapsed_time_minutes = saved.max_elapsed_time_minutes
    else:
        # Older proposals persisted only YAML.
        workflow = await _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
            settings_fallback_yaml=ctx.persisted_workflow_yaml,
        )
    ctx.staged_workflow = workflow
    ctx.staged_workflow_yaml = workflow_yaml
    ctx.workflow_yaml = workflow_yaml
    ctx.last_workflow = workflow
    ctx.last_workflow_yaml = workflow_yaml


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
    originating_call_id: str | None = None,
) -> dict[str, Any]:
    def _blocked(block: AuthorTimeBlock) -> dict[str, Any]:
        _clear_code_authoring_repair_context(ctx)
        result: dict[str, Any] = {"ok": False, "error": block.error, "block_id": block.block_id}
        if block.user_facing_summary is not None:
            result["user_facing_summary"] = block.user_facing_summary
        if block.data:
            result["data"] = block.data
        return result

    def _tool_error(error: str, *, user_facing_summary: str | None = None) -> dict[str, Any]:
        # The submission cannot become a Workflow, so there is no authored artifact to refuse:
        # report it honestly without a block identity, a turn halt, or a churn increment.
        result: dict[str, Any] = {"ok": False, "error": error}
        if user_facing_summary is not None:
            result["user_facing_summary"] = user_facing_summary
        return result

    authority_error = _authority_tool_error(ctx, "update_workflow")
    if authority_error is not None:
        return _tool_error(authority_error)

    workflow_yaml = params["workflow_yaml"]
    submitted_workflow_yaml = workflow_yaml
    raw_conflict_marker_error = _raw_workflow_yaml_conflict_marker_error(workflow_yaml)
    if raw_conflict_marker_error is not None:
        return _tool_error(raw_conflict_marker_error, user_facing_summary=_compiled_authoring_user_summary())
    ctx.raw_block_observation_refs = params.get("raw_block_observation_refs", params.get("block_observation_refs"))
    ctx.block_observation_refs = normalize_block_observation_refs(params.get("block_observation_refs"))
    ctx.raw_code_artifact_metadata = params.get("raw_code_artifact_metadata", params.get("code_artifact_metadata"))
    ctx.submitted_code_artifact_metadata_snapshot = copy.deepcopy(params.get("code_artifact_metadata"))
    params["workflow_yaml"] = workflow_yaml
    scout_trajectory = ctx.scout_trajectory
    normalization = _normalize_code_artifact_metadata_detailed(
        params.get("code_artifact_metadata"),
        workflow_yaml,
        scout_trajectory=scout_trajectory if isinstance(scout_trajectory, list) else None,
        verified_runtime_output_paths_by_label=_verified_runtime_output_contract_paths_by_label(ctx, workflow_yaml),
    )
    code_artifact_metadata = normalization.normalized
    schema_incompatibility_finding = merge_schema_incompatibilities(normalization.schema_incompatibilities)

    prior_workflow_yaml = ctx.workflow_yaml
    existing_metadata = ctx.code_artifact_metadata
    previous_metadata_contract = contract_from_code_artifact_metadata(existing_metadata)
    code_safety_errors = _code_block_safety_errors(workflow_yaml, prior_workflow_yaml)
    code_safety_rejection_facts: list[CodeSafetyRejectionFact] = []
    if code_safety_errors:
        _clear_code_authoring_repair_context(ctx)
        code_safety_rejection_facts = _code_safety_rejection_facts(
            code_safety_errors,
            submission_ref=originating_call_id,
        )
        rejected_labels = list(dict.fromkeys(fact.block_label for fact in code_safety_rejection_facts))
        _record_author_time_reject_outcome(
            ctx,
            reason_code="code_safety_reject",
            summary="Code authoring guardrail rejected the submitted code block.",
            structural_payload=_code_safety_reject_payload(code_safety_rejection_facts),
            block_labels=rejected_labels,
            code_safety_rejection_facts=code_safety_rejection_facts,
        )
    # Per-label salvage keeps conforming metadata across a rejection; a
    # rejected code block keeps nothing, since its yaml never becomes the
    # draft. Prior-draft labels survive every rejection gate below — the
    # accept path prunes to the submitted blocks once the draft switches.
    if code_artifact_metadata and not code_safety_errors:
        merged_metadata = {
            **(existing_metadata if isinstance(existing_metadata, dict) else {}),
            **code_artifact_metadata,
        }
        retained_labels = set(_workflow_yaml_code_blocks_by_label(workflow_yaml)) | set(
            _workflow_yaml_code_blocks_by_label(prior_workflow_yaml)
        )
        merged_metadata = {block: row for block, row in merged_metadata.items() if block in retained_labels}
        ctx.code_artifact_metadata = merged_metadata
        ctx.workflow_verification_evidence.code_artifact_metadata = merged_metadata
        _apply_code_artifact_requested_output_evidence_sources(ctx, merged_metadata)
        params["code_artifact_metadata"] = merged_metadata
    submitted_labels = set(_workflow_yaml_code_blocks_by_label(workflow_yaml))
    active_metadata = ctx.code_artifact_metadata
    accepted_metadata = (
        {block: row for block, row in active_metadata.items() if block in submitted_labels}
        if isinstance(active_metadata, dict)
        else {}
    )
    accepted_metadata_contract = contract_from_code_artifact_metadata(accepted_metadata)
    human_facing_code_safety_errors = _human_facing_code_safety_errors(code_safety_errors)
    if human_facing_code_safety_errors:
        return _blocked(
            AuthorTimeBlock(
                block_id=CODE_SAFETY_BLOCK_ID,
                error="\n".join(human_facing_code_safety_errors),
                user_facing_summary=_code_seam_rejection_user_summary(
                    metadata_rejected=False,
                    code_rejected=True,
                ),
                data={
                    **_code_repair_progress_data(None),
                    "code_safety_rejection_facts": [
                        fact.model_dump(mode="json") for fact in code_safety_rejection_facts
                    ],
                },
            ),
        )
    if allow_missing_credentials is None:
        allow_missing_credentials = ctx.allow_untested_workflow_draft is True
    if not allow_missing_credentials:
        credential_error = await _credential_reference_validation_error(workflow_yaml, ctx)
        if credential_error is not None:
            return _blocked(AuthorTimeBlock(block_id=CREDENTIAL_SCOUT_BLOCK_ID, error=credential_error))

    misbinding_findings = _credential_id_misbinding_findings(workflow_yaml)
    if misbinding_findings:
        LOG.info(
            "copilot credential id misbinding finding",
            organization_id=ctx.organization_id,
            workflow_id=ctx.workflow_id,
            findings=misbinding_findings,
        )

    output_policy_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        workflow_yaml=workflow_yaml,
        tool_arguments=params,
    )
    output_policy_steered_reasons = demote_author_time_steer_reasons(output_policy_verdict)
    if not output_policy_verdict.allowed:
        output_policy_trace_data = output_policy_verdict_to_trace_data(
            output_policy_verdict,
            surface="tool_body",
            tool_name="update_workflow",
        )
        if output_policy_steered_reasons:
            output_policy_trace_data = {
                **output_policy_trace_data,
                "steered_reason_codes": [reason.value for reason in output_policy_steered_reasons],
            }
        output_policy_error = format_output_policy_tool_error(output_policy_verdict)
        LOG.info(
            "copilot output policy tool body verdict",
            **output_policy_trace_data,
        )
        _record_author_time_reject_outcome(
            ctx,
            reason_code="output_policy_reject",
            summary=output_policy_error,
            structural_payload=output_policy_trace_data,
        )
        return _blocked(AuthorTimeBlock(block_id=CREDENTIAL_SCOUT_BLOCK_ID, error=output_policy_error))

    # Prefer the most-recent in-turn emission so cross-path flows (inline
    # REPLACE_WORKFLOW followed by update_workflow) compare against what the
    # LLM actually saw, not the turn-start persisted state.
    last_yaml = ctx.last_workflow_yaml
    prior_yaml = last_yaml if isinstance(last_yaml, str) and last_yaml else ctx.workflow_yaml

    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.TASK_V3_PURE:
        task_v3_pure_violations = _task_v3_pure_policy_violations(workflow_yaml)
        if task_v3_pure_violations:
            violation_items = [(violation.label, violation.block_type) for violation in task_v3_pure_violations]
            _record_banned_block_reject_span("_update_workflow", violation_items)
            return _blocked(
                AuthorTimeBlock(
                    block_id=BANNED_BLOCKS_BLOCK_ID,
                    error=_task_v3_pure_reject_message(task_v3_pure_violations),
                    data={"violations": [violation.as_dict() for violation in task_v3_pure_violations]},
                )
            )

    # Post-emission reject of copilot-v2 writes that introduce a banned
    # block type. The schema pre_hook only fires when the LLM consults the
    # schema; this safety net fires regardless of emission path. Label-based
    # diff preserves legacy workflows — only NEW banned labels trip the reject.
    banned_items = _detect_new_banned_blocks(
        workflow_yaml,
        ctx.workflow_yaml,
        banned_types=_copilot_banned_block_types(ctx),
    )
    if banned_items:
        _record_banned_block_reject_span("_update_workflow", banned_items)
        return _blocked(
            AuthorTimeBlock(
                block_id=BANNED_BLOCKS_BLOCK_ID,
                error=_banned_block_reject_message(banned_items, ctx),
            )
        )

    try:
        # Before redaction: a connection name equal to a registered scrub value would otherwise
        # reach the resolver already replaced by the placeholder.
        workflow_yaml, google_connection_resolution = await canonicalize_named_google_sheet_bindings(workflow_yaml, ctx)
        # Ahead of both persistence and the context assignment below, so the row, the draft the
        # model reads back, and the bytes apply_block_edit anchors against are one string. Scrubbing
        # the payload alone would leave the model reading redacted code and anchoring on raw code.
        workflow_yaml = redact_credentials_in_workflow_yaml(
            workflow_yaml, ctx.workflow_permanent_id, registered_scrub_values(ctx)
        )
        # Same reason, one field over: the conversion seam binds a block's declared parameters
        # into its parameter_keys, and binding only inside that call left the converted workflow
        # and the staged text disagreeing -- the test run got the keys and the document the user
        # accepts did not, so the saved block died on the NameError the binder exists to prevent.
        workflow_yaml = bind_referenced_parameters_in_yaml(workflow_yaml)
        if previous_metadata_contract is not None or accepted_metadata_contract is not None:
            workflow_yaml, clear_persisted_completion_contract = reconcile_workflow_completion_contract(
                workflow_yaml,
                accepted_metadata_contract,
                previous_contract=previous_metadata_contract,
            )
            if isinstance(ctx, CopilotContext):
                ctx.clear_persisted_completion_contract = clear_persisted_completion_contract
            params["workflow_yaml"] = workflow_yaml
        # Retain settings whose omission means inheritance in the durable YAML. Header
        # values cannot be reconstructed from the masked Workflow JSON after reload.
        submitted_definition = parse_workflow_yaml(workflow_yaml)
        prior_definition = parse_workflow_yaml(prior_yaml) if prior_yaml else None
        if isinstance(submitted_definition, dict) and isinstance(prior_definition, dict):
            inherited_settings = {
                key: prior_definition[key]
                for key in ("cdp_connect_headers", "max_elapsed_time_minutes")
                if key not in submitted_definition and key in prior_definition
            }
            if inherited_settings:
                submitted_definition.update(inherited_settings)
                workflow_yaml = dump_workflow_yaml(submitted_definition)
                params["workflow_yaml"] = workflow_yaml
        prior_workflow = await _get_prior_workflow(ctx)
        workflow = await _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
            settings_fallback_yaml=prior_yaml,
            settings_fallback_workflow=prior_workflow,
        )
        webhook_callback_url = workflow.webhook_callback_url
        if isinstance(webhook_callback_url, str) and webhook_callback_url != getattr(
            prior_workflow, "webhook_callback_url", None
        ):
            workflow.webhook_callback_url = validate_webhook_url(webhook_callback_url)
        _record_workflow_proxy_location_span(workflow_yaml, workflow)

        # Runs materialize this proposal as their own version. The saved workflow
        # remains unchanged until Accept, including parameter and setting edits.
        ctx.staged_workflow_yaml = workflow_yaml
        ctx.staged_workflow = workflow
        ctx.has_staged_proposal = True
        ctx.workflow_yaml = workflow_yaml
        if isinstance(ctx, CopilotContext):
            ctx.runner_code_block_associations_by_label = runner_code_block_associations(
                workflow_yaml,
                prior_associations=ctx.runner_code_block_associations_by_label,
                preserve_existing=params.get("_preserve_code_block_associations") is True,
            )
            current_google_connection_bindings = google_sheet_connection_bindings(workflow)
            turn_start_workflow = prior_workflow
            if ctx.google_connection_turn_start_bindings is None:
                baseline_ready = True
                turn_start_workflow_yaml = ctx.google_connection_turn_start_workflow_yaml
                if turn_start_workflow_yaml:
                    try:
                        turn_start_workflow = await _process_workflow_yaml(
                            workflow_id=ctx.workflow_id,
                            workflow_permanent_id=ctx.workflow_permanent_id,
                            organization_id=ctx.organization_id,
                            workflow_yaml=turn_start_workflow_yaml,
                        )
                    except Exception as baseline_err:
                        baseline_ready = False
                        LOG.warning("copilot_google_connection_notice_baseline_failed", error=str(baseline_err))
                if baseline_ready:
                    ctx.google_connection_turn_start_bindings = google_sheet_connection_bindings(turn_start_workflow)
            current_google_connection_ids = tuple(
                dict.fromkeys(connection_id for _, connection_id in current_google_connection_bindings)
            )
            if ctx.google_connection_turn_start_bindings is not None:
                try:
                    visible_google_credentials = await google_oauth_service.get_visible_credentials_for_org(
                        ctx.organization_id
                    )
                    next_google_connection_notices = collect_google_connection_notices(
                        turn_start_bindings=ctx.google_connection_turn_start_bindings,
                        current_bindings=current_google_connection_bindings,
                        visible_credentials=visible_google_credentials,
                    )
                    capture_root = os.environ.get("COPILOT_DUMP_GOOGLE_CONNECTION_NOTICE_INPUTS")
                    if capture_root and current_google_connection_bindings:
                        try:
                            write_google_connection_notice_capture(
                                output_root=capture_root,
                                turn_id=ctx.turn_id,
                                turn_start_workflow=turn_start_workflow,
                                final_workflow=workflow,
                                accepted_workflow_yaml=workflow_yaml,
                                visible_credentials=visible_google_credentials,
                                observed_notices=next_google_connection_notices,
                            )
                        except Exception as capture_err:
                            LOG.warning("copilot_google_connection_notice_capture_failed", error=str(capture_err))
                    ctx.google_connection_notices = next_google_connection_notices
                except Exception as lookup_err:
                    ctx.google_connection_notices = retain_notices_after_lookup_failure(
                        current_connection_ids=current_google_connection_ids,
                        notices=ctx.google_connection_notices,
                    )
                    LOG.warning("copilot_google_connection_notice_lookup_failed", error=str(lookup_err))
        _clear_code_authoring_repair_context(ctx)
        accepted_metadata = ctx.code_artifact_metadata
        if isinstance(accepted_metadata, dict) and accepted_metadata:
            accepted_labels = set(_workflow_yaml_code_blocks_by_label(workflow_yaml))
            pruned_metadata = {block: row for block, row in accepted_metadata.items() if block in accepted_labels}
            if pruned_metadata != accepted_metadata:
                ctx.code_artifact_metadata = pruned_metadata
                ctx.workflow_verification_evidence.code_artifact_metadata = pruned_metadata
        # Best-effort — narrative emit failures must never abort an
        # otherwise-successful update_workflow tool call. ``isinstance``
        # narrows the parameter's declared ``AgentContext`` to the
        # envelope-aware ``CopilotContext`` for mypy.
        changed_code_blocks = _changed_code_blocks(prior_workflow_yaml, submitted_workflow_yaml, workflow_yaml)
        written_diffs: list[CodeWriteDiff] = []
        if isinstance(ctx, CopilotContext):
            # Best-effort — the workflow is already persisted, so a narrative detail must never
            # turn a successful write into a failed turn.
            try:
                # Keyed by the call that produced it, and extended rather than assigned: parallel
                # tool calls are the provider default, so a second write can land before the first
                # one's result drains, and one call can write more than one block.
                written_diffs, ctx.code_write_patch_budget = build_code_write_diffs(
                    _workflow_yaml_code_blocks_by_label(prior_workflow_yaml),
                    changed_code_blocks,
                    scrub=lambda text: scrub_secrets_from_text(ctx, text),
                    budget=ctx.code_write_patch_budget,
                )
                if written_diffs and originating_call_id is not None:
                    stashed = ctx.pending_code_write_diffs.get(originating_call_id, [])
                    ctx.pending_code_write_diffs[originating_call_id] = [*stashed, *written_diffs]
            except Exception as diff_err:
                LOG.warning("copilot_code_write_diff_failed", error=str(diff_err))
        if isinstance(ctx, CopilotContext) and ctx.stream is not None:
            try:
                await maybe_emit_design_end(ctx.stream, ctx)
                # The diffs ride the draft, which fires now, rather than the tool_result: a write
                # and its test share one tool call, so the result lands only after the run.
                await emit_workflow_draft(
                    ctx.stream,
                    ctx,
                    workflow,
                    code_diffs=written_diffs or None,
                    tool_call_id=originating_call_id,
                )
            except Exception as emit_err:
                LOG.warning("copilot_narrative_workflow_draft_emit_failed", error=str(emit_err))
        disposition = persistence_disposition(ctx)
        disposition_message = _PERSISTENCE_MESSAGES[disposition]
        data: dict[str, Any] = {
            "persistence": disposition,
            "persistence_message": disposition_message,
            "message": disposition_message,
            "block_count": len(workflow.workflow_definition.blocks) if workflow.workflow_definition else 0,
        }
        stored_code, stored_code_withheld = _accepted_code_delta(changed_code_blocks)
        if stored_code:
            data["stored_code"] = stored_code
        if stored_code_withheld:
            data["stored_code_withheld"] = stored_code_withheld
        if stored_code or stored_code_withheld:
            LOG.info(
                "copilot write returned stored code",
                returned_chars={label: len(code) for label, code in stored_code.items()},
                withheld_labels=stored_code_withheld,
            )
        # Best-effort — the workflow is already persisted by this point, so an advisory that trips on
        # crafted block code must never turn a successful update into a failed turn.
        try:
            advisory_labels = _advisory_labels_by_message(changed_code_blocks)
        except Exception as advisory_err:
            LOG.warning("copilot_advisory_code_block_diagnostics_failed", error=str(advisory_err))
            advisory_labels = {}
        try:
            scanner_labels = await _scanner_advisory_labels_by_message(changed_code_blocks, ctx.organization_id)
        except Exception as scanner_err:
            LOG.warning("copilot_scanner_advisory_diagnostics_failed", error=str(scanner_err))
            scanner_labels = {}
        findings = _author_time_findings(
            schema_incompatibility=schema_incompatibility_finding,
            metadata_violations=normalization.violations,
            code_block_diagnostics=advisory_labels,
            scanner_diagnostics=scanner_labels,
        )
        if findings:
            data["findings"] = findings
        if google_connection_resolution:
            data["google_connection_resolution"] = google_connection_resolution
        if isinstance(ctx, CopilotContext) and ctx.google_connection_notices:
            data["google_connection_notices"] = [notice.to_payload() for notice in ctx.google_connection_notices]
        return {
            "ok": True,
            "data": data,
            "_workflow": workflow,
        }
    except (yaml.YAMLError, ValidationError, SkyvernHTTPException, BaseWorkflowHTTPException) as e:
        return _tool_error(
            f"{INTERNAL_VALIDATION_FAILURE_PREFIX}{e}",
            user_facing_summary=(
                _code_seam_rejection_user_summary(metadata_rejected=False, code_rejected=True)
                if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
                else None
            ),
        )


def _record_workflow_proxy_location_span(workflow_yaml: str, workflow: Workflow) -> None:
    input_present, input_proxy_location = _raw_yaml_proxy_location(workflow_yaml)
    effective_proxy_location = _proxy_location_trace_value(runtime_proxy_location(workflow.proxy_location))
    with copilot_span(
        "workflow_proxy_location_normalized",
        data={
            "input_proxy_location_present": input_present,
            "input_proxy_location": input_proxy_location,
            "effective_proxy_location": effective_proxy_location,
        },
    ):
        pass


def _record_workflow_update_result(
    copilot_ctx: Any, result: dict[str, Any], prior_definition: object | None = None
) -> None:
    if not (result.get("ok") and "_workflow" in result):
        return

    wf = result["_workflow"]
    copilot_ctx.last_workflow = wf
    copilot_ctx.last_workflow_yaml = copilot_ctx.workflow_yaml or None
    copilot_ctx.effective_workflow_proxy_location = runtime_proxy_location(getattr(wf, "proxy_location", None))
    data = result.get("data")
    if isinstance(data, dict):
        block_count = data.get("block_count")
        if isinstance(block_count, int):
            copilot_ctx.last_update_block_count = block_count
    copilot_ctx.update_workflow_called = True
    copilot_ctx.test_after_update_done = False
    copilot_ctx.last_test_ok = None
    clear_active_run_evidence_on_workflow_edit(copilot_ctx)
    # A fresh workflow edit invalidates the prior test's failure state —
    # otherwise an exhausted POST_UPDATE_NUDGE on the new draft would raise
    # CopilotNonRetriableNavError with the old run's error, telling the user
    # to "verify the URL" for a URL they just corrected in the new draft.
    copilot_ctx.last_test_non_retriable_nav_error = None
    _invalidate_verified_state_on_edit(copilot_ctx, prior_definition, getattr(wf, "workflow_definition", None))
