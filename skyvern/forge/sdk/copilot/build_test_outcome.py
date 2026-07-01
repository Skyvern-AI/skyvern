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

from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

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
]

_STRUCTURAL_KEY_VERSION = "recorded_build_test_outcome:v1"
_AUTHORED_STRUCTURE_VERSION = "recorded_build_test_outcome_authored_structure:v1"
_TEXT_MAX = 180
_REF_TEXT_MAX = 96
_HISTORY_LIMIT = 8
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
    structural_failure_identity: str = ""
    verified_progress_marker: str = ""
    page_evidence_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_requested_output_facts: list[dict[str, object]] = Field(default_factory=list)
    authored_structure_signature: str | None = None
    display_text: str = ""
    key_provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def structural_key_payload(self) -> dict[str, object] | None:
        if not (self.structural_failure_identity or self.verified_progress_marker or self.page_evidence_refs):
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


class _RecordedBuildTestOutcomeContext(Protocol):
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None
    recorded_build_test_outcome_history: list[dict[str, object]]


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
        previous_key = previous.get("structural_key")
        if isinstance(previous_key, str):
            return previous_key == latest["structural_key"]
    return None


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
        page_evidence_refs=_clean_list(page_evidence_refs),
        key_provenance={
            "structural_failure_identity": "author-time validator structural reason",
            "page_evidence_refs": "author-time validator structural refs",
        },
    )


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


def recorded_outcome_from_run_blocks_result(
    result: Mapping[str, object],
    *,
    page_evidence: Mapping[str, object] | None = None,
    recorded_run_outcome: RecordedRunOutcome | None = None,
    completion_verification: CompletionVerificationResult | None = None,
    authored_structure_signature: str | None = None,
) -> RecordedBuildTestOutcome | None:
    data = _dict(result.get("data"))
    workflow_run_id = _safe_str(data.get("workflow_run_id"))
    blocks = _block_dicts(data.get("blocks"))
    failed_block = _first_failed_block(blocks)
    block_labels = [_safe_str(block.get("label")) for block in blocks if _safe_str(block.get("label"))]
    page_refs = _page_evidence_refs(page_evidence)
    output_refs = _output_evidence_refs(blocks)
    verification_identity = _completion_verification_identity(completion_verification)
    missing_output_facts = _missing_requested_output_facts(completion_verification, blocks)
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
                verified_progress_marker=verification_identity or "run_completed_verified",
                evidence_refs=output_refs,
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "Completion verification passed.",
                key_provenance={
                    "verified_progress_marker": "CompletionVerificationResult satisfied criteria",
                    "evidence_refs": "run output structure",
                },
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
                authored_structure_signature=authored_structure_signature,
                observed_evidence_summary=recorded_run_outcome.display_reason or "",
                key_provenance={"structural_failure_identity": "no typed verification/page/output identity available"},
            )
        return RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code=reason_code,
            workflow_run_id=recorded_run_outcome.workflow_run_id or workflow_run_id or None,
            block_labels=block_labels,
            structural_failure_identity=structural_identity,
            page_evidence_refs=page_refs,
            evidence_refs=evidence_refs,
            missing_requested_output_facts=missing_output_facts,
            authored_structure_signature=authored_structure_signature,
            observed_evidence_summary=recorded_run_outcome.display_reason or "",
            key_provenance={
                "structural_failure_identity": "CompletionVerificationResult verdict structure",
                "page_evidence_refs": "bounded post-run page evidence",
                "evidence_refs": "run output structure",
                "missing_requested_output_facts": "CompletionVerificationResult unsatisfied output paths and run output shape",
            },
        )
    run_status = _safe_str(data.get("overall_status"))
    failure_type = _safe_str(data.get("failure_type"))
    failure_categories = _failure_category_refs(data.get("failure_categories"))
    status = _safe_str(failed_block.get("status")) if failed_block is not None else run_status
    runtime_failure_identity = _runtime_failure_identity(failed_block)
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
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label=_safe_str(failed_block.get("label")) if failed_block is not None else "",
        verdict=verdict,
        reason_code="runtime_block_failure" if failed_block is not None or not bool(result.get("ok")) else "failed_run",
        workflow_run_id=workflow_run_id or None,
        block_labels=block_labels,
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


def _output_path_root(output_path: str) -> str:
    return _bounded_ref(output_path.split(".", 1)[0].split("[", 1)[0])


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
    if grounding_mode == "shape" and not has_exact_value:
        return "presence_only_evidence"
    return "typed_value_unverified"


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
