from __future__ import annotations

import ast
import io
import json
import keyword
import re
import textwrap
import tokenize
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated, Any, Literal, NamedTuple
from urllib.parse import urlsplit

import structlog
import yaml
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.copilot.attribution import resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    CopilotToolBlockerSignal,
    clear_terminal_evidence_on_workflow_edit,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.code_block_preflight import (
    SANDBOX_UNRESOLVED_NAME_REASON_CODE,
    author_time_code_block_diagnostics,
    sandbox_unresolved_name_diagnostics,
    sandbox_unresolved_name_repair_diagnostic,
    strip_redundant_sandbox_imports,
)
from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError, author_time_code_security_errors
from skyvern.forge.sdk.copilot.code_block_steps import apply_derived_code_block_steps, fill_code_block_prompts_in_yaml
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _BARE_TAG_RE,
    _SYNTHESIZED_BLOCK_LABEL,
    SynthesisDiagnostics,
    _get_by_role_expr_strict,
    _is_positional_selector,
    _parse_role_name,
    artifact_dependency_id,
    artifact_observation_ref_id,
    synthesize_code_block,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    composition_page_evidence_error,
    normalize_block_observation_refs,
    workflow_target_url,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.data_write_defaults import default_data_write_continue_on_failure
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_CODE_AUTHORING_GUARDRAIL_REJECTS,
    MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS,
    code_authoring_churn_stop_signal,
    credential_priority_authoring_churn_stop_signal,
)
from skyvern.forge.sdk.copilot.loop_detection import clear_failed_step_tracker_for_tools_in_ctx
from skyvern.forge.sdk.copilot.narration import CODE_REPAIR_PROGRESS_SURFACE_KIND, CODE_REPAIR_PROGRESS_TEXT
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_code_artifact_violations
from skyvern.forge.sdk.copilot.output_policy import (
    OutputPolicyReason,
    OutputPolicyVerdict,
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
    url_origin,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    REGISTERED_DOWNLOAD_OUTPUT_KEYS,
    ReachedDownloadTarget,
    code_is_download_intent,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext, ScoutedInteraction
from skyvern.forge.sdk.copilot.streaming_adapter import emit_workflow_draft, maybe_emit_design_end
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import blocker_signal_is_genuinely_terminal
from skyvern.forge.sdk.copilot.workflow_credential_utils import credential_params, parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import RESERVED_PARAMETER_KEYS, is_sensitive_workflow_parameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.proxy_location import runtime_proxy_location
from skyvern.schemas.workflows import BlockType

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
_CREDENTIAL_FIELD_ACCESS_RE = re.compile(
    r"\b(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)\.(?:(?P<field>username|password|totp)\b|(?P<otp_method>otp)\s*\()"
)
_CODE_SUBMIT_ACTION_RE = re.compile(r"\.(?:click|press)\s*\(")
_SCOUT_SUBMIT_TOOL_NAMES = frozenset({"click", "press_key"})


class CredentialFieldAccess(NamedTuple):
    parameter_key: str
    field: str
    requires_live_scout: bool


def _credential_field_accesses(code: str) -> list[CredentialFieldAccess]:
    accesses: list[CredentialFieldAccess] = []
    for match in _CREDENTIAL_FIELD_ACCESS_RE.finditer(code):
        field = match.group("field")
        if field:
            accesses.append(
                CredentialFieldAccess(
                    parameter_key=match.group("parameter"),
                    field=field,
                    requires_live_scout=True,
                )
            )
            continue
        if match.group("otp_method"):
            accesses.append(
                CredentialFieldAccess(
                    parameter_key=match.group("parameter"),
                    field="totp",
                    requires_live_scout=False,
                )
            )
    return accesses


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


def _normalize_code_artifact_metadata(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    impose_defaults: bool = False,
    scout_trajectory: list[ScoutedInteraction] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    result = _normalize_code_artifact_metadata_detailed(
        raw_metadata,
        workflow_yaml,
        impose_defaults=impose_defaults,
        scout_trajectory=scout_trajectory,
    )
    return result.normalized, result.error


def _normalize_code_artifact_metadata_detailed(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    impose_defaults: bool = False,
    scout_trajectory: list[ScoutedInteraction] | None = None,
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
    violations: list[str] = []
    offending_labels: list[str] = []
    anchored: list[dict[str, Any]] = []
    unanchored: list[dict[str, Any]] = []
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
        if return_shape_error is not None:
            item_violations.append(return_shape_error)
        schema_conformance_error = _extraction_schema_conformance_error(label, dumped, block_code)
        if schema_conformance_error is not None:
            item_violations.append(schema_conformance_error)
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
            normalized, _format_code_artifact_violations(violations), violations, offending_labels
        )
    return CodeArtifactNormalization(normalized, None, [], [])


def _artifact_label_fragment(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "artifact"


def _artifact_mutable_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


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


def _code_block_available_binding_keys_by_label(workflow_yaml: str | None) -> dict[str, list[str]]:
    if workflow_yaml is None:
        return {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return {}
    available_by_label: dict[str, set[str]] = {}

    def block_output_key(block: Mapping[str, Any]) -> str | None:
        label = str(block.get("label") or "").strip()
        return f"{label}_output" if label else None

    def visit_branch(branch: Mapping[str, Any], available_keys: set[str]) -> None:
        for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
            visit_blocks(branch.get(key), set(available_keys))
        for branch_key in _ORDERED_BRANCH_LIST_KEYS:
            branches = branch.get(branch_key)
            if not isinstance(branches, list):
                continue
            for nested_branch in branches:
                if isinstance(nested_branch, Mapping):
                    visit_branch(nested_branch, set(available_keys))

    def visit_blocks(blocks: Any, available_keys: set[str]) -> set[str]:
        if not isinstance(blocks, list):
            return available_keys
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            label = str(block.get("label") or "").strip()
            if label and _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
                available_by_label.setdefault(label, set()).update(available_keys)
            for key in _ORDERED_CHILD_BLOCK_LIST_KEYS:
                visit_blocks(block.get(key), set(available_keys))
            for branch_key in _ORDERED_BRANCH_LIST_KEYS:
                branches = block.get(branch_key)
                if not isinstance(branches, list):
                    continue
                for branch in branches:
                    if isinstance(branch, Mapping):
                        visit_branch(branch, set(available_keys))
            output_key = block_output_key(block)
            if output_key:
                available_keys.add(output_key)
        return available_keys

    workflow_definition = parsed.get("workflow_definition")
    blocks = workflow_definition.get("blocks") if isinstance(workflow_definition, Mapping) else None
    visit_blocks(blocks, _declared_string_workflow_parameter_keys(parsed))
    return {label: sorted(keys) for label, keys in available_by_label.items()}


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


_CHURN_REASON_CODES = frozenset({"code_authoring_guardrail_churn", "credential_priority_authoring_churn"})


def _record_code_authoring_guardrail_reject(ctx: AgentContext, *, defer_churn_stop: bool = False) -> None:
    ctx.code_authoring_guardrail_reject_count += 1
    ctx.last_code_authoring_reject_was_credential_priority = defer_churn_stop
    LOG.info(
        "copilot code-authoring guardrail reject recorded",
        reject_count=ctx.code_authoring_guardrail_reject_count,
        credential_priority=defer_churn_stop,
    )
    if defer_churn_stop:
        if ctx.code_authoring_guardrail_reject_count < MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS:
            return
        signal: CopilotToolBlockerSignal = credential_priority_authoring_churn_stop_signal(ctx)
    elif ctx.code_authoring_guardrail_reject_count < MAX_CODE_AUTHORING_GUARDRAIL_REJECTS:
        return
    else:
        signal = code_authoring_churn_stop_signal(ctx)
    # A genuinely-terminal held blocker keeps both the rendered reply and the
    # halt kind, so the churn stop defers to it rather than overriding.
    if blocker_signal_is_genuinely_terminal(ctx.blocker_signal):
        return
    stash_blocker_signal(ctx, signal)
    ctx.blocker_signal = signal


def _signal_is_churn(signal: CopilotToolBlockerSignal | None) -> bool:
    return signal is not None and signal.internal_reason_code in _CHURN_REASON_CODES


def _clear_held_churn_signals(ctx: AgentContext) -> None:
    if _signal_is_churn(ctx.blocker_signal):
        ctx.blocker_signal = None
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


def _code_repair_progress_data(
    repair_context: CodeAuthoringRepairContext | None = None,
) -> dict[str, Any]:
    """Tag a code-authoring reject so the streaming adapter renders it as quiet de-duplicated progress."""
    data: dict[str, Any] = {
        "surface_kind": CODE_REPAIR_PROGRESS_SURFACE_KIND,
        "progress_text": CODE_REPAIR_PROGRESS_TEXT,
    }
    if repair_context is not None:
        data["authoring_repair_context"] = repair_context.model_dump(mode="json")
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


def _missing_code_artifact_metadata_error(workflow_yaml: str, ctx: AgentContext, raw_metadata: Any) -> str | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return None
    code_blocks = _workflow_code_blocks(dict(parsed))
    if not code_blocks:
        return None
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
        return None
    existing_metadata = getattr(ctx, "code_artifact_metadata", None)
    if _raw_code_artifact_metadata_empty(raw_metadata):
        missing = [label for label in labels if not _existing_metadata_covers_output(label, existing_metadata)]
    else:
        missing = [
            label
            for label in labels
            if not _raw_metadata_covers_output_label(raw_metadata, label, candidate_labels=labels)
        ]
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


def _should_impose_after_update_attempt(ctx: AgentContext) -> bool:
    target = ctx.reached_download_target
    return isinstance(target, ReachedDownloadTarget) and not target.already_registered and bool(target.selector.strip())


def _select_synthesized_imposition_code_block(
    code_blocks: list[dict[str, Any]],
    *,
    prior_yaml: str | None,
) -> dict[str, Any] | None:
    if len(code_blocks) == 1:
        return code_blocks[0]

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


def _is_ignorable_entry_opener_drop(dropped: Mapping[str, Any], diagnostics: SynthesisDiagnostics) -> bool:
    return (
        dropped.get("reason_code") == "ambiguous_bare_selector"
        and dropped.get("tool_name") == "click"
        and dropped.get("trajectory_index") == 0
        and str(dropped.get("selector") or "").strip() in {"button", "role=button"}
        and bool(diagnostics.locator_provenance)
    )


def _locator_provenance_is_self_validating(provenance: Mapping[str, Any]) -> bool:
    source = provenance.get("source")
    if source == "selector":
        return provenance.get("selector") == provenance.get("emitted_literal")
    if source == "aria_role_name":
        role = str(provenance.get("role") or "")
        name = str(provenance.get("name") or "")
        return bool(role) and bool(name) and _get_by_role_expr_strict(role, name) == provenance.get("emitted_literal")
    return False


_IDENTITY_QUALIFIER_BOUNDARY = ("[", "#", ".")
_FILTERING_PSEUDO_CLASSES = (
    ":visible",
    ":enabled",
    ":disabled",
    ":checked",
    ":not(",
    ":has(",
    ":has-text(",
    ":text(",
    ":is(",
)
_EXACT_TEXT_XPATH_TAG_RE = re.compile(
    r"""^(?:xpath=)?//(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)\s*\[\s*normalize-space\(\s*(?:\.|text\(\))?\s*\)\s*=\s*(?P<quote>['"])[^'"]+(?P=quote)\s*\]\s*$"""
)


def _qualifier_narrows_to_identity(qualifier: str) -> bool:
    if not qualifier or qualifier[0] not in _IDENTITY_QUALIFIER_BOUNDARY:
        return False
    if any(pseudo in qualifier for pseudo in _FILTERING_PSEUDO_CLASSES):
        return False
    bracket_depth = 0
    quote: str | None = None
    for char in qualifier:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif bracket_depth == 0 and (char.isspace() or char in ">+~"):
            return False
    return True


def _selector_refines(bare: str, candidate: str) -> bool:
    bare = bare.strip()
    candidate = candidate.strip()
    if not bare or not candidate or bare == candidate:
        return False

    bare_role = _parse_role_name(bare)
    candidate_role = _parse_role_name(candidate)
    if bare_role is not None or candidate_role is not None:
        if bare_role is None or candidate_role is None:
            return False
        bare_role_name, bare_name, bare_suffix = bare_role
        candidate_role_name, candidate_name, candidate_suffix = candidate_role
        return (
            bare_role_name == candidate_role_name
            and not bare_name
            and not bare_suffix
            and bool(candidate_name)
            and not candidate_suffix
        )

    if not _BARE_TAG_RE.match(bare):
        return False
    if not candidate.startswith(bare) or _is_positional_selector(candidate):
        return False
    return _qualifier_narrows_to_identity(candidate[len(bare) :])


def _stable_same_kind_bare_click_refiner(bare: str, candidate: str) -> bool:
    bare = bare.strip()
    candidate = candidate.strip()
    if not bare or not candidate or bare == candidate or _is_positional_selector(candidate):
        return False
    if _selector_refines(bare, candidate):
        return True
    if bare != "button":
        return False

    candidate_role = _parse_role_name(candidate)
    if candidate_role is not None:
        role_name, accessible_name, suffix = candidate_role
        return role_name == "button" and bool(accessible_name) and not suffix

    xpath_match = _EXACT_TEXT_XPATH_TAG_RE.match(candidate)
    return xpath_match is not None and xpath_match.group("tag").casefold() == "button"


def _bare_drop_superseded_on_screen(
    dropped: Mapping[str, Any],
    scout_trajectory: list[ScoutedInteraction],
    *,
    claimed_refiner_indices: set[int],
) -> tuple[bool, dict[str, Any] | None]:
    if dropped.get("reason_code") != "ambiguous_bare_selector" or dropped.get("tool_name") != "click":
        return False, None
    dropped_selector = str(dropped.get("selector") or "").strip()
    if not dropped_selector:
        return False, None

    dropped_index = dropped.get("trajectory_index")
    if not isinstance(dropped_index, int) or dropped_index < 0 or dropped_index >= len(scout_trajectory):
        return False, None
    source_url = str(scout_trajectory[dropped_index].get("source_url") or "").strip()
    if not source_url:
        return False, None

    for refiner_index in range(dropped_index + 1, len(scout_trajectory)):
        if refiner_index in claimed_refiner_indices:
            continue
        later = scout_trajectory[refiner_index]
        if later.get("tool_name") != "click":
            continue
        if str(later.get("source_url") or "").strip() != source_url:
            continue
        later_selector = str(later.get("selector") or "").strip()
        if not _stable_same_kind_bare_click_refiner(dropped_selector, later_selector):
            continue
        claimed_refiner_indices.add(refiner_index)
        return True, {
            "dropped_index": dropped_index,
            "dropped_selector": dropped_selector,
            "refiner_index": refiner_index,
            "refiner_selector": later_selector,
            "source_url": source_url,
        }
    return False, None


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
    # No default_value: direct scout fills become required workflow inputs at runtime.
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


def _apply_scouted_typed_default_promotions(workflow_yaml: str, ctx: AgentContext) -> tuple[str, list[str]]:
    if not getattr(ctx, "impose_synthesized_code_block", False):
        return workflow_yaml, []
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return workflow_yaml, []
    scout_trajectory = ctx.scout_trajectory
    if not isinstance(scout_trajectory, list) or not scout_trajectory:
        return workflow_yaml, []
    synthesized = synthesize_code_block(
        scout_trajectory,
        reached_download_target=getattr(ctx, "reached_download_target", None),
    )
    if synthesized is None:
        return workflow_yaml, []

    defaults_by_value: dict[str, list[str]] = {}
    for parameter in synthesized.parameters:
        if parameter.get("credential_id"):
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
        return workflow_yaml, []

    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return workflow_yaml, []
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return workflow_yaml, []
    parameters = workflow_definition.get("parameters")
    if parameters is None:
        parameters = []
        workflow_definition["parameters"] = parameters
    if not isinstance(parameters, list):
        return workflow_yaml, ["Unable to bind typed workflow inputs: workflow_definition.parameters must be a list."]

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
        return workflow_yaml, []

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
    return yaml.safe_dump(parsed, sort_keys=False), []


def _is_credential_parameter(parameter: Mapping[str, Any]) -> bool:
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    return parameter_type == "credential" or (
        parameter_type == "workflow" and workflow_parameter_type == "credential_id"
    )


def _submitted_string_parameter_default(
    parameters: list[Any],
    *,
    synthesized_key: str,
    typed_length: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[dict[str, Any]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        candidate_key = str(parameter.get("key") or "").strip()
        if not candidate_key or candidate_key == synthesized_key:
            continue
        if _is_credential_parameter(parameter):
            continue
        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type and parameter_type != "workflow":
            continue
        if workflow_parameter_type and workflow_parameter_type != "string":
            continue
        default_value = parameter.get("default_value")
        if isinstance(default_value, str):
            candidates.append(parameter)

    if len(candidates) != 1:
        return None, None

    candidate = candidates[0]
    default_value = str(candidate.get("default_value") or "")
    if _SECRET_LIKE_LITERAL_RE.search(default_value):
        return (
            None,
            f"Unable to bind synthesized parameter `{synthesized_key}`: submitted parameter default looks credential-like.",
        )
    if typed_length is not None and typed_length > 0 and len(default_value) != typed_length:
        return (
            None,
            f"Unable to bind synthesized parameter `{synthesized_key}`: submitted parameter default length does not match the scout record.",
        )
    return candidate, None


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


def _matching_string_parameter_key_by_default_length(
    parameters: list[Any],
    *,
    default_length: int | None,
    exclude_key: str,
) -> str | None:
    if default_length is None or default_length <= 0:
        return None
    matches: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = str(parameter.get("key") or "").strip()
        if not key or key == exclude_key:
            continue
        default_value = _string_parameter_default_value(parameter)
        if not default_value or _SECRET_LIKE_LITERAL_RE.search(default_value):
            continue
        if len(default_value) == default_length:
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


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


def _combined_string_default_expression(
    parameters: list[Any],
    *,
    synthesized_default: str,
    typed_length: int | None,
) -> tuple[list[str], str] | None:
    if not synthesized_default and typed_length is None:
        return None

    candidates: list[tuple[str, str]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = str(parameter.get("key") or "").strip()
        if not _is_python_identifier(key):
            continue
        default_value = _string_parameter_default_value(parameter)
        if not default_value or _SECRET_LIKE_LITERAL_RE.search(default_value):
            continue
        candidates.append((key, default_value))

    matches: list[tuple[list[str], str]] = []
    for first_index, (first_key, first_default) in enumerate(candidates):
        for last_key, last_default in candidates[first_index + 1 :]:
            combined_default = f"{first_default} {last_default}".strip()
            if synthesized_default:
                if combined_default != synthesized_default:
                    continue
            elif len(combined_default) != typed_length:
                continue
            matches.append(([first_key, last_key], f'(str({first_key}) + " " + str({last_key}))'))
    if len(matches) > 1:
        LOG.debug(
            "copilot_synthesized_parameter_combined_default_ambiguous",
            match_count=len(matches),
            synthesized_default_present=bool(synthesized_default),
            typed_length=typed_length,
        )
        return None
    return matches[0] if matches else None


class _SynthesizedParameterReconciliation(NamedTuple):
    parameter_keys: list[str]
    violations: list[str]
    aliases: dict[str, str]
    expressions: dict[str, str]


def _reconcile_synthesized_parameters(
    *,
    parsed: dict[str, Any],
    code_block: dict[str, Any],
    submitted_code: str,
    synthesized_parameters: list[dict[str, str]],
    scout_trajectory: list[ScoutedInteraction],
) -> _SynthesizedParameterReconciliation:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return _SynthesizedParameterReconciliation(
            [], ["Unable to bind synthesized parameters: workflow_definition is missing."], {}, {}
        )
    parameters = workflow_definition.get("parameters")
    if parameters is None:
        parameters = []
        workflow_definition["parameters"] = parameters
    if not isinstance(parameters, list):
        return _SynthesizedParameterReconciliation(
            [], ["Unable to bind synthesized parameters: workflow_definition.parameters must be a list."], {}, {}
        )

    existing_by_key = {
        str(param.get("key")): param for param in parameters if isinstance(param, dict) and param.get("key")
    }
    existing_credentials = credential_params(parameters)
    parameter_keys: list[str] = []
    violations: list[str] = []
    aliases: dict[str, str] = {}
    expressions: dict[str, str] = {}
    non_credential_synthesized = [param for param in synthesized_parameters if not param.get("credential_id")]
    typed_lengths = [
        int(interaction.get("typed_length") or 0)
        for interaction in scout_trajectory
        if str(interaction.get("tool_name") or "") == "type_text"
    ]

    for synthesized_param in synthesized_parameters:
        key = str(synthesized_param.get("key") or "").strip()
        if not key:
            violations.append("Unable to bind synthesized parameter: parameter key is missing.")
            continue
        if key in parameter_keys:
            violations.append(f"Unable to bind synthesized parameter `{key}`: duplicate synthesized key.")
            continue
        parameter_keys.append(key)

        credential_id = str(synthesized_param.get("credential_id") or "").strip()
        existing = existing_by_key.get(key)
        synthesized_default = str(synthesized_param.get("default_value") or "").strip()
        typed_length = _coerce_positive_int(synthesized_param.get("typed_length"))
        if credential_id:
            if existing is not None:
                if existing_credentials.get(key) != credential_id:
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
            alias_key = _matching_string_parameter_key_by_default(
                parameters,
                default_value=synthesized_default,
                exclude_key=key,
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                _drop_parameter_key(parameters, key)
                continue
            alias_key = _matching_string_parameter_key_by_default_length(
                parameters,
                default_length=typed_length,
                exclude_key=key,
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                _drop_parameter_key(parameters, key)
                continue
            if _is_credential_parameter(existing):
                violations.append(
                    f"Unable to bind synthesized parameter `{key}`: submitted parameter is credential-typed."
                )
            elif synthesized_default:
                existing_default = _string_parameter_default_value(existing)
                if existing_default != synthesized_default:
                    violations.append(
                        f"Unable to bind synthesized parameter `{key}`: "
                        "submitted parameter default does not match the scout record."
                    )
            continue

        if synthesized_default:
            # Narrow defense-in-depth backstop for synthesized rows; values
            # captured from live type_text scouting are fully screened by
            # safe_typed_default_value before they enter the scout trajectory.
            if _SECRET_LIKE_LITERAL_RE.search(synthesized_default):
                violations.append(
                    f"Unable to bind synthesized parameter `{key}`: synthesized default looks credential-like."
                )
                continue
            alias_key = _matching_string_parameter_key_by_default(
                parameters,
                default_value=synthesized_default,
                exclude_key=key,
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                continue
            combined_expression = _combined_string_default_expression(
                parameters,
                synthesized_default=synthesized_default,
                typed_length=typed_length,
            )
            if combined_expression is not None:
                expression_keys, expression = combined_expression
                parameter_keys.pop()
                for expression_key in expression_keys:
                    if expression_key not in parameter_keys:
                        parameter_keys.append(expression_key)
                expressions[key] = expression
                continue
            parameters.append(_string_parameter_row(synthesized_default, key))
            continue

        if len(non_credential_synthesized) != 1:
            alias_key = _matching_string_parameter_key_by_default_length(
                parameters,
                default_length=typed_length,
                exclude_key=key,
            )
            if alias_key is not None:
                aliases[key] = alias_key
                parameter_keys[-1] = alias_key
                continue

        combined_expression = _combined_string_default_expression(
            parameters,
            synthesized_default=synthesized_default,
            typed_length=typed_length,
        )
        if combined_expression is not None:
            expression_keys, expression = combined_expression
            parameter_keys.pop()
            for expression_key in expression_keys:
                if expression_key not in parameter_keys:
                    parameter_keys.append(expression_key)
            expressions[key] = expression
            continue

        if len(non_credential_synthesized) != 1:
            # Fall through only after length aliasing and combined-expression binding both miss.
            violations.append(
                f"Unable to bind synthesized parameter `{key}`: missing submitted workflow parameter and literal binding is ambiguous."
            )
            continue

        typed_length = typed_lengths[0] if len(typed_lengths) == 1 else None
        submitted_parameter, error = _submitted_string_parameter_default(
            parameters,
            synthesized_key=key,
            typed_length=typed_length,
        )
        if error:
            violations.append(error)
            continue
        if submitted_parameter is not None:
            submitted_parameter["parameter_type"] = "workflow"
            submitted_parameter["workflow_parameter_type"] = "string"
            submitted_parameter["key"] = key
            continue

        direct_fill_usage = _submitted_direct_fill_type_usage(submitted_code, key)
        if direct_fill_usage.matched and not direct_fill_usage.mismatched:
            parameters.append(_required_string_parameter_row(key))
            continue
        if direct_fill_usage.matched and direct_fill_usage.mismatched:
            violations.append(
                f"Unable to bind synthesized parameter `{key}`: submitted code mixes direct fills using `{key}` "
                "with other browser-locator fill/type values. Use the synthesized parameter for every scout-input "
                "fill in the code block, or declare explicit workflow parameters/defaults for the other filled values."
            )
            continue

        literal, error = _safe_singleton_literal_for_parameter(submitted_code, key, typed_length)
        if error:
            violations.append(error)
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
    return _SynthesizedParameterReconciliation(parameter_keys, violations, aliases, expressions)


def _maybe_impose_synthesized_code_block(workflow_yaml: str, ctx: AgentContext) -> _SynthesizedCodeImpositionResult:
    if not getattr(ctx, "impose_synthesized_code_block", False):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    if ctx.update_workflow_called and not _should_impose_after_update_attempt(ctx):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)

    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(scout_trajectory, list) or not scout_trajectory:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    first_source_url = str(scout_trajectory[0].get("source_url") or "").strip()
    if not first_source_url:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)

    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    code_blocks = _workflow_code_blocks(parsed)
    prior_source, prior_yaml = _prior_yaml_source(ctx)
    code_block = _select_synthesized_imposition_code_block(code_blocks, prior_yaml=prior_yaml)
    if code_block is None:
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)
    submitted_code = str(code_block.get("code") or "")

    if not _submitted_code_block_changed(code_block, prior_yaml):
        return _SynthesizedCodeImpositionResult(workflow_yaml=workflow_yaml)

    synthesized = synthesize_code_block(
        scout_trajectory,
        strict_selectors=True,
        reached_download_target=ctx.reached_download_target,
    )
    if synthesized is None:
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=["Unable to impose synthesized code block: scout trajectory produced no runnable code."],
        )

    diagnostics = synthesized.diagnostics
    violations: list[str] = []
    if diagnostics.truncated:
        violations.append("Unable to impose synthesized code block: scout trajectory was truncated.")
    claimed_refiner_indices: set[int] = set()
    forgiven_superseded_bare_drops: list[dict[str, Any]] = []
    repair_context: CodeAuthoringRepairContext | None = None
    for dropped in diagnostics.dropped_interactions:
        if _is_ignorable_entry_opener_drop(dropped, diagnostics):
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

    parameter_reconciliation = _reconcile_synthesized_parameters(
        parsed=parsed,
        code_block=code_block,
        submitted_code=submitted_code,
        synthesized_parameters=synthesized.parameters,
        scout_trajectory=scout_trajectory,
    )
    violations.extend(parameter_reconciliation.violations)
    if violations:
        return _SynthesizedCodeImpositionResult(
            workflow_yaml=workflow_yaml,
            violations=violations,
            repair_context=repair_context,
        )

    extraction_suffix = _submitted_suffix_after_synthesized_code(submitted_code, synthesized.code)
    raw_metadata = getattr(ctx, "raw_code_artifact_metadata", None)
    preserve_submitted_extraction = (
        bool(raw_metadata)
        and _raw_metadata_declares_goal_values_for_block(raw_metadata, str(code_block.get("label") or ""))
        and not extraction_suffix
        and not _is_submitted_code_synthesized_only(submitted_code, synthesized.code)
    )
    imposed_code = textwrap.dedent(submitted_code if preserve_submitted_extraction else synthesized.code).lstrip("\n")
    if extraction_suffix:
        imposed_code = imposed_code.rstrip() + "\n" + extraction_suffix.rstrip() + "\n"
    for old_key, new_key in parameter_reconciliation.aliases.items():
        imposed_code = _replace_python_identifier(imposed_code, old_key, new_key)
    for old_key, expression in parameter_reconciliation.expressions.items():
        imposed_code = _replace_python_identifier(imposed_code, old_key, expression)
    code_block["code"] = imposed_code
    credential_parameter_keys = [
        str(param.get("key") or "") for param in synthesized.parameters if str(param.get("credential_id") or "").strip()
    ]
    substitutions = {
        "block_label": str(code_block.get("label") or ""),
        "source_trajectory_count": len(scout_trajectory),
        "parameter_keys": parameter_reconciliation.parameter_keys,
        "credential_parameter_keys": credential_parameter_keys,
        "selector_provenance": diagnostics.locator_provenance,
        "prior_source": prior_source,
    }
    if parameter_reconciliation.aliases:
        substitutions["parameter_aliases"] = parameter_reconciliation.aliases
    if parameter_reconciliation.expressions:
        substitutions["parameter_expressions"] = parameter_reconciliation.expressions
    if extraction_suffix:
        substitutions["preserved_extraction_suffix"] = True
    if preserve_submitted_extraction:
        substitutions["preserved_submitted_extraction_code"] = True
    if forgiven_superseded_bare_drops:
        substitutions["forgiven_superseded_bare_drops"] = forgiven_superseded_bare_drops
    return _SynthesizedCodeImpositionResult(
        workflow_yaml=yaml.safe_dump(parsed, sort_keys=False),
        substitutions=substitutions,
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
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_target_names(element))
        return names
    return set()


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
    values: list[Any] = []
    for field_name in ("claimed_outcomes", "terminal_verifier_expectations"):
        for row in _artifact_rows(artifact.get(field_name)):
            schema = row.get("extraction_schema")
            if schema is not None and not (isinstance(schema, str) and not schema.strip()):
                values.append(schema)
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
    credential_params_by_key = credential_params(workflow_definition.get("parameters"))
    if not credential_params_by_key:
        return []
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(scout_trajectory, list):
        scout_trajectory = []

    errors: list[str] = []
    for block in workflow_blocks(parsed):
        if _enum_or_string_name(block.get("block_type")) != BlockType.CODE.value:
            continue
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        required_fields_by_credential: dict[str, set[str]] = {}
        for access in _credential_field_accesses(code):
            if not access.requires_live_scout:
                continue
            credential_id = credential_params_by_key.get(access.parameter_key)
            if credential_id:
                required_fields_by_credential.setdefault(credential_id, set()).add(access.field)
        if not required_fields_by_credential:
            continue

        matched_fill_indexes: list[int] = []
        matched_source_urls: set[str] = set()
        missing_fields: list[str] = []
        for credential_id, required_fields in required_fields_by_credential.items():
            matched_fields: set[str] = set()
            for index, interaction in enumerate(scout_trajectory):
                if str(interaction.get("tool_name") or "").strip() != "fill_credential_field":
                    continue
                if str(interaction.get("credential_id") or "").strip() != credential_id:
                    continue
                field = str(interaction.get("credential_field") or "").strip()
                if field not in required_fields:
                    continue
                matched_fields.add(field)
                matched_fill_indexes.append(index)
                source_url = str(interaction.get("source_url") or "").strip()
                if source_url:
                    matched_source_urls.add(source_url)
            for field in sorted(required_fields - matched_fields):
                missing_fields.append(field)

        requires_submit = bool(_CODE_SUBMIT_ACTION_RE.search(code))
        missing_submit = False
        if requires_submit:
            latest_fill_index = max(matched_fill_indexes, default=-1)
            if latest_fill_index < 0:
                missing_submit = True
            else:
                missing_submit = True
                for index, interaction in enumerate(scout_trajectory):
                    if index <= latest_fill_index:
                        continue
                    if str(interaction.get("tool_name") or "").strip() not in _SCOUT_SUBMIT_TOOL_NAMES:
                        continue
                    source_url = str(interaction.get("source_url") or "").strip()
                    if matched_source_urls and source_url not in matched_source_urls:
                        continue
                    missing_submit = False
                    break

        if not missing_fields and not missing_submit:
            continue

        block_label = str(block.get("label") or "").strip() or "this code block"
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


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
) -> dict[str, Any]:
    def reject(
        *,
        error: str,
        user_facing_summary: str | None = None,
        data: dict[str, Any] | None = None,
        repair_context: CodeAuthoringRepairContext | None = None,
    ) -> dict[str, Any]:
        if repair_context is None:
            _clear_code_authoring_repair_context(ctx)
        else:
            _set_code_authoring_repair_context(ctx, repair_context)
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
    # Imposition reconciles synthesized aliases/parameters before the persisted YAML contract is checked.
    imposition = _maybe_impose_synthesized_code_block(workflow_yaml, ctx)
    if imposition.violations:
        return reject(
            error="\n".join(imposition.violations),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(imposition.repair_context),
            repair_context=imposition.repair_context,
        )
    workflow_yaml = imposition.workflow_yaml
    stripped_sandbox_imports: list[str] = []
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        workflow_yaml, stripped_sandbox_imports = _strip_redundant_sandbox_imports_in_yaml(workflow_yaml)
    workflow_yaml, typed_default_violations = _apply_scouted_typed_default_promotions(workflow_yaml, ctx)
    if typed_default_violations:
        return reject(
            error="\n".join(typed_default_violations),
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    params["workflow_yaml"] = workflow_yaml
    parameter_contract_error = _code_block_parameter_contract_error(workflow_yaml)
    if parameter_contract_error is not None:
        return reject(
            error=parameter_contract_error,
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    missing_metadata_error = _missing_code_artifact_metadata_error(
        workflow_yaml,
        ctx,
        params.get("code_artifact_metadata"),
    )
    if missing_metadata_error is not None:
        return reject(
            error=missing_metadata_error,
            user_facing_summary=_compiled_authoring_user_summary(),
            data=_code_repair_progress_data(),
        )
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    normalization = _normalize_code_artifact_metadata_detailed(
        params.get("code_artifact_metadata"),
        workflow_yaml,
        impose_defaults=_copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        scout_trajectory=scout_trajectory if isinstance(scout_trajectory, list) else None,
    )
    code_artifact_metadata = normalization.normalized
    code_artifact_metadata_error = normalization.error
    if code_artifact_metadata_error is not None:
        record_code_artifact_violations(ctx, normalization.violations, normalization.offending_labels)
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
        else _credentialed_code_block_scout_gate_errors(workflow_yaml, ctx)
    )
    unresolved_symbol_priority_reject = _is_unresolved_symbol_repair_context(code_authoring_repair_context)
    credential_priority_reject = (
        bool(credential_scout_errors) and code_artifact_metadata_error is None and not unresolved_symbol_priority_reject
    )
    if code_safety_errors:
        _set_code_authoring_repair_context(ctx, code_authoring_repair_context)
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
        params["code_artifact_metadata"] = merged_metadata
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
        return reject(
            error="\n".join(credential_scout_errors),
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
        _record_code_authoring_guardrail_reject(ctx, defer_churn_stop=True)
        return reject(
            error="\n".join(credential_scout_errors),
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
        _record_code_only_raw_secret_reject_span(ctx, output_policy_verdict)
        LOG.info(
            "copilot output policy tool body verdict",
            **output_policy_verdict_to_trace_data(
                output_policy_verdict,
                surface="tool_body",
                tool_name="update_workflow",
            ),
        )
        return reject(error=format_output_policy_tool_error(output_policy_verdict))

    # Prefer the most-recent in-turn emission so cross-path flows (inline
    # REPLACE_WORKFLOW followed by update_workflow) compare against what the
    # LLM actually saw, not the turn-start persisted state.
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    prior_yaml = last_yaml if isinstance(last_yaml, str) and last_yaml else ctx.workflow_yaml
    stale_metadata = _detect_stale_block_metadata(workflow_yaml, prior_yaml)
    if stale_metadata:
        return reject(error=_stale_block_metadata_message(stale_metadata))

    wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
    if wait_block_error:
        return reject(error=wait_block_error)

    challenge_http_error = _challenge_http_request_reject_message(ctx, workflow_yaml, ctx.workflow_yaml)
    if challenge_http_error:
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
        return reject(error=_banned_block_reject_message(banned_items, ctx))

    download_scout_error = _download_scout_required_error(ctx, workflow_yaml)
    if download_scout_error:
        return reject(error=download_scout_error)

    download_binding_error = _download_binding_required_error(ctx, workflow_yaml)
    if download_binding_error:
        return reject(error=download_binding_error)

    composition_evidence_error = composition_page_evidence_error(ctx, workflow_yaml)
    if composition_evidence_error:
        LOG.info(
            "copilot composition page evidence rejected workflow",
            workflow_permanent_id=ctx.workflow_permanent_id,
            target_url=workflow_target_url(workflow_yaml),
        )
        return reject(error=composition_evidence_error)

    # New data-write blocks default to surfacing failures rather than swallowing them.
    workflow_yaml = default_data_write_continue_on_failure(workflow_yaml, ctx.workflow_yaml)

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
        workflow = _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml_with_steps,
        )
        _record_workflow_proxy_location_span(workflow_yaml, workflow)

        # Param / top-level setting changes go through canonical because
        # prepare_workflow and the runtime parameter-row read consume canonical
        # values; terminal handlers roll back on non-auto-accept.
        prior_workflow = await _get_prior_workflow(ctx)
        requires_canonical_persist = _workflow_requires_canonical_persist(prior_workflow, workflow)
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
        ctx.code_authoring_guardrail_reject_count = 0
        ctx.last_code_authoring_reject_was_credential_priority = False
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
            error=f"Workflow validation failed: {e}",
            user_facing_summary=user_facing_summary,
            data=repair_data,
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
