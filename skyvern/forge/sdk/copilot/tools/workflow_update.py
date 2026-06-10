from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import structlog
import yaml
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.copilot.attribution import resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.code_block_synthesis import artifact_dependency_id, artifact_observation_ref_id
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    composition_page_evidence_error,
    normalize_block_observation_refs,
    workflow_target_url,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    POST_INTERMEDIATE_SUCCESS_NUDGE,
    _goal_likely_needs_more_blocks,
)
from skyvern.forge.sdk.copilot.loop_detection import clear_failed_step_tracker_for_tools_in_ctx
from skyvern.forge.sdk.copilot.output_policy import (
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext, ScoutedInteraction
from skyvern.forge.sdk.copilot.streaming_adapter import emit_workflow_draft, maybe_emit_design_end
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.proxy_location import ProxyLocation
from skyvern.schemas.workflows import BlockType

from ._shared import (
    BLOCK_RUNNING_TOOLS,
    _enum_or_string_name,
    _proxy_location_trace_value,
    _raw_yaml_proxy_location,
    _workflow_definition_as_dict,
    _workflow_yaml_blocks_by_label,
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
from .guardrails import _authority_tool_error

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


def _normalize_code_artifact_metadata(
    raw_metadata: Any,
    workflow_yaml: str,
    *,
    impose_defaults: bool = False,
    scout_trajectory: list[ScoutedInteraction] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Normalize submitted artifact metadata at the persist seam.

    Entries keyed to a missing code-block label are re-keyed to the single
    uncovered block or dropped, never rejected. When imposing, mechanical graph
    fields are server-defaulted (and uncovered labels get a deterministic
    skeleton) so only contradictions reject."""
    if raw_metadata in (None, [], {}):
        return {}, None
    items = _code_artifact_metadata_items(raw_metadata)
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    trajectory: list[ScoutedInteraction] = scout_trajectory or []
    violations: list[str] = []
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
            violations.append(f"Artifact metadata is malformed: {exc}")
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
        item_violations.extend(_code_artifact_metadata_shape_errors(label, dumped))
        if item_violations:
            violations.extend(item_violations)
            continue
        normalized[label] = dumped
    if impose_defaults:
        for label in uncovered:
            normalized[label] = _imposed_artifact_skeleton(label, code_blocks[label], trajectory)
    if violations:
        return normalized, _format_code_artifact_violations(violations)
    return normalized, None


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
    blocks: dict[str, Mapping[str, Any]] = {}
    for label, block in _workflow_yaml_blocks_by_label(workflow_yaml).items():
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
            blocks[label] = block
    return blocks


def _artifact_id_for_block_label(label: str) -> str:
    return f"code_artifact:{_artifact_label_fragment(label)}"


def _code_block_safety_errors(workflow_yaml: str | None, prior_yaml: str | None) -> list[str]:
    """Run the sandbox's static safety rule on new/changed code blocks before any run.

    Label-scoped diff so legacy code blocks the model did not touch cannot wedge
    every subsequent update."""
    prior_blocks = _workflow_yaml_code_blocks_by_label(prior_yaml)
    errors: list[str] = []
    for label, block in _workflow_yaml_code_blocks_by_label(workflow_yaml).items():
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        prior_block = prior_blocks.get(label)
        if prior_block is not None and str(prior_block.get("code") or "") == code:
            continue
        try:
            CodeBlock.is_safe_code(code)
        except SyntaxError as exc:
            errors.append(f"Code block `{label}` is not valid Python: {exc}")
        except InsecureCodeDetected as exc:
            errors.append(
                f"Code block `{label}` failed the sandbox safety check: {exc}. Rewrite without import "
                "statements, dunder access, or private attributes; the sandbox already provides `page`, "
                "`json`, `re`, `html`, `asyncio.sleep`, and common builtins."
            )
    return errors


def _code_seam_rejection_user_summary(*, metadata_rejected: bool, code_rejected: bool) -> str:
    if metadata_rejected and code_rejected:
        return "I need to adjust the workflow's code and its verification details before testing."
    if code_rejected:
        return "I need to adjust the workflow's code so it can run safely before testing."
    return "I need to adjust how the workflow verifies its results before testing."


def _code_artifact_metadata_shape_errors(label: str, artifact: Mapping[str, Any]) -> list[str]:
    """Return every shape violation for one artifact; the caller aggregates them."""
    errors: list[str] = []
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
        if not claim_criteria:
            errors.append(f"Artifact metadata claim `{claim_id}` for `{label}` requires covered criterion ids.")
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
        if not _artifact_string_list(expectation.get("criteria_ids")) and not _artifact_string_list(
            expectation.get("claimed_outcome_ids")
        ):
            errors.append(
                f"Artifact metadata terminal verifier expectation `{expectation_id or index}` for `{label}` "
                "requires `criteria_ids` or `claimed_outcome_ids`."
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


def _artifact_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
) -> dict[str, Any]:
    authority_error = _authority_tool_error(ctx, "update_workflow")
    if authority_error is not None:
        return {"ok": False, "error": authority_error}

    workflow_yaml = params["workflow_yaml"]
    # Tool wrappers run authority/loop guards before calling here. The composition
    # gate below consumes these refs, so they must be visible before validation.
    ctx.raw_block_observation_refs = params.get("raw_block_observation_refs", params.get("block_observation_refs"))
    ctx.block_observation_refs = normalize_block_observation_refs(params.get("block_observation_refs"))
    ctx.raw_code_artifact_metadata = params.get("raw_code_artifact_metadata", params.get("code_artifact_metadata"))
    scout_trajectory = getattr(ctx, "scout_trajectory", None)
    code_artifact_metadata, code_artifact_metadata_error = _normalize_code_artifact_metadata(
        params.get("code_artifact_metadata"),
        workflow_yaml,
        impose_defaults=_copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        scout_trajectory=scout_trajectory if isinstance(scout_trajectory, list) else None,
    )
    prior_workflow_yaml = getattr(ctx, "workflow_yaml", None)
    code_safety_errors = _code_block_safety_errors(workflow_yaml, prior_workflow_yaml)
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
    seam_errors = [error for error in (code_artifact_metadata_error, *code_safety_errors) if error]
    if seam_errors:
        return {
            "ok": False,
            "error": "\n".join(seam_errors),
            "user_facing_summary": _code_seam_rejection_user_summary(
                metadata_rejected=code_artifact_metadata_error is not None,
                code_rejected=bool(code_safety_errors),
            ),
        }
    if allow_missing_credentials is None:
        allow_missing_credentials = getattr(ctx, "allow_untested_workflow_draft", False) is True
    if not allow_missing_credentials:
        credential_error = await _credential_reference_validation_error(workflow_yaml, ctx)
        if credential_error is not None:
            return {"ok": False, "error": credential_error}

    misbinding_findings = _credential_id_misbinding_findings(workflow_yaml)
    if misbinding_findings:
        LOG.info(
            "copilot credential id misbinding rejected",
            organization_id=ctx.organization_id,
            workflow_id=ctx.workflow_id,
            findings=misbinding_findings,
        )
        return {"ok": False, "error": _credential_id_misbinding_error_message(misbinding_findings)}

    output_policy_verdict = evaluate_output_policy(
        request_policy=getattr(ctx, "request_policy", None),
        workflow_yaml=workflow_yaml,
        tool_arguments=params,
    )
    if not output_policy_verdict.allowed:
        LOG.info(
            "copilot output policy tool body verdict",
            **output_policy_verdict_to_trace_data(
                output_policy_verdict,
                surface="tool_body",
                tool_name="update_workflow",
            ),
        )
        return {"ok": False, "error": format_output_policy_tool_error(output_policy_verdict)}

    # Prefer the most-recent in-turn emission so cross-path flows (inline
    # REPLACE_WORKFLOW followed by update_workflow) compare against what the
    # LLM actually saw, not the turn-start persisted state.
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    prior_yaml = last_yaml if isinstance(last_yaml, str) and last_yaml else ctx.workflow_yaml
    stale_metadata = _detect_stale_block_metadata(workflow_yaml, prior_yaml)
    if stale_metadata:
        return {"ok": False, "error": _stale_block_metadata_message(stale_metadata)}

    wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
    if wait_block_error:
        return {"ok": False, "error": wait_block_error}

    challenge_http_error = _challenge_http_request_reject_message(ctx, workflow_yaml, ctx.workflow_yaml)
    if challenge_http_error:
        return {"ok": False, "error": challenge_http_error}

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
        return {"ok": False, "error": _banned_block_reject_message(banned_items, ctx)}

    composition_evidence_error = composition_page_evidence_error(ctx, workflow_yaml)
    if composition_evidence_error:
        LOG.info(
            "copilot composition page evidence rejected workflow",
            workflow_permanent_id=ctx.workflow_permanent_id,
            target_url=workflow_target_url(workflow_yaml),
        )
        return {"ok": False, "error": composition_evidence_error}

    try:
        workflow = _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
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
        return {
            "ok": True,
            "data": {
                "message": "Workflow updated successfully.",
                "block_count": len(workflow.workflow_definition.blocks) if workflow.workflow_definition else 0,
            },
            "_workflow": workflow,
        }
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        return {
            "ok": False,
            "error": f"Workflow validation failed: {e}",
        }


def _record_workflow_proxy_location_span(workflow_yaml: str, workflow: Workflow) -> None:
    input_present, input_proxy_location = _raw_yaml_proxy_location(workflow_yaml)
    effective_proxy_location = _proxy_location_trace_value(workflow.proxy_location)
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
    _clear_resolved_per_tool_budget_problem_labels(copilot_ctx, wf)
    copilot_ctx.last_workflow_yaml = copilot_ctx.workflow_yaml or None
    copilot_ctx.effective_workflow_proxy_location = getattr(wf, "proxy_location", None) or ProxyLocation.RESIDENTIAL
    data = result.get("data")
    if isinstance(data, dict):
        block_count = data.get("block_count")
        if isinstance(block_count, int):
            copilot_ctx.last_update_block_count = block_count
    copilot_ctx.last_test_ok = None
    copilot_ctx.last_test_failure_reason = None
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


def _last_update_is_single_goto_bootstrap(copilot_ctx: CopilotContext) -> bool:
    last_workflow = copilot_ctx.last_workflow
    definition = _workflow_definition_as_dict(last_workflow.workflow_definition if last_workflow is not None else None)
    blocks = definition.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != 1:
        return False
    block = blocks[0]
    if not isinstance(block, dict):
        return False
    return str(block.get("block_type") or "").strip().lower() == "goto_url"


def _pre_run_workflow_coverage_error(copilot_ctx: Any) -> str | None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    if not isinstance(block_count, int):
        return None
    if block_count == 1 and _last_update_is_single_goto_bootstrap(copilot_ctx):
        return None

    user_message = getattr(copilot_ctx, "user_message", "")
    request_policy = getattr(copilot_ctx, "request_policy", None)
    completion_contract = getattr(request_policy, "completion_contract", None)
    if isinstance(completion_contract, str):
        completion_contract = completion_contract.strip() or None
    else:
        completion_contract = None

    if not _goal_likely_needs_more_blocks(user_message, block_count, completion_contract):
        return None

    nudge_count = getattr(copilot_ctx, "coverage_nudge_count", 0)
    if nudge_count >= 1:
        return None
    copilot_ctx.coverage_nudge_count = nudge_count + 1
    return (
        f"{POST_INTERMEDIATE_SUCCESS_NUDGE} The workflow was saved with {block_count} block"
        f"{'' if block_count == 1 else 's'}, but it has not been run because the request-policy "
        "completion contract still leaves distinct requested actions uncovered."
    )
