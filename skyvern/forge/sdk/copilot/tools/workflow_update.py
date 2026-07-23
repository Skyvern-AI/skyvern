from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import keyword
import re
import textwrap
import tokenize
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from enum import StrEnum
from typing import Annotated, Any, Literal, NamedTuple, cast
from urllib.parse import urlsplit

import structlog
import yaml
from jinja2 import TemplateSyntaxError
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.copilot.attribution import resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.authoring_parameter_binding import (
    AuthoringParameterBindingCandidate,
    AuthoringParameterBindingDirective,
    AuthoringParameterBindingMatchBasis,
    AuthoringParameterBindingSnapshot,
    AuthoringParameterBindingTerminalTool,
    AuthoringParameterFieldBinding,
    AuthoringParameterTerminalBinding,
    SameMonthFileMatchFormat,
    SameMonthFileMatchHole,
    SameMonthFileMatchTransform,
    authored_selector_parameter_bindings,
    authoring_parameter_binding_directive_consumed,
    build_authoring_parameter_binding_directive,
    build_authoring_parameter_binding_snapshot,
    derive_same_month_file_match_transform,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    METADATA_REJECT_SAME_KEY_TERMINAL_REASON_CODE,
    OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE,
    CopilotToolBlockerSignal,
    blocker_signal_is_genuinely_terminal,
    build_definition_contract_unsatisfied_blocker_signal,
    build_metadata_reject_same_key_terminal_signal,
    build_output_source_unobservable_blocker_signal,
    clear_terminal_evidence_on_workflow_edit,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestOutcomeReasonCode,
    MetadataRejectFamily,
    MetadataRejectLadderDecision,
    MetadataRejectLadderInput,
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    RecordedOutcomeGroundingRequirement,
    _stable_hash,
    adjudicate_metadata_reject_ladder,
    authored_block_signatures_from_workflow,
    authored_structure_signature_from_workflow,
    latest_recorded_build_test_outcome_repeated,
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
    recorded_outcome_from_authoring_repair_context,
    run_backed_repair_evidence_exists,
)
from skyvern.forge.sdk.copilot.code_block_preflight import (
    SANDBOX_UNRESOLVED_NAME_REASON_CODE,
    CodeBlockRenderDiagnostic,
    author_time_code_block_diagnostics,
    code_block_render_diagnostic,
    sandbox_unresolved_name_diagnostics,
    sandbox_unresolved_name_repair_diagnostic,
    strip_redundant_sandbox_imports,
)
from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError, author_time_code_security_errors
from skyvern.forge.sdk.copilot.code_block_steps import apply_derived_code_block_steps, fill_code_block_prompts_in_yaml
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _CODE_SUBMIT_ACTION_RE,
    _CREDENTIAL_FIELDS,
    _INTERNAL_SCOUT_VARS,
    _SYNTHESIZED_BLOCK_LABEL,
    CREDENTIAL_FILL_TOOL_NAME,
    INPUT_TEMPLATED_PROVENANCE_SOURCE,
    LOCATOR_WITNESS_PARAM_SOURCE,
    SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE,
    SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE,
    SCOUTED_SPINE_TRUNCATED_REASON_CODE,
    SCOUTED_SPINE_UNDER_BUILD_REASON_CODE,
    SCOUTED_SPINE_UNRECORDED_INDEX_REASON_CODE,
    UNCOVERED_RUNG_FINDING,
    ObligationFinding,
    SynthesisDiagnostics,
    SynthesizedCodeBlock,
    _bare_drop_superseded_on_screen,
    _bare_locator_call_selector,
    _credential_field_accesses,
    _get_by_role_expr_strict,
    _is_ignorable_entry_opener_drop,
    _is_positional_selector,
    _is_submit_interaction,
    _selector_refines,
    artifact_dependency_id,
    artifact_observation_ref_id,
    build_input_templated_locator,
    build_same_month_file_match_locator,
    credential_scout_gap,
    dynamic_row_evidence_fingerprint,
    dynamic_row_period_matches_match_selected_row,
    freeze_requested_output_extraction_candidate,
    grounded_parameter_key_is_safe,
    input_correspondences_for_interaction,
    input_correspondences_for_selector,
    locator_selector_literals,
    missing_rung_text,
    normalized_locator_expr,
    normalized_scout_selector,
    obligation_finding_reason_code,
    obligation_finding_selector,
    produce_covered_static_return_envelope,
    render_missing_rung_call_sources,
    render_obligation_findings,
    selection_option_value_admissible,
    spine_partition_findings,
    synthesize_code_block,
    synthesize_code_block_with_extraction,
    templated_selection_locator_binding,
    uncovered_required_emitted_interactions,
    uncovered_rung_records,
    validated_dynamic_row_period_matches,
)
from skyvern.forge.sdk.copilot.completion_verification import grade_definition_criteria
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    composition_page_evidence_error,
    normalize_block_observation_refs,
    workflow_target_url,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import (
    OUTPUT_OWNER_AMBIGUITY_REASON_CODE,
    CodeAuthoringRepairContext,
    CopilotContext,
)
from skyvern.forge.sdk.copilot.data_write_defaults import default_data_write_continue_on_failure
from skyvern.forge.sdk.copilot.enforcement import (
    _CHURN_REASON_CODES,
    _record_code_authoring_guardrail_reject,
    _scouted_spine_open_obligation,
    arm_credential_scout_reopen,
    download_satisfied_requested_output_paths,
    requested_output_extraction_plan,
    requested_output_extraction_plan_changed,
    requested_scalar_output_extraction_plan,
    synthesized_goal_completion_landing_pending,
    synthesized_persistence_reopened,
    synthesized_persistence_reopened_after_failed_run,
    synthesized_trajectory_is_goal_complete,
    synthesized_trajectory_reaches_goal,
)
from skyvern.forge.sdk.copilot.loop_detection import clear_failed_step_tracker_for_tools_in_ctx
from skyvern.forge.sdk.copilot.narration import CODE_REPAIR_PROGRESS_SURFACE_KIND, CODE_REPAIR_PROGRESS_TEXT
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_code_artifact_violations
from skyvern.forge.sdk.copilot.output_contracts import (
    OutputContractActuation,
    OutputContractActuationEvidence,
    OutputContractActuationKind,
    OutputContractAdvisoryState,
    classify_output_contract_bail_family,
    code_block_available_binding_keys_by_label,
    declared_string_workflow_parameter_keys,
    declared_workflow_parameter_keys,
    resolve_output_contract_actuation,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import FrozenRequestedOutputExtractionCandidate
from skyvern.forge.sdk.copilot.output_policy import (
    OutputPolicyReason,
    OutputPolicyVerdict,
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
    url_origin,
)
from skyvern.forge.sdk.copilot.output_utils import INTERNAL_VALIDATION_FAILURE_PREFIX
from skyvern.forge.sdk.copilot.reached_download_target import (
    REGISTERED_DOWNLOAD_OUTPUT_KEYS,
    ReachedDownloadTarget,
    code_is_download_intent,
)
from skyvern.forge.sdk.copilot.request_policy import (
    REQUESTED_OUTPUT_PATH_MINT_SOURCES,
    CompletionCriterion,
    JudgmentPredicate,
    RequestedOutputEvidenceSource,
    _coerce_requested_output_evidence_source,
    _is_judgment_boolean_criterion,
)
from skyvern.forge.sdk.copilot.result_evidence import loaded_result_source_producible
from skyvern.forge.sdk.copilot.runtime import (
    DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
    METADATA_RUN_PREFLIGHT_REJECT_GATE_ID,
    OUTPUT_CONTRACT_ACTUATION_GATE_ID,
    RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID,
    SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
    AgentContext,
    AuthorTimeGateAblationPayload,
    CodeArtifactMetadataValue,
    NeverCapturedObligation,
    NeverCapturedReplayPayload,
    RejectedCodeArtifactMetadataCapture,
    ScoutedInteraction,
    copilot_author_time_gate_log_only_enabled,
    record_author_time_gate_ablation_event,
)
from skyvern.forge.sdk.copilot.schema_incompatibility import (
    SCHEMA_INCOMPATIBILITY_FAILURE_TYPE,
    SchemaIncompatibility,
    build_schema_incompatibility_blocker_signal,
    merge_schema_incompatibilities,
    render_schema_incompatibility_agent_steer,
)
from skyvern.forge.sdk.copilot.streaming_adapter import emit_workflow_draft, maybe_emit_design_end
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import (
    stash_repair_ceiling_turn_halt,
    stash_turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.copilot.turn_ownership import (
    TurnClaimant,
    claim_and_stash_blocker_signal,
    claim_turn,
    current_turn_owner,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    credential_param_ids,
    credential_params,
    parse_workflow_yaml,
    workflow_blocks,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import RESERVED_PARAMETER_KEYS, is_sensitive_workflow_parameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.proxy_location import runtime_proxy_location
from skyvern.schemas.workflows import BlockType
from skyvern.utils.templating import get_missing_variables

from ._shared import (
    BLOCK_RUNNING_TOOLS,
    _enum_or_string_name,
    _proxy_location_trace_value,
    _raw_yaml_proxy_location,
)
from .banned_blocks import (
    _banned_block_reject_message,
    _challenge_http_request_reject_message,
    _copilot_banned_block_types,
    _copilot_block_authoring_policy,
    _detect_new_banned_blocks,
    _record_banned_block_reject_span,
    _timing_only_challenge_wait_reject_message,
)
from .blockers import _clear_resolved_per_tool_budget_problem_labels
from .credentials import (
    _credential_id_misbinding_error_message,
    _credential_id_misbinding_findings,
    _credential_reference_validation_error,
)
from .frontier import (
    _detect_stale_block_metadata,
    _get_prior_workflow,
    _invalidate_verified_state_on_edit,
    _stale_block_metadata_message,
    _workflow_requires_canonical_persist,
)
from .guardrails import (
    _authority_tool_error,
    _download_binding_required_error,
    _download_scout_required_error,
    _request_policy_allows_untested_code_block_draft,
)

LOG = structlog.get_logger()


class BlockObservationRef(BaseModel):
    label: str
    observation_step: Annotated[int, Field(ge=0)]


ArtifactEvidenceStatus = Literal["satisfied", "missing", "diagnostic_only", "observed_not_verified"]
ExtractionSchemaProvenance = Literal["self_authored", "user_edited"]
SelectedExtractionMetadataDisposition = Literal[
    "none",
    "browser_spine_replaced_metadata_stale",
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
    extraction_schema_provenance: ExtractionSchemaProvenance = "self_authored"
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
    extraction_schema_provenance: ExtractionSchemaProvenance = "self_authored"


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


class CodeArtifactMetadata(BaseModel):
    artifact_id: str | None = Field(
        default=None, description="Server-owned id; defaults to `code_artifact:<block_label>` when omitted."
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
            "requires claim-scoped `evidence_refs`. Mechanical links are server-defaulted at the "
            "persist seam in code-block authoring mode."
        ),
    )
    page_dependencies: list[CodeArtifactPageDependency] = Field(
        default_factory=list,
        description=(
            "Pages or states the code depends on; non-`missing` rows carry scoped evidence or observation "
            "refs. Server-defaulted when omitted."
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
        description=(
            "Artifact-level observation refs; same shape rules as `evidence_refs`. Server-defaulted when omitted."
        ),
    )
    terminal_verifier_expectations: list[CodeArtifactTerminalVerifierExpectation] = Field(
        default_factory=list,
        description="What terminal verification must observe; link `criteria_ids` or `claimed_outcome_ids`.",
    )
    exploration_observations: list[CodeArtifactExplorationObservation] = Field(
        default_factory=list,
        description="Scout-time observations; status stays `observed_not_verified` until verification passes.",
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
    impose_defaults: bool = False,
    scout_trajectory: list[ScoutedInteraction] | None = None,
    verified_runtime_output_paths_by_label: Mapping[str, set[str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    result = _normalize_code_artifact_metadata_detailed(
        raw_metadata,
        workflow_yaml,
        impose_defaults=impose_defaults,
        scout_trajectory=scout_trajectory,
        verified_runtime_output_paths_by_label=verified_runtime_output_paths_by_label,
    )
    return result.normalized, result.error


def _normalize_code_artifact_metadata_detailed(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    impose_defaults: bool = False,
    scout_trajectory: list[ScoutedInteraction] | None = None,
    verified_runtime_output_paths_by_label: Mapping[str, set[str]] | None = None,
    advisory_declared_output_return_shape_labels: set[str] | None = None,
) -> CodeArtifactNormalization:
    """Normalize submitted artifact metadata at the persist seam.

    Entries keyed to a missing code-block label are re-keyed to the single
    uncovered block or dropped, never rejected. When imposing, mechanical graph
    fields are server-defaulted (and uncovered labels get a deterministic
    skeleton) so only contradictions reject. Returns the per-violation list and
    offending labels alongside the batched error for durable telemetry."""
    if raw_metadata in (None, [], {}):
        return CodeArtifactNormalization({}, None, [], [])
    items = _code_artifact_metadata_items(raw_metadata)
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    trajectory: list[ScoutedInteraction] = scout_trajectory or []
    advisory_return_shape_labels = advisory_declared_output_return_shape_labels or set()
    violations: list[str] = []
    offending_labels: list[str] = []
    schema_incompatibilities: list[SchemaIncompatibility] = []
    anchored: list[dict[str, Any]] = []
    unanchored: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for raw_item in items:
        try:
            raw_item = _default_missing_extraction_schema_provenance(raw_item)
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
            unanchored.append(dumped)
            continue
        if label in seen_labels:
            LOG.info("copilot_code_artifact_metadata_duplicate_dropped", block_label=label)
            continue
        seen_labels.add(label)
        dumped["block_label"] = label
        anchored.append(dumped)

    uncovered = [label for label in code_blocks if label not in seen_labels]
    if len(unanchored) == 1 and len(uncovered) == 1:
        # The only metadata-less code block is the only plausible owner of the
        # only unanchored entry; re-key instead of dropping.
        rekeyed = unanchored.pop()
        LOG.info(
            "copilot_code_artifact_metadata_rekeyed",
            stale_label=str(rekeyed.get("block_label") or "") or None,
            block_label=uncovered[0],
        )
        rekeyed["block_label"] = uncovered[0]
        rekeyed.pop("artifact_id", None)
        rekeyed.pop("block_id", None)
        seen_labels.add(uncovered[0])
        anchored.append(rekeyed)
        uncovered = []
    if unanchored:
        LOG.info(
            "copilot_code_artifact_metadata_stale_entries_dropped",
            stale_labels=[str(item.get("block_label") or "") or None for item in unanchored],
        )

    normalized: dict[str, dict[str, Any]] = {}
    for dumped in anchored:
        label = str(dumped["block_label"])
        artifact_id = str(dumped.get("artifact_id") or "").strip()
        if not artifact_id.startswith("code_artifact:"):
            artifact_id = _artifact_id_for_block_label(label)
        dumped["artifact_id"] = artifact_id
        block_id = str(
            dumped.get("block_id") or code_blocks[label].get("block_id") or code_blocks[label].get("id") or ""
        ).strip()
        if block_id:
            dumped["block_id"] = block_id
        if impose_defaults:
            if not str(dumped.get("declared_goal") or "").strip():
                dumped["declared_goal"] = _block_goal_fallback(label, code_blocks[label])
            _impose_code_artifact_metadata_defaults(label, dumped, trajectory)
        item_violations: list[str] = []
        declared_goal = str(dumped.get("declared_goal") or "").strip()
        if not declared_goal:
            item_violations.append(f"Artifact metadata for `{label}` requires a non-empty `declared_goal`.")
        for field_name in _CODE_ARTIFACT_REQUIRED_LIST_FIELDS:
            value = dumped.get(field_name)
            if not isinstance(value, list) or not value:
                item_violations.append(f"Artifact metadata for `{label}` requires non-empty `{field_name}`.")
        if not dumped.get("evidence_refs") and not dumped.get("observation_refs"):
            item_violations.append(f"Artifact metadata for `{label}` requires `evidence_refs` or `observation_refs`.")
        item_violations.extend(
            _code_artifact_metadata_shape_errors(
                label,
                dumped,
                reject_unfilled_goal_value_paths=impose_defaults,
            )
        )
        block_code = str(code_blocks[label].get("code") or "")
        require_declared_output = impose_defaults and (
            len(code_blocks) == 1 or _block_declares_output_intent(code_blocks[label])
        )
        return_shape_error = _extraction_return_shape_error(
            label,
            dumped,
            block_code,
            require_declared_output=require_declared_output,
        )
        if return_shape_error is not None and label not in advisory_return_shape_labels:
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
        download_shape_error = _download_return_shape_error(label, dumped, block_code)
        if download_shape_error is not None:
            item_violations.append(download_shape_error)
        if item_violations:
            violations.extend(item_violations)
            offending_labels.append(label)
            continue
        normalized[label] = dumped
    if impose_defaults:
        for label in uncovered:
            normalized[label] = _imposed_artifact_skeleton(label, code_blocks[label], trajectory)
    if violations:
        return CodeArtifactNormalization(
            normalized,
            _format_code_artifact_violations(violations),
            violations,
            offending_labels,
            schema_incompatibilities,
        )
    return CodeArtifactNormalization(normalized, None, [], [], [])


def _artifact_label_fragment(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "artifact"


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


def _drop_contradictory_checkpoint_mode(ref: dict[str, Any]) -> None:
    status = str(ref.get("status") or "").strip()
    mode = ref.get("checkpoint_next_mode")
    if mode == "advance" and status != "diagnostic_only":
        ref.pop("checkpoint_next_mode", None)
    elif mode == "stop" and status not in {"observed_not_verified", "diagnostic_only"}:
        ref.pop("checkpoint_next_mode", None)


def _impose_code_artifact_metadata_defaults(
    label: str,
    artifact: dict[str, Any],
    scout_trajectory: list[ScoutedInteraction],
) -> None:
    """Fill the mechanical evidence-graph fields the persist seam owns; semantic
    content (declared_goal, claim/criterion text) stays model-authored, and
    contradictions (e.g. `satisfied` without evidence) are left for validation."""
    fragment = _artifact_label_fragment(label)
    dependency_id = artifact_dependency_id(label)
    observation_ref_id = artifact_observation_ref_id(label)
    declared_goal = str(artifact.get("declared_goal") or "").strip()
    default_goal_value_paths = _first_artifact_goal_value_paths(artifact.get("claimed_outcomes")) or (
        _first_artifact_goal_value_paths(artifact.get("terminal_verifier_expectations"))
    )

    dependencies = _artifact_mutable_rows(artifact.get("page_dependencies"))
    for index, dependency in enumerate(dependencies):
        if not str(dependency.get("id") or "").strip():
            dependency["id"] = f"dependency:{fragment}_{index}"
        if not str(dependency.get("scope") or "").strip():
            dependency["scope"] = "page"
    if not dependencies:
        default_dependency: dict[str, Any] = {
            "id": dependency_id,
            "scope": "page",
            "status": "observed_not_verified",
            "observation_refs": [observation_ref_id],
        }
        entry_url_hint = next(
            (url for url in (str(item.get("source_url") or "").strip() for item in scout_trajectory) if url),
            None,
        )
        if entry_url_hint:
            default_dependency["url_hint"] = entry_url_hint
        artifact["page_dependencies"] = [default_dependency]
        dependencies = _artifact_mutable_rows(artifact.get("page_dependencies"))
    primary_dependency_id = str(dependencies[0].get("id") or "").strip() or dependency_id

    claims = _artifact_mutable_rows(artifact.get("claimed_outcomes"))
    for index, claim in enumerate(claims):
        if not str(claim.get("id") or "").strip():
            claim["id"] = f"claim:{fragment}_{index}"
        if not str(claim.get("scope") or "").strip():
            claim["scope"] = "outcome"
        if not str(claim.get("text") or "").strip():
            claim["text"] = declared_goal or f"outcome of `{label}`"
    if not claims and declared_goal:
        artifact["claimed_outcomes"] = [
            {
                "id": f"claim:{fragment}_goal",
                "scope": "outcome",
                "text": declared_goal,
                "status": "observed_not_verified",
                "depends_on": [primary_dependency_id],
                "observation_refs": [observation_ref_id],
            }
        ]
        if default_goal_value_paths:
            artifact["claimed_outcomes"][0]["goal_value_paths"] = list(default_goal_value_paths)
        claims = _artifact_mutable_rows(artifact.get("claimed_outcomes"))

    criteria = _artifact_mutable_rows(artifact.get("completion_criteria"))
    for index, criterion in enumerate(criteria):
        if not str(criterion.get("id") or "").strip():
            criterion["id"] = f"criterion:{fragment}_goal_{index}"
        if not str(criterion.get("text") or "").strip():
            criterion["text"] = declared_goal or f"criterion for `{label}`"
    if not criteria and claims:
        artifact["completion_criteria"] = [
            {
                "id": f"criterion:{fragment}_goal_{index}",
                "text": str(claim.get("text") or declared_goal or "").strip() or f"criterion for `{label}`",
                "level": "terminal",
                "terminal": True,
            }
            for index, claim in enumerate(claims)
        ]
        criteria = _artifact_mutable_rows(artifact.get("completion_criteria"))
    criterion_ids = [str(criterion.get("id") or "").strip() for criterion in criteria]
    criterion_ids = [criterion_id for criterion_id in criterion_ids if criterion_id]

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if not _artifact_string_list(claim.get("depends_on")):
            claim["depends_on"] = [primary_dependency_id]
        if (
            criterion_ids
            and not _artifact_string_list(claim.get("covered_criteria"))
            and not _artifact_string_list(claim.get("criteria_ids"))
        ):
            claim["covered_criteria"] = list(criterion_ids)
        if default_goal_value_paths and not _artifact_string_list(claim.get("goal_value_paths")):
            claim["goal_value_paths"] = list(default_goal_value_paths)
        status = str(claim.get("status") or "").strip()
        if (
            status not in {"missing", "satisfied"}
            and not _artifact_string_list(claim.get("evidence_refs"))
            and not _artifact_string_list(claim.get("observation_refs"))
        ):
            claim["observation_refs"] = [observation_ref_id]

    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        status = str(dependency.get("status") or "").strip()
        if (
            status not in {"missing", "satisfied"}
            and not _artifact_string_list(dependency.get("evidence_refs"))
            and not _artifact_string_list(dependency.get("observation_refs"))
        ):
            dependency["observation_refs"] = [observation_ref_id]

    expectations = _artifact_mutable_rows(artifact.get("terminal_verifier_expectations"))
    if not expectations and (criterion_ids or declared_goal):
        artifact["terminal_verifier_expectations"] = [
            {
                "id": f"expectation:{fragment}_terminal",
                "text": f"Terminal verification observes: {declared_goal or label}",
                "criteria_ids": list(criterion_ids),
            }
        ]
        expectations = _artifact_mutable_rows(artifact.get("terminal_verifier_expectations"))
    for index, expectation in enumerate(expectations):
        if not isinstance(expectation, dict):
            continue
        if not str(expectation.get("id") or "").strip():
            expectation["id"] = f"expectation:{fragment}_{index}"
        if not str(expectation.get("text") or "").strip():
            expectation["text"] = f"Terminal verification observes: {declared_goal or label}"
        if (
            criterion_ids
            and not _artifact_string_list(expectation.get("criteria_ids"))
            and not _artifact_string_list(expectation.get("claimed_outcome_ids"))
        ):
            expectation["criteria_ids"] = list(criterion_ids)
        if default_goal_value_paths and not _artifact_string_list(expectation.get("goal_value_paths")):
            expectation["goal_value_paths"] = list(default_goal_value_paths)

    if not _artifact_mutable_rows(artifact.get("evidence_refs")) and not _artifact_mutable_rows(
        artifact.get("observation_refs")
    ):
        artifact["observation_refs"] = [
            {
                "observation_ref": observation_ref_id,
                "dependency_id": primary_dependency_id,
                "status": "observed_not_verified",
                "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
            }
        ]

    for field_name, ref_key in (("evidence_refs", "evidence_ref"), ("observation_refs", "observation_ref")):
        for index, ref in enumerate(_artifact_mutable_rows(artifact.get(field_name))):
            if not str(ref.get(ref_key) or "").strip():
                prefix = "evidence" if ref_key == "evidence_ref" else "observation"
                ref[ref_key] = f"{prefix}:{fragment}_{index}"
            if not any(str(ref.get(key) or "").strip() for key in ("claim_id", "dependency_id", "criterion_id")):
                ref["dependency_id"] = primary_dependency_id
            status = str(ref.get("status") or "").strip()
            if status != "missing" and not str(ref.get("source_tool") or "").strip():
                ref["source_tool"] = SCOUT_INTERACTION_EVIDENCE_TOOL
            _drop_contradictory_checkpoint_mode(ref)

    if isinstance(artifact.get("exploration_observations"), list):
        observations: list[dict[str, Any]] = []
        for index, observation in enumerate(_artifact_mutable_rows(artifact["exploration_observations"])):
            if not str(observation.get("text") or "").strip():
                continue
            if not str(observation.get("id") or "").strip():
                observation["id"] = f"exploration:{fragment}_{index}"
            if observation.get("checkpoint_next_mode") == "advance":
                observation.pop("checkpoint_next_mode", None)
            observations.append(observation)
        artifact["exploration_observations"] = observations


def _block_goal_fallback(label: str, block: Mapping[str, Any]) -> str:
    title = str(block.get("title") or "").strip()
    return title or label.replace("_", " ").strip() or label


def _imposed_artifact_skeleton(
    label: str,
    block: Mapping[str, Any],
    scout_trajectory: list[ScoutedInteraction],
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "block_label": label,
        "artifact_id": _artifact_id_for_block_label(label),
        "declared_goal": _block_goal_fallback(label, block),
        "terminal_verifier_expectations": [
            {"goal_value_paths": ["<fill: output JSON path(s) carrying requested goal values>"]}
        ],
    }
    block_id = str(block.get("block_id") or block.get("id") or "").strip()
    if block_id:
        artifact["block_id"] = block_id
    _impose_code_artifact_metadata_defaults(label, artifact, scout_trajectory)
    return artifact


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


def _default_missing_extraction_schema_provenance(raw_item: Any) -> Any:
    if isinstance(raw_item, CodeArtifactMetadata) or not isinstance(raw_item, Mapping):
        return raw_item
    item = copy.deepcopy(raw_item)
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        rows = item.get(field_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            schema = row.get("extraction_schema")
            if schema is None or (isinstance(schema, str) and not schema.strip()):
                continue
            row.setdefault("extraction_schema_provenance", "user_edited")
    return item


def _downgrade_stale_selected_metadata_item(item: dict[str, Any], selected_label: str) -> None:
    if str(item.get("block_label") or "").strip() != selected_label:
        return
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_mutable_rows(item.get(field_name)):
            row.pop("goal_value_paths", None)
            if field_name == "terminal_verifier_expectations":
                row.pop("criteria_ids", None)
                row.pop("claimed_outcome_ids", None)
    criteria = _artifact_mutable_rows(item.get("completion_criteria"))
    if not criteria:
        item["completion_criteria"] = [
            {
                "id": f"criterion:{_artifact_label_fragment(selected_label)}_diagnostic",
                "text": str(item.get("declared_goal") or selected_label).strip() or selected_label,
                "level": "outcome",
                "terminal": False,
            }
        ]
        criteria = _artifact_mutable_rows(item.get("completion_criteria"))
    for criterion in criteria:
        criterion["level"] = "outcome"
        criterion["terminal"] = False
    criterion_ids = [str(criterion.get("id") or "").strip() for criterion in criteria]
    criterion_ids = [criterion_id for criterion_id in criterion_ids if criterion_id]
    for row in _artifact_mutable_rows(item.get("claimed_outcomes")):
        row.pop("criteria_ids", None)
        if criterion_ids:
            row["covered_criteria"] = list(criterion_ids)
    for row in _artifact_mutable_rows(item.get("terminal_verifier_expectations")):
        if criterion_ids:
            row["criteria_ids"] = list(criterion_ids)


def _downgrade_stale_selected_goal_value_paths(raw_metadata: Any, selected_label: str) -> Any:
    if raw_metadata in (None, [], {}) or not selected_label:
        return raw_metadata
    scrubbed = copy.deepcopy(raw_metadata)
    if isinstance(scrubbed, list):
        for item in scrubbed:
            if isinstance(item, dict):
                _downgrade_stale_selected_metadata_item(item, selected_label)
    elif isinstance(scrubbed, dict):
        if "block_label" in scrubbed:
            _downgrade_stale_selected_metadata_item(scrubbed, selected_label)
        else:
            for block_label, value in scrubbed.items():
                if block_label == selected_label and isinstance(value, dict):
                    value.setdefault("block_label", selected_label)
                    _downgrade_stale_selected_metadata_item(value, selected_label)
    return scrubbed


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


def _artifact_id_for_block_label(label: str) -> str:
    return f"code_artifact:{_artifact_label_fragment(label)}"


def _code_block_safety_errors(workflow_yaml: str | None, prior_yaml: str | None) -> list[str | CodeBlockSecurityError]:
    """Run the sandbox's static safety rule on new/changed code blocks before any run.

    Label-scoped diff so legacy code blocks the model did not touch cannot wedge
    every subsequent update."""
    prior_blocks = _workflow_yaml_code_blocks_by_label(prior_yaml)
    errors: list[str | CodeBlockSecurityError] = []
    for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items():
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
            CodeBlock.is_safe_code(code)
        except SyntaxError as exc:
            errors.append(f"Code block `{label}` is not valid Python: {exc}")
        except InsecureCodeDetected as exc:
            errors.append(
                f"Code block `{label}` failed the sandbox safety check: {exc}. Rewrite without import "
                "statements, dunder access, or private attributes; the sandbox provides `page`, declared "
                "code-block parameter keys, `json`, `re`, `html`, `asyncio.sleep`, and its explicit safe helper "
                "namespace. `Exception` is the only available exception type."
            )
        errors.extend(author_time_code_security_errors(label=label, code=code))
        author_time_diagnostics = author_time_code_block_diagnostics(code)
        errors.extend(
            f"Code block `{label}` failed the generated-code preflight check: {item.message}"
            for item in author_time_diagnostics
        )
        unresolved_diagnostics = sandbox_unresolved_name_diagnostics(code, parameter_keys=parameter_keys)
        errors.extend(
            f"Code block `{label}` failed the sandbox name check: {item.message}" for item in unresolved_diagnostics
        )
    return errors


def _code_block_render_bound_names(workflow_yaml: str) -> set[str]:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return set()
    names = set(declared_workflow_parameter_keys(parsed))
    workflow_definition = parsed.get("workflow_definition")
    if isinstance(workflow_definition, dict):
        for key in credential_params(workflow_definition.get("parameters")):
            names.add(f"{key}_real_username")
            names.add(f"{key}_real_password")
    for block in workflow_blocks(parsed):
        label = str(block.get("label") or "").strip()
        if label:
            names.add(label)
            names.add(f"{label}_output")
    return names


def _code_block_render_reject(
    workflow_yaml: str, prior_yaml: str | None
) -> tuple[str, CodeBlockRenderDiagnostic] | None:
    prior_blocks = _workflow_yaml_code_blocks_by_label(prior_yaml)
    bound_names = _code_block_render_bound_names(workflow_yaml)
    for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items():
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        prior_block = prior_blocks.get(label)
        if prior_block is not None and str(prior_block.get("code") or "") == code:
            continue
        diagnostic = code_block_render_diagnostic(code, bound_names)
        if diagnostic is not None:
            return label, diagnostic
    return None


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


def _unresolved_symbol_repair_context_enabled(ctx: AgentContext) -> bool:
    return normalize_block_authoring_policy(ctx.block_authoring_policy) == BlockAuthoringPolicy.CODE_ONLY_BROWSER


def _declared_string_workflow_parameter_keys(parsed: Mapping[str, Any]) -> set[str]:
    return declared_string_workflow_parameter_keys(parsed)


def _code_block_available_binding_keys_by_label(workflow_yaml: str | None) -> dict[str, list[str]]:
    return code_block_available_binding_keys_by_label(workflow_yaml)


def _code_block_authoring_repair_context(
    workflow_yaml: str | None,
    prior_yaml: str | None,
) -> CodeAuthoringRepairContext | None:
    prior_blocks = _workflow_yaml_code_blocks_by_label(prior_yaml)
    available_binding_keys_by_label = _code_block_available_binding_keys_by_label(workflow_yaml)
    for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items():
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        parameter_keys = _code_block_parameter_keys(block)
        prior_block = prior_blocks.get(label)
        if (
            prior_block is not None
            and str(prior_block.get("code") or "") == code
            and _code_block_parameter_keys(prior_block) == parameter_keys
        ):
            continue
        try:
            CodeBlock.is_safe_code(code)
        except (SyntaxError, InsecureCodeDetected):
            continue
        if author_time_code_security_errors(label=label, code=code):
            continue
        if author_time_code_block_diagnostics(code):
            continue
        diagnostic = sandbox_unresolved_name_repair_diagnostic(code, parameter_keys=parameter_keys)
        if diagnostic is None or not diagnostic.unresolved_names:
            continue
        available_parameter_keys = available_binding_keys_by_label.get(label, [])
        binding_candidates = list(dict.fromkeys([*available_parameter_keys, *diagnostic.unresolved_names]))
        return CodeAuthoringRepairContext(
            block_label=label,
            reason_code=diagnostic.code,
            unresolved_names=list(diagnostic.unresolved_names),
            parameter_keys=list(diagnostic.parameter_keys),
            available_parameter_keys=available_parameter_keys,
            binding_candidates=binding_candidates,
            allowed_global_names=list(diagnostic.allowed_global_names),
            allowed_helper_surface={
                helper: list(attributes) for helper, attributes in diagnostic.allowed_helper_surface.items()
            },
            repair_instruction=(
                "For each workflow-input-like unresolved name, create a workflow string parameter with the exact "
                "same key when no declared parameter exists, add that exact key to this code block's "
                "parameter_keys, reference it as a bare Python variable in code, do not hardcode the eval value, "
                "and rerun via update_and_run_blocks."
            ),
        )
    return None


def _adopt_exact_declared_parameter_keys_for_unresolved_names(workflow_yaml: str) -> str:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml
    declared_string_keys = _declared_string_workflow_parameter_keys(parsed)
    workflow_definition = parsed.get("workflow_definition")
    parameters = workflow_definition.get("parameters") if isinstance(workflow_definition, dict) else None
    declared_credential_keys: set[str] = set()
    if isinstance(parameters, list):
        declared_credential_keys = {
            str(parameter.get("key") or "").strip()
            for parameter in parameters
            if isinstance(parameter, dict)
            if str(parameter.get("key") or "").strip() and _is_credential_parameter(parameter)
        }
    declared_adoptable_keys = declared_string_keys | declared_credential_keys
    if not declared_adoptable_keys:
        return workflow_yaml
    available_binding_keys_by_label = _code_block_available_binding_keys_by_label(workflow_yaml)
    adopted_by_label: dict[str, list[str]] = {}
    for block in _workflow_code_blocks(parsed):
        label = str(block.get("label") or "").strip()
        if not label:
            continue
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        parameter_keys = _code_block_parameter_keys(block)
        available_declared_keys = (
            set(available_binding_keys_by_label.get(label, [])) & declared_string_keys
        ) | declared_credential_keys
        if not available_declared_keys:
            continue
        diagnostic = sandbox_unresolved_name_repair_diagnostic(code, parameter_keys=parameter_keys)
        if diagnostic is None or not diagnostic.unresolved_names:
            continue
        adopted_keys = [
            name
            for name in diagnostic.unresolved_names
            if name in available_declared_keys and name not in parameter_keys
        ]
        if not adopted_keys:
            continue
        raw_keys = block.get("parameter_keys")
        merged_keys = (
            [str(key) for key in raw_keys if isinstance(key, str) and key] if isinstance(raw_keys, list) else []
        )
        for key in adopted_keys:
            if key not in merged_keys:
                merged_keys.append(key)
        block["parameter_keys"] = merged_keys
        adopted_by_label[label] = adopted_keys
    if not adopted_by_label:
        return workflow_yaml
    LOG.info("copilot adopted exact declared parameter keys for unresolved names", adopted_by_label=adopted_by_label)
    return yaml.safe_dump(parsed, sort_keys=False)


def _repair_context_log_values(values: list[str], *, max_items: int = 20) -> list[str]:
    cleaned: list[str] = []
    for raw_value in values[:max_items]:
        value = str(raw_value or "").replace("\r", " ").replace("\n", " ").strip()
        if not value or _SECRET_LIKE_LITERAL_RE.search(value):
            continue
        cleaned.append(value[:80])
    return cleaned


def _set_code_authoring_repair_context(ctx: AgentContext, repair_context: CodeAuthoringRepairContext | None) -> None:
    ctx.last_code_authoring_repair_context = repair_context
    if repair_context is not None:
        record_build_test_outcome(ctx, recorded_outcome_from_authoring_repair_context(repair_context))
        LOG.info(
            "copilot code authoring repair context stored",
            reason_code=repair_context.reason_code,
            block_label=repair_context.block_label,
            unresolved_names=_repair_context_log_values(repair_context.unresolved_names),
            parameter_keys=_repair_context_log_values(repair_context.parameter_keys),
            available_parameter_keys=_repair_context_log_values(repair_context.available_parameter_keys),
            binding_candidates=_repair_context_log_values(repair_context.binding_candidates),
        )


def _clear_code_authoring_repair_context(ctx: AgentContext) -> None:
    ctx.last_code_authoring_repair_context = None


def _is_unresolved_symbol_repair_context(repair_context: CodeAuthoringRepairContext | None) -> bool:
    return repair_context is not None and repair_context.reason_code == SANDBOX_UNRESOLVED_NAME_REASON_CODE


def _record_author_time_reject_outcome(
    ctx: AgentContext,
    *,
    reason_code: BuildTestOutcomeReasonCode,
    summary: str,
    structural_payload: Mapping[str, object] | None = None,
    authored_structure_signature: str | None = None,
    block_labels: list[str] | None = None,
    missing_requested_output_facts: list[dict[str, object]] | None = None,
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
    )


def _code_safety_reject_payload(errors: list[str | CodeBlockSecurityError]) -> Mapping[str, object] | None:
    entries: list[dict[str, object]] = []
    for error in errors:
        if isinstance(error, CodeBlockSecurityError):
            entries.append(
                {
                    "block_label": error.block_label,
                    "reason_code": error.reason_code,
                    "surface": error.surface,
                }
            )
    if not entries:
        return None
    return {"code_safety_errors": entries}


def _credential_scout_reject_payload(workflow_yaml: str) -> Mapping[str, object] | None:
    entries: list[dict[str, object]] = []
    for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items():
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        accesses = [
            {
                "parameter_key": access.parameter_key,
                "field": access.field,
                "requires_live_scout": access.requires_live_scout,
            }
            for access in _credential_field_accesses(code)
        ]
        if accesses or _CODE_SUBMIT_ACTION_RE.search(code):
            entries.append(
                {
                    "block_label": label,
                    "credential_field_accesses": accesses,
                    "requires_submit": bool(_CODE_SUBMIT_ACTION_RE.search(code)),
                }
            )
    if not entries:
        return None
    return {"credential_scout_requirements": entries}


def _credential_scout_reopen_identity_digest(workflow_yaml: str) -> str:
    structural_identity = "author_time:" + _stable_hash(_credential_scout_reject_payload(workflow_yaml) or {})
    accessed_parameter_keys: set[str] = set()
    for block in _workflow_yaml_code_blocks_by_label(workflow_yaml).values():
        code = str(block.get("code") or "")
        accessed_parameter_keys.update(
            access.parameter_key for access in _credential_field_accesses(code) if access.requires_live_scout
        )
    credential_params_by_key: dict[str, set[str]] = {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if isinstance(parsed, dict):
        workflow_definition = parsed.get("workflow_definition")
        if isinstance(workflow_definition, dict):
            credential_params_by_key = credential_param_ids(workflow_definition.get("parameters"))
    binding = {key: sorted(ids) for key, ids in credential_params_by_key.items() if key in accessed_parameter_keys}
    return f"{structural_identity}|{_stable_hash(binding)}"


def _code_artifact_metadata_reject_payload(
    *,
    workflow_yaml: str,
    raw_metadata: object,
    offending_labels: list[str],
    violation_categories: list[str],
    missing_labels: list[str] | None = None,
) -> Mapping[str, object] | None:
    labels = sorted(dict.fromkeys([*offending_labels, *(missing_labels or [])]))
    if not labels:
        labels = sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml))
    payload = {
        "reason_code": "metadata_reject",
        "offending_labels": labels,
        "required_fields": [
            "declared_goal",
            *_CODE_ARTIFACT_REQUIRED_LIST_FIELDS,
            "evidence_refs_or_observation_refs",
        ],
        "missing_fields_by_label": _metadata_missing_required_fields_by_label(
            raw_metadata,
            labels=labels,
            missing_labels=missing_labels or [],
        ),
        "output_path_roots": _metadata_output_path_roots(raw_metadata),
        "output_path_roots_by_label": _metadata_output_path_roots_by_label(raw_metadata),
        "code_block_output_status": _metadata_reject_code_block_output_status(
            workflow_yaml,
            raw_metadata=raw_metadata,
            labels=labels,
        ),
        "violation_categories": sorted(dict.fromkeys(violation_categories)),
    }
    return payload


def _metadata_missing_required_fields_by_label(
    raw_metadata: object,
    *,
    labels: list[str],
    missing_labels: list[str],
) -> dict[str, list[str]]:
    items_by_label: dict[str, Mapping[str, Any]] = {}
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        label = str(item.get("block_label") or "").strip()
        if label and label not in items_by_label:
            items_by_label[label] = item

    missing_label_set = set(missing_labels)
    missing_by_label: dict[str, list[str]] = {}
    for label in labels:
        item = items_by_label.get(label)
        if item is None or label in missing_label_set:
            missing_by_label[label] = [
                "declared_goal",
                *_CODE_ARTIFACT_REQUIRED_LIST_FIELDS,
                "evidence_refs_or_observation_refs",
            ]
            continue
        missing_fields: list[str] = []
        if not str(item.get("declared_goal") or "").strip():
            missing_fields.append("declared_goal")
        for field_name in _CODE_ARTIFACT_REQUIRED_LIST_FIELDS:
            value = item.get(field_name)
            if not isinstance(value, list) or not value:
                missing_fields.append(field_name)
        if not item.get("evidence_refs") and not item.get("observation_refs"):
            missing_fields.append("evidence_refs_or_observation_refs")
        if missing_fields:
            missing_by_label[label] = missing_fields
    return missing_by_label


def _metadata_output_path_roots(raw_metadata: object) -> list[str]:
    roots: set[str] = set()
    for label_roots in _metadata_output_path_roots_by_label(raw_metadata).values():
        roots.update(label_roots)
    return sorted(roots)


def _metadata_output_paths(raw_metadata: object) -> set[str]:
    paths: set[str] = set()
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is not None:
            paths.update(_metadata_item_output_paths(item))
    return paths


def _metadata_extraction_schema_paths(raw_metadata: object) -> set[str]:
    paths: set[str] = set()
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is not None:
            paths.update(_metadata_item_extraction_schema_paths(item))
    return paths


def _metadata_output_path_roots_by_label(raw_metadata: object) -> dict[str, list[str]]:
    roots_by_label: dict[str, set[str]] = {}
    for label, paths in _metadata_output_paths_by_label(raw_metadata).items():
        roots = roots_by_label.setdefault(label, set())
        for path in paths:
            root = _top_level_path_segment(path)
            if root:
                roots.add(root)
    return {label: sorted(roots) for label, roots in sorted(roots_by_label.items()) if roots}


def _metadata_output_paths_by_label(raw_metadata: object) -> dict[str, list[str]]:
    paths_by_label: dict[str, set[str]] = {}
    unlabeled_index = 0
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        label = str(item.get("block_label") or "").strip()
        if not label:
            unlabeled_index += 1
            label = f"<unlabeled:{unlabeled_index}>"
        paths = paths_by_label.setdefault(label, set())
        paths.update(_metadata_item_output_paths(item))
    return {label: sorted(paths) for label, paths in sorted(paths_by_label.items()) if paths}


def _metadata_item_output_path_roots(item: Mapping[str, Any]) -> set[str]:
    roots: set[str] = set()
    for path in _metadata_item_output_paths(item):
        root = _top_level_path_segment(path)
        if root:
            roots.add(root)
    return roots


def _metadata_item_output_paths(item: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            paths.update(_artifact_goal_value_paths(row.get("goal_value_paths")))
    paths.update(_metadata_item_extraction_schema_paths(item))
    return paths


def _metadata_item_extraction_schema_paths(item: Mapping[str, Any]) -> set[str]:
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            schema = _parse_extraction_schema(row.get("extraction_schema"))
            if schema is not None:
                return _schema_property_paths(schema)
    return set()


def _metadata_reject_code_block_output_status(
    workflow_yaml: str,
    *,
    raw_metadata: object,
    labels: list[str],
) -> dict[str, Mapping[str, object]]:
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    metadata_by_label: dict[str, Mapping[str, Any]] = {}
    for raw_item in _code_artifact_metadata_items(raw_metadata):
        item = _raw_metadata_item_mapping(raw_item)
        if item is None:
            continue
        label = str(item.get("block_label") or "").strip()
        if label and label not in metadata_by_label:
            metadata_by_label[label] = item

    status: dict[str, Mapping[str, object]] = {}
    for label in labels:
        block = code_blocks.get(label)
        if block is None:
            continue
        block_code = str(block.get("code") or "")
        metadata = metadata_by_label.get(label)
        status[label] = {
            "block_type": _enum_or_string_name(block.get("block_type")) or str(block.get("block_type") or ""),
            "has_code": bool(block_code.strip()),
            "declares_output_intent": _block_declares_output_intent(block),
            "declares_output_roots": sorted(_metadata_item_output_path_roots(metadata)) if metadata else [],
            "has_meaningful_output": _code_block_has_meaningful_output(block_code, metadata),
        }
    return status


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


def _metadata_violation_categories(violations: list[str]) -> list[str]:
    categories: list[str] = []
    for violation in violations:
        if "requires non-empty" in violation:
            categories.append("missing_required_list")
        elif "requires a non-empty `declared_goal`" in violation:
            categories.append("missing_declared_goal")
        elif "requires `evidence_refs` or `observation_refs`" in violation:
            categories.append("missing_artifact_refs")
        elif "requires `source_tool`" in violation:
            categories.append("missing_source_tool")
        elif "goal_value_paths" in violation:
            categories.append("invalid_goal_value_paths")
        elif "extraction_schema" in violation:
            categories.append("invalid_extraction_schema")
        elif "return" in violation or "output" in violation:
            categories.append("invalid_output_shape")
        else:
            categories.append("metadata_contract_violation")
    return categories


class _ConvergenceReject(NamedTuple):
    authored_structure_signature: str
    reason: Literal["identical_authored_structure", "frontier_unchanged"]
    commit_early_terminal: bool


def _recorded_outcome_convergence_reject(
    ctx: AgentContext,
    *,
    workflow_yaml: str,
    code_artifact_metadata: object,
) -> _ConvergenceReject | None:
    latest = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if not isinstance(latest, RecordedBuildTestOutcome) or not latest.is_authoritative:
        return None
    candidate_signature = authored_structure_signature_from_workflow(workflow_yaml, code_artifact_metadata)
    if candidate_signature is None:
        return None
    if candidate_signature == latest.authored_structure_signature:
        return _ConvergenceReject(candidate_signature, "identical_authored_structure", False)
    constraint = getattr(ctx, "recorded_outcome_binding_constraint", None)
    if not isinstance(constraint, RecordedOutcomeBindingConstraint):
        return None
    # An author-time reject re-keys `latest` without an executed run, so keep the binding
    # anchored to its run-outcome key across consecutive frontier-unchanged rejects until a
    # real run legitimately re-keys it.
    if latest.phase != "author_time_reject" and constraint.repeated_structural_key != latest.structural_key:
        return None
    candidate_block_signatures = authored_block_signatures_from_workflow(workflow_yaml, code_artifact_metadata)
    if constraint.owning_block_frontier_moved(candidate_block_signatures):
        return None
    return _ConvergenceReject(candidate_signature, "frontier_unchanged", constraint.frontier_uncrossable)


def _commit_recorded_outcome_early_terminal(ctx: AgentContext) -> None:
    constraint = ctx.recorded_outcome_binding_constraint
    diagnostic_reason = (
        constraint.diagnostic_reason if isinstance(constraint, RecordedOutcomeBindingConstraint) else "none"
    )
    signal = CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=(
            f"The recorded build-test outcome names an uncrossable page frontier ({diagnostic_reason}) and the "
            "frontier block is unchanged; stop retrying and report the recorded blocker from the preserved draft."
        ),
        user_facing_reason="I can't get past this page, so I'll stop here and report what I found.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="repair_ceiling_reached",
        blocked_tool="update_workflow",
    )
    stash_blocker_signal(ctx, signal)
    stash_repair_ceiling_turn_halt(
        ctx, signal, consecutive_identical_repair_count=ctx.consecutive_non_converging_repair_count
    )


def _recorded_outcome_requires_output_candidate(ctx: AgentContext) -> bool:
    latest = ctx.latest_recorded_build_test_outcome
    if not isinstance(latest, RecordedBuildTestOutcome) or not latest.is_authoritative:
        return False
    if latest.phase != "persisted_block_run" or latest.reason_code != "outcome_not_demonstrated":
        return False
    if latest.missing_requested_output_facts:
        return True
    verification = getattr(ctx, "completion_verification_result", None)
    if verification is None or getattr(verification, "status", None) != "evaluated":
        return False
    return not verification.is_fully_satisfied()


def _recorded_outcome_missing_output_paths(ctx: AgentContext) -> set[str]:
    latest = ctx.latest_recorded_build_test_outcome
    paths: set[str] = set()
    if isinstance(latest, RecordedBuildTestOutcome) and latest.reason_code == "outcome_not_demonstrated":
        for fact in latest.missing_requested_output_facts:
            path = str(fact.get("output_path") or "").strip()
            if path:
                paths.add(path)
                continue
            root = str(fact.get("output_root") or "").strip()
            if root:
                paths.add(root)
    if paths:
        return paths
    verification = getattr(ctx, "completion_verification_result", None)
    if verification is None or getattr(verification, "status", None) != "evaluated":
        return set()
    for verdict in getattr(verification, "verdicts", []):
        if getattr(verdict, "satisfied", False):
            continue
        output_path = str(getattr(verdict, "output_path", "") or "").strip()
        if output_path:
            paths.add(output_path)
    return paths


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


SYNTHESIZED_BUSINESS_INPUT_FLOOR_REASON_CODE = "synthesized_business_input_floor_unsatisfied"


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


def _synthesized_business_input_floor_reject(
    ctx: AgentContext,
    workflow_yaml: str,
    imposition: _SynthesizedCodeImpositionResult,
) -> _DefinitionPlaneReject | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    if not (
        imposition.substitutions is not None or ctx.synthesized_block_offered or ctx.spine_imposition_owned_attempt
    ):
        return None
    criteria = _active_completion_criteria(ctx)
    has_business_goal = any(
        criterion.kind == "terminal_action"
        or (
            criterion.level == "run"
            and criterion.requested_output_floor_rekeyed
            and bool(criterion.floor_rekeyed_from_path)
        )
        for criterion in criteria
    )
    if not has_business_goal:
        return None
    rejection = _definition_plane_preflight_reject(
        ctx,
        workflow_yaml,
        enforce_untagged_declared_inputs=True,
    )
    retained_required_keys = set(getattr(ctx, "synthesized_business_required_parameter_keys", set()))
    if not retained_required_keys:
        return rejection if rejection is not None and rejection.unreferenced_parameter_keys else None
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return rejection if rejection is not None and rejection.unreferenced_parameter_keys else None
    runtime_sources = _workflow_runtime_parameter_sources(parsed)
    if runtime_sources is None:
        return rejection if rejection is not None and rejection.unreferenced_parameter_keys else None
    missing_retained_keys = retained_required_keys - runtime_sources
    if not missing_retained_keys:
        return rejection if rejection is not None and rejection.unreferenced_parameter_keys else None
    current_missing_keys = set(rejection.unreferenced_parameter_keys) if rejection is not None else set()
    return _DefinitionPlaneReject(
        criterion_ids=(
            rejection.criterion_ids if rejection is not None else tuple(criterion.id for criterion in criteria)
        ),
        reason_codes=(
            rejection.reason_codes if rejection is not None else ("synthesized_required_parameters_unreferenced",)
        ),
        unreferenced_parameter_keys=tuple(sorted(current_missing_keys | missing_retained_keys)),
    )


def _reopen_scout_after_synthesized_business_input_floor(ctx: AgentContext) -> None:
    ctx.synthesized_block_offered = False
    ctx.synthesized_block_offered_trajectory_len = 0
    ctx.synthesized_block_offered_goal_complete = False
    ctx.spine_imposition_owned_attempt = False


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


def _record_definition_plane_reject(
    ctx: AgentContext,
    workflow_yaml: str,
    rejection: _DefinitionPlaneReject,
    *,
    code_artifact_metadata: object = None,
) -> None:
    structural_payload, authored_signature = _definition_plane_structural_payload(
        workflow_yaml,
        rejection,
        code_artifact_metadata,
    )
    block_labels = sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml))
    _record_author_time_reject_outcome(
        ctx,
        reason_code="definition_contract_unsatisfied",
        summary=_definition_plane_reject_error(rejection),
        structural_payload=structural_payload,
        authored_structure_signature=authored_signature,
        block_labels=block_labels,
    )


def _record_definition_plane_ablation_event(
    ctx: AgentContext,
    workflow_yaml: str,
    rejection: _DefinitionPlaneReject,
    *,
    code_artifact_metadata: object = None,
) -> bool:
    fingerprint = authored_structure_signature_from_workflow(workflow_yaml, code_artifact_metadata)
    if fingerprint is None:
        return False
    payload: AuthorTimeGateAblationPayload = {
        "criterion_ids": list(rejection.criterion_ids),
        "definition_reason_codes": list(rejection.reason_codes),
        "unreferenced_parameter_keys": list(rejection.unreferenced_parameter_keys),
    }
    return record_author_time_gate_ablation_event(
        ctx,
        gate_id=DEFINITION_CONTRACT_UNSATISFIED_GATE_ID,
        reason_code="definition_contract_unsatisfied",
        fingerprint=fingerprint,
        blocked_tool="update_workflow",
        payload=payload,
    )


def _stash_unresolved_recorded_outcome_grounding_halt(
    ctx: AgentContext,
    unresolved_parameter_keys: Iterable[str],
) -> bool:
    requirement = ctx.recorded_outcome_grounding_requirement
    constraint = ctx.recorded_outcome_binding_constraint
    latest = ctx.latest_recorded_build_test_outcome
    if (
        not isinstance(requirement, RecordedOutcomeGroundingRequirement)
        or requirement.phase != "author_time_reject"
        or not requirement.satisfied
        or not isinstance(constraint, RecordedOutcomeBindingConstraint)
        or constraint.repeated_structural_key != requirement.structural_key
        or not isinstance(latest, RecordedBuildTestOutcome)
        or latest.structural_key != requirement.structural_key
        or latest_recorded_build_test_outcome_repeated(ctx) is not True
    ):
        return False
    keys = sorted(set(unresolved_parameter_keys))
    if not keys:
        return False
    payload: AuthorTimeGateAblationPayload = {
        "phase": requirement.phase,
        "outcome_reason_code": requirement.reason_code,
        "workflow_run_id": requirement.workflow_run_id,
        "block_labels": list(requirement.block_labels),
        "unresolved_parameter_keys": keys,
    }
    if record_author_time_gate_ablation_event(
        ctx,
        gate_id=RECORDED_OUTCOME_GROUNDING_BINDER_CEILING_GATE_ID,
        reason_code="definition_contract_unsatisfied",
        fingerprint=requirement.structural_key,
        blocked_tool="update_workflow",
        payload=payload,
    ):
        return False
    signal = build_definition_contract_unsatisfied_blocker_signal(
        unresolved_parameter_keys=keys,
        grounding_unresolved=True,
    )
    claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, signal, force_stash=True)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="recorded_outcome_regrounding")
    LOG.info(
        "copilot recorded outcome regrounding halted before reauthor",
        structural_key=requirement.structural_key,
        unresolved_parameter_keys=keys,
    )
    return True


def _scout_trajectory_index(interaction: Mapping[str, Any], position: int) -> int:
    raw_index = interaction.get("trajectory_index")
    return raw_index if isinstance(raw_index, int) and raw_index >= 0 else position


class _AuthoringParameterBindingResolution(NamedTuple):
    snapshot: AuthoringParameterBindingSnapshot | None
    directive: AuthoringParameterBindingDirective | None


def _same_month_file_match_transform(
    parsed: Mapping[str, Any],
    runtime_parameters: Mapping[str, Any] | None,
    *,
    candidate_keys: Sequence[str],
    selector: str,
) -> SameMonthFileMatchTransform | None:
    declared_keys = _declared_string_workflow_parameter_keys(parsed)
    if not set(candidate_keys).issubset(declared_keys):
        return None
    definition = parsed.get("workflow_definition")
    rows = definition.get("parameters") if isinstance(definition, Mapping) else None
    if not isinstance(rows, list):
        return None
    defaults = {
        str(row.get("key") or "").strip(): default
        for row in rows
        if isinstance(row, Mapping) and (default := _string_parameter_default_value(row)) is not None
    }
    supplied = runtime_parameters if isinstance(runtime_parameters, Mapping) else {}
    values: dict[str, str] = {}
    for key in candidate_keys:
        runtime_value = supplied.get(key)
        value = runtime_value if isinstance(runtime_value, str) else defaults.get(key)
        if isinstance(value, str):
            values[key] = value
    correspondences = input_correspondences_for_selector(selector, values)
    return derive_same_month_file_match_transform(
        selector=selector,
        parameter_values=values,
        identity_correspondences=correspondences,
    )


def _runtime_declared_string_parameter_values(
    parsed: Mapping[str, Any], runtime_parameters: Mapping[str, Any] | None
) -> dict[str, str]:
    definition = parsed.get("workflow_definition")
    rows = definition.get("parameters") if isinstance(definition, Mapping) else None
    if not isinstance(rows, list):
        return {}
    declared_keys = _declared_string_workflow_parameter_keys(parsed)
    supplied = runtime_parameters if isinstance(runtime_parameters, Mapping) else {}
    values: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("key") or "").strip()
        if key not in declared_keys:
            continue
        runtime_value = supplied.get(key)
        value = runtime_value if isinstance(runtime_value, str) else _string_parameter_default_value(row)
        if isinstance(value, str):
            values[key] = value
    return values


class _SelectionBindingRecord(NamedTuple):
    declared_key: str
    match_basis: AuthoringParameterBindingMatchBasis
    field_selector: str
    trajectory_index: int
    terminal_tool: AuthoringParameterBindingTerminalTool
    terminal_selector: str


def _selection_parameter_binding_resolution(
    ctx: AgentContext,
    *,
    target_keys: Sequence[str],
    ephemeral_values: Mapping[str, Any],
    structural_key: str,
    source_origin: str,
) -> _AuthoringParameterBindingResolution:
    trajectory = ctx.scout_trajectory
    if not isinstance(trajectory, list) or not trajectory:
        return _AuthoringParameterBindingResolution(None, None)
    target = set(target_keys)
    admitted: list[_SelectionBindingRecord] = []
    for position, interaction in enumerate(trajectory):
        if not isinstance(interaction, Mapping):
            continue
        if url_origin(str(interaction.get("source_url") or "").strip()) != source_origin:
            continue
        tool = str(interaction.get("tool_name") or "")
        index = _scout_trajectory_index(interaction, position)
        if tool == "click":
            templated = templated_selection_locator_binding(interaction)
            if templated is None:
                continue
            key, join_selector = templated
            if key in target:
                admitted.append(
                    _SelectionBindingRecord(
                        key,
                        "scouted_selection_value",
                        join_selector,
                        index,
                        "click",
                        str(interaction.get("selector") or "").strip(),
                    )
                )
        elif tool == "select_option":
            value = str(interaction.get("value") or "").strip()
            selector = _safe_selector_repair_atom(interaction.get("selector"))
            if not value or not selector:
                continue
            for key in target_keys:
                key_value = ephemeral_values.get(key)
                if isinstance(key_value, str) and key_value == value and selection_option_value_admissible(value, key):
                    admitted.append(
                        _SelectionBindingRecord(key, "scouted_option_value", selector, index, "select_option", selector)
                    )
    if not admitted:
        return _AuthoringParameterBindingResolution(None, None)
    directive = build_authoring_parameter_binding_directive(
        structural_key=structural_key,
        source_origin=source_origin,
        candidates=[
            AuthoringParameterBindingCandidate(declared_key=record.declared_key, field_selector=record.field_selector)
            for record in admitted
        ],
    )
    resolved: list[AuthoringParameterFieldBinding] = []
    for key in target_keys:
        matches = [record for record in admitted if record.declared_key == key]
        if len(matches) != 1:
            return _AuthoringParameterBindingResolution(None, directive)
        record = matches[0]
        resolved.append(
            AuthoringParameterFieldBinding(
                declared_key=key,
                field_selector=record.field_selector,
                field_trajectory_index=record.trajectory_index,
                match_basis=record.match_basis,
            )
        )
    if len({binding.field_selector for binding in resolved}) != len(resolved):
        return _AuthoringParameterBindingResolution(None, directive)
    terminal_record = max(admitted, key=lambda record: record.trajectory_index)
    terminal = AuthoringParameterTerminalBinding(
        tool_name=terminal_record.terminal_tool,
        trajectory_index=terminal_record.trajectory_index,
        selector=terminal_record.terminal_selector,
    )
    snapshot = build_authoring_parameter_binding_snapshot(
        structural_key=structural_key,
        source_origin=source_origin,
        field_bindings=resolved,
        terminal=terminal,
    )
    LOG.info(
        "copilot recorded outcome selection rung bound",
        binding_fingerprint=snapshot.fingerprint,
        parameter_keys=[binding.declared_key for binding in snapshot.field_bindings],
        binding_count=len(snapshot.field_bindings),
        terminal_tool=snapshot.terminal.tool_name,
        terminal_trajectory_index=snapshot.terminal.trajectory_index,
    )
    return _AuthoringParameterBindingResolution(snapshot, directive)


def _pending_authoring_parameter_binding_directive(
    ctx: AgentContext,
) -> tuple[CodeAuthoringRepairContext, AuthoringParameterBindingDirective] | None:
    repair_context = ctx.last_code_authoring_repair_context
    if (
        not isinstance(repair_context, CodeAuthoringRepairContext)
        or repair_context.reason_code != _SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE
        or repair_context.parameter_binding_directive is None
    ):
        return None
    return repair_context, repair_context.parameter_binding_directive


def _authoring_parameter_binding_resolution(
    ctx: AgentContext,
    parsed: Mapping[str, Any],
    runtime_parameters: Mapping[str, Any] | None,
    *,
    unreferenced_parameter_keys: Sequence[str],
    carrier_code: str,
    diagnostic_structural_key: str,
) -> _AuthoringParameterBindingResolution:
    requirement = ctx.recorded_outcome_grounding_requirement
    constraint = ctx.recorded_outcome_binding_constraint
    repeated_grounding_payload = None
    structural_key = diagnostic_structural_key
    if (
        isinstance(requirement, RecordedOutcomeGroundingRequirement)
        and requirement.satisfied
        and requirement.payload is not None
        and requirement.payload.source_tool == "inspect_page_for_composition"
        and requirement.payload.target_url == "current_page"
        and isinstance(constraint, RecordedOutcomeBindingConstraint)
        and constraint.repeated_structural_key == requirement.structural_key
    ):
        repeated_grounding_payload = requirement.payload
        structural_key = constraint.repeated_structural_key
    if not structural_key:
        return _AuthoringParameterBindingResolution(None, None)
    target_keys = tuple(sorted(set(unreferenced_parameter_keys)))
    if not target_keys or any(not grounded_parameter_key_is_safe(key) for key in target_keys):
        return _AuthoringParameterBindingResolution(None, None)
    declared_keys = _declared_string_workflow_parameter_keys(parsed)
    if not set(target_keys).issubset(declared_keys):
        return _AuthoringParameterBindingResolution(None, None)

    evidence = ctx.composition_page_evidence
    forms = evidence.get("forms") if isinstance(evidence, Mapping) else None
    current_url = str(evidence.get("current_url") or "").strip() if isinstance(evidence, Mapping) else ""
    source_tool = str(evidence.get("source_tool") or "").strip() if isinstance(evidence, Mapping) else ""
    if not isinstance(forms, list) or not current_url or source_tool != "inspect_page_for_composition":
        return _AuthoringParameterBindingResolution(None, None)
    if repeated_grounding_payload is not None and repeated_grounding_payload.source_url != current_url:
        return _AuthoringParameterBindingResolution(None, None)
    source_origin = url_origin(current_url)
    if source_origin is None:
        return _AuthoringParameterBindingResolution(None, None)

    definition = parsed.get("workflow_definition")
    rows = definition.get("parameters") if isinstance(definition, Mapping) else None
    defaults = (
        {str(row.get("key") or "").strip(): row.get("default_value") for row in rows if isinstance(row, Mapping)}
        if isinstance(rows, list)
        else {}
    )
    supplied = runtime_parameters if isinstance(runtime_parameters, Mapping) else {}
    ephemeral_values = {key: supplied.get(key, defaults.get(key)) for key in target_keys}
    authored_bindings = authored_selector_parameter_bindings(carrier_code, set(target_keys))
    if authored_bindings is None:
        return _AuthoringParameterBindingResolution(None, None)

    terminal_records: list[tuple[int, Mapping[str, Any]]] = []
    for position, interaction in enumerate(ctx.scout_trajectory):
        if not isinstance(interaction, Mapping):
            continue
        if _is_submit_interaction(interaction) and str(interaction.get("source_url") or "").strip() == current_url:
            terminal_records.append((position, interaction))

    snapshots: list[AuthoringParameterBindingSnapshot] = []
    candidate_pairs: list[AuthoringParameterBindingCandidate] = []
    any_active_fields = False
    for form in forms:
        if not isinstance(form, Mapping):
            continue
        fields = [field for field in form.get("fields") or [] if isinstance(field, Mapping)]
        active_fields = [
            field for field in fields if field.get("disabled") is not True and str(field.get("selector") or "").strip()
        ]
        any_active_fields = any_active_fields or bool(active_fields)
        field_selectors = {str(field.get("selector") or "").strip() for field in active_fields}
        submit_selectors = {
            str(control.get("selector") or "").strip()
            for control in form.get("submit_controls") or []
            if isinstance(control, Mapping)
            and control.get("disabled") is not True
            and str(control.get("selector") or "").strip()
        }
        terminals = [
            (position, interaction)
            for position, interaction in terminal_records
            if (
                str(interaction.get("tool_name") or "") == "click"
                and str(interaction.get("selector") or "").strip() in submit_selectors
            )
            or (
                str(interaction.get("tool_name") or "") == "press_key"
                and str(interaction.get("selector") or "").strip() in field_selectors
            )
        ]
        if len(terminals) != 1:
            continue
        terminal_trajectory_index = _scout_trajectory_index(terminals[0][1], terminals[0][0])

        resolved: list[AuthoringParameterFieldBinding] = []
        used_selectors: set[str] = set()
        ambiguous = False
        for key in target_keys:
            selector_basis: list[tuple[str, str]] = []
            authored_selectors = sorted(
                selector for selector, keys in authored_bindings.items() if key in keys and selector in field_selectors
            )
            if authored_selectors:
                selector_basis = [(selector, "exact_authored_selector") for selector in authored_selectors]
            else:
                correspondence_selectors = sorted(
                    {
                        str(interaction.get("selector") or "").strip()
                        for interaction in ctx.scout_trajectory
                        if isinstance(interaction, Mapping)
                        and str(interaction.get("selector") or "").strip() in field_selectors
                        and any(
                            isinstance(correspondence, Mapping) and correspondence.get("input_key") == key
                            for correspondence in interaction.get("input_correspondences") or []
                        )
                    }
                )
                if correspondence_selectors:
                    selector_basis = [
                        (selector, "grounded_input_correspondence") for selector in correspondence_selectors
                    ]
                else:
                    value = ephemeral_values.get(key)
                    if isinstance(value, str) and value:
                        value_selectors = sorted(
                            str(field.get("selector") or "").strip()
                            for field in active_fields
                            if field.get("value") == value
                        )
                        selector_basis = [(selector, "unique_ephemeral_value") for selector in value_selectors]
            for selector, _basis in selector_basis:
                sanitized_selector = _safe_selector_repair_atom(selector)
                if sanitized_selector:
                    candidate_pairs.append(
                        AuthoringParameterBindingCandidate(
                            declared_key=key,
                            field_selector=sanitized_selector,
                        )
                    )
            if len(selector_basis) != 1:
                ambiguous = True
                continue
            selector, basis = selector_basis[0]
            matching_fills = [
                _scout_trajectory_index(interaction, position)
                for position, interaction in enumerate(ctx.scout_trajectory)
                if isinstance(interaction, Mapping)
                and str(interaction.get("tool_name") or "") == "type_text"
                and str(interaction.get("source_url") or "").strip() == current_url
                and str(interaction.get("selector") or "").strip() == selector
            ]
            if len(matching_fills) > 1 or selector in used_selectors:
                ambiguous = True
                continue
            if any(fill_index > terminal_trajectory_index for fill_index in matching_fills):
                ambiguous = True
                continue
            used_selectors.add(selector)
            resolved.append(
                AuthoringParameterFieldBinding(
                    declared_key=key,
                    field_selector=selector,
                    field_trajectory_index=matching_fills[0] if matching_fills else None,
                    match_basis=cast(AuthoringParameterBindingMatchBasis, basis),
                )
            )
        if ambiguous or len(resolved) != len(target_keys):
            continue
        terminal_position, terminal_interaction = terminals[0]
        terminal = AuthoringParameterTerminalBinding(
            tool_name=cast(AuthoringParameterBindingTerminalTool, str(terminal_interaction.get("tool_name") or "")),
            trajectory_index=_scout_trajectory_index(terminal_interaction, terminal_position),
            selector=str(terminal_interaction.get("selector") or "").strip(),
            key=str(terminal_interaction.get("key") or "").strip(),
        )
        snapshots.append(
            build_authoring_parameter_binding_snapshot(
                structural_key=structural_key,
                source_origin=source_origin,
                field_bindings=resolved,
                terminal=terminal,
            )
        )

    unique_snapshots = {snapshot.fingerprint: snapshot for snapshot in snapshots}
    directive = (
        build_authoring_parameter_binding_directive(
            structural_key=structural_key,
            source_origin=source_origin,
            candidates=candidate_pairs,
        )
        if candidate_pairs
        else None
    )
    if len(unique_snapshots) != 1:
        if not any_active_fields:
            selection = _selection_parameter_binding_resolution(
                ctx,
                target_keys=target_keys,
                ephemeral_values=ephemeral_values,
                structural_key=structural_key,
                source_origin=source_origin,
            )
            if selection.snapshot is not None:
                return selection
            return _AuthoringParameterBindingResolution(None, directive or selection.directive)
        return _AuthoringParameterBindingResolution(None, directive)
    snapshot = next(iter(unique_snapshots.values()))
    LOG.info(
        "copilot recorded outcome submit rung bound",
        binding_fingerprint=snapshot.fingerprint,
        parameter_keys=[binding.declared_key for binding in snapshot.field_bindings],
        binding_count=len(snapshot.field_bindings),
        terminal_tool=snapshot.terminal.tool_name,
        terminal_trajectory_index=snapshot.terminal.trajectory_index,
    )
    return _AuthoringParameterBindingResolution(snapshot, directive)


def _authoring_parameter_binding_resolution_for_definition(
    ctx: AgentContext,
    parsed: Mapping[str, Any],
    runtime_parameters: Mapping[str, Any] | None,
    code_artifact_metadata: object = None,
) -> _AuthoringParameterBindingResolution | None:
    workflow_yaml = yaml.safe_dump(dict(parsed), sort_keys=False)
    rejection = _definition_plane_preflight_reject(ctx, workflow_yaml)
    if rejection is None:
        return None
    return _authoring_parameter_binding_resolution(
        ctx,
        parsed,
        runtime_parameters,
        unreferenced_parameter_keys=rejection.unreferenced_parameter_keys,
        carrier_code="",
        diagnostic_structural_key=_definition_plane_structural_key(
            workflow_yaml,
            rejection,
            code_artifact_metadata,
        ),
    )


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
    if _recorded_outcome_requires_output_candidate(ctx):
        return (
            {path for path in _recorded_outcome_missing_output_paths(ctx) if _output_path_has_child(path)},
            "recorded_outcome",
            "recorded_outcome_missing_output_coverage",
        )
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
_SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE = "separated_spine_shape_required"
_SEPARATED_BROWSER_SPINE_PLUS_EXTRACTION_STRUCTURE = "separated_browser_spine_plus_extraction"
_OUTPUT_CONTRACT_REJECT_REASON_CODE = "output_contract_required"
_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE = "value_bearing_output_required"
_OUTPUT_CONTRACT_UNDECLARED_SENTINEL_PATH = "output"
_OUTPUT_CONTRACT_REJECT_BUDGET_REASON_CODE = OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE
_MAX_OUTPUT_CONTRACT_REJECTS = 4
_MAX_OUTPUT_CONTRACT_DEFERRALS = 3
_MISSING_CODE_ARTIFACT_METADATA_REJECT_FAMILY = "missing_code_artifact_metadata"
_METADATA_NORMALIZATION_REJECT_FAMILY = "metadata_normalization"
# Families sharing the reject-budget ladder: a candidate rotating its structural fingerprint within the
# family must not reset its streak, though an imposition landing still does (definition is already terminal).
_METADATA_FAMILY_REJECT_FAMILIES = frozenset(
    {
        _OUTPUT_CONTRACT_REJECT_REASON_CODE,
        _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
        _MISSING_CODE_ARTIFACT_METADATA_REJECT_FAMILY,
        _METADATA_NORMALIZATION_REJECT_FAMILY,
    }
)
_MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN = 3
_OUTPUT_CONTRACT_ABLATION_GATE_ID = OUTPUT_CONTRACT_ACTUATION_GATE_ID
_METADATA_PREFLIGHT_ABLATION_GATE_ID = METADATA_RUN_PREFLIGHT_REJECT_GATE_ID
_VALUE_BEARING_ROOT_GUIDANCE_PATH = "output"
_VALUE_BEARING_PREARM_FINGERPRINT_PREFIX = "value-bearing:prearm:"
_VALUE_BEARING_GUIDANCE_FINGERPRINT_PREFIX = "value-bearing:guidance:"


@dataclass(frozen=True)
class _OutputContractEvaluation:
    block_label: str
    artifact_id: str
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
    can_attempt_run: bool = False

    @property
    def has_deficiencies(self) -> bool:
        return bool(
            self.missing_metadata_paths
            or self.missing_schema_paths
            or self.missing_return_paths
            or self.shape_violations
        )


def _record_output_contract_ablation_event(
    ctx: AgentContext,
    evaluation: _OutputContractEvaluation,
    *,
    gate_id: str,
    blocked_tool: str,
    fingerprint: str,
) -> bool:
    reason_code = str(
        evaluation.payload.get("reason_code") or evaluation.reason_code or _OUTPUT_CONTRACT_REJECT_REASON_CODE
    )
    payload: AuthorTimeGateAblationPayload = {
        "block_label": evaluation.block_label,
        "canonical_output_contract_signature": evaluation.canonical_signature,
        "canonical_required_child_paths": sorted(evaluation.required_paths),
        "missing_metadata_paths": list(evaluation.missing_metadata_paths),
        "missing_schema_paths": list(evaluation.missing_schema_paths),
        "missing_return_paths": list(evaluation.missing_return_paths),
        "shape_violations": list(evaluation.shape_violations),
        "can_attempt_run": evaluation.can_attempt_run,
    }
    return record_author_time_gate_ablation_event(
        ctx,
        gate_id=gate_id,
        reason_code=reason_code,
        fingerprint=fingerprint,
        blocked_tool=blocked_tool,
        payload=payload,
    )


def _record_output_contract_actuation_ablation_event(
    ctx: AgentContext,
    workflow_yaml: str,
    *,
    reason_code: str,
    signature: str,
    block_labels: Sequence[str],
    required_paths: Iterable[str],
) -> bool:
    if not copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID):
        return False
    payload: AuthorTimeGateAblationPayload = {
        "block_labels": list(block_labels),
        "canonical_output_contract_signature": signature,
        "canonical_required_child_paths": sorted(required_paths),
    }
    return record_author_time_gate_ablation_event(
        ctx,
        gate_id=_OUTPUT_CONTRACT_ABLATION_GATE_ID,
        reason_code=reason_code,
        fingerprint=_output_contract_structural_fingerprint(workflow_yaml, signature),
        blocked_tool="update_workflow",
        payload=payload,
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
        if part.endswith("[]"):
            segments.append((part[:-2], True))
            continue
        if "[]" in part:
            name = part.replace("[]", "")
            if name:
                segments.append((name, True))
            continue
        segments.append((part, False))
    return segments


def _schema_template_for_required_paths(
    required_paths: Iterable[str],
    declaration_paths: Iterable[str] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    declaration = _metadata_contract_required_paths(declaration_paths)

    def ensure_required(container: dict[str, Any], key: str) -> None:
        required = container.setdefault("required", [])
        if isinstance(required, list) and key not in required:
            required.append(key)

    for path in sorted(_metadata_contract_required_paths(required_paths) | declaration):
        segments = _path_segments(path)
        container = schema
        for index, (key, is_array) in enumerate(segments):
            if not key:
                break
            properties = container.setdefault("properties", {})
            if not isinstance(properties, dict):
                break
            ensure_required(container, key)
            is_leaf = index == len(segments) - 1
            if is_array:
                child = properties.setdefault(
                    key,
                    {"type": "array", "items": {"type": "object", "properties": {}, "required": []}},
                )
                if isinstance(child, dict):
                    child["type"] = "array"
                    items = child.setdefault("items", {"type": "object", "properties": {}, "required": []})
                    if isinstance(items, dict):
                        items.setdefault("type", "object")
                        items.setdefault("properties", {})
                        items.setdefault("required", [])
                        container = items
                continue
            if is_leaf and path in declaration:
                properties.setdefault(key, {"type": ["string", "null"]})
                continue
            child = properties.setdefault(key, {} if is_leaf else {"type": "object", "properties": {}, "required": []})
            if isinstance(child, dict) and not is_leaf:
                child.setdefault("type", "object")
                child.setdefault("properties", {})
                child.setdefault("required", [])
                container = child
    return schema


def _schema_template_text_for_required_paths(
    required_paths: Iterable[str],
    declaration_paths: Iterable[str] = (),
) -> str:
    return json.dumps(_schema_template_for_required_paths(required_paths, declaration_paths), sort_keys=True)


def _metadata_contract_template(
    *,
    block_label: str,
    required_paths: set[str],
    source: str,
    reason_code: str,
    declaration_paths: set[str] | None = None,
) -> dict[str, Any]:
    declaration_paths = declaration_paths or set()
    artifact_id = _artifact_id_for_block_label(block_label)
    schema_text = _schema_template_text_for_required_paths(required_paths | declaration_paths, declaration_paths)
    paths = sorted(required_paths - declaration_paths)
    return {
        "block_label": block_label,
        "artifact_id": artifact_id,
        "declared_goal": "Return the requested structured output paths.",
        "claimed_outcomes": [
            {
                "id": f"claim:{artifact_id}",
                "scope": "outcome",
                "status": "observed_not_verified",
                "goal_value_paths": paths,
                "extraction_schema": schema_text,
            }
        ],
        "terminal_verifier_expectations": [
            {
                "id": f"expectation:{artifact_id}",
                "goal_value_paths": paths,
                "extraction_schema": schema_text,
                "source": source,
                "reason_code": reason_code,
            }
        ],
    }


def _return_skeleton_for_required_paths(
    required_paths: Iterable[str],
    declaration_paths: Iterable[str] = (),
) -> str:
    declaration = _metadata_contract_required_paths(declaration_paths)
    paths = sorted(_metadata_contract_required_paths(required_paths) | declaration)
    roots = sorted({_output_path_root(path) for path in paths if _output_path_root(path)})
    if not roots:
        return ""
    if roots == ["output"]:
        declaration_children = {
            child
            for path in declaration
            if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
        }
        child_names = sorted(
            {
                child
                for path in paths
                if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
            }
        )
        if child_names:
            pairs = ", ".join(
                f'"{name}": None' if name in declaration_children else f'"{name}": {name}' for name in child_names
            )
            return f'return {{"output": {{{pairs}}}}}'
        return "return output"
    if len(roots) == 1 and _return_scaffold_name_is_safe(roots[0]):
        root = roots[0]
        return f'return {{"{root}": {root}}}'
    pairs = ", ".join(f'"{root}": {root}' for root in roots if _return_scaffold_name_is_safe(root))
    return f"return {{{pairs}}}" if pairs else ""


def _value_bearing_satisfying_templates(
    *,
    block_label: str,
    required_paths: set[str],
    declaration_paths: set[str],
    source: str,
    reason_code: str,
) -> dict[str, Any]:
    declaration = _metadata_contract_required_paths(declaration_paths)
    declaration_children = sorted(
        child
        for path in declaration
        if (child := _output_path_direct_child(path, _VALUE_BEARING_ROOT_GUIDANCE_PATH))
        and _return_scaffold_name_is_safe(child)
    )
    root_declarations_are_direct = len(declaration_children) == len(declaration)
    if required_paths != {_VALUE_BEARING_ROOT_GUIDANCE_PATH} or not root_declarations_are_direct:
        return {
            "code_artifact_metadata": (
                _metadata_contract_template(
                    block_label=block_label,
                    required_paths=required_paths,
                    source=source,
                    reason_code=reason_code,
                    declaration_paths=declaration_paths,
                )
                if block_label
                else None
            ),
            "extraction_schema": _schema_template_for_required_paths(required_paths, declaration_paths),
            "return_skeleton": _return_skeleton_for_required_paths(required_paths, declaration_paths),
        }
    schema = (
        _schema_template_for_required_paths(declaration, declaration)
        if declaration
        else {
            "type": "object",
            "properties": {_VALUE_BEARING_ROOT_GUIDANCE_PATH: {}},
            "required": [_VALUE_BEARING_ROOT_GUIDANCE_PATH],
        }
    )
    metadata_template = (
        _metadata_contract_template(
            block_label=block_label,
            required_paths=set(),
            source=source,
            reason_code=reason_code,
            declaration_paths=declaration,
        )
        if block_label
        else None
    )
    if metadata_template is not None:
        schema_text = json.dumps(schema, sort_keys=True)
        for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
            row = metadata_template[field_name][0]
            row["goal_value_paths"] = [_VALUE_BEARING_ROOT_GUIDANCE_PATH]
            row["extraction_schema"] = schema_text
    declaration_defaults = ", ".join(f'"{child}": None' for child in declaration_children)
    return_skeleton = (
        'return {"' + _VALUE_BEARING_ROOT_GUIDANCE_PATH + '": {' + declaration_defaults + ", **output_value}}"
        if declaration_defaults
        else f'return {{"{_VALUE_BEARING_ROOT_GUIDANCE_PATH}": output_value}}'
    )
    return {
        "code_artifact_metadata": metadata_template,
        "extraction_schema": schema,
        "return_skeleton": return_skeleton,
    }


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


def _scout_spine_requires_separated_blocks(ctx: AgentContext) -> bool:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return False
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(scout_trajectory, list) or len(scout_trajectory) < 2:
        return False
    synthesized = synthesize_code_block(
        scout_trajectory,
        strict_selectors=True,
        reached_download_target=getattr(ctx, "reached_download_target", None),
    )
    if synthesized is None or synthesized.diagnostics.truncated:
        return False
    mutations, _, ambiguous = _browser_surface_for_code(synthesized.code)
    return not ambiguous and len(mutations) >= 2


def _output_contract_scope_key(ctx: AgentContext | None) -> str:
    if ctx is None:
        return ""
    turn_id = str(getattr(ctx, "turn_id", "") or "").strip()
    if turn_id:
        return f"turn:{turn_id}"
    workflow_permanent_id = str(getattr(ctx, "workflow_permanent_id", "") or "").strip()
    turn_state = getattr(ctx, "completion_criteria_turn_state", None)
    active_set_id = str(getattr(turn_state, "active_set_id", "") or "").strip()
    if workflow_permanent_id and active_set_id:
        return f"workflow:{workflow_permanent_id}:criteria_set:{active_set_id}"
    decision = getattr(turn_state, "decision", None)
    epoch = getattr(decision, "epoch", None)
    if workflow_permanent_id and epoch is not None:
        return f"workflow:{workflow_permanent_id}:criteria_epoch:{epoch}"
    return ""


def _stable_output_contract_key(
    scope_key: str,
    required_paths: set[str],
    request_slot_identity: Sequence[tuple[str, str]] = (),
) -> str:
    scope_key = scope_key.strip()
    if not scope_key:
        return ""
    payload: dict[str, str | list[str] | list[tuple[str, str]]] = {
        "scope": scope_key,
        "required_paths": sorted(required_paths),
    }
    if request_slot_identity:
        payload["request_slot_identity"] = sorted(request_slot_identity)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _output_contract_author_time_structural_payload(
    ctx: AgentContext,
    required_paths: set[str],
    *,
    block_label: str = "",
    deficiency_family: str = "output_contract_unsatisfied",
) -> Mapping[str, object] | None:
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    if not signature:
        return None
    payload: dict[str, object] = {
        "version": "metadata_reject_output_contract:v1",
        "canonical_output_contract_signature": signature,
        "canonical_required_child_paths": sorted(required_paths),
        "deficiency_family": deficiency_family,
    }
    if block_label.strip():
        payload["block_label"] = block_label.strip()
    return payload


def _output_contract_signature(
    *,
    ctx: AgentContext | None = None,
    scope_key: str = "",
    required_paths: set[str],
) -> str:
    """Sole producer of canonical output-contract keys: the degraded request-slot identity is
    derived from ctx here so one logical contract keeps one key at every consuming seam."""
    request_slot_identity: tuple[tuple[str, str], ...] = (
        tuple(diagnostic.identity for diagnostic in _degraded_request_slot_diagnostics(ctx)) if ctx is not None else ()
    )
    return _stable_output_contract_key(
        scope_key or _output_contract_scope_key(ctx),
        required_paths,
        request_slot_identity,
    )


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


def _default_output_contract_block_label(workflow_yaml: str) -> str:
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    return next(iter(code_blocks)) if len(code_blocks) == 1 else ""


def _output_contract_pin_key(ctx: AgentContext, workflow_yaml: str, required_paths: set[str]) -> str:
    return _output_contract_signature(ctx=ctx, required_paths=required_paths)


def _pinned_output_contract_block_label(
    ctx: AgentContext,
    workflow_yaml: str,
    required_paths: set[str],
) -> str:
    pinned_by_contract = getattr(ctx, "output_contract_pinned_block_label_by_signature", None)
    if not isinstance(pinned_by_contract, Mapping):
        return ""
    pin_key = _output_contract_pin_key(ctx, workflow_yaml, required_paths)
    if not pin_key:
        return ""
    label = str(pinned_by_contract.get(pin_key) or "").strip()
    return label if label in _workflow_yaml_code_blocks_by_label(workflow_yaml) else ""


def _pin_output_contract_block_label(
    ctx: AgentContext,
    workflow_yaml: str,
    required_paths: set[str],
    label: str,
) -> None:
    label = label.strip()
    if not label:
        return
    pinned_by_contract = getattr(ctx, "output_contract_pinned_block_label_by_signature", None)
    if not isinstance(pinned_by_contract, dict):
        pinned_by_contract = {}
    pin_key = _output_contract_pin_key(ctx, workflow_yaml, required_paths)
    if not pin_key:
        return
    pinned_by_contract.setdefault(pin_key, label)
    ctx.output_contract_pinned_block_label_by_signature = pinned_by_contract


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
            _pin_output_contract_block_label(ctx, workflow_yaml, required_paths, current_owner_labels[0])
            return current_owner_labels[0], current_owner_labels
        return "", current_owner_labels
    pinned_label = _pinned_output_contract_block_label(ctx, workflow_yaml, required_paths)
    if pinned_label:
        return pinned_label, [pinned_label]
    owner_labels = _output_metadata_owner_labels(ctx, workflow_yaml, raw_code_artifact_metadata, required_paths)
    if len(owner_labels) == 1:
        _pin_output_contract_block_label(ctx, workflow_yaml, required_paths, owner_labels[0])
        return owner_labels[0], owner_labels
    default_label = _default_output_contract_block_label(workflow_yaml)
    if default_label and not owner_labels:
        _pin_output_contract_block_label(ctx, workflow_yaml, required_paths, default_label)
        return default_label, [default_label]
    return "", owner_labels


def _runtime_output_repair_contract_from_recorded_outcome(ctx: AgentContext) -> _RuntimeOutputRepairContract | None:
    outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if not isinstance(outcome, RecordedBuildTestOutcome):
        return None
    if not (
        outcome.is_authoritative
        and outcome.phase == "persisted_block_run"
        and outcome.reason_code == "outcome_not_demonstrated"
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


def _statically_valueless_return_envelope(code: str) -> bool:
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
        return_nodes = [node for node in _iter_top_level_scope(tree.body) if isinstance(node, ast.Return)]
        if not return_nodes:
            return False
        assignments, uncertain_names = _top_level_static_assignments(tree)
        for node in return_nodes:
            if node.value is None:
                return False
            resolved = _resolve_static_expression(node.value, assignments, uncertain_names)
            if not isinstance(resolved, ast.Dict):
                return False
            if _static_leaf_value_state(resolved) != "empty":
                return False
        return True
    except (RecursionError, SyntaxError, ValueError):
        return False


def _static_value_bearing_violations(code: str, observation_paths: set[str]) -> list[str]:
    if not _statically_lacks_value_bearing_observation_paths(code, observation_paths):
        return []
    return [
        "Unable to impose synthesized code block: selected output extraction returns only statically "
        f"empty value(s) for required output path(s): {', '.join(sorted(observation_paths))}."
    ]


def _evaluate_output_contract_for_code_block(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    *,
    allow_static_return_advisory: bool = False,
    enforce_value_bearing_liveness: bool = False,
) -> _OutputContractEvaluation | None:
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
    missing_observation_return_paths = sorted(observation_paths - submitted_code_paths)
    missing_declaration_return_paths = sorted(declaration_paths - submitted_code_paths)
    shape_violations: list[str] = []
    declaration_only_contract = bool(declaration_paths) and not observation_paths
    if enforce_value_bearing_liveness:
        if contract.liveness is _OutputContractLiveness.DEGRADED_EMPTY or declaration_only_contract:
            root_envelope_unproved = (
                required_paths == {_VALUE_BEARING_ROOT_GUIDANCE_PATH}
                and _root_output_envelope_state(target_code) != "proven"
            )
            if (
                target_block is None
                or root_envelope_unproved
                or _statically_lacks_value_bearing_observation_paths(target_code, required_paths)
            ):
                shape_violations.append(_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE)
        elif target_block is not None:
            root_envelope_unproved = (
                observation_paths == {_VALUE_BEARING_ROOT_GUIDANCE_PATH}
                and _root_output_envelope_state(target_code) != "proven"
            )
            if root_envelope_unproved or _statically_lacks_value_bearing_observation_paths(
                target_code, observation_paths
            ):
                shape_violations.append(_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE)
    if not block_label:
        shape_violations.append("ambiguous_output_owner" if owner_labels else "missing_output_owner")
    elif target_block is None:
        shape_violations.append("missing_output_block")
    elif required_paths and _scout_spine_requires_separated_blocks(ctx):
        synthesized = synthesize_code_block(
            ctx.scout_trajectory,
            strict_selectors=True,
            reached_download_target=getattr(ctx, "reached_download_target", None),
        )
        if synthesized is not None and _browser_surface_contains_full_action_spine(
            str(target_block.get("code") or ""), synthesized.code
        ):
            shape_violations.append(_SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE)

    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    separated_spine_advisory_run = (
        allow_static_return_advisory
        and target_block is not None
        and _SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE in shape_violations
        and _output_contract_advisory_granted(ctx, signature)
    )
    run_gating_shape_violations = [
        violation
        for violation in shape_violations
        if not (violation == _SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE and separated_spine_advisory_run)
    ]
    actuated_static_return_advisory = (
        allow_static_return_advisory
        and bool(missing_observation_return_paths)
        and not missing_metadata_paths
        and not missing_schema_paths
        and not run_gating_shape_violations
        and target_block is not None
        and _output_contract_advisory_granted(ctx, signature)
    )
    static_return_advisory = (
        allow_static_return_advisory
        and bool(missing_observation_return_paths)
        and not missing_metadata_paths
        and not missing_schema_paths
        and not run_gating_shape_violations
        and target_block is not None
        and actuated_static_return_advisory
    )
    separated_spine_run_eligible = (
        separated_spine_advisory_run
        and not missing_metadata_paths
        and not missing_schema_paths
        and not run_gating_shape_violations
    )
    # Declaration paths are trivially satisfiable by construction, so no advisory door may waive
    # them; only observation-lane return misses are advisory-eligible.
    run_eligible = (static_return_advisory or separated_spine_run_eligible) and not missing_declaration_return_paths
    effective_missing_return_paths = (
        missing_declaration_return_paths if static_return_advisory else missing_return_paths
    )
    runtime_signature = _runtime_output_contract_signature(runtime_contract)
    artifact_id = _artifact_id_for_block_label(block_label) if block_label else ""
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
    satisfying_templates = _value_bearing_satisfying_templates(
        block_label=block_label,
        required_paths=required_paths,
        declaration_paths=declaration_paths,
        source=source,
        reason_code=reason_code,
    )
    payload: dict[str, Any] = {
        "reason_code": (
            _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE
            if value_bearing_output_required
            else _OUTPUT_CONTRACT_REJECT_REASON_CODE
        ),
        "block_label": block_label,
        "artifact_id": artifact_id,
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
        "static_return_advisory_paths": missing_observation_return_paths if static_return_advisory else [],
        "actuated_static_return_advisory": actuated_static_return_advisory,
        "shape_violations": shape_violations,
        "can_attempt_run": run_eligible,
        "reject_reason": ""
        if run_eligible
        else (
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
        "satisfying_templates": satisfying_templates,
        "missing_requested_output_facts": _missing_requested_output_facts(
            missing_paths,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
        ),
    }
    if _SEPARATED_SPINE_SHAPE_REQUIRED_REASON_CODE in shape_violations and block_label:
        attempt_key = _output_contract_spine_directive_attempt_key(
            signature=signature, block_label=block_label, workflow_yaml=workflow_yaml
        )
        if attempt_key in ctx.output_contract_spine_directive_blockers_by_attempt_key:
            blockers = ctx.output_contract_spine_directive_blockers_by_attempt_key[attempt_key]
            stage_count = ctx.output_contract_spine_directive_stage_count_by_attempt_key.get(attempt_key)
            payload["spine_structure_directive"] = {
                "required_block_structure": _SEPARATED_BROWSER_SPINE_PLUS_EXTRACTION_STRUCTURE,
                "spine_stage_count": stage_count,
                "spine_split_blockers": blockers,
            }
            if repair is not None:
                repair.required_block_structure = _SEPARATED_BROWSER_SPINE_PLUS_EXTRACTION_STRUCTURE
                repair.spine_stage_count = stage_count
                repair.spine_split_blockers = list(blockers)
    if not block_label and signature in ctx.output_contract_output_owner_directive_candidates_by_signature:
        repair = _output_owner_ambiguity_repair_context(
            required_paths=observation_paths,
            owner_labels=owner_labels,
            source=source,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
        )
        payload["output_owner_directive"] = {"output_owner_candidate_labels": repair.output_owner_candidate_labels}
    progress_data = _code_repair_progress_data(
        repair,
        missing_requested_output_facts=payload["missing_requested_output_facts"],
        metadata_repair_contract=metadata_repair_contract,
    )
    progress_data.update(payload)
    return _OutputContractEvaluation(
        block_label=block_label,
        artifact_id=artifact_id,
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
        can_attempt_run=run_eligible,
    )


def _adjudicate_output_contract_ladder_after_reject(
    ctx: AgentContext,
    evaluation: _OutputContractEvaluation,
    *,
    workflow_yaml: str,
    current_fingerprint: str,
    steer_only: bool = False,
    structure_directive_paths: set[str] | None = None,
) -> OutputContractActuation | None:
    """Run the actuation ladder at the shared deficiency seam so a signature whose formation
    remains incomplete advances toward an advisory-consumed run or a typed terminal within the
    existing caps, keeping the loop/churn defer bounded. A bail with no owner block is left to the
    owner-directive path, not granted an inert run."""
    if ctx.turn_halt is not None or ctx.output_contract_bail_actuated_this_call:
        return None
    # A statically valueless contract cannot be adjudicated by dispatching a run. The native
    # reject seam may still ask the same ladder for steer-only structure guidance, but ordinary
    # callers remain inert so they cannot mint an advisory grant that would yield the preflight.
    if _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE in evaluation.shape_violations and not steer_only:
        return None
    signature = evaluation.canonical_signature
    block_label = evaluation.block_label
    if not signature or not block_label:
        return None
    if _output_contract_advisory_state(ctx, signature) in {
        OutputContractAdvisoryState.GRANTED,
        OutputContractAdvisoryState.CONSUMED,
    }:
        return None
    block = _workflow_yaml_code_blocks_by_label(workflow_yaml).get(block_label)
    target_code = str(block.get("code") or "") if block is not None else ""
    blockers = list(evaluation.shape_violations) or [_OUTPUT_CONTRACT_REJECT_REASON_CODE]
    actuation = _actuate_output_contract_bail(
        ctx,
        blockers=blockers,
        target_code=target_code,
        required_paths=(
            structure_directive_paths
            if steer_only and structure_directive_paths is not None
            else evaluation.observation_paths
        ),
        signature=signature,
        current_fingerprint=current_fingerprint,
        advisory_run_grantable=blockers == [_OUTPUT_CONTRACT_REJECT_REASON_CODE],
        declaration_paths=evaluation.declaration_paths,
        steer_only=steer_only,
    )
    if actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL:
        _stash_output_source_unobservable_terminal(
            ctx,
            reason_code=actuation.reason_code,
            required_paths=evaluation.required_paths,
            block_label=block_label,
            signature=signature,
            blockers=blockers,
        )
    elif actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE:
        _record_armed_directive_fingerprint(ctx, signature, current_fingerprint)
    return actuation


_METADATA_CONVERGENCE_DIRECTIVE_BLOCKER = "missing_code_artifact_metadata"


def _metadata_reject_directive_payload(
    *,
    missing_fields_by_label: dict[str, list[str]],
    required_paths: set[str],
    escalate: bool,
) -> dict[str, Any]:
    directive: dict[str, Any] = {
        "rung": 2 if escalate else 1,
        "missing_fields_by_label": {label: list(fields) for label, fields in missing_fields_by_label.items()},
    }
    if escalate:
        # "evidence_refs_or_observation_refs" is a messaging label for the either-one requirement;
        # the skeleton must only offer real CodeArtifactMetadata fields the model can set.
        directive["metadata_fill_in_skeleton"] = {
            label: {
                field: "" if field == "declared_goal" else []
                for raw_field in fields
                for field in (
                    ("evidence_refs", "observation_refs")
                    if raw_field == "evidence_refs_or_observation_refs"
                    else (raw_field,)
                )
            }
            for label, fields in missing_fields_by_label.items()
        }
    if required_paths:
        directive["required_output_paths"] = sorted(required_paths)
        directive["extraction_schema_template"] = _schema_template_for_required_paths(required_paths)
    return directive


def _emit_metadata_convergence_directive(
    *,
    signature: str,
    block_label: str,
    missing_fields_by_label: dict[str, list[str]],
    required_paths: set[str],
    escalate: bool,
) -> dict[str, Any]:
    LOG.info(
        "copilot_output_contract_spine_structure_directive_emitted",
        block_label=block_label,
        canonical_output_contract_signature=signature,
        spine_split_blockers=[_METADATA_CONVERGENCE_DIRECTIVE_BLOCKER],
        spine_stage_count=None,
        rung=2 if escalate else 1,
    )
    return _metadata_reject_directive_payload(
        missing_fields_by_label=missing_fields_by_label,
        required_paths=required_paths,
        escalate=escalate,
    )


def _metadata_reject_seam_fingerprint(
    workflow_yaml: str, signature: str, missing_fields_by_label: dict[str, list[str]]
) -> str:
    return hashlib.sha256(
        (
            _output_contract_structural_fingerprint(workflow_yaml, signature)
            + json.dumps(missing_fields_by_label, sort_keys=True)
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class _ValueBearingEvaluationGuidance:
    required_output_paths: tuple[str, ...]
    shape_violations: tuple[str, ...]
    satisfying_templates: dict[str, Any]

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "required_output_paths": list(self.required_output_paths),
            "shape_violations": list(self.shape_violations),
            "satisfying_templates": self.satisfying_templates,
        }


def _value_bearing_evaluation_guidance(
    evaluation: _OutputContractEvaluation,
) -> _ValueBearingEvaluationGuidance | None:
    raw_paths = evaluation.payload.get("canonical_required_child_paths")
    raw_shape_violations = evaluation.payload.get("shape_violations")
    raw_satisfying_templates = evaluation.payload.get("satisfying_templates")
    if (
        not isinstance(raw_paths, list)
        or not raw_paths
        or any(not isinstance(path, str) or not path for path in raw_paths)
        or not isinstance(raw_shape_violations, list)
        or any(not isinstance(violation, str) or not violation for violation in raw_shape_violations)
        or _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE not in raw_shape_violations
        or not isinstance(raw_satisfying_templates, dict)
    ):
        return None
    paths = tuple(sorted(set(raw_paths)))
    if any(
        path != _canonical_requested_output_path(path) or _output_path_root(path) != _VALUE_BEARING_ROOT_GUIDANCE_PATH
        for path in paths
    ):
        return None
    return _ValueBearingEvaluationGuidance(
        required_output_paths=paths,
        shape_violations=tuple(raw_shape_violations),
        satisfying_templates=dict(raw_satisfying_templates),
    )


def _value_bearing_reject_seam_fingerprint(
    signature: str,
    guidance: _ValueBearingEvaluationGuidance,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "directive_family": _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
                "canonical_output_contract_signature": signature,
                "guidance": guidance.fingerprint_payload(),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"{_VALUE_BEARING_GUIDANCE_FINGERPRINT_PREFIX}{digest}"


def _value_bearing_reject_directive_payload(
    *,
    guidance: _ValueBearingEvaluationGuidance,
    escalate: bool,
) -> dict[str, Any]:
    return {
        "rung": 2 if escalate else 1,
        "required_output_paths": list(guidance.required_output_paths),
        "shape_violations": list(guidance.shape_violations),
        "satisfying_templates": guidance.satisfying_templates,
    }


def _emit_value_bearing_convergence_directive(
    *,
    signature: str,
    block_label: str,
    guidance: _ValueBearingEvaluationGuidance,
    guidance_fingerprint: str,
    candidate_fingerprint: str,
    escalate: bool,
) -> dict[str, Any]:
    directive = _value_bearing_reject_directive_payload(
        guidance=guidance,
        escalate=escalate,
    )
    LOG.info(
        "copilot_output_contract_spine_structure_directive_emitted",
        directive_family=_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
        block_label=block_label,
        canonical_output_contract_signature=signature,
        canonical_required_child_paths=list(guidance.required_output_paths),
        guidance_fingerprint=guidance_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        spine_split_blockers=list(guidance.shape_violations),
        spine_stage_count=None,
        rung=directive["rung"],
    )
    return directive


def _value_bearing_definition_reject_corrective(
    ctx: AgentContext,
    *,
    workflow_yaml: str,
    raw_metadata: object,
) -> dict[str, Any] | None:
    if copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID):
        return None
    evaluation = _evaluate_output_contract_for_code_block(
        ctx,
        workflow_yaml,
        raw_metadata,
        allow_static_return_advisory=True,
        enforce_value_bearing_liveness=True,
    )
    if evaluation is None or _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE not in evaluation.shape_violations:
        return None
    guidance = _value_bearing_evaluation_guidance(evaluation)
    if guidance is None or not evaluation.canonical_signature or not evaluation.block_label:
        return None
    guidance_fingerprint = _value_bearing_reject_seam_fingerprint(
        evaluation.canonical_signature,
        guidance,
    )
    escalate = _prior_output_contract_directive_unconsumed(
        ctx,
        evaluation.canonical_signature,
        guidance_fingerprint,
    )
    actuation = _adjudicate_output_contract_ladder_after_reject(
        ctx,
        evaluation,
        workflow_yaml=workflow_yaml,
        current_fingerprint=guidance_fingerprint,
        steer_only=True,
        structure_directive_paths=set(guidance.required_output_paths),
    )
    if actuation is None or actuation.kind != OutputContractActuationKind.STRUCTURE_DIRECTIVE:
        return None
    candidate_fingerprint = _output_contract_structural_fingerprint(
        workflow_yaml,
        evaluation.canonical_signature,
    )
    directive = _emit_value_bearing_convergence_directive(
        signature=evaluation.canonical_signature,
        block_label=evaluation.block_label,
        guidance=guidance,
        guidance_fingerprint=guidance_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        escalate=escalate,
    )
    return {
        "canonical_output_contract_signature": evaluation.canonical_signature,
        "canonical_required_child_paths": list(guidance.required_output_paths),
        "shape_violations": list(guidance.shape_violations),
        "satisfying_templates": guidance.satisfying_templates,
        "output_contract_actuation": actuation.kind.value,
        "value_bearing_convergence_directive": directive,
    }


def _adjudicate_metadata_reject_ladder(
    ctx: AgentContext,
    *,
    workflow_yaml: str,
    raw_metadata: object,
    missing_labels: list[str],
    required_paths: set[str],
) -> dict[str, Any] | None:
    """Adjudicate an enforcing metadata reject without coupling it to unrelated outcome state."""
    if copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID):
        return None
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    block_label = missing_labels[0] if len(missing_labels) == 1 else ""
    if not signature or not block_label:
        return None
    missing_fields_by_label = _metadata_missing_required_fields_by_label(
        raw_metadata, labels=missing_labels, missing_labels=missing_labels
    )
    reject_payload = _code_artifact_metadata_reject_payload(
        workflow_yaml=workflow_yaml,
        raw_metadata=raw_metadata,
        offending_labels=[],
        missing_labels=missing_labels,
        violation_categories=["missing_code_artifact_metadata"],
    )
    latest_outcome = ctx.latest_recorded_build_test_outcome
    candidate_outcome = (
        latest_outcome
        if isinstance(latest_outcome, RecordedBuildTestOutcome)
        and latest_outcome.phase == "author_time_reject"
        and latest_outcome.reason_code == "metadata_reject"
        else _build_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary="Submitted workflow is missing required code artifact metadata.",
            structural_payload=reject_payload,
            block_labels=missing_labels,
            missing_requested_output_facts=_missing_requested_output_facts(
                required_paths,
                reason_code=_required_child_output_paths_for_authoring(ctx)[2],
            ),
        )
    )
    seam_fingerprint = _metadata_reject_seam_fingerprint(workflow_yaml, signature, missing_fields_by_label)
    if ctx.metadata_reject_ladder_state is None and _prior_output_contract_directive_unconsumed(
        ctx,
        signature,
        seam_fingerprint,
    ):
        structural_key = candidate_outcome.structural_key
        if structural_key is not None:
            ctx.metadata_reject_ladder_state = adjudicate_metadata_reject_ladder(
                None,
                MetadataRejectLadderInput(
                    reject_family="missing_code_artifact_metadata",
                    structural_key=structural_key,
                    missing_fields_by_label=missing_fields_by_label,
                ),
            ).state
    decision = _metadata_reject_ladder_decision(
        ctx,
        candidate_outcome=candidate_outcome,
        reject_family="missing_code_artifact_metadata",
        missing_fields_by_label=missing_fields_by_label,
    )
    if decision is None:
        return None
    if decision.action == "terminal":
        return _metadata_reject_terminal_data(decision)
    assert decision.rung is not None
    _record_armed_directive_fingerprint(ctx, signature, seam_fingerprint)
    return _emit_metadata_convergence_directive(
        signature=signature,
        block_label=block_label,
        missing_fields_by_label=missing_fields_by_label,
        required_paths=required_paths,
        escalate=decision.rung == 2,
    )


def _metadata_reject_ladder_decision(
    ctx: AgentContext,
    *,
    candidate_outcome: RecordedBuildTestOutcome,
    reject_family: MetadataRejectFamily,
    missing_fields_by_label: dict[str, list[str]],
) -> MetadataRejectLadderDecision | None:
    structural_key = candidate_outcome.structural_key
    if structural_key is None or not missing_fields_by_label:
        return None
    prior_state = ctx.metadata_reject_ladder_state
    latest_outcome = ctx.latest_recorded_build_test_outcome
    if (
        prior_state is not None
        and isinstance(latest_outcome, RecordedBuildTestOutcome)
        and latest_outcome.phase == "author_time_reject"
        and (
            latest_outcome.reason_code != "metadata_reject"
            or latest_outcome.structural_key != prior_state.structural_key
        )
    ):
        _reset_metadata_reject_ladder_state(
            ctx,
            superseding_reject_family=latest_outcome.reason_code,
        )
        prior_state = None
    decision = adjudicate_metadata_reject_ladder(
        prior_state,
        MetadataRejectLadderInput(
            reject_family=reject_family,
            structural_key=structural_key,
            missing_fields_by_label=missing_fields_by_label,
        ),
    )
    ctx.metadata_reject_ladder_state = decision.state
    if decision.state.streak_count >= 2:
        LOG.info(
            "copilot_metadata_reject_same_key_detected",
            reject_family=reject_family,
            structural_key=structural_key,
            gate_id=decision.gate_id,
            streak_count=decision.state.streak_count,
            missing_fields_by_label=decision.missing_fields_by_label,
        )
    if decision.action == "terminal":
        signal = build_metadata_reject_same_key_terminal_signal(
            structural_key=structural_key,
            reject_family=reject_family,
            missing_fields_by_label=decision.missing_fields_by_label,
        )
        claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, signal)
        stash_turn_halt_from_blocker_signal(ctx, signal, source="workflow_update")
        LOG.info(
            "copilot_metadata_reject_same_key_terminal_selected",
            reject_family=reject_family,
            structural_key=structural_key,
            gate_id=decision.gate_id,
            streak_count=decision.state.streak_count,
            missing_fields_by_label=decision.missing_fields_by_label,
        )
    return decision


def _reset_metadata_reject_ladder_state(ctx: AgentContext, *, superseding_reject_family: str) -> None:
    prior_state = ctx.metadata_reject_ladder_state
    if prior_state is None:
        return
    ctx.metadata_reject_ladder_state = None
    LOG.info(
        "copilot_metadata_reject_streak_reset",
        prior_reject_family=prior_state.reject_family,
        prior_structural_key=prior_state.structural_key,
        prior_streak_count=prior_state.streak_count,
        superseding_reject_family=superseding_reject_family,
    )


def _metadata_reject_terminal_data(decision: MetadataRejectLadderDecision) -> dict[str, CodeArtifactMetadataValue]:
    return cast(
        dict[str, CodeArtifactMetadataValue],
        {
            "reason_code": METADATA_REJECT_SAME_KEY_TERMINAL_REASON_CODE,
            "reject_family": decision.state.reject_family,
            "structural_key": decision.state.structural_key,
            "gate_id": decision.gate_id,
            "missing_fields_by_label": decision.missing_fields_by_label,
            "preserves_workflow_draft": True,
        },
    )


def _synthesized_metadata_reject_directive(
    ctx: AgentContext,
    *,
    workflow_yaml: str,
    raw_metadata: object,
    label_candidates: list[str],
    required_paths: set[str],
) -> dict[str, Any] | None:
    if len(label_candidates) != 1:
        _reset_metadata_reject_ladder_state(
            ctx,
            superseding_reject_family="recorded_outcome_metadata_reject",
        )
        return None
    label = label_candidates[0]
    full_required_fields = {"declared_goal", *_CODE_ARTIFACT_REQUIRED_LIST_FIELDS, "evidence_refs_or_observation_refs"}
    missing_fields_by_label = _metadata_missing_required_fields_by_label(
        raw_metadata, labels=[label], missing_labels=[]
    )
    if set(missing_fields_by_label.get(label) or []) != full_required_fields:
        _reset_metadata_reject_ladder_state(
            ctx,
            superseding_reject_family="recorded_outcome_metadata_reject",
        )
        return None
    return _adjudicate_metadata_reject_ladder(
        ctx,
        workflow_yaml=workflow_yaml,
        raw_metadata=raw_metadata,
        missing_labels=[label],
        required_paths=required_paths,
    )


def _record_output_contract_reject(
    ctx: AgentContext,
    evaluation: _OutputContractEvaluation,
    *,
    summary: str,
    authored_structural_fingerprint: str = "",
    workflow_yaml: str = "",
    raw_metadata: object = None,
) -> dict[str, Any]:
    # A candidate so incomplete that no required child paths are derivable must still consume the
    # reject budget: an empty set would silently skip counting and degrade the turn to the generic
    # churn stop (loop_detected) instead of this ladder's typed terminal.
    value_bearing_reject = _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE in evaluation.shape_violations
    value_bearing_guidance = _value_bearing_evaluation_guidance(evaluation) if value_bearing_reject else None
    value_bearing_directive_paths = (
        set(value_bearing_guidance.required_output_paths) if value_bearing_guidance is not None else set()
    )
    counted_paths = (
        value_bearing_directive_paths or evaluation.required_paths or {_OUTPUT_CONTRACT_UNDECLARED_SENTINEL_PATH}
    )
    count = _record_output_contract_family_reject(
        ctx,
        counted_paths,
        reject_family=str(evaluation.payload.get("reason_code") or _OUTPUT_CONTRACT_REJECT_REASON_CODE),
        authored_structural_fingerprint=authored_structural_fingerprint,
    )
    _capture_rejected_code_artifact_metadata(ctx)
    payload = dict(evaluation.payload)
    payload["output_contract_reject_count"] = count
    payload["output_contract_reject_budget"] = _MAX_OUTPUT_CONTRACT_REJECTS
    value_bearing_directive_eligible = bool(
        value_bearing_reject
        and evaluation.canonical_signature
        and evaluation.block_label
        and value_bearing_guidance is not None
    )
    missing_fields_by_label = (
        _metadata_missing_required_fields_by_label(raw_metadata, labels=[evaluation.block_label], missing_labels=[])
        if evaluation.block_label
        else {}
    )
    if value_bearing_directive_eligible:
        assert value_bearing_guidance is not None
        directive_fingerprint = _value_bearing_reject_seam_fingerprint(
            evaluation.canonical_signature,
            value_bearing_guidance,
        )
    else:
        directive_fingerprint = (
            _metadata_reject_seam_fingerprint(workflow_yaml, evaluation.canonical_signature, missing_fields_by_label)
            if missing_fields_by_label
            else authored_structural_fingerprint
        )
    directive_escalate = _prior_output_contract_directive_unconsumed(
        ctx, evaluation.canonical_signature, directive_fingerprint
    )
    budget_terminal = value_bearing_directive_eligible and _adjudicate_output_contract_budget(
        ctx,
        counted_paths,
        count=count,
        block_label=evaluation.block_label,
    )
    actuation = (
        None
        if budget_terminal
        else _adjudicate_output_contract_ladder_after_reject(
            ctx,
            evaluation,
            workflow_yaml=workflow_yaml,
            current_fingerprint=directive_fingerprint,
            steer_only=value_bearing_directive_eligible,
            structure_directive_paths=value_bearing_directive_paths if value_bearing_directive_eligible else None,
        )
    )
    value_bearing_directive_emitted = False
    if actuation is not None:
        payload["output_contract_actuation"] = actuation.kind.value
        if actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE and value_bearing_directive_eligible:
            assert value_bearing_guidance is not None
            candidate_fingerprint = _output_contract_structural_fingerprint(
                workflow_yaml, evaluation.canonical_signature
            )
            value_bearing_directive = _emit_value_bearing_convergence_directive(
                signature=evaluation.canonical_signature,
                block_label=evaluation.block_label,
                guidance=value_bearing_guidance,
                guidance_fingerprint=directive_fingerprint,
                candidate_fingerprint=candidate_fingerprint,
                escalate=directive_escalate,
            )
            payload["value_bearing_convergence_directive"] = value_bearing_directive
            value_bearing_directive_emitted = True
            latest_outcome = ctx.latest_recorded_build_test_outcome
            if isinstance(latest_outcome, RecordedBuildTestOutcome) and latest_outcome.phase == "author_time_reject":
                record_build_test_outcome(ctx, None)
        if (
            actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
            and not value_bearing_directive_eligible
            and evaluation.block_label
            and missing_fields_by_label
        ):
            payload["metadata_convergence_directive"] = _emit_metadata_convergence_directive(
                signature=evaluation.canonical_signature,
                block_label=evaluation.block_label,
                missing_fields_by_label=missing_fields_by_label,
                required_paths=evaluation.required_paths,
                escalate=directive_escalate,
            )
        if actuation.kind != OutputContractActuationKind.STRUCTURE_DIRECTIVE:
            latest_outcome = ctx.latest_recorded_build_test_outcome
            if (
                isinstance(latest_outcome, RecordedBuildTestOutcome)
                and latest_outcome.phase == "author_time_reject"
                and latest_outcome.reason_code == "metadata_reject"
            ):
                # The typed adjudication now owns this same deficiency. Keep the historical reject for
                # ceiling/liveness evidence, but do not render it again as a fresh rejection next turn.
                record_build_test_outcome(ctx, None)
    if not value_bearing_directive_eligible:
        budget_terminal = _adjudicate_output_contract_budget(
            ctx,
            counted_paths,
            count=count,
            block_label=evaluation.block_label,
        )
    if budget_terminal:
        payload["reason_code"] = _OUTPUT_CONTRACT_REJECT_BUDGET_REASON_CODE
        payload["reject_reason"] = _OUTPUT_CONTRACT_REJECT_BUDGET_REASON_CODE
    if (
        actuation is None or actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    ) and not value_bearing_directive_emitted:
        structural_payload = _output_contract_author_time_structural_payload(
            ctx,
            evaluation.required_paths,
            block_label=evaluation.block_label,
        )
        _record_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary=summary,
            structural_payload=structural_payload or payload,
            block_labels=[evaluation.block_label] if evaluation.block_label else [],
            missing_requested_output_facts=payload.get("missing_requested_output_facts")
            if isinstance(payload.get("missing_requested_output_facts"), list)
            else None,
        )
        if count < 1:
            _record_code_authoring_guardrail_reject(ctx)
    return payload


def _record_output_contract_family_reject(
    ctx: AgentContext,
    required_paths: set[str],
    *,
    reject_family: str,
    authored_structural_fingerprint: str = "",
) -> int:
    if not required_paths:
        return 0
    scope_key = _output_contract_scope_key(ctx)
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    if not signature:
        LOG.info(
            "copilot_output_contract_reject_count_unscoped",
            reject_family=reject_family,
            canonical_required_child_paths=sorted(required_paths),
        )
        return 0
    count_by_signature = getattr(ctx, "output_contract_reject_count_by_signature", None)
    if not isinstance(count_by_signature, dict):
        count_by_signature = {}
    if authored_structural_fingerprint:
        prior_fingerprint = ctx.output_contract_last_reject_fingerprint_by_signature.get(signature)
        imposed_since = ctx.output_contract_imposed_since_last_reject_by_signature.get(signature, False)
        fingerprint_rotated = prior_fingerprint is not None and prior_fingerprint != authored_structural_fingerprint
        rotation_resets = fingerprint_rotated and reject_family not in _METADATA_FAMILY_REJECT_FAMILIES
        if imposed_since or rotation_resets:
            count_by_signature[signature] = 0
            ctx.output_contract_imposed_since_last_reject_by_signature[signature] = False
            LOG.info(
                "copilot_output_contract_reject_streak_reset",
                canonical_output_contract_signature=signature,
                imposed_since_last_reject=imposed_since,
                reject_family=reject_family,
            )
        ctx.output_contract_last_reject_fingerprint_by_signature[signature] = authored_structural_fingerprint
    count = int(count_by_signature.get(signature, 0) or 0) + 1
    count_by_signature[signature] = count
    ctx.output_contract_reject_count_by_signature = count_by_signature
    LOG.info(
        "copilot_output_contract_reject_counted",
        output_contract_scope_key=scope_key,
        canonical_output_contract_signature=signature,
        output_contract_reject_count=count,
        output_contract_reject_budget=_MAX_OUTPUT_CONTRACT_REJECTS,
        reject_family=reject_family,
        canonical_required_child_paths=sorted(required_paths),
    )
    return count


def _record_output_contract_deferral(ctx: AgentContext, required_paths: set[str]) -> int:
    if not required_paths:
        return 0
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    if not signature:
        return 0
    count = int(ctx.output_contract_deferral_count_by_signature.get(signature, 0) or 0) + 1
    ctx.output_contract_deferral_count_by_signature[signature] = count
    return count


def _adjudicate_output_contract_budget(
    ctx: AgentContext,
    required_paths: set[str],
    *,
    count: int,
    block_label: str,
) -> bool:
    if count < _MAX_OUTPUT_CONTRACT_REJECTS:
        return False
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    if not run_backed_repair_evidence_exists(ctx):
        deferral_count = _record_output_contract_deferral(ctx, required_paths)
        if deferral_count < _MAX_OUTPUT_CONTRACT_DEFERRALS:
            LOG.info(
                "copilot_output_contract_budget_rewrite_deferred_no_run",
                block_label=block_label,
                canonical_output_contract_signature=signature,
                output_contract_reject_count=count,
                output_contract_deferral_count=deferral_count,
            )
            return False
        LOG.info(
            "copilot_output_contract_budget_deferral_cap_reached",
            block_label=block_label,
            canonical_output_contract_signature=signature,
            output_contract_reject_count=count,
            output_contract_deferral_count=deferral_count,
        )
    _stash_output_contract_reject_budget_terminal(
        ctx, required_paths=required_paths, block_label=block_label, signature=signature
    )
    return True


def _stash_output_contract_reject_budget_terminal(
    ctx: AgentContext,
    *,
    required_paths: set[str],
    block_label: str,
    signature: str,
) -> None:
    if ctx.turn_halt is not None or blocker_signal_is_genuinely_terminal(ctx.blocker_signal):
        return
    signal = build_output_source_unobservable_blocker_signal(
        reason_code=_OUTPUT_CONTRACT_REJECT_BUDGET_REASON_CODE,
        required_paths=required_paths,
        block_label=block_label,
    )
    stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="workflow_update")
    LOG.info(
        "copilot_output_contract_reject_budget_terminal",
        block_label=block_label,
        canonical_output_contract_signature=signature,
        canonical_required_child_paths=sorted(required_paths),
    )


def _capture_rejected_code_artifact_metadata(ctx: AgentContext) -> None:
    snapshot = ctx.submitted_code_artifact_metadata_snapshot
    if snapshot is None:
        return
    captures = ctx.rejected_code_artifact_metadata_captures
    captures.append(RejectedCodeArtifactMetadataCapture(payload=snapshot))
    del captures[:-5]
    LOG.info(
        "copilot_rejected_code_artifact_metadata_captured",
        capture_count=len(captures),
        submitted_metadata_labels=sorted(snapshot) if isinstance(snapshot, dict) else None,
        submitted_metadata_type=type(snapshot).__name__,
        rejected_code_artifact_metadata_payload=snapshot,
    )


def _output_contract_reject_result(
    evaluation: _OutputContractEvaluation,
    *,
    payload: dict[str, Any] | None = None,
    tool_name: str = "update_workflow",
) -> dict[str, Any]:
    data = payload or evaluation.payload
    observation_paths = {str(path) for path in data.get("canonical_required_child_paths", []) or []} - {
        str(path) for path in data.get("declaration_only_child_paths", []) or []
    }
    if data.get("reason_code") == _OUTPUT_CONTRACT_REJECT_BUDGET_REASON_CODE:
        error = (
            "The workflow output contract repair budget is exhausted for this canonical requested-output contract. "
            "Return with the typed output-contract payload instead of trying another variant."
        )
    elif data.get("reason_code") == _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE and not observation_paths:
        error = (
            f"{tool_name} cannot proceed: every requested value-bearing output has degraded out of the "
            "completion criteria, so no code revision can satisfy the contract. Re-establish at least one "
            "requested value-bearing output before authoring again."
        )
    else:
        path_text = ", ".join(str(path) for path in data.get("canonical_required_child_paths", []) or [])
        path_suffix = f" Required requested output paths: {path_text}." if path_text else ""
        declaration_suffix = _declaration_repair_sentence(
            str(path) for path in data.get("declaration_only_child_paths", []) or []
        )
        error = (
            f"{tool_name} cannot proceed until the submitted workflow satisfies the requested output contract. "
            "Use the returned code_artifact_metadata, extraction_schema, and return skeleton templates exactly for "
            "the canonical required child paths." + path_suffix + declaration_suffix
        )
    return {
        "ok": False,
        "error": error,
        "user_facing_summary": _compiled_authoring_user_summary(),
        "data": data,
    }


def _ensure_metadata_contract_rows(
    item: dict[str, Any],
    *,
    goal_value_paths: set[str],
    schema_text: str,
) -> None:
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        rows = _artifact_mutable_rows(item.get(field_name))
        if not rows:
            item[field_name] = [{"goal_value_paths": sorted(goal_value_paths)}]
            rows = _artifact_mutable_rows(item.get(field_name))
        for row in rows:
            if goal_value_paths and not _artifact_goal_value_paths(row.get("goal_value_paths")):
                row["goal_value_paths"] = sorted(goal_value_paths)
            if schema_text and not str(row.get("extraction_schema") or "").strip():
                row["extraction_schema"] = schema_text


def _metadata_item_declares_extraction_schema(item: Mapping[str, Any] | None) -> bool:
    if item is None:
        return False
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(item.get(field_name)):
            if str(row.get("extraction_schema") or "").strip():
                return True
    return False


def _output_contract_scaffold_target_label(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    required_paths: set[str],
    *,
    declaration_paths: set[str] | None = None,
    allow_missing_static_return: bool = False,
) -> str:
    declaration_paths = declaration_paths or set()
    label, owner_labels = _target_output_contract_block_label(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        required_paths,
    )
    if not label or owner_labels != [label]:
        return ""
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    target_block = code_blocks.get(label)
    if target_block is None:
        return ""
    target_code = str(target_block.get("code") or "")
    if not allow_missing_static_return and not (
        required_paths | declaration_paths
    ) <= _code_block_produced_output_paths(target_code):
        return ""
    if not allow_missing_static_return and _scout_spine_requires_separated_blocks(ctx):
        synthesized = synthesize_code_block(
            ctx.scout_trajectory,
            strict_selectors=True,
            reached_download_target=getattr(ctx, "reached_download_target", None),
        )
        if synthesized is not None and _browser_surface_contains_full_action_spine(target_code, synthesized.code):
            return ""
    return label


def _apply_metadata_contract_scaffold(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    *,
    required_paths: set[str],
    source: str,
    reason_code: str,
    declaration_paths: set[str] | None = None,
    allow_missing_static_return: bool = False,
) -> object:
    declaration_paths = declaration_paths or set()
    union_paths = required_paths | declaration_paths
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return raw_code_artifact_metadata
    if not union_paths:
        return raw_code_artifact_metadata
    label = _output_contract_scaffold_target_label(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        required_paths,
        declaration_paths=declaration_paths,
        allow_missing_static_return=allow_missing_static_return,
    )
    if not label:
        return raw_code_artifact_metadata
    existing_item = _metadata_item_for_block_label(raw_code_artifact_metadata, label)
    prior_item = _metadata_item_for_block_label(getattr(ctx, "code_artifact_metadata", None), label)
    if _metadata_item_declares_extraction_schema(existing_item) and not _metadata_item_effective_schema_text(
        existing_item, union_paths
    ):
        return raw_code_artifact_metadata
    if _metadata_item_declares_extraction_schema(prior_item) and not _metadata_item_effective_schema_text(
        prior_item, union_paths
    ):
        return raw_code_artifact_metadata
    schema_text = (
        _metadata_item_effective_schema_text(existing_item, union_paths)
        or _metadata_item_effective_schema_text(
            prior_item,
            union_paths,
        )
        or _schema_template_text_for_required_paths(union_paths, declaration_paths)
    )
    items = [
        copy.deepcopy(item)
        for item in _code_artifact_metadata_items(raw_code_artifact_metadata)
        if _raw_metadata_item_mapping(item) is not None
    ]
    target_index: int | None = None
    for index, raw_item in enumerate(items):
        item = _raw_metadata_item_mapping(raw_item)
        if item is not None and str(item.get("block_label") or "").strip() == label:
            target_index = index
            break
    if target_index is None:
        target: dict[str, Any] = {"block_label": label}
        items.append(target)
    else:
        target = dict(items[target_index])
        items[target_index] = target
    target["block_label"] = label
    target["artifact_id"] = _artifact_id_for_block_label(label)
    _ensure_metadata_contract_rows(target, goal_value_paths=required_paths, schema_text=schema_text)
    return items


def _scaffold_metadata_from_owned_carrier_produced_output(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
) -> tuple[object, bool]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return raw_code_artifact_metadata, False
    if not ctx.spine_imposition_owned_attempt:
        return raw_code_artifact_metadata, False
    carrier_label = str(ctx.spine_imposition_carrier_label or "").strip()
    if not carrier_label:
        return raw_code_artifact_metadata, False
    if carrier_label not in _missing_code_artifact_metadata_labels(workflow_yaml, ctx, raw_code_artifact_metadata):
        return raw_code_artifact_metadata, False
    carrier_block = _workflow_yaml_code_blocks_by_label(workflow_yaml).get(carrier_label)
    if carrier_block is None:
        return raw_code_artifact_metadata, False
    produced_paths = {
        path
        for path in _code_block_produced_output_paths(str(carrier_block.get("code") or ""))
        if _output_path_has_child(path)
    } - _judgment_output_paths(ctx)
    if not produced_paths:
        return raw_code_artifact_metadata, False
    schema_text = _schema_template_text_for_required_paths(produced_paths)
    items = [
        copy.deepcopy(item)
        for item in _code_artifact_metadata_items(raw_code_artifact_metadata)
        if _raw_metadata_item_mapping(item) is not None
    ]
    target_index: int | None = None
    for index, raw_item in enumerate(items):
        item = _raw_metadata_item_mapping(raw_item)
        if item is not None and str(item.get("block_label") or "").strip() == carrier_label:
            target_index = index
            break
    if target_index is None:
        target: dict[str, Any] = {"block_label": carrier_label}
        items.append(target)
    else:
        target = dict(items[target_index])
        items[target_index] = target
    target["block_label"] = carrier_label
    target["artifact_id"] = _artifact_id_for_block_label(carrier_label)
    _ensure_metadata_contract_rows(target, goal_value_paths=produced_paths, schema_text=schema_text)
    return items, True


def _scaffold_metadata_contract_for_update(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
) -> tuple[object, bool]:
    contract = _output_contract_required_paths_source(ctx)
    if contract.union and contract.liveness is not _OutputContractLiveness.VALUE_REQUIRED:
        return raw_code_artifact_metadata, False
    if not contract.union:
        return _scaffold_metadata_from_owned_carrier_produced_output(ctx, workflow_yaml, raw_code_artifact_metadata)
    signature = _output_contract_signature(ctx=ctx, required_paths=contract.union)
    scaffolded = _apply_metadata_contract_scaffold(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        required_paths=contract.observation_paths,
        source=contract.source,
        reason_code=contract.reason_code,
        declaration_paths=contract.declaration_paths,
        allow_missing_static_return=_output_contract_advisory_granted(ctx, signature),
    )
    return scaffolded, scaffolded is not raw_code_artifact_metadata


def _apply_metadata_contract_schema_to_workflow_yaml(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
) -> str:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return workflow_yaml
    contract = _output_contract_required_paths_source(ctx)
    required_paths = contract.union
    if not required_paths or contract.liveness is not _OutputContractLiveness.VALUE_REQUIRED:
        return workflow_yaml
    label, owner_labels = _target_output_contract_block_label(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        contract.observation_paths,
    )
    if not label or owner_labels != [label]:
        return workflow_yaml
    metadata_item = _metadata_item_for_block_label(raw_code_artifact_metadata, label)
    schema_text = _metadata_item_effective_schema_text(metadata_item, required_paths)
    if not schema_text:
        return workflow_yaml
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml
    applied = False
    for block in _workflow_code_blocks(parsed):
        if str(block.get("label") or "").strip() != label:
            continue
        if str(block.get("extraction_schema") or "").strip():
            return workflow_yaml
        block["extraction_schema"] = schema_text
        applied = True
        break
    if not applied:
        return workflow_yaml
    LOG.info(
        "copilot_output_contract_schema_projected_to_workflow",
        block_label=label,
        canonical_required_child_paths=sorted(required_paths),
        source=contract.source,
        reason_code=contract.reason_code,
    )
    return yaml.safe_dump(parsed, sort_keys=False)


def _workflow_needs_contract_readback_persist(
    ctx: AgentContext,
    prior_workflow: Workflow | None,
    workflow: Workflow,
    *,
    allow_static_output_uncertainty: bool,
) -> bool:
    if not allow_static_output_uncertainty:
        return False
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return False
    if prior_workflow is None:
        return False
    definition = getattr(workflow, "workflow_definition", None)
    if not getattr(definition, "blocks", None):
        return False
    prior_definition = getattr(prior_workflow, "workflow_definition", None)
    if getattr(prior_definition, "blocks", None):
        return False
    return True


def _output_contract_reject_count(ctx: AgentContext, signature: str) -> int:
    count_by_signature = getattr(ctx, "output_contract_reject_count_by_signature", None)
    if not isinstance(count_by_signature, Mapping):
        return 0
    try:
        return int(count_by_signature.get(signature, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _runtime_output_repair_attempt_key(ctx: AgentContext, required_paths: set[str]) -> str:
    runtime_contract = _runtime_output_repair_contract_from_recorded_outcome(ctx)
    outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if runtime_contract is None or not isinstance(outcome, RecordedBuildTestOutcome):
        return ""
    payload = {
        "output_contract_signature": _output_contract_signature(ctx=ctx, required_paths=required_paths),
        "runtime_output_contract_signature": _runtime_output_contract_signature(runtime_contract),
        "recorded_structural_key": outcome.structural_key,
        "authored_structure_signature": outcome.authored_structure_signature,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _runtime_output_repair_attempt_recorded(ctx: AgentContext, attempt_key: str) -> bool:
    if not attempt_key:
        return True
    attempts = getattr(ctx, "runtime_output_repair_attempt_by_signature", None)
    return isinstance(attempts, Mapping) and bool(attempts.get(attempt_key))


def _record_runtime_output_repair_attempt(ctx: AgentContext, attempt_key: str) -> None:
    if not attempt_key:
        return
    attempts = getattr(ctx, "runtime_output_repair_attempt_by_signature", None)
    if not isinstance(attempts, dict):
        attempts = {}
    attempts[attempt_key] = True
    ctx.runtime_output_repair_attempt_by_signature = attempts


def _output_contract_spine_directive_attempt_key(*, signature: str, block_label: str, workflow_yaml: str) -> str:
    payload = {
        "signature": signature,
        "block_label": block_label,
        "workflow_yaml_hash": hashlib.sha256(workflow_yaml.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _referenced_parameter_keys_in_code(code: str, parameter_keys: list[str]) -> list[str]:
    protected = {key for key in parameter_keys if key}
    if not protected:
        return []
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return []
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return sorted(referenced & protected)


class _SpineSplitOutcome(NamedTuple):
    imposed_yaml: str | None
    blockers: list[str]
    stage_count: int | None


def _produce_split_extraction_envelope(
    ctx: AgentContext,
    extraction_suffix: str,
    *,
    required_paths: set[str],
    declaration_paths: set[str] | None,
    label: str,
    signature: str,
) -> str | None:
    """Scalar-only producer for the spine-split seam: the extraction suffix carries no browser
    actions, so the mixed download shape abstains here and the split bail stays byte-identical."""
    declarations = declaration_paths or set()
    if required_paths & download_satisfied_requested_output_paths(ctx):
        return None
    scalar_paths = required_paths - declarations
    envelope = produce_covered_static_return_envelope(
        extraction_suffix,
        plan=requested_scalar_output_extraction_plan(ctx),
        scalar_required_paths=scalar_paths,
        declaration_paths=declarations,
        download_required_paths=set(),
        expects_download=False,
    )
    if envelope is None:
        return None
    _, revalidation = _extraction_code_with_value_bearing_static_return(
        envelope.code, required_paths=scalar_paths, declaration_paths=declarations
    )
    if revalidation:
        return None
    LOG.info(
        "copilot_output_contract_keyed_static_return_produced",
        block_label=label,
        keyed_paths=list(envelope.keyed_paths),
        download_registration_paths=[],
        canonical_output_contract_signature=signature,
    )
    return envelope.code


def _attempt_separated_spine_split(
    *,
    ctx: AgentContext,
    parsed: dict[str, Any],
    label: str,
    target_code: str,
    synthesized: SynthesizedCodeBlock,
    required_paths: set[str],
    signature: str,
    declaration_paths: set[str] | None = None,
) -> _SpineSplitOutcome:
    code_block = next(
        (block for block in _workflow_code_blocks(parsed) if str(block.get("label") or "").strip() == label),
        None,
    )
    if code_block is None:
        return _SpineSplitOutcome(None, ["target_block_not_resolved_in_parsed"], None)

    reconciliation = _reconcile_synthesized_parameters(
        parsed=parsed,
        code_block=code_block,
        submitted_code=target_code,
        synthesized_parameters=synthesized.parameters,
        scout_trajectory=ctx.scout_trajectory,
    )
    if reconciliation.violations:
        return _SpineSplitOutcome(None, ["parameter_reconciliation_failed"], None)

    reconciled_submitted = _apply_parameter_reconciliation_to_code(textwrap.dedent(target_code), reconciliation)
    reconciled_synthesized = _apply_parameter_reconciliation_to_code(textwrap.dedent(synthesized.code), reconciliation)
    extraction_suffix = _submitted_suffix_after_synthesized_code(reconciled_submitted, reconciled_synthesized)
    if not extraction_suffix:
        return _SpineSplitOutcome(None, ["extraction_boundary_ambiguous"], None)
    suffix_mutations, _, suffix_ambiguous = _browser_surface_for_code(extraction_suffix)
    if suffix_mutations or suffix_ambiguous:
        return _SpineSplitOutcome(None, ["extraction_suffix_contains_browser_actions"], None)

    keyed_extraction, static_violations = _extraction_code_with_value_bearing_static_return(
        extraction_suffix, required_paths=required_paths, declaration_paths=declaration_paths
    )
    if static_violations:
        produced_extraction = _produce_split_extraction_envelope(
            ctx,
            extraction_suffix,
            required_paths=required_paths,
            declaration_paths=declaration_paths,
            label=label,
            signature=signature,
        )
        if produced_extraction is None:
            return _SpineSplitOutcome(None, ["static_return_envelope_unavailable"], None)
        keyed_extraction = produced_extraction
    if _browser_surface_contains_full_action_spine(keyed_extraction, reconciled_synthesized):
        return _SpineSplitOutcome(None, ["extraction_retains_full_spine"], None)

    stage_codes = _synthesized_durable_stage_codes(synthesized, source_code=reconciled_synthesized)
    if len(stage_codes) < 2:
        return _SpineSplitOutcome(None, ["insufficient_durable_stages"], len(stage_codes))

    split_violations = _split_selected_output_owner_into_browser_stages(
        parsed=parsed,
        code_block=code_block,
        synthesized=synthesized,
        synthesized_code=reconciled_synthesized,
        extraction_code=keyed_extraction,
        parameter_keys=reconciliation.parameter_keys,
    )
    if split_violations:
        return _SpineSplitOutcome(None, split_violations, len(stage_codes))

    referenced_keys = _referenced_parameter_keys_in_code(keyed_extraction, reconciliation.parameter_keys)
    if referenced_keys:
        code_block["parameter_keys"] = referenced_keys

    return _SpineSplitOutcome(yaml.safe_dump(parsed, sort_keys=False), [], len(stage_codes))


def _arm_output_contract_spine_directive(
    ctx: AgentContext,
    *,
    attempt_key: str,
    blockers: list[str],
    stage_count: int | None,
    block_label: str,
    signature: str,
) -> None:
    already_armed = attempt_key in ctx.output_contract_spine_directive_blockers_by_attempt_key
    ctx.output_contract_spine_directive_blockers_by_attempt_key[attempt_key] = list(blockers)
    if stage_count is not None:
        ctx.output_contract_spine_directive_stage_count_by_attempt_key[attempt_key] = stage_count
    if already_armed:
        return
    LOG.info(
        "copilot_output_contract_spine_structure_directive_emitted",
        block_label=block_label,
        canonical_output_contract_signature=signature,
        spine_split_blockers=blockers,
        spine_stage_count=stage_count,
    )


def _arm_output_contract_output_owner_directive(
    ctx: AgentContext,
    *,
    signature: str,
    owner_labels: list[str],
) -> None:
    already_armed = signature in ctx.output_contract_output_owner_directive_candidates_by_signature
    ctx.output_contract_output_owner_directive_candidates_by_signature[signature] = list(owner_labels)
    if already_armed:
        return
    LOG.info(
        "copilot_output_contract_output_owner_directive_emitted",
        canonical_output_contract_signature=signature,
        output_owner_candidate_labels=owner_labels,
    )


def _arm_output_contract_value_bearing_directive(
    ctx: AgentContext,
    *,
    signature: str,
    workflow_yaml: str,
    liveness: _OutputContractLiveness,
) -> None:
    fingerprint = (
        f"{_VALUE_BEARING_PREARM_FINGERPRINT_PREFIX}{_output_contract_structural_fingerprint(workflow_yaml, signature)}"
    )
    armed = ctx.output_contract_armed_directive_fingerprint_by_signature.get(signature)
    already_armed = armed == fingerprint
    if not (armed or "").startswith(_VALUE_BEARING_GUIDANCE_FINGERPRINT_PREFIX):
        _record_armed_directive_fingerprint(ctx, signature, fingerprint)
    if already_armed:
        return
    LOG.info(
        "copilot_output_contract_value_bearing_directive_emitted",
        canonical_output_contract_signature=signature,
        reason_code=_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
        contract_liveness=liveness.value,
    )


def _output_owner_ambiguity_repair_context(
    *,
    required_paths: set[str],
    owner_labels: list[str],
    source: str,
    reason_code: str,
    declaration_paths: set[str] | None = None,
) -> CodeAuthoringRepairContext:
    goal_paths = _normalized_repair_paths(required_paths)
    union_paths = sorted(dict.fromkeys([*goal_paths, *_normalized_repair_paths(declaration_paths or set())]))
    candidates = sorted(dict.fromkeys(str(label).strip() for label in owner_labels if str(label).strip()))
    path_text = ", ".join(union_paths)
    return CodeAuthoringRepairContext(
        block_label="",
        reason_code=OUTPUT_OWNER_AMBIGUITY_REASON_CODE,
        runtime_failure_class=reason_code,
        required_goal_value_paths=goal_paths,
        required_extraction_schema_paths=union_paths,
        required_code_return_paths=union_paths,
        metadata_contract_source=source,
        metadata_contract_reason_code=reason_code,
        output_owner_candidate_labels=candidates,
        repair_instruction=(
            "Designate exactly one code block as the sole output owner for required paths "
            f"{path_text}; declare code_artifact_metadata on that single block and remove competing output owners."
        ),
    )


def _output_contract_structural_fingerprint(workflow_yaml: str, signature: str) -> str:
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    topology: list[dict[str, Any]] = []
    for label, block in code_blocks.items():
        code = str(block.get("code") or "")
        mutations, _, ambiguous = _browser_surface_for_code(code)
        topology.append(
            {
                "label": label,
                "produced_output_paths": sorted(_code_block_produced_output_paths(code)),
                "mutates_browser": bool(mutations or ambiguous),
            }
        )
    payload = {"signature": signature, "topology": topology}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _code_reads_page_value(code: str) -> bool:
    tree = _wrapped_code_ast(code)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _VALUE_BEARING_READ_METHODS
        ):
            return True
    return False


def _output_contract_click_only_spine(target_code: str, declaration_paths: set[str] | None = None) -> bool:
    mutations, _, ambiguous = _browser_surface_for_code(target_code)
    if not (mutations or ambiguous):
        return False
    if _code_block_produced_output_paths(target_code) - _declaration_envelope_paths(declaration_paths or set()):
        return False
    return not _code_reads_page_value(target_code)


def _output_paths_share_lineage(covered: str, required: str) -> bool:
    return covered == required or covered.startswith(f"{required}.") or required.startswith(f"{covered}.")


def _observed_required_output_values(ctx: AgentContext, required_paths: set[str]) -> bool:
    covered = ctx.scouted_output_covered_paths
    if not covered:
        return False
    if covered & required_paths:
        return True
    return any(_output_paths_share_lineage(str(path), required) for path in covered for required in required_paths)


def _prior_output_contract_actuation(ctx: AgentContext, signature: str) -> bool:
    return bool(
        ctx.output_contract_imposed_since_last_reject_by_signature.get(signature)
        or ctx.output_contract_armed_directive_fingerprint_by_signature.get(signature)
        or signature in ctx.output_contract_output_owner_directive_candidates_by_signature
        or ctx.output_contract_actuation_count_by_signature.get(signature)
        or _output_contract_advisory_state(ctx, signature) != OutputContractAdvisoryState.UNUSED
    )


def _prior_output_contract_directive_unconsumed(ctx: AgentContext, signature: str, current_fingerprint: str) -> bool:
    armed = ctx.output_contract_armed_directive_fingerprint_by_signature.get(signature)
    if (armed or "").startswith(_VALUE_BEARING_PREARM_FINGERPRINT_PREFIX):
        return False
    return bool(armed) and armed == current_fingerprint


def _record_armed_directive_fingerprint(ctx: AgentContext, signature: str, fingerprint: str) -> None:
    if signature:
        ctx.output_contract_armed_directive_fingerprint_by_signature[signature] = fingerprint


def _mark_output_contract_imposed(ctx: AgentContext, signature: str) -> None:
    if signature:
        ctx.output_contract_imposed_since_last_reject_by_signature[signature] = True
        _clear_declick_attempt(ctx, signature)


def _prior_declick_attempt_recorded(ctx: AgentContext, signature: str) -> bool:
    return bool(signature) and bool(ctx.output_contract_declick_attempted_by_signature.get(signature))


def _record_declick_attempt(ctx: AgentContext, signature: str) -> None:
    if signature and not ctx.output_contract_declick_attempted_by_signature.get(signature):
        ctx.output_contract_declick_attempted_by_signature[signature] = True
        LOG.info("copilot_output_contract_declick_attempt_recorded", canonical_output_contract_signature=signature)


def _clear_declick_attempt(ctx: AgentContext, signature: str) -> None:
    if signature:
        ctx.output_contract_declick_attempted_by_signature.pop(signature, None)


def _output_contract_actuation_progress_exhausted(ctx: AgentContext, signature: str) -> bool:
    if not signature:
        return False
    count = int(ctx.output_contract_actuation_count_by_signature.get(signature, 0) or 0)
    return count >= _MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN


def _record_output_contract_actuation_progress(ctx: AgentContext, signature: str) -> None:
    if not signature:
        return
    count = int(ctx.output_contract_actuation_count_by_signature.get(signature, 0) or 0) + 1
    ctx.output_contract_actuation_count_by_signature[signature] = count
    LOG.info(
        "copilot_output_contract_actuation_progress",
        canonical_output_contract_signature=signature,
        actuations_without_run=count,
    )


def _output_contract_advisory_state(ctx: AgentContext, signature: str) -> OutputContractAdvisoryState:
    state = ctx.output_contract_actuation_by_signature.get(signature)
    return state if isinstance(state, OutputContractAdvisoryState) else OutputContractAdvisoryState.UNUSED


def _output_contract_advisory_granted(ctx: AgentContext, signature: str) -> bool:
    return _output_contract_advisory_state(ctx, signature) == OutputContractAdvisoryState.GRANTED


def _revoke_output_contract_advisory_run(ctx: AgentContext, signature: str) -> None:
    if _output_contract_advisory_granted(ctx, signature):
        ctx.output_contract_actuation_by_signature.pop(signature, None)
        LOG.info(
            "copilot_output_contract_advisory_run_revoked",
            canonical_output_contract_signature=signature,
            reason_code=_OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
        )


def _grant_output_contract_advisory_run(ctx: AgentContext, signature: str) -> None:
    if not signature:
        return
    if _output_contract_advisory_state(ctx, signature) == OutputContractAdvisoryState.CONSUMED:
        return
    if _output_contract_advisory_state(ctx, signature) == OutputContractAdvisoryState.GRANTED:
        return
    ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.GRANTED
    claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)
    LOG.info(
        "copilot_output_contract_advisory_run_granted",
        canonical_output_contract_signature=signature,
    )


def consume_output_contract_advisory_grant_for_run(
    ctx: AgentContext, *, workflow_run_id: str | None = None
) -> list[str]:
    consumed: list[str] = []
    for signature, state in list(ctx.output_contract_actuation_by_signature.items()):
        if state == OutputContractAdvisoryState.GRANTED:
            ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.CONSUMED
            consumed.append(signature)
    for signature in consumed:
        LOG.info(
            "copilot_output_contract_advisory_run_consumed",
            canonical_output_contract_signature=signature,
            workflow_run_id=workflow_run_id,
        )
    if ctx.output_contract_actuation_count_by_signature:
        ctx.output_contract_actuation_count_by_signature.clear()
    if ctx.output_contract_declick_attempted_by_signature:
        ctx.output_contract_declick_attempted_by_signature.clear()
    return consumed


def consume_output_contract_advisory_grant_for_run_result(
    ctx: AgentContext, run_result: Mapping[str, object]
) -> list[str]:
    workflow_run_id = _workflow_run_id_from_run_result(run_result)
    if workflow_run_id is None:
        data = run_result.get("data")
        if isinstance(data, Mapping) and run_result.get("ok") is not False:
            LOG.warning(
                "copilot_output_contract_advisory_run_result_missing_workflow_run_id",
                data_keys=sorted(str(key) for key in data),
            )
        return []
    consumed = consume_output_contract_advisory_grant_for_run(ctx, workflow_run_id=workflow_run_id)
    for signature in consumed:
        LOG.info(
            "copilot_output_contract_advisory_run_dispatched_at_seam",
            canonical_output_contract_signature=signature,
            workflow_run_id=workflow_run_id,
        )
    return consumed


def _workflow_run_id_from_run_result(run_result: Mapping[str, object]) -> str | None:
    data = run_result.get("data")
    if isinstance(data, Mapping):
        workflow_run_id = data.get("workflow_run_id")
        if isinstance(workflow_run_id, str) and workflow_run_id.strip():
            return workflow_run_id
    return None


def _stash_output_source_unobservable_terminal(
    ctx: AgentContext,
    *,
    reason_code: str,
    required_paths: set[str],
    block_label: str,
    signature: str,
    blockers: list[str],
) -> None:
    if ctx.turn_halt is not None:
        return
    payload: AuthorTimeGateAblationPayload = {
        "block_label": block_label,
        "canonical_output_contract_signature": signature,
        "canonical_required_child_paths": sorted(required_paths),
        "spine_split_blockers": list(blockers),
    }
    if (
        copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID)
        and ctx.output_contract_bail_actuated_this_call
    ):
        return
    if record_author_time_gate_ablation_event(
        ctx,
        gate_id=_OUTPUT_CONTRACT_ABLATION_GATE_ID,
        reason_code=reason_code,
        fingerprint=signature,
        blocked_tool="update_workflow",
        payload=payload,
    ):
        return
    signal = build_output_source_unobservable_blocker_signal(
        reason_code=reason_code,
        required_paths=required_paths,
        block_label=block_label,
    )
    _record_author_time_reject_outcome(
        ctx,
        reason_code=cast(BuildTestOutcomeReasonCode, reason_code),
        summary=signal.user_facing_reason,
        structural_payload=_output_contract_author_time_structural_payload(ctx, required_paths, block_label=block_label)
        or {},
        block_labels=[block_label] if block_label else [],
    )
    stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="workflow_update")
    LOG.info(
        "copilot_output_contract_blocked_terminal",
        reason=reason_code,
        block_label=block_label,
        canonical_output_contract_signature=signature,
        spine_split_blockers=list(blockers),
    )


def _run_authority_permits_dispatch(ctx: AgentContext) -> bool:
    turn_intent = getattr(ctx, "turn_intent", None)
    authority = getattr(turn_intent, "authority", None)
    if authority is None:
        return True
    return bool(getattr(authority, "may_run_blocks", False)) and not bool(
        getattr(authority, "requires_user_input", False)
    )


def _page_extraction_imposed(ctx: AgentContext, signature: str) -> bool:
    return bool(signature) and bool(ctx.output_contract_page_extraction_imposed_by_signature.get(signature))


def _arm_pending_run_evidence(ctx: AgentContext, signature: str, required_paths: set[str]) -> None:
    if not signature or not required_paths:
        return
    ctx.output_contract_pending_run_evidence[signature] = sorted(required_paths)
    ctx.output_contract_run_output_observed_by_signature.pop(signature, None)
    ctx.output_contract_run_bound_required_path_by_signature.pop(signature, None)


def _registered_output_paths(result: object) -> set[str]:
    paths: set[str] = set()
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return paths

    def absorb(prefix: str, value: object) -> None:
        if isinstance(value, dict) and value:
            for key, child in value.items():
                absorb(f"{prefix}.{key}" if prefix else str(key), child)
        elif prefix and _runtime_output_value_is_meaningful(value):
            paths.add(prefix)

    for block in data.get("blocks") or []:
        if isinstance(block, dict):
            absorb("", block.get("extracted_data"))
    for item in data.get("registered_output_parameter_values") or []:
        if isinstance(item, dict):
            absorb("", item.get("value"))
    return paths


def _strip_output_namespace_root(path: str) -> str:
    # Required paths carry the ``output.`` namespace while run output keys the bare leaf, so
    # drop the shared root before lineage matching to compare the two in the same namespace.
    return path[len("output.") :] if path.startswith("output.") else path


def record_output_contract_run_output_evidence(ctx: AgentContext, result: object) -> None:
    """Run-result seam: record whether the dispatched run's output was observed and whether it covered any
    required path, keyed per armed signature. The exhaustion terminal reads this executed-run evidence rather
    than draft shape, and an observed-but-unbound run routes to the page-source rung, never to a terminal."""
    pending = ctx.output_contract_pending_run_evidence
    if not pending:
        return
    observed_paths = {_strip_output_namespace_root(path) for path in _registered_output_paths(result)}
    for signature, paths in list(pending.items()):
        required_paths = {_strip_output_namespace_root(str(path)) for path in paths}
        bound = bool(required_paths) and all(
            any(_output_paths_share_lineage(observed, required) for observed in observed_paths)
            for required in required_paths
        )
        ctx.output_contract_run_output_observed_by_signature[signature] = True
        ctx.output_contract_run_bound_required_path_by_signature[signature] = bound
        LOG.info(
            "copilot_output_contract_run_output_evidence_recorded",
            canonical_output_contract_signature=signature,
            bound_required_path=bound,
        )
    ctx.output_contract_pending_run_evidence = {}


def _reopen_dispatch_lacked_bound_extraction(ctx: AgentContext, signature: str) -> bool:
    """One-shot per signature: a consumed advisory run whose observed output bound no required path is not
    exhaustion evidence — a code static-return provably cannot key values a click-only trajectory never
    captured. Reset the signature to UNUSED so the ladder re-enters once before any terminal."""
    if not signature:
        return False
    if _output_contract_advisory_state(ctx, signature) != OutputContractAdvisoryState.CONSUMED:
        return False
    if not ctx.output_contract_run_output_observed_by_signature.get(signature):
        return False
    if ctx.output_contract_run_bound_required_path_by_signature.get(signature):
        return False
    if ctx.output_contract_page_extraction_imposed_by_signature.get(signature):
        return False
    if ctx.output_contract_dispatch_reopened_by_signature.get(signature):
        return False
    ctx.output_contract_dispatch_reopened_by_signature[signature] = True
    ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.UNUSED
    ctx.output_contract_actuation_count_by_signature[signature] = max(
        1, int(ctx.output_contract_actuation_count_by_signature.get(signature, 0) or 0)
    )
    LOG.info(
        "copilot_output_contract_dispatch_lacked_bound_extraction",
        canonical_output_contract_signature=signature,
    )
    return True


def _actuate_output_contract_bail(
    ctx: AgentContext,
    *,
    blockers: list[str],
    target_code: str,
    required_paths: set[str],
    signature: str,
    current_fingerprint: str,
    advisory_run_grantable: bool = False,
    declaration_paths: set[str] | None = None,
    steer_only: bool = False,
) -> OutputContractActuation:
    click_only_spine = _output_contract_click_only_spine(target_code, declaration_paths)
    observed_required_values = _observed_required_output_values(ctx, required_paths)
    loaded_result_evidence = ctx.latest_evaluate_result_composition_steer
    result_source_producible = loaded_result_source_producible(loaded_result_evidence, target_code=target_code)
    claimed_signature = ctx.latest_evaluate_result_composition_signature
    if result_source_producible and claimed_signature not in (None, signature):
        result_source_producible = False
    elif result_source_producible:
        ctx.latest_evaluate_result_composition_signature = signature
    if not click_only_spine or observed_required_values or result_source_producible:
        _clear_declick_attempt(ctx, signature)
    evidence = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=click_only_spine,
        observed_required_values=observed_required_values,
        prior_actuation=_prior_output_contract_actuation(ctx, signature),
        prior_directive_unconsumed=_prior_output_contract_directive_unconsumed(ctx, signature, current_fingerprint),
        advisory_state=_output_contract_advisory_state(ctx, signature),
        actuation_progress_exhausted=_output_contract_actuation_progress_exhausted(ctx, signature),
        declick_attempt_failed=_prior_declick_attempt_recorded(ctx, signature),
        advisory_run_grantable=advisory_run_grantable,
        consumed_run_output_observed=bool(ctx.output_contract_run_output_observed_by_signature.get(signature)),
        consumed_run_bound_required_path=bool(ctx.output_contract_run_bound_required_path_by_signature.get(signature)),
        consumed_run_carried_page_extraction=_page_extraction_imposed(ctx, signature),
        loaded_result_source_producible=result_source_producible,
    )
    actuation = resolve_output_contract_actuation(
        family=classify_output_contract_bail_family(blockers),
        evidence=evidence,
    )
    if actuation.kind == OutputContractActuationKind.ADVISORY_RUN and not _run_authority_permits_dispatch(ctx):
        actuation = OutputContractActuation(OutputContractActuationKind.STRUCTURE_DIRECTIVE, actuation.family)
    if steer_only and actuation.kind in {
        OutputContractActuationKind.ADVISORY_RUN,
        OutputContractActuationKind.BLOCKED_TERMINAL,
    }:
        actuation = OutputContractActuation(OutputContractActuationKind.STRUCTURE_DIRECTIVE, actuation.family)
    payload: AuthorTimeGateAblationPayload = {
        "actuation_kind": actuation.kind.value,
        "family": actuation.family.value,
        "blockers": list(blockers),
        "canonical_required_child_paths": sorted(required_paths),
        "advisory_state": evidence.advisory_state.value,
        "advisory_run_grantable": evidence.advisory_run_grantable,
        "loaded_result_source_producible": evidence.loaded_result_source_producible,
        "loaded_result_structure_signature": loaded_result_evidence.structure_signature
        if result_source_producible and loaded_result_evidence is not None
        else "",
        "loaded_result_source_tool": loaded_result_evidence.source_tool
        if result_source_producible and loaded_result_evidence is not None
        else "",
        "loaded_result_source_url": loaded_result_evidence.source_url
        if result_source_producible and loaded_result_evidence is not None
        else "",
    }
    ctx.output_contract_bail_actuated_this_call = True
    if record_author_time_gate_ablation_event(
        ctx,
        gate_id=_OUTPUT_CONTRACT_ABLATION_GATE_ID,
        reason_code=actuation.reason_code or actuation.kind.value,
        fingerprint=current_fingerprint,
        blocked_tool="update_workflow",
        payload=payload,
    ):
        return actuation
    if actuation.kind == OutputContractActuationKind.ADVISORY_RUN:
        _grant_output_contract_advisory_run(ctx, signature)
        _arm_pending_run_evidence(ctx, signature, required_paths)
    elif (
        actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
        and not evidence.prior_directive_unconsumed
        and not steer_only
    ):
        _record_output_contract_actuation_progress(ctx, signature)
    if (
        not steer_only
        and actuation.kind
        not in {OutputContractActuationKind.BLOCKED_TERMINAL, OutputContractActuationKind.ADVISORY_RUN}
        and click_only_spine
        and not observed_required_values
    ):
        _record_declick_attempt(ctx, signature)
    return actuation


def _merge_declaration_children_into_literal_returns(code: str, declaration_paths: set[str]) -> str:
    if {_output_path_root(path) for path in declaration_paths} != {"output"}:
        return ""
    declaration_children = sorted(
        {
            child
            for path in declaration_paths
            if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
        }
    )
    if not declaration_children:
        return ""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    lines = code.splitlines()
    insertions: list[tuple[int, int, str]] = []
    for node in _iter_top_level_scope(tree.body):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        output_value: ast.expr | None = None
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "output":
                output_value = value
                break
        if output_value is None:
            pairs = ", ".join(f'"{name}": None' for name in declaration_children)
            insert_text = f'"output": {{{pairs}}}'
            target = node.value
        elif isinstance(output_value, ast.Dict):
            existing_keys = {
                key.value for key in output_value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            missing_children = [name for name in declaration_children if name not in existing_keys]
            if not missing_children:
                continue
            insert_text = ", ".join(f'"{name}": None' for name in missing_children)
            target = output_value
        else:
            return ""
        # Front insertion keeps any later literal or unpacked entries authoritative over the
        # stamped None defaults.
        suffix = ", " if (target.keys or target.values) else ""
        insertions.append((target.lineno - 1, target.col_offset + 1, insert_text + suffix))
    if not insertions:
        return ""
    for line_index, col, text in sorted(insertions, reverse=True):
        if line_index < 0 or line_index >= len(lines) or col > len(lines[line_index]):
            return ""
        lines[line_index] = lines[line_index][:col] + text + lines[line_index][col:]
    return "\n".join(lines)


def _code_with_declared_contract_defaults(code: str, declaration_paths: set[str]) -> str:
    stripped_code = textwrap.dedent(code).strip()
    if not stripped_code or not declaration_paths:
        return ""
    if declaration_paths <= _code_block_produced_output_paths(stripped_code):
        return ""
    merged = _merge_declaration_children_into_literal_returns(stripped_code, declaration_paths)
    if merged and declaration_paths <= _code_block_produced_output_paths(merged):
        return merged
    try:
        tree = ast.parse(stripped_code)
    except SyntaxError:
        return ""
    if any(isinstance(node, ast.Return) for node in _iter_top_level_scope(tree.body)):
        return ""
    appended, violations = _extraction_code_with_required_static_return(
        stripped_code,
        required_paths=set(),
        declaration_paths=declaration_paths,
    )
    if violations or appended == stripped_code:
        return ""
    if declaration_paths <= _code_block_produced_output_paths(appended):
        return appended
    return ""


def _stamp_declaration_contract_defaults(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    contract: _OutputContractRequiredPaths,
    signature: str,
) -> tuple[str, bool]:
    if not contract.declaration_paths:
        return workflow_yaml, False
    label, owner_labels = _target_output_contract_block_label(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        contract.observation_paths,
    )
    if not label or owner_labels != [label]:
        return workflow_yaml, False
    target_block = _workflow_yaml_code_blocks_by_label(workflow_yaml).get(label)
    if target_block is None:
        return workflow_yaml, False
    target_code = str(target_block.get("code") or "")
    produced_observation_paths = contract.observation_paths & _code_block_produced_output_paths(target_code)
    if not produced_observation_paths:
        return workflow_yaml, False
    if _statically_lacks_value_bearing_observation_paths(target_code, produced_observation_paths):
        return workflow_yaml, False
    stamped_code = _code_with_declared_contract_defaults(target_code, contract.declaration_paths)
    if not stamped_code:
        return workflow_yaml, False
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml, False
    applied = False
    for block in _workflow_code_blocks(parsed):
        if str(block.get("label") or "").strip() == label:
            block["code"] = stamped_code.rstrip() + "\n"
            applied = True
            break
    if not applied:
        return workflow_yaml, False
    LOG.info(
        "copilot_output_contract_declaration_stamped",
        block_label=label,
        declaration_only_child_paths=sorted(contract.declaration_paths),
        canonical_output_contract_signature=signature,
    )
    return yaml.safe_dump(parsed, sort_keys=False), True


def _impose_covered_static_return_envelope(
    ctx: AgentContext,
    *,
    parsed: dict[str, Any],
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    label: str,
    target_code: str,
    observation_paths: set[str],
    declaration_paths: set[str],
    source: str,
    reason_code: str,
    signature: str,
    runtime_attempt_key: str,
) -> tuple[str, object, bool] | None:
    """Deterministic evidence-derived envelope on the whole-block scaffold route: author the scalar
    live-reads (mixed shape merges the resolved download descriptor into one return), or admit a
    download-only block, so ``static_return_envelope_unavailable`` never fires for scout-covered goals.
    Returns None to leave today's bail ladder byte-identical."""
    download_registered = download_satisfied_requested_output_paths(ctx)
    scalar_paths = observation_paths - download_registered
    download_paths = observation_paths & download_registered
    download = ctx.reached_download_target
    expects_download = download is not None and bool(download.selector) and _code_uses_expect_download(target_code)
    block_metadata = _metadata_item_for_block_label(raw_code_artifact_metadata, label) or {}

    if not scalar_paths:
        if not (download_paths and expects_download):
            return None
        if _download_return_shape_error(label, block_metadata, target_code) is not None:
            return None
        scaffolded_metadata = _apply_metadata_contract_scaffold(
            ctx,
            workflow_yaml,
            raw_code_artifact_metadata,
            required_paths=observation_paths,
            source=source,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
            allow_missing_static_return=True,
        )
        _mark_output_contract_imposed(ctx, signature)
        _record_runtime_output_repair_attempt(ctx, runtime_attempt_key)
        LOG.info(
            "copilot_output_contract_download_registration_admitted",
            block_label=label,
            download_registration_paths=sorted(download_paths),
            canonical_output_contract_signature=signature,
        )
        return workflow_yaml, scaffolded_metadata, scaffolded_metadata is not raw_code_artifact_metadata

    envelope = produce_covered_static_return_envelope(
        target_code,
        plan=requested_scalar_output_extraction_plan(ctx),
        scalar_required_paths=scalar_paths,
        declaration_paths=declaration_paths,
        download_required_paths=download_paths,
        expects_download=expects_download,
    )
    if envelope is None:
        return None
    _, revalidation = _extraction_code_with_value_bearing_static_return(
        envelope.code, required_paths=scalar_paths, declaration_paths=declaration_paths
    )
    if revalidation:
        return None
    if download_paths and _download_return_shape_error(label, block_metadata, envelope.code) is not None:
        return None

    for block in _workflow_code_blocks(parsed):
        if str(block.get("label") or "").strip() == label:
            block["code"] = envelope.code.rstrip() + "\n"
            break
    imposed_yaml = yaml.safe_dump(parsed, sort_keys=False)
    scaffolded_metadata = _apply_metadata_contract_scaffold(
        ctx,
        imposed_yaml,
        raw_code_artifact_metadata,
        required_paths=observation_paths,
        source=source,
        reason_code=reason_code,
        declaration_paths=declaration_paths,
        allow_missing_static_return=bool(download_paths),
    )
    _mark_output_contract_imposed(ctx, signature)
    _record_runtime_output_repair_attempt(ctx, runtime_attempt_key)
    LOG.info(
        "copilot_output_contract_keyed_static_return_produced",
        block_label=label,
        keyed_paths=list(envelope.keyed_paths),
        download_registration_paths=sorted(download_paths),
        canonical_output_contract_signature=signature,
    )
    LOG.info(
        "copilot_output_contract_envelope_imposed_after_steering",
        block_label=label,
        canonical_output_contract_signature=signature,
        steering_reject_count=_output_contract_reject_count(ctx, signature),
        return_envelope_applied=True,
        metadata_scaffold_applied=scaffolded_metadata is not raw_code_artifact_metadata,
    )
    return imposed_yaml, scaffolded_metadata, True


def _impose_output_contract_envelope_after_steering(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
) -> tuple[str, object, bool]:
    ctx.output_contract_bail_actuated_this_call = False
    contract = _output_contract_required_paths_source(ctx)
    required_paths = set(contract.union)
    directive_paths = _value_bearing_directive_paths(contract)
    observation_paths = contract.observation_paths
    declaration_paths = contract.declaration_paths
    source = contract.source
    reason_code = contract.reason_code
    if contract.liveness is not _OutputContractLiveness.VALUE_REQUIRED:
        if not directive_paths:
            return workflow_yaml, raw_code_artifact_metadata, False
        signature = _output_contract_signature(ctx=ctx, required_paths=directive_paths)
        _arm_output_contract_value_bearing_directive(
            ctx,
            signature=signature,
            workflow_yaml=workflow_yaml,
            liveness=contract.liveness,
        )
        return workflow_yaml, raw_code_artifact_metadata, False
    if not required_paths:
        return workflow_yaml, raw_code_artifact_metadata, False
    signature = _output_contract_signature(ctx=ctx, required_paths=required_paths)
    # The stamp precedes actuation and ignores advisory state so no
    # acceptance route can persist a block that omits the declaration paths.
    workflow_yaml, declaration_stamped = _stamp_declaration_contract_defaults(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        contract,
        signature,
    )
    _reopen_dispatch_lacked_bound_extraction(ctx, signature)
    runtime_attempt_key = _runtime_output_repair_attempt_key(ctx, required_paths)
    label, owner_labels = _target_output_contract_block_label(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        observation_paths,
    )
    if not label or owner_labels != [label]:
        _arm_output_contract_output_owner_directive(ctx, signature=signature, owner_labels=owner_labels)
        return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    target_block = code_blocks.get(label)
    if target_block is None:
        return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
    target_code = str(target_block.get("code") or "")
    current_fingerprint = _output_contract_structural_fingerprint(workflow_yaml, signature)
    if _scout_spine_requires_separated_blocks(ctx):
        synthesized = synthesize_code_block(
            ctx.scout_trajectory,
            strict_selectors=True,
            reached_download_target=getattr(ctx, "reached_download_target", None),
        )
        if synthesized is not None and _browser_surface_contains_full_action_spine(target_code, synthesized.code):
            split = _attempt_separated_spine_split(
                ctx=ctx,
                parsed=parse_workflow_yaml(workflow_yaml),
                label=label,
                target_code=target_code,
                synthesized=synthesized,
                required_paths=required_paths,
                signature=signature,
                declaration_paths=declaration_paths,
            )
            if split.imposed_yaml is not None:
                scaffolded_metadata = _apply_metadata_contract_scaffold(
                    ctx,
                    split.imposed_yaml,
                    raw_code_artifact_metadata,
                    required_paths=observation_paths,
                    source=source,
                    reason_code=reason_code,
                    declaration_paths=declaration_paths,
                )
                _mark_output_contract_imposed(ctx, signature)
                _record_runtime_output_repair_attempt(ctx, runtime_attempt_key)
                LOG.info(
                    "copilot_output_contract_spine_split_imposed",
                    block_label=label,
                    stage_count=split.stage_count,
                    canonical_output_contract_signature=signature,
                )
                return split.imposed_yaml, scaffolded_metadata, True
            actuation = _actuate_output_contract_bail(
                ctx,
                blockers=split.blockers,
                target_code=target_code,
                required_paths=observation_paths,
                signature=signature,
                current_fingerprint=current_fingerprint,
                advisory_run_grantable=True,
                declaration_paths=declaration_paths,
            )
            if actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL:
                _stash_output_source_unobservable_terminal(
                    ctx,
                    reason_code=actuation.reason_code,
                    required_paths=required_paths,
                    block_label=label,
                    signature=signature,
                    blockers=split.blockers,
                )
                return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
            if actuation.kind == OutputContractActuationKind.ADVISORY_RUN:
                return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
            if copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID):
                return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
            attempt_key = _output_contract_spine_directive_attempt_key(
                signature=signature, block_label=label, workflow_yaml=workflow_yaml
            )
            _arm_output_contract_spine_directive(
                ctx,
                attempt_key=attempt_key,
                blockers=split.blockers,
                stage_count=split.stage_count,
                block_label=label,
                signature=signature,
            )
            _record_armed_directive_fingerprint(ctx, signature, current_fingerprint)
            return workflow_yaml, raw_code_artifact_metadata, declaration_stamped
    keyed_code, violations = _extraction_code_with_value_bearing_static_return(
        target_code,
        required_paths=required_paths,
        declaration_paths=declaration_paths,
    )
    if violations:
        produced = _impose_covered_static_return_envelope(
            ctx,
            parsed=parsed,
            workflow_yaml=workflow_yaml,
            raw_code_artifact_metadata=raw_code_artifact_metadata,
            label=label,
            target_code=target_code,
            observation_paths=observation_paths,
            declaration_paths=declaration_paths,
            source=source,
            reason_code=reason_code,
            signature=signature,
            runtime_attempt_key=runtime_attempt_key,
        )
        if produced is not None:
            produced_yaml, produced_metadata, produced_changed = produced
            return produced_yaml, produced_metadata, produced_changed or declaration_stamped
        scaffolded_metadata = _apply_metadata_contract_scaffold(
            ctx,
            workflow_yaml,
            raw_code_artifact_metadata,
            required_paths=observation_paths,
            source=source,
            reason_code=reason_code,
            declaration_paths=declaration_paths,
            allow_missing_static_return=True,
        )
        scaffolded = scaffolded_metadata is not raw_code_artifact_metadata
        if scaffolded:
            _record_runtime_output_repair_attempt(ctx, runtime_attempt_key)
            LOG.info(
                "copilot_output_contract_formation_applied",
                block_label=label,
                canonical_output_contract_signature=signature,
                return_envelope_applied=False,
                metadata_scaffold_applied=True,
            )
        LOG.info(
            "copilot_output_contract_envelope_scaffold_bailed",
            reason="static_return_envelope_unavailable",
            block_label=label,
            canonical_output_contract_signature=signature,
            violations=violations,
        )
        actuation = _actuate_output_contract_bail(
            ctx,
            blockers=["static_return_envelope_unavailable"],
            target_code=target_code,
            required_paths=observation_paths,
            signature=signature,
            current_fingerprint=current_fingerprint,
            declaration_paths=declaration_paths,
        )
        if actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL:
            _stash_output_source_unobservable_terminal(
                ctx,
                reason_code=actuation.reason_code,
                required_paths=required_paths,
                block_label=label,
                signature=signature,
                blockers=["static_return_envelope_unavailable"],
            )
        return workflow_yaml, scaffolded_metadata, scaffolded or declaration_stamped
    changed_code = keyed_code != textwrap.dedent(target_code).strip()
    if changed_code:
        for block in _workflow_code_blocks(parsed):
            if str(block.get("label") or "").strip() == label:
                block["code"] = keyed_code.rstrip() + "\n"
                break
        workflow_yaml = yaml.safe_dump(parsed, sort_keys=False)
    scaffolded_metadata = _apply_metadata_contract_scaffold(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        required_paths=observation_paths,
        source=source,
        reason_code=reason_code,
        declaration_paths=declaration_paths,
    )
    scaffolded = scaffolded_metadata is not raw_code_artifact_metadata
    applied = changed_code or scaffolded
    if applied:
        _mark_output_contract_imposed(ctx, signature)
        _record_runtime_output_repair_attempt(ctx, runtime_attempt_key)
        LOG.info(
            "copilot_output_contract_envelope_imposed_after_steering",
            block_label=label,
            canonical_output_contract_signature=signature,
            steering_reject_count=_output_contract_reject_count(ctx, signature),
            return_envelope_applied=changed_code,
            metadata_scaffold_applied=scaffolded,
        )
    return workflow_yaml, scaffolded_metadata, applied or declaration_stamped


def _has_granted_output_contract_advisory_run(ctx: AgentContext) -> bool:
    return any(
        state == OutputContractAdvisoryState.GRANTED for state in ctx.output_contract_actuation_by_signature.values()
    )


def _metadata_preflight_reject_yields_to_ladder(ctx: AgentContext) -> bool:
    """Register the preflight reject's claim and report whether it must yield to a live GRANTED advisory run.

    The reject always registers its (transient) claim. It yields only when a GRANTED advisory — the ladder's
    decision to dispatch despite deficiencies — owns the turn: yielding lets the forced dispatch consume the grant
    instead of re-blocking it. An ordinary reject cycle (a landed actuation without a grant) is the ladder's own
    mechanism, not a contradiction, so the reject still fires; and it does not yield to a genuinely-terminal owner
    (e.g. a just-committed early terminal), which legitimately ends the turn rather than dispatching a run behind it.
    """
    owner = current_turn_owner(ctx)
    claim_turn(ctx, TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT)
    return (
        _has_granted_output_contract_advisory_run(ctx)
        and owner is not None
        and owner.claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION
    )


def _run_dispatch_definition_reject(ctx: AgentContext, workflow_yaml: str) -> dict[str, Any] | None:
    # A keyless draft may persist (draft-first authoring), but a run must never dispatch
    # while declared string keys are unreferenced — mirrors the update_and_run gate.
    if not workflow_yaml:
        return None
    definition_reject = _definition_plane_preflight_reject(
        ctx,
        workflow_yaml,
        enforce_untagged_declared_inputs=True,
    )
    if definition_reject is None:
        return None
    halted = _stash_unresolved_recorded_outcome_grounding_halt(
        ctx,
        definition_reject.unreferenced_parameter_keys,
    )
    if not halted:
        if _record_definition_plane_ablation_event(
            ctx,
            workflow_yaml,
            definition_reject,
            code_artifact_metadata=ctx.code_artifact_metadata,
        ):
            return None
        _record_definition_plane_reject(
            ctx,
            workflow_yaml,
            definition_reject,
            code_artifact_metadata=ctx.code_artifact_metadata,
        )
    return {
        "ok": False,
        "error": _definition_plane_reject_error(definition_reject),
        "user_facing_summary": _compiled_authoring_user_summary(),
        "data": {
            "reason_code": "definition_contract_unsatisfied",
            "definition_criterion_ids": list(definition_reject.criterion_ids),
            "definition_reason_codes": list(definition_reject.reason_codes),
            "unreferenced_parameter_keys": list(definition_reject.unreferenced_parameter_keys),
        },
    }


def _metadata_contract_run_preflight_reject(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
    runtime_parameters: Mapping[str, Any] | None = None,
    *,
    enforce_untagged_declared_inputs: bool = False,
) -> dict[str, Any] | None:
    if isinstance(ctx, AgentContext):
        ctx.submitted_code_artifact_metadata_snapshot = copy.deepcopy(raw_code_artifact_metadata)
    definition_reject = (
        _definition_plane_preflight_reject(
            ctx,
            workflow_yaml,
            enforce_untagged_declared_inputs=enforce_untagged_declared_inputs,
        )
        if isinstance(ctx, AgentContext)
        else None
    )
    parsed = parse_workflow_yaml(workflow_yaml)
    binding_resolution = (
        _authoring_parameter_binding_resolution_for_definition(
            ctx,
            parsed,
            runtime_parameters,
            raw_code_artifact_metadata,
        )
        if isinstance(ctx, AgentContext) and isinstance(parsed, Mapping)
        else None
    )
    binding_path_available = binding_resolution is not None and (
        binding_resolution.snapshot is not None or binding_resolution.directive is not None
    )
    if definition_reject is not None and (enforce_untagged_declared_inputs or not binding_path_available):
        definition_corrective = (
            None
            if copilot_author_time_gate_log_only_enabled(ctx, DEFINITION_CONTRACT_UNSATISFIED_GATE_ID)
            else _value_bearing_definition_reject_corrective(
                ctx,
                workflow_yaml=workflow_yaml,
                raw_metadata=raw_code_artifact_metadata,
            )
        )
        definition_data = {
            "reason_code": "definition_contract_unsatisfied",
            "definition_criterion_ids": list(definition_reject.criterion_ids),
            "definition_reason_codes": list(definition_reject.reason_codes),
            "unreferenced_parameter_keys": list(definition_reject.unreferenced_parameter_keys),
            **(definition_corrective or {}),
        }
        halted = _stash_unresolved_recorded_outcome_grounding_halt(
            ctx,
            definition_reject.unreferenced_parameter_keys,
        )
        if halted:
            return {
                "ok": False,
                "error": _definition_plane_reject_error(definition_reject),
                "user_facing_summary": _compiled_authoring_user_summary(),
                "data": definition_data,
            }
        if not _record_definition_plane_ablation_event(
            ctx,
            workflow_yaml,
            definition_reject,
            code_artifact_metadata=raw_code_artifact_metadata,
        ):
            _record_definition_plane_reject(
                ctx,
                workflow_yaml,
                definition_reject,
                code_artifact_metadata=raw_code_artifact_metadata,
            )
            return {
                "ok": False,
                "error": _definition_plane_reject_error(definition_reject),
                "user_facing_summary": _compiled_authoring_user_summary(),
                "data": definition_data,
            }
    convergence_reject = _recorded_outcome_convergence_reject(
        ctx,
        workflow_yaml=workflow_yaml,
        code_artifact_metadata=raw_code_artifact_metadata,
    )
    if convergence_reject is not None and not binding_path_available:
        block_labels = sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml))
        _record_author_time_reject_outcome(
            ctx,
            reason_code="unchanged_after_recorded_outcome",
            summary="The authored code and output structure are unchanged after the last recorded test outcome.",
            structural_payload={
                "reason_code": "unchanged_after_recorded_outcome",
                "authored_structure_signature": convergence_reject.authored_structure_signature,
                "block_labels": block_labels,
            },
            authored_structure_signature=convergence_reject.authored_structure_signature,
            block_labels=block_labels,
        )
        _record_code_authoring_guardrail_reject(
            ctx, frontier_unchanged=convergence_reject.reason == "frontier_unchanged"
        )
        LOG.info(
            "copilot recorded outcome convergence behavior",
            convergence_reason=convergence_reject.reason,
            commit_early_terminal=convergence_reject.commit_early_terminal,
            block_labels=block_labels,
        )
        if convergence_reject.commit_early_terminal:
            _commit_recorded_outcome_early_terminal(ctx)
        if _metadata_preflight_reject_yields_to_ladder(ctx):
            return None
        return {
            "ok": False,
            "error": (
                "Submitted workflow left the frontier the last recorded test outcome named unchanged. "
                "Revise the code block or output metadata that owns that frontier before testing again."
            ),
            "user_facing_summary": _compiled_authoring_user_summary(),
            "data": _code_repair_progress_data(),
        }
    evaluation = _evaluate_output_contract_for_code_block(
        ctx,
        workflow_yaml,
        raw_code_artifact_metadata,
        allow_static_return_advisory=True,
        enforce_value_bearing_liveness=True,
    )
    if evaluation is None or not evaluation.has_deficiencies:
        return None
    if _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE in evaluation.shape_violations:
        _revoke_output_contract_advisory_run(ctx, evaluation.canonical_signature)
    authored_fingerprint = _output_contract_structural_fingerprint(workflow_yaml, evaluation.canonical_signature)
    advisory_granted = _output_contract_advisory_granted(ctx, evaluation.canonical_signature)
    # A granted advisory must arm run-output evidence before the grant is consumed, even when the
    # workflow is otherwise run-attemptable — otherwise the dispatched run records no evidence.
    if evaluation.can_attempt_run and not advisory_granted:
        return None
    if _record_output_contract_ablation_event(
        ctx,
        evaluation,
        gate_id=_METADATA_PREFLIGHT_ABLATION_GATE_ID,
        blocked_tool="update_and_run_blocks",
        fingerprint=authored_fingerprint,
    ):
        return None
    payload = _record_output_contract_reject(
        ctx,
        evaluation,
        summary="Submitted workflow does not satisfy the requested output contract before run.",
        authored_structural_fingerprint=authored_fingerprint,
        workflow_yaml=workflow_yaml,
        raw_metadata=raw_code_artifact_metadata,
    )
    if (
        evaluation.can_attempt_run or _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE not in evaluation.shape_violations
    ) and (advisory_granted or _output_contract_advisory_granted(ctx, evaluation.canonical_signature)):
        _arm_pending_run_evidence(ctx, evaluation.canonical_signature, set(evaluation.observation_paths))
        return None
    if evaluation.repair_context is not None:
        ctx.last_code_authoring_repair_context = evaluation.repair_context
    if _metadata_preflight_reject_yields_to_ladder(ctx):
        return None
    if payload.get("reason_code") == METADATA_REJECT_SAME_KEY_TERMINAL_REASON_CODE:
        assert ctx.blocker_signal is not None
        return {
            "ok": False,
            "error": ctx.blocker_signal.user_facing_reason,
            "user_facing_summary": ctx.blocker_signal.user_facing_reason,
            "data": payload,
        }
    if "value_bearing_convergence_directive" in payload:
        return _output_contract_reject_result(
            evaluation,
            payload=payload,
            tool_name="update_and_run_blocks",
        )
    payload = dict(payload)
    payload["output_contract_reason_code"] = payload.get("reason_code")
    payload["reason_code"] = _METADATA_CONTRACT_REQUIRED_BEFORE_RUN_REASON_CODE
    payload["reject_reason"] = _METADATA_CONTRACT_REQUIRED_BEFORE_RUN_REASON_CODE
    block_label = evaluation.block_label or "the target output block"
    return {
        "ok": False,
        "error": (
            "update_and_run_blocks cannot attempt a run until structurally complete "
            f"`code_artifact_metadata` is submitted for `{block_label}`."
        ),
        "user_facing_summary": _compiled_authoring_user_summary(),
        "data": payload,
    }


def metadata_same_key_terminal_preflight(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_code_artifact_metadata: object,
) -> dict[str, CodeArtifactMetadataValue] | None:
    prior_state = ctx.metadata_reject_ladder_state
    if (
        prior_state is None
        or prior_state.streak_count < 2
        or copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID)
    ):
        return None
    missing_error = _missing_code_artifact_metadata_error(workflow_yaml, ctx, raw_code_artifact_metadata)
    if missing_error is None:
        return None
    missing_labels = _missing_code_artifact_metadata_labels(workflow_yaml, ctx, raw_code_artifact_metadata)
    if len(missing_labels) != 1:
        return None
    required_paths, _, coverage_reason_code = _required_child_output_paths_for_authoring(ctx)
    reject_payload = _code_artifact_metadata_reject_payload(
        workflow_yaml=workflow_yaml,
        raw_metadata=raw_code_artifact_metadata,
        offending_labels=[],
        missing_labels=missing_labels,
        violation_categories=["missing_code_artifact_metadata"],
    )
    candidate_outcome = _build_author_time_reject_outcome(
        ctx,
        reason_code="metadata_reject",
        summary=missing_error,
        structural_payload=reject_payload,
        block_labels=missing_labels,
        missing_requested_output_facts=_missing_requested_output_facts(
            required_paths,
            reason_code=coverage_reason_code,
        ),
    )
    structural_key = candidate_outcome.structural_key
    missing_fields_by_label = _metadata_missing_required_fields_by_label(
        raw_code_artifact_metadata,
        labels=missing_labels,
        missing_labels=missing_labels,
    )
    if (
        structural_key is None
        or prior_state.reject_family != "missing_code_artifact_metadata"
        or prior_state.structural_key != structural_key
        or prior_state.gate_id != "code_artifact_metadata"
        or prior_state.missing_fields_by_label != missing_fields_by_label
    ):
        return None
    decision = _metadata_reject_ladder_decision(
        ctx,
        candidate_outcome=candidate_outcome,
        reject_family="missing_code_artifact_metadata",
        missing_fields_by_label=missing_fields_by_label,
    )
    if decision is None or decision.action != "terminal":
        return None
    assert ctx.blocker_signal is not None
    return {
        "ok": False,
        "error": ctx.blocker_signal.user_facing_reason,
        "user_facing_summary": ctx.blocker_signal.user_facing_reason,
        "data": _metadata_reject_terminal_data(decision),
    }


def output_contract_value_bearing_run_reject(
    ctx: AgentContext,
    code_by_label: Mapping[str, str],
) -> dict[str, Any] | None:
    """Refuses dispatch of a saved workflow whose author contract is statically provably
    valueless. Observation paths stay fail-open when unknown; a root-only repair must prove its envelope."""
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    contract = _output_contract_required_paths_source(ctx)
    declaration_only_contract = bool(contract.declaration_paths) and not contract.observation_paths
    root_only_liveness_contract = (
        contract.liveness is _OutputContractLiveness.DEGRADED_EMPTY or declaration_only_contract
    )
    if root_only_liveness_contract:
        statically_valueless = not code_by_label or all(
            _root_output_envelope_state(code) != "proven"
            or _statically_lacks_value_bearing_observation_paths(code, {_VALUE_BEARING_ROOT_GUIDANCE_PATH})
            for code in code_by_label.values()
        )
    else:
        statically_valueless = False
    if contract.liveness is _OutputContractLiveness.VALUE_REQUIRED and code_by_label:
        statically_valueless = all(
            _statically_lacks_value_bearing_observation_paths(code, contract.observation_paths)
            for code in code_by_label.values()
        )
    # The code lane is criteria-independent so the invariant survives drift in the persisted
    # criteria source (an unparseable row must not readmit a statically hollow envelope).
    statically_empty_return_envelope = False
    if not statically_valueless and code_by_label:
        statically_empty_return_envelope = all(
            _statically_valueless_return_envelope(code) for code in code_by_label.values()
        )
        statically_valueless = statically_empty_return_envelope
    if not statically_valueless:
        return None
    LOG.info(
        "copilot_value_bearing_dispatch_preflight_reject",
        contract_liveness=contract.liveness.value,
        canonical_required_child_paths=sorted(contract.union),
        declaration_only_child_paths=sorted(contract.declaration_paths),
        statically_empty_return_envelope=statically_empty_return_envelope,
        block_labels=sorted(code_by_label),
    )
    if statically_empty_return_envelope and not contract.union:
        error = (
            "Cannot run the saved workflow: its code statically returns no values (every return "
            "envelope is empty or null-only). Revise the code to return at least one real value "
            "before running."
        )
    elif contract.liveness is _OutputContractLiveness.DEGRADED_EMPTY or declaration_only_contract:
        error = (
            "Cannot run the saved workflow: its code does not prove a non-empty top-level `output` "
            "value. Revise the owning code block to return a real value under `output` before running."
        )
    else:
        error = (
            "Cannot run the saved workflow: its output contract carries no value-bearing output "
            "(only a blocker declaration or statically empty values). Revise the owning code block "
            "to produce at least one requested value-bearing output before running."
        )
    return {
        "ok": False,
        "error": error,
        "user_facing_summary": _compiled_authoring_user_summary(),
        "data": {
            "reason_code": _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
            "reject_reason": _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE,
            "contract_liveness": contract.liveness.value,
            "canonical_required_child_paths": sorted(contract.union),
            "declaration_only_child_paths": sorted(contract.declaration_paths),
            "statically_empty_return_envelope": statically_empty_return_envelope,
            "degraded_request_slots": [slot.to_payload() for slot in contract.degraded_request_slots],
            "can_attempt_run": False,
        },
    }


def _output_path_root(path: str) -> str:
    return path.split(".", 1)[0].split("[", 1)[0].strip()


def _output_path_has_child(path: str) -> bool:
    return "." in path or "[" in path


def _candidate_missing_required_output_paths(
    workflow_yaml: str,
    code_artifact_metadata: object,
    *,
    required_paths: set[str],
) -> list[str]:
    if not required_paths:
        return []
    required_paths = {path for path in (str(item).strip() for item in required_paths) if path}
    declared_paths_by_label = {
        label: set(paths) for label, paths in _metadata_output_paths_by_label(code_artifact_metadata).items()
    }
    produced_by_label = _workflow_yaml_produced_output_roots_by_label(workflow_yaml)
    covered_paths: set[str] = set()
    abstained_declared_paths: set[str] = set()
    for label, declared_paths in declared_paths_by_label.items():
        produced = produced_by_label.get(label)
        if produced is None:
            continue
        for required_path in required_paths & declared_paths:
            if _top_level_path_segment(required_path) in produced.roots:
                covered_paths.add(required_path)
        if produced.abstained:
            abstained_declared_paths.update(required_paths & declared_paths)
    missing_paths = required_paths - covered_paths
    if missing_paths:
        missing_paths -= abstained_declared_paths
    return sorted(missing_paths)


def _recorded_runtime_produced_output_roots(ctx: AgentContext, workflow_yaml: str) -> set[str]:
    verified_outputs = getattr(ctx, "verified_block_outputs", None)
    if not isinstance(verified_outputs, Mapping):
        return set()
    code_block_labels = set(_workflow_yaml_code_blocks_by_label(workflow_yaml))
    roots: set[str] = set()
    for label, output in verified_outputs.items():
        if not isinstance(label, str) or label not in code_block_labels:
            continue
        roots.update(_meaningful_runtime_output_roots(output))
    return roots


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


def _workflow_yaml_produced_output_roots_by_label(workflow_yaml: str) -> dict[str, _ProducedOutputRoots]:
    return {
        label: _code_block_produced_output_roots(str(block.get("code") or ""))
        for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items()
    }


def _workflow_yaml_produced_output_roots(workflow_yaml: str) -> set[str]:
    roots: set[str] = set()
    for produced in _workflow_yaml_produced_output_roots_by_label(workflow_yaml).values():
        roots.update(produced.roots)
    return roots


def _workflow_yaml_produced_output_paths(workflow_yaml: str) -> set[str]:
    paths: set[str] = set()
    for block in _workflow_yaml_code_blocks_by_label(workflow_yaml).values():
        paths.update(_code_block_produced_output_paths(str(block.get("code") or "")))
    return paths


def _code_block_produced_output_roots(code: str) -> _ProducedOutputRoots:
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return _ProducedOutputRoots(set(), True)
    scope_statements = list(_iter_top_level_scope(tree.body))
    assigned_roots = _assigned_top_level_names(tree.body)
    dict_assignments: dict[str, set[str]] = {}
    dynamic_dict_assignment_names: set[str] = set()
    helper_return_roots = _helper_function_literal_return_roots(tree.body)
    returned_roots: set[str] = set()
    abstained = False
    saw_return = False
    for node in scope_statements:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Dict):
                produced = _dict_literal_output_roots(node.value)
                dict_assignments[node.targets[0].id] = produced.roots
                if produced.abstained:
                    dynamic_dict_assignment_names.add(node.targets[0].id)
        elif isinstance(node, ast.Assign):
            _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Dict):
                produced = _dict_literal_output_roots(node.value)
                dict_assignments[node.target.id] = produced.roots
                if produced.abstained:
                    dynamic_dict_assignment_names.add(node.target.id)
            _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, node)
        elif isinstance(node, ast.AugAssign):
            _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, node)
        elif isinstance(node, ast.Return):
            saw_return = True
            if node.value is not None:
                returned = _return_output_roots(
                    node.value,
                    dict_assignments,
                    dynamic_dict_assignment_names,
                    helper_return_roots,
                )
                returned_roots.update(returned.roots)
                abstained = abstained or returned.abstained
    roots = returned_roots if saw_return else assigned_roots
    return _ProducedOutputRoots(roots, abstained)


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


def _top_level_simple_assignment_names(code: str) -> set[str]:
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _output_path_direct_child(path: str, root: str) -> str:
    if not path.startswith(root + "."):
        return ""
    child = path[len(root) + 1 :]
    return re.split(r"[.\[]", child, maxsplit=1)[0].strip()


def _return_scaffold_name_is_safe(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _extraction_code_with_required_static_return(
    code: str,
    *,
    required_paths: set[str],
    declaration_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    declaration_paths = declaration_paths or set()
    required_paths = required_paths | declaration_paths
    stripped_code = textwrap.dedent(code).strip()
    if not stripped_code or not required_paths:
        return stripped_code, []
    if required_paths <= _code_block_produced_output_paths(stripped_code):
        return stripped_code, []
    try:
        tree = ast.parse(stripped_code)
    except SyntaxError:
        return stripped_code, []
    declaration_children = {
        child
        for path in declaration_paths
        if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
    }
    if any(isinstance(node, ast.Return) for node in _iter_top_level_scope(tree.body)):
        replacement = _replace_direct_child_local_return(stripped_code, tree, required_paths, declaration_children)
        if replacement and required_paths <= _code_block_produced_output_paths(replacement):
            return replacement, []
        missing = sorted(required_paths - _code_block_produced_output_paths(stripped_code))
        return stripped_code, [
            "Unable to impose synthesized code block: selected output extraction does not return a keyed "
            f"structure covering required output path(s): {', '.join(missing)}."
        ]
    assigned_names = _top_level_simple_assignment_names(stripped_code)
    roots = {_output_path_root(path) for path in required_paths if _output_path_root(path)}
    candidate = ""
    if len(roots) == 1:
        root = next(iter(roots))
        if _return_scaffold_name_is_safe(root) and root in assigned_names:
            candidate = stripped_code + f'\nreturn {{"{root}": {root}}}'
        elif root == "output" and "output" in assigned_names:
            candidate = stripped_code + "\nreturn output"
        elif root == "output":
            child_names = sorted(
                {
                    child
                    for path in required_paths
                    if (child := _output_path_direct_child(path, "output"))
                    and _return_scaffold_name_is_safe(child)
                    and (child in assigned_names or child in declaration_children)
                }
            )
            required_child_names = {
                child
                for path in required_paths
                if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
            }
            if child_names and set(child_names) == required_child_names:
                child_pairs = ", ".join(
                    f'"{name}": None'
                    if name in declaration_children and name not in assigned_names
                    else f'"{name}": {name}'
                    for name in child_names
                )
                candidate = stripped_code + f'\nreturn {{"output": {{{child_pairs}}}}}'
    if not candidate and len(roots) == 1:
        candidate = _single_mapping_local_static_return_candidate(stripped_code, next(iter(roots)), required_paths)
    if candidate and required_paths <= _code_block_produced_output_paths(candidate):
        return candidate, []
    missing = sorted(required_paths - _code_block_produced_output_paths(candidate or stripped_code))
    return stripped_code, [
        "Unable to impose synthesized code block: selected output extraction does not return a keyed "
        f"structure covering required output path(s): {', '.join(missing)}."
    ]


def _extraction_code_with_value_bearing_static_return(
    code: str,
    *,
    required_paths: set[str],
    declaration_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    keyed_code, violations = _extraction_code_with_required_static_return(
        code, required_paths=required_paths, declaration_paths=declaration_paths
    )
    if violations:
        return keyed_code, violations
    liveness_violations = _static_value_bearing_violations(keyed_code, required_paths - (declaration_paths or set()))
    if liveness_violations:
        return textwrap.dedent(code).strip(), liveness_violations
    return keyed_code, []


def _single_mapping_local_static_return_candidate(code: str, root: str, required_paths: set[str]) -> str:
    """When the extraction suffix, on all top-level paths, assigns exactly one dict-literal
    local and never returns, key that one mapping to the required root. Bounded to a single
    top-level mapping local and a single required root; anything branchier falls through."""
    if not _return_scaffold_name_is_safe(root):
        return ""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    if any(isinstance(node, ast.Return) for node in _iter_top_level_scope(tree.body)):
        return ""
    mapping_locals: list[str] = []
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
            continue
        if not value.keys or any(
            not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in value.keys
        ):
            continue
        mapping_locals.append(target.id)
    if len(set(mapping_locals)) != 1:
        return ""
    mapping_local = mapping_locals[-1]
    if not _return_scaffold_name_is_safe(mapping_local):
        return ""
    return code + f'\nreturn {{"{root}": {mapping_local}}}'


def _replace_direct_child_local_return(
    code: str,
    tree: ast.Module,
    required_paths: set[str],
    declaration_children: set[str] | None = None,
) -> str:
    declaration_children = declaration_children or set()
    roots = {_output_path_root(path) for path in required_paths if _output_path_root(path)}
    if roots != {"output"}:
        return ""
    child_names = sorted(
        {
            child
            for path in required_paths
            if (child := _output_path_direct_child(path, "output")) and _return_scaffold_name_is_safe(child)
        }
    )
    local_child_names = [child for child in child_names if child not in declaration_children]
    if len(local_child_names) != 1:
        return ""
    child_name = local_child_names[0]
    returns = [node for node in tree.body if isinstance(node, ast.Return)]
    if len(returns) != 1:
        return ""
    return_node = returns[0]
    if not isinstance(return_node.value, ast.Name) or return_node.value.id != child_name:
        return ""
    if return_node.end_lineno is None:
        return ""
    lines = code.splitlines()
    if return_node.lineno < 1 or return_node.lineno > len(lines):
        return ""
    indent_match = re.match(r"\s*", lines[return_node.lineno - 1])
    if indent_match is None:
        return ""
    indent = indent_match.group(0)
    pairs = ", ".join(
        f'"{name}": None' if name in declaration_children else f'"{name}": {name}' for name in child_names
    )
    replacement = f'{indent}return {{"output": {{{pairs}}}}}'
    return "\n".join(
        [
            *lines[: return_node.lineno - 1],
            replacement,
            *lines[return_node.end_lineno :],
        ]
    )


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
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    else:
        targets = [node.target]
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


def _helper_function_literal_return_roots(statements: list[ast.stmt]) -> dict[str, _ProducedOutputRoots]:
    helpers: dict[str, _ProducedOutputRoots] = {}
    for statement in statements:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dict_assignments: dict[str, set[str]] = {}
        dynamic_dict_assignment_names: set[str] = set()
        roots: set[str] = set()
        abstained = False
        saw_return = False
        for helper_statement in _iter_top_level_scope(statement.body):
            if (
                isinstance(helper_statement, ast.Assign)
                and len(helper_statement.targets) == 1
                and isinstance(helper_statement.targets[0], ast.Name)
            ):
                if isinstance(helper_statement.value, ast.Dict):
                    produced = _dict_literal_output_roots(helper_statement.value)
                    dict_assignments[helper_statement.targets[0].id] = produced.roots
                    if produced.abstained:
                        dynamic_dict_assignment_names.add(helper_statement.targets[0].id)
            elif isinstance(helper_statement, ast.Assign):
                _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, helper_statement)
            elif isinstance(helper_statement, ast.AnnAssign):
                if isinstance(helper_statement.target, ast.Name) and isinstance(helper_statement.value, ast.Dict):
                    produced = _dict_literal_output_roots(helper_statement.value)
                    dict_assignments[helper_statement.target.id] = produced.roots
                    if produced.abstained:
                        dynamic_dict_assignment_names.add(helper_statement.target.id)
                _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, helper_statement)
            elif isinstance(helper_statement, ast.AugAssign):
                _apply_literal_dict_key_assignment(dict_assignments, dynamic_dict_assignment_names, helper_statement)
            elif isinstance(helper_statement, ast.Return):
                saw_return = True
                if helper_statement.value is not None:
                    returned = _return_output_roots(
                        helper_statement.value,
                        dict_assignments,
                        dynamic_dict_assignment_names,
                        {},
                    )
                    roots.update(returned.roots)
                    abstained = abstained or returned.abstained
        if roots or abstained or saw_return:
            helpers[statement.name] = _ProducedOutputRoots(roots, abstained)
    return helpers


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


def _dict_literal_string_key_roots(node: ast.expr) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    roots: set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.strip():
            roots.add(key.value.strip())
    return roots


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


def _output_empty_code_block_labels(workflow_yaml: str, code_artifact_metadata: object) -> list[str]:
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    if not code_blocks:
        return []
    metadata_by_label = code_artifact_metadata if isinstance(code_artifact_metadata, Mapping) else {}
    labels_without_output = [
        label
        for label, block in code_blocks.items()
        if not _code_block_has_meaningful_output(
            str(block.get("code") or ""),
            metadata_by_label.get(label),
        )
    ]
    return sorted(labels_without_output) if len(labels_without_output) == len(code_blocks) else []


def _code_block_has_meaningful_output(code: str, artifact: object) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    helper_return_roots = _helper_function_literal_return_roots(tree.body)

    class ReturnVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.has_meaningful_return = False

        def visit_Return(self, node: ast.Return) -> None:
            if _return_value_is_meaningful(node.value, helper_return_roots):
                self.has_meaningful_return = True

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    visitor = ReturnVisitor()
    visitor.visit(tree)
    if visitor.has_meaningful_return:
        return True
    if _code_block_has_top_level_return(tree):
        return False
    if isinstance(artifact, Mapping):
        goal_roots = _artifact_goal_value_roots(artifact)
        if goal_roots and _missing_declared_output_roots(code, goal_roots) is None:
            return True
    return False


@dataclass(frozen=True)
class _ScoutedSelectOptionInteraction:
    selector: str
    source_origin: str
    value_present: bool
    label_present: bool


def _scouted_select_option_interactions(ctx: AgentContext) -> list[_ScoutedSelectOptionInteraction]:
    trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(trajectory, list):
        return []
    interactions: list[_ScoutedSelectOptionInteraction] = []
    seen: set[tuple[str, str, bool, bool]] = set()
    for interaction in trajectory:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != "select_option":
            continue
        selector = _safe_selector_repair_atom(interaction.get("selector"))
        if not selector:
            continue
        raw_source_url = str(interaction.get("source_url") or "").strip()
        source_origin = (url_origin(raw_source_url) or "") if raw_source_url else ""
        entry = _ScoutedSelectOptionInteraction(
            selector=selector,
            source_origin=source_origin,
            value_present=bool(str(interaction.get("value") or "").strip()),
            label_present=bool(str(interaction.get("label") or interaction.get("option_label") or "").strip()),
        )
        key = (entry.selector, entry.source_origin, entry.value_present, entry.label_present)
        if key in seen:
            continue
        seen.add(key)
        interactions.append(entry)
    return interactions


def _select_option_text_click_repair_context(
    workflow_yaml: str, ctx: AgentContext
) -> CodeAuthoringRepairContext | None:
    expected_interactions = _scouted_select_option_interactions(ctx)
    if not expected_interactions:
        return None
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    candidate_selectors = _workflow_select_option_call_selectors(code_blocks.values())
    if None in candidate_selectors:
        return None
    missing_interactions = [
        interaction for interaction in expected_interactions if interaction.selector not in candidate_selectors
    ]
    if not missing_interactions:
        return None
    for label, block in code_blocks.items():
        tree = _parsed_code_tree(str(block.get("code") or ""))
        if tree is None or not _code_contains_get_by_text_click(tree):
            continue
        missing_interaction = missing_interactions[0]
        return CodeAuthoringRepairContext(
            block_label=label,
            reason_code="select_option_interaction_mismatch",
            selector=missing_interaction.selector,
            source_url=missing_interaction.source_origin or None,
            current_origin=missing_interaction.source_origin or None,
            binding_candidates=[
                "expected_tool:select_option",
                "authored_tool:get_by_text_click",
                f"value_present:{missing_interaction.value_present}",
                f"label_present:{missing_interaction.label_present}",
            ],
            repair_instruction=(
                "Use the captured select element API for this interaction, for example "
                "page.locator(selector).select_option(...), instead of clicking option text."
            ),
        )
    return None


def _workflow_select_option_call_selectors(code_blocks: Iterable[Mapping[str, Any]]) -> set[str | None]:
    selectors: set[str | None] = set()
    for block in code_blocks:
        tree = _parsed_code_tree(str(block.get("code") or ""))
        if tree is not None:
            selectors.update(_select_option_call_selectors(tree))
    return selectors


def _parsed_code_tree(code: str) -> ast.AST | None:
    try:
        return ast.parse(code)
    except SyntaxError:
        return None


def _code_contains_get_by_text_click(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "click"
            and _call_chain_contains_method(node.func.value, "get_by_text")
        ):
            return True
    return False


def _select_option_call_selectors(tree: ast.AST) -> set[str | None]:
    locator_aliases = _locator_alias_selectors(tree)
    selectors: set[str | None] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "select_option":
            continue
        resolved = _locator_receiver_selectors(node.func.value, locator_aliases)
        if resolved:
            selectors.update(resolved)
        else:
            selectors.add(None)
    return selectors


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


def _code_block_has_top_level_return(tree: ast.AST) -> bool:
    body = tree.body if isinstance(tree, ast.Module) else []
    return any(isinstance(statement, ast.Return) for statement in _iter_top_level_scope(body))


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


def _signal_is_churn(signal: CopilotToolBlockerSignal | None) -> bool:
    return signal is not None and signal.internal_reason_code in _CHURN_REASON_CODES


def _clear_held_churn_signals(ctx: AgentContext) -> None:
    if _signal_is_churn(ctx.blocker_signal):
        ctx.blocker_signal = None
        ctx.blocker_signal_claimant = None
    if _signal_is_churn(ctx.latest_tool_blocker_signal):
        ctx.latest_tool_blocker_signal = None


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


def _declared_workflow_parameter_keys(parsed: dict[str, Any]) -> set[str] | None:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return set()
    parameters = workflow_definition.get("parameters")
    if parameters is None:
        return set()
    if not isinstance(parameters, list):
        return None
    return {
        str(parameter.get("key") or "").strip()
        for parameter in parameters
        if isinstance(parameter, Mapping) and str(parameter.get("key") or "").strip()
    }


_ORDERED_CHILD_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_ORDERED_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")


def _code_block_parameter_contract_error(workflow_yaml: str) -> str | None:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return None
    declared_parameter_keys = _declared_workflow_parameter_keys(parsed)
    if declared_parameter_keys is None:
        return "Unable to validate code-block parameter keys: workflow_definition.parameters must be a list."
    registered_download_keys = set(REGISTERED_DOWNLOAD_OUTPUT_KEYS)

    errors: list[str] = []

    def block_output_key(block: Mapping[str, Any]) -> str | None:
        label = str(block.get("label") or "").strip()
        return f"{label}_output" if label else None

    def check_code_block(block: Mapping[str, Any], available_parameter_keys: set[str]) -> None:
        label = str(block.get("label") or "").strip() or "unlabeled code block"
        code = str(block.get("code") or "")
        for line_number, line in enumerate(code.splitlines(), start=1):
            marker = _conflict_marker_for_line(line)
            if marker is not None:
                errors.append(
                    f"Code block `{label}` contains unresolved conflict marker `{marker}` on code line "
                    f"{line_number}. Remove every git conflict marker line before retrying."
                )
                break
        parameter_keys = _code_block_parameter_keys(block)
        registered_keys = sorted(parameter_keys & registered_download_keys)
        if registered_keys:
            joined = ", ".join(f"`{key}`" for key in registered_keys)
            errors.append(
                f"Code block `{label}` lists registered download output key(s) {joined} in `parameter_keys`, "
                "but the execution layer injects registered download output keys only after a browser download "
                "fires. Remove them from `parameter_keys` and return a small descriptor instead."
            )
        undeclared_keys = sorted(parameter_keys - available_parameter_keys - registered_download_keys)
        if undeclared_keys:
            joined = ", ".join(f"`{key}`" for key in undeclared_keys)
            errors.append(
                f"Code block `{label}` lists undeclared `parameter_keys`: {joined}. Declare each workflow input "
                "under `workflow_definition.parameters`, use a prior block output key such as "
                "`<block_label>_output`, or remove stale `parameter_keys` entries before retrying."
            )

    def visit_branch(branch: Mapping[str, Any], available_parameter_keys: set[str]) -> None:
        for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
            # Child scopes inherit known keys without leaking their outputs back into sibling branches.
            visit_blocks(branch.get(key), set(available_parameter_keys))
        for branch_key in _ORDERED_BRANCH_LIST_KEYS:
            branches = branch.get(branch_key)
            if not isinstance(branches, list):
                continue
            for nested_branch in branches:
                if isinstance(nested_branch, Mapping):
                    # Branch scopes intentionally isolate output keys from their parent branch.
                    visit_branch(nested_branch, set(available_parameter_keys))

    def visit_blocks(blocks: Any, available_parameter_keys: set[str]) -> set[str]:
        if not isinstance(blocks, list):
            return available_parameter_keys
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
                check_code_block(block, available_parameter_keys)
            for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
                visit_blocks(block.get(key), set(available_parameter_keys))
            for branch_key in _ORDERED_BRANCH_LIST_KEYS:
                branches = block.get(branch_key)
                if not isinstance(branches, list):
                    continue
                for branch in branches:
                    if isinstance(branch, Mapping):
                        visit_branch(branch, set(available_parameter_keys))
            output_key = block_output_key(block)
            if output_key:
                available_parameter_keys.add(output_key)
        return available_parameter_keys

    workflow_definition = parsed.get("workflow_definition")
    blocks = workflow_definition.get("blocks") if isinstance(workflow_definition, dict) else None
    try:
        visit_blocks(blocks, set(declared_parameter_keys))
    except RecursionError:
        return "Workflow YAML nesting is too deep to validate."
    return "\n".join(errors) if errors else None


class _NeverCapturedObligationCandidate(NamedTuple):
    block_label: str
    site: str
    method: str
    receiver: str
    call_shape: str
    expected_tool_name: str
    expected_argument_literal: str | None


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


@dataclass
class _SynthesizedCodeImpositionResult:
    workflow_yaml: str
    substitutions: dict[str, Any] | None = None
    violations: list[str] = dataclass_field(default_factory=list)
    repair_context: CodeAuthoringRepairContext | None = None
    scrubbed_selected_metadata_label: str | None = None
    selected_extraction_metadata_disposition: SelectedExtractionMetadataDisposition = "none"
    minted_parameter_keys: list[str] = dataclass_field(default_factory=list)
    metadata_repair_contract: dict[str, object] | None = None
    ablation_gate_id: str | None = None
    never_captured_candidate: _NeverCapturedObligationCandidate | None = None
    omission_digest: str | None = None


_SUBMITTED_LITERAL_METHODS = frozenset({"fill", "type"})
_SECRET_LIKE_LITERAL_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key|credential|bearer\s+|sk-[a-zA-Z0-9])",
    re.I,
)


def _compiled_authoring_user_summary() -> str:
    return "I need to bind the compiled browser-step code safely before saving this workflow."


def _ambiguous_bare_selector_repair_context(
    *,
    code_block: Mapping[str, Any],
    dropped: Mapping[str, Any],
    scout_trajectory: list[ScoutedInteraction],
) -> CodeAuthoringRepairContext | None:
    if dropped.get("reason_code") != "ambiguous_bare_selector":
        return None
    dropped_selector = str(dropped.get("selector") or "").strip()
    if not dropped_selector:
        return None
    dropped_index = dropped.get("trajectory_index")
    source_url = ""
    refiner_selector: str | None = None
    selector_alternatives: list[dict[str, str]] = []
    if isinstance(dropped_index, int) and 0 <= dropped_index < len(scout_trajectory):
        source_url = str(scout_trajectory[dropped_index].get("source_url") or "").strip()
        for later in scout_trajectory[dropped_index + 1 :]:
            if source_url and str(later.get("source_url") or "").strip() != source_url:
                continue
            later_selector = str(later.get("selector") or "").strip()
            if later_selector and _selector_refines(dropped_selector, later_selector):
                refiner_selector = later_selector
                break
        if refiner_selector is None:
            selector_alternatives = _ambiguous_selector_same_page_alternatives(
                dropped_selector=dropped_selector,
                source_url=source_url,
                scout_trajectory=scout_trajectory,
                dropped_index=dropped_index,
            )
    safe_source_url = url_origin(source_url) if source_url else None
    return CodeAuthoringRepairContext(
        block_label=str(code_block.get("label") or ""),
        reason_code="ambiguous_bare_selector",
        selector=dropped_selector,
        source_url=safe_source_url,
        refiner_selector=refiner_selector,
        selector_alternatives=selector_alternatives,
        repair_instruction=(
            "Replace the ambiguous bare selector with a unique selector or role/name locator from the same page."
        ),
    )


def _same_page_url(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme,
        left_parts.netloc,
        left_parts.path.rstrip("/"),
    ) == (
        right_parts.scheme,
        right_parts.netloc,
        right_parts.path.rstrip("/"),
    )


def _safe_selector_repair_atom(value: Any, *, max_chars: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text or len(text) > max_chars:
        return ""
    if _SECRET_LIKE_LITERAL_RE.search(text):
        return ""
    return text


def _ambiguous_selector_same_page_alternatives(
    *,
    dropped_selector: str,
    source_url: str,
    scout_trajectory: list[ScoutedInteraction],
    dropped_index: int,
) -> list[dict[str, str]]:
    alternatives: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, interaction in enumerate(scout_trajectory):
        if index == dropped_index:
            continue
        if not _same_page_url(source_url, str(interaction.get("source_url") or "").strip()):
            continue
        tool_name = _safe_selector_repair_atom(interaction.get("tool_name"), max_chars=40)
        selector = _safe_selector_repair_atom(interaction.get("selector"))
        if not selector or selector == dropped_selector or _is_positional_selector(selector):
            continue
        role = _safe_selector_repair_atom(interaction.get("role"), max_chars=60)
        alternative = {"tool_name": tool_name, "role": role, "selector": selector}
        key = (alternative["tool_name"], alternative["role"], alternative["selector"])
        if key in seen:
            continue
        seen.add(key)
        alternatives.append(alternative)
    return alternatives


def _prior_yaml_source(ctx: AgentContext) -> tuple[str, str | None]:
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    if isinstance(last_yaml, str) and last_yaml:
        return "last_workflow_yaml", last_yaml
    workflow_yaml = getattr(ctx, "workflow_yaml", None)
    return "workflow_yaml", workflow_yaml if isinstance(workflow_yaml, str) else None


def _workflow_code_blocks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for block in workflow_blocks(parsed)
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value
    ]


_CODE_OUTPUT_INTENT_RE = re.compile(
    r"\b(?:extract|output|read\s+back|capture\s+(?:the\s+)?(?:data|fields|values)|return\s+structured)\b",
    re.I,
)


def _raw_code_artifact_metadata_empty(raw_metadata: Any) -> bool:
    return raw_metadata in (None, [], {})


def _workflow_declares_output_parameter(parsed: Mapping[str, Any]) -> bool:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, Mapping):
        return False
    parameters = definition.get("parameters")
    if not isinstance(parameters, list):
        return False
    return any(
        isinstance(parameter, Mapping) and _enum_or_string_name(parameter.get("parameter_type")) == "output"
        for parameter in parameters
    )


def _block_declares_output_intent(block: Mapping[str, Any]) -> bool:
    text = " ".join(str(block.get(field_name) or "") for field_name in ("prompt", "description", "title", "label"))
    return bool(_CODE_OUTPUT_INTENT_RE.search(text))


def _existing_metadata_covers_output(label: str, existing_metadata: Any) -> bool:
    if not isinstance(existing_metadata, Mapping):
        return False
    metadata = existing_metadata.get(label)
    return isinstance(metadata, Mapping) and _artifact_declares_goal_values(metadata)


def _missing_code_artifact_metadata_labels(workflow_yaml: str, ctx: AgentContext, raw_metadata: Any) -> list[str]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return []
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return []
    code_blocks = _workflow_code_blocks(dict(parsed))
    if not code_blocks:
        return []
    has_workflow_output = _workflow_declares_output_parameter(parsed)
    output_intent_blocks = [block for block in code_blocks if _block_declares_output_intent(block)]
    if has_workflow_output and not output_intent_blocks and len(code_blocks) == 1:
        output_intent_blocks = list(code_blocks)
    labels = [
        str(block.get("label") or "").strip() or "this code block"
        for block in (output_intent_blocks if has_workflow_output else code_blocks)
        if has_workflow_output or _block_declares_output_intent(block)
    ]
    if not labels:
        return []
    existing_metadata = getattr(ctx, "code_artifact_metadata", None)
    if _raw_code_artifact_metadata_empty(raw_metadata):
        return [label for label in labels if not _existing_metadata_covers_output(label, existing_metadata)]
    return [
        label for label in labels if not _raw_metadata_covers_output_label(raw_metadata, label, candidate_labels=labels)
    ]


def _missing_code_artifact_metadata_error(workflow_yaml: str, ctx: AgentContext, raw_metadata: Any) -> str | None:
    missing = _missing_code_artifact_metadata_labels(workflow_yaml, ctx, raw_metadata)
    if not missing:
        return None
    joined = ", ".join(f"`{label}`" for label in missing)
    return (
        f"Code-only browser workflow output block(s) {joined} must pass `code_artifact_metadata` with concrete "
        "`goal_value_paths` before saving or testing. Include an `extraction_schema` when the user requested "
        "structured fields, and make the code return a keyed dict/list or expose top-level locals matching those "
        "goal path roots."
    )


def _submitted_code_block_changed(block: Mapping[str, Any], prior_yaml: str | None) -> bool:
    label = str(block.get("label") or "").strip()
    if not label or not prior_yaml:
        return True
    prior = parse_workflow_yaml(prior_yaml)
    if not isinstance(prior, dict):
        return True
    for prior_block in _workflow_code_blocks(prior):
        if str(prior_block.get("label") or "").strip() != label:
            continue
        return str(prior_block.get("code") or "") != str(block.get("code") or "")
    return True


def _should_impose_after_update_attempt(ctx: AgentContext, *, repeated_identical_omission: bool = False) -> bool:
    target = ctx.reached_download_target
    return (
        (isinstance(target, ReachedDownloadTarget) and not target.already_registered and bool(target.selector.strip()))
        or synthesized_persistence_reopened_after_failed_run(ctx)
        or ctx.synthesized_block_reopened_for_credential_scout
        or ctx.synthesized_block_reopened_for_capture_obligation
        or repeated_identical_omission
        or _author_time_reject_reopens_synthesized_imposition(ctx)
    )


def _current_draft_repeats_prior_scouted_spine_omission(workflow_yaml: str, ctx: AgentContext) -> bool:
    """The repeated-omission imposition arm admits only when the CURRENT draft still leaves the same
    scouted-spine rungs open: recompute this draft's omission digest and require it non-empty and equal
    to the immediately-prior reject's digest, so A,A,B never imposes on B off a latch set by a prior draft."""
    if not ctx.scouted_spine_repeated_identical_missing_steps:
        return False
    current = _pre_persist_scouted_spine_result(workflow_yaml, ctx)
    digest = current.omission_digest if current is not None else None
    if digest is None or digest != ctx.scouted_spine_previous_omission_digest:
        ctx.scouted_spine_repeated_identical_missing_steps = False
        ctx.scouted_spine_previous_omission_digest = None
        return False
    return True


def _author_time_reject_reopens_synthesized_imposition(ctx: AgentContext) -> bool:
    latest = ctx.latest_recorded_build_test_outcome
    if not (
        isinstance(latest, RecordedBuildTestOutcome)
        and latest.is_authoritative
        and latest.phase == "author_time_reject"
    ):
        return False
    if latest.reason_code in {"metadata_reject", "synthesized_parameter_binding_ambiguous"}:
        return True
    repair_context = ctx.last_code_authoring_repair_context
    # Ambiguous bare selectors are repaired by the same strict imposition path as metadata gaps.
    return (
        latest.reason_code == "code_safety_reject"
        and isinstance(repair_context, CodeAuthoringRepairContext)
        and repair_context.reason_code == "ambiguous_bare_selector"
    )


def _reopen_download_target_registers(ctx: AgentContext) -> bool:
    target = ctx.reached_download_target
    return isinstance(target, ReachedDownloadTarget) and not target.already_registered and bool(target.selector.strip())


def _log_imposition_skipped_after_update(ctx: AgentContext) -> None:
    scout_trajectory = ctx.scout_trajectory
    if not scout_trajectory:
        return
    LOG.info(
        "copilot_imposition_skipped_after_update",
        trajectory_length=len(scout_trajectory),
        reopen_download_target=_reopen_download_target_registers(ctx),
        reopen_persistence_after_failed_run=synthesized_persistence_reopened_after_failed_run(ctx),
        reopen_author_time_reject=_author_time_reject_reopens_synthesized_imposition(ctx),
        reaches_goal=synthesized_trajectory_reaches_goal(ctx),
        goal_complete=synthesized_trajectory_is_goal_complete(ctx),
        synthesized_goal_complete_landed=ctx.synthesized_goal_complete_landed,
    )


def _imposition_admission_key_after_update(ctx: AgentContext) -> str:
    if synthesized_goal_completion_landing_pending(ctx):
        return "goal_completion_landing_pending"
    if synthesized_trajectory_reaches_goal(ctx) and not ctx.synthesized_goal_complete_landed:
        return "goal_reaching_spine_unlanded"
    if _reopen_download_target_registers(ctx):
        return "reopen_download_target"
    if synthesized_persistence_reopened_after_failed_run(ctx):
        return "reopen_failed_run"
    return "reopen_author_time_reject"


def _log_imposition_admitted_after_update(ctx: AgentContext) -> None:
    LOG.info(
        "copilot_imposition_admitted_after_update",
        admission_key=_imposition_admission_key_after_update(ctx),
        trajectory_length=len(ctx.scout_trajectory),
        goal_complete=synthesized_trajectory_is_goal_complete(ctx),
    )


def _recorded_outcome_imposition_block_labels(ctx: AgentContext) -> frozenset[str]:
    constraint = ctx.recorded_outcome_binding_constraint
    if isinstance(constraint, RecordedOutcomeBindingConstraint):
        return frozenset(label.strip() for label in constraint.owning_block_labels if label.strip())
    latest = ctx.latest_recorded_build_test_outcome
    if isinstance(latest, RecordedBuildTestOutcome) and latest.is_authoritative:
        return frozenset(label.strip() for label in latest.block_labels if label.strip())
    return frozenset()


def _select_synthesized_imposition_code_block(
    code_blocks: list[dict[str, Any]],
    *,
    prior_yaml: str | None,
    preferred_labels: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if len(code_blocks) == 1:
        return code_blocks[0]

    if preferred_labels:
        preferred_matches = [
            block for block in code_blocks if str(block.get("label") or "").strip() in preferred_labels
        ]
        if len(preferred_matches) == 1:
            return preferred_matches[0]

    synthesized_label_matches = [
        block for block in code_blocks if str(block.get("label") or "").strip() == _SYNTHESIZED_BLOCK_LABEL
    ]
    if len(synthesized_label_matches) == 1:
        synthesized_block = synthesized_label_matches[0]
        if not code_is_download_intent(str(synthesized_block.get("code") or "")):
            return synthesized_block
        return None
    if synthesized_label_matches:
        return None

    changed_blocks = [block for block in code_blocks if _submitted_code_block_changed(block, prior_yaml)]
    if len(changed_blocks) == 1:
        return changed_blocks[0]

    changed_without_download_intent = [
        block for block in changed_blocks if not code_is_download_intent(str(block.get("code") or ""))
    ]
    if len(changed_without_download_intent) == 1:
        return changed_without_download_intent[0]
    return None


_ADMISSIBLE_BROWSER_COVERAGE_ROUTE = (
    "The scouted spine is the browser draft: extend browser coverage by scouting the step so the spine carries it, "
    "never by authoring browser calls freehand."
)


class _StaleSpineRungReplacement(NamedTuple):
    violation: str | None
    replaced_labels: list[str]


def _code_block_drives_browser(block: Mapping[str, Any]) -> bool:
    mutations, _, ambiguous = _browser_surface_for_code(str(block.get("code") or ""))
    return bool(mutations or ambiguous)


def _spine_carrier_code_block(
    code_blocks: list[dict[str, Any]],
    *,
    prior_yaml: str | None,
) -> dict[str, Any] | None:
    if not code_blocks:
        return None
    changed_blocks = [block for block in code_blocks if _submitted_code_block_changed(block, prior_yaml)]
    for block in changed_blocks:
        if _code_block_drives_browser(block):
            return block
    return changed_blocks[0] if changed_blocks else code_blocks[0]


def _stale_spine_rung_blocks(
    code_blocks: list[dict[str, Any]],
    *,
    carrier: Mapping[str, Any],
    prior_yaml: str | None,
) -> list[dict[str, Any]]:
    """Changed non-carrier browser-driving blocks, each on an owned attempt either a duplicate spine rung or an
    ungrounded hand-authored one that the spine replaces. Blocks that only parse or extract carry no browser surface
    and are never touched."""
    return [
        block
        for block in code_blocks
        if block is not carrier
        and _submitted_code_block_changed(block, prior_yaml)
        and _code_block_drives_browser(block)
    ]


def _stale_spine_rung_provenance(
    stale_rungs: list[dict[str, Any]],
    *,
    scouted_mutations: list[_BrowserMutationSignature],
    diagnostics: SynthesisDiagnostics,
) -> list[_BrowserSurfaceRejectionProvenance]:
    """Provenance for the ungrounded actions among the replaced rungs; a rung that only duplicates scouted
    signatures yields none, which is what distinguishes a duplicate from a hand-authored rung in the record."""
    scouted_signatures = set(scouted_mutations)
    provenance: list[_BrowserSurfaceRejectionProvenance] = []
    for block in stale_rungs:
        label = _code_block_label(block)
        mutations, _, ambiguous = _browser_surface_for_code(str(block.get("code") or ""))
        provenance.extend(
            _classify_unscouted_mutation(
                mutation,
                scouted_mutations=scouted_mutations,
                diagnostics=diagnostics,
                site="whole_trajectory",
                block_label=label,
            )
            for mutation in sorted(mutation for mutation in mutations if mutation not in scouted_signatures)
        )
        provenance.extend(
            _ambiguous_browser_action_provenance(action, site="whole_trajectory", block_label=label)
            for action in sorted(ambiguous)
        )
    return provenance


def _draft_leaves_scouted_partition_open(
    code_blocks: list[dict[str, Any]],
    *,
    synthesized: SynthesizedCodeBlock,
    scout_trajectory: list[ScoutedInteraction],
) -> bool:
    draft_calls = [
        (mutation.method, mutation.receiver)
        for block in code_blocks
        for mutation in _browser_surface_for_code(str(block.get("code") or ""))[0]
    ]
    return bool(spine_partition_findings(synthesized.diagnostics, draft_calls, scout_trajectory))


def _code_block_browser_actions(block: Mapping[str, Any]) -> list[str]:
    mutations, _, ambiguous = _browser_surface_for_code(str(block.get("code") or ""))
    return sorted({f"{mutation.receiver}.{mutation.method}" for mutation in mutations} | set(ambiguous))


def _workflow_string_scalars(node: Any, *, excluded: Mapping[str, Any]) -> Iterator[str]:
    if node is excluded:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _workflow_string_scalars(value, excluded=excluded)
    elif isinstance(node, list):
        for item in node:
            yield from _workflow_string_scalars(item, excluded=excluded)


def _code_block_output_is_referenced(parsed: Mapping[str, Any], block: Mapping[str, Any]) -> bool:
    label = str(block.get("label") or "").strip()
    if not label:
        return False
    output_key = f"{label}_output"
    return any(output_key in text for text in _workflow_string_scalars(parsed, excluded=block))


def _remove_top_level_code_blocks(parsed: dict[str, Any], removed: list[dict[str, Any]]) -> bool:
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return False
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return False
    if any(not any(existing is block for existing in blocks) for block in removed):
        return False
    definition["blocks"] = [block for block in blocks if all(block is not stale for stale in removed)]
    return True


def _drop_stale_spine_rung_blocks(
    parsed: dict[str, Any],
    stale_rungs: list[dict[str, Any]],
    *,
    carrier_label: str,
    provenance: list[_BrowserSurfaceRejectionProvenance],
) -> _StaleSpineRungReplacement:
    definition = parsed.get("workflow_definition")
    all_blocks = definition.get("blocks") if isinstance(definition, dict) else None
    surviving_blocks = [
        block
        for block in (all_blocks if isinstance(all_blocks, list) else [])
        if all(block is not stale for stale in stale_rungs)
    ]
    # A stale rung that a surviving block still points at via next_block_label/branch cannot be dropped:
    # removing it would manufacture the dangling reference that _dangling_next_block_label_violation rejects.
    # References from the stale set itself do not count (those pointers leave with the dropped blocks).
    _, surviving_references = _workflow_block_graph({"workflow_definition": {"blocks": surviving_blocks}})
    graph_referenced_labels = set(surviving_references)
    undroppable = [
        block
        for block in stale_rungs
        if _code_block_output_is_referenced(parsed, block)
        or str(block.get("label") or "").strip() in graph_referenced_labels
    ]
    if not undroppable and _remove_top_level_code_blocks(parsed, stale_rungs):
        replaced_labels = [_code_block_label(block) for block in stale_rungs]
        LOG.info(
            "copilot_spine_stale_rung_dropped",
            carrier_label=carrier_label,
            dropped_labels=replaced_labels,
            dropped_actions=[action for block in stale_rungs for action in _code_block_browser_actions(block)],
            dropped_provenance=[record._asdict() for record in provenance],
        )
        return _StaleSpineRungReplacement(violation=None, replaced_labels=replaced_labels)
    blocked = ", ".join(f"`{_code_block_label(block)}`" for block in (undroppable or stale_rungs))
    violation = (
        f"Unable to impose synthesized code block: the scouted spine belongs on `{carrier_label}`, but {blocked} "
        f"still drive(s) the browser and cannot be removed automatically. Delete those block(s) and resubmit with "
        f"the browser spine on a single code block. {_ADMISSIBLE_BROWSER_COVERAGE_ROUTE}"
        f"{_provenance_suffix_text(provenance)}"
    )
    return _StaleSpineRungReplacement(violation=violation, replaced_labels=[])


def _locator_provenance_is_self_validating(provenance: Mapping[str, Any]) -> bool:
    source = provenance.get("source")
    if source == "selector":
        return provenance.get("selector") == provenance.get("emitted_literal")
    if source == "aria_role_name":
        role = str(provenance.get("role") or "")
        name = str(provenance.get("name") or "")
        return bool(role) and bool(name) and _get_by_role_expr_strict(role, name) == provenance.get("emitted_literal")
    if source == INPUT_TEMPLATED_PROVENANCE_SOURCE:
        surface = str(provenance.get("surface") or "")
        input_holes = provenance.get("holes")
        if not isinstance(input_holes, list) or not input_holes:
            return False
        selector = str(provenance.get("selector") or "")
        row_text = ""
        if surface == "row_text":
            source_url = provenance.get("source_url")
            target_selector = provenance.get("target_selector")
            row_selector = provenance.get("row_selector")
            row_text_value = provenance.get("row_text")
            row_selector_count = provenance.get("row_selector_count")
            row_text_match_count = provenance.get("row_text_match_count")
            period_matches = provenance.get("period_matches")
            validated_period_matches = (
                validated_dynamic_row_period_matches(period_matches, row_selector_count)
                if isinstance(row_selector_count, int) and not isinstance(row_selector_count, bool)
                else None
            )
            selected_index = provenance.get("selected_index")
            evidence_fingerprint = provenance.get("evidence_fingerprint")
            if (
                not isinstance(source_url, str)
                or not source_url.strip()
                or not isinstance(target_selector, str)
                or not target_selector.strip()
                or not isinstance(row_selector, str)
                or not row_selector.strip()
                or not isinstance(row_text_value, str)
                or not row_text_value.strip()
                or isinstance(row_selector_count, bool)
                or not isinstance(row_selector_count, int)
                or row_selector_count < 2
                or row_selector_count > 100
                or isinstance(row_text_match_count, bool)
                or not isinstance(row_text_match_count, int)
                or row_text_match_count < 1
                or row_text_match_count > row_selector_count
                or validated_period_matches is None
                or not dynamic_row_period_matches_match_selected_row(row_text_value, validated_period_matches)
                or isinstance(selected_index, bool)
                or not isinstance(selected_index, int)
                or selected_index < 0
                or selected_index >= row_selector_count
                or not isinstance(evidence_fingerprint, str)
                or evidence_fingerprint
                != dynamic_row_evidence_fingerprint(
                    source_url=source_url,
                    target_selector=target_selector,
                    row_selector=row_selector,
                    row_text=row_text_value,
                    row_selector_count=row_selector_count,
                    row_text_match_count=row_text_match_count,
                    period_matches=validated_period_matches,
                    selected_index=selected_index,
                )
            ):
                return False
            selector = row_selector
            row_text = row_text_value
        recomputed = build_input_templated_locator(
            surface=surface,
            selector=selector,
            role=str(provenance.get("role") or ""),
            name=str(provenance.get("name") or ""),
            holes=input_holes,
            row_text=row_text,
            period_matches=validated_period_matches if surface == "row_text" and validated_period_matches else (),
        )
        return recomputed is not None and recomputed == provenance.get("emitted_literal")
    if source == SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE:
        raw_date_keys = provenance.get("date_keys")
        raw_expected_declared_keys = provenance.get("expected_declared_keys")
        raw_holes = provenance.get("holes")
        date_format_id = provenance.get("date_format_id")
        provenance_fingerprint = provenance.get("provenance_fingerprint")
        if (
            not isinstance(raw_date_keys, list)
            or len(raw_date_keys) != 2
            or any(not isinstance(key, str) for key in raw_date_keys)
            or not isinstance(raw_expected_declared_keys, list)
            or any(not isinstance(key, str) for key in raw_expected_declared_keys)
            or date_format_id != "iso_date_to_year_month"
            or not isinstance(provenance_fingerprint, str)
            or not isinstance(raw_holes, list)
        ):
            return False
        same_month_holes: list[SameMonthFileMatchHole] = []
        for raw_hole in raw_holes:
            if not isinstance(raw_hole, Mapping):
                return False
            declared_keys = raw_hole.get("declared_keys")
            matched_literal = raw_hole.get("matched_literal")
            position = raw_hole.get("position")
            format_id = raw_hole.get("format_id")
            source_values = raw_hole.get("source_values")
            if (
                not isinstance(declared_keys, list)
                or any(not isinstance(key, str) for key in declared_keys)
                or not isinstance(matched_literal, str)
                or not isinstance(position, int)
                or format_id not in {"identity", "iso_date_to_year_month"}
                or not isinstance(source_values, list)
                or any(not isinstance(value, str) for value in source_values)
            ):
                return False
            same_month_holes.append(
                SameMonthFileMatchHole(
                    declared_keys=tuple(declared_keys),
                    matched_literal=matched_literal,
                    position=position,
                    format_id=cast(SameMonthFileMatchFormat, format_id),
                    source_values=tuple(source_values),
                )
            )
        transform = SameMonthFileMatchTransform(
            selector=str(provenance.get("selector") or ""),
            holes=tuple(same_month_holes),
            date_keys=(raw_date_keys[0], raw_date_keys[1]),
            expected_declared_keys=tuple(raw_expected_declared_keys),
            provenance_fingerprint=provenance_fingerprint,
        )
        recomputed = build_same_month_file_match_locator(transform, transform.selector)
        return recomputed is not None and recomputed == provenance.get("emitted_literal")
    return False


def _public_locator_provenance(provenance_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    public_rows: list[dict[str, Any]] = []
    for provenance in provenance_rows:
        if provenance.get("surface") != "row_text":
            public_rows.append(dict(provenance))
            continue
        holes = provenance.get("holes")
        input_keys: set[str] = set()
        transforms: set[str] = set()
        if isinstance(holes, list):
            for hole in holes:
                if not isinstance(hole, Mapping):
                    continue
                inputs = [hole]
                equivalents = hole.get("equivalent_inputs")
                if isinstance(equivalents, list):
                    inputs.extend(item for item in equivalents if isinstance(item, Mapping))
                for witness in inputs:
                    key = str(witness.get("input_key") or "")
                    transform = str(witness.get("transform") or "")
                    if key:
                        input_keys.add(key)
                    if transform:
                        transforms.add(transform)
        source_url = str(provenance.get("source_url") or "")
        public_rows.append(
            {
                "trajectory_index": provenance.get("trajectory_index"),
                "source": str(provenance.get("source") or ""),
                "surface": "row_text",
                "source_origin": url_origin(source_url) if source_url else None,
                "input_keys": sorted(input_keys),
                "transforms": sorted(transforms),
            }
        )
    return public_rows


_PAGE_MUTATION_METHODS = frozenset(
    {
        "goto",
        "reload",
        "go_back",
        "go_forward",
        "set_content",
    }
)
_LOCATOR_MUTATION_METHODS = frozenset(
    {
        "check",
        "click",
        "dblclick",
        "dispatch_event",
        "drag_to",
        "fill",
        "focus",
        "hover",
        "press",
        "select_option",
        "set_checked",
        "tap",
        "type",
        "uncheck",
    }
)
_PAGE_FACTORY_METHODS = frozenset({"frame_locator", "get_by_role", "locator"})
_PAGE_READ_METHODS = frozenset(
    {
        "get_attribute",
        "inner_html",
        "inner_text",
        "input_value",
        "is_checked",
        "is_disabled",
        "is_editable",
        "is_enabled",
        "is_hidden",
        "is_visible",
        "text_content",
        "wait_for_load_state",
    }
)
_LOCATOR_READ_METHODS = frozenset(
    {
        "all_inner_texts",
        "all_text_contents",
        "count",
        "get_attribute",
        "inner_html",
        "inner_text",
        "input_value",
        "is_checked",
        "is_disabled",
        "is_editable",
        "is_enabled",
        "is_hidden",
        "is_visible",
        "text_content",
        "wait_for",
    }
)
_VALUE_BEARING_READ_METHODS = frozenset(
    {
        "all_inner_texts",
        "all_text_contents",
        "extract",
        "get_attribute",
        "inner_html",
        "inner_text",
        "input_value",
        "text_content",
    }
)


class _BrowserMutationSignature(NamedTuple):
    method: str
    receiver: str
    call_shape: str
    argument_literal: str | None
    generator_compatible: bool


_BrowserSurfaceProvenanceKind = Literal["never_captured", "shape_diverged", "ambiguous", "suffix_disallowed"]
_BrowserSurfaceDivergenceSource = Literal["synthesized", "trajectory_dropped"]
_BrowserSurfaceProvenanceSite = Literal["whole_trajectory", "extraction_suffix", "fragment_scout"]

_BROWSER_SURFACE_PROVENANCE_EVENT = "copilot_browser_surface_rejection_provenance"


class _BrowserSurfaceRejectionProvenance(NamedTuple):
    kind: _BrowserSurfaceProvenanceKind
    action: str
    site: _BrowserSurfaceProvenanceSite
    block_label: str
    nearest_method: str | None = None
    nearest_receiver: str | None = None
    nearest_selector: str | None = None
    divergence_source: _BrowserSurfaceDivergenceSource | None = None
    mutation: _BrowserMutationSignature | None = None


class _BrowserSurfaceValidation(NamedTuple):
    violations: list[str]
    provenance: list[_BrowserSurfaceRejectionProvenance]


_NEVER_CAPTURED_SCOUT_TOOL_BY_METHOD = {
    "click": "click",
    "fill": "type_text",
    "press": "press_key",
    "select_option": "select_option",
}


def _never_captured_obligation_candidate(
    provenance: Iterable[_BrowserSurfaceRejectionProvenance],
) -> _NeverCapturedObligationCandidate | None:
    for record in provenance:
        mutation = record.mutation
        if record.kind != "never_captured" or mutation is None:
            continue
        expected_tool_name = _NEVER_CAPTURED_SCOUT_TOOL_BY_METHOD.get(mutation.method)
        if expected_tool_name is None or not mutation.generator_compatible:
            continue
        if mutation.method == "press" and mutation.argument_literal is None:
            continue
        return _NeverCapturedObligationCandidate(
            block_label=record.block_label,
            site=record.site,
            method=mutation.method,
            receiver=normalized_locator_expr(mutation.receiver),
            call_shape=mutation.call_shape,
            expected_tool_name=expected_tool_name,
            expected_argument_literal=mutation.argument_literal,
        )
    return None


def _arm_never_captured_obligation(
    ctx: AgentContext,
    workflow_yaml: str,
    candidate: _NeverCapturedObligationCandidate | None,
    replay_payload: NeverCapturedReplayPayload,
) -> None:
    if candidate is None:
        return
    turn_id = str(getattr(ctx, "turn_id", ""))
    draft_fingerprint = hashlib.sha256(workflow_yaml.encode()).hexdigest()
    call_shape_digest = hashlib.sha256(candidate.call_shape.encode()).hexdigest()
    identity_payload = "\0".join(
        (
            turn_id,
            draft_fingerprint,
            candidate.block_label,
            candidate.site,
            candidate.method,
            candidate.receiver,
            call_shape_digest,
        )
    )
    identity_digest = hashlib.sha256(identity_payload.encode()).hexdigest()
    if identity_digest in ctx.never_captured_obligation_identity_history:
        return
    armed_after_trajectory_index = max(
        (index for item in ctx.scout_trajectory if isinstance((index := item.get("trajectory_index")), int)),
        default=-1,
    )
    ctx.never_captured_obligation = NeverCapturedObligation(
        identity_digest=identity_digest,
        turn_id=turn_id,
        draft_fingerprint=draft_fingerprint,
        block_label=candidate.block_label,
        site=candidate.site,
        method=candidate.method,
        normalized_receiver=candidate.receiver,
        call_shape_digest=call_shape_digest,
        expected_tool_name=candidate.expected_tool_name,
        armed_after_trajectory_index=armed_after_trajectory_index,
        expected_argument_literal=candidate.expected_argument_literal,
        replay_payload=replay_payload,
    )
    ctx.never_captured_obligation_identity_history.add(identity_digest)
    LOG.info(
        "copilot_never_captured_obligation_armed",
        identity_digest=identity_digest,
        turn_id=turn_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
        draft_fingerprint=draft_fingerprint,
        block_label=candidate.block_label,
        site=candidate.site,
        method=candidate.method,
        receiver=candidate.receiver,
        call_shape_digest=call_shape_digest,
        expected_tool_name=candidate.expected_tool_name,
        armed_after_trajectory_index=armed_after_trajectory_index,
    )


class _BrowserBindings(NamedTuple):
    page_aliases: set[str]
    locator_aliases: set[str]
    method_aliases: set[str]


class _BrowserExpressionKind(StrEnum):
    LOCATOR = "locator"
    SCALAR = "scalar"
    OTHER = "other"


def _code_block_label(block: Mapping[str, Any]) -> str:
    return str(block.get("label") or "").strip() or "unlabeled code block"


def _call_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def _locator_receiver_for_signature(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Attribute) and node.attr in {"first", "last"}:
        node = node.value
    return node


def _direct_locator_receiver_signature(node: ast.AST) -> str | None:
    receiver = _locator_receiver_for_signature(node)
    if _is_page_locator_expression(receiver):
        return _call_name(receiver)
    return None


# page.get_by_label(...) is a durable locator factory strict synthesis never emits, so it is scanned
# as a durable mutation receiver only to route it through the admissibility contract, never to admit it.
_DURABLE_NON_SYNTHESIZED_FACTORY_METHODS = frozenset({"get_by_label"})


def _durable_non_synthesized_receiver_signature(node: ast.AST) -> str | None:
    receiver = _locator_receiver_for_signature(node)
    if (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Attribute)
        and receiver.func.attr in _DURABLE_NON_SYNTHESIZED_FACTORY_METHODS
        and isinstance(receiver.func.value, ast.Name)
        and receiver.func.value.id == "page"
    ):
        return _call_name(receiver)
    return None


def _receiver_is_durable_non_synthesized_factory(receiver: str) -> bool:
    try:
        node = ast.parse(receiver, mode="eval").body
    except SyntaxError:
        return False
    return _durable_non_synthesized_receiver_signature(node) is not None


def _direct_page_method_signature(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Name) or node.id != "page":
        return None
    return "page"


def _browser_mutation_argument_literal(node: ast.Call) -> str | None:
    if not node.args:
        return None
    value = node.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _browser_mutation_is_generator_compatible(node: ast.Call, method: str) -> bool:
    if node.keywords:
        return False
    if method == "click":
        return not node.args
    if method in {"fill", "press", "select_option"}:
        return len(node.args) == 1
    return False


def _bounded_nth_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and 0 <= node.value <= 10_000
    )


def _browser_mutation_signature_for_call(node: ast.Call) -> _BrowserMutationSignature | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _LOCATOR_MUTATION_METHODS:
        receiver = _direct_locator_receiver_signature(func.value) or _durable_non_synthesized_receiver_signature(
            func.value
        )
        if receiver is not None:
            return _BrowserMutationSignature(
                func.attr,
                receiver,
                ast.dump(node, include_attributes=False),
                _browser_mutation_argument_literal(node),
                _browser_mutation_is_generator_compatible(node, func.attr),
            )
        return None
    if func.attr in _PAGE_MUTATION_METHODS:
        receiver = _direct_page_method_signature(func.value)
        if receiver is not None:
            return _BrowserMutationSignature(
                func.attr,
                receiver,
                ast.dump(node, include_attributes=False),
                _browser_mutation_argument_literal(node),
                False,
            )
    return None


def _assigned_value_is_page(value: ast.AST, page_aliases: set[str]) -> bool:
    return isinstance(value, ast.Name) and (value.id == "page" or value.id in page_aliases)


def _collect_page_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _assigned_value_is_page(node.value, aliases):
                for target in node.targets:
                    before = len(aliases)
                    aliases.update(_target_names(target))
                    changed = changed or len(aliases) != before
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and _assigned_value_is_page(node.value, aliases)
            ):
                before = len(aliases)
                aliases.update(_target_names(node.target))
                changed = changed or len(aliases) != before
            elif isinstance(node, ast.NamedExpr) and _assigned_value_is_page(node.value, aliases):
                before = len(aliases)
                aliases.update(_target_names(node.target))
                changed = changed or len(aliases) != before
    return aliases


def _assigned_value_is_locator(value: ast.AST, locator_aliases: set[str]) -> bool:
    return _direct_locator_receiver_signature(value) is not None or (
        isinstance(value, ast.Name) and value.id in locator_aliases
    )


def _collect_locator_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _assigned_value_is_locator(node.value, aliases):
                for target in node.targets:
                    before = len(aliases)
                    aliases.update(_target_names(target))
                    changed = changed or len(aliases) != before
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and _assigned_value_is_locator(node.value, aliases)
            ):
                before = len(aliases)
                aliases.update(_target_names(node.target))
                changed = changed or len(aliases) != before
            elif isinstance(node, ast.NamedExpr) and _assigned_value_is_locator(node.value, aliases):
                before = len(aliases)
                aliases.update(_target_names(node.target))
                changed = changed or len(aliases) != before
    return aliases


def _expr_contains_browser_receiver(node: ast.AST, bindings: _BrowserBindings) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "page" or node.id in bindings.page_aliases or node.id in bindings.locator_aliases
    if _direct_locator_receiver_signature(node) is not None:
        return True
    if isinstance(node, ast.Attribute):
        return _expr_contains_browser_receiver(node.value, bindings)
    return any(_expr_contains_browser_receiver(child, bindings) for child in ast.iter_child_nodes(node))


def _collect_browser_method_aliases(tree: ast.AST, page_aliases: set[str], locator_aliases: set[str]) -> set[str]:
    aliases: set[str] = set()
    bindings = _BrowserBindings(page_aliases, locator_aliases, set())
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value: ast.AST | None = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                value = node.value
                targets.extend(node.targets)
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                targets.append(node.target)
            elif isinstance(node, ast.NamedExpr):
                value = node.value
                targets.append(node.target)
            if (isinstance(value, ast.Attribute) and _expr_contains_browser_receiver(value.value, bindings)) or (
                isinstance(value, ast.Name) and value.id in aliases
            ):
                for target in targets:
                    before = len(aliases)
                    aliases.update(_target_names(target))
                    changed = changed or len(aliases) != before
    return aliases


def _is_dynamic_browser_dispatch(node: ast.Call, bindings: _BrowserBindings) -> bool:
    func = node.func
    if not isinstance(func, ast.Call):
        return False
    if not isinstance(func.func, ast.Name) or func.func.id != "getattr" or not func.args:
        return False
    return _expr_contains_browser_receiver(func.args[0], bindings)


def _browser_expression_kind(node: ast.AST, bindings: _BrowserBindings) -> _BrowserExpressionKind:
    # Alias trust is intentionally limited to compiler-owned scout variables;
    # submitted-code aliases never gain browser-read authority by assignment.
    if isinstance(node, ast.Name) and node.id in bindings.locator_aliases and node.id in _INTERNAL_SCOUT_VARS:
        return _BrowserExpressionKind.LOCATOR
    if _direct_locator_receiver_signature(node) is not None:
        return _BrowserExpressionKind.LOCATOR
    if _durable_non_synthesized_receiver_signature(node) is not None:
        return _BrowserExpressionKind.LOCATOR
    if isinstance(node, ast.Attribute) and node.attr in {"first", "last"}:
        if _browser_expression_kind(node.value, bindings) == _BrowserExpressionKind.LOCATOR:
            return _BrowserExpressionKind.LOCATOR
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver_kind = _browser_expression_kind(node.func.value, bindings)
        if (
            node.func.attr in {"locator", "get_by_role", "frame_locator"}
            and receiver_kind == _BrowserExpressionKind.LOCATOR
        ):
            return _BrowserExpressionKind.LOCATOR
        if node.func.attr == "nth" and receiver_kind == _BrowserExpressionKind.LOCATOR and len(node.args) == 1:
            index = node.args[0]
            if _bounded_nth_constant(index):
                return _BrowserExpressionKind.LOCATOR
        if node.func.attr in _LOCATOR_READ_METHODS and receiver_kind == _BrowserExpressionKind.LOCATOR:
            return _BrowserExpressionKind.SCALAR
        if (
            node.func.attr in _PAGE_READ_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "page"
        ):
            return _BrowserExpressionKind.SCALAR
        if (
            node.func.attr
            in {
                "casefold",
                "endswith",
                "join",
                "lower",
                "lstrip",
                "replace",
                "rsplit",
                "rstrip",
                "split",
                "startswith",
                "strip",
                "upper",
            }
            and receiver_kind == _BrowserExpressionKind.SCALAR
        ):
            return _BrowserExpressionKind.SCALAR
    if isinstance(node, ast.Await):
        return _browser_expression_kind(node.value, bindings)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {
            "all",
            "any",
            "bool",
            "dict",
            "enumerate",
            "float",
            "int",
            "len",
            "list",
            "max",
            "min",
            "range",
            "set",
            "sorted",
            "str",
            "sum",
            "tuple",
        }
    ):
        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        kinds = [_browser_expression_kind(value, bindings) for value in values]
        if (
            any(kind == _BrowserExpressionKind.SCALAR for kind in kinds)
            and all(kind != _BrowserExpressionKind.LOCATOR for kind in kinds)
            and all(
                kind == _BrowserExpressionKind.SCALAR or not _expr_contains_browser_receiver(value, bindings)
                for value, kind in zip(values, kinds, strict=True)
            )
        ):
            return _BrowserExpressionKind.SCALAR
    return _BrowserExpressionKind.OTHER


def _is_allowed_browser_read_call(node: ast.Call, bindings: _BrowserBindings) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _PAGE_FACTORY_METHODS:
        return _browser_expression_kind(node, bindings) == _BrowserExpressionKind.LOCATOR
    if func.attr in _PAGE_READ_METHODS and isinstance(func.value, ast.Name) and func.value.id == "page":
        return True
    if func.attr in _LOCATOR_READ_METHODS:
        return _browser_expression_kind(func.value, bindings) == _BrowserExpressionKind.LOCATOR
    return False


def _browser_surface_for_code(code: str) -> tuple[list[_BrowserMutationSignature], list[str], list[str]]:
    tree = _wrapped_code_ast(code)
    if tree is None:
        return [], [], []
    direct_mutations: list[_BrowserMutationSignature] = []
    unscouted: list[str] = []
    ambiguous: list[str] = []
    page_aliases = _collect_page_aliases(tree)
    locator_aliases = _collect_locator_aliases(tree)
    method_aliases = _collect_browser_method_aliases(tree, page_aliases, locator_aliases)
    bindings = _BrowserBindings(
        page_aliases,
        locator_aliases,
        method_aliases,
    )
    # ast.walk yields breadth-first; the coverage scan (uncovered_required_emitted_interactions)
    # matches draft calls as an ordered subsequence, so enumerate Call nodes in source order or a
    # nested-but-present rung is falsely reported uncovered.
    call_nodes = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in call_nodes:
        if isinstance(node.func, ast.Name) and node.func.id in bindings.method_aliases:
            ambiguous.append(_call_name(node))
            continue
        if isinstance(node.func, ast.Name) and node.func.id in bindings.method_aliases:
            ambiguous.append(_call_name(node))
            continue
        signature = _browser_mutation_signature_for_call(node)
        if signature is not None:
            direct_mutations.append(signature)
            continue
        if _is_dynamic_browser_dispatch(node, bindings):
            ambiguous.append(_call_name(node))
            continue
        if _browser_expression_kind(node, bindings) in {
            _BrowserExpressionKind.LOCATOR,
            _BrowserExpressionKind.SCALAR,
        }:
            continue
        if _is_allowed_browser_read_call(node, bindings):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if _expr_contains_browser_receiver(func.value, bindings):
                ambiguous.append(_call_name(node))
        elif isinstance(func, ast.Name) and (
            any(_expr_contains_browser_receiver(arg, bindings) for arg in node.args)
            or any(_expr_contains_browser_receiver(keyword.value, bindings) for keyword in node.keywords)
        ):
            ambiguous.append(_call_name(node))
    for signature in direct_mutations:
        unscouted.append(f"{signature.receiver}.{signature.method}")
    return direct_mutations, sorted(unscouted), sorted(set(ambiguous))


_SCOUT_TOOL_EMITTED_METHODS = {
    "click": "click",
    "type_text": "fill",
    "press_key": "press",
    "select_option": "select_option",
    CREDENTIAL_FILL_TOOL_NAME: "fill",
}


def _captured_selector_for_signature(
    signature: _BrowserMutationSignature, diagnostics: SynthesisDiagnostics | None
) -> str | None:
    if diagnostics is None:
        return None
    # Signature receivers come from ast.unparse while emitted locators are synthesizer text, so both
    # sides are normalized before comparison.
    receiver = normalized_locator_expr(signature.receiver)
    for record in diagnostics.emitted_interactions:
        if (
            str(record.get("method") or "") == signature.method
            and normalized_locator_expr(str(record.get("locator") or "")) == receiver
        ):
            selector = str(record.get("selector") or "")
            if selector:
                return selector
    return None


def _classify_unscouted_mutation(
    mutation: _BrowserMutationSignature,
    *,
    scouted_mutations: list[_BrowserMutationSignature],
    diagnostics: SynthesisDiagnostics | None,
    site: _BrowserSurfaceProvenanceSite,
    block_label: str,
) -> _BrowserSurfaceRejectionProvenance:
    action = f"{mutation.receiver}.{mutation.method}"
    exact = next(
        (
            signature
            for signature in scouted_mutations
            if signature.method == mutation.method and signature.receiver == mutation.receiver
        ),
        None,
    )
    if exact is not None:
        return _BrowserSurfaceRejectionProvenance(
            kind="shape_diverged",
            action=action,
            site=site,
            block_label=block_label,
            nearest_method=exact.method,
            nearest_receiver=exact.receiver,
            nearest_selector=_captured_selector_for_signature(exact, diagnostics),
            divergence_source="synthesized",
            mutation=mutation,
        )
    receiver_literals = locator_selector_literals(mutation.receiver)
    emitted_records = diagnostics.emitted_interactions if diagnostics is not None else []
    for record in emitted_records:
        selector = str(record.get("selector") or "")
        if str(record.get("method") or "") == mutation.method and selector and selector in receiver_literals:
            return _BrowserSurfaceRejectionProvenance(
                kind="shape_diverged",
                action=action,
                site=site,
                block_label=block_label,
                nearest_method=mutation.method,
                nearest_receiver=str(record.get("locator") or "") or None,
                nearest_selector=selector,
                divergence_source="synthesized",
                mutation=mutation,
            )
    dropped_records = diagnostics.dropped_interactions if diagnostics is not None else []
    for record in dropped_records:
        if _SCOUT_TOOL_EMITTED_METHODS.get(str(record.get("tool_name") or "")) != mutation.method:
            continue
        selector = str(record.get("selector") or "")
        if selector and selector not in receiver_literals:
            continue
        return _BrowserSurfaceRejectionProvenance(
            kind="shape_diverged",
            action=action,
            site=site,
            block_label=block_label,
            nearest_method=mutation.method,
            nearest_selector=selector or None,
            divergence_source="trajectory_dropped",
            mutation=mutation,
        )
    nearest = next((signature for signature in scouted_mutations if signature.method == mutation.method), None) or next(
        (signature for signature in scouted_mutations if signature.receiver == mutation.receiver), None
    )
    return _BrowserSurfaceRejectionProvenance(
        kind="never_captured",
        action=action,
        site=site,
        block_label=block_label,
        nearest_method=nearest.method if nearest is not None else None,
        nearest_receiver=nearest.receiver if nearest is not None else None,
        nearest_selector=_captured_selector_for_signature(nearest, diagnostics) if nearest is not None else None,
        mutation=mutation,
    )


def _ambiguous_browser_action_provenance(
    action: str, *, site: _BrowserSurfaceProvenanceSite, block_label: str
) -> _BrowserSurfaceRejectionProvenance:
    return _BrowserSurfaceRejectionProvenance(kind="ambiguous", action=action, site=site, block_label=block_label)


def _provenance_reject_clause(provenance: _BrowserSurfaceRejectionProvenance) -> str:
    if provenance.kind == "never_captured":
        clause = f"`{provenance.action}`: never_captured — the scout never captured this action; re-scout that step"
        if provenance.nearest_receiver is not None and provenance.nearest_method is not None:
            clause += f" (nearest scouted signature: `{provenance.nearest_receiver}.{provenance.nearest_method}`)"
        return clause
    if provenance.kind == "shape_diverged":
        clause = f"`{provenance.action}`: shape_diverged ({provenance.divergence_source})"
        if provenance.nearest_receiver is not None and provenance.nearest_method is not None:
            clause += (
                f" — reuse the exact synthesized call on `{provenance.nearest_receiver}.{provenance.nearest_method}`"
            )
        else:
            clause += " — reuse the exact synthesized call for this captured step"
        if provenance.nearest_selector is not None:
            clause += f" (captured selector {provenance.nearest_selector!r})"
        return clause
    if provenance.kind == "suffix_disallowed":
        return (
            f"`{provenance.action}`: suffix_disallowed — the synthesized spine already performs this action; "
            "remove the duplicate from the extraction suffix"
        )
    return (
        f"`{provenance.action}`: ambiguous — rewrite it as a direct page/locator call "
        "or reuse the exact synthesized call"
    )


def _log_browser_surface_rejection_provenance(provenance: list[_BrowserSurfaceRejectionProvenance]) -> None:
    for record in provenance:
        LOG.info(
            _BROWSER_SURFACE_PROVENANCE_EVENT,
            kind=record.kind,
            action=record.action,
            site=record.site,
            block_label=record.block_label,
            nearest_method=record.nearest_method,
            nearest_receiver=record.nearest_receiver,
            nearest_selector=record.nearest_selector,
            divergence_source=record.divergence_source,
        )


def _provenance_suffix_text(provenance: list[_BrowserSurfaceRejectionProvenance]) -> str:
    if not provenance:
        return ""
    return " Provenance: " + "; ".join(_provenance_reject_clause(record) for record in provenance) + "."


def _whole_trajectory_browser_surface_violations(
    *,
    code_blocks: list[dict[str, Any]],
    selected_code_block: dict[str, Any],
    submitted_selected_code: str,
    synthesized_code: str,
    prior_yaml: str | None = None,
    synthesized_diagnostics: SynthesisDiagnostics | None = None,
    admit_on_receiver: bool = False,
    restrict_to_durable_factory: bool = False,
) -> _BrowserSurfaceValidation:
    violations: list[str] = []
    provenance: list[_BrowserSurfaceRejectionProvenance] = []
    scouted_mutations, _, _ = _browser_surface_for_code(synthesized_code)
    scouted_signatures = set(scouted_mutations)
    # Freehand admission is selector-resolvability, so a block that replays a scouted locator with a
    # different literal argument stays admissible; imposition siblings keep the stricter full-call match.
    admitted_receivers = {
        (mutation.method, normalized_locator_expr(mutation.receiver)) for mutation in scouted_mutations
    }
    for block in code_blocks:
        if block is selected_code_block:
            continue
        if prior_yaml is not None and not _submitted_code_block_changed(block, prior_yaml):
            continue
        label = _code_block_label(block)
        block_code = str(block.get("code") or "")
        block_mutations, _, block_ambiguous = _browser_surface_for_code(block_code)
        if admit_on_receiver:
            unscouted_mutations = [
                mutation
                for mutation in block_mutations
                if mutation.receiver != "page"
                and (mutation.method, normalized_locator_expr(mutation.receiver)) not in admitted_receivers
            ]
        else:
            unscouted_mutations = [mutation for mutation in block_mutations if mutation not in scouted_signatures]
        if restrict_to_durable_factory:
            unscouted_mutations = [
                mutation
                for mutation in unscouted_mutations
                if _receiver_is_durable_non_synthesized_factory(mutation.receiver)
            ]
        if unscouted_mutations:
            action_text = ", ".join(
                f"{mutation.receiver}.{mutation.method}" for mutation in sorted(unscouted_mutations)
            )
            block_provenance = [
                _classify_unscouted_mutation(
                    mutation,
                    scouted_mutations=scouted_mutations,
                    diagnostics=synthesized_diagnostics,
                    site="whole_trajectory",
                    block_label=label,
                )
                for mutation in sorted(unscouted_mutations)
            ]
            provenance.extend(block_provenance)
            violations.append(
                f"Unable to impose synthesized code block: `{label}` contains unscouted browser action(s): "
                f"{action_text}.{_provenance_suffix_text(block_provenance)}"
            )
        if block_ambiguous and not restrict_to_durable_factory:
            ambiguous_provenance = [
                _ambiguous_browser_action_provenance(action, site="whole_trajectory", block_label=label)
                for action in block_ambiguous
            ]
            provenance.extend(ambiguous_provenance)
            violations.append(
                f"Unable to impose synthesized code block: `{label}` contains ambiguous browser action(s): "
                + ", ".join(block_ambiguous)
                + "."
                + _provenance_suffix_text(ambiguous_provenance)
            )
    _log_browser_surface_rejection_provenance(provenance)
    return _BrowserSurfaceValidation(violations, provenance)


def _separated_spine_already_imposed(
    code_blocks: list[dict[str, Any]],
    selected_code_block: dict[str, Any],
    synthesized_code: str,
) -> bool:
    scouted_mutations, _, _ = _browser_surface_for_code(synthesized_code)
    if not scouted_mutations:
        return False
    selected_mutations, _, _ = _browser_surface_for_code(str(selected_code_block.get("code") or ""))
    if selected_mutations:
        return False
    sibling_signatures: set[_BrowserMutationSignature] = set()
    for block in code_blocks:
        if block is selected_code_block:
            continue
        block_mutations, _, _ = _browser_surface_for_code(str(block.get("code") or ""))
        sibling_signatures.update(block_mutations)
    already_imposed = sibling_signatures == set(scouted_mutations)
    if already_imposed:
        LOG.info(
            "copilot_separated_spine_fast_path",
            spine_coverage="set_equality",
            synthesized_mutation_count=len(scouted_mutations),
            sibling_signature_count=len(sibling_signatures),
            duplicate_rungs_lost=len(scouted_mutations) != len(set(scouted_mutations)),
        )
    return already_imposed


def _browser_surface_contains_full_action_spine(submitted_code: str, synthesized_code: str) -> bool:
    synthesized_mutations, _, _ = _browser_surface_for_code(synthesized_code)
    submitted_mutations, _, submitted_ambiguous = _browser_surface_for_code(submitted_code)
    if submitted_ambiguous:
        return False
    if not synthesized_mutations:
        return False
    submitted_iter = iter(submitted_mutations)
    return all(any(candidate == synthesized for candidate in submitted_iter) for synthesized in synthesized_mutations)


_SCOUTED_SPINE_UNDER_BUILD_REASON_CODE = SCOUTED_SPINE_UNDER_BUILD_REASON_CODE
_SCOUTED_SPINE_REASON_CODES = frozenset(
    {
        SCOUTED_SPINE_UNDER_BUILD_REASON_CODE,
        SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE,
        SCOUTED_SPINE_UNRECORDED_INDEX_REASON_CODE,
        SCOUTED_SPINE_TRUNCATED_REASON_CODE,
    }
)


def _synthesized_resubmission_credential_scout_requirements(
    ctx: AgentContext, synthesized: SynthesizedCodeBlock, *, block_label: str
) -> list[str]:
    # Runs the credential-scout gate against the verbatim resubmission the under-build reject asks
    # for, so the reject names every step of a route that is admissible end-to-end.
    credential_parameters = [
        parameter for parameter in synthesized.parameters if str(parameter.get("credential_id") or "").strip()
    ]
    if not credential_parameters:
        return []
    probe_yaml = yaml.safe_dump(
        {
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": str(parameter.get("key") or ""),
                        "credential_id": str(parameter.get("credential_id") or ""),
                    }
                    for parameter in credential_parameters
                ],
                "blocks": [{"block_type": "code", "label": block_label, "code": synthesized.code}],
            }
        },
        sort_keys=False,
    )
    return _credentialed_code_block_scout_gate_errors(probe_yaml, ctx)


def _record_scouted_spine_omission_reject(ctx: AgentContext, digest: str | None) -> None:
    if digest is not None and digest == ctx.scouted_spine_previous_omission_digest:
        ctx.scouted_spine_repeated_identical_missing_steps = True
        LOG.info("copilot_scouted_spine_repeated_identical_omission", omission_digest=digest)
    else:
        ctx.scouted_spine_repeated_identical_missing_steps = False
    ctx.scouted_spine_previous_omission_digest = digest


def _scouted_spine_omission_digest(records: Sequence[Mapping[str, Any]]) -> str:
    items = sorted(
        (
            str(record.get("tool_name") or ""),
            str(record.get("method") or ""),
            str(record.get("selector") or record.get("locator") or ""),
            str(record.get("trajectory_index")),
        )
        for record in records
    )
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def _scouted_spine_under_build_result(
    workflow_yaml: str,
    *,
    ctx: AgentContext,
    synthesized: SynthesizedCodeBlock,
    draft_codes: list[str],
    block_label: str,
    site: str = "imposition",
    draft_repairable_only: bool = False,
) -> _SynthesizedCodeImpositionResult | None:
    diagnostics = synthesized.diagnostics
    # Lane-flagged emissions (optional dismissals, readonly verifies, entry recovery) are conditional
    # or read-only replays, not load-bearing rungs.
    required = [record for record in diagnostics.emitted_interactions if not str(record.get("lane") or "")]
    draft_calls = [
        (mutation.method, mutation.receiver) for code in draft_codes for mutation in _browser_surface_for_code(code)[0]
    ]
    uncovered = (
        uncovered_required_emitted_interactions(diagnostics.emitted_interactions, draft_calls) if required else []
    )
    if uncovered:
        credential_scout_requirements = _synthesized_resubmission_credential_scout_requirements(
            ctx, synthesized, block_label=block_label
        )
        missing_text = missing_rung_text(uncovered)
        LOG.info(
            "copilot_scouted_spine_under_build",
            block_label=block_label,
            site=site,
            required_rung_count=len(required),
            covered_rung_count=len(required) - len(uncovered),
            missing_rungs=missing_text,
            credential_scout_precondition_pending=bool(credential_scout_requirements),
        )
        first_uncovered = uncovered[0]
        pass_route = render_missing_rung_call_sources(uncovered)
        violation = (
            f"Unable to impose synthesized code block: `{block_label}` under-builds the scouted spine "
            f"({_SCOUTED_SPINE_UNDER_BUILD_REASON_CODE}): the draft covers "
            f"{len(required) - len(uncovered)} of {len(required)} scouted rung(s); missing: {missing_text}. "
        )
        if credential_scout_requirements:
            violation += (
                "Two steps are required, in order. Step 1 — satisfy the credential-scout precondition: "
                + " ".join(credential_scout_requirements)
                + " Step 2 — resubmit the code block, reusing the synthesized rung source verbatim so every "
                "scouted rung is replayed."
            )
        else:
            violation += (
                "Author the remaining synthesized rungs — reuse the synthesized code block verbatim so every "
                "scouted rung is replayed."
            )
        if pass_route:
            violation += "\n" + pass_route
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=[violation],
            repair_context=CodeAuthoringRepairContext(
                block_label=block_label,
                reason_code=_SCOUTED_SPINE_UNDER_BUILD_REASON_CODE,
                selector=str(first_uncovered.get("selector") or "") or None,
            ),
            omission_digest=_scouted_spine_omission_digest(uncovered),
        )
    # Non-uncovered partition findings are synthesizer-side: no draft edit closes them, so a
    # repair-convergence-only site routes them to the turn-end obligation halt instead of a churn loop.
    if draft_repairable_only:
        return None
    partition_findings = [
        finding
        for finding in spine_partition_findings(diagnostics, draft_calls, ctx.scout_trajectory or [])
        if finding.kind != UNCOVERED_RUNG_FINDING
    ]
    if not partition_findings:
        return None
    return _scouted_spine_partition_under_build_result(
        workflow_yaml, findings=partition_findings, block_label=block_label, site=site
    )


def _scouted_spine_partition_under_build_result(
    workflow_yaml: str,
    *,
    findings: list[ObligationFinding],
    block_label: str,
    site: str,
) -> _SynthesizedCodeImpositionResult:
    first = findings[0]
    reason_code = obligation_finding_reason_code(first)
    summary = render_obligation_findings(findings)
    LOG.info(
        "copilot_scouted_spine_under_build",
        block_label=block_label,
        site=site,
        reason_code=reason_code,
        finding_count=len(findings),
        finding_summary=summary,
    )
    violation = (
        f"Unable to impose synthesized code block: `{block_label}` under-builds the scouted spine "
        f"({reason_code}): {summary}. Resubmit the code block reusing the synthesized rung source verbatim so "
        "every captured interaction is replayed or carries a forgiven-drop reason."
    )
    return _SynthesizedCodeImpositionResult(
        workflow_yaml=workflow_yaml,
        violations=[violation],
        repair_context=CodeAuthoringRepairContext(
            block_label=block_label,
            reason_code=reason_code,
            selector=obligation_finding_selector(first),
        ),
    )


def _pre_persist_scouted_spine_result(workflow_yaml: str, ctx: AgentContext) -> _SynthesizedCodeImpositionResult | None:
    """Last author-time gate before a durable persist: whatever path produced the final yaml, a draft
    whose browser calls leave the scouted spine partition open is rejected instead of persisted."""
    if not ctx.impose_synthesized_code_block:
        return None
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    scout_trajectory = ctx.scout_trajectory
    if not scout_trajectory:
        return None
    if not str(scout_trajectory[0].get("source_url") or "").strip():
        return None
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return None
    synthesized = ctx.imposition_synthesized_block
    if synthesized is None:
        # Deliberate fallback: when no imposition pass synthesized this attempt, grade a fresh strict
        # synthesis of the raw trajectory, without imposition's grounded-outcome reconciliation.
        synthesized = synthesize_code_block(
            scout_trajectory,
            strict_selectors=True,
            reached_download_target=ctx.reached_download_target,
        )
    if synthesized is None:
        return None
    code_blocks = _workflow_code_blocks(parsed)
    # A submission with zero code blocks still holds the open spine obligation: empty draft calls
    # leave every required rung uncovered rather than slipping the seam.
    return _scouted_spine_under_build_result(
        workflow_yaml,
        ctx=ctx,
        synthesized=synthesized,
        draft_codes=[str(block.get("code") or "") for block in code_blocks],
        block_label=", ".join(_code_block_label(block) for block in code_blocks) or _SYNTHESIZED_BLOCK_LABEL,
        site="pre_persist",
        draft_repairable_only=True,
    )


_FREEHAND_UNRESOLVABLE_SELECTOR_REASON_CODE = "freehand_unresolvable_selector"
_FREEHAND_UNGUARDED_CREDENTIAL_REASON_CODE = "freehand_unguarded_credential_fill"


def _scouted_selector_forms(scout_trajectory: list[ScoutedInteraction]) -> set[str]:
    return {
        normalized_scout_selector(selector)
        for interaction in scout_trajectory
        if (selector := str(interaction.get("selector") or "").strip())
    }


def _receiver_is_self_validating_get_by_role(receiver: str) -> bool:
    try:
        node = ast.parse(receiver, mode="eval").body
    except SyntaxError:
        return False
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_by_role"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "page"
    ):
        return False
    if len(node.args) != 1:
        return False
    role = node.args[0]
    if not (isinstance(role, ast.Constant) and isinstance(role.value, str) and role.value):
        return False
    if any(keyword.arg is None or not isinstance(keyword.value, ast.Constant) for keyword in node.keywords):
        return False
    return any(
        keyword.arg == "name"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
        and keyword.value.value
        for keyword in node.keywords
    )


def _fragment_scout_freehand_validation(
    *,
    code_blocks: list[dict[str, Any]],
    exempt_block: dict[str, Any] | None,
    prior_yaml: str | None,
    scout_trajectory: list[ScoutedInteraction],
    synthesized_code: str,
    synthesized_diagnostics: SynthesisDiagnostics | None,
) -> _BrowserSurfaceValidation:
    """A fragment scout is no authoritative full-spine reference, but every durable mutation must still
    resolve to a scout-captured form (captured selector or synthesized receiver) or be rejected; only
    selectorless page navigation and self-validating literal role+name receivers are admitted unscouted."""
    scouted_forms = _scouted_selector_forms(scout_trajectory)
    scouted_mutations, _, _ = _browser_surface_for_code(synthesized_code)
    admitted_receivers = {
        (mutation.method, normalized_locator_expr(mutation.receiver)) for mutation in scouted_mutations
    }
    violations: list[str] = []
    provenance: list[_BrowserSurfaceRejectionProvenance] = []
    for block in code_blocks:
        if block is exempt_block:
            continue
        if prior_yaml is not None and not _submitted_code_block_changed(block, prior_yaml):
            continue
        label = _code_block_label(block)
        block_mutations, _, block_ambiguous = _browser_surface_for_code(str(block.get("code") or ""))
        freehand_mutations: list[_BrowserMutationSignature] = []
        for mutation in block_mutations:
            if mutation.receiver == "page":
                continue
            if (mutation.method, normalized_locator_expr(mutation.receiver)) in admitted_receivers:
                continue
            if _receiver_is_self_validating_get_by_role(mutation.receiver):
                continue
            selector = _bare_locator_call_selector(mutation.receiver)
            if selector is not None and normalized_scout_selector(selector) in scouted_forms:
                continue
            freehand_mutations.append(mutation)
        if freehand_mutations:
            action_text = ", ".join(f"{mutation.receiver}.{mutation.method}" for mutation in sorted(freehand_mutations))
            block_provenance = [
                _classify_unscouted_mutation(
                    mutation,
                    scouted_mutations=scouted_mutations,
                    diagnostics=synthesized_diagnostics,
                    site="fragment_scout",
                    block_label=label,
                )
                for mutation in sorted(freehand_mutations)
            ]
            provenance.extend(block_provenance)
            violations.append(
                f"Unable to impose synthesized code block: `{label}` contains unscouted browser action(s): "
                f"{action_text}.{_provenance_suffix_text(block_provenance)}"
            )
        if block_ambiguous:
            ambiguous_provenance = [
                _ambiguous_browser_action_provenance(action, site="fragment_scout", block_label=label)
                for action in block_ambiguous
            ]
            provenance.extend(ambiguous_provenance)
            violations.append(
                f"Unable to impose synthesized code block: `{label}` contains ambiguous browser action(s): "
                + ", ".join(block_ambiguous)
                + "."
                + _provenance_suffix_text(ambiguous_provenance)
            )
    _log_browser_surface_rejection_provenance(provenance)
    return _BrowserSurfaceValidation(violations, provenance)


def _workflow_credential_parameter_keys(parsed: Mapping[str, Any]) -> set[str]:
    workflow_definition = parsed.get("workflow_definition")
    parameters = workflow_definition.get("parameters") if isinstance(workflow_definition, dict) else None
    if not isinstance(parameters, list):
        return set()
    keys: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, Mapping) or not _is_credential_parameter(parameter):
            continue
        key = str(parameter.get("key") or "").strip()
        if key:
            keys.add(key)
    return keys


def _credential_field_fill_argument(arg: ast.AST, credential_parameter_keys: AbstractSet[str]) -> bool:
    if (
        isinstance(arg, ast.Attribute)
        and arg.attr in _CREDENTIAL_FIELDS
        and isinstance(arg.value, ast.Name)
        and arg.value.id in credential_parameter_keys
    ):
        return True
    target = arg.value if isinstance(arg, ast.Await) else arg
    return (
        isinstance(target, ast.Call)
        and isinstance(target.func, ast.Attribute)
        and target.func.attr == "otp"
        and isinstance(target.func.value, ast.Name)
        and target.func.value.id in credential_parameter_keys
    )


def _is_credential_field_fill_call(node: ast.AST, credential_parameter_keys: AbstractSet[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fill"
        and bool(node.args)
        and _credential_field_fill_argument(node.args[0], credential_parameter_keys)
    )


def _is_presence_guard_test(test: ast.AST) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Attribute) and node.attr in {"count", "is_visible"}:
            return True
        if isinstance(node, ast.Name) and node.id in _INTERNAL_SCOUT_VARS:
            return True
    return False


def _credential_fill_is_presence_guarded(node: ast.AST, parents: Mapping[int, ast.AST]) -> bool:
    current: ast.AST = node
    while id(current) in parents:
        parent = parents[id(current)]
        if (
            isinstance(parent, ast.If)
            and any(current is stmt for stmt in parent.body)
            and _is_presence_guard_test(parent.test)
        ):
            return True
        current = parent
    return False


def _block_has_unguarded_credential_fill(code: str, credential_parameter_keys: AbstractSet[str]) -> bool:
    if not credential_parameter_keys:
        return False
    tree = _wrapped_code_ast(code)
    if tree is None:
        return False
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return any(
        _is_credential_field_fill_call(node, credential_parameter_keys)
        and not _credential_fill_is_presence_guarded(node, parents)
        for node in ast.walk(tree)
    )


def _freehand_surface_reject(
    workflow_yaml: str, code_blocks: list[dict[str, Any]], validation: _BrowserSurfaceValidation
) -> _SynthesizedCodeImpositionResult:
    first = validation.provenance[0] if validation.provenance else None
    repair_context = CodeAuthoringRepairContext(
        block_label=first.block_label if first is not None else _code_block_label(code_blocks[0]),
        reason_code=_FREEHAND_UNRESOLVABLE_SELECTOR_REASON_CODE,
        selector=first.nearest_selector if first is not None else None,
    )
    return _SynthesizedCodeImpositionResult(
        workflow_yaml=workflow_yaml,
        violations=validation.violations,
        repair_context=repair_context,
        never_captured_candidate=_never_captured_obligation_candidate(validation.provenance),
    )


def _persist_seam_freehand_surface_result(
    workflow_yaml: str, ctx: AgentContext
) -> _SynthesizedCodeImpositionResult | None:
    # Imposition validates only the spine it rewrites; this leg closes the persist lanes where a changed
    # freehand browser block never reached that surface gate, applying the same admissibility to siblings.
    if not ctx.impose_synthesized_code_block:
        return None
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    scout_trajectory = ctx.scout_trajectory
    if not scout_trajectory:
        return None
    if not str(scout_trajectory[0].get("source_url") or "").strip():
        return None
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return None
    code_blocks = _workflow_code_blocks(parsed)
    if not code_blocks:
        return None
    _, prior_yaml = _prior_yaml_source(ctx)
    carrier_label = ctx.spine_imposition_carrier_label
    exempt_block = (
        next((block for block in code_blocks if _code_block_label(block) == carrier_label), None)
        if carrier_label
        else None
    )
    credential_parameter_keys = _workflow_credential_parameter_keys(parsed)
    for block in code_blocks:
        if block is exempt_block:
            continue
        if prior_yaml is not None and not _submitted_code_block_changed(block, prior_yaml):
            continue
        if not _block_has_unguarded_credential_fill(str(block.get("code") or ""), credential_parameter_keys):
            continue
        label = _code_block_label(block)
        LOG.info("copilot_freehand_unguarded_credential_fill", block_label=label)
        violation = (
            f"Unable to persist code block: `{label}` fills a credential field without a presence guard, so it "
            "re-fills and times out when the workflow replays on an already-authenticated page. Reuse the "
            "synthesized entry rung, which fills only when the login form is still present."
        )
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=[violation],
            repair_context=CodeAuthoringRepairContext(
                block_label=label, reason_code=_FREEHAND_UNGUARDED_CREDENTIAL_REASON_CODE
            ),
        )
    synthesized = synthesize_code_block(
        scout_trajectory, strict_selectors=True, reached_download_target=ctx.reached_download_target
    )
    synthesized_code = "" if synthesized is None else (synthesized.interaction_code or synthesized.code)
    synthesized_diagnostics = None if synthesized is None else synthesized.diagnostics
    sentinel_selected: dict[str, Any] = {}
    selected_code_block = exempt_block if exempt_block is not None else sentinel_selected
    # get_by_label is never a synthesized selector, so an unresolvable one is rejected regardless of whether
    # the scout reached its goal; a fragment scout still fails this class closed to the scout-the-step route.
    factory_validation = _whole_trajectory_browser_surface_violations(
        code_blocks=code_blocks,
        selected_code_block=selected_code_block,
        submitted_selected_code="",
        synthesized_code=synthesized_code,
        prior_yaml=prior_yaml,
        synthesized_diagnostics=synthesized_diagnostics,
        admit_on_receiver=True,
        restrict_to_durable_factory=True,
    )
    if factory_validation.violations:
        return _freehand_surface_reject(workflow_yaml, code_blocks, factory_validation)
    # The full synthesized spine is an authoritative admissibility reference only when the scout captured a
    # durable entry through a commit; a fragment scout would false-reject legitimate multi-block authoring.
    if synthesized is None or not synthesized_trajectory_reaches_goal(ctx):
        # scouted_interactions joins the admitted set because the trajectory is cap-evicted; both
        # collections carry only scout-captured selectors.
        fragment_validation = _fragment_scout_freehand_validation(
            code_blocks=code_blocks,
            exempt_block=exempt_block,
            prior_yaml=prior_yaml,
            scout_trajectory=[*scout_trajectory, *ctx.scouted_interactions],
            synthesized_code=synthesized_code,
            synthesized_diagnostics=synthesized_diagnostics,
        )
        if fragment_validation.violations:
            return _freehand_surface_reject(workflow_yaml, code_blocks, fragment_validation)
        return None
    validation = _whole_trajectory_browser_surface_violations(
        code_blocks=code_blocks,
        selected_code_block=selected_code_block,
        submitted_selected_code="",
        synthesized_code=synthesized_code,
        prior_yaml=prior_yaml,
        synthesized_diagnostics=synthesized_diagnostics,
        admit_on_receiver=True,
    )
    if not validation.violations:
        return None
    return _freehand_surface_reject(workflow_yaml, code_blocks, validation)


def _workflow_yaml_browser_call_pairs(workflow_yaml: str) -> list[tuple[str, str]]:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return []
    return [
        (mutation.method, mutation.receiver)
        for block in _workflow_code_blocks(parsed)
        for mutation in _browser_surface_for_code(str(block.get("code") or ""))[0]
    ]


def _submitted_suffix_after_synthesized_code(submitted_code: str, synthesized_code: str) -> str:
    # Preserve a pure suffix appended after the synthesized steps. Returns empty for
    # prepended extraction scaffolding; that shape is handled by preserve_submitted_extraction.
    submitted = textwrap.dedent(submitted_code).strip()
    synthesized = textwrap.dedent(synthesized_code).strip()
    if not submitted or not synthesized:
        return ""
    if submitted == synthesized:
        return ""
    if submitted.startswith(synthesized):
        suffix = submitted[len(synthesized) :]
        return textwrap.dedent(suffix).lstrip("\n").rstrip()
    submitted_lines = submitted_code.strip("\n").splitlines()
    synthesized_lines = synthesized_code.strip("\n").splitlines()
    if len(submitted_lines) <= len(synthesized_lines):
        return ""
    submitted_prefix = "\n".join(submitted_lines[: len(synthesized_lines)])
    if textwrap.dedent(submitted_prefix).strip() != textwrap.dedent(synthesized_code).strip():
        return ""
    return textwrap.dedent("\n".join(submitted_lines[len(synthesized_lines) :])).lstrip("\n").rstrip()


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


def _is_submitted_code_synthesized_only(submitted_code: str, synthesized_code: str) -> bool:
    submitted = textwrap.dedent(submitted_code).strip()
    synthesized = textwrap.dedent(synthesized_code).strip()
    return bool(submitted and synthesized and submitted == synthesized)


def _wrapped_code_ast(code: str) -> ast.AST | None:
    body = "\n".join(f"    {line}" for line in code.splitlines())
    if not body.strip():
        body = "    pass"
    try:
        return ast.parse(f"async def __submitted_code__():\n{body}\n")
    except SyntaxError:
        return None


def _is_page_locator_expression(value: ast.AST) -> bool:
    # Peel a trailing `.first`/`.last` (the synthesizer's disambiguator for a bare role/tag selector)
    # so literal-fill parameter binding still recognizes the underlying page locator.
    while isinstance(value, ast.Attribute) and value.attr in {"first", "last"}:
        value = value.value
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
        return False
    if value.func.attr not in {"locator", "get_by_role"}:
        return False
    return isinstance(value.func.value, ast.Name) and value.func.value.id == "page"


def _single_assignment_string_literals(tree: ast.AST) -> dict[str, str]:
    assignments: dict[str, list[str | None]] = {}

    def record(name: str, value: ast.AST | None) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            assignments.setdefault(name, []).append(value.value)
        else:
            assignments.setdefault(name, []).append(None)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.value)
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            record(node.target.id, node.value)
            continue
        if isinstance(node, (ast.AugAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
            record(node.target.id, None)
            continue
        if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
            record(node.target.id, None)

    return {name: values[0] for name, values in assignments.items() if len(values) == 1 and values[0] is not None}


def _string_literal_argument(first_arg: ast.AST, assignment_literals: Mapping[str, str]) -> str | None:
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        return first_arg.value
    if isinstance(first_arg, ast.Name):
        return assignment_literals.get(first_arg.id)
    if (
        isinstance(first_arg, ast.Call)
        and isinstance(first_arg.func, ast.Name)
        and first_arg.func.id == "str"
        and len(first_arg.args) == 1
        and isinstance(first_arg.args[0], ast.Name)
        and not first_arg.keywords
    ):
        return assignment_literals.get(first_arg.args[0].id)
    return None


def _submitted_fill_type_arguments(code: str) -> list[str | None]:
    tree = _wrapped_code_ast(code)
    if tree is None:
        return []
    assignment_literals = _single_assignment_string_literals(tree)
    arguments: list[str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SUBMITTED_LITERAL_METHODS:
            continue
        if not _is_page_locator_expression(func.value):
            arguments.append(None)
            continue
        if not node.args:
            arguments.append(None)
            continue
        arguments.append(_string_literal_argument(node.args[0], assignment_literals))
    return arguments


def _direct_parameter_reference_argument_name(first_arg: ast.AST) -> str | None:
    if isinstance(first_arg, ast.Name):
        return first_arg.id
    if (
        isinstance(first_arg, ast.Call)
        and isinstance(first_arg.func, ast.Name)
        and first_arg.func.id == "str"
        and len(first_arg.args) == 1
        and isinstance(first_arg.args[0], ast.Name)
        and not first_arg.keywords
    ):
        return first_arg.args[0].id
    return None


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
            # Model only the submitted block's outer runtime scope here; helper-local
            # assignments should not shadow workflow parameters in the outer direct-fill path.
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes


def _name_is_assigned(tree: ast.AST, parameter_key: str) -> bool:
    for node in _submitted_scope_nodes(tree):
        targets: list[ast.AST | None] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            targets.append(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name) and target.id == parameter_key:
                return True
    return False


class _DirectFillTypeUsage(NamedTuple):
    matched: bool
    mismatched: bool


def _submitted_direct_fill_type_usage(code: str, parameter_key: str) -> _DirectFillTypeUsage:
    tree = _wrapped_code_ast(code)
    if tree is None or _name_is_assigned(tree, parameter_key):
        return _DirectFillTypeUsage(False, False)
    matched = False
    mismatched = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SUBMITTED_LITERAL_METHODS:
            continue
        if not _is_page_locator_expression(func.value) or not node.args:
            continue
        if _direct_parameter_reference_argument_name(node.args[0]) != parameter_key:
            mismatched = True
        else:
            matched = True
    return _DirectFillTypeUsage(matched, mismatched)


def _submitted_uses_parameter_in_direct_fill_type(code: str, parameter_key: str) -> bool:
    usage = _submitted_direct_fill_type_usage(code, parameter_key)
    return usage.matched and not usage.mismatched


def _safe_singleton_literal_for_parameter(
    code: str, parameter_key: str, typed_length: int | None
) -> tuple[str | None, str | None]:
    arguments = _submitted_fill_type_arguments(code)
    if len(arguments) != 1 or arguments[0] is None:
        return (
            None,
            f"Unable to bind synthesized parameter `{parameter_key}`: submitted code must contain exactly one direct browser-locator string literal fill/type call or single local string constant binding.",
        )
    literal = arguments[0]
    if _SECRET_LIKE_LITERAL_RE.search(literal):
        return None, f"Unable to bind synthesized parameter `{parameter_key}`: submitted literal looks credential-like."
    if typed_length is not None and typed_length > 0 and len(literal) != typed_length:
        return (
            None,
            f"Unable to bind synthesized parameter `{parameter_key}`: submitted literal length does not match the scout record.",
        )
    return literal, None


def _ast_column_offsets_are_utf8_bytes() -> bool:
    probe_code = 'await page.locator("#café-search").fill("x")'
    tree = _wrapped_code_ast(probe_code)
    if tree is None:
        LOG.debug("copilot_ast_column_offset_probe_fallback", reason="parse_failed")
        return True
    literal = next(
        (
            node.args[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fill"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "x"
        ),
        None,
    )
    if literal is None:
        LOG.debug("copilot_ast_column_offset_probe_fallback", reason="literal_not_found")
        return True
    prefix = 'await page.locator("#café-search").fill('
    return literal.col_offset == 4 + len(prefix.encode("utf-8"))


_AST_COLUMN_OFFSETS_ARE_UTF8_BYTES = _ast_column_offsets_are_utf8_bytes()


def _wrapped_position_to_original_offset(code: str, lineno: int, col_offset: int) -> int | None:
    lines = code.splitlines(keepends=True)
    line_index = lineno - 2
    if line_index < 0 or line_index >= len(lines):
        return None
    column_offset = max(col_offset - 4, 0)
    if _AST_COLUMN_OFFSETS_ARE_UTF8_BYTES:
        # CPython reports AST column offsets as UTF-8 byte offsets in the
        # parsed source on our supported runtime, even when ast.parse receives
        # a str. Convert back to a Python string index after removing the
        # wrapper indent.
        column_chars = len(lines[line_index].encode("utf-8")[:column_offset].decode("utf-8", errors="ignore"))
    else:
        column_chars = column_offset
    return sum(len(line) for line in lines[:line_index]) + column_chars


def _rewrite_direct_literal_fills(code: str, value_to_key: Mapping[str, str]) -> tuple[str, list[str]]:
    tree = _wrapped_code_ast(code)
    if tree is None:
        return code, []
    replacements: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SUBMITTED_LITERAL_METHODS:
            continue
        if not _is_page_locator_expression(func.value) or not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            continue
        key = value_to_key.get(first_arg.value)
        if key is None or first_arg.end_lineno is None or first_arg.end_col_offset is None:
            continue
        start = _wrapped_position_to_original_offset(code, first_arg.lineno, first_arg.col_offset)
        end = _wrapped_position_to_original_offset(code, first_arg.end_lineno, first_arg.end_col_offset)
        if start is None or end is None:
            continue
        replacements.append((start, end, f"str({key})", key))
    if not replacements:
        return code, []
    rewritten = code
    used_keys: list[str] = []
    for start, end, replacement, key in sorted(replacements, key=lambda item: item[0], reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        if key not in used_keys:
            used_keys.append(key)
    return rewritten, list(reversed(used_keys))


def _string_parameter_row(default_value: str, key: str) -> dict[str, Any]:
    return {
        "parameter_type": "workflow",
        "workflow_parameter_type": "string",
        "key": key,
        "default_value": default_value,
    }


def _required_string_parameter_row(key: str) -> dict[str, Any]:
    return {
        "parameter_type": "workflow",
        "workflow_parameter_type": "string",
        "key": key,
    }


def _workflow_output_parameter_keys(parsed: dict[str, Any]) -> set[str]:
    return {
        f"{str(block.get('label') or '').strip()}_output"
        for block in workflow_blocks(parsed)
        if str(block.get("label") or "").strip()
    }


def _plain_parameter_conflicts(parameter: Mapping[str, Any], default_value: str) -> bool:
    if is_sensitive_workflow_parameter(dict(parameter)):
        return True
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    if parameter_type and parameter_type != "workflow":
        return True
    if workflow_parameter_type and workflow_parameter_type != "string":
        return True
    existing_default = parameter.get("default_value")
    return isinstance(existing_default, str) and existing_default not in {"", default_value}


def _normalize_plain_parameter_row(parameter: dict[str, Any], default_value: str) -> None:
    if not isinstance(parameter.get("default_value"), str) or parameter.get("default_value") == "":
        parameter["default_value"] = default_value
    parameter["parameter_type"] = "workflow"
    parameter["workflow_parameter_type"] = "string"


def _allocate_promoted_parameter_key(
    *,
    base_key: str,
    default_value: str,
    parameters: list[Any],
    reserved_keys: set[str],
) -> tuple[str, bool]:
    existing_by_key = {
        str(param.get("key")): param for param in parameters if isinstance(param, dict) and param.get("key")
    }
    base = base_key if base_key and base_key.isidentifier() else "typed_value"
    candidate = base
    suffix = 2
    while candidate in reserved_keys:
        candidate = f"{base}_{suffix}"
        suffix += 1
    # Termination is guaranteed because `existing_by_key` and `reserved_keys`
    # are finite while `suffix` increases monotonically.
    while True:
        existing = existing_by_key.get(candidate)
        if existing is None:
            return candidate, True
        if not _plain_parameter_conflicts(existing, default_value):
            return candidate, False
        candidate = f"{base}_{suffix}"
        suffix += 1
        while candidate in reserved_keys:
            candidate = f"{base}_{suffix}"
            suffix += 1


def _strip_redundant_sandbox_imports_in_yaml(workflow_yaml: str) -> tuple[str, list[str]]:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml, []
    stripped_modules: list[str] = []
    any_change = False
    for block in _workflow_code_blocks(parsed):
        code = block.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        sanitized, modules = strip_redundant_sandbox_imports(code)
        if sanitized == code:
            continue
        block["code"] = sanitized
        stripped_modules.extend(modules)
        any_change = True
    if not any_change:
        return workflow_yaml, []
    return yaml.safe_dump(parsed, sort_keys=False), stripped_modules


def _code_block_has_load_bearing_mutation(code: str) -> bool:
    mutations, _reads, _ambiguous = _browser_surface_for_code(code)
    return any(mutation.method in _LOCATOR_MUTATION_METHODS for mutation in mutations)


def _iter_workflow_blocks(blocks: object) -> Iterator[dict[str, Any]]:
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        yield block
        yield from _iter_workflow_blocks(block.get("loop_blocks"))


def _workflow_block_graph(parsed: Mapping[str, Any]) -> tuple[set[str], list[str]]:
    definition = parsed.get("workflow_definition")
    blocks = definition.get("blocks") if isinstance(definition, dict) else None
    labels: set[str] = set()
    references: list[str] = []
    for block in _iter_workflow_blocks(blocks):
        label = str(block.get("label") or "").strip()
        if label:
            labels.add(label)
    for block in _iter_workflow_blocks(blocks):
        next_label = str(block.get("next_block_label") or "").strip()
        if next_label:
            references.append(next_label)
        branch_conditions = block.get("branch_conditions")
        if isinstance(branch_conditions, list):
            for branch in branch_conditions:
                if not isinstance(branch, dict):
                    continue
                branch_target = str(branch.get("next_block_label") or "").strip()
                if branch_target:
                    references.append(branch_target)
    return labels, references


def _dangling_next_block_label_violation(parsed: Mapping[str, Any]) -> str | None:
    labels, references = _workflow_block_graph(parsed)
    dangling = sorted({reference for reference in references if reference not in labels})
    if not dangling:
        return None
    return (
        "Unable to persist workflow: next_block_label reference(s) point to no existing block: "
        + ", ".join(f"`{label}`" for label in dangling)
        + "."
    )


def _scaffolding_only_body_violation(ctx: AgentContext, parsed: Mapping[str, Any]) -> str | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    scout_trajectory = ctx.scout_trajectory
    if not isinstance(scout_trajectory, list) or not scout_trajectory:
        return None
    synthesized = synthesize_code_block(
        scout_trajectory, reached_download_target=getattr(ctx, "reached_download_target", None)
    )
    if synthesized is None:
        return None
    required = [record for record in synthesized.diagnostics.emitted_interactions if not str(record.get("lane") or "")]
    if not required:
        return None
    code_blocks = _workflow_code_blocks(parsed) if isinstance(parsed, dict) else []
    if not code_blocks:
        return None
    if any(_code_block_has_load_bearing_mutation(str(block.get("code") or "")) for block in code_blocks):
        return None
    return (
        "Unable to persist workflow: the persisted code block(s) commit no load-bearing browser interaction "
        f"({SCOUTED_SPINE_UNDER_BUILD_REASON_CODE}), though the scout demonstrated {len(required)} rung(s). "
        "Reuse the synthesized code block verbatim so the captured interactions are replayed."
    )


def _normalize_prompt_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _stub_with_raw_prompt_violation(ctx: AgentContext, parsed: Mapping[str, Any]) -> str | None:
    packet = getattr(ctx, "turn_context_packet", None)
    if packet is None:
        return None
    anchor = _normalize_prompt_text(str(packet.transcript_context.earliest_user_turn or ""))
    if not anchor:
        return None
    if _scaffolding_only_body_violation(ctx, parsed) is None:
        return None
    for block in _iter_workflow_blocks(parsed.get("workflow_definition", {}).get("blocks")):
        for field_name in ("prompt", "description", "title"):
            if _normalize_prompt_text(str(block.get(field_name) or "")) == anchor:
                return (
                    "Unable to persist workflow: a block reproduces the raw user request as its "
                    f"{field_name} while committing no load-bearing browser interaction "
                    f"({SCOUTED_SPINE_UNDER_BUILD_REASON_CODE})."
                )
    return None


def _code_references_parameter(code: str, key: str) -> bool:
    sources = _code_runtime_parameter_sources(code, {key})
    return sources is not None and key in sources


def _orphan_minted_parameter_violation(parsed: Mapping[str, Any], minted_keys: list[str]) -> str | None:
    if not minted_keys or not isinstance(parsed, dict):
        return None
    codes = [str(block.get("code") or "") for block in _workflow_code_blocks(parsed)]
    orphans = sorted(
        {key for key in minted_keys if key and not any(_code_references_parameter(code, key) for code in codes)}
    )
    if not orphans:
        return None
    return (
        "Unable to persist workflow: minted workflow parameter(s) are not referenced by any generated code: "
        + ", ".join(f"`{key}`" for key in orphans)
        + "."
    )


def _final_yaml_structural_violations(
    ctx: AgentContext,
    workflow_yaml: str,
    *,
    minted_parameter_keys: list[str],
    promoted_parameter_keys: list[str],
    carried_by_imposition: bool,
) -> list[str]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return []
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return []
    candidates: list[str | None] = [
        _dangling_next_block_label_violation(parsed),
        _orphan_minted_parameter_violation(parsed, [*minted_parameter_keys, *promoted_parameter_keys]),
    ]
    # The spine-coverage seam runs only when imposition made no substitution; a successful imposition
    # skips it, so the body-shape predicates re-validate exactly that bypassed window.
    if carried_by_imposition:
        candidates.insert(0, _scaffolding_only_body_violation(ctx, parsed))
        candidates.insert(1, _stub_with_raw_prompt_violation(ctx, parsed))
    return [violation for violation in candidates if violation is not None]


def _apply_scouted_typed_default_promotions(workflow_yaml: str, ctx: AgentContext) -> tuple[str, list[str], list[str]]:
    if not getattr(ctx, "impose_synthesized_code_block", False):
        return workflow_yaml, [], []
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return workflow_yaml, [], []
    scout_trajectory = ctx.scout_trajectory
    if not isinstance(scout_trajectory, list) or not scout_trajectory:
        return workflow_yaml, [], []
    synthesized = synthesize_code_block(
        scout_trajectory,
        reached_download_target=getattr(ctx, "reached_download_target", None),
    )
    if synthesized is None:
        return workflow_yaml, [], []

    defaults_by_value: dict[str, list[str]] = {}
    for parameter in synthesized.parameters:
        if parameter.get("credential_id") or parameter.get("source") == LOCATOR_WITNESS_PARAM_SOURCE:
            continue
        key = str(parameter.get("key") or "").strip()
        default_value = str(parameter.get("default_value") or "").strip()
        if key and default_value and not _SECRET_LIKE_LITERAL_RE.search(default_value):
            defaults_by_value.setdefault(default_value, []).append(key)
    ambiguous_key_sets = [sorted(set(keys)) for keys in defaults_by_value.values() if len(set(keys)) > 1]
    if ambiguous_key_sets:
        LOG.debug(
            "copilot_scouted_typed_default_promotion_ambiguous_values_skipped",
            ambiguous_value_count=len(ambiguous_key_sets),
            candidate_key_sets=ambiguous_key_sets,
        )
    value_to_key = {value: keys[0] for value, keys in defaults_by_value.items() if len(set(keys)) == 1}
    if not value_to_key:
        return workflow_yaml, [], []

    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml, [], []
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return workflow_yaml, [], []
    parameters = workflow_definition.get("parameters")
    if parameters is None:
        parameters = []
        workflow_definition["parameters"] = parameters
    if not isinstance(parameters, list):
        return (
            workflow_yaml,
            ["Unable to bind typed workflow inputs: workflow_definition.parameters must be a list."],
            [],
        )

    reserved_keys = set(RESERVED_PARAMETER_KEYS) | _workflow_output_parameter_keys(parsed)
    allocated_value_to_key: dict[str, str] = {}
    default_by_allocated_key: dict[str, str] = {}
    should_create_by_allocated_key: dict[str, bool] = {}
    for value, base_key in value_to_key.items():
        key, should_create = _allocate_promoted_parameter_key(
            base_key=base_key,
            default_value=value,
            parameters=parameters,
            reserved_keys=reserved_keys,
        )
        allocated_value_to_key[value] = key
        default_by_allocated_key[key] = value
        should_create_by_allocated_key[key] = should_create
        reserved_keys.add(key)

    any_rewrite = False
    used_promoted_keys: set[str] = set()
    for block in _workflow_code_blocks(parsed):
        code = str(block.get("code") or "")
        rewritten, used_keys = _rewrite_direct_literal_fills(code, allocated_value_to_key)
        if not used_keys:
            continue
        block["code"] = rewritten
        existing_keys = block.get("parameter_keys")
        merged_keys = (
            [str(key) for key in existing_keys if isinstance(key, str)] if isinstance(existing_keys, list) else []
        )
        for key in used_keys:
            if key not in merged_keys:
                merged_keys.append(key)
            used_promoted_keys.add(key)
        block["parameter_keys"] = merged_keys
        any_rewrite = True

    if not any_rewrite:
        return workflow_yaml, [], []

    existing_by_key = {
        str(param.get("key")): param for param in parameters if isinstance(param, dict) and param.get("key")
    }
    for key in sorted(used_promoted_keys):
        default_value = default_by_allocated_key.get(key, "")
        if should_create_by_allocated_key.get(key):
            parameters.append(_string_parameter_row(default_value, key))
            continue
        existing = existing_by_key.get(key)
        if isinstance(existing, dict):
            _normalize_plain_parameter_row(existing, default_value)
    return yaml.safe_dump(parsed, sort_keys=False), [], sorted(used_promoted_keys)


def _is_credential_parameter(parameter: Mapping[str, Any]) -> bool:
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    return parameter_type == "credential" or (
        parameter_type == "workflow" and workflow_parameter_type == "credential_id"
    )


def _string_parameter_default_value(parameter: Mapping[str, Any]) -> str | None:
    if _is_credential_parameter(parameter):
        return None
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    if parameter_type and parameter_type != "workflow":
        return None
    if workflow_parameter_type and workflow_parameter_type != "string":
        return None
    default_value = parameter.get("default_value")
    return default_value if isinstance(default_value, str) else None


def _matching_string_parameter_key_by_default(
    parameters: list[Any],
    *,
    default_value: str,
    exclude_key: str,
) -> str | None:
    if not default_value or _SECRET_LIKE_LITERAL_RE.search(default_value):
        return None
    matches: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = str(parameter.get("key") or "").strip()
        if not key or key == exclude_key:
            continue
        if _string_parameter_default_value(parameter) == default_value:
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


def _fill_type_argument_parameter_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
    ):
        return node.args[0].id
    return ""


def _selector_join_parameter_alias_by_authored_fill(
    *,
    parameters: list[Any],
    submitted_code: str,
    synthesized_key: str,
    selector: str,
) -> str | None:
    selector = selector.strip()
    if not selector:
        return None
    available = {
        str(parameter.get("key") or "").strip()
        for parameter in parameters
        if isinstance(parameter, dict)
        and str(parameter.get("key") or "").strip()
        and not _is_credential_parameter(parameter)
    }
    available.discard(synthesized_key)
    if not available:
        return None
    try:
        tree = ast.parse(textwrap.dedent(submitted_code).strip() or "pass")
    except SyntaxError:
        return None
    locator_aliases = _locator_alias_selectors(tree)
    matches: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"fill", "type"} or not node.args:
            continue
        selectors = _locator_receiver_selectors(node.func.value, locator_aliases)
        if selectors != {selector}:
            continue
        parameter_name = _fill_type_argument_parameter_name(node.args[0])
        if parameter_name in available:
            matches.add(parameter_name)
    return next(iter(matches)) if len(matches) == 1 else None


def _drop_parameter_key(parameters: list[Any], key: str) -> None:
    parameters[:] = [
        parameter
        for parameter in parameters
        if not (isinstance(parameter, dict) and str(parameter.get("key") or "").strip() == key)
    ]


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _replace_python_identifier(source: str, old: str, new: str) -> str:
    if old == new:
        return source
    if not _is_python_identifier(old):
        return source
    try:
        tokens = [
            token._replace(string=new) if token.type == tokenize.NAME and token.string == old else token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
        ]
        replaced = tokenize.untokenize(tokens)
        ast.parse(replaced)
    except (SyntaxError, tokenize.TokenError):
        return source
    return replaced


def _is_python_identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value)


class _SynthesizedParameterReconciliation(NamedTuple):
    parameter_keys: list[str]
    violations: list[str]
    aliases: dict[str, str]
    repair_context: CodeAuthoringRepairContext | None = None


def _apply_parameter_reconciliation_to_code(code: str, reconciliation: _SynthesizedParameterReconciliation) -> str:
    reconciled = code
    for old_key, new_key in reconciliation.aliases.items():
        reconciled = _replace_python_identifier(reconciled, old_key, new_key)
    return reconciled


_SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE = "synthesized_parameter_binding_ambiguous"


def _record_synthesized_parameter_binding_ablation_event(
    ctx: AgentContext,
    workflow_yaml: str,
    repair_context: CodeAuthoringRepairContext,
) -> bool:
    fingerprint = authored_structure_signature_from_workflow(workflow_yaml, ctx.raw_code_artifact_metadata)
    if fingerprint is None:
        return False
    payload: AuthorTimeGateAblationPayload = {
        "block_label": repair_context.block_label,
        "unresolved_names": list(repair_context.unresolved_names),
        "parameter_keys": list(repair_context.parameter_keys),
        "available_parameter_keys": list(repair_context.available_parameter_keys),
        "binding_candidates": list(repair_context.binding_candidates),
    }
    return record_author_time_gate_ablation_event(
        ctx,
        gate_id=SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID,
        reason_code=_SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE,
        fingerprint=fingerprint,
        blocked_tool="update_workflow",
        payload=payload,
    )


def _synthesized_parameter_binding_repair_context(
    *,
    parsed: Mapping[str, Any],
    code_block: Mapping[str, Any],
    synthesized_key: str,
    parameter_keys: list[str],
    scout_trajectory: list[ScoutedInteraction],
    synthesized_parameters: list[dict[str, str]],
    parameter_binding_snapshot: AuthoringParameterBindingSnapshot | None = None,
) -> CodeAuthoringRepairContext:
    available_parameter_keys = sorted(_declared_string_workflow_parameter_keys(parsed))
    binding_candidates = [synthesized_key] + [key for key in available_parameter_keys if key != synthesized_key]
    matched_scout = _scout_interaction_for_synthesized_parameter(
        synthesized_key=synthesized_key,
        scout_trajectory=scout_trajectory,
        synthesized_parameters=synthesized_parameters,
        parameter_binding_snapshot=parameter_binding_snapshot,
    )
    selector = _safe_selector_repair_atom(matched_scout.get("selector")) if matched_scout is not None else ""
    source_url = str(matched_scout.get("source_url") or "").strip() if matched_scout is not None else ""
    return CodeAuthoringRepairContext(
        block_label=str(code_block.get("label") or ""),
        reason_code=_SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE,
        unresolved_names=[synthesized_key],
        parameter_keys=list(parameter_keys),
        available_parameter_keys=available_parameter_keys,
        binding_candidates=binding_candidates,
        selector=selector or None,
        source_url=url_origin(source_url) if source_url else None,
        repair_instruction=(
            f"Declare and use workflow string parameter `{synthesized_key}` exactly, include it in parameter_keys, "
            "reference it as a bare Python variable in code, and rerun via update_and_run_blocks."
        ),
    )


def _scout_interaction_for_synthesized_parameter(
    *,
    synthesized_key: str,
    scout_trajectory: list[ScoutedInteraction],
    synthesized_parameters: list[dict[str, str]],
    parameter_binding_snapshot: AuthoringParameterBindingSnapshot | None = None,
) -> ScoutedInteraction | None:
    if parameter_binding_snapshot is not None:
        matches = [
            binding
            for binding in parameter_binding_snapshot.field_bindings
            if binding.declared_key == synthesized_key and binding.field_trajectory_index is not None
        ]
        if len(matches) != 1:
            return None
        trajectory_index = matches[0].field_trajectory_index
        if trajectory_index is None:
            return None
        interactions = [
            interaction
            for position, interaction in enumerate(scout_trajectory)
            if _scout_trajectory_index(interaction, position) == trajectory_index
        ]
        if len(interactions) != 1:
            return None
        interaction = interactions[0]
        if str(interaction.get("selector") or "").strip() != matches[0].field_selector:
            return None
        return interaction
    non_credential_keys = [
        str(parameter.get("key") or "").strip()
        for parameter in synthesized_parameters
        if str(parameter.get("key") or "").strip()
        and not parameter.get("credential_id")
        and parameter.get("source") != LOCATOR_WITNESS_PARAM_SOURCE
    ]
    typed_interactions = [
        interaction for interaction in scout_trajectory if str(interaction.get("tool_name") or "") == "type_text"
    ]
    if non_credential_keys.count(synthesized_key) != 1:
        return None
    index = non_credential_keys.index(synthesized_key)
    if index >= len(typed_interactions):
        return None
    return typed_interactions[index]


def _reconcile_synthesized_parameters(
    *,
    parsed: dict[str, Any],
    code_block: dict[str, Any],
    submitted_code: str,
    synthesized_parameters: list[dict[str, str]],
    scout_trajectory: list[ScoutedInteraction],
    parameter_binding_snapshot: AuthoringParameterBindingSnapshot | None = None,
) -> _SynthesizedParameterReconciliation:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return _SynthesizedParameterReconciliation(
            [], ["Unable to bind synthesized parameters: workflow_definition is missing."], {}
        )
    parameters = workflow_definition.get("parameters")
    if parameters is None:
        parameters = []
        workflow_definition["parameters"] = parameters
    if not isinstance(parameters, list):
        return _SynthesizedParameterReconciliation(
            [], ["Unable to bind synthesized parameters: workflow_definition.parameters must be a list."], {}
        )

    existing_by_key = {
        str(param.get("key")): param for param in parameters if isinstance(param, dict) and param.get("key")
    }
    existing_credentials = credential_param_ids(parameters)
    parameter_keys: list[str] = []
    violations: list[str] = []
    aliases: dict[str, str] = {}
    used_selector_join_aliases: set[str] = set()
    repair_context: CodeAuthoringRepairContext | None = None
    non_credential_synthesized = [
        param
        for param in synthesized_parameters
        if not param.get("credential_id") and param.get("source") != LOCATOR_WITNESS_PARAM_SOURCE
    ]
    typed_lengths = [
        int(interaction.get("typed_length") or 0)
        for interaction in scout_trajectory
        if str(interaction.get("tool_name") or "") == "type_text"
    ]

    def add_binding_violation(key: str, message: str) -> None:
        nonlocal repair_context
        violations.append(message)
        if repair_context is None:
            repair_context = _synthesized_parameter_binding_repair_context(
                parsed=parsed,
                code_block=code_block,
                synthesized_key=key,
                parameter_keys=parameter_keys,
                scout_trajectory=scout_trajectory,
                synthesized_parameters=synthesized_parameters,
                parameter_binding_snapshot=parameter_binding_snapshot,
            )

    for synthesized_param in synthesized_parameters:
        key = str(synthesized_param.get("key") or "").strip()
        if not key:
            violations.append("Unable to bind synthesized parameter: parameter key is missing.")
            continue
        if key in parameter_keys:
            add_binding_violation(key, f"Unable to bind synthesized parameter `{key}`: duplicate synthesized key.")
            continue
        parameter_keys.append(key)

        credential_id = str(synthesized_param.get("credential_id") or "").strip()
        existing = existing_by_key.get(key)
        synthesized_default = str(synthesized_param.get("default_value") or "").strip()
        typed_length = _coerce_positive_int(synthesized_param.get("typed_length"))
        matched_scout = _scout_interaction_for_synthesized_parameter(
            synthesized_key=key,
            scout_trajectory=scout_trajectory,
            synthesized_parameters=synthesized_parameters,
            parameter_binding_snapshot=parameter_binding_snapshot,
        )
        matched_selector = str(matched_scout.get("selector") or "").strip() if matched_scout is not None else ""
        selector_join_alias = (
            None
            if parameter_binding_snapshot is not None
            else _selector_join_parameter_alias_by_authored_fill(
                parameters=parameters,
                submitted_code=submitted_code,
                synthesized_key=key,
                selector=matched_selector,
            )
        )
        if selector_join_alias is not None:
            if selector_join_alias in used_selector_join_aliases:
                add_binding_violation(
                    key,
                    f"Unable to bind synthesized parameter `{key}`: authored selector-join alias is reused by another synthesized input.",
                )
                continue
            aliases[key] = selector_join_alias
            used_selector_join_aliases.add(selector_join_alias)
            parameter_keys[-1] = selector_join_alias
            continue
        scout_typed_length = (
            _coerce_positive_int(matched_scout.get("typed_length")) if matched_scout is not None else None
        )
        typed_length = typed_length or scout_typed_length
        if credential_id:
            if existing is not None:
                if credential_id not in existing_credentials.get(key, set()):
                    violations.append(
                        f"Unable to bind synthesized credential parameter `{key}`: submitted credential binding does not match scout metadata."
                    )
                continue
            parameters.append(
                {
                    "parameter_type": "workflow",
                    "workflow_parameter_type": "credential_id",
                    "key": key,
                    "default_value": credential_id,
                }
            )
            continue

        if existing is not None:
            alias_key = (
                None
                if parameter_binding_snapshot is not None
                else _matching_string_parameter_key_by_default(
                    parameters,
                    default_value=synthesized_default,
                    exclude_key=key,
                )
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                _drop_parameter_key(parameters, key)
                continue
            if _is_credential_parameter(existing):
                add_binding_violation(
                    key,
                    f"Unable to bind synthesized parameter `{key}`: submitted parameter is credential-typed.",
                )
            elif synthesized_default:
                existing_default = _string_parameter_default_value(existing)
                if existing_default != synthesized_default:
                    add_binding_violation(
                        key,
                        f"Unable to bind synthesized parameter `{key}`: "
                        "submitted parameter default does not match the scout record.",
                    )
            continue

        if synthesized_default:
            # Narrow defense-in-depth backstop for synthesized rows; values
            # captured from live type_text scouting are fully screened by
            # safe_typed_default_value before they enter the scout trajectory.
            if _SECRET_LIKE_LITERAL_RE.search(synthesized_default):
                add_binding_violation(
                    key,
                    f"Unable to bind synthesized parameter `{key}`: synthesized default looks credential-like.",
                )
                continue
            alias_key = (
                None
                if parameter_binding_snapshot is not None
                else _matching_string_parameter_key_by_default(
                    parameters,
                    default_value=synthesized_default,
                    exclude_key=key,
                )
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                continue
            parameters.append(_string_parameter_row(synthesized_default, key))
            continue

        typed_length = typed_length or scout_typed_length or (typed_lengths[0] if len(typed_lengths) == 1 else None)
        direct_fill_usage = _submitted_direct_fill_type_usage(submitted_code, key)
        if direct_fill_usage.matched and direct_fill_usage.mismatched:
            add_binding_violation(
                key,
                f"Unable to bind synthesized parameter `{key}`: submitted code mixes direct fills using `{key}` "
                "with other browser-locator fill/type values. Use the synthesized parameter for every scout-input "
                "fill in the code block, or declare explicit workflow parameters/defaults for the other filled values.",
            )
            continue

        if len(non_credential_synthesized) != 1:
            if matched_scout is not None or direct_fill_usage.matched:
                parameters.append(_required_string_parameter_row(key))
                continue
            add_binding_violation(
                key,
                f"Unable to bind synthesized parameter `{key}`: missing submitted workflow parameter and literal binding is ambiguous.",
            )
            continue

        if direct_fill_usage.matched:
            parameters.append(_required_string_parameter_row(key))
            continue

        literal, error = _safe_singleton_literal_for_parameter(submitted_code, key, typed_length)
        if error:
            add_binding_violation(key, error)
            continue
        parameters.append(
            {
                "parameter_type": "workflow",
                "workflow_parameter_type": "string",
                "key": key,
                "default_value": literal,
            }
        )

    code_block["parameter_keys"] = parameter_keys
    # Submitted synthesized parameter rows are re-derived from scout evidence before persistence.
    code_block.pop("parameters", None)
    return _SynthesizedParameterReconciliation(parameter_keys, violations, aliases, repair_context)


def _synthesized_durable_stage_codes(synthesized: SynthesizedCodeBlock, *, source_code: str | None = None) -> list[str]:
    steps = getattr(synthesized, "steps", None)
    if not isinstance(steps, list) or len(steps) < 2:
        return []
    lines = textwrap.dedent(source_code if source_code is not None else synthesized.code).strip("\n").splitlines()
    if not lines:
        return []
    ranges: list[tuple[int, int]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            return []
        start = _coerce_positive_int(step.get("line_start"))
        end = _coerce_positive_int(step.get("line_end"))
        if start is None or end is None or end < start or end > len(lines):
            return []
        ranges.append((start, end))
    if ranges != sorted(ranges):
        return []
    # The witness prelude (runtime charset guards + month helper) sits above the first step range, so the
    # slice would drop it; each separated stage is an independent CodeBlock that must carry its own guards.
    prelude = "\n".join(lines[: ranges[0][0] - 1]).strip()
    stage_codes: list[str] = []
    for start, end in ranges:
        body = "\n".join(lines[start - 1 : end]).strip()
        if not body:
            continue
        stage_codes.append(f"{prelude}\n{body}" if prelude else body)
    return stage_codes


def _top_level_block_list_for_selected_code_block(
    parsed: Mapping[str, Any],
    code_block: Mapping[str, Any],
) -> tuple[list[Any], int] | None:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return None
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list):
        return None
    matches = [index for index, block in enumerate(blocks) if block is code_block]
    if len(matches) != 1:
        return None
    return blocks, matches[0]


def _browser_stage_label(base_label: str, index: int) -> str:
    safe_base = re.sub(r"[^0-9A-Za-z_]+", "_", base_label).strip("_") or "browser"
    return f"{safe_base}_browser_stage_{index}"


def _split_selected_output_owner_into_browser_stages(
    *,
    parsed: Mapping[str, Any],
    code_block: dict[str, Any],
    synthesized: SynthesizedCodeBlock,
    synthesized_code: str,
    extraction_code: str,
    parameter_keys: list[str],
) -> list[str]:
    block_position = _top_level_block_list_for_selected_code_block(parsed, code_block)
    if block_position is None:
        return ["Unable to impose synthesized code block: selected output block insertion point is ambiguous."]
    blocks, selected_index = block_position
    stage_codes = _synthesized_durable_stage_codes(synthesized, source_code=synthesized_code)
    if len(stage_codes) < 2:
        return ["Unable to impose synthesized code block: synthesized browser stage boundaries are ambiguous."]
    output_label = str(code_block.get("label") or "").strip()
    if not output_label:
        return ["Unable to impose synthesized code block: output owner block label is missing."]
    parsed_blocks = _workflow_code_blocks(parsed) if isinstance(parsed, dict) else []
    existing_labels = {
        str(block.get("label") or "").strip()
        for block in parsed_blocks
        if isinstance(block, Mapping) and block is not code_block
    }
    stage_labels = [_browser_stage_label(output_label, index) for index in range(1, len(stage_codes) + 1)]
    if len(set(stage_labels)) != len(stage_labels) or any(label in existing_labels for label in stage_labels):
        return ["Unable to impose synthesized code block: synthesized browser stage label would collide."]
    stage_blocks: list[dict[str, Any]] = []
    for label, code in zip(stage_labels, stage_codes):
        stage_block: dict[str, Any] = {
            "block_type": "code",
            "label": label,
            "code": code.rstrip() + "\n",
        }
        if parameter_keys:
            stage_block["parameter_keys"] = list(parameter_keys)
        stage_blocks.append(stage_block)
    code_block["code"] = textwrap.dedent(extraction_code).strip() + "\n"
    code_block.pop("parameter_keys", None)
    blocks[selected_index : selected_index + 1] = [*stage_blocks, code_block]
    return []


def _co_computed_metadata_repair_contract(
    ctx: AgentContext,
    workflow_yaml: str,
    raw_metadata: object,
) -> dict[str, object] | None:
    # Derive the missing-metadata block labels the same way the save-path reject does, so the repair
    # hint names the actually-uncovered block(s) rather than the imposed carrier in a multi-block workflow.
    missing_labels = _missing_code_artifact_metadata_labels(workflow_yaml, ctx, raw_metadata)
    if not missing_labels:
        return None
    required_paths, source, reason_code = _required_child_output_paths_for_authoring(ctx)
    return _metadata_repair_contract(
        block_labels=missing_labels,
        required_paths=required_paths,
        source=source,
        reason_code=reason_code,
    )


def _maybe_impose_synthesized_code_block(
    workflow_yaml: str,
    ctx: AgentContext,
    runtime_parameters: Mapping[str, Any] | None = None,
) -> _SynthesizedCodeImpositionResult:
    ctx.pending_goal_complete_landing = False
    ctx.pending_requested_output_extraction_candidate = None
    ctx.spine_imposition_owned_attempt = False
    ctx.spine_imposition_carrier_label = None
    ctx.imposition_synthesized_block = None
    ctx.authoring_parameter_binding_snapshot = None
    if ctx.turn_origin != TurnOrigin.interactive:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if not getattr(ctx, "impose_synthesized_code_block", False):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    reaches_goal = synthesized_trajectory_reaches_goal(ctx)
    # Goal-completeness gates on the requested-output plan, which materializes mid-turn, so keying availability on
    # it would make ownership race the plan; reach is monotone in the scout's capture. Landing still closes the lane.
    spine_landing_available = reaches_goal and not ctx.synthesized_goal_complete_landed
    repeated_identical_omission = _current_draft_repeats_prior_scouted_spine_omission(workflow_yaml, ctx)
    if (
        ctx.update_workflow_called
        and not spine_landing_available
        and not _should_impose_after_update_attempt(ctx, repeated_identical_omission=repeated_identical_omission)
    ):
        _log_imposition_skipped_after_update(ctx)
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if ctx.update_workflow_called:
        _log_imposition_admitted_after_update(ctx)

    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(scout_trajectory, list) or not scout_trajectory:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    first_source_url = str(scout_trajectory[0].get("source_url") or "").strip()
    if not first_source_url:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)

    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    requirement = ctx.recorded_outcome_grounding_requirement
    constraint = ctx.recorded_outcome_binding_constraint
    code_blocks = _workflow_code_blocks(parsed)
    prior_source, prior_yaml = _prior_yaml_source(ctx)
    code_block = _select_synthesized_imposition_code_block(
        code_blocks,
        prior_yaml=prior_yaml,
        preferred_labels=_recorded_outcome_imposition_block_labels(ctx),
    )
    if code_block is None and reaches_goal:
        code_block = _spine_carrier_code_block(code_blocks, prior_yaml=prior_yaml)
    if code_block is None:
        if reaches_goal:
            LOG.info("copilot_spine_imposition_no_carrier", code_block_count=len(code_blocks))
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    submitted_code = str(code_block.get("code") or "")
    ctx.spine_imposition_carrier_label = _code_block_label(code_block)

    definition_diagnostic = _definition_plane_preflight_reject(ctx, workflow_yaml)
    carrier_label = _code_block_label(code_block)
    pending_directive = _pending_authoring_parameter_binding_directive(ctx)
    active_repair_context, prior_directive = pending_directive or (None, None)
    if active_repair_context is not None and active_repair_context.block_label != carrier_label:
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: stored parameter binding directive is stale."],
            repair_context=active_repair_context,
        )
    declared_keys = (
        set(definition_diagnostic.unreferenced_parameter_keys)
        if definition_diagnostic is not None and definition_diagnostic.unreferenced_parameter_keys
        else set(active_repair_context.unresolved_names)
        if active_repair_context is not None
        else set()
    )
    diagnostic_structural_key = (
        prior_directive.structural_key
        if prior_directive is not None
        else _definition_plane_structural_key(
            workflow_yaml,
            definition_diagnostic,
            ctx.raw_code_artifact_metadata,
        )
        if definition_diagnostic is not None
        else ""
    )
    binding_resolution = (
        _authoring_parameter_binding_resolution(
            ctx,
            parsed,
            runtime_parameters,
            unreferenced_parameter_keys=sorted(declared_keys),
            carrier_code=submitted_code,
            diagnostic_structural_key=diagnostic_structural_key,
        )
        if declared_keys
        else _AuthoringParameterBindingResolution(None, None)
    )
    binding_snapshot = binding_resolution.snapshot
    file_match_transform = (
        _same_month_file_match_transform(
            parsed,
            runtime_parameters,
            candidate_keys=sorted(_declared_string_workflow_parameter_keys(parsed)),
            selector=ctx.reached_download_target.selector,
        )
        if ctx.reached_download_target is not None
        and not ctx.reached_download_target.already_registered
        and bool(ctx.reached_download_target.selector)
        else None
    )
    ctx.authoring_parameter_binding_snapshot = binding_snapshot
    if prior_directive is not None and binding_snapshot is None:
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: stored parameter binding directive is stale."],
            repair_context=active_repair_context,
        )
    if binding_snapshot is None and declared_keys and binding_resolution.directive is not None:
        binding_repair_context = CodeAuthoringRepairContext(
            block_label=carrier_label,
            reason_code=_SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE,
            unresolved_names=sorted(declared_keys),
            parameter_keys=sorted(declared_keys),
            available_parameter_keys=sorted(_declared_string_workflow_parameter_keys(parsed)),
            parameter_binding_directive=binding_resolution.directive,
            repair_instruction=(
                "Use each declared workflow key for its exact selector pair, include every key in parameter_keys, "
                "and rerun via update_and_run_blocks."
            ),
        )
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: current-page parameter binding is ambiguous."],
            repair_context=binding_repair_context,
        )
    grounding_repair_active = (
        binding_snapshot is None
        and file_match_transform is None
        and bool(declared_keys)
        and isinstance(requirement, RecordedOutcomeGroundingRequirement)
        and requirement.satisfied
        and isinstance(constraint, RecordedOutcomeBindingConstraint)
        and constraint.repeated_structural_key == requirement.structural_key
    )
    if grounding_repair_active and _stash_unresolved_recorded_outcome_grounding_halt(ctx, declared_keys):
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: current-page parameter binding is unresolved."],
        )
    synthesis_values = _runtime_declared_string_parameter_values(parsed, runtime_parameters)
    synthesis_trajectory: list[Mapping[str, Any]] = []
    for interaction in scout_trajectory:
        enriched_interaction = dict(interaction)
        correspondences = input_correspondences_for_interaction(interaction, synthesis_values)
        if correspondences:
            enriched_interaction["input_correspondences"] = correspondences
        else:
            enriched_interaction.pop("input_correspondences", None)
        synthesis_trajectory.append(enriched_interaction)

    owns_spine = reaches_goal
    ctx.spine_imposition_owned_attempt = owns_spine
    carrier_changed = _submitted_code_block_changed(code_block, prior_yaml)
    if not owns_spine and not carrier_changed:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if owns_spine:
        LOG.info(
            "copilot_spine_imposition_owned_attempt",
            carrier_label=_code_block_label(code_block),
            code_block_count=len(code_blocks),
            carrier_changed=carrier_changed,
        )

    extraction_plan = requested_output_extraction_plan(ctx)
    extraction_candidate_refresh_allowed = synthesized_persistence_reopened(
        ctx
    ) or requested_output_extraction_plan_changed(ctx, extraction_plan)
    synthesized = (
        synthesize_code_block_with_extraction(
            synthesis_trajectory,
            extraction_plan,
            strict_selectors=True,
            reached_download_target=ctx.reached_download_target,
            parameter_binding_snapshot=binding_snapshot,
            file_match_transform=file_match_transform,
        )
        if extraction_plan is not None
        else synthesize_code_block(
            synthesis_trajectory,
            strict_selectors=True,
            reached_download_target=ctx.reached_download_target,
            parameter_binding_snapshot=binding_snapshot,
            file_match_transform=file_match_transform,
        )
    )
    if synthesized is None:
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: scout trajectory produced no runnable code."],
        )
    ctx.imposition_synthesized_block = synthesized
    if synthesized.diagnostics.grounded_submit_binding_fingerprints:
        LOG.info(
            "copilot recorded outcome binding consumed by synthesizer",
            structural_key=requirement.structural_key
            if isinstance(requirement, RecordedOutcomeGroundingRequirement)
            else None,
            binding_fingerprints=synthesized.diagnostics.grounded_submit_binding_fingerprints,
        )
    imposed_candidate: FrozenRequestedOutputExtractionCandidate | None = None
    if extraction_plan is not None:
        candidate = freeze_requested_output_extraction_candidate(synthesized, extraction_plan, source="generated")
        if candidate is None:
            return _SynthesizedCodeImpositionResult(
                workflow_yaml=workflow_yaml,
                violations=["Unable to impose synthesized code block: extraction candidate is incomplete."],
            )
        existing_candidate = ctx.requested_output_extraction_candidate
        if (
            existing_candidate is not None
            and existing_candidate != candidate
            and not extraction_candidate_refresh_allowed
        ):
            return _SynthesizedCodeImpositionResult(
                workflow_yaml=workflow_yaml,
                violations=["Unable to impose synthesized code block: extraction candidate identity changed."],
            )
        imposed_candidate = candidate

    synthesized_spine_code = synthesized.interaction_code or synthesized.code
    if not carrier_changed and not _draft_leaves_scouted_partition_open(
        code_blocks, synthesized=synthesized, scout_trajectory=scout_trajectory
    ):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    replaced_rung_labels: list[str] = []
    if owns_spine:
        stale_rungs = _stale_spine_rung_blocks(code_blocks, carrier=code_block, prior_yaml=prior_yaml)
        if stale_rungs:
            replacement = _drop_stale_spine_rung_blocks(
                parsed,
                stale_rungs,
                carrier_label=_code_block_label(code_block),
                provenance=_stale_spine_rung_provenance(
                    stale_rungs,
                    scouted_mutations=_browser_surface_for_code(synthesized_spine_code)[0],
                    diagnostics=synthesized.diagnostics,
                ),
            )
            if replacement.violation is not None:
                return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml, violations=[replacement.violation])
            replaced_rung_labels = replacement.replaced_labels
            code_blocks = [block for block in code_blocks if all(block is not stale for stale in stale_rungs)]
    if _separated_spine_already_imposed(code_blocks, code_block, synthesized_spine_code):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)

    diagnostics = synthesized.diagnostics
    violations: list[str] = []
    if diagnostics.truncated:
        violations.append("Unable to impose synthesized code block: scout trajectory was truncated.")
    claimed_refiner_indices: set[int] = set()
    forgiven_superseded_bare_drops: list[dict[str, Any]] = []
    forgiven_entry_opener_drops: list[dict[str, Any]] = []
    repair_context: CodeAuthoringRepairContext | None = None
    for dropped in diagnostics.dropped_interactions:
        if _is_ignorable_entry_opener_drop(dropped, diagnostics):
            entry_opener_record = {
                "trajectory_index": dropped.get("trajectory_index"),
                "tool_name": str(dropped.get("tool_name") or ""),
                "reason_code": str(dropped.get("reason_code") or ""),
                "selector": str(dropped.get("selector") or "").strip(),
                "forgiveness": "entry_opener_superseded_by_locator_provenance",
            }
            forgiven_entry_opener_drops.append(entry_opener_record)
            LOG.info("copilot_imposition_forgave_entry_opener_drop", **entry_opener_record)
            continue
        forgiven, refiner_record = _bare_drop_superseded_on_screen(
            dropped, scout_trajectory, claimed_refiner_indices=claimed_refiner_indices
        )
        if forgiven and refiner_record is not None:
            forgiven_superseded_bare_drops.append(refiner_record)
            LOG.info("copilot_imposition_forgave_superseded_bare_drop", **refiner_record)
            continue
        reason = str(dropped.get("reason_code") or "unknown")
        tool_name = str(dropped.get("tool_name") or "unknown")
        index = dropped.get("trajectory_index", "?")
        violations.append(
            f"Unable to impose synthesized code block: dropped scout interaction {index} from `{tool_name}` ({reason})."
        )
        if repair_context is None:
            repair_context = _ambiguous_bare_selector_repair_context(
                code_block=code_block,
                dropped=dropped,
                scout_trajectory=scout_trajectory,
            )
    for provenance in diagnostics.locator_provenance:
        if not _locator_provenance_is_self_validating(provenance):
            violations.append("Unable to impose synthesized code block: locator provenance was not byte-equal.")
            break
    surface_validation = _whole_trajectory_browser_surface_violations(
        code_blocks=code_blocks,
        selected_code_block=code_block,
        submitted_selected_code=submitted_code,
        synthesized_code=synthesized_spine_code,
        prior_yaml=prior_yaml,
        synthesized_diagnostics=diagnostics,
    )
    violations.extend(surface_validation.violations)
    rejection_provenance = list(surface_validation.provenance)
    ambiguous_reject_present = any(record.kind == "ambiguous" for record in surface_validation.provenance)
    submitted_extraction_suffix = _submitted_suffix_after_synthesized_code(submitted_code, synthesized_spine_code)
    extraction_suffix = submitted_extraction_suffix or synthesized.extraction_code
    extraction_candidate_source = "submitted" if submitted_extraction_suffix else "generated"
    if (
        submitted_extraction_suffix
        and synthesized.extraction_code
        and submitted_extraction_suffix.strip() != synthesized.extraction_code.strip()
    ):
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=[
                "Unable to impose synthesized code block: submitted extraction candidate does not execute the "
                "captured requested-output plan recipe."
            ],
        )
    if extraction_suffix:
        suffix_mutations, _, suffix_ambiguous = _browser_surface_for_code(extraction_suffix)
        selected_label = _code_block_label(code_block)
        if suffix_mutations:
            action_text = ", ".join(f"{mutation.receiver}.{mutation.method}" for mutation in sorted(suffix_mutations))
            synthesized_mutations, _, _ = _browser_surface_for_code(synthesized.code)
            synthesized_signatures = set(synthesized_mutations)
            suffix_provenance = [
                _BrowserSurfaceRejectionProvenance(
                    kind="suffix_disallowed",
                    action=f"{mutation.receiver}.{mutation.method}",
                    site="extraction_suffix",
                    block_label=selected_label,
                    nearest_method=mutation.method,
                    nearest_receiver=mutation.receiver,
                    nearest_selector=_captured_selector_for_signature(mutation, diagnostics),
                    mutation=mutation,
                )
                if mutation in synthesized_signatures
                else _classify_unscouted_mutation(
                    mutation,
                    scouted_mutations=synthesized_mutations,
                    diagnostics=diagnostics,
                    site="extraction_suffix",
                    block_label=selected_label,
                )
                for mutation in sorted(suffix_mutations)
            ]
            _log_browser_surface_rejection_provenance(suffix_provenance)
            rejection_provenance.extend(suffix_provenance)
            violations.append(
                "Unable to impose synthesized code block: extraction suffix contains unscouted browser action(s): "
                + action_text
                + "."
                + _provenance_suffix_text(suffix_provenance)
            )
        if suffix_ambiguous:
            ambiguous_reject_present = True
            suffix_ambiguous_provenance = [
                _ambiguous_browser_action_provenance(action, site="extraction_suffix", block_label=selected_label)
                for action in suffix_ambiguous
            ]
            _log_browser_surface_rejection_provenance(suffix_ambiguous_provenance)
            violations.append(
                "Unable to impose synthesized code block: extraction suffix contains ambiguous browser action(s): "
                + ", ".join(suffix_ambiguous)
                + "."
                + _provenance_suffix_text(suffix_ambiguous_provenance)
            )

    parameter_reconciliation = _reconcile_synthesized_parameters(
        parsed=parsed,
        code_block=code_block,
        submitted_code=submitted_code,
        synthesized_parameters=synthesized.parameters,
        scout_trajectory=scout_trajectory,
        parameter_binding_snapshot=binding_snapshot,
    )
    parameter_binding_is_only_violation = not violations and bool(parameter_reconciliation.violations)
    violations.extend(parameter_reconciliation.violations)
    if repair_context is None:
        repair_context = parameter_reconciliation.repair_context
    raw_metadata = getattr(ctx, "raw_code_artifact_metadata", None)
    if violations:
        persisted_calls = ctx.persisted_draft_browser_calls
        if ambiguous_reject_present and ctx.update_workflow_called and persisted_calls is not None:
            open_obligation = uncovered_required_emitted_interactions(diagnostics.emitted_interactions, persisted_calls)
            artifact = render_missing_rung_call_sources(open_obligation)
            if artifact:
                violations.append(_missing_scouted_rung_violation_text(artifact))
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=violations,
            repair_context=repair_context,
            metadata_repair_contract=_co_computed_metadata_repair_contract(ctx, workflow_yaml, raw_metadata),
            never_captured_candidate=_never_captured_obligation_candidate(rejection_provenance),
            ablation_gate_id=(
                SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID
                if parameter_binding_is_only_violation
                and repair_context is not None
                and repair_context.reason_code == _SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE
                else None
            ),
        )

    metadata_declares_goal_values = bool(raw_metadata) and _raw_metadata_declares_goal_values_for_block(
        raw_metadata, str(code_block.get("label") or "")
    )
    reconciled_submitted_code = _apply_parameter_reconciliation_to_code(
        textwrap.dedent(submitted_code), parameter_reconciliation
    )
    reconciled_synthesized_code = _apply_parameter_reconciliation_to_code(
        textwrap.dedent(synthesized_spine_code), parameter_reconciliation
    )
    submitted_contains_full_spine = _browser_surface_contains_full_action_spine(
        reconciled_submitted_code, reconciled_synthesized_code
    )
    assigned_submitted_parameter_keys = _assigned_submitted_parameter_keys(
        reconciled_submitted_code,
        parameter_reconciliation.parameter_keys,
    )
    if metadata_declares_goal_values and submitted_contains_full_spine and assigned_submitted_parameter_keys:
        joined = ", ".join(f"`{key}`" for key in assigned_submitted_parameter_keys)
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=[
                "Unable to impose synthesized code block: submitted extraction code assigns synthesized "
                f"parameter key(s) {joined} before the scout browser spine."
            ],
            repair_context=repair_context,
        )
    selected_mutations, _, selected_ambiguous = _browser_surface_for_code(submitted_code)
    preserve_submitted_extraction = (
        metadata_declares_goal_values
        and submitted_contains_full_spine
        and not extraction_suffix
        and not _is_submitted_code_synthesized_only(submitted_code, synthesized_spine_code)
    )
    append_selected_extraction = (
        metadata_declares_goal_values
        and not submitted_contains_full_spine
        and not selected_mutations
        and not selected_ambiguous
    )
    scrubbed_selected_metadata_label = (
        str(code_block.get("label") or "")
        if metadata_declares_goal_values
        and not preserve_submitted_extraction
        and not extraction_suffix
        and not append_selected_extraction
        else None
    )
    credential_parameter_keys = [
        str(param.get("key") or "") for param in synthesized.parameters if str(param.get("credential_id") or "").strip()
    ]
    substitutions = {
        "block_label": str(code_block.get("label") or ""),
        "source_trajectory_count": len(scout_trajectory),
        "parameter_keys": parameter_reconciliation.parameter_keys,
        "credential_parameter_keys": credential_parameter_keys,
        "selector_provenance": _public_locator_provenance(diagnostics.locator_provenance),
        "prior_source": prior_source,
    }
    if extraction_plan is not None:
        substitutions.update(
            {
                "extraction_plan_identity": extraction_plan.identity,
                "extraction_candidate_fingerprint": synthesized.extraction_fingerprint,
                "extraction_candidate_source": extraction_candidate_source,
            }
        )
    reconciled_extraction_suffix = (
        _submitted_suffix_after_synthesized_code(reconciled_submitted_code, reconciled_synthesized_code)
        if preserve_submitted_extraction
        else ""
    )
    split_extraction_code = ""
    if extraction_suffix:
        split_extraction_code = extraction_suffix
    elif append_selected_extraction:
        split_extraction_code = textwrap.dedent(submitted_code).strip()
    elif preserve_submitted_extraction and reconciled_extraction_suffix:
        split_extraction_code = reconciled_extraction_suffix
    durable_stage_codes = _synthesized_durable_stage_codes(synthesized, source_code=reconciled_synthesized_code)
    should_split_output_owner = (
        metadata_declares_goal_values
        and len(durable_stage_codes) >= 2
        and (bool(extraction_suffix) or append_selected_extraction or preserve_submitted_extraction)
    )
    if should_split_output_owner:
        if not split_extraction_code:
            return _SynthesizedCodeImpositionResult(
                workflow_yaml=workflow_yaml,
                violations=[
                    "Unable to impose synthesized code block: selected output extraction boundary is ambiguous."
                ],
                repair_context=repair_context,
            )
        split_extraction_code = _apply_parameter_reconciliation_to_code(split_extraction_code, parameter_reconciliation)
        target_metadata = _metadata_item_for_block_label(raw_metadata, str(code_block.get("label") or ""))
        required_split_paths = _metadata_item_goal_value_paths(target_metadata)
        if required_split_paths:
            split_extraction_code, static_return_violations = _extraction_code_with_value_bearing_static_return(
                split_extraction_code,
                required_paths=required_split_paths,
            )
            if static_return_violations:
                return _SynthesizedCodeImpositionResult(
                    workflow_yaml=workflow_yaml,
                    violations=static_return_violations,
                    repair_context=repair_context,
                )
        split_violations = _split_selected_output_owner_into_browser_stages(
            parsed=parsed,
            code_block=code_block,
            synthesized=synthesized,
            synthesized_code=reconciled_synthesized_code,
            extraction_code=split_extraction_code,
            parameter_keys=parameter_reconciliation.parameter_keys,
        )
        if split_violations:
            return _SynthesizedCodeImpositionResult(
                workflow_yaml=workflow_yaml,
                violations=split_violations,
                repair_context=repair_context,
            )
        split_under_build = _scouted_spine_under_build_result(
            workflow_yaml,
            ctx=ctx,
            synthesized=synthesized,
            draft_codes=[*durable_stage_codes, split_extraction_code],
            block_label=str(code_block.get("label") or ""),
            site="separated_split",
        )
        if split_under_build is not None:
            return split_under_build
        substitutions["separated_browser_stage_count"] = len(durable_stage_codes)
    else:
        imposed_code = textwrap.dedent(
            submitted_code if preserve_submitted_extraction else synthesized_spine_code
        ).lstrip("\n")
        if extraction_suffix:
            imposed_code = imposed_code.rstrip() + "\n" + extraction_suffix.rstrip() + "\n"
        elif append_selected_extraction:
            imposed_code = imposed_code.rstrip() + "\n" + textwrap.dedent(submitted_code).strip() + "\n"
        imposed_code = _apply_parameter_reconciliation_to_code(imposed_code, parameter_reconciliation)
        under_build = _scouted_spine_under_build_result(
            workflow_yaml,
            ctx=ctx,
            synthesized=synthesized,
            draft_codes=[imposed_code],
            block_label=str(code_block.get("label") or ""),
        )
        if under_build is not None:
            return under_build
        code_block["code"] = imposed_code
    if replaced_rung_labels:
        substitutions["replaced_hand_authored_browser_rungs"] = replaced_rung_labels
        substitutions["browser_coverage_route"] = _ADMISSIBLE_BROWSER_COVERAGE_ROUTE
    if parameter_reconciliation.aliases:
        substitutions["parameter_aliases"] = parameter_reconciliation.aliases
    if extraction_suffix:
        substitutions["preserved_extraction_suffix"] = True
    if preserve_submitted_extraction:
        substitutions["preserved_submitted_extraction_code"] = True
    if scrubbed_selected_metadata_label:
        substitutions["scrubbed_stale_selected_goal_value_paths"] = True
    if forgiven_superseded_bare_drops:
        substitutions["forgiven_superseded_bare_drops"] = forgiven_superseded_bare_drops
    if forgiven_entry_opener_drops:
        substitutions["forgiven_entry_opener_drops"] = forgiven_entry_opener_drops
    selected_extraction_metadata_disposition: SelectedExtractionMetadataDisposition = "none"
    if scrubbed_selected_metadata_label:
        selected_extraction_metadata_disposition = "browser_spine_replaced_metadata_stale"
    elif preserve_submitted_extraction:
        selected_extraction_metadata_disposition = "self_authored_extraction_preserved"
    elif extraction_suffix or append_selected_extraction:
        selected_extraction_metadata_disposition = "sibling_or_suffix_extraction_preserved"
    if prior_directive is not None and binding_snapshot is not None:
        binding_artifacts: list[tuple[str, Sequence[str]]] = []
        for final_block in _workflow_code_blocks(parsed):
            raw_parameter_keys = final_block.get("parameter_keys")
            final_parameter_keys = (
                tuple(str(key) for key in raw_parameter_keys if isinstance(key, str) and key)
                if isinstance(raw_parameter_keys, list)
                else ()
            )
            binding_artifacts.append((str(final_block.get("code") or ""), final_parameter_keys))
        if not any(
            authoring_parameter_binding_directive_consumed(
                prior_directive,
                binding_snapshot,
                code=block_code,
                parameter_keys=block_parameter_keys,
            )
            for block_code, block_parameter_keys in binding_artifacts
        ):
            return _SynthesizedCodeImpositionResult(
                workflow_yaml=workflow_yaml,
                violations=["Unable to impose synthesized code block: stored parameter binding directive is stale."],
                repair_context=active_repair_context,
            )
        LOG.info(
            "copilot authoring parameter binding directive consumed",
            binding_fingerprint=binding_snapshot.fingerprint,
            directive_fingerprint=prior_directive.fingerprint,
            parameter_keys=[binding.declared_key for binding in binding_snapshot.field_bindings],
        )
    if extraction_plan is not None:
        LOG.info(
            "copilot_requested_output_extraction_candidate_imposed",
            canonical_paths=list(extraction_plan.requested_output_paths),
            extraction_plan_identity=extraction_plan.identity,
            observation_step=extraction_plan.observation_step,
            candidate_source=extraction_candidate_source,
            candidate_fingerprint=synthesized.extraction_fingerprint,
        )
    ctx.pending_requested_output_extraction_candidate = imposed_candidate
    if synthesized_trajectory_is_goal_complete(ctx):
        ctx.pending_goal_complete_landing = True
    return _SynthesizedCodeImpositionResult(
        workflow_yaml=yaml.safe_dump(parsed, sort_keys=False),
        substitutions=substitutions,
        scrubbed_selected_metadata_label=scrubbed_selected_metadata_label,
        selected_extraction_metadata_disposition=selected_extraction_metadata_disposition,
        minted_parameter_keys=list(parameter_reconciliation.parameter_keys),
    )


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


_EXPECT_DOWNLOAD_ATTR = "expect_download"


def _call_is_expect_download(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        return _call_is_expect_download(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == _EXPECT_DOWNLOAD_ATTR
    return False


def _code_uses_expect_download(code: str) -> bool:
    """True only for the registering form: `expect_download()` called as the context
    expression of an `async with`/`with`. A bare `page.expect_download` attribute or an
    uncaptured call fires no download, so it does not count."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncWith, ast.With)):
            for item in node.items:
                if _call_is_expect_download(item.context_expr):
                    return True
    return False


def _dict_keys(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Await):
        return _dict_keys(node.value)
    if isinstance(node, ast.Dict):
        return {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    return set()


_REGISTERED_DOWNLOAD_OUTPUT_KEY_SET = frozenset(REGISTERED_DOWNLOAD_OUTPUT_KEYS)
_DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET = frozenset({"downloaded_file_path", "download_url"})


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


def _code_returns_download_descriptor_leak_keys(code: str) -> bool:
    """Detect simple top-level descriptor returns; execution-registered artifacts remain
    the authoritative download proof."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False
    leak_key_locals: set[str] = set()
    for node in _iter_top_level_scope(tree.body):
        if isinstance(node, ast.Return) and node.value is not None:
            if _dict_keys(node.value) & _DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET:
                return True
            if isinstance(node.value, ast.Name) and node.value.id in leak_key_locals:
                return True
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if _dict_keys(node.value) & _DOWNLOAD_DESCRIPTOR_LEAK_KEY_SET:
                leak_key_locals.add(node.targets[0].id)
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
    a block is download-intent when it carries the expect_download idiom, self-asserts a
    registration key in a top-level dict, or declares a registration key as a goal path."""
    if not code.strip():
        return False
    return (
        _code_uses_expect_download(code)
        or _code_returns_registration_keys(code)
        or _artifact_declares_registration_keys(artifact)
    )


def _download_return_shape_error(label: str, artifact: Mapping[str, Any], code: str) -> str | None:
    """Fail closed on a download-intent code block that cannot register a real download:
    one lacking the `page.expect_download` idiom (no browser download ever fires), or one
    whose return writes the execution-layer-owned registration keys (self-certification)."""
    if not _is_download_intent(artifact, code):
        return None
    if not _code_uses_expect_download(code):
        return (
            f"Code block `{label}` is a download block but does not fire the browser download with the "
            "`page.expect_download` idiom (async with page.expect_download() as dl_info: await "
            "page.click(<unique selector>)). A static fetch or a plain click registers no file. Author the "
            "expect_download idiom against a unique target so the runtime registers the file into the workflow "
            "output (downloaded_files); never place file bytes or URLs in the chat reply."
        )
    if _code_returns_registration_keys(code):
        return (
            f"Code block `{label}` `return`s the download registration keys "
            f"({', '.join(REGISTERED_DOWNLOAD_OUTPUT_KEYS)}) itself. The execution layer injects those keys when a "
            "browser download fires; writing them self-certifies a download that may never have happened. Return a "
            'small keyed descriptor instead (for example `return {"saved_as": dl_info.value.suggested_filename}`) and '
            "let the expect_download idiom register the file."
        )
    if _code_returns_download_descriptor_leak_keys(code):
        return (
            f"Code block `{label}` returns raw download path/URL descriptor keys. Return only non-sensitive summary "
            "fields such as the suggested filename; the execution layer injects artifact URLs and IDs."
        )
    return None


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


def _credentialed_code_block_scout_gate_errors(
    workflow_yaml: str | None,
    ctx: AgentContext,
    *,
    block_labels: Iterable[str] | None = None,
) -> list[str]:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return []
    if not workflow_yaml:
        return []
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return []
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return []
    credential_params_by_key = credential_param_ids(workflow_definition.get("parameters"))
    if not credential_params_by_key:
        return []
    prior_blocks_by_label = {}
    prior_credential_params_by_key = {}
    prior_workflow_yaml = ctx.workflow_yaml
    if isinstance(prior_workflow_yaml, str) and prior_workflow_yaml.strip():
        prior_parsed = parse_workflow_yaml(prior_workflow_yaml)
        if isinstance(prior_parsed, dict):
            prior_workflow_definition = prior_parsed.get("workflow_definition")
            if isinstance(prior_workflow_definition, dict):
                prior_credential_params_by_key = credential_param_ids(prior_workflow_definition.get("parameters"))
                for prior_block in workflow_blocks(prior_parsed):
                    if _enum_or_string_name(prior_block.get("block_type")) != BlockType.CODE.value:
                        continue
                    prior_label = str(prior_block.get("label") or "").strip()
                    if prior_label:
                        prior_blocks_by_label[prior_label] = prior_block
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(scout_trajectory, list):
        scout_trajectory = []
    selected_block_labels = {label.strip() for label in block_labels or [] if label.strip()}

    errors: list[str] = []
    for block in workflow_blocks(parsed):
        if _enum_or_string_name(block.get("block_type")) != BlockType.CODE.value:
            continue
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        block_label = str(block.get("label") or "").strip()
        if selected_block_labels and block_label not in selected_block_labels:
            continue
        matching_prior_block = prior_blocks_by_label.get(block_label) if block_label else None
        if (
            isinstance(matching_prior_block, dict)
            and str(matching_prior_block.get("code") or "") == code
            and _code_block_parameter_keys(matching_prior_block) == _code_block_parameter_keys(block)
        ):
            accessed_parameter_keys = {
                access.parameter_key for access in _credential_field_accesses(code) if access.requires_live_scout
            }
            if accessed_parameter_keys and all(
                prior_credential_params_by_key.get(parameter_key) == credential_params_by_key.get(parameter_key)
                for parameter_key in accessed_parameter_keys
            ):
                continue
        required_fields_by_parameter: dict[str, tuple[set[str], set[str]]] = {}
        for access in _credential_field_accesses(code):
            if not access.requires_live_scout:
                continue
            credential_ids = credential_params_by_key.get(access.parameter_key)
            if credential_ids:
                allowed_credential_ids, required_fields = required_fields_by_parameter.setdefault(
                    access.parameter_key, (credential_ids, set())
                )
                required_fields.add(access.field)
        if not required_fields_by_parameter:
            continue

        gap = credential_scout_gap(
            scout_trajectory,
            list(required_fields_by_parameter.values()),
            requires_submit=bool(_CODE_SUBMIT_ACTION_RE.search(code)),
        )
        missing_fields = gap.missing_fields
        missing_submit = gap.missing_submit

        if not missing_fields and not missing_submit:
            continue

        block_label = block_label or "this code block"
        requirements: list[str] = []
        if missing_fields:
            joined_fields = ", ".join(f"`{field}`" for field in missing_fields)
            requirements.append(f"successful `fill_credential_field` scouting for {joined_fields}")
        if missing_submit:
            requirements.append("a later submit action on the same page")
        requirement_text = " and ".join(requirements)
        errors.append(
            f"Code block `{block_label}` reads saved credential fields, but the current debug-browser scout "
            f"record is missing {requirement_text}. First scout the live form with `fill_credential_field` for "
            "each referenced credential field, then click the submit control or press Enter in the debug browser "
            "before retrying `update_workflow` or `update_and_run_blocks`."
        )
    return errors


def _missing_scouted_rung_violation_text(artifact: str) -> str:
    return "The persisted draft is missing scouted rung(s). " + artifact


def _open_scouted_spine_obligation_artifact(ctx: AgentContext) -> str:
    try:
        findings = _scouted_spine_open_obligation(ctx)
    except Exception:
        LOG.warning("copilot_scouted_spine_obligation_read_failed", exc_info=True)
        return ""
    artifact = render_missing_rung_call_sources(uncovered_rung_records(findings))
    if not artifact:
        return ""
    return _missing_scouted_rung_violation_text(artifact)


def _credential_scout_reject_error_text(ctx: AgentContext, credential_scout_errors: list[str]) -> str:
    error_text = "\n".join(credential_scout_errors)
    obligation_artifact = _open_scouted_spine_obligation_artifact(ctx)
    if obligation_artifact:
        error_text += "\n" + obligation_artifact
    return error_text


def _reject_schema_incompatibility(
    ctx: AgentContext,
    incompatibility: SchemaIncompatibility,
    reject: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Emit the typed, non-repairable schema-incompatibility outcome: stash the terminal
    blocker + turn halt so enforcement renders a product-language reply (instead of
    falling through to repair churn), record it on the context, and reject without
    persisting the incompatible draft."""
    signal = build_schema_incompatibility_blocker_signal(incompatibility)
    _record_author_time_reject_outcome(
        ctx,
        reason_code="schema_incompatibility",
        summary=signal.user_facing_reason,
        structural_payload=incompatibility.to_summary_dict(),
        block_labels=[incompatibility.block_label],
    )
    stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="workflow_update")
    ctx.latest_schema_incompatibility = incompatibility
    return reject(
        error=render_schema_incompatibility_agent_steer(incompatibility),
        user_facing_summary=signal.user_facing_reason,
        data={
            "failure_type": SCHEMA_INCOMPATIBILITY_FAILURE_TYPE,
            "schema_incompatibility": incompatibility.to_summary_dict(),
        },
    )


def _declared_string_parameter_values(workflow_yaml: str) -> dict[str, str]:
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return {}
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return {}
    parameters = workflow_definition.get("parameters")
    if not isinstance(parameters, list):
        return {}
    values: dict[str, str] = {}
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            continue
        key = str(parameter.get("key") or "").strip()
        if not key:
            continue
        default_value = _string_parameter_default_value(parameter)
        if isinstance(default_value, str) and default_value:
            values[key] = default_value
    return values


def _enrich_scout_trajectory_input_correspondences(workflow_yaml: str, ctx: AgentContext) -> None:
    """Match the submitted workflow's declared string-parameter values against each scout click's captured
    literals and stamp the correspondence onto the trajectory once, so every synthesize_code_block call reads
    it. Idempotent; empty declared params clears prior stamps."""
    scout_trajectory = ctx.scout_trajectory
    if not scout_trajectory:
        return
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return
    declared_params = _declared_string_parameter_values(workflow_yaml)
    for interaction in scout_trajectory:
        if not isinstance(interaction, dict):
            continue
        correspondences = input_correspondences_for_interaction(interaction, declared_params) if declared_params else []
        if correspondences:
            interaction["input_correspondences"] = correspondences
        else:
            interaction.pop("input_correspondences", None)


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
    allow_static_output_uncertainty: bool = False,
    formation_prepared: bool = False,
) -> dict[str, Any]:
    ctx.output_contract_bail_actuated_this_call = False

    def reject(
        *,
        error: str,
        user_facing_summary: str | None = None,
        data: dict[str, Any] | None = None,
        repair_context: CodeAuthoringRepairContext | None = None,
        record_repair_context_outcome: bool = True,
    ) -> dict[str, Any]:
        if repair_context is None:
            _clear_code_authoring_repair_context(ctx)
        elif record_repair_context_outcome:
            _set_code_authoring_repair_context(ctx, repair_context)
        else:
            ctx.last_code_authoring_repair_context = repair_context
        result: dict[str, Any] = {"ok": False, "error": error}
        if user_facing_summary is not None:
            result["user_facing_summary"] = user_facing_summary
        if data is not None:
            result["data"] = data
        return result

    authority_error = _authority_tool_error(ctx, "update_workflow")
    if authority_error is not None:
        return reject(error=authority_error)

    workflow_yaml = params["workflow_yaml"]
    never_captured_replay_payload = NeverCapturedReplayPayload(
        params=copy.deepcopy(params),
        allow_missing_credentials=allow_missing_credentials,
        allow_static_output_uncertainty=allow_static_output_uncertainty,
        formation_prepared=formation_prepared,
    )
    raw_conflict_marker_error = _raw_workflow_yaml_conflict_marker_error(workflow_yaml)
    if raw_conflict_marker_error is not None:
        return reject(
            error=raw_conflict_marker_error,
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    # Tool wrappers run authority/loop guards before calling here. The composition
    # gate below consumes these refs, so they must be visible before validation.
    ctx.raw_block_observation_refs = params.get("raw_block_observation_refs", params.get("block_observation_refs"))
    ctx.block_observation_refs = normalize_block_observation_refs(params.get("block_observation_refs"))
    ctx.raw_code_artifact_metadata = params.get("raw_code_artifact_metadata", params.get("code_artifact_metadata"))
    ctx.submitted_code_artifact_metadata_snapshot = copy.deepcopy(params.get("code_artifact_metadata"))
    # Imposition reconciles synthesized aliases/parameters before the persisted YAML contract is checked.
    _enrich_scout_trajectory_input_correspondences(workflow_yaml, ctx)
    runtime_parameters = params.get("parameters")
    capture_obligation = ctx.never_captured_obligation
    if (
        capture_obligation is not None
        and capture_obligation.state == "captured"
        and capture_obligation.draft_fingerprint != hashlib.sha256(workflow_yaml.encode()).hexdigest()
    ):
        ctx.never_captured_obligation = replace(capture_obligation, state="consumed")
        ctx.synthesized_block_reopened_for_capture_obligation = False
        LOG.info(
            "copilot_never_captured_obligation_expired_for_different_draft",
            identity_digest=capture_obligation.identity_digest,
        )
    imposition = _maybe_impose_synthesized_code_block(
        workflow_yaml,
        ctx,
        runtime_parameters if isinstance(runtime_parameters, Mapping) else None,
    )
    capture_obligation = ctx.never_captured_obligation
    if capture_obligation is not None and capture_obligation.state == "captured":
        ctx.never_captured_obligation = replace(capture_obligation, state="consumed")
    ctx.synthesized_block_reopened_for_capture_obligation = False
    # Consume the one-shot credential-scout reopen before the gate below so a fresh reject can re-arm it.
    ctx.synthesized_block_reopened_for_credential_scout = False
    if imposition.violations:
        if (
            imposition.ablation_gate_id == SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_GATE_ID
            and imposition.repair_context is not None
            and _record_synthesized_parameter_binding_ablation_event(
                ctx,
                workflow_yaml,
                imposition.repair_context,
            )
        ):
            imposition = _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
        else:
            _arm_never_captured_obligation(
                ctx,
                workflow_yaml,
                imposition.never_captured_candidate,
                never_captured_replay_payload,
            )
            if (
                imposition.repair_context is not None
                and imposition.repair_context.reason_code == _SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE
            ):
                required_paths = _output_contract_required_paths_source(ctx).union
                _record_output_contract_family_reject(
                    ctx,
                    required_paths,
                    reject_family=_SYNTHESIZED_PARAMETER_BINDING_AMBIGUOUS_REASON_CODE,
                )
            if (
                imposition.repair_context is not None
                and imposition.repair_context.reason_code in _SCOUTED_SPINE_REASON_CODES
            ):
                _set_code_authoring_repair_context(ctx, imposition.repair_context)
                _record_code_authoring_guardrail_reject(ctx)
                return reject(
                    error="\n".join(imposition.violations),
                    user_facing_summary=_compiled_authoring_user_summary(),
                    data=_code_repair_progress_data(imposition.repair_context),
                    repair_context=imposition.repair_context,
                    record_repair_context_outcome=False,
                )
            return reject(
                error="\n".join(imposition.violations),
                user_facing_summary=_compiled_authoring_user_summary(),
                data=_code_repair_progress_data(
                    imposition.repair_context,
                    metadata_repair_contract=imposition.metadata_repair_contract,
                ),
                repair_context=imposition.repair_context,
            )
    workflow_yaml = imposition.workflow_yaml
    if imposition.substitutions is None:
        freehand_surface = _persist_seam_freehand_surface_result(workflow_yaml, ctx)
        if freehand_surface is not None:
            _arm_never_captured_obligation(
                ctx,
                workflow_yaml,
                freehand_surface.never_captured_candidate,
                never_captured_replay_payload,
            )
            _set_code_authoring_repair_context(ctx, freehand_surface.repair_context)
            _record_code_authoring_guardrail_reject(ctx)
            return reject(
                error="\n".join(freehand_surface.violations),
                user_facing_summary=_compiled_authoring_user_summary(),
                data=_code_repair_progress_data(freehand_surface.repair_context),
                repair_context=freehand_surface.repair_context,
                record_repair_context_outcome=False,
            )
    stripped_sandbox_imports: list[str] = []
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        workflow_yaml, stripped_sandbox_imports = _strip_redundant_sandbox_imports_in_yaml(workflow_yaml)
    workflow_yaml, typed_default_violations, promoted_parameter_keys = _apply_scouted_typed_default_promotions(
        workflow_yaml, ctx
    )
    if typed_default_violations:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(
            error="\n".join(typed_default_violations),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        workflow_yaml = _adopt_exact_declared_parameter_keys_for_unresolved_names(workflow_yaml)
    final_structural_violations = _final_yaml_structural_violations(
        ctx,
        workflow_yaml,
        minted_parameter_keys=imposition.minted_parameter_keys,
        promoted_parameter_keys=promoted_parameter_keys,
        carried_by_imposition=imposition.substitutions is not None,
    )
    if final_structural_violations:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(
            error="\n".join(final_structural_violations),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    business_input_floor_reject = _synthesized_business_input_floor_reject(ctx, workflow_yaml, imposition)
    if business_input_floor_reject is not None:
        unreferenced_keys = list(business_input_floor_reject.unreferenced_parameter_keys)
        ctx.synthesized_business_required_parameter_keys.update(unreferenced_keys)
        _record_author_time_reject_outcome(
            ctx,
            reason_code="required_input_unbound",
            summary=_definition_plane_reject_error(business_input_floor_reject),
            structural_payload={
                "reason_code": SYNTHESIZED_BUSINESS_INPUT_FLOOR_REASON_CODE,
                "unreferenced_parameter_keys": unreferenced_keys,
            },
            block_labels=[
                _code_block_label(block) for block in _workflow_code_blocks(parse_workflow_yaml(workflow_yaml) or {})
            ],
        )
        _record_code_authoring_guardrail_reject(ctx)
        _reopen_scout_after_synthesized_business_input_floor(ctx)
        return reject(
            error=(
                _definition_plane_reject_error(business_input_floor_reject)
                + " Continue scouting the business-value fields before saving or running this synthesized block."
            ),
            user_facing_summary=_compiled_authoring_user_summary(),
            data={
                "reason_code": SYNTHESIZED_BUSINESS_INPUT_FLOOR_REASON_CODE,
                "unreferenced_parameter_keys": unreferenced_keys,
            },
        )
    definition_reject = _definition_plane_preflight_reject(ctx, workflow_yaml)
    if definition_reject is not None:
        definition_corrective = (
            None
            if copilot_author_time_gate_log_only_enabled(ctx, DEFINITION_CONTRACT_UNSATISFIED_GATE_ID)
            else _value_bearing_definition_reject_corrective(
                ctx,
                workflow_yaml=workflow_yaml,
                raw_metadata=ctx.raw_code_artifact_metadata,
            )
        )
        definition_data = {
            "reason_code": "definition_contract_unsatisfied",
            "definition_criterion_ids": list(definition_reject.criterion_ids),
            "definition_reason_codes": list(definition_reject.reason_codes),
            "unreferenced_parameter_keys": list(definition_reject.unreferenced_parameter_keys),
            **(definition_corrective or {}),
        }
        halted = _stash_unresolved_recorded_outcome_grounding_halt(
            ctx,
            definition_reject.unreferenced_parameter_keys,
        )
        if halted:
            return reject(
                error=_definition_plane_reject_error(definition_reject),
                user_facing_summary=_compiled_authoring_user_summary(),
                data=definition_data,
            )
        if not _record_definition_plane_ablation_event(
            ctx,
            workflow_yaml,
            definition_reject,
            code_artifact_metadata=ctx.raw_code_artifact_metadata,
        ):
            _record_definition_plane_reject(
                ctx,
                workflow_yaml,
                definition_reject,
                code_artifact_metadata=ctx.raw_code_artifact_metadata,
            )
            return reject(
                error=_definition_plane_reject_error(definition_reject),
                user_facing_summary=_compiled_authoring_user_summary(),
                data=definition_data,
            )
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        unrenderable = _code_block_render_reject(workflow_yaml, getattr(ctx, "workflow_yaml", None))
        if unrenderable is not None:
            unrenderable_label, render_diagnostic = unrenderable
            render_error = (
                f"Code block `{unrenderable_label}` contains a Jinja expression the workflow runtime "
                f"cannot render. {render_diagnostic.message}"
            )
            LOG.info(
                "copilot code block render preflight reject",
                reason_code="code_block_unrenderable",
                block_label=unrenderable_label,
                failing_expression=render_diagnostic.failing_expression,
            )
            _record_author_time_reject_outcome(
                ctx,
                reason_code="code_block_unrenderable",
                summary=render_error,
                structural_payload={
                    "reason_code": "code_block_unrenderable",
                    "block_label": unrenderable_label,
                    "failing_expression": render_diagnostic.failing_expression,
                },
                block_labels=[unrenderable_label],
            )
            _record_code_authoring_guardrail_reject(ctx)
            return reject(
                error=render_error,
                user_facing_summary=_compiled_authoring_user_summary(),
                data={
                    "reason_code": "code_block_unrenderable",
                    "block_label": unrenderable_label,
                    "failing_expression": render_diagnostic.failing_expression,
                },
            )
    params["workflow_yaml"] = workflow_yaml
    metadata_scrubbed_by_imposition = False
    if (
        imposition.selected_extraction_metadata_disposition == "browser_spine_replaced_metadata_stale"
        and imposition.scrubbed_selected_metadata_label
    ):
        scrubbed_metadata = _downgrade_stale_selected_goal_value_paths(
            params.get("code_artifact_metadata"),
            imposition.scrubbed_selected_metadata_label,
        )
        params["code_artifact_metadata"] = scrubbed_metadata
        ctx.raw_code_artifact_metadata = scrubbed_metadata
        metadata_scrubbed_by_imposition = True
    parameter_contract_error = _code_block_parameter_contract_error(workflow_yaml)
    if parameter_contract_error is not None:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(
            error=parameter_contract_error,
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    workflow_yaml, imposed_metadata, envelope_imposed = (
        (workflow_yaml, params.get("code_artifact_metadata"), False)
        if formation_prepared and imposition.substitutions is None
        else _impose_output_contract_envelope_after_steering(
            ctx,
            workflow_yaml,
            params.get("code_artifact_metadata"),
        )
    )
    if envelope_imposed:
        params["workflow_yaml"] = workflow_yaml
        params["code_artifact_metadata"] = imposed_metadata
        params["raw_code_artifact_metadata"] = imposed_metadata
        ctx.raw_code_artifact_metadata = imposed_metadata
    scaffolded_metadata, scaffold_applied = (
        (params.get("code_artifact_metadata"), False)
        if formation_prepared and not metadata_scrubbed_by_imposition
        else _scaffold_metadata_contract_for_update(
            ctx,
            workflow_yaml,
            params.get("code_artifact_metadata"),
        )
    )
    if scaffold_applied:
        params["code_artifact_metadata"] = scaffolded_metadata
        params["raw_code_artifact_metadata"] = scaffolded_metadata
        ctx.raw_code_artifact_metadata = scaffolded_metadata
    (
        required_child_output_paths,
        output_path_coverage_source,
        output_path_coverage_reason_code,
    ) = _required_child_output_paths_for_authoring(ctx)
    output_contract_evaluation = _evaluate_output_contract_for_code_block(
        ctx,
        workflow_yaml,
        params.get("code_artifact_metadata"),
        allow_static_return_advisory=allow_static_output_uncertainty,
        enforce_value_bearing_liveness=True,
    )
    if (
        output_contract_evaluation is not None
        and _OUTPUT_CONTRACT_VALUE_REQUIRED_REASON_CODE in output_contract_evaluation.shape_violations
    ):
        _revoke_output_contract_advisory_run(ctx, output_contract_evaluation.canonical_signature)
    output_contract_static_advisory_allowed = (
        output_contract_evaluation is not None and output_contract_evaluation.can_attempt_run
    )
    if (
        output_contract_evaluation is not None
        and output_contract_evaluation.has_deficiencies
        and not output_contract_static_advisory_allowed
    ):
        authored_fingerprint = _output_contract_structural_fingerprint(
            workflow_yaml, output_contract_evaluation.canonical_signature
        )
        if not _record_output_contract_ablation_event(
            ctx,
            output_contract_evaluation,
            gate_id=_OUTPUT_CONTRACT_ABLATION_GATE_ID,
            blocked_tool="update_workflow",
            fingerprint=authored_fingerprint,
        ):
            payload = _record_output_contract_reject(
                ctx,
                output_contract_evaluation,
                summary="Submitted workflow does not satisfy the requested output contract.",
                authored_structural_fingerprint=authored_fingerprint,
                workflow_yaml=workflow_yaml,
                raw_metadata=params.get("code_artifact_metadata"),
            )
            if allow_static_output_uncertainty and _output_contract_advisory_granted(
                ctx, output_contract_evaluation.canonical_signature
            ):
                _arm_pending_run_evidence(
                    ctx,
                    output_contract_evaluation.canonical_signature,
                    set(output_contract_evaluation.observation_paths),
                )
            else:
                reject_result = _output_contract_reject_result(
                    output_contract_evaluation,
                    payload=payload,
                    tool_name="update_workflow",
                )
                _record_code_authoring_guardrail_reject(ctx)
                return reject(
                    error=str(reject_result["error"]),
                    user_facing_summary=str(
                        reject_result.get("user_facing_summary") or _compiled_authoring_user_summary()
                    ),
                    data=reject_result.get("data") if isinstance(reject_result.get("data"), dict) else None,
                    repair_context=output_contract_evaluation.repair_context,
                    record_repair_context_outcome=False,
                )
    typed_output_owner_contract_complete = (
        formation_prepared
        and output_contract_evaluation is not None
        and not output_contract_evaluation.has_deficiencies
    )
    # update_and_run_blocks already resolved the exact output owner and validated its typed
    # contract. Do not let the legacy output-intent scan reclassify a non-owner browser block
    # as another output owner. Save-only update_workflow retains the conservative label gate.
    missing_metadata_error = (
        None
        if typed_output_owner_contract_complete
        else _missing_code_artifact_metadata_error(
            workflow_yaml,
            ctx,
            params.get("code_artifact_metadata"),
        )
    )
    missing_labels = (
        []
        if missing_metadata_error is None
        else _missing_code_artifact_metadata_labels(workflow_yaml, ctx, params.get("code_artifact_metadata"))
    )
    metadata_reject_ablated = False
    if missing_metadata_error is not None and _record_output_contract_actuation_ablation_event(
        ctx,
        workflow_yaml,
        reason_code=_MISSING_CODE_ARTIFACT_METADATA_REJECT_FAMILY,
        signature=(output_contract_evaluation.canonical_signature if output_contract_evaluation is not None else ""),
        block_labels=missing_labels,
        required_paths=required_child_output_paths,
    ):
        missing_metadata_error = None
        metadata_reject_ablated = True
    if missing_metadata_error is not None:
        missing_metadata_output_facts = _missing_requested_output_facts(
            required_child_output_paths,
            reason_code=output_path_coverage_reason_code,
        )
        if required_child_output_paths:
            missing_metadata_error = (
                f"{missing_metadata_error}\nRequired requested output paths: "
                f"{', '.join(sorted(required_child_output_paths))}"
            )
        metadata_repair_context = _metadata_output_repair_context(
            block_labels=missing_labels,
            required_paths=required_child_output_paths,
            coverage_reason_code=output_path_coverage_reason_code,
            source=output_path_coverage_source,
            summary=missing_metadata_error,
        )
        metadata_repair_contract = _metadata_repair_contract(
            block_labels=missing_labels,
            required_paths=required_child_output_paths,
            source=output_path_coverage_source,
            reason_code=output_path_coverage_reason_code,
        )
        metadata_reject_payload = _code_artifact_metadata_reject_payload(
            workflow_yaml=workflow_yaml,
            raw_metadata=params.get("code_artifact_metadata"),
            offending_labels=[],
            missing_labels=missing_labels,
            violation_categories=["missing_code_artifact_metadata"],
        )
        candidate_outcome = _build_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary=missing_metadata_error,
            structural_payload=metadata_reject_payload,
            block_labels=missing_labels,
            missing_requested_output_facts=missing_metadata_output_facts,
        )
        missing_fields_by_label = _metadata_missing_required_fields_by_label(
            params.get("code_artifact_metadata"),
            labels=missing_labels,
            missing_labels=missing_labels,
        )
        if len(missing_labels) != 1:
            _reset_metadata_reject_ladder_state(
                ctx,
                superseding_reject_family="multi_label_missing_code_artifact_metadata",
            )
        metadata_ladder_decision = (
            None
            if len(missing_labels) != 1
            or copilot_author_time_gate_log_only_enabled(ctx, _OUTPUT_CONTRACT_ABLATION_GATE_ID)
            else _metadata_reject_ladder_decision(
                ctx,
                candidate_outcome=candidate_outcome,
                reject_family="missing_code_artifact_metadata",
                missing_fields_by_label=missing_fields_by_label,
            )
        )
        if metadata_ladder_decision is not None and metadata_ladder_decision.action == "terminal":
            assert ctx.blocker_signal is not None
            return reject(
                error=ctx.blocker_signal.user_facing_reason,
                user_facing_summary=ctx.blocker_signal.user_facing_reason,
                data=_metadata_reject_terminal_data(metadata_ladder_decision),
                repair_context=metadata_repair_context,
                record_repair_context_outcome=False,
            )
        missing_metadata_reject_count = _record_output_contract_family_reject(
            ctx,
            required_child_output_paths,
            reject_family=_MISSING_CODE_ARTIFACT_METADATA_REJECT_FAMILY,
        )
        record_build_test_outcome(ctx, candidate_outcome)
        _capture_rejected_code_artifact_metadata(ctx)
        credential_scout_errors = (
            []
            if _request_policy_allows_untested_code_block_draft(ctx)
            else _credentialed_code_block_scout_gate_errors(
                workflow_yaml,
                ctx,
                block_labels=params.get("block_labels"),
            )
        )
        budget_terminal = _adjudicate_output_contract_budget(
            ctx,
            required_child_output_paths,
            count=missing_metadata_reject_count,
            block_label=missing_labels[0] if len(missing_labels) == 1 else "",
        )
        if credential_scout_errors:
            _record_code_authoring_guardrail_reject(ctx, defer_churn_stop=True)
        elif missing_metadata_reject_count < 1 and not budget_terminal:
            _record_code_authoring_guardrail_reject(ctx)
        metadata_signature = _output_contract_signature(ctx=ctx, required_paths=required_child_output_paths)
        if metadata_ladder_decision is not None and metadata_ladder_decision.rung is not None:
            _record_armed_directive_fingerprint(
                ctx,
                metadata_signature,
                _metadata_reject_seam_fingerprint(
                    workflow_yaml,
                    metadata_signature,
                    missing_fields_by_label,
                ),
            )
        metadata_convergence_directive = (
            _emit_metadata_convergence_directive(
                signature=metadata_signature,
                block_label=missing_labels[0],
                missing_fields_by_label=missing_fields_by_label,
                required_paths=required_child_output_paths,
                escalate=metadata_ladder_decision.rung == 2,
            )
            if metadata_ladder_decision is not None
            and metadata_ladder_decision.rung is not None
            and len(missing_labels) == 1
            else None
        )
        metadata_reject_data = _code_repair_progress_data(
            metadata_repair_context,
            missing_requested_output_facts=missing_metadata_output_facts,
            metadata_repair_contract=metadata_repair_contract,
        )
        if metadata_convergence_directive is not None:
            metadata_reject_data["metadata_convergence_directive"] = metadata_convergence_directive
        return reject(
            error=missing_metadata_error,
            user_facing_summary=_compiled_authoring_user_summary(),
            data=metadata_reject_data,
            repair_context=metadata_repair_context,
            record_repair_context_outcome=False,
        )
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    normalization = _normalize_code_artifact_metadata_detailed(
        params.get("code_artifact_metadata"),
        workflow_yaml,
        impose_defaults=_copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        scout_trajectory=scout_trajectory if isinstance(scout_trajectory, list) else None,
        verified_runtime_output_paths_by_label=_verified_runtime_output_contract_paths_by_label(ctx, workflow_yaml),
        advisory_declared_output_return_shape_labels=(
            {output_contract_evaluation.block_label}
            if output_contract_static_advisory_allowed and output_contract_evaluation is not None
            else None
        ),
    )
    code_artifact_metadata = normalization.normalized
    code_artifact_metadata_error = normalization.error
    # A schema incompatibility rejects below regardless, so suppressing here would emit an
    # ablation event for a save that never lands and overstate what the flag let through.
    if (
        code_artifact_metadata_error is not None
        and not normalization.schema_incompatibilities
        and (
            metadata_reject_ablated
            or _record_output_contract_actuation_ablation_event(
                ctx,
                workflow_yaml,
                reason_code=_METADATA_NORMALIZATION_REJECT_FAMILY,
                signature=(
                    output_contract_evaluation.canonical_signature if output_contract_evaluation is not None else ""
                ),
                block_labels=normalization.offending_labels,
                required_paths=required_child_output_paths,
            )
        )
    ):
        code_artifact_metadata_error = None
    if code_artifact_metadata_error is not None:
        _reset_metadata_reject_ladder_state(
            ctx,
            superseding_reject_family=_METADATA_NORMALIZATION_REJECT_FAMILY,
        )
        record_code_artifact_violations(ctx, normalization.violations, normalization.offending_labels)
        normalization_reject_count = _record_output_contract_family_reject(
            ctx,
            required_child_output_paths,
            reject_family=_METADATA_NORMALIZATION_REJECT_FAMILY,
        )
        _record_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary=code_artifact_metadata_error,
            structural_payload=_output_contract_author_time_structural_payload(
                ctx,
                required_child_output_paths,
                block_label=normalization.offending_labels[0] if len(normalization.offending_labels) == 1 else "",
            )
            or _code_artifact_metadata_reject_payload(
                workflow_yaml=workflow_yaml,
                raw_metadata=params.get("code_artifact_metadata"),
                offending_labels=normalization.offending_labels,
                violation_categories=_metadata_violation_categories(normalization.violations),
            ),
            block_labels=normalization.offending_labels,
        )
        _capture_rejected_code_artifact_metadata(ctx)
        _adjudicate_output_contract_budget(
            ctx,
            required_child_output_paths,
            count=normalization_reject_count,
            block_label=normalization.offending_labels[0] if len(normalization.offending_labels) == 1 else "",
        )
    if normalization.schema_incompatibilities:
        incompatibility = merge_schema_incompatibilities(normalization.schema_incompatibilities)
        if incompatibility is not None:
            return _reject_schema_incompatibility(ctx, incompatibility, reject)
    prior_workflow_yaml = getattr(ctx, "workflow_yaml", None)
    code_safety_errors = _code_block_safety_errors(workflow_yaml, prior_workflow_yaml)
    code_authoring_repair_context = (
        _code_block_authoring_repair_context(workflow_yaml, prior_workflow_yaml)
        if code_safety_errors and _unresolved_symbol_repair_context_enabled(ctx)
        else None
    )
    credential_scout_errors = (
        []
        if _request_policy_allows_untested_code_block_draft(ctx)
        else _credentialed_code_block_scout_gate_errors(workflow_yaml, ctx, block_labels=params.get("block_labels"))
    )
    unresolved_symbol_priority_reject = _is_unresolved_symbol_repair_context(code_authoring_repair_context)
    credential_priority_reject = (
        bool(credential_scout_errors) and code_artifact_metadata_error is None and not unresolved_symbol_priority_reject
    )
    if code_safety_errors:
        _set_code_authoring_repair_context(ctx, code_authoring_repair_context)
        if code_authoring_repair_context is None:
            _record_author_time_reject_outcome(
                ctx,
                reason_code="code_safety_reject",
                summary="Code authoring guardrail rejected the submitted code block.",
                structural_payload=_code_safety_reject_payload(code_safety_errors),
            )
        _record_code_authoring_guardrail_reject(ctx, defer_churn_stop=credential_priority_reject)
    # Per-label salvage keeps conforming metadata across a rejection; a
    # rejected code block keeps nothing, since its yaml never becomes the
    # draft. Prior-draft labels survive every rejection gate below — the
    # accept path prunes to the submitted blocks once the draft switches.
    if code_artifact_metadata and not code_safety_errors:
        existing_metadata = getattr(ctx, "code_artifact_metadata", None)
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
        workflow_yaml = _apply_metadata_contract_schema_to_workflow_yaml(ctx, workflow_yaml, merged_metadata)
        params["workflow_yaml"] = workflow_yaml
    if (
        credential_scout_errors
        and code_safety_errors
        and code_artifact_metadata_error is None
        and unresolved_symbol_priority_reject
    ):
        return reject(
            error="\n".join(str(error) for error in code_safety_errors if error),
            user_facing_summary=_code_seam_rejection_user_summary(
                metadata_rejected=False,
                code_rejected=True,
            ),
            data=_code_repair_progress_data(code_authoring_repair_context),
            repair_context=code_authoring_repair_context,
        )
    if credential_scout_errors and code_safety_errors and code_artifact_metadata_error is None:
        arm_credential_scout_reopen(ctx, _credential_scout_reopen_identity_digest(workflow_yaml))
        return reject(
            error=_credential_scout_reject_error_text(ctx, credential_scout_errors),
            user_facing_summary=CREDENTIAL_SCOUT_VERIFY_REPLY,
            data={
                "failure_type": "missing_credential_or_init",
                "diagnostic_code_safety_errors": code_safety_errors,
                **(
                    {"authoring_repair_context": code_authoring_repair_context.model_dump(mode="json")}
                    if code_authoring_repair_context is not None
                    else {}
                ),
            },
            repair_context=code_authoring_repair_context,
        )
    seam_errors = [
        error
        for error in (
            code_artifact_metadata_error,
            *_human_facing_code_safety_errors(code_safety_errors),
        )
        if error
    ]
    if seam_errors:
        return reject(
            error="\n".join(seam_errors),
            user_facing_summary=_code_seam_rejection_user_summary(
                metadata_rejected=code_artifact_metadata_error is not None,
                code_rejected=bool(code_safety_errors),
            ),
            data=_code_repair_progress_data(code_authoring_repair_context),
            repair_context=code_authoring_repair_context,
        )
    if credential_scout_errors:
        _record_author_time_reject_outcome(
            ctx,
            reason_code="credential_scout_reject",
            summary=CREDENTIAL_SCOUT_VERIFY_REPLY,
            structural_payload=_credential_scout_reject_payload(workflow_yaml),
        )
        _record_code_authoring_guardrail_reject(ctx, defer_churn_stop=True)
        arm_credential_scout_reopen(ctx, _credential_scout_reopen_identity_digest(workflow_yaml))
        return reject(
            error=_credential_scout_reject_error_text(ctx, credential_scout_errors),
            user_facing_summary=CREDENTIAL_SCOUT_VERIFY_REPLY,
            data={"failure_type": "missing_credential_or_init"},
        )
    if allow_missing_credentials is None:
        allow_missing_credentials = getattr(ctx, "allow_untested_workflow_draft", False) is True
    if not allow_missing_credentials:
        credential_error = await _credential_reference_validation_error(workflow_yaml, ctx)
        if credential_error is not None:
            return reject(error=credential_error)

    misbinding_findings = _credential_id_misbinding_findings(workflow_yaml)
    if misbinding_findings:
        LOG.info(
            "copilot credential id misbinding rejected",
            organization_id=ctx.organization_id,
            workflow_id=ctx.workflow_id,
            findings=misbinding_findings,
        )
        return reject(error=_credential_id_misbinding_error_message(misbinding_findings))

    output_policy_verdict = evaluate_output_policy(
        request_policy=getattr(ctx, "request_policy", None),
        workflow_yaml=workflow_yaml,
        tool_arguments=params,
    )
    if not output_policy_verdict.allowed:
        output_policy_trace_data = output_policy_verdict_to_trace_data(
            output_policy_verdict,
            surface="tool_body",
            tool_name="update_workflow",
        )
        output_policy_error = format_output_policy_tool_error(output_policy_verdict)
        _record_code_only_raw_secret_reject_span(ctx, output_policy_verdict)
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
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=output_policy_error)

    # Prefer the most-recent in-turn emission so cross-path flows (inline
    # REPLACE_WORKFLOW followed by update_workflow) compare against what the
    # LLM actually saw, not the turn-start persisted state.
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    prior_yaml = last_yaml if isinstance(last_yaml, str) and last_yaml else ctx.workflow_yaml
    stale_metadata = _detect_stale_block_metadata(workflow_yaml, prior_yaml)
    if stale_metadata:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=_stale_block_metadata_message(stale_metadata))

    wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
    if wait_block_error:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=wait_block_error)

    challenge_http_error = _challenge_http_request_reject_message(ctx, workflow_yaml, ctx.workflow_yaml)
    if challenge_http_error:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=challenge_http_error)

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
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=_banned_block_reject_message(banned_items, ctx))

    download_scout_error = _download_scout_required_error(ctx, workflow_yaml)
    if download_scout_error:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=download_scout_error)

    download_binding_error = _download_binding_required_error(ctx, workflow_yaml)
    if download_binding_error:
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=download_binding_error)

    composition_evidence_error = composition_page_evidence_error(ctx, workflow_yaml)
    if composition_evidence_error:
        LOG.info(
            "copilot composition page evidence rejected workflow",
            workflow_permanent_id=ctx.workflow_permanent_id,
            target_url=workflow_target_url(workflow_yaml),
        )
        _record_code_authoring_guardrail_reject(ctx)
        return reject(error=composition_evidence_error)

    # New data-write blocks default to surfacing failures rather than swallowing them.
    workflow_yaml = default_data_write_continue_on_failure(workflow_yaml, ctx.workflow_yaml)

    convergence_reject = _recorded_outcome_convergence_reject(
        ctx,
        workflow_yaml=workflow_yaml,
        code_artifact_metadata=getattr(ctx, "code_artifact_metadata", None),
    )
    if convergence_reject is not None:
        block_labels = sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml))
        _record_author_time_reject_outcome(
            ctx,
            reason_code="unchanged_after_recorded_outcome",
            summary="The authored code and output structure are unchanged after the last recorded test outcome.",
            structural_payload={
                "reason_code": "unchanged_after_recorded_outcome",
                "authored_structure_signature": convergence_reject.authored_structure_signature,
                "block_labels": block_labels,
            },
            authored_structure_signature=convergence_reject.authored_structure_signature,
            block_labels=block_labels,
        )
        _record_code_authoring_guardrail_reject(
            ctx, frontier_unchanged=convergence_reject.reason == "frontier_unchanged"
        )
        LOG.info(
            "copilot recorded outcome convergence behavior",
            convergence_reason=convergence_reject.reason,
            commit_early_terminal=convergence_reject.commit_early_terminal,
            block_labels=block_labels,
        )
        if convergence_reject.commit_early_terminal:
            _commit_recorded_outcome_early_terminal(ctx)
        return reject(
            error=(
                "Submitted workflow left the frontier the last recorded test outcome named unchanged. "
                "Revise the code block or output metadata that owns that frontier before testing again."
            ),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )

    recorded_output_paths_required = output_path_coverage_source == "recorded_outcome"
    recorded_missing_output_paths = (
        _recorded_outcome_missing_output_paths(ctx) if recorded_output_paths_required else set()
    )
    unresolved_recorded_output_paths = recorded_missing_output_paths

    output_empty_labels = (
        _output_empty_code_block_labels(workflow_yaml, getattr(ctx, "code_artifact_metadata", None))
        if recorded_output_paths_required and (not recorded_missing_output_paths or unresolved_recorded_output_paths)
        else []
    )
    if output_empty_labels:
        authored_structure_signature = authored_structure_signature_from_workflow(
            workflow_yaml,
            getattr(ctx, "code_artifact_metadata", None),
        )
        _record_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary="Submitted workflow does not return any keyed output after the last recorded test outcome.",
            structural_payload={
                "reason_code": "recorded_outcome_requires_output_candidate",
                "authored_structure_signature": authored_structure_signature,
                "empty_output_block_labels": output_empty_labels,
                "recorded_reason_code": "outcome_not_demonstrated",
            },
            authored_structure_signature=authored_structure_signature,
            block_labels=output_empty_labels,
        )
        _record_code_authoring_guardrail_reject(ctx)
        reject_data = _code_repair_progress_data()
        metadata_convergence_directive = _synthesized_metadata_reject_directive(
            ctx,
            workflow_yaml=workflow_yaml,
            raw_metadata=ctx.code_artifact_metadata,
            label_candidates=output_empty_labels,
            required_paths=unresolved_recorded_output_paths,
        )
        if (
            metadata_convergence_directive is not None
            and metadata_convergence_directive.get("reason_code") == METADATA_REJECT_SAME_KEY_TERMINAL_REASON_CODE
        ):
            assert ctx.blocker_signal is not None
            return reject(
                error=ctx.blocker_signal.user_facing_reason,
                user_facing_summary=ctx.blocker_signal.user_facing_reason,
                data=metadata_convergence_directive,
                record_repair_context_outcome=False,
            )
        if metadata_convergence_directive is not None:
            reject_data["metadata_convergence_directive"] = metadata_convergence_directive
        return reject(
            error=(
                "Submitted workflow does not return any keyed output after the last recorded test outcome. "
                "Add structured output for the unsatisfied completion criteria before testing again."
            ),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=reject_data,
        )

    missing_output_paths = (
        _candidate_missing_required_output_paths(
            workflow_yaml,
            getattr(ctx, "code_artifact_metadata", None),
            required_paths=unresolved_recorded_output_paths,
        )
        if recorded_output_paths_required
        else []
    )
    if missing_output_paths:
        authored_structure_signature = authored_structure_signature_from_workflow(
            workflow_yaml,
            getattr(ctx, "code_artifact_metadata", None),
        )
        block_labels = sorted(_workflow_yaml_code_blocks_by_label(workflow_yaml))
        missing_output_roots = sorted(
            {root for path in missing_output_paths if (root := _top_level_path_segment(path))}
        )
        _record_author_time_reject_outcome(
            ctx,
            reason_code="metadata_reject",
            summary=(
                "Submitted workflow does not cover the missing requested output paths "
                "from the last recorded test outcome."
            ),
            structural_payload={
                "reason_code": "recorded_outcome_missing_output_coverage",
                "authored_structure_signature": authored_structure_signature,
                "missing_output_paths": missing_output_paths,
                "missing_output_roots": missing_output_roots,
                "block_labels": block_labels,
                "recorded_reason_code": "outcome_not_demonstrated",
            },
            authored_structure_signature=authored_structure_signature,
            block_labels=block_labels,
            missing_requested_output_facts=[
                {
                    "output_path": path,
                    "output_root": _top_level_path_segment(path),
                    "reason_code": "recorded_outcome_missing_output_coverage",
                    "value_status": "no_typed_value",
                }
                for path in missing_output_paths
            ],
        )
        _record_code_authoring_guardrail_reject(ctx)
        missing_path_text = ", ".join(missing_output_paths[:8])
        reject_data = _code_repair_progress_data()
        metadata_convergence_directive = _synthesized_metadata_reject_directive(
            ctx,
            workflow_yaml=workflow_yaml,
            raw_metadata=ctx.code_artifact_metadata,
            label_candidates=block_labels,
            required_paths=set(missing_output_paths),
        )
        if (
            metadata_convergence_directive is not None
            and metadata_convergence_directive.get("reason_code") == METADATA_REJECT_SAME_KEY_TERMINAL_REASON_CODE
        ):
            assert ctx.blocker_signal is not None
            return reject(
                error=ctx.blocker_signal.user_facing_reason,
                user_facing_summary=ctx.blocker_signal.user_facing_reason,
                data=metadata_convergence_directive,
                record_repair_context_outcome=False,
            )
        if metadata_convergence_directive is not None:
            reject_data["metadata_convergence_directive"] = metadata_convergence_directive
        return reject(
            error=(
                "Submitted workflow does not cover the missing requested output paths from the last recorded test "
                f"outcome: {missing_path_text}. Declare those exact output_path values in goal_value_paths and "
                "produce matching structured output before testing again; output_root is diagnostic only."
            ),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=reject_data,
        )

    select_option_mismatch_context = _select_option_text_click_repair_context(workflow_yaml, ctx)
    if select_option_mismatch_context is not None:
        _set_code_authoring_repair_context(ctx, select_option_mismatch_context)
        _record_code_authoring_guardrail_reject(ctx)
        return {
            "ok": False,
            "error": (
                "Submitted workflow replaces a captured select-option interaction with a text click. "
                "Use the select element API for the captured selector before testing again."
            ),
            "user_facing_summary": _compiled_authoring_user_summary(),
            "data": _code_repair_progress_data(select_option_mismatch_context),
        }

    pre_persist_spine = _pre_persist_scouted_spine_result(workflow_yaml, ctx)
    if pre_persist_spine is not None:
        _record_scouted_spine_omission_reject(ctx, pre_persist_spine.omission_digest)
        _set_code_authoring_repair_context(ctx, pre_persist_spine.repair_context)
        _record_code_authoring_guardrail_reject(ctx)
        return reject(
            error="\n".join(pre_persist_spine.violations),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(pre_persist_spine.repair_context),
            repair_context=pre_persist_spine.repair_context,
            record_repair_context_outcome=False,
        )

    try:
        # A code block renders code-first (goal + plain step timeline) only when it
        # carries a `prompt`; the model authors the goal as artifact `declared_goal`
        # and code regeneration drops it, so carry the goal onto the block here.
        artifact_metadata = getattr(ctx, "code_artifact_metadata", None)
        fallback_goals = {
            label: str(meta["declared_goal"]).strip()
            for label, meta in (artifact_metadata or {}).items()
            if isinstance(meta, dict) and str(meta.get("declared_goal") or "").strip()
        }
        workflow_yaml = fill_code_block_prompts_in_yaml(
            workflow_yaml, prior_yaml=prior_yaml, fallback_goals=fallback_goals
        )
        # Derive plain-language steps from each code block's code so the editor timeline
        # mirrors the actual code (deterministic action_type + line ranges).
        workflow_yaml_with_steps = await apply_derived_code_block_steps(workflow_yaml)
        workflow = await _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml_with_steps,
            settings_fallback_yaml=prior_yaml,
        )
        _record_workflow_proxy_location_span(workflow_yaml, workflow)

        # Param / top-level setting changes go through canonical because
        # prepare_workflow and the runtime parameter-row read consume canonical
        # values; terminal handlers roll back on non-auto-accept.
        prior_workflow = await _get_prior_workflow(ctx)
        requires_canonical_persist = _workflow_requires_canonical_persist(
            prior_workflow, workflow
        ) or _workflow_needs_contract_readback_persist(
            ctx,
            prior_workflow,
            workflow,
            allow_static_output_uncertainty=allow_static_output_uncertainty,
        )
        if requires_canonical_persist:
            created_by_stamp = await resolve_copilot_created_by_stamp(ctx.workflow_id, ctx.organization_id)
            await app.WORKFLOW_SERVICE.update_workflow_definition(
                workflow_id=ctx.workflow_id,
                organization_id=ctx.organization_id,
                title=workflow.title,
                description=workflow.description,
                workflow_definition=workflow.workflow_definition,
                proxy_location=workflow.proxy_location,
                webhook_callback_url=workflow.webhook_callback_url,
                totp_verification_url=workflow.totp_verification_url,
                totp_identifier=workflow.totp_identifier,
                persist_browser_session=workflow.persist_browser_session,
                pin_saved_session_ip=workflow.pin_saved_session_ip,
                browser_profile_id=workflow.browser_profile_id,
                browser_profile_key=workflow.browser_profile_key,
                model=workflow.model,
                max_screenshot_scrolling_times=workflow.max_screenshot_scrolls,
                extra_http_headers=workflow.extra_http_headers,
                cdp_connect_headers=workflow.cdp_connect_headers,
                run_with=workflow.run_with,
                ai_fallback=workflow.ai_fallback,
                cache_key=workflow.cache_key,
                adaptive_caching=workflow.adaptive_caching,
                enable_self_healing=workflow.enable_self_healing,
                code_version=workflow.code_version,
                run_sequentially=workflow.run_sequentially,
                sequential_key=workflow.sequential_key,
                created_by=created_by_stamp,
                edited_by="copilot",
            )
            ctx.canonical_was_persisted_due_to_param_change = True
        ctx.staged_workflow_yaml = workflow_yaml
        ctx.staged_workflow = workflow
        ctx.has_staged_proposal = True
        ctx.workflow_yaml = workflow_yaml
        ctx.persisted_draft_browser_calls = _workflow_yaml_browser_call_pairs(workflow_yaml)
        ctx.code_authoring_guardrail_reject_count = 0
        ctx.scouted_spine_previous_omission_digest = None
        ctx.scouted_spine_repeated_identical_missing_steps = False
        ctx.last_code_authoring_reject_was_credential_priority = False
        ctx.last_output_policy_reject_reason_codes = None
        ctx.metadata_reject_ladder_state = None
        _clear_code_authoring_repair_context(ctx)
        _clear_held_churn_signals(ctx)
        accepted_metadata = getattr(ctx, "code_artifact_metadata", None)
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
        if isinstance(ctx, CopilotContext) and ctx.stream is not None:
            try:
                await maybe_emit_design_end(ctx.stream, ctx)
                await emit_workflow_draft(ctx.stream, ctx, workflow)
            except Exception as emit_err:
                LOG.warning("copilot_narrative_workflow_draft_emit_failed", error=str(emit_err))
        data: dict[str, Any] = {
            "message": "Workflow updated successfully.",
            "block_count": len(workflow.workflow_definition.blocks) if workflow.workflow_definition else 0,
        }
        if imposition.substitutions is not None:
            data["imposed_substitutions"] = imposition.substitutions
        if stripped_sandbox_imports:
            data["stripped_redundant_imports"] = stripped_sandbox_imports
        return {
            "ok": True,
            "data": data,
            "_workflow": workflow,
        }
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        repair_data = None
        user_facing_summary = None
        if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
            repair_data = _code_repair_progress_data()
            user_facing_summary = _code_seam_rejection_user_summary(
                metadata_rejected=False,
                code_rejected=True,
            )
        return reject(
            error=f"{INTERNAL_VALIDATION_FAILURE_PREFIX}{e}",
            user_facing_summary=user_facing_summary,
            data=repair_data,
        )


async def _replay_captured_never_captured_obligation(ctx: AgentContext) -> dict[str, Any] | None:
    """Retry the exact rejected authoring call after its canonical browser action is captured."""
    obligation = getattr(ctx, "never_captured_obligation", None)
    if (
        obligation is None
        or obligation.state != "captured"
        or obligation.turn_id != str(getattr(ctx, "turn_id", ""))
        or obligation.replay_payload is None
    ):
        return None
    payload = obligation.replay_payload
    workflow_yaml = payload.params.get("workflow_yaml")
    if (
        not isinstance(workflow_yaml, str)
        or hashlib.sha256(workflow_yaml.encode()).hexdigest() != obligation.draft_fingerprint
    ):
        ctx.never_captured_obligation = replace(obligation, state="consumed")
        ctx.synthesized_block_reopened_for_capture_obligation = False
        LOG.warning(
            "copilot_never_captured_obligation_replay_payload_invalid",
            identity_digest=obligation.identity_digest,
            turn_id=obligation.turn_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            draft_fingerprint=obligation.draft_fingerprint,
            block_label=obligation.block_label,
            site=obligation.site,
        )
        return None
    prior_definition = getattr(getattr(ctx, "last_workflow", None), "workflow_definition", None)
    try:
        result = await _update_workflow(
            copy.deepcopy(payload.params),
            ctx,
            allow_missing_credentials=payload.allow_missing_credentials,
            allow_static_output_uncertainty=payload.allow_static_output_uncertainty,
            formation_prepared=payload.formation_prepared,
        )
    except Exception:
        current_obligation = getattr(ctx, "never_captured_obligation", None)
        if current_obligation is not None and current_obligation.identity_digest == obligation.identity_digest:
            ctx.never_captured_obligation = obligation
            ctx.synthesized_block_reopened_for_capture_obligation = True
        raise
    current_obligation = ctx.never_captured_obligation
    if current_obligation is not None and current_obligation.state == "captured":
        ctx.never_captured_obligation = replace(current_obligation, state="consumed")
        ctx.synthesized_block_reopened_for_capture_obligation = False
    _record_workflow_update_result(ctx, result, prior_definition)
    data = result.get("data")
    post_replay_obligation = ctx.never_captured_obligation
    obligation_state = (
        post_replay_obligation.state
        if post_replay_obligation is not None and post_replay_obligation.identity_digest == obligation.identity_digest
        else "superseded"
    )
    LOG.info(
        "copilot_never_captured_obligation_replayed",
        identity_digest=obligation.identity_digest,
        turn_id=obligation.turn_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
        draft_fingerprint=obligation.draft_fingerprint,
        block_label=obligation.block_label,
        site=obligation.site,
        captured_trajectory_index=obligation.captured_trajectory_index,
        obligation_state=obligation_state,
        ok=result.get("ok") is True,
        reason_code=data.get("reason_code") if isinstance(data, dict) else None,
    )
    return result


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


def _record_code_only_raw_secret_reject_span(ctx: AgentContext, verdict: OutputPolicyVerdict) -> None:
    if OutputPolicyReason.RAW_SECRET_LEAK not in verdict.reason_codes:
        return
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return
    with copilot_span(
        "update_workflow_code_only_raw_secret_reject",
        data={
            "tool_name": "update_workflow",
            "reason_code": OutputPolicyReason.RAW_SECRET_LEAK.value,
            "block_authoring_policy": BlockAuthoringPolicy.CODE_ONLY_BROWSER.value,
        },
    ):
        pass


def _persisted_landing_leaves_spine_uncovered(ctx: AgentContext) -> bool:
    try:
        return bool(_scouted_spine_open_obligation(ctx))
    except Exception:
        LOG.warning("copilot_landing_spine_coverage_read_failed", exc_info=True)
        return True


def _record_workflow_update_result(
    copilot_ctx: Any, result: dict[str, Any], prior_definition: object | None = None
) -> None:
    if not (result.get("ok") and "_workflow" in result):
        return

    wf = result["_workflow"]
    copilot_ctx.last_workflow = wf
    _clear_resolved_per_tool_budget_problem_labels(copilot_ctx, wf)
    copilot_ctx.last_workflow_yaml = copilot_ctx.workflow_yaml or None
    copilot_ctx.effective_workflow_proxy_location = runtime_proxy_location(getattr(wf, "proxy_location", None))
    data = result.get("data")
    if isinstance(data, dict):
        block_count = data.get("block_count")
        if isinstance(block_count, int):
            copilot_ctx.last_update_block_count = block_count
    copilot_ctx.update_workflow_called = True
    if copilot_ctx.pending_requested_output_extraction_candidate is not None:
        copilot_ctx.requested_output_extraction_candidate = copilot_ctx.pending_requested_output_extraction_candidate
        copilot_ctx.pending_requested_output_extraction_candidate = None
    if copilot_ctx.pending_goal_complete_landing:
        if not _persisted_landing_leaves_spine_uncovered(copilot_ctx):
            copilot_ctx.synthesized_goal_complete_landed = True
        copilot_ctx.pending_goal_complete_landing = False
    copilot_ctx.synthesized_block_reopened_after_failed_run = False
    copilot_ctx.synthesized_block_reopened_for_output_coverage = False
    copilot_ctx.uncovered_output_rescout_steer_key = None
    copilot_ctx.test_after_update_done = False
    copilot_ctx.post_update_nudge_count = 0
    copilot_ctx.last_test_ok = None
    copilot_ctx.last_test_failure_reason = None
    clear_terminal_evidence_on_workflow_edit(copilot_ctx)
    # A fresh workflow edit invalidates the prior test's failure state —
    # otherwise an exhausted POST_UPDATE_NUDGE on the new draft would raise
    # CopilotNonRetriableNavError with the old run's error, telling the user
    # to "verify the URL" for a URL they just corrected in the new draft.
    copilot_ctx.last_test_non_retriable_nav_error = None
    copilot_ctx.non_retriable_nav_error_last_emitted_signature = None

    # Block-running failures keyed off (labels, parameters) go stale once the
    # workflow itself changes — without this clear, a user who fixes the bug
    # via update_workflow gets a LOOP DETECTED on the next legitimate run.
    clear_failed_step_tracker_for_tools_in_ctx(copilot_ctx, BLOCK_RUNNING_TOOLS)

    _invalidate_verified_state_on_edit(copilot_ctx, prior_definition, getattr(wf, "workflow_definition", None))
