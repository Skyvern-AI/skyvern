from __future__ import annotations

import hashlib
import json
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, Protocol
from urllib.parse import urlsplit

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.challenge_evidence import carrier_backed_anti_bot_categories
from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    only_degraded_blocking,
)
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome, RunOutcomeReasonCode

LOG = structlog.get_logger()

BuildTestOutcomePhase = Literal["scout_evaluate", "persisted_block_run", "author_time_reject"]
BuildTestOutcomeVerdict = Literal[
    "progress_observed",
    "repairable_failure",
    "authoring_rejected",
    "not_authoritative",
]
BuildTestOutcomeReasonCode = Literal[
    "loaded_result_targets_observed",
    "runtime_block_failure",
    "runtime_missing_output_dependency",
    "sandbox_unresolved_name",
    "synthesized_parameter_binding_ambiguous",
    "code_safety_reject",
    "credential_scout_reject",
    "schema_incompatibility",
    "verified_success",
    "outcome_not_demonstrated",
    "no_meaningful_output",
    "terminal_challenge_blocker",
    "blocker_reported",
    "failed_run",
    "suspicious_success",
    "missing_structural_evidence",
    "unchanged_after_recorded_outcome",
    "metadata_reject",
    "output_policy_reject",
    "scout_act_observe_hollow_after_interaction",
    "required_input_unbound",
    "fallback_floor_turn_unsatisfiable",
    "output_source_unobservable",
    "actuation_exhausted",
]

_STRUCTURAL_KEY_VERSION = "recorded_build_test_outcome:v1"
_AUTHORED_STRUCTURE_VERSION = "recorded_build_test_outcome_authored_structure:v1"
_TEXT_MAX = 180
_REF_TEXT_MAX = 96
_VALUE_EXCERPT_MAX = 700
_HISTORY_LIMIT = 8
_INSPECT_PAGE_SOURCE_TOOL = "inspect_page_for_composition"
_PLAYWRIGHT_LOCATOR_WAIT_RE = re.compile(
    r"waiting for locator\((?P<quote>['\"])(?P<selector>.*?)(?P=quote)\)"
    r"(?P<locator_chain>(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)*)\s+to be (?P<state>[a-z_]+)",
    re.IGNORECASE,
)
_PLAYWRIGHT_HIDDEN_TAG_RE = re.compile(r"locator resolved to hidden <(?P<tag>[a-z0-9:-]+)", re.IGNORECASE)


class RecordedBuildTestOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: BuildTestOutcomePhase
    attempted_tool: str = ""
    attempted_target: str = ""
    attempted_block_label: str = ""
    verdict: BuildTestOutcomeVerdict
    reason_code: BuildTestOutcomeReasonCode
    observed_evidence_summary: str = ""
    workflow_run_id: str | None = None
    block_labels: list[str] = Field(default_factory=list)
    requested_block_labels: list[str] = Field(default_factory=list)
    block_shape_hashes: dict[str, str] = Field(default_factory=dict)
    structural_failure_identity: str = ""
    verified_progress_marker: str = ""
    page_evidence_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_requested_output_facts: list[dict[str, object]] = Field(default_factory=list)
    runtime_output_repair_facts: list[dict[str, object]] = Field(default_factory=list)
    authored_structure_signature: str | None = None
    display_text: str = ""
    observed_page_value_excerpt: str = ""
    key_provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def structural_key_payload(self) -> dict[str, object] | None:
        if not (
            self.structural_failure_identity
            or self.verified_progress_marker
            or self.page_evidence_refs
            or self.runtime_output_repair_facts
        ):
            return None
        return {
            "version": _STRUCTURAL_KEY_VERSION,
            "phase": self.phase,
            "attempted_tool": self.attempted_tool,
            "attempted_target": self.attempted_target,
            "reason_code": self.reason_code,
            "verdict": self.verdict,
            "structural_failure_identity": self.structural_failure_identity,
            "verified_progress_marker": self.verified_progress_marker,
            "page_evidence_refs": sorted(self.page_evidence_refs),
            "evidence_refs": sorted(self.evidence_refs),
            "missing_requested_output_facts": self.missing_requested_output_facts,
            "runtime_output_repair_facts": self.runtime_output_repair_facts,
        }

    @property
    def structural_key(self) -> str | None:
        payload = self.structural_key_payload
        if payload is None:
            return None
        return _stable_hash(payload)

    @property
    def is_authoritative(self) -> bool:
        return self.structural_key is not None


class RecordedOutcomeGroundingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repeated_structural_key: str
    source_tool: str
    observed_after_workflow_run: bool = False
    workflow_run_id: str | None = None
    observed_empty_page: bool = False
    challenge_gated: bool = False
    capture_degraded: bool = False
    target_url: str | None = None
    source_url: str | None = None
    requirement_workflow_run_id: str | None = None
    payload_workflow_run_id: str | None = None
    diagnostic_reason: Literal["none", "empty_page", "challenge_gated", "capture_degraded"] = "none"
    current_origin: str | None = None
    current_url_present: bool = False
    current_title_present: bool = False
    form_summaries: list[str] = Field(default_factory=list)
    result_container_summaries: list[str] = Field(default_factory=list)
    navigation_action_summaries: list[str] = Field(default_factory=list)
    challenge_control_summaries: list[str] = Field(default_factory=list)


class RecordedOutcomeGroundingRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: BuildTestOutcomePhase
    reason_code: BuildTestOutcomeReasonCode
    structural_key: str
    workflow_run_id: str | None = None
    block_labels: list[str] = Field(default_factory=list)
    required_tool: Literal["inspect_page_for_composition"] = "inspect_page_for_composition"
    required_target_url: Literal["current_page"] = "current_page"
    observation_requirement: Literal["current_page_bounded_composition_evidence"] = (
        "current_page_bounded_composition_evidence"
    )
    satisfied: bool = False
    payload: RecordedOutcomeGroundingPayload | None = None


BindingFrontierFacet = Literal[
    "unexecuted_submit",
    "value_shape",
    "amend_in_place",
    "selector_frontier",
]
_UNCROSSABLE_DIAGNOSTIC_REASONS = frozenset({"empty_page", "challenge_gated", "capture_degraded"})
_AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES: frozenset[RunOutcomeReasonCode] = frozenset(
    {"outcome_not_demonstrated", "no_meaningful_output"}
)


def _recorded_outcome_degrade_eligible(
    recorded_run_outcome: RecordedRunOutcome,
    failed_block: Mapping[str, object] | None,
) -> bool:
    if failed_block is not None:
        return False
    reason_code = recorded_run_outcome.reason_code
    return reason_code is None or reason_code in _AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES


class RecordedOutcomeBindingConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repeated_structural_key: str
    phase: BuildTestOutcomePhase
    reason_code: BuildTestOutcomeReasonCode
    frontier_facet: BindingFrontierFacet
    owning_block_labels: list[str] = Field(default_factory=list)
    diagnostic_reason: Literal["none", "empty_page", "challenge_gated", "capture_degraded"] = "none"
    workflow_run_id: str | None = None
    recorded_block_signatures: dict[str, str] = Field(default_factory=dict)

    @property
    def frontier_uncrossable(self) -> bool:
        return self.diagnostic_reason in _UNCROSSABLE_DIAGNOSTIC_REASONS

    def owning_block_frontier_moved(self, candidate_block_signatures: Mapping[str, str]) -> bool:
        if not self.owning_block_labels:
            return True
        for label in self.owning_block_labels:
            recorded = self.recorded_block_signatures.get(label)
            if recorded is None or candidate_block_signatures.get(label) != recorded:
                return True
        return False


class _RecordedBuildTestOutcomeContext(Protocol):
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None
    recorded_build_test_outcome_history: list[dict[str, object]]
    recorded_persisted_block_run_workflow_run_id: str | None
    recorded_outcome_grounding_requirement: RecordedOutcomeGroundingRequirement | None
    recorded_outcome_binding_constraint: RecordedOutcomeBindingConstraint | None


def record_build_test_outcome(ctx: _RecordedBuildTestOutcomeContext, outcome: RecordedBuildTestOutcome | None) -> None:
    if outcome is None:
        ctx.latest_recorded_build_test_outcome = None
        return
    ctx.latest_recorded_build_test_outcome = outcome
    history = getattr(ctx, "recorded_build_test_outcome_history", None)
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "phase": outcome.phase,
            "reason_code": outcome.reason_code,
            "verdict": outcome.verdict,
            "structural_key": outcome.structural_key,
            "is_authoritative": outcome.is_authoritative,
            "workflow_run_id": outcome.workflow_run_id,
            "authored_structure_signature": outcome.authored_structure_signature,
        }
    )
    del history[:-_HISTORY_LIMIT]
    ctx.recorded_build_test_outcome_history = history
    if outcome.phase == "persisted_block_run" and outcome.is_authoritative and outcome.workflow_run_id:
        ctx.recorded_persisted_block_run_workflow_run_id = outcome.workflow_run_id
    LOG.info(
        "copilot recorded build-test outcome stored",
        phase=outcome.phase,
        reason_code=outcome.reason_code,
        verdict=outcome.verdict,
        structural_key=outcome.structural_key,
        is_authoritative=outcome.is_authoritative,
        workflow_run_id=outcome.workflow_run_id,
        authored_structure_signature=outcome.authored_structure_signature,
    )


def authored_structure_signature_from_workflow(
    workflow_yaml: str | None,
    code_artifact_metadata: object = None,
) -> str | None:
    payload = _authored_structure_payload_from_workflow(workflow_yaml, code_artifact_metadata)
    if payload is None:
        return None
    return _stable_hash(payload)


def authored_block_signatures_from_workflow(
    workflow_yaml: str | None,
    code_artifact_metadata: object = None,
) -> dict[str, str]:
    payload = _authored_structure_payload_from_workflow(workflow_yaml, code_artifact_metadata)
    if payload is None:
        return {}
    signatures: dict[str, str] = {}
    code_blocks = payload.get("code_blocks")
    if not isinstance(code_blocks, list):
        return signatures
    for block in code_blocks:
        if not isinstance(block, Mapping):
            continue
        label = _safe_str(block.get("label"))
        if not label:
            continue
        signatures[label] = _stable_hash(
            {
                "code_hash": block.get("code_hash"),
                "parameter_keys": block.get("parameter_keys"),
                "output_metadata": block.get("output_metadata"),
            }
        )
    return signatures


def authored_block_parameter_keys_from_workflow(
    workflow_yaml: str | None,
    code_artifact_metadata: object = None,
) -> dict[str, list[str]]:
    payload = _authored_structure_payload_from_workflow(workflow_yaml, code_artifact_metadata)
    if payload is None:
        return {}
    result: dict[str, list[str]] = {}
    code_blocks = payload.get("code_blocks")
    if not isinstance(code_blocks, list):
        return result
    for block in code_blocks:
        if not isinstance(block, Mapping):
            continue
        label = _safe_str(block.get("label"))
        if not label:
            continue
        keys = block.get("parameter_keys")
        if isinstance(keys, list):
            result[label] = [_safe_str(key) for key in keys if _safe_str(key)]
    return result


def _binding_frontier_facet(outcome: RecordedBuildTestOutcome) -> BindingFrontierFacet:
    if outcome.reason_code == "scout_act_observe_hollow_after_interaction":
        return "unexecuted_submit"
    if outcome.runtime_output_repair_facts:
        return "amend_in_place"
    if outcome.missing_requested_output_facts:
        return "value_shape"
    if outcome.reason_code in {
        "sandbox_unresolved_name",
        "synthesized_parameter_binding_ambiguous",
        "required_input_unbound",
    }:
        return "amend_in_place"
    if outcome.reason_code in {"outcome_not_demonstrated", "no_meaningful_output", "runtime_missing_output_dependency"}:
        return "value_shape"
    return "selector_frontier"


def _bind_recorded_outcome_constraint(ctx: object, requirement: RecordedOutcomeGroundingRequirement) -> None:
    outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    payload = requirement.payload
    if not isinstance(outcome, RecordedBuildTestOutcome) or payload is None:
        return
    owning_labels = _clean_list(
        outcome.block_labels or ([outcome.attempted_block_label] if outcome.attempted_block_label else [])
    )
    recorded_signatures = authored_block_signatures_from_workflow(
        getattr(ctx, "workflow_yaml", None),
        getattr(ctx, "code_artifact_metadata", None),
    )
    constraint = RecordedOutcomeBindingConstraint(
        repeated_structural_key=requirement.structural_key,
        phase=outcome.phase,
        reason_code=outcome.reason_code,
        frontier_facet=_binding_frontier_facet(outcome),
        owning_block_labels=owning_labels,
        diagnostic_reason=payload.diagnostic_reason,
        workflow_run_id=requirement.workflow_run_id,
        recorded_block_signatures={
            label: recorded_signatures[label] for label in owning_labels if label in recorded_signatures
        },
    )
    ctx.recorded_outcome_binding_constraint = constraint  # type: ignore[attr-defined]
    LOG.info(
        "copilot recorded outcome binding bound",
        repeated_structural_key=constraint.repeated_structural_key,
        frontier_facet=constraint.frontier_facet,
        owning_block_labels=constraint.owning_block_labels,
        diagnostic_reason=constraint.diagnostic_reason,
        frontier_uncrossable=constraint.frontier_uncrossable,
        workflow_run_id=constraint.workflow_run_id,
    )


def latest_recorded_build_test_outcome_repeated(ctx: object) -> bool | None:
    history = getattr(ctx, "recorded_build_test_outcome_history", None)
    if not isinstance(history, list) or not history:
        return None
    latest = history[-1]
    if not isinstance(latest, dict) or not isinstance(latest.get("structural_key"), str):
        return None
    for previous in reversed(history[:-1]):
        if not isinstance(previous, dict):
            continue
        if previous.get("phase") == "scout_evaluate":
            continue
        previous_key = previous.get("structural_key")
        if isinstance(previous_key, str):
            return previous_key == latest["structural_key"]
    return None


def run_backed_repair_evidence_exists(ctx: object) -> bool:
    # Reached from the enforcement belt with an untyped ctx; a ctx without the latch must read as
    # "no run-backed evidence" so the guardrail fails safe instead of raising.
    run_id = getattr(ctx, "recorded_persisted_block_run_workflow_run_id", None)
    return isinstance(run_id, str) and bool(run_id)


def arm_recorded_outcome_grounding_requirement(ctx: object) -> RecordedOutcomeGroundingRequirement | None:
    outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if not isinstance(outcome, RecordedBuildTestOutcome) or not outcome.is_authoritative:
        return None
    structural_key = outcome.structural_key
    if not isinstance(structural_key, str):
        return None
    workflow_run_id = outcome.workflow_run_id
    if not isinstance(workflow_run_id, str) or not workflow_run_id:
        fallback_run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
        workflow_run_id = fallback_run_id if isinstance(fallback_run_id, str) and fallback_run_id else None
    if outcome.verdict == "progress_observed":
        return None
    repeated_key = latest_recorded_build_test_outcome_repeated(ctx) is True
    executed_run_outcome = workflow_run_id is not None
    if not repeated_key and not executed_run_outcome:
        return None
    existing = getattr(ctx, "recorded_outcome_grounding_requirement", None)
    if isinstance(existing, RecordedOutcomeGroundingRequirement) and existing.structural_key == structural_key:
        if existing.workflow_run_id == workflow_run_id:
            return existing
    requirement = RecordedOutcomeGroundingRequirement(
        phase=outcome.phase,
        reason_code=outcome.reason_code,
        structural_key=structural_key,
        workflow_run_id=workflow_run_id,
        block_labels=list(outcome.block_labels),
    )
    if workflow_run_id is None:
        ctx.composition_page_evidence = None  # type: ignore[attr-defined]
    ctx.recorded_outcome_grounding_requirement = requirement  # type: ignore[attr-defined]
    LOG.info(
        "copilot recorded outcome grounding armed",
        phase=requirement.phase,
        reason_code=requirement.reason_code,
        structural_key=requirement.structural_key,
        workflow_run_id=requirement.workflow_run_id,
        block_labels=requirement.block_labels,
        satisfied=False,
    )
    return requirement


def clear_recorded_outcome_grounding_requirement(ctx: object) -> None:
    ctx.recorded_outcome_grounding_requirement = None  # type: ignore[attr-defined]
    ctx.recorded_outcome_binding_constraint = None  # type: ignore[attr-defined]


def recorded_outcome_grounding_requires_current_page(ctx: object) -> bool:
    requirement = getattr(ctx, "recorded_outcome_grounding_requirement", None)
    if not isinstance(requirement, RecordedOutcomeGroundingRequirement) or requirement.satisfied:
        return False
    if isinstance(requirement.workflow_run_id, str) and requirement.workflow_run_id:
        return True
    evidence = getattr(ctx, "composition_page_evidence", None)
    if isinstance(evidence, dict) and _evidence_current_url(evidence):
        return True
    observed_urls = getattr(ctx, "observed_browser_urls", None)
    return isinstance(observed_urls, list) and any(isinstance(url, str) and url.strip() for url in observed_urls)


def maybe_satisfy_recorded_outcome_grounding_requirement(ctx: object) -> bool:
    requirement = getattr(ctx, "recorded_outcome_grounding_requirement", None)
    if not isinstance(requirement, RecordedOutcomeGroundingRequirement):
        return False
    evidence = getattr(ctx, "composition_page_evidence", None)
    payload = _grounding_payload_from_evidence(requirement, evidence)
    if payload is None:
        _log_grounding_rejection(requirement, evidence)
        return False
    satisfied_requirement = requirement.model_copy(update={"satisfied": True, "payload": payload})
    ctx.recorded_outcome_grounding_requirement = satisfied_requirement  # type: ignore[attr-defined]
    LOG.info(
        "copilot recorded outcome grounding satisfied",
        structural_key=requirement.structural_key,
        requirement_workflow_run_id=requirement.workflow_run_id,
        payload_workflow_run_id=payload.workflow_run_id,
        observed_after_workflow_run=payload.observed_after_workflow_run,
        source_tool=payload.source_tool,
    )
    _bind_recorded_outcome_constraint(ctx, satisfied_requirement)
    return True


def _log_grounding_rejection(requirement: RecordedOutcomeGroundingRequirement, evidence: object) -> None:
    evidence_dict = evidence if isinstance(evidence, dict) else {}
    LOG.info(
        "copilot recorded outcome grounding rejected",
        reject_reason=_grounding_reject_reason(requirement, evidence),
        structural_key=requirement.structural_key,
        requirement_workflow_run_id=requirement.workflow_run_id,
        evidence_workflow_run_id=evidence_dict.get("workflow_run_id"),
        evidence_observed_after_workflow_run=evidence_dict.get("observed_after_workflow_run"),
        source_tool=evidence_dict.get("source_tool"),
        current_url_present=_evidence_current_url(evidence_dict) is not None,
    )


def _grounding_reject_reason(
    requirement: RecordedOutcomeGroundingRequirement,
    evidence: object,
) -> Literal["not_inspect_source", "degraded_page", "run_id_mismatch", "no_url"]:
    if not isinstance(evidence, dict) or evidence.get("source_tool") != _INSPECT_PAGE_SOURCE_TOOL:
        return "not_inspect_source"
    if _evidence_current_url(evidence) is None:
        return "no_url"
    run_id = requirement.workflow_run_id
    if isinstance(run_id, str) and run_id:
        if evidence.get("observed_after_workflow_run") is not True or evidence.get("workflow_run_id") != run_id:
            return "run_id_mismatch"
    return "degraded_page"


def _grounding_payload_from_evidence(
    requirement: RecordedOutcomeGroundingRequirement,
    evidence: object,
) -> RecordedOutcomeGroundingPayload | None:
    if not isinstance(evidence, dict):
        return None
    if evidence.get("source_tool") != _INSPECT_PAGE_SOURCE_TOOL:
        return None
    current_url = _evidence_current_url(evidence)
    if current_url is None:
        return None
    run_id = requirement.workflow_run_id
    if isinstance(run_id, str) and run_id:
        if evidence.get("observed_after_workflow_run") is not True or evidence.get("workflow_run_id") != run_id:
            return None
    challenge_gated = _challenge_gated_page_evidence(evidence)
    capture_degraded = not has_bounded_page_schema(evidence)
    observed_empty_page = _observed_empty_page_evidence(evidence)
    # No-run degraded captures are typed grounding evidence; downstream binding remains a separate gate.
    diagnostic_reason: Literal["none", "empty_page", "challenge_gated", "capture_degraded"] = "none"
    if challenge_gated:
        diagnostic_reason = "challenge_gated"
    elif capture_degraded:
        diagnostic_reason = "capture_degraded"
    elif observed_empty_page:
        diagnostic_reason = "empty_page"
    title = evidence.get("page_title") or evidence.get("title")
    payload_run_id = evidence.get("workflow_run_id") if isinstance(evidence.get("workflow_run_id"), str) else None
    return RecordedOutcomeGroundingPayload(
        repeated_structural_key=requirement.structural_key,
        source_tool=_INSPECT_PAGE_SOURCE_TOOL,
        observed_after_workflow_run=evidence.get("observed_after_workflow_run") is True,
        workflow_run_id=payload_run_id,
        observed_empty_page=observed_empty_page,
        challenge_gated=challenge_gated,
        capture_degraded=capture_degraded,
        target_url=requirement.required_target_url,
        source_url=current_url,
        requirement_workflow_run_id=requirement.workflow_run_id,
        payload_workflow_run_id=payload_run_id,
        diagnostic_reason=diagnostic_reason,
        current_origin=_origin(current_url),
        current_url_present=True,
        current_title_present=isinstance(title, str) and bool(title.strip()),
        form_summaries=_form_summaries(evidence.get("forms")),
        result_container_summaries=_entry_summaries(
            evidence.get("result_containers"), ("selector", "text_excerpt", "row_selector")
        ),
        navigation_action_summaries=_entry_summaries(evidence.get("navigation_targets"), ("text", "selector")),
        challenge_control_summaries=_entry_summaries(evidence.get("challenge_controls"), ("text", "selector")),
    )


def _terminal_or_degraded_page_evidence(evidence: dict[str, object]) -> bool:
    if not has_bounded_page_schema(evidence):
        return True
    return _challenge_gated_page_evidence(evidence)


def _challenge_gated_page_evidence(evidence: dict[str, object]) -> bool:
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict) and (
        challenge_state.get("gates_submit_controls") is True
        or (challenge_state.get("detected") is True and challenge_state.get("requires_human_verification") is True)
    ):
        return True
    indicators = evidence.get("anti_bot_indicators")
    controls = evidence.get("challenge_controls")
    return (
        isinstance(indicators, list)
        and any(isinstance(item, str) and item.strip() for item in indicators)
        and isinstance(controls, list)
        and any(isinstance(item, dict) for item in controls)
    )


def _observed_empty_page_evidence(evidence: dict[str, object]) -> bool:
    if evidence.get("observed_empty_page") is True:
        return True
    for key in ("forms", "result_containers", "navigation_targets", "challenge_controls"):
        value = evidence.get(key)
        if not isinstance(value, list) or value:
            return False
    return True


def _evidence_current_url(evidence: dict[str, object]) -> str | None:
    value = evidence.get("current_url") or evidence.get("inspected_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _origin(value: str) -> str | None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _entry_summaries(value: object, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for entry in value[:5]:
        if not isinstance(entry, dict):
            continue
        parts = []
        for key in keys:
            item = entry.get(key)
            if isinstance(item, bool):
                parts.append("disabled" if item else "enabled")
            elif isinstance(item, str) and item.strip():
                parts.append(_bounded_text(item, 80))
        if parts:
            summaries.append(_bounded_text(" ".join(parts), 120))
    return summaries


def _form_summaries(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for form in value:
        if not isinstance(form, dict):
            continue
        summaries.extend(_entry_summaries(form.get("fields"), ("label", "selector")))
        summaries.extend(_entry_summaries(form.get("submit_controls"), ("text", "selector", "disabled")))
    return summaries[:5]


def recorded_outcome_from_authoring_repair_context(
    repair_context: CodeAuthoringRepairContext,
) -> RecordedBuildTestOutcome:
    reason_code = _authoring_reason_code(repair_context.reason_code)
    identity_payload = {
        "reason_code": repair_context.reason_code,
        "unresolved_names": sorted(repair_context.unresolved_names),
        "parameter_keys": sorted(repair_context.parameter_keys),
        "available_parameter_keys": sorted(repair_context.available_parameter_keys),
        "binding_candidates": sorted(repair_context.binding_candidates),
        "missing_output_key": _bounded_ref(repair_context.missing_output_key),
        "available_output_keys": sorted(repair_context.available_output_keys),
        "current_block_parameter_keys": sorted(repair_context.current_block_parameter_keys),
        "output_dependency_failure_class": _bounded_ref(repair_context.output_dependency_failure_class),
        "selector": _bounded_ref(repair_context.selector),
        "refiner_selector": _bounded_ref(repair_context.refiner_selector),
        "runtime_failure_class": _bounded_ref(repair_context.runtime_failure_class),
        "failed_block_status": _bounded_ref(repair_context.failed_block_status),
    }
    page_refs = _page_refs_from_authoring_context(repair_context)
    return RecordedBuildTestOutcome(
        phase="author_time_reject",
        attempted_tool="update_workflow",
        attempted_block_label=repair_context.block_label,
        verdict="authoring_rejected",
        reason_code=reason_code,
        block_labels=[repair_context.block_label],
        workflow_run_id=repair_context.workflow_run_id,
        structural_failure_identity="authoring:" + _stable_hash(identity_payload),
        page_evidence_refs=page_refs,
        observed_evidence_summary=_bounded_text(repair_context.runtime_failure_reason or repair_context.reason_code),
        key_provenance={
            "structural_failure_identity": "CodeAuthoringRepairContext structural fields",
            "page_evidence_refs": "CodeAuthoringRepairContext bounded page fields",
        },
    )


def recorded_outcome_from_author_time_reject(
    *,
    reason_code: BuildTestOutcomeReasonCode,
    attempted_tool: str = "update_workflow",
    attempted_block_label: str = "",
    block_labels: Sequence[str] = (),
    structural_failure_identity: str = "",
    structural_payload: Mapping[str, object] | None = None,
    authored_structure_signature: str | None = None,
    observed_evidence_summary: str = "",
    observed_page_value_excerpt: str = "",
    page_evidence_refs: Sequence[str] = (),
    missing_requested_output_facts: Sequence[Mapping[str, object]] = (),
) -> RecordedBuildTestOutcome:
    if structural_payload is not None:
        structural_failure_identity = "author_time:" + _stable_hash(structural_payload)
    return RecordedBuildTestOutcome(
        phase="author_time_reject",
        attempted_tool=attempted_tool,
        attempted_block_label=attempted_block_label,
        verdict="authoring_rejected",
        reason_code=reason_code,
        block_labels=_clean_list(block_labels),
        structural_failure_identity=structural_failure_identity,
        authored_structure_signature=authored_structure_signature,
        missing_requested_output_facts=[dict(fact) for fact in missing_requested_output_facts],
        observed_evidence_summary=_bounded_text(observed_evidence_summary),
        observed_page_value_excerpt=" ".join(observed_page_value_excerpt.split())[:_VALUE_EXCERPT_MAX],
        page_evidence_refs=_clean_list(page_evidence_refs),
        key_provenance={
            "structural_failure_identity": "author-time validator structural reason",
            "page_evidence_refs": "author-time validator structural refs",
        },
    )


def author_time_reject_missing_output_paths(latest: RecordedBuildTestOutcome | None) -> set[str]:
    if latest is None or latest.phase != "author_time_reject":
        return set()
    paths: set[str] = set()
    for fact in latest.missing_requested_output_facts:
        if not isinstance(fact, Mapping):
            continue
        output_path = fact.get("output_path")
        if isinstance(output_path, str) and output_path:
            paths.add(output_path)
    return paths


def recorded_outcome_from_loaded_result_evidence(
    evidence: LoadedResultCompositionEvidence,
) -> RecordedBuildTestOutcome:
    page_refs = [
        f"result_containers:{evidence.result_container_count}",
        f"table_result_containers:{evidence.table_result_container_count}",
    ]
    for target in evidence.targets:
        page_refs.append(
            "target:"
            + _stable_hash(
                {
                    "is_table": target.is_table,
                    "row_count": target.row_count,
                    "structure_signature": target.structure_signature,
                }
            )
        )
    return RecordedBuildTestOutcome(
        phase="scout_evaluate",
        attempted_tool="evaluate",
        attempted_target="loaded_result_targets",
        verdict="progress_observed",
        reason_code="loaded_result_targets_observed",
        structural_failure_identity=f"loaded_result:{evidence.structure_signature}",
        page_evidence_refs=page_refs,
        observed_evidence_summary=(
            f"{evidence.result_container_count} result container(s), "
            f"{evidence.table_result_container_count} table-like container(s)."
        ),
        key_provenance={
            "structural_failure_identity": "LoadedResultCompositionEvidence.structure_signature",
            "page_evidence_refs": "loaded-result target structural signatures",
        },
    )


def recorded_outcome_from_scout_act_observe_hollow(
    *,
    interaction_tool: str,
    selector: str,
    current_url: str,
    source_url: str | None,
    page_evidence: Mapping[str, object] | None,
    recapture_attempted: bool,
    recapture_result: str,
) -> RecordedBuildTestOutcome:
    shape = _hollow_page_shape(page_evidence)
    source_origin = _origin_ref(source_url)
    current_origin = _origin_ref(current_url)
    bounded_recapture_result = _bounded_ref(recapture_result)
    value_excerpt = _observed_page_value_excerpt(page_evidence)
    LOG.info(
        "copilot_hollow_value_carry",
        reason_code="scout_act_observe_hollow_after_interaction",
        value_excerpt_len=len(value_excerpt),
        value_excerpt_sha8=hashlib.sha256(value_excerpt.encode()).hexdigest()[:8] if value_excerpt else "",
        current_origin=current_origin,
    )
    structural_payload = {
        "interaction_tool": _bounded_ref(interaction_tool),
        "selector": _bounded_ref(selector),
        "source_origin": source_origin,
        "current_origin": current_origin,
        "shape": shape,
        "recapture_attempted": recapture_attempted,
        "recapture_result": bounded_recapture_result,
    }
    page_refs = list(dict.fromkeys(ref for ref in (source_origin, current_origin) if ref))
    page_refs.extend(
        [
            f"forms:{shape['form_count']}",
            f"navigation_targets:{shape['navigation_target_count']}",
            f"result_containers:{shape['result_container_count']}",
            f"clickable_controls:{shape['clickable_control_count']}",
            f"recapture_attempted:{str(recapture_attempted).lower()}",
            f"recapture_result:{bounded_recapture_result}",
        ]
    )
    return RecordedBuildTestOutcome(
        phase="scout_evaluate",
        attempted_tool="scout_interaction",
        attempted_target=_bounded_ref(selector),
        verdict="repairable_failure",
        reason_code="scout_act_observe_hollow_after_interaction",
        structural_failure_identity="scout_act_observe:" + _stable_hash(structural_payload),
        page_evidence_refs=page_refs,
        observed_evidence_summary="Scout interaction reached the page, but bounded page evidence stayed hollow.",
        observed_page_value_excerpt=value_excerpt,
        key_provenance={
            "structural_failure_identity": "scout interaction identity and bounded hollow page shape",
            "page_evidence_refs": "scout interaction source/current URL origins and structural counts",
        },
    )


def _required_input_unbound_identity(
    failed_block: Mapping[str, object] | None,
    referenced_unbound_keys: Sequence[str],
) -> str:
    return "required_input_unbound:" + _stable_hash(
        {
            "source": "required_input_unbound",
            "referenced_unbound_keys": sorted({str(key) for key in referenced_unbound_keys}),
            "block_label": _safe_str(failed_block.get("label")) if failed_block is not None else "",
            "block_status": _safe_str(failed_block.get("status")) if failed_block is not None else "",
        }
    )


def _required_input_unbound_outcome(
    failed_block: Mapping[str, object] | None,
    block_labels: list[str],
    requested_block_labels: list[str],
    block_shape_hashes: Mapping[str, str],
    workflow_run_id: str | None,
    authored_structure_signature: str | None,
    referenced_unbound_keys: Sequence[str],
) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label=_safe_str(failed_block.get("label")) if failed_block is not None else "",
        verdict="repairable_failure",
        reason_code="required_input_unbound",
        workflow_run_id=workflow_run_id or None,
        block_labels=block_labels,
        requested_block_labels=requested_block_labels,
        block_shape_hashes=dict(block_shape_hashes),
        structural_failure_identity=_required_input_unbound_identity(failed_block, referenced_unbound_keys),
        authored_structure_signature=authored_structure_signature,
        observed_evidence_summary=_bounded_text(
            "unbound required inputs: " + ", ".join(_clean_list(referenced_unbound_keys))
        ),
        key_provenance={
            "structural_failure_identity": (
                "resolution-seam unbound required parameter keys referenced by the failed block"
            )
        },
    )


def recorded_outcome_from_run_blocks_result(
    result: Mapping[str, object],
    *,
    page_evidence: Mapping[str, object] | None = None,
    recorded_run_outcome: RecordedRunOutcome | None = None,
    completion_verification: CompletionVerificationResult | None = None,
    authored_structure_signature: str | None = None,
    registered_output_parameter_payloads: Sequence[Mapping[str, object]] | None = None,
    unbound_required_parameter_keys: Sequence[str] | None = None,
    block_parameter_keys: Mapping[str, Sequence[str]] | None = None,
    block_shape_hashes: Mapping[str, str] | None = None,
) -> RecordedBuildTestOutcome | None:
    data = _dict(result.get("data"))
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    blocks = _block_dicts(data.get("blocks"))
    failed_block = _first_failed_block(blocks)
    block_labels = [_safe_str(block.get("label")) for block in blocks if _safe_str(block.get("label"))]
    requested = data.get("requested_block_labels")
    requested_block_labels = (
        _clean_list([label for label in requested if isinstance(label, str)]) if isinstance(requested, list) else []
    )
    block_shape_hashes = dict(block_shape_hashes or {})
    referenced_unbound_keys = _referenced_unbound_input_keys(
        result,
        failed_block,
        unbound_required_parameter_keys or [],
        block_parameter_keys or {},
    )
    page_refs = _page_evidence_refs(page_evidence)
    output_refs = _output_evidence_refs(blocks)
    verification_identity = _completion_verification_identity(completion_verification)
    missing_output_facts = _missing_requested_output_facts(completion_verification, blocks)
    authoritative_workflow_run_id = (
        recorded_run_outcome.workflow_run_id if recorded_run_outcome is not None else None
    ) or workflow_run_id
    runtime_output_facts = _runtime_output_repair_facts(
        completion_verification,
        blocks,
        registered_output_parameter_payloads or _mapping_list(data.get("registered_output_parameter_values")),
        authoritative_workflow_run_id,
    )
    if recorded_run_outcome is not None:
        reason_code = _run_outcome_reason_code(recorded_run_outcome)
        if reason_code == "terminal_challenge_blocker":
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code=reason_code,
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "terminal blocker precedence suppresses repair prompt"},
            )
        if recorded_run_outcome.verdict == "demonstrated":
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="progress_observed",
                reason_code="verified_success",
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                verified_progress_marker=verification_identity or "run_completed_verified",
                evidence_refs=output_refs,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "Completion verification passed.",
                key_provenance={
                    "verified_progress_marker": "CompletionVerificationResult satisfied criteria",
                    "evidence_refs": "run output structure",
                },
            )
        if recorded_run_outcome.verdict == "not_evaluated":
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code=reason_code,
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "run outcome was not evaluated"},
            )
        if referenced_unbound_keys:
            return _required_input_unbound_outcome(
                failed_block,
                block_labels,
                requested_block_labels,
                block_shape_hashes,
                recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                authored_structure_signature,
                referenced_unbound_keys,
            )
        structural_identity = verification_identity
        evidence_refs = output_refs
        if not structural_identity and not page_refs and not evidence_refs:
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code=reason_code,
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "no typed verification/page/output identity available"},
            )
        if (
            completion_verification is not None
            and only_degraded_blocking(completion_verification)
            and _recorded_outcome_degrade_eligible(recorded_run_outcome, failed_block)
        ):
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code="fallback_floor_turn_unsatisfiable",
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "turn-unsatisfiable fallback floor, no reachable route"},
            )
        return RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code=reason_code,
            workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
            block_labels=block_labels,
            requested_block_labels=requested_block_labels,
            block_shape_hashes=block_shape_hashes,
            structural_failure_identity=structural_identity,
            page_evidence_refs=page_refs,
            evidence_refs=evidence_refs,
            missing_requested_output_facts=missing_output_facts,
            runtime_output_repair_facts=runtime_output_facts,
            authored_structure_signature=authored_structure_signature,
            observed_evidence_summary=recorded_run_outcome.display_reason or "",
            key_provenance={
                "structural_failure_identity": "CompletionVerificationResult verdict structure",
                "page_evidence_refs": "bounded post-run page evidence",
                "evidence_refs": "run output structure",
                "missing_requested_output_facts": "CompletionVerificationResult unsatisfied output paths and run output shape",
                "runtime_output_repair_facts": "same-run registered output parameters and completion verdicts",
            },
        )
    run_status = _safe_str(data.get("overall_status"))
    failure_type = _safe_str(data.get("failure_type"))
    failure_categories = _failure_category_refs(carrier_backed_anti_bot_categories(data.get("failure_categories")))
    status = _safe_str(failed_block.get("status")) if failed_block is not None else run_status
    runtime_failure_identity = _runtime_failure_identity(failed_block)
    if referenced_unbound_keys:
        return _required_input_unbound_outcome(
            failed_block,
            block_labels,
            requested_block_labels,
            block_shape_hashes,
            workflow_run_id or None,
            authored_structure_signature,
            referenced_unbound_keys,
        )
    if not (failure_categories or failure_type or runtime_failure_identity or page_refs or output_refs):
        return None
    structural_identity = (
        _stable_hash(
            {
                "failure_type": failure_type,
                "failure_categories": failure_categories,
                "runtime_failure_identity": runtime_failure_identity,
                "status": status,
            }
        )
        if failure_categories or failure_type or runtime_failure_identity
        else ""
    )
    verdict: BuildTestOutcomeVerdict = "repairable_failure" if bool(result.get("ok")) is False else "progress_observed"
    if not structural_identity and not page_refs and not output_refs:
        verdict = "not_authoritative"
    reason_code = "runtime_block_failure" if failed_block is not None or not bool(result.get("ok")) else "failed_run"
    has_runtime_failure_evidence = bool(failure_categories or failure_type or runtime_failure_identity or failed_block)
    if (
        verdict == "repairable_failure"
        and not has_runtime_failure_evidence
        and completion_verification is not None
        and only_degraded_blocking(completion_verification)
    ):
        verdict = "not_authoritative"
        reason_code = "fallback_floor_turn_unsatisfiable"
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label=_safe_str(failed_block.get("label")) if failed_block is not None else "",
        verdict=verdict,
        reason_code=reason_code,
        workflow_run_id=workflow_run_id or None,
        block_labels=block_labels,
        requested_block_labels=requested_block_labels,
        block_shape_hashes=block_shape_hashes,
        structural_failure_identity=structural_identity,
        page_evidence_refs=page_refs,
        evidence_refs=output_refs,
        authored_structure_signature=authored_structure_signature,
        observed_evidence_summary=_bounded_text(run_status),
        key_provenance={
            "structural_failure_identity": (
                "typed runtime failure structure"
                if runtime_failure_identity
                else "typed failure categories or failure_type"
            ),
            "page_evidence_refs": "bounded post-run page evidence",
            "evidence_refs": "run output structure",
        },
    )


def _stable_hash(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authored_structure_payload_from_workflow(
    workflow_yaml: str | None,
    code_artifact_metadata: object,
) -> dict[str, object] | None:
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return None
    parsed = _parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return None
    definition = _dict(parsed.get("workflow_definition"))
    code_blocks = _code_block_signature_payloads(definition.get("blocks"))
    if not code_blocks:
        return None
    metadata_by_label = _artifact_metadata_by_label(code_artifact_metadata)
    return {
        "version": _AUTHORED_STRUCTURE_VERSION,
        "workflow_parameter_keys": _workflow_parameter_keys(definition),
        "code_blocks": [
            {
                **block,
                "output_metadata": _artifact_output_metadata_signature(metadata_by_label.get(str(block["label"]))),
            }
            for block in code_blocks
        ],
    }


def _parse_workflow_yaml(workflow_yaml: str) -> object:
    try:
        return yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None


def _workflow_parameter_keys(definition: Mapping[str, object]) -> list[str]:
    keys: list[str] = []
    for parameter in _mapping_list(definition.get("parameters")):
        key = _safe_str(parameter.get("key"))
        if key:
            keys.append(key)
    return sorted(dict.fromkeys(keys))


def _code_block_signature_payloads(value: object) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for block in _mapping_list(value):
        block_type = _safe_str(block.get("block_type")).lower()
        if block_type == "code":
            label = _safe_str(block.get("label"))
            code = block.get("code")
            if label and isinstance(code, str):
                payloads.append(
                    {
                        "label": label,
                        "code_hash": _stable_hash(_normalized_code_text(code)),
                        "parameter_keys": _string_list(block.get("parameter_keys")),
                    }
                )
        for child_key in ("blocks",):
            payloads.extend(_code_block_signature_payloads(block.get(child_key)))
        for branch in _mapping_list(block.get("branch_conditions")):
            payloads.extend(_code_block_signature_payloads(branch.get("blocks")))
    return sorted(payloads, key=lambda item: str(item.get("label")))


def _authoring_reason_code(value: str) -> BuildTestOutcomeReasonCode:
    if value == SANDBOX_UNRESOLVED_NAME_REASON_CODE:
        return "sandbox_unresolved_name"
    if value == "synthesized_parameter_binding_ambiguous":
        return "synthesized_parameter_binding_ambiguous"
    if value == "runtime_block_failure":
        return "runtime_block_failure"
    if value == "runtime_missing_output_dependency":
        return "runtime_missing_output_dependency"
    if value == "select_option_interaction_mismatch":
        # Select-option mismatches are author-time policy rejects, not a separate outcome class.
        return "code_safety_reject"
    return "code_safety_reject"


def _normalized_code_text(code: str) -> str:
    return "\n".join(line.rstrip() for line in textwrap.dedent(code).strip().splitlines())


def _artifact_metadata_by_label(code_artifact_metadata: object) -> dict[str, Mapping[str, object]]:
    rows: Iterable[tuple[object, object]]
    if isinstance(code_artifact_metadata, Mapping):
        rows = code_artifact_metadata.items()
    elif isinstance(code_artifact_metadata, list):
        rows = [(None, row) for row in code_artifact_metadata]
    else:
        return {}
    by_label: dict[str, Mapping[str, object]] = {}
    for fallback_label, row in rows:
        if not isinstance(row, Mapping):
            continue
        label = _safe_str(row.get("block_label")) or _safe_str(fallback_label)
        if label:
            by_label[label] = row
    return by_label


def _artifact_output_metadata_signature(artifact: Mapping[str, object] | None) -> dict[str, object]:
    if artifact is None:
        return {}
    return {
        "claimed_outcomes": _output_rows_signature(artifact.get("claimed_outcomes")),
        "terminal_verifier_expectations": _output_rows_signature(artifact.get("terminal_verifier_expectations")),
    }


def _output_rows_signature(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in _mapping_list(value):
        rows.append(
            {
                "goal_value_paths": _string_list(row.get("goal_value_paths")),
                "extraction_schema_paths": _extraction_schema_paths(row.get("extraction_schema")),
            }
        )
    return rows


def _extraction_schema_paths(value: object) -> list[str]:
    schema: object = value
    if isinstance(value, str):
        try:
            schema = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(schema, Mapping):
        return []
    paths: list[str] = []
    _collect_schema_paths(schema, prefix="", paths=paths)
    return sorted(dict.fromkeys(paths))


def _collect_schema_paths(schema: Mapping[str, object], *, prefix: str, paths: list[str]) -> None:
    schema_type = _safe_str(schema.get("type"))
    if prefix:
        paths.append(f"{prefix}:{schema_type or 'unknown'}")
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for key, child in sorted(properties.items(), key=lambda item: str(item[0])):
            if isinstance(child, Mapping):
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                _collect_schema_paths(child, prefix=child_prefix, paths=paths)
    items = schema.get("items")
    if isinstance(items, Mapping):
        _collect_schema_paths(items, prefix=f"{prefix}[]" if prefix else "[]", paths=paths)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(dict.fromkeys(item for item in value if isinstance(item, str)))


def _bounded_text(value: object, max_chars: int = _TEXT_MAX) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_raw_secrets_for_prompt(" ".join(value.split()))
    return text[:max_chars]


def _bounded_ref(value: object, max_chars: int = _REF_TEXT_MAX) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_chars]


def _safe_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _block_dicts(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _first_failed_block(blocks: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    for block in blocks:
        if _safe_str(block.get("status")).lower() in {"failed", "terminated", "canceled", "timed_out"}:
            return block
    return None


def _referenced_unbound_input_keys(
    result: Mapping[str, object],
    failed_block: Mapping[str, object] | None,
    unbound_required_parameter_keys: Sequence[str],
    block_parameter_keys: Mapping[str, Sequence[str]],
) -> list[str]:
    if bool(result.get("ok")) is not False or failed_block is None:
        return []
    label = _safe_str(failed_block.get("label"))
    referenced = block_parameter_keys.get(label) if label else None
    if not referenced:
        return []
    referenced_set = set(referenced)
    return [key for key in dict.fromkeys(unbound_required_parameter_keys) if key in referenced_set]


def _runtime_failure_identity(failed_block: Mapping[str, object] | None) -> str:
    if failed_block is None:
        return ""
    failure_reason = _safe_str(failed_block.get("failure_reason"))
    if not failure_reason:
        return ""
    locator_wait_match = _PLAYWRIGHT_LOCATOR_WAIT_RE.search(failure_reason)
    if locator_wait_match is None:
        return ""
    hidden_tag_match = _PLAYWRIGHT_HIDDEN_TAG_RE.search(failure_reason)
    selector = _bounded_ref(locator_wait_match.group("selector"))
    locator_chain = _bounded_ref(locator_wait_match.group("locator_chain"))
    state = _bounded_ref(locator_wait_match.group("state").casefold())
    hidden_tag = _bounded_ref(hidden_tag_match.group("tag").casefold()) if hidden_tag_match is not None else ""
    return _stable_hash(
        {
            "source": "playwright_locator_wait",
            "selector": selector,
            "locator_chain": locator_chain,
            "state": state,
            "hidden_tag": hidden_tag,
            "block_label": _safe_str(failed_block.get("label")),
            "block_status": _safe_str(failed_block.get("status")),
        }
    )


def _clean_list(values: Sequence[str]) -> list[str]:
    return [cleaned for value in values for cleaned in [_bounded_ref(value)] if cleaned]


def _origin_ref(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"origin:{parsed.scheme}://{parsed.netloc}"


def _observed_page_value_excerpt(page_evidence: Mapping[str, object] | None) -> str:
    evidence = page_evidence or {}
    for key in ("visible_text_excerpt", "visible_text", "bodyText"):
        text = _safe_str(evidence.get(key))
        if text.strip():
            return " ".join(text.split())[:_VALUE_EXCERPT_MAX]
    return ""


def observed_value_extraction_scaffold_lines(observed_values: str, output_paths: Sequence[str]) -> list[str]:
    observed_values = " ".join(observed_values.split())[:_VALUE_EXCERPT_MAX]
    if not observed_values:
        return []
    paths = [path for path in dict.fromkeys(str(p).strip() for p in output_paths) if path]
    if not paths:
        return [f"observed_page_values: {observed_values}"]
    lines = [
        "OBSERVED PAGE VALUES CONTRACT: author a keyed extraction over the on-screen values below and bind "
        "each required output_path to its observed value.",
        f"observed_values: {observed_values}",
        "bind_output_paths:",
    ]
    lines.extend(f"- {path}: <observed value>" for path in paths[:8])
    return lines


def _hollow_page_shape(page_evidence: Mapping[str, object] | None) -> dict[str, object]:
    evidence = page_evidence or {}
    challenge_state = evidence.get("challenge_state")
    return {
        "page_title_present": bool(_safe_str(evidence.get("page_title"))),
        "schema_empty_page": evidence.get("schema_empty_page") is True,
        "body_has_markup": bool(_safe_str(evidence.get("body")) or _safe_str(evidence.get("html"))),
        "visible_text_present": bool(_safe_str(evidence.get("visible_text")) or _safe_str(evidence.get("bodyText"))),
        "form_count": _bounded_len(evidence.get("forms")),
        "navigation_target_count": _bounded_len(evidence.get("navigation_targets")),
        "result_container_count": _bounded_len(evidence.get("result_containers")),
        "clickable_control_count": _bounded_len(evidence.get("clickable_controls")),
        "modal_overlay_count": _bounded_len(evidence.get("modal_overlays")),
        "challenge_detected": isinstance(challenge_state, Mapping) and challenge_state.get("detected") is True,
    }


def _bounded_len(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _page_refs_from_authoring_context(repair_context: CodeAuthoringRepairContext) -> list[str]:
    refs: list[str] = []
    if repair_context.current_origin:
        refs.append(f"origin:{_bounded_ref(repair_context.current_origin)}")
    for summary in repair_context.page_form_summaries[:3]:
        refs.append(f"form:{_bounded_ref(summary)}")
    for summary in repair_context.page_result_summaries[:3]:
        refs.append(f"result:{_bounded_ref(summary)}")
    for summary in repair_context.page_action_summaries[:3]:
        refs.append(f"action:{_bounded_ref(summary)}")
    return refs


def _page_evidence_refs(page_evidence: Mapping[str, object] | None) -> list[str]:
    if page_evidence is None:
        return []
    refs: list[str] = []
    origin = _origin_ref(page_evidence.get("current_url")) or _origin_ref(page_evidence.get("inspected_url"))
    if origin:
        refs.append(origin)
    refs.extend(_form_refs(page_evidence.get("forms")))
    refs.extend(_result_refs(page_evidence.get("result_containers")))
    refs.extend(_action_refs(page_evidence.get("navigation_targets")))
    return refs[:12]


def _form_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for form in value[:3]:
        if not isinstance(form, Mapping):
            continue
        for field in _mapping_list(form.get("fields"))[:3]:
            label = _bounded_ref(field.get("label"))
            selector = _bounded_ref(field.get("selector"))
            if label or selector:
                refs.append(f"form:{' '.join(item for item in (label, selector) if item)}")
        for control in _mapping_list(form.get("submit_controls"))[:2]:
            text = _bounded_ref(control.get("text"))
            selector = _bounded_ref(control.get("selector"))
            if text or selector:
                refs.append(f"submit:{' '.join(item for item in (text, selector) if item)}")
    return refs


def _result_refs(value: object) -> list[str]:
    refs: list[str] = []
    for container in _mapping_list(value)[:4]:
        selector = _bounded_ref(container.get("selector")) or "unknown"
        row_count = container.get("row_count")
        row_text = str(row_count) if isinstance(row_count, int) else "unknown"
        refs.append(f"result:{selector} rows={row_text}")
    return refs


def _action_refs(value: object) -> list[str]:
    refs: list[str] = []
    for action in _mapping_list(value)[:4]:
        text = _bounded_ref(action.get("text"))
        selector = _bounded_ref(action.get("selector"))
        if text or selector:
            refs.append(f"action:{' '.join(item for item in (text, selector) if item)}")
    return refs


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _run_outcome_reason_code(recorded_run_outcome: RecordedRunOutcome) -> BuildTestOutcomeReasonCode:
    reason_code = recorded_run_outcome.reason_code
    if reason_code in {
        "outcome_not_demonstrated",
        "no_meaningful_output",
        "terminal_challenge_blocker",
        "blocker_reported",
    }:
        return reason_code
    if recorded_run_outcome.verdict == "demonstrated":
        return "verified_success"
    return "failed_run"


def _completion_verification_identity(completion_verification: CompletionVerificationResult | None) -> str:
    if completion_verification is None or completion_verification.status != "evaluated":
        return ""
    verdict_payload = [
        {
            "criterion_id": verdict.criterion_id,
            "state": verdict.state,
            "reason_code": verdict.reason_code,
            "output_path": verdict.output_path,
            "grounding_mode": verdict.grounding_mode,
            "expected_output_shape": verdict.expected_output_shape,
            "has_exact_value": verdict.has_exact_value,
        }
        for verdict in completion_verification.verdicts
    ]
    payload = {
        "criterion_ids": sorted(completion_verification.criterion_ids),
        "verdicts": verdict_payload,
        "no_gradeable_run_plane": completion_verification.no_gradeable_run_plane,
        "structural_unfired_criterion_ids": sorted(completion_verification.structural_unfired_criterion_ids),
    }
    return "completion:" + _stable_hash(payload)


def _missing_requested_output_facts(
    completion_verification: CompletionVerificationResult | None,
    blocks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if completion_verification is None or completion_verification.status != "evaluated":
        return []
    empty_output_block_labels = _empty_output_block_labels(blocks)
    facts: list[dict[str, object]] = []
    for verdict in completion_verification.verdicts:
        if verdict.satisfied or not verdict.output_path:
            continue
        if _has_presence_only_output_evidence(verdict):
            continue
        output_path = _bounded_ref(verdict.output_path)
        output_root = _output_path_root(output_path)
        if not output_root:
            continue
        fact: dict[str, object] = {
            "criterion_id": _bounded_ref(verdict.criterion_id),
            "output_path": output_path,
            "output_root": output_root,
            "reason_code": _bounded_ref(verdict.reason_code),
            "value_status": _output_path_value_status(blocks, output_path, verdict),
        }
        if verdict.grounding_mode:
            fact["grounding_mode"] = verdict.grounding_mode
        if verdict.expected_output_shape:
            fact["expected_output_shape"] = _bounded_ref(verdict.expected_output_shape)
        if empty_output_block_labels:
            fact["empty_output_block_labels"] = empty_output_block_labels
        partial_labels = _partial_output_block_labels(blocks, output_root)
        if partial_labels:
            fact["partial_output_block_labels"] = partial_labels
        facts.append(fact)
    return sorted(facts, key=lambda item: str(item.get("output_path") or ""))


def _runtime_output_repair_facts(
    completion_verification: CompletionVerificationResult | None,
    blocks: Sequence[Mapping[str, object]],
    registered_output_parameter_payloads: Sequence[Mapping[str, object]],
    workflow_run_id: str,
) -> list[dict[str, object]]:
    if completion_verification is None or completion_verification.status != "evaluated" or not workflow_run_id:
        return []
    facts: list[dict[str, object]] = []
    for verdict in completion_verification.verdicts:
        if not verdict.output_path:
            continue
        output_path = _bounded_ref(verdict.output_path)
        if not _output_path_has_child(output_path):
            continue
        output_root = _output_path_root(output_path)
        if not output_root:
            continue
        values, evidence_refs, block_labels = _runtime_output_values_for_path(
            blocks,
            registered_output_parameter_payloads,
            workflow_run_id,
            output_path,
        )
        owner_labels = _runtime_output_owner_labels(blocks, block_labels, verdict)
        if verdict.satisfied:
            if not owner_labels:
                continue
            value_status = "satisfied"
        else:
            value_status = _runtime_output_value_status(values, verdict)
        fact: dict[str, object] = {
            "workflow_run_id": _bounded_ref(workflow_run_id),
            "output_path": output_path,
            "output_root": output_root,
            "criterion_id": _bounded_ref(verdict.criterion_id),
            "reason_code": _bounded_ref(verdict.reason_code),
            "value_status": value_status,
        }
        if verdict.satisfied or len(owner_labels) > 1:
            fact["owner_labels"] = owner_labels
        if len(owner_labels) == 1:
            fact["block_label"] = owner_labels[0]
        if verdict.grounding_mode:
            fact["grounding_mode"] = verdict.grounding_mode
        if verdict.expected_output_shape:
            fact["expected_output_shape"] = _bounded_ref(verdict.expected_output_shape)
        if evidence_refs:
            fact["evidence_refs"] = evidence_refs
        facts.append(fact)
    return sorted(facts, key=lambda item: str(item.get("output_path") or ""))


def _has_presence_only_output_evidence(verdict: CriterionVerdict) -> bool:
    return (
        verdict.reason_code == "structurally_abstained"
        and verdict.grounding_mode == "missing"
        and isinstance(verdict.evidence_ref, str)
        and bool(verdict.evidence_ref.strip())
    )


def _output_path_root(output_path: str) -> str:
    return _bounded_ref(output_path.split(".", 1)[0].split("[", 1)[0])


def _output_path_has_child(output_path: str) -> bool:
    return "." in output_path or "[]" in output_path


def _output_path_value_status(
    blocks: Sequence[Mapping[str, object]],
    output_path: str,
    verdict: object,
) -> str:
    values: list[object] = []
    for block in blocks:
        extracted = block.get("extracted_data")
        if extracted is None:
            continue
        value, present = _value_at_output_path(extracted, output_path)
        if present:
            values.append(value)
    if not values:
        return "no_typed_value"
    if all(_is_empty_output_value(value) for value in values):
        return "empty_typed_value"
    grounding_mode = getattr(verdict, "grounding_mode", None)
    has_exact_value = getattr(verdict, "has_exact_value", False)
    if grounding_mode in {"shape", "judgment_boolean"} and not has_exact_value:
        return "presence_only_evidence"
    return "typed_value_unverified"


def _runtime_output_values_for_path(
    blocks: Sequence[Mapping[str, object]],
    registered_output_parameter_payloads: Sequence[Mapping[str, object]],
    workflow_run_id: str,
    output_path: str,
) -> tuple[list[object], list[str], list[str]]:
    values: list[object] = []
    evidence_refs: list[str] = []
    block_labels: list[str] = []
    current_labels = {label for block in blocks for label in [_bounded_ref(block.get("label"))] if label}
    for item in registered_output_parameter_payloads:
        item_run_id = _safe_str(item.get("workflow_run_id"))
        if item_run_id != workflow_run_id:
            continue
        value, present = _registered_output_value_for_path(item, output_path)
        if not present:
            continue
        values.append(value)
        label = _registered_output_owner_label(item, current_labels)
        key = _bounded_ref(item.get("output_parameter_key"))
        if label:
            block_labels.append(label)
        if label or key:
            evidence_refs.append(f"registered_output:{label or 'unknown'}:{key or output_path}")
    for block in blocks:
        extracted = block.get("extracted_data")
        if extracted is None:
            continue
        value, present = _value_at_output_path(extracted, output_path)
        if not present:
            continue
        values.append(value)
        label = _bounded_ref(block.get("label"))
        if label:
            block_labels.append(label)
            evidence_refs.append(f"output:{label}")
    return values, list(dict.fromkeys(evidence_refs)), sorted(dict.fromkeys(block_labels))


def _registered_output_owner_label(item: Mapping[str, object], current_labels: set[str]) -> str:
    label = _bounded_ref(item.get("block_label"))
    if label in current_labels:
        return label
    return ""


def _runtime_output_owner_labels(
    blocks: Sequence[Mapping[str, object]],
    block_labels: Sequence[str],
    verdict: CriterionVerdict,
) -> list[str]:
    if not verdict.satisfied and verdict.requested_output_evidence_source == "independent_run_evidence":
        return []
    current_labels = {label for block in blocks for label in [_bounded_ref(block.get("label"))] if label}
    labels = {label for label in block_labels if label in current_labels}
    evidence_label = _block_output_evidence_ref_label(verdict.evidence_ref)
    if evidence_label in current_labels:
        labels.add(evidence_label)
    return sorted(labels)


def _block_output_evidence_ref_label(evidence_ref: str | None) -> str:
    if not evidence_ref or not evidence_ref.startswith("block_outputs:"):
        return ""
    return _bounded_ref(evidence_ref.removeprefix("block_outputs:").split(".", 1)[0])


def _registered_output_value_for_path(item: Mapping[str, object], output_path: str) -> tuple[object | None, bool]:
    value = item.get("value")
    key = _safe_str(item.get("output_parameter_key"))
    if key == output_path:
        return value, True
    if output_path.startswith("output.") and key == output_path.split(".", 1)[1]:
        return value, True
    if isinstance(value, Mapping):
        if output_path.startswith("output."):
            unwrapped_value, unwrapped_present = _value_at_output_path(value, output_path.split(".", 1)[1])
            if unwrapped_present:
                return unwrapped_value, True
        return _value_at_output_path(value, output_path)
    return None, False


def _runtime_output_value_status(values: Sequence[object], verdict: CriterionVerdict) -> str:
    if values:
        if any(value is None for value in values):
            return "null"
        expected_shape = (verdict.expected_output_shape or "").casefold()
        if expected_shape in {"string", "str"} and any(not isinstance(value, str) for value in values):
            return "type_mismatch"
        if expected_shape in {"array", "list"} and any(not isinstance(value, list) for value in values):
            return "shape_mismatch"
        if expected_shape in {"object", "dict"} and any(not isinstance(value, Mapping) for value in values):
            return "shape_mismatch"
        return "no_typed_value" if not verdict.has_exact_value else "type_mismatch"
    if verdict.reason_code == "structurally_abstained":
        return "structural_abstained"
    return "no_typed_value"


def _value_at_output_path(value: object, output_path: str) -> tuple[object | None, bool]:
    current = value
    for segment in [part for part in re.split(r"\.|\[\]", output_path) if part]:
        if isinstance(current, Mapping):
            if segment not in current:
                return None, False
            current = current[segment]
            continue
        if isinstance(current, list):
            found_values = [item.get(segment) for item in current if isinstance(item, Mapping) and segment in item]
            if not found_values:
                return None, False
            current = found_values
            continue
        return None, False
    return current, True


def _is_empty_output_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _empty_output_block_labels(blocks: Sequence[Mapping[str, object]]) -> list[str]:
    labels: list[str] = []
    for block in blocks:
        extracted = block.get("extracted_data")
        if extracted is None:
            continue
        if _is_empty_output_value(extracted):
            label = _bounded_ref(block.get("label"))
            if label:
                labels.append(label)
    return labels


def _partial_output_block_labels(blocks: Sequence[Mapping[str, object]], output_root: str) -> list[str]:
    labels: list[str] = []
    for block in blocks:
        extracted = block.get("extracted_data")
        if not isinstance(extracted, Mapping) or output_root in extracted or not extracted:
            continue
        label = _bounded_ref(block.get("label"))
        if label:
            labels.append(label)
    return labels


def _failure_category_refs(value: object) -> list[str]:
    refs: list[str] = []
    for entry in _mapping_list(value):
        category = _bounded_ref(entry.get("category"))
        reason = _bounded_ref(entry.get("reason_code"))
        if category or reason:
            refs.append(":".join(part for part in (category, reason) if part))
    return refs


def _output_evidence_refs(blocks: Sequence[Mapping[str, object]]) -> list[str]:
    refs: list[str] = []
    for block in blocks[:8]:
        extracted = block.get("extracted_data")
        if extracted is None:
            continue
        refs.append("output:" + _stable_hash(_value_shape(extracted)))
    return refs


def _value_shape(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return "max_depth"
    if isinstance(value, Mapping):
        return {
            str(key): _value_shape(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [_value_shape(item, depth=depth + 1) for item in value[:3]],
        }
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": type(value).__name__, "zero": value == 0}
    if isinstance(value, str):
        return {"type": "str", "empty": value == ""}
    if value is None:
        return "none"
    return type(value).__name__
