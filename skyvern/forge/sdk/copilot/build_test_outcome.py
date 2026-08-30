from __future__ import annotations

import ast
import hashlib
import json
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, Protocol
from urllib.parse import urlsplit

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from skyvern.forge.sdk.copilot.challenge_evidence import (
    carrier_backed_anti_bot_categories,
    interactive_challenge_controls,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    only_degraded_blocking,
)
from skyvern.forge.sdk.copilot.composition_evidence import page_evidence_source_matches_run, workflow_target_url
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, PageObstruction
from skyvern.forge.sdk.copilot.failure_tracking import selector_identities_in_text, selector_identity_from_failure
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.schemas.copilot_turn_outcome import UnresolvedRuntimeFailure

LOG = structlog.get_logger()

BuildTestOutcomePhase = Literal["scout_evaluate", "persisted_block_run", "author_time_reject"]
BuildTestOutcomeVerdict = Literal[
    "progress_observed",
    "repairable_failure",
    "authoring_rejected",
    "not_authoritative",
]
BuildTestOutcomeReasonCode = Literal[
    "runtime_block_failure",
    "runtime_missing_output_dependency",
    "synthesized_parameter_binding_ambiguous",
    "code_safety_reject",
    "code_block_unrenderable",
    "credential_scout_reject",
    "schema_incompatibility",
    "verified_success",
    "outcome_not_demonstrated",
    "no_meaningful_output",
    "terminal_challenge_blocker",
    "blocker_reported",
    "failed_run",
    "run_completed_unevaluated",
    "unrecoverable_tool_error",
    "suspicious_success",
    "missing_structural_evidence",
    "unchanged_after_recorded_outcome",
    "metadata_reject",
    "output_policy_reject",
    "scout_act_observe_hollow_after_interaction",
    "required_input_unbound",
    "definition_contract_unsatisfied",
    "fallback_floor_turn_unsatisfiable",
]
_TERMINAL_CHALLENGE_REASON_CODES: frozenset[BuildTestOutcomeReasonCode] = frozenset({"terminal_challenge_blocker"})
PostRunPagePathKind = Literal["login", "challenge", "incomplete_navigation", "non_page_outcome"]
PostRunPagePathTargetKind = Literal["form_submit", "navigation", "clickable", "challenge"]
BuildTestPacketWorkflowSource = Literal["accepted_write_readback", "turn_start_persisted_readback", "unavailable"]
BuildTestPacketUnfinishedKind = Literal["unverified_block", "missing_requested_output"]
BuildTestPacketPageCaptureStatus = Literal["captured", "unavailable"]
BuildTestPacketPageCaptureOmission = Literal["screenshot_capture_failed", "page_capture_unavailable"]
BuildTestPacketLocatorUnobservedReason = Literal[
    "worker_owned_run",
    "run_browser_unavailable",
    "run_page_unavailable",
    "observation_deadline_exceeded",
    "locator_resolution_failed",
    "identity_read_failed",
]

_STRUCTURAL_KEY_VERSION = "recorded_build_test_outcome:v1"
_AUTHORED_STRUCTURE_VERSION = "recorded_build_test_outcome_authored_structure:v1"
_TEXT_MAX = 180
_REF_TEXT_MAX = 96
_VALUE_EXCERPT_MAX = 700
_HISTORY_LIMIT = 8
_INSPECT_PAGE_SOURCE_TOOL = "inspect_page_for_composition"
_UNRECOVERABLE_TOOL_ERROR_CATEGORY = "UNRECOVERABLE_TOOL_ERROR"
_PLAYWRIGHT_LOCATOR_WAIT_RE = re.compile(
    r"waiting for locator\((?P<quote>['\"])(?P<selector>.*?)(?P=quote)\)"
    r"(?P<locator_chain>(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)*)\s+to be (?P<state>[a-z_]+)",
    re.IGNORECASE,
)
_PLAYWRIGHT_HIDDEN_TAG_RE = re.compile(r"locator resolved to hidden <(?P<tag>[a-z0-9:-]+)", re.IGNORECASE)


class PostRunPagePathTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PostRunPagePathTargetKind
    selector: str = Field(min_length=1)


class PostRunPagePathFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PostRunPagePathKind
    workflow_run_id: str = Field(min_length=1)
    current_url: str = Field(min_length=1)
    continuation_targets: tuple[PostRunPagePathTarget, ...]
    enter_allowed: bool = False

    @property
    def is_page_path(self) -> bool:
        return self.kind != "non_page_outcome" and bool(self.continuation_targets)


class BuildTestPacketRunBrowser(BaseModel):
    """Which browser this run executed in, relative to the one the chat's tools drive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ran_outside_this_chats_browser: bool
    note: str


class BuildTestPacketRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: str | None = None
    browser_session_id: str | None = None
    status: str | None = None
    browser: BuildTestPacketRunBrowser | None = None


class BuildTestPacketPageState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_origin: str | None = None
    current_url: str | None = None
    title: str | None = None
    evidence_source: str | None = None
    observed_after_workflow_run: bool = False
    form_summaries: list[str] = Field(default_factory=list)
    result_summaries: list[str] = Field(default_factory=list)
    action_summaries: list[str] = Field(default_factory=list)
    challenge_summaries: list[str] = Field(default_factory=list)
    obstruction_summaries: list[str] = Field(default_factory=list)
    obstructions: list[PageObstruction] = Field(default_factory=list)


class BuildTestPacketPageCapture(BaseModel):
    """The factual result of trying to retain post-run page evidence.

    This is transport metadata only. It does not classify the run or choose a repair action.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BuildTestPacketPageCaptureStatus
    omission: BuildTestPacketPageCaptureOmission | None = None

    @model_validator(mode="after")
    def validate_capture_state(self) -> BuildTestPacketPageCapture:
        if self.status == "unavailable" and self.omission != "page_capture_unavailable":
            raise ValueError("an unavailable page capture must carry page_capture_unavailable")
        if self.status == "captured" and self.omission == "page_capture_unavailable":
            raise ValueError("a captured page cannot carry a page-unavailable omission")
        return self


class BuildTestPacketLocatorObservation(BaseModel):
    """What one authored locator resolved to on the page the failure left behind.

    Capture order, no ranking: the count and the identities are reported, the repair is not chosen.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authored_selector: str = Field(min_length=1)
    match_count: int | None = Field(default=None, ge=0, strict=True)
    match_index: Literal[0] | None = None
    observed_after_run: Literal[True] = True
    observed_candidates: list[str] | None = None
    unobserved_reason: BuildTestPacketLocatorUnobservedReason | None = None

    @model_validator(mode="after")
    def validate_observation_state(self) -> BuildTestPacketLocatorObservation:
        if self.unobserved_reason is not None:
            if self.match_count is not None or self.match_index is not None or self.observed_candidates is not None:
                raise ValueError("an unobserved locator cannot carry observed fields")
            return self
        if self.match_count is None:
            raise ValueError("a locator row must be observed or carry an unobserved reason")
        if self.match_count == 0:
            if self.match_index is not None or self.observed_candidates is not None:
                raise ValueError("a zero-match locator cannot carry an index or identities")
            return self
        if type(self.match_index) is not int or self.match_index != 0 or not self.observed_candidates:
            raise ValueError("a positive locator count requires match index zero and an identity")
        if any(not candidate for candidate in self.observed_candidates):
            raise ValueError("locator identities must be non-empty")
        return self


class BuildTestPacketFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_block_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    block_label: str | None = None
    block_type: str | None = None
    block_status: str | None = None
    reason: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    failing_line: int | None = None
    action_trace: list[str] = Field(default_factory=list)
    page_state: BuildTestPacketPageState | None = None
    locator_observations: list[BuildTestPacketLocatorObservation] = Field(default_factory=list)


class BuildTestPacketRegisteredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: str | None = None
    output_parameter_id: str | None = None
    output_parameter_key: str | None = None
    block_label: str | None = None
    block_type: str | None = None
    value: JsonValue = None
    value_complete: bool = True


class BuildTestPacketRequestedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: str = Field(min_length=1)
    output_parameter_id: str = Field(min_length=1)
    output_parameter_key: str = Field(min_length=1)
    description: str | None = None
    block_label: str | None = None
    block_type: str | None = None


class BuildTestPacketDownload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    file_name: str | None = None


class BuildTestPacketScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    present: bool
    provenance: str | None = None


class BuildTestPacketUnfinishedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BuildTestPacketUnfinishedKind
    label: str | None = None
    output_path: str | None = None
    reason_code: str | None = None


class BuildTestEvidencePacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["build_test_evidence_packet_v1"] = "build_test_evidence_packet_v1"
    workflow_permanent_id: str | None = None
    canonical_workflow_yaml: str | None = None
    canonical_workflow_source: BuildTestPacketWorkflowSource
    canonical_workflow_yaml_complete: bool = True
    attempted_block_labels: list[str] = Field(default_factory=list)
    executed_block_labels: list[str] = Field(default_factory=list)
    run: BuildTestPacketRun
    action_observations: list[str] = Field(default_factory=list)
    failure: BuildTestPacketFailure | None = None
    page_state: BuildTestPacketPageState | None = None
    page_capture: BuildTestPacketPageCapture | None = None
    requested_outputs: list[BuildTestPacketRequestedOutput] = Field(default_factory=list)
    registered_outputs: list[BuildTestPacketRegisteredOutput] = Field(default_factory=list)
    downloads: list[BuildTestPacketDownload] = Field(default_factory=list)
    screenshot: BuildTestPacketScreenshot
    unfinished_items: list[BuildTestPacketUnfinishedItem] = Field(default_factory=list)
    omission_notices: list[str] = Field(default_factory=list)


class RecordedBuildTestOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: BuildTestOutcomePhase
    attempted_tool: str = ""
    attempted_target: str = ""
    attempted_block_label: str = ""
    attempted_call_ref: str = ""
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
    page_capture: BuildTestPacketPageCapture | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    missing_requested_output_facts: list[dict[str, object]] = Field(default_factory=list)
    runtime_output_repair_facts: list[dict[str, object]] = Field(default_factory=list)
    page_path_failure: PostRunPagePathFailure | None = None
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


_AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES: frozenset[BuildTestOutcomeReasonCode] = frozenset(
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


class _RecordedBuildTestOutcomeContext(Protocol):
    workflow_yaml: str
    persisted_workflow_yaml: str | None
    staged_workflow_yaml: str | None
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None
    recorded_build_test_outcome_history: list[dict[str, object]]
    recorded_persisted_block_run_workflow_run_id: str | None


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
            "block_labels": list(outcome.block_labels),
            "attempted_block_label": outcome.attempted_block_label,
            "attempted_block_signature": _attempted_block_signature(ctx, outcome),
            "attempted_call_ref": outcome.attempted_call_ref,
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
        # The three fields `unresolved_runtime_block_failure` branches on. Without them a suppressed
        # honesty note cannot be attributed to a branch from the logs alone.
        attempted_block_label=outcome.attempted_block_label,
        attempted_call_ref=outcome.attempted_call_ref,
        attempted_block_signature=_attempted_block_signature(ctx, outcome),
    )


def _attempted_block_signature(ctx: _RecordedBuildTestOutcomeContext, outcome: RecordedBuildTestOutcome) -> str:
    """YAML-derived so it stays comparable later; ``block_shape_hashes`` is built from block models
    and never hashes equal to a YAML-derived signature."""
    if outcome.reason_code != "runtime_block_failure" or not outcome.attempted_block_label:
        return ""
    return authored_block_signatures_from_workflow(ctx.workflow_yaml).get(outcome.attempted_block_label, "")


def _code_blocks_by_label(workflow_yaml: str | None) -> dict[str, str]:
    """Code text for every code block in the draft, including blocks nested in containers or loops."""
    code_by_label: dict[str, str] = {}
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return code_by_label
    parsed = _parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, Mapping):
        return code_by_label

    def walk(value: object) -> None:
        for block in _mapping_list(value):
            label = _safe_str(block.get("label"))
            code = block.get("code")
            if _safe_str(block.get("block_type")).lower() == "code" and label and isinstance(code, str):
                code_by_label[label] = code
            walk(block.get("blocks"))
            walk(block.get("loop_blocks"))

    walk(_dict(parsed.get("workflow_definition")).get("blocks"))
    return code_by_label


# Every construct that can reach the end of a block without running something inside it, including
# expression-level ones: a zero-length comprehension skips its body exactly as an unentered loop does.
# Over-detection only costs a redundant note; a miss silently drops a real failure.
_SELECTOR_CALL_ATTRS = frozenset({"locator", "get_by_role", "get_by_text", "get_by_label", "get_by_placeholder"})


def _parse_block_code(code: str) -> ast.AsyncFunctionDef | None:
    """Block code is an async body, so it only parses inside a wrapper; None means unparseable."""
    try:
        wrapper = ast.parse("async def _block():\n" + textwrap.indent(code, "    ")).body[0]
    except Exception:
        # Deeply nested generated code raises MemoryError from the parser rather than SyntaxError,
        # and no caller may propagate a parse failure out of turn assembly.
        return None
    return wrapper if isinstance(wrapper, ast.AsyncFunctionDef) else None


def _selector_removal_is_provable(code: str) -> bool:
    """Whether every selector in the block is a literal, so a scan that misses one proves removal.

    A selector built at runtime (an f-string, a variable, a concatenation) is invisible to the
    literal scan, so its absence would otherwise read as the failing call having been edited away.
    """
    wrapper = _parse_block_code(code)
    if wrapper is None:
        return False
    for node in ast.walk(wrapper):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _SELECTOR_CALL_ATTRS:
            continue
        selector_args = [*node.args, *(kw.value for kw in node.keywords if kw.arg == "name")]
        if any(not isinstance(arg, ast.Constant) or not isinstance(arg.value, str) for arg in selector_args):
            return False
    return True


def _yaml_digest(text: str | None) -> str:
    """Identity of a workflow's bytes, so sources can be compared without logging their content."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16] if text else "absent"


def unresolved_runtime_block_failure(
    ctx: _RecordedBuildTestOutcomeContext,
    *,
    reported_workflow_yaml: str | None = None,
    pending_later_run_id: str | None = None,
) -> UnresolvedRuntimeFailure | None:
    return unresolved_runtime_block_failure_with_disposition(
        ctx, reported_workflow_yaml=reported_workflow_yaml, pending_later_run_id=pending_later_run_id
    )[0]


def unresolved_runtime_block_failure_with_disposition(
    ctx: _RecordedBuildTestOutcomeContext,
    *,
    reported_workflow_yaml: str | None = None,
    pending_later_run_id: str | None = None,
) -> tuple[UnresolvedRuntimeFailure | None, str]:
    """The newest runtime block failure the retained evidence does not show was resolved.

    A later run of the same block is not clearance. It proves the code executed again, not that it met
    the condition that failed: a login step can fail against an already-authenticated page and pass
    against a signed-out one, same lines, opposite precondition. Only evidence that the code itself
    changed -- the failing call removed, or the block's signature changed -- clears the failure.
    """
    history = ctx.recorded_build_test_outcome_history
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if entry.get("reason_code") != "runtime_block_failure":
            continue
        label = _safe_str(entry.get("attempted_block_label"))
        signature = _safe_str(entry.get("attempted_block_signature"))
        run_id = _safe_str(entry.get("workflow_run_id"))
        call_ref = _safe_str(entry.get("attempted_call_ref"))
        # Scout evaluations and author-time rejects share this history, so a later *run* has to be
        # identified by phase and a different run id -- otherwise author-time work after the failure
        # would read as a later run that skipped the block.
        later_runs: list[object] = [
            entry_after
            for entry_after in history[index + 1 :]
            if entry_after.get("phase") == "persisted_block_run"
            and _safe_str(entry_after.get("workflow_run_id")) not in ("", run_id)
        ]
        # A run asking mid-flight is its own later run: its outcome reaches the history only after the
        # result it is about has been handed back, so without this it would see nothing after the
        # failure and decline.
        if pending_later_run_id and pending_later_run_id != run_id:
            later_runs.append({"workflow_run_id": pending_later_run_id})
        # Clearance reads only the workflow the user can actually run. A draft that drops the failing
        # call would clear a failure the delivered workflow still carries.
        delivered_yaml = reported_workflow_yaml
        code = _code_blocks_by_label(delivered_yaml).get(label) if delivered_yaml else None
        if not (label and run_id and later_runs and code):
            if not (label and run_id):
                return None, "incomplete_failure_record"
            # `no_later_run` is not a suppression: with nothing after it, the failure is the turn's
            # own headline and needs no separate qualification.
            if not later_runs:
                return None, "no_later_run"
            # Nothing to read the failing block from: either no delivered candidate at all, or the
            # block is absent from the one we have because it was authored after the snapshot was
            # taken. Absence is not proof of repair, so the failure stands.
            return (
                UnresolvedRuntimeFailure(workflow_run_id=run_id, block_label=label),
                "no_reported_candidate" if not delivered_yaml else "block_absent_from_delivered",
            )
        if call_ref:
            if call_ref not in selector_identities_in_text(code) and _selector_removal_is_provable(code):
                LOG.info(
                    "copilot unresolved runtime failure cleared by call removal",
                    delivered_digest=_yaml_digest(delivered_yaml),
                    draft_digest=_yaml_digest(ctx.workflow_yaml),
                    delivered_is_the_draft=delivered_yaml == ctx.workflow_yaml,
                )
                return None, f"failing_call_removed:{run_id}:{label}"
        elif signature and authored_block_signatures_from_workflow(delivered_yaml).get(label) != signature:
            return None, f"block_signature_changed:{run_id}:{label}"
        return UnresolvedRuntimeFailure(workflow_run_id=run_id, block_label=label), "unresolved"
    return None, "no_runtime_failure"


def bind_post_run_page_path_failure(
    ctx: _RecordedBuildTestOutcomeContext,
    page_evidence: Mapping[str, object],
) -> bool:
    """Bind a fresh same-run page-path condition without changing the outcome's structural identity."""
    latest = ctx.latest_recorded_build_test_outcome
    if (
        not isinstance(latest, RecordedBuildTestOutcome)
        or not latest.is_authoritative
        or latest.phase != "persisted_block_run"
        or latest.reason_code not in _AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES
        or latest.verdict != "repairable_failure"
        or not latest.workflow_run_id
    ):
        return False
    condition = _post_run_page_path_failure(
        page_evidence,
        latest.workflow_run_id,
        required_target_url=workflow_target_url(ctx.workflow_yaml),
    )
    if condition is None:
        return False
    ctx.latest_recorded_build_test_outcome = latest.model_copy(update={"page_path_failure": condition})
    return True


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
    page_capture: BuildTestPacketPageCapture | None,
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
        page_capture=page_capture,
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
    requested_output_parameter_payloads: Sequence[BuildTestPacketRequestedOutput] | None = None,
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
    graded_page_evidence = page_evidence if _post_run_page_evidence_matches_result(data, page_evidence) else None
    page_refs = _page_evidence_refs(graded_page_evidence)
    page_capture = post_run_page_capture_from_result(data, graded_page_evidence)
    output_refs = _output_evidence_refs(blocks)
    verification_identity = _completion_verification_identity(completion_verification)
    authoritative_workflow_run_id = (
        recorded_run_outcome.workflow_run_id if recorded_run_outcome is not None else None
    ) or workflow_run_id
    requested_output_payloads = list(requested_output_parameter_payloads or [])
    if requested_output_parameter_payloads is None:
        for payload in _mapping_list(data.get("requested_output_parameter_definitions")):
            try:
                requested_output_payloads.append(BuildTestPacketRequestedOutput.model_validate(payload))
            except ValueError:
                continue
    raw_registered_output_payloads = data.get("registered_output_parameter_values")
    omission_registered_output_payloads = (
        registered_output_parameter_payloads
        if registered_output_parameter_payloads is not None
        else (
            _mapping_list(raw_registered_output_payloads) if isinstance(raw_registered_output_payloads, list) else None
        )
    )
    registered_output_models: list[BuildTestPacketRegisteredOutput] = []
    for payload in omission_registered_output_payloads or []:
        try:
            registered_output_models.append(BuildTestPacketRegisteredOutput.model_validate(payload))
        except ValueError:
            continue
    typed_output_omission_facts = (
        _typed_requested_output_omission_facts(
            requested_output_payloads,
            registered_output_models,
            authoritative_workflow_run_id,
        )
        if omission_registered_output_payloads is not None
        else []
    )
    missing_output_facts = _merge_missing_requested_output_facts(
        _missing_requested_output_facts(completion_verification, blocks),
        typed_output_omission_facts,
    )
    page_path_failure = _post_run_page_path_failure(graded_page_evidence, authoritative_workflow_run_id or None)
    runtime_output_facts = _runtime_output_repair_facts(
        completion_verification,
        blocks,
        registered_output_parameter_payloads or _mapping_list(data.get("registered_output_parameter_values")),
        authoritative_workflow_run_id,
    )
    if recorded_run_outcome is not None and (
        failed_block is None
        or _run_outcome_reason_code(recorded_run_outcome) in _TERMINAL_CHALLENGE_REASON_CODES
        or recorded_run_outcome.verdict == "not_evaluated"
    ):
        reason_code = _run_outcome_reason_code(recorded_run_outcome)
        if reason_code in _TERMINAL_CHALLENGE_REASON_CODES:
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code=reason_code,
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                page_capture=page_capture,
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
                page_capture=page_capture,
                evidence_refs=output_refs,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "Completion verification passed.",
                key_provenance={
                    "verified_progress_marker": "CompletionVerificationResult satisfied criteria",
                    "evidence_refs": "run output structure",
                },
            )
        if recorded_run_outcome.verdict == "not_evaluated" and not typed_output_omission_facts:
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="not_authoritative",
                reason_code=reason_code,
                workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
                block_labels=block_labels,
                requested_block_labels=requested_block_labels,
                block_shape_hashes=block_shape_hashes,
                page_capture=page_capture,
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
                page_capture,
                authored_structure_signature,
                referenced_unbound_keys,
            )
        structural_identity = verification_identity or _typed_omission_identity(missing_output_facts)
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
                page_capture=page_capture,
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
                page_capture=page_capture,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "turn-unsatisfiable fallback floor, no reachable route"},
            )
        return RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="no_meaningful_output" if missing_output_facts else reason_code,
            workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
            block_labels=block_labels,
            requested_block_labels=requested_block_labels,
            block_shape_hashes=block_shape_hashes,
            structural_failure_identity=structural_identity,
            page_evidence_refs=page_refs,
            page_capture=page_capture,
            evidence_refs=evidence_refs,
            missing_requested_output_facts=missing_output_facts,
            runtime_output_repair_facts=runtime_output_facts,
            page_path_failure=page_path_failure,
            authored_structure_signature=authored_structure_signature,
            observed_evidence_summary=recorded_run_outcome.display_reason or "",
            key_provenance={
                "structural_failure_identity": (
                    "same-run requested-output omission facts"
                    if typed_output_omission_facts
                    else "CompletionVerificationResult verdict structure"
                ),
                "page_evidence_refs": "bounded post-run page evidence",
                "evidence_refs": "run output structure",
                "missing_requested_output_facts": (
                    "same-run requested output definitions and registered values"
                    if typed_output_omission_facts
                    else "CompletionVerificationResult unsatisfied output paths and run output shape"
                ),
                "runtime_output_repair_facts": "same-run registered output parameters and completion verdicts",
                "page_path_failure": "same-run bounded post-run page structure and executable continuations",
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
            page_capture,
            authored_structure_signature,
            referenced_unbound_keys,
        )
    if not (
        failure_categories
        or failure_type
        or runtime_failure_identity
        or page_refs
        or output_refs
        or missing_output_facts
    ):
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
    if not structural_identity:
        structural_identity = _typed_omission_identity(missing_output_facts)
    verdict: BuildTestOutcomeVerdict = (
        "repairable_failure"
        if bool(result.get("ok")) is False or failed_block is not None or missing_output_facts
        else "progress_observed"
    )
    if not structural_identity and not page_refs and not output_refs:
        verdict = "not_authoritative"
    reason_code = (
        "runtime_block_failure"
        if failed_block is not None or not bool(result.get("ok"))
        else ("no_meaningful_output" if missing_output_facts else "run_completed_unevaluated")
    )
    if any(ref.split(":", 1)[0] == _UNRECOVERABLE_TOOL_ERROR_CATEGORY for ref in failure_categories):
        # The run failed on the tool plane, not on what was authored, so it is not test
        # signal: dropping the identity keys it out of outcome dedup and grounding too.
        verdict = "not_authoritative"
        reason_code = "unrecoverable_tool_error"
        structural_identity = ""
        page_refs = []
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
        attempted_call_ref=selector_identity_from_failure(
            _safe_str(failed_block.get("failure_reason")) if failed_block is not None else ""
        ),
        verdict=verdict,
        reason_code=reason_code,
        workflow_run_id=workflow_run_id or None,
        block_labels=block_labels,
        requested_block_labels=requested_block_labels,
        block_shape_hashes=block_shape_hashes,
        structural_failure_identity=structural_identity,
        page_evidence_refs=page_refs,
        page_capture=page_capture,
        evidence_refs=output_refs,
        missing_requested_output_facts=missing_output_facts,
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
            "missing_requested_output_facts": "same-run requested output definitions and registered values",
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


def post_run_page_capture_from_result(
    data: Mapping[str, object],
    page_evidence: Mapping[str, object] | None,
) -> BuildTestPacketPageCapture | None:
    """Read only the typed post-run capture fact carried by the run result.

    A usable page packet establishes that capture reached the page. Screenshot availability is a
    separate fact, so its omission must not erase the page or action facts already retained.
    """
    raw_capture = data.get("post_run_page_capture")
    if isinstance(raw_capture, Mapping):
        try:
            return BuildTestPacketPageCapture.model_validate(raw_capture)
        except ValueError:
            return None
    if not _post_run_page_evidence_matches_result(data, page_evidence):
        return None
    assert page_evidence is not None
    omissions = page_evidence.get("visual_capture_omissions")
    if isinstance(omissions, list) and "screenshot_capture_failed" in omissions:
        return BuildTestPacketPageCapture(status="captured", omission="screenshot_capture_failed")
    return BuildTestPacketPageCapture(status="captured")


def _post_run_page_evidence_matches_result(
    data: Mapping[str, object],
    page_evidence: Mapping[str, object] | None,
) -> bool:
    """Only evidence stamped after this result's run can establish that capture succeeded."""
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    return bool(
        page_evidence is not None
        and workflow_run_id
        and page_evidence.get("observed_after_workflow_run") is True
        and page_evidence.get("workflow_run_id") == workflow_run_id
        and page_evidence_source_matches_run(
            _safe_str(page_evidence.get("source_browser_session_id")),
            _safe_str(data.get("browser_session_id")),
        )
    )


def _post_run_page_path_failure(
    page_evidence: Mapping[str, object] | None,
    workflow_run_id: str | None,
    *,
    required_target_url: str | None = None,
) -> PostRunPagePathFailure | None:
    if (
        page_evidence is None
        or not workflow_run_id
        or page_evidence.get("observed_after_workflow_run") is not True
        or page_evidence.get("workflow_run_id") != workflow_run_id
    ):
        return None
    current_url = _safe_str(page_evidence.get("current_url")) or _safe_str(page_evidence.get("inspected_url"))
    if not current_url:
        return None

    def collect_targets(kind: PostRunPagePathTargetKind, controls: object) -> list[PostRunPagePathTarget]:
        return [
            PostRunPagePathTarget(kind=kind, selector=selector)
            for control in _mapping_list(controls)
            if control.get("disabled") is not True and (selector := _safe_str(control.get("selector")).strip())
        ]

    def structural_submit_controls(
        controls: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        return [
            control
            for control in controls
            if control.get("disabled") is not True and _safe_str(control.get("type")).strip().casefold() == "submit"
        ]

    def structural_challenge_controls(
        controls: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        # Admission must never key on page text: labels are untrusted, page-controlled
        # input. Only carrier membership plus typed control structure may mint a target.
        enabled = [control for control in controls if control.get("disabled") is not True]
        toggles = [
            control
            for control in enabled
            if _safe_str(control.get("tag")).strip().casefold() == "input"
            and _safe_str(control.get("type")).strip().casefold() in {"checkbox", "radio"}
            and control.get("checked") is not True
        ]
        non_toggle_controls = [
            control
            for control in enabled
            if (
                _safe_str(control.get("tag")).strip().casefold() == "button"
                or (
                    _safe_str(control.get("tag")).strip().casefold() == "input"
                    and _safe_str(control.get("type")).strip().casefold() in {"button", "submit"}
                )
            )
        ]
        submit_controls = structural_submit_controls(non_toggle_controls)
        return [*toggles, *submit_controls]

    forms = _mapping_list(page_evidence.get("forms"))
    login_targets: list[PostRunPagePathTarget] = []
    for form in forms:
        fields = _mapping_list(form.get("fields"))
        has_password_field = any(_safe_str(field.get("type")).strip().casefold() == "password" for field in fields)
        if not has_password_field:
            continue
        login_targets.extend(
            collect_targets(
                "form_submit",
                structural_submit_controls(_mapping_list(form.get("submit_controls"))),
            )
        )

    challenge_state = page_evidence.get("challenge_state")
    challenge_targets: list[PostRunPagePathTarget] = []
    if isinstance(challenge_state, Mapping):
        gated_submit_controls = _mapping_list(challenge_state.get("gated_submit_controls"))
        challenge_targets.extend(collect_targets("challenge", gated_submit_controls))
    challenge_targets.extend(
        collect_targets(
            "challenge",
            structural_challenge_controls(
                interactive_challenge_controls(_mapping_list(page_evidence.get("challenge_controls")))
            ),
        )
    )

    navigation_targets: list[PostRunPagePathTarget] = []
    if required_target_url:
        for control in _mapping_list(page_evidence.get("navigation_targets")):
            if not _same_navigation_target_url(_safe_str(control.get("href")), required_target_url):
                continue
            navigation_targets.extend(collect_targets("navigation", [control]))

    challenge_associated = isinstance(challenge_state, Mapping) and (
        challenge_state.get("gates_submit_controls") is True or bool(challenge_targets)
    )
    if challenge_associated and challenge_targets:
        kind: PostRunPagePathKind = "challenge"
        targets = _dedupe_page_path_targets(challenge_targets)
    elif login_targets:
        kind = "login"
        targets = _dedupe_page_path_targets(login_targets)
    elif navigation_targets:
        kind = "incomplete_navigation"
        targets = _dedupe_page_path_targets(navigation_targets)
    else:
        kind = "non_page_outcome"
        targets = []
    return PostRunPagePathFailure(
        kind=kind,
        workflow_run_id=workflow_run_id,
        current_url=current_url,
        continuation_targets=targets,
        enter_allowed=kind in {"login", "challenge"}
        and any(target.kind in {"form_submit", "challenge"} for target in targets),
    )


def _dedupe_page_path_targets(
    targets: Sequence[PostRunPagePathTarget],
) -> list[PostRunPagePathTarget]:
    deduped: list[PostRunPagePathTarget] = []
    seen: set[str] = set()
    for target in targets:
        if target.selector in seen:
            continue
        seen.add(target.selector)
        deduped.append(target)
    return deduped


def _same_navigation_target_url(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme.casefold(),
        left_parts.netloc.casefold(),
        left_parts.path.rstrip("/") or "/",
        left_parts.query,
        left_parts.fragment,
    ) == (
        right_parts.scheme.casefold(),
        right_parts.netloc.casefold(),
        right_parts.path.rstrip("/") or "/",
        right_parts.query,
        right_parts.fragment,
    )


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
        "blocker_reported",
        *_TERMINAL_CHALLENGE_REASON_CODES,
    }:
        return reason_code
    if recorded_run_outcome.verdict == "demonstrated":
        return "verified_success"
    if recorded_run_outcome.verdict == "not_evaluated":
        return "run_completed_unevaluated"
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


def _typed_requested_output_omission_facts(
    requested_outputs: Sequence[BuildTestPacketRequestedOutput],
    registered_outputs: Sequence[BuildTestPacketRegisteredOutput],
    workflow_run_id: str,
) -> list[dict[str, object]]:
    if not workflow_run_id:
        return []
    registered_ids = {
        output.output_parameter_id
        for output in registered_outputs
        if output.workflow_run_id == workflow_run_id and output.output_parameter_id
    }
    facts: list[dict[str, object]] = []
    for requested in requested_outputs:
        if requested.workflow_run_id != workflow_run_id:
            continue
        output_parameter_id = requested.output_parameter_id
        output_parameter_key = requested.output_parameter_key
        if output_parameter_id in registered_ids:
            continue
        fact: dict[str, object] = {
            "output_path": f"output.{_bounded_ref(output_parameter_key)}",
            "output_root": _bounded_ref(output_parameter_key),
            "output_parameter_id": _bounded_ref(output_parameter_id),
            "reason_code": "registered_output_missing",
            "value_status": "not_registered",
        }
        block_label = requested.block_label
        if block_label:
            fact["block_label"] = _bounded_ref(block_label)
        facts.append(fact)
    return sorted(facts, key=lambda item: str(item["output_path"]))


def _merge_missing_requested_output_facts(
    first: Sequence[Mapping[str, object]], second: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for fact in (*first, *second):
        output_path = fact.get("output_path")
        if isinstance(output_path, str) and output_path:
            merged.setdefault(output_path, dict(fact))
    return [merged[key] for key in sorted(merged)]


def _typed_omission_identity(facts: Sequence[Mapping[str, object]]) -> str:
    return "typed_output_omission:" + _stable_hash(list(facts)) if facts else ""


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


def registered_output_payload_binds_output_path(
    payloads: Sequence[Mapping[str, object]],
    output_path: str,
) -> bool:
    for item in payloads:
        value, present = _registered_output_value_for_path(item, output_path)
        if present and not _is_empty_output_value(value):
            return True
    return False


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


def history_has_runtime_block_failure(ctx: _RecordedBuildTestOutcomeContext) -> bool:
    """Whether this turn recorded any runtime block failure, resolved or not."""
    return any(entry.get("reason_code") == "runtime_block_failure" for entry in ctx.recorded_build_test_outcome_history)
