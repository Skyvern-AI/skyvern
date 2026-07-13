"""Outcome-criteria verification judge for the workflow copilot.

For a workflow test run — whether it completed cleanly or was canceled/partial —
this focused LLM call checks each completion criterion (an end-state outcome the
user asked for) against the evidence the run actually produced — extraction/
validation block outputs and the observed end-state URL/title — and returns a typed
per-criterion verdict. The deterministic gate consumes the typed result; this
module never decides the gate.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import structlog
import yaml

from skyvern.config import settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    TerminalActionFamily,
    is_contingent_missing_antecedent_degraded,
    is_fallback_floor_base_criterion,
    is_turn_unsatisfiable_fallback_degraded,
    redact_raw_secrets_for_prompt,
)
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()

PROMPT_TEMPLATE_NAME = "workflow-copilot-completion-verification"
_EVIDENCE_VALUE_MAX_CHARS = 2000
_EVIDENCE_REF_MAX_CHARS = 240
_MISSING_EVIDENCE_MAX_CHARS = 500
_MAX_BLOCK_OUTPUTS = 20
_MAX_TRACE_VERDICTS = 8
_REASON_CODES = frozenset({"evidence_confirms", "no_evidence", "evidence_contradicts", "unknown"})
_STRUCTURAL_ABSTENTION_REASON_CODE = "structurally_abstained"
_CONTINGENT_ABSTENTION_REASON_CODES = frozenset(
    {"unknown", "no_evidence", "evidence_contradicts", "missing_exact_field", "unproducible"}
)
REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID = "__copilot_registered_download__downloaded_files_non_empty"
TERMINAL_RECORD_CORROBORATION_CRITERION_ID = "__copilot_terminal_record_corroboration__"
_REGISTERED_DOWNLOAD_COUNT_KEYS = (
    "downloaded_file_count",
    "downloaded_file_url_count",
    "downloaded_file_artifact_count",
)
_PAGE_EVIDENCE_KEYS = (
    "current_url",
    "page_title",
    "visual_evidence_summary",
    "screenshot_used",
    "evidence_sources",
    "visible_text_excerpt",
    "forms",
    "navigation_targets",
    "result_containers",
    "anti_bot_indicators",
    "challenge_state",
    "visual_evidence_omissions",
    "inspection_warnings",
    "observed_empty_page",
    "evidence_confidence",
)

VerificationStatus = Literal["evaluated", "unavailable"]
CriterionState = Literal["satisfied", "unsatisfied", "unknown"]
EvidenceSourceKind = Literal[
    "runtime_output",
    "same_record_context",
    "independent_page_evidence",
    "terminal_record",
    "registered_output_parameter",
    "registered_artifact_content",
]


@dataclass(frozen=True)
class CriterionVerdict:
    criterion_id: str
    state: CriterionState
    reason_code: str
    evidence_ref: str | None = None
    missing_evidence: str | None = None
    output_path: str | None = None
    grounding_mode: Literal["exact_value", "shape", "missing", "terminal_record", "judgment_boolean"] | None = None
    expected_output_shape: str | None = None
    has_exact_value: bool = False
    requested_output_evidence_source: str | None = None
    evidence_source: EvidenceSourceKind | None = None
    self_emitted_judgment_not_independent: bool = False

    @property
    def satisfied(self) -> bool:
        return self.state == "satisfied"


@dataclass(frozen=True)
class CompletionVerificationResult:
    status: VerificationStatus
    criterion_ids: list[str] = field(default_factory=list)
    verdicts: list[CriterionVerdict] = field(default_factory=list)
    no_gradeable_run_plane: bool = False
    contingent_criterion_ids: list[str] = field(default_factory=list)
    contingent_on_by_criterion_id: dict[str, str] = field(default_factory=dict)
    contingent_antecedent_output_path_by_criterion_id: dict[str, str] = field(default_factory=dict)
    structural_unfired_criterion_ids: list[str] = field(default_factory=list)
    degraded_criterion_ids: list[str] = field(default_factory=list)
    floor_rekeyed_criterion_ids: list[str] = field(default_factory=list)
    floor_rekeyed_output_path_by_criterion_id: dict[str, str] = field(default_factory=dict)
    floor_rekeyed_backed_by_criterion_id: dict[str, bool] = field(default_factory=dict)
    contingent_degraded_criterion_ids: list[str] = field(default_factory=list)
    requested_output_criteria_count: int = 0

    def is_fully_satisfied(self) -> bool:
        if self.status != "evaluated" or not self.criterion_ids:
            return False
        verdict_by_id = {verdict.criterion_id: verdict for verdict in self.verdicts}
        has_observed_reach_state = any(
            _is_satisfied_observed_end_state_verdict(verdict) for verdict in verdict_by_id.values()
        )
        has_requested_output_abstention_corroboration = any(
            _is_satisfied_observed_end_state_verdict(verdict) or _is_satisfied_terminal_record_verdict(verdict)
            for verdict in verdict_by_id.values()
        )
        abstained_id_set = self.abstained_criterion_ids()
        degraded_id_set = degraded_lane_criterion_ids(self) - (
            set(self.contingent_degraded_criterion_ids) & abstained_id_set
        )
        satisfied_run_plane_count = 0
        for criterion_id in self.criterion_ids:
            verdict = verdict_by_id.get(criterion_id)
            if verdict is not None and verdict.satisfied:
                # A definition-plane satisfied verdict proves the workflow is configurable,
                # never that a run reached the outcome, so it cannot authorize verified success
                # on its own — only a satisfied run-plane verdict can.
                if criterion_id not in abstained_id_set and not verdict.reason_code.startswith(
                    _DEFINITION_REASON_PREFIX
                ):
                    satisfied_run_plane_count += 1
                continue
            # A degraded criterion that did not structurally abstain has no reachable satisfaction
            # route, so an unsatisfied one is never creditable via corroboration or value-blind excusal.
            if criterion_id in degraded_id_set:
                return False
            # A structurally-unfired contingent criterion is N/A for this run: it neither vetoes nor
            # earns credit, so success must derive from the remaining genuinely-satisfied criteria.
            if criterion_id in abstained_id_set:
                continue
            # A definition-plane ``unknown`` is a YAML-grader abstention, not a refutation,
            # so it must not veto a run whose observable outcome evidence is fully confirmed.
            if verdict is not None and _is_definition_plane_abstention(verdict):
                continue
            # An incomplete typed classification contract likewise abstains rather than refutes:
            # it must not disown a delivered sibling output whose evidence is confirmed.
            if verdict is not None and _is_incomplete_validation_classification_abstention(verdict):
                continue
            if verdict is not None and verdict.self_emitted_judgment_not_independent:
                if _has_independent_satisfied_requested_output_corroborator(verdict_by_id, criterion_id):
                    satisfied_run_plane_count += 1
                    continue
                return False
            if verdict is not None and _is_corroborated_structural_requested_output_abstention(
                verdict,
                verdict_by_id,
                criterion_id,
                has_requested_output_abstention_corroboration,
            ):
                satisfied_run_plane_count += 1
                continue
            if verdict is not None and _is_structural_requested_output_abstention(verdict):
                continue
            if has_observed_reach_state and verdict is not None and _is_reperception_contradiction(verdict):
                continue
            return False
        return satisfied_run_plane_count > 0

    def abstained_criterion_ids(self) -> set[str]:
        """A criterion abstains when its antecedent is known not to have fired: the
        ``contingent_missing_antecedent`` degrade is that determination, so its lane counts alongside
        the contingent metadata even when the criterion carries no antecedent binding."""
        lane_ids = set(self.contingent_criterion_ids) | set(self.contingent_degraded_criterion_ids)
        return set(self.structural_unfired_criterion_ids) & lane_ids

    def is_structural_contingent_abstention(self, verdict: CriterionVerdict) -> bool:
        return _is_contingent_abstention(verdict, self.abstained_criterion_ids())

    def to_trace_data(self) -> dict[str, Any]:
        unmet = [
            verdict
            for verdict in self.verdicts
            if not verdict.satisfied and not self.is_structural_contingent_abstention(verdict)
        ]
        missing_evidence: list[str] = []
        for verdict in unmet:
            detail = verdict_missing_evidence(verdict)
            if detail:
                missing_evidence.append(f"{verdict.criterion_id}: {detail}")
        data: dict[str, Any] = {
            "status": self.status,
            "criterion_count": len(self.criterion_ids),
            "satisfied_count": sum(1 for verdict in self.verdicts if verdict.state == "satisfied"),
            "unsatisfied_count": sum(1 for verdict in self.verdicts if verdict.state == "unsatisfied"),
            "unknown_count": sum(1 for verdict in self.verdicts if verdict.state == "unknown"),
            "fully_satisfied": self.is_fully_satisfied(),
            "no_gradeable_run_plane": self.no_gradeable_run_plane,
            "requested_output_criteria_count": self.requested_output_criteria_count,
            "reason_codes": [verdict.reason_code for verdict in self.verdicts],
            "unmet_criterion_ids": [verdict.criterion_id for verdict in unmet],
            "missing_evidence": missing_evidence,
        }
        if self.contingent_criterion_ids:
            data["contingent_criterion_ids"] = list(self.contingent_criterion_ids)
        if self.structural_unfired_criterion_ids:
            data["structural_unfired_criterion_ids"] = list(self.structural_unfired_criterion_ids)
        if self.contingent_degraded_criterion_ids:
            data["contingent_degraded_criterion_ids"] = list(self.contingent_degraded_criterion_ids)
        contingent_id_set = set(self.contingent_criterion_ids)
        structural_unfired_id_set = set(self.structural_unfired_criterion_ids)
        for index, verdict in enumerate(self.verdicts[:_MAX_TRACE_VERDICTS]):
            prefix = f"verdict_{index}"
            data[f"{prefix}_criterion_id"] = verdict.criterion_id
            data[f"{prefix}_state"] = verdict.state
            data[f"{prefix}_satisfied"] = verdict.satisfied
            data[f"{prefix}_reason_code"] = verdict.reason_code
            if verdict.output_path:
                data[f"{prefix}_output_path"] = verdict.output_path
            if verdict.grounding_mode:
                data[f"{prefix}_grounding_mode"] = verdict.grounding_mode
            if verdict.expected_output_shape:
                data[f"{prefix}_expected_output_shape"] = verdict.expected_output_shape
            if (
                verdict.output_path
                or verdict.grounding_mode
                or verdict.expected_output_shape
                or verdict.has_exact_value
                or verdict.requested_output_evidence_source
                or verdict.evidence_source
            ):
                data[f"{prefix}_has_exact_value"] = verdict.has_exact_value
            if verdict.requested_output_evidence_source:
                data[f"{prefix}_requested_output_evidence_source"] = verdict.requested_output_evidence_source
            if verdict.evidence_source:
                data[f"{prefix}_evidence_source"] = verdict.evidence_source
            if verdict.self_emitted_judgment_not_independent:
                data[f"{prefix}_self_emitted_judgment_not_independent"] = True
            if contingent_on := self.contingent_on_by_criterion_id.get(verdict.criterion_id):
                data[f"{prefix}_contingent_on"] = contingent_on
            if contingent_path := self.contingent_antecedent_output_path_by_criterion_id.get(verdict.criterion_id):
                data[f"{prefix}_contingent_antecedent_output_path"] = contingent_path
            if verdict.criterion_id in contingent_id_set or verdict.criterion_id in structural_unfired_id_set:
                data[f"{prefix}_structural_unfired"] = verdict.criterion_id in structural_unfired_id_set
            evidence_ref = _clean_optional_text(verdict.evidence_ref, max_chars=_EVIDENCE_REF_MAX_CHARS)
            if evidence_ref:
                data[f"{prefix}_evidence_ref"] = evidence_ref
            detail = None if self.is_structural_contingent_abstention(verdict) else verdict_missing_evidence(verdict)
            if detail:
                data[f"{prefix}_missing_evidence"] = detail
        return data

    def verdict_state_counts(self) -> dict[str, int]:
        return {
            "satisfied": sum(1 for verdict in self.verdicts if verdict.state == "satisfied"),
            "unsatisfied": sum(1 for verdict in self.verdicts if verdict.state == "unsatisfied"),
            "unknown": sum(1 for verdict in self.verdicts if verdict.state == "unknown"),
        }


@dataclass(frozen=True)
class DeliveredUnverifiedTerminalState:
    observed_verdicts: tuple[CriterionVerdict, ...]


@dataclass(frozen=True)
class RunEvidenceSnapshot:
    workflow_run_id: str | None = None
    block_outputs: dict[str, Any] = field(default_factory=dict)
    block_output_sources: dict[str, EvidenceSourceKind] = field(default_factory=dict)
    current_url: str | None = None
    page_title: str | None = None
    run_terminal_status: str | None = None
    executed_block_labels: list[str] = field(default_factory=list)
    verified_context_block_labels: list[str] = field(default_factory=list)
    failed_block_labels: list[str] = field(default_factory=list)
    failure_classes: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    page_evidence: dict[str, Any] = field(default_factory=dict)
    pre_run_page_reference_text: str | None = None

    def has_evidence(self) -> bool:
        return bool(
            self.block_outputs
            or self.current_url
            or self.page_title
            or self.failed_block_labels
            or self.failure_classes
            or self.failure_reasons
            or self.page_evidence
        )

    def render_prompt_block(self) -> str:
        lines: list[str] = []
        if self.workflow_run_id:
            lines.append(f"workflow_run_id: {self.workflow_run_id}")
        if self.current_url:
            lines.append(f"observed_end_state_url: {self.current_url}")
        if self.page_title:
            lines.append(f"observed_end_state_page_title: {self.page_title}")
        if self.run_terminal_status:
            lines.append(f"run_terminal_status: {self.run_terminal_status}")
        if self.verified_context_block_labels:
            lines.append(
                "verified_context_block_labels: " + ", ".join(self.verified_context_block_labels[:_MAX_BLOCK_OUTPUTS])
            )
        if self.executed_block_labels:
            lines.append("executed_block_labels: " + ", ".join(self.executed_block_labels[:_MAX_BLOCK_OUTPUTS]))
        if self.failed_block_labels:
            lines.append("failed_block_labels: " + ", ".join(self.failed_block_labels[:_MAX_BLOCK_OUTPUTS]))
        if self.failure_classes:
            lines.append("failure_classes: " + ", ".join(self.failure_classes[:_MAX_BLOCK_OUTPUTS]))
        if self.failure_reasons:
            lines.append("failure_reasons:")
            for reason in self.failure_reasons[:_MAX_BLOCK_OUTPUTS]:
                rendered_reason = " ".join(str(reason).split())[:_EVIDENCE_VALUE_MAX_CHARS]
                lines.append(f"  - {rendered_reason}")
        if self.block_outputs:
            lines.append("produced_block_outputs:")
            for label, payload in list(self.block_outputs.items())[:_MAX_BLOCK_OUTPUTS]:
                serialized = payload if isinstance(payload, str) else json.dumps(payload, default=str)
                serialized = " ".join(serialized.split())[:_EVIDENCE_VALUE_MAX_CHARS]
                source = self.block_output_sources.get(label)
                source_detail = f" [{source}]" if source else ""
                lines.append(f"  - {label}{source_detail}: {serialized}")
        page_evidence = {
            key: self.page_evidence[key]
            for key in _PAGE_EVIDENCE_KEYS
            if key in self.page_evidence and self.page_evidence[key] not in (None, "", [], {})
        }
        if page_evidence:
            serialized = json.dumps(page_evidence, default=str)
            serialized = " ".join(serialized.split())[:_EVIDENCE_VALUE_MAX_CHARS]
            lines.append(f"page_evidence: {serialized}")
        return redact_raw_secrets_for_prompt("\n".join(lines))


_UNAVAILABLE = CompletionVerificationResult(status="unavailable")
_MISSING_VERDICT_EVIDENCE = "judge did not return a verdict for this criterion"
_INCOMPLETE_VALIDATION_CLASSIFICATION_CONTRACT = "incomplete typed classification contract"
_INCOMPLETE_VALIDATION_CLASSIFICATION_ABSTENTION_REASON = "validation_classification_incomplete_contract"
_MISSING_REGISTERED_DOWNLOAD_EVIDENCE = "run output did not include a non-empty registered browser download"
_DELIVERED_UNVERIFIED_OUTPUT_SOURCES = frozenset({"runtime_output", "registered_output_parameter"})


def registered_download_completion_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
        outcome="A browser download is registered with a non-empty downloaded file surface.",
    )


def is_registered_download_completion_criterion(criterion: CompletionCriterion) -> bool:
    return criterion.id == REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID


def _is_positive_download_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _registered_download_evidence_label(snapshot: RunEvidenceSnapshot) -> str | None:
    for label, payload in snapshot.block_outputs.items():
        if not isinstance(payload, dict) or payload.get("download_registered") is not True:
            continue
        if any(_is_positive_download_count(payload.get(key)) for key in _REGISTERED_DOWNLOAD_COUNT_KEYS):
            return str(label)
    return None


def grade_registered_download_criteria(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    registered_criteria = [
        criterion for criterion in criteria if is_registered_download_completion_criterion(criterion)
    ]
    if not registered_criteria:
        return []
    label = _registered_download_evidence_label(snapshot)
    if label is not None:
        return [
            CriterionVerdict(
                criterion_id=criterion.id,
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref=f"block_outputs:{label}",
            )
            for criterion in registered_criteria
        ]
    return [
        CriterionVerdict(
            criterion_id=criterion.id,
            state="unsatisfied",
            reason_code="no_evidence",
            missing_evidence=_MISSING_REGISTERED_DOWNLOAD_EVIDENCE,
        )
        for criterion in registered_criteria
    ]


def _clean_optional_text(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(redact_raw_secrets_for_prompt(value).split())[:max_chars].strip()
    return cleaned or None


def _default_missing_evidence(reason_code: str) -> str:
    if reason_code == "evidence_contradicts":
        return "produced evidence contradicted this criterion"
    if reason_code == "unknown":
        return "judge could not determine this criterion from the produced evidence"
    if reason_code == "no_evidence":
        return "run output did not include evidence for this criterion"
    return "judge did not mark this criterion satisfied"


def verdict_missing_evidence(verdict: CriterionVerdict) -> str | None:
    if verdict.satisfied:
        return None
    cleaned = _clean_optional_text(verdict.missing_evidence, max_chars=_MISSING_EVIDENCE_MAX_CHARS)
    return cleaned or _default_missing_evidence(verdict.reason_code)


def _missing_verdict(criterion_id: str) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion_id,
        state="unknown",
        reason_code="unknown",
        missing_evidence=_MISSING_VERDICT_EVIDENCE,
    )


def summarize_unsatisfied_outcomes(result: CompletionVerificationResult, criteria: list[CompletionCriterion]) -> str:
    outcome_by_id = {criterion.id: criterion.outcome for criterion in criteria}
    satisfied = {verdict.criterion_id for verdict in result.verdicts if verdict.satisfied}
    unmet = [outcome_by_id[cid] for cid in result.criterion_ids if cid not in satisfied and cid in outcome_by_id]
    return "; ".join(unmet)


def _render_criteria(criteria: list[CompletionCriterion]) -> str:
    parts: list[str] = []
    for criterion in criteria:
        flags = ""
        if criterion.implicit:
            flags += " (implicit)"
        if criterion.method_mandated:
            flags += " (method_mandated)"
        contingent_on = f" [contingent_on={criterion.contingent_on}]" if criterion.contingent_on else ""
        antecedent_output_path = (
            f" [contingent_antecedent_output_path={criterion.contingent_antecedent_output_path}]"
            if criterion.contingent_antecedent_output_path
            else ""
        )
        deliverable_kind = f" [deliverable_kind={criterion.deliverable_kind}]" if criterion.deliverable_kind else ""
        output_path = f" [required_output_path={criterion.output_path}]" if criterion.output_path else ""
        classification_output_key = (
            f" [classification_output_key={criterion.classification_output_key}]"
            if criterion.classification_output_key
            else ""
        )
        expected_classification = (
            f" [expected_classification={criterion.expected_classification}]"
            if criterion.expected_classification is not None
            else ""
        )
        parts.append(
            f"- {criterion.id}: {criterion.outcome}"
            f" [kind={criterion.kind}]"
            f"{contingent_on}{antecedent_output_path}{deliverable_kind}{output_path}"
            f"{classification_output_key}{expected_classification}{flags}"
        )
    return "\n".join(parts)


def _contingent_metadata_for_criteria(
    criteria: Iterable[CompletionCriterion],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    contingent_on_by_id = {
        criterion.id: criterion.contingent_on for criterion in criteria if criterion.contingent_on is not None
    }
    contingent_antecedent_output_path_by_id = {
        criterion.id: criterion.contingent_antecedent_output_path
        for criterion in criteria
        if criterion.contingent_antecedent_output_path is not None
    }
    contingent_ids = list(dict.fromkeys([*contingent_on_by_id, *contingent_antecedent_output_path_by_id]))
    return contingent_ids, contingent_on_by_id, contingent_antecedent_output_path_by_id


def _output_path_field(path: str) -> str:
    return path.removeprefix("output.")


def _field_aliases_for_output_path(path: str) -> tuple[str, str]:
    field = _output_path_field(path)
    return field, f"{field}_output"


def _find_structured_field_values(value: Any, field_names: Collection[str]) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in field_names:
                yield item
            yield from _find_structured_field_values(item, field_names)
    elif isinstance(value, list):
        for item in value:
            yield from _find_structured_field_values(item, field_names)


_BLOCKER_PATH_FIELDS = frozenset({"blocker", "manual_service_blocker"})
_BLOCKER_FIELDS = frozenset({"blocker", "blocker_output", "manual_service_blocker", "manual_service_blocker_output"})


def structural_unfired_contingent_criterion_ids(
    criteria: Iterable[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
) -> list[str]:
    unfired_ids: list[str] = []
    for criterion in criteria:
        if is_turn_unsatisfiable_fallback_degraded(criterion):
            continue
        if is_contingent_missing_antecedent_degraded(criterion):
            # The degrade value is itself the source determination that the antecedent never fired,
            # so only affirmative evidence that a real blocker fired can keep the criterion in play.
            if not (_contingent_antecedent_blocker_shaped(criterion) and _has_real_blocker_evidence(snapshot)):
                unfired_ids.append(criterion.id)
            continue
        path = criterion.contingent_antecedent_output_path
        if not path:
            continue
        field_names = set(_field_aliases_for_output_path(path))
        values: list[Any] = []
        for key, value in snapshot.block_outputs.items():
            if key in field_names:
                values.append(value)
            values.extend(_find_structured_field_values(value, field_names))
        if _is_blocker_contingent_criterion(criterion):
            if _has_real_blocker_evidence(snapshot):
                continue
            if _has_structural_no_blocker_evidence(snapshot):
                unfired_ids.append(criterion.id)
                continue
        if not values:
            continue
        if any(_is_meaningful_contingent_antecedent_value(value) for value in values):
            continue
        unfired_ids.append(criterion.id)
    return unfired_ids


def _is_blocker_contingent_criterion(criterion: CompletionCriterion) -> bool:
    path = criterion.contingent_antecedent_output_path
    if path is None:
        return False
    return _output_path_field(path) in _BLOCKER_PATH_FIELDS


def _contingent_antecedent_blocker_shaped(criterion: CompletionCriterion) -> bool:
    """A pathless contingent criterion anchors to blocker evidence only when its own consequent
    writes the blocker field, which makes the run's blocker record evidence about its antecedent."""
    if criterion.contingent_antecedent_output_path is not None:
        return False
    path = criterion.output_path
    return path is not None and _output_path_field(path) in _BLOCKER_PATH_FIELDS


def _blocker_family_values(snapshot: RunEvidenceSnapshot) -> list[Any]:
    values: list[Any] = []
    for key, value in snapshot.block_outputs.items():
        if key in _BLOCKER_FIELDS:
            values.append(value)
        values.extend(_find_structured_field_values(value, _BLOCKER_FIELDS))
    return values


def _has_real_blocker_evidence(snapshot: RunEvidenceSnapshot) -> bool:
    return any(_is_real_blocker_evidence(value) for value in _blocker_family_values(snapshot))


def _is_real_blocker_evidence(value: Any) -> bool:
    return _is_meaningful_contingent_antecedent_value(value) and not _is_structural_no_blocker_marker(value)


def _has_structural_no_blocker_evidence(snapshot: RunEvidenceSnapshot) -> bool:
    """A registered blocker-family key is an explicit negative record whatever its value shape
    (None/""/false/"none"); only an absent key is an absent record, which ``_blocker_family_values`` guarantees."""
    for value in _blocker_family_values(snapshot):
        if _is_structural_no_blocker_marker(value):
            return True
    return False


def _is_structural_no_blocker_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.casefold().split())
    return normalized in {
        "",
        "none",
        "null",
        "false",
        "no",
        "n/a",
        "na",
        "no blocker",
        "no blockers",
        "none found",
        "not blocked",
    }


def _evidence_source_from_ref(
    block_output_sources: Mapping[str, EvidenceSourceKind], evidence_ref: str | None
) -> EvidenceSourceKind | None:
    label = _evidence_ref_record_label(evidence_ref)
    return block_output_sources.get(label) if label else None


def _coerce_result(
    raw: Any,
    criterion_ids: list[str],
    *,
    contingent_criterion_ids: Iterable[str] = (),
    contingent_on_by_criterion_id: dict[str, str] | None = None,
    contingent_antecedent_output_path_by_criterion_id: dict[str, str] | None = None,
    structural_unfired_criterion_ids: Iterable[str] = (),
    block_output_sources: Mapping[str, EvidenceSourceKind] | None = None,
) -> CompletionVerificationResult:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        raw = parse_final_response(raw)
    if not isinstance(raw, dict):
        return _UNAVAILABLE
    raw_verdicts = raw.get("verdicts")
    if not isinstance(raw_verdicts, list):
        return _UNAVAILABLE

    allowed = set(criterion_ids)
    by_id: dict[str, CriterionVerdict] = {}
    for item in raw_verdicts:
        if not isinstance(item, dict):
            continue
        criterion_id = item.get("criterion_id")
        if criterion_id not in allowed or criterion_id in by_id:
            continue
        reason = item.get("reason_code")
        reason_code = reason if isinstance(reason, str) and reason in _REASON_CODES else "unknown"
        # A criterion counts as satisfied only when the judge cites confirming
        # evidence; "no_evidence"/"contradicts" are affirmative negatives, while an
        # incoherent or "unknown" verdict stays unknown — it never passes and never fails.
        if bool(item.get("satisfied")) and reason_code == "evidence_confirms":
            state: CriterionState = "satisfied"
        elif reason_code in ("no_evidence", "evidence_contradicts"):
            state = "unsatisfied"
        else:
            state = "unknown"
        evidence_ref = _clean_optional_text(item.get("evidence_ref"), max_chars=_EVIDENCE_REF_MAX_CHARS)
        missing_evidence = None
        if state != "satisfied":
            missing_evidence = _clean_optional_text(
                item.get("missing_evidence"), max_chars=_MISSING_EVIDENCE_MAX_CHARS
            ) or _default_missing_evidence(reason_code)
        by_id[criterion_id] = CriterionVerdict(
            criterion_id=criterion_id,
            state=state,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
            missing_evidence=missing_evidence,
            evidence_source=_evidence_source_from_ref(block_output_sources or {}, evidence_ref),
        )

    verdicts = [by_id.get(criterion_id, _missing_verdict(criterion_id)) for criterion_id in criterion_ids]
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=list(criterion_ids),
        verdicts=verdicts,
        contingent_criterion_ids=list(contingent_criterion_ids),
        contingent_on_by_criterion_id=dict(contingent_on_by_criterion_id or {}),
        contingent_antecedent_output_path_by_criterion_id=dict(contingent_antecedent_output_path_by_criterion_id or {}),
        structural_unfired_criterion_ids=list(structural_unfired_criterion_ids),
    )


_DEFINITION_PARAMETER_HINT_RE = re.compile(r"\b(?:inputs?|parameters?|params?|reusable|configurable)\b", re.I)
_DEFINITION_INPUT_LIST_RE = re.compile(
    r"(?:accepts?|takes?|uses?|with|defines?)\s+(?P<items>[^.;:]{3,160}?)\s+as\s+(?:the\s+)?"
    r"(?:reusable|configurable|run[ -]?time)?\s*(?:workflow\s+)?(?:inputs?|parameters?)\b"
    r"|(?P<items_are>[^.;:]{3,160}?)\s+(?:are|is|should\s+be|will\s+be)\s+(?:the\s+)?"
    r"(?:reusable|configurable|run[ -]?time)\s+(?:workflow\s+)?(?:inputs?|parameters?)\b",
    re.I,
)
_DEFINITION_INPUT_SPLIT_RE = re.compile(r",|\band\b|&|/", re.I)
_DEFINITION_INPUT_STOPWORDS = frozenset({"the", "a", "an", "my", "their", "all", "of", "workflow", "number", "value"})


def _normalize_input_phrase(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _named_definition_inputs(outcome: str) -> list[str]:
    match = _DEFINITION_INPUT_LIST_RE.search(outcome)
    if match is None:
        return []
    items_text = match.group("items") or match.group("items_are") or ""
    candidates: list[str] = []
    for segment in _DEFINITION_INPUT_SPLIT_RE.split(items_text):
        phrase = _normalize_input_phrase(segment)
        words = [word for word in phrase.split() if word not in _DEFINITION_INPUT_STOPWORDS]
        if 0 < len(words) <= 4:
            candidates.append(" ".join(words))
    return candidates


def _input_matches_any_key(candidate: str, normalized_keys: list[str]) -> bool:
    return any(candidate == key or candidate in key or key in candidate for key in normalized_keys)


def _moustache_reference_pattern(key: str) -> re.Pattern[str]:
    return re.compile(r"\{\{[^{}]*\b" + re.escape(key) + r"\b[^{}]*\}\}")


def _value_references_key(value: Any, key: str, pattern: re.Pattern[str], depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, str):
        return bool(pattern.search(value))
    if isinstance(value, dict):
        parameter_keys = value.get("parameter_keys")
        if isinstance(parameter_keys, list) and key in parameter_keys:
            return True
        return any(_value_references_key(item, key, pattern, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_value_references_key(item, key, pattern, depth + 1) for item in value)
    return False


def _workflow_parameter_reference_state(workflow_yaml: str | None) -> tuple[list[str], list[str]] | None:
    """(defined workflow-parameter keys, the subset referenced by blocks); None when unparseable."""
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return [], []
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    definition = parsed.get("workflow_definition")
    definition = definition if isinstance(definition, dict) else {}
    raw_parameters = definition.get("parameters")
    raw_parameters = raw_parameters if isinstance(raw_parameters, list) else []
    keys = [
        item["key"]
        for item in raw_parameters
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and item["key"].strip()
        and item.get("parameter_type") in (None, "workflow")
    ]
    blocks = definition.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    referenced = [key for key in keys if _value_references_key(blocks, key, _moustache_reference_pattern(key))]
    return keys, referenced


def grade_definition_criteria(criteria: list[CompletionCriterion], workflow_yaml: str | None) -> list[CriterionVerdict]:
    """Grade definition-level criteria against the workflow YAML, never the run snapshot.

    Deterministic check for the reusable-inputs class only; anything else is ``unknown``
    (never ``no_evidence`` — runs cannot evidence the definition plane).
    """
    reference_state = _workflow_parameter_reference_state(workflow_yaml)
    verdicts: list[CriterionVerdict] = []
    for criterion in criteria:
        if not _DEFINITION_PARAMETER_HINT_RE.search(criterion.outcome) or reference_state is None:
            verdicts.append(
                CriterionVerdict(criterion_id=criterion.id, state="unknown", reason_code="definition_unknown")
            )
            continue
        defined, referenced = reference_state
        named = _named_definition_inputs(criterion.outcome)
        if defined and referenced:
            # When the criterion names specific inputs, each must match a defined
            # parameter key; one stray parameter must not satisfy a multi-input ask.
            # An unmatchable name degrades to unknown, not unsatisfied — extraction
            # is heuristic and a false negative would drive repair of correct YAML.
            normalized_keys = [_normalize_input_phrase(key) for key in defined]
            if named and not all(_input_matches_any_key(candidate, normalized_keys) for candidate in named):
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id, state="unknown", reason_code="definition_parameters_unmatched"
                    )
                )
                continue
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="definition_parameters_referenced",
                    evidence_ref="workflow_yaml:" + ",".join(referenced[:8]),
                )
            )
        elif not named:
            # A reusable-inputs hint with no specific named inputs cannot be proven
            # false: a read-only/validation workflow legitimately defines none, so a
            # missing/unreferenced parameter set abstains rather than sinking the run.
            verdicts.append(
                CriterionVerdict(criterion_id=criterion.id, state="unknown", reason_code="definition_parameters_absent")
            )
        elif defined:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id, state="unsatisfied", reason_code="definition_parameters_unreferenced"
                )
            )
        else:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id, state="unsatisfied", reason_code="definition_parameters_missing"
                )
            )
    return verdicts


# Quoted literals need >=4 chars: a 2-3 char quoted token (a state code, "id", "ok")
# collides with incidental prose, too low-specificity to credit on lexical presence.
_QUOTED_LITERAL_RE = re.compile(r"[\"'‘’“”]([^\"'‘’“”]{4,120})[\"'‘’“”]")
_CURRENCY_LITERAL_RE = re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?")
_ISO_DATE_LITERAL_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A bare digit run qualifies as an identifier only at >=5 digits: a 4-digit run is
# usually a year and too low-specificity to credit on a coincidental output match.
_LONG_DIGIT_LITERAL_RE = re.compile(r"\b\d{5,}\b")
# A mixed alphanumeric code (e.g. WTR-1842-DEMO, ABC12345) credits only via the
# letter+digit, >=6 alnum filter below, which excludes bare words and bare years.
_STRUCTURED_ID_LITERAL_RE = re.compile(r"\b[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\b")


def _normalize_present_value(text: str) -> str:
    return " ".join(text.casefold().split())


def _is_structured_identifier(token: str) -> bool:
    alnum = [ch for ch in token if ch.isalnum()]
    return len(alnum) >= 6 and any(ch.isalpha() for ch in alnum) and any(ch.isdigit() for ch in alnum)


def _extract_present_value_literals(outcome: str) -> list[str]:
    literals: list[str] = [match.group(1) for match in _QUOTED_LITERAL_RE.finditer(outcome)]
    for pattern in (_CURRENCY_LITERAL_RE, _ISO_DATE_LITERAL_RE, _LONG_DIGIT_LITERAL_RE):
        literals.extend(pattern.findall(outcome))
    literals.extend(token for token in _STRUCTURED_ID_LITERAL_RE.findall(outcome) if _is_structured_identifier(token))
    normalized: list[str] = []
    seen: set[str] = set()
    for literal in literals:
        candidate = _normalize_present_value(literal)
        if len(candidate) >= 2 and candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def _serialized_block_output_haystacks(block_outputs: dict[str, Any]) -> list[tuple[str, str]]:
    haystacks: list[tuple[str, str]] = []
    for label, payload in block_outputs.items():
        if payload is None:
            continue
        forms = [payload] if isinstance(payload, str) else [json.dumps(payload, default=str), str(payload)]
        normalized = " ".join(_normalize_present_value(form) for form in forms if form)
        if normalized:
            haystacks.append((str(label), normalized))
    return haystacks


def _present_verbatim(literal: str, haystack: str) -> bool:
    # Boundary-aware: plain substring would over-credit a short/numeric literal against
    # a sibling value ('$10' in '$100', 'ca' in 'california') — require token boundaries.
    start = 0
    while (idx := haystack.find(literal, start)) != -1:
        before = haystack[idx - 1] if idx else ""
        before_prev = haystack[idx - 2] if idx >= 2 else ""
        end = idx + len(literal)
        after = haystack[end] if end < len(haystack) else ""
        after_next = haystack[end + 1] if end + 1 < len(haystack) else ""
        embedded = (
            before.isalnum()
            or after.isalnum()
            or (after in ".," and after_next.isdigit())
            or (before in ".," and before_prev.isdigit())
        )
        if not embedded:
            return True
        start = idx + 1
    return False


def grade_present_value_criteria(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    """Deterministically credit a run-plane criterion whose explicitly named/quoted
    value appears verbatim in the run's own block outputs.

    Abstains (emits nothing) for any criterion lacking a high-specificity literal or
    whose literal is not present, so the judge keeps deciding and recall is never
    weakened; only ever upgrades to ``satisfied``.
    """
    haystacks = _serialized_block_output_haystacks(snapshot.block_outputs)
    if not haystacks:
        return []
    verdicts: list[CriterionVerdict] = []
    for criterion in criteria:
        literals = _extract_present_value_literals(criterion.outcome)
        if not literals:
            continue
        # Every named literal must appear in a SINGLE block output: a partial match
        # (e.g. a date present while the named total is not) is not the named outcome.
        match_label = next(
            (
                label
                for label, haystack in haystacks
                if all(_present_verbatim(literal, haystack) for literal in literals)
            ),
            None,
        )
        if match_label is not None:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="present_value_verbatim",
                    evidence_ref=f"block_outputs:{match_label}",
                )
            )
    return verdicts


def _classification_string(value: str) -> str:
    return " ".join(value.casefold().split())


def _classification_value_matches(expected: str | bool, actual: Any) -> bool:
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if not isinstance(actual, str):
        return False
    normalized_expected = _classification_string(expected)
    normalized_actual = _classification_string(actual)
    return bool(normalized_expected) and normalized_actual == normalized_expected


def _is_empty_classification_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _classification_canonical_value(expected: str | bool, actual: Any) -> str | bool | None:
    if not _classification_value_matches(expected, actual):
        return None
    if isinstance(expected, bool):
        return actual
    return _classification_string(actual)


def _classification_output_candidates(
    snapshot: RunEvidenceSnapshot,
    output_key: str,
) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    for label, payload in snapshot.block_outputs.items():
        if isinstance(payload, dict) and output_key in payload:
            candidates.append((f"block_outputs:{label}.{output_key}", payload[output_key]))
    return sorted(candidates, key=lambda candidate: candidate[0])


def _validation_classification_unsatisfied(
    criterion: CompletionCriterion,
    *,
    reason_code: Literal["no_evidence", "evidence_contradicts"],
    output_key: str,
    evidence_ref: str | None = None,
    missing_evidence: str | None = None,
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion.id,
        state="unsatisfied",
        reason_code=reason_code,
        evidence_ref=evidence_ref,
        missing_evidence=missing_evidence,
        output_path=output_key,
        grounding_mode="exact_value",
        has_exact_value=True,
    )


def grade_validation_classification_criteria(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    verdicts: list[CriterionVerdict] = []
    for criterion in criteria:
        if criterion.kind != "validation_classification":
            continue
        output_key = criterion.classification_output_key
        expected = criterion.expected_classification
        if output_key is None or expected is None:
            # An incomplete typed contract cannot grade anything, so it fails safe to a non-sinking
            # abstention like grade_definition_criteria rather than a sinking unsatisfied verdict —
            # a value-less criterion minted without the fields this arm needs must not disown a
            # delivered sibling output. It can never satisfy on its own: is_fully_satisfied still
            # requires a genuinely satisfied run-plane criterion.
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unknown",
                    reason_code=_INCOMPLETE_VALIDATION_CLASSIFICATION_ABSTENTION_REASON,
                    missing_evidence=_INCOMPLETE_VALIDATION_CLASSIFICATION_CONTRACT,
                )
            )
            continue
        candidates = _classification_output_candidates(snapshot, output_key)
        if not candidates:
            verdicts.append(
                _validation_classification_unsatisfied(
                    criterion,
                    reason_code="no_evidence",
                    output_key=output_key,
                    missing_evidence=f"missing classification output key {output_key}",
                )
            )
            continue
        if len(candidates) == 1:
            evidence_ref, actual = candidates[0]
            if _is_empty_classification_value(actual):
                verdicts.append(
                    _validation_classification_unsatisfied(
                        criterion,
                        reason_code="no_evidence",
                        output_key=output_key,
                        evidence_ref=evidence_ref,
                        missing_evidence=f"empty classification output key {output_key}",
                    )
                )
                continue
            if not _classification_value_matches(expected, actual):
                verdicts.append(
                    _validation_classification_unsatisfied(
                        criterion,
                        reason_code="evidence_contradicts",
                        output_key=output_key,
                        evidence_ref=evidence_ref,
                        missing_evidence=f"classification output key {output_key} did not match expected value",
                    )
                )
                continue
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="satisfied",
                    reason_code="evidence_confirms",
                    evidence_ref=evidence_ref,
                    output_path=output_key,
                    grounding_mode="exact_value",
                    has_exact_value=True,
                )
            )
            continue
        matching_canonical_values: set[str | bool] = set()
        contradiction_evidence_ref: str | None = None
        for evidence_ref, actual in candidates:
            if _is_empty_classification_value(actual):
                contradiction_evidence_ref = evidence_ref
                break
            canonical_value = _classification_canonical_value(expected, actual)
            if canonical_value is None:
                contradiction_evidence_ref = evidence_ref
                break
            matching_canonical_values.add(canonical_value)
        if contradiction_evidence_ref is not None or len(matching_canonical_values) != 1:
            verdicts.append(
                _validation_classification_unsatisfied(
                    criterion,
                    reason_code="evidence_contradicts",
                    output_key=output_key,
                    evidence_ref=contradiction_evidence_ref,
                    missing_evidence=f"classification output key {output_key} had contradictory evidence",
                )
            )
            continue
        evidence_ref = candidates[0][0]
        verdicts.append(
            CriterionVerdict(
                criterion_id=criterion.id,
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref=evidence_ref,
                output_path=output_key,
                grounding_mode="exact_value",
                has_exact_value=True,
            )
        )
    return verdicts


def grade_record_semantic_consistency(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    """Deterministically reject internally contradictory structured-record status outputs."""

    criterion = next(iter(_status_consistency_criterion(criteria)), None)
    if criterion is None:
        return []

    for label, payload in snapshot.block_outputs.items():
        contradiction = _structured_record_contradiction(payload)
        if contradiction:
            return [
                CriterionVerdict(
                    criterion_id=criterion.id,
                    state="unsatisfied",
                    reason_code="evidence_contradicts",
                    evidence_ref=f"block_outputs:{label}",
                    missing_evidence=contradiction,
                )
            ]
    return []


_STRUCTURED_RECORD_CRITERION_IDS = frozenset(
    {
        "fallback_record_identity",
        "fallback_record_identifier",
        "fallback_record_groups",
        "fallback_record_status",
    }
)


def grade_structured_record_criteria(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    """Deterministically credit generic structured-record fallback criteria.

    Single-block-wins: a criterion set is credited only when one block satisfies it.
    Verdicts are never merged across blocks because a structured record is a single
    coherent record, and crediting fields drawn from different blocks could certify a
    record the run never produced as a whole.
    """

    criteria_by_id = {
        criterion.id: criterion for criterion in criteria if criterion.id in _STRUCTURED_RECORD_CRITERION_IDS
    }
    if not criteria_by_id:
        return []
    best_verdicts: list[CriterionVerdict] = []
    for label, payload in snapshot.block_outputs.items():
        record = _structured_record_payload(payload)
        if record is None:
            continue
        verdicts: list[CriterionVerdict] = []
        if "fallback_record_identity" in criteria_by_id and structured_record_has_identity(record):
            verdicts.append(_structured_record_satisfied("fallback_record_identity", label))
        if "fallback_record_identifier" in criteria_by_id and _structured_record_has_identifier(record):
            verdicts.append(_structured_record_satisfied("fallback_record_identifier", label))
        if "fallback_record_groups" in criteria_by_id and _structured_record_has_group_entries(record):
            verdicts.append(_structured_record_satisfied("fallback_record_groups", label))
        if "fallback_record_status" in criteria_by_id:
            contradiction = _structured_record_contradiction(record)
            if contradiction:
                verdicts.append(
                    CriterionVerdict(
                        criterion_id="fallback_record_status",
                        state="unsatisfied",
                        reason_code="evidence_contradicts",
                        evidence_ref=f"block_outputs:{label}",
                        missing_evidence=contradiction,
                    )
                )
            elif _structured_record_has_status(record):
                verdicts.append(_structured_record_satisfied("fallback_record_status", label))
        if verdicts:
            if len(verdicts) == len(criteria_by_id):
                return verdicts
            if len(verdicts) > len(best_verdicts):
                best_verdicts = verdicts
    return best_verdicts


_TERMINAL_ACTION_KEY_TOKENS = (
    (("submitted",), "submission"),
    (("request", "submitted"), "request"),
    (("application", "submitted"), "application"),
    (("form", "submitted"), "form"),
    (("order", "placed"), "order"),
)
_TERMINAL_ARTIFACT_KEY_TOKENS = (
    (("confirmation", "number"), "confirmation"),
    (("confirmation", "id"), "confirmation"),
    (("request", "number"), "request"),
    (("request", "id"), "request"),
    (("submission", "id"), "submission"),
    (("submission", "number"), "submission"),
    (("application", "number"), "application"),
    (("application", "id"), "application"),
    (("order", "number"), "order"),
    (("order", "id"), "order"),
    (("receipt", "number"), "receipt"),
    (("receipt", "id"), "receipt"),
    (("reference", "number"), "reference"),
    (("reference", "id"), "reference"),
)
_GENERIC_TERMINAL_SUCCESS_LEAF_TOKENS = frozenset({"submit", "submitted", "submission", "place", "placed"})
_NEGATIVE_GUARD_TOKENS = frozenset({"blocker", "error", "failure", "failed", "challenge"})
_NEGATIVE_TERMINAL_STATUS_VALUES = frozenset(
    {
        "blocked",
        "cancelled",
        "canceled",
        "captcha required",
        "denied",
        "error",
        "failed",
        "failure",
        "incomplete",
        "not submitted",
        "timeout",
        "unable",
    }
)
_TERMINAL_RECORD_FAMILIES: tuple[TerminalActionFamily, ...] = ("request", "application", "form", "order")
_TERMINAL_RECORD_FAMILY_ARTIFACTS: dict[TerminalActionFamily, frozenset[str]] = {
    "request": frozenset({"confirmation", "reference", "request", "submission"}),
    "application": frozenset({"application", "confirmation", "reference", "submission"}),
    "form": frozenset({"confirmation", "reference", "submission"}),
    "order": frozenset({"order", "receipt"}),
}
_TERMINAL_RECORD_FAMILY_ACTIONS: dict[TerminalActionFamily, frozenset[str]] = {
    "request": frozenset({"request", "submission"}),
    "application": frozenset({"application", "submission"}),
    "form": frozenset({"form", "submission"}),
    "order": frozenset({"order"}),
}
_VALIDATION_REVIEW_MIN_VALUE_COUNT = 2


def grade_terminal_goal_record_criteria(
    criteria: list[CompletionCriterion], snapshot: RunEvidenceSnapshot
) -> list[CriterionVerdict]:
    verdicts: list[CriterionVerdict] = []
    eligible_criteria = [
        criterion
        for criterion in criteria
        if criterion.kind == "terminal_action" and criterion.terminal_action_family in _TERMINAL_RECORD_FAMILY_ARTIFACTS
    ]
    if not eligible_criteria:
        return []
    for label, payload in snapshot.block_outputs.items():
        record = _structured_record_payload(payload)
        if record is None:
            continue
        for criterion in eligible_criteria:
            family = criterion.terminal_action_family
            if family is not None and _terminal_goal_record_confirmed(record, family):
                verdicts.append(
                    CriterionVerdict(
                        criterion_id=criterion.id,
                        state="satisfied",
                        reason_code="evidence_confirms",
                        evidence_ref=f"block_outputs:{label}",
                        grounding_mode="terminal_record",
                        evidence_source="terminal_record",
                    )
                )
        if verdicts:
            return verdicts
    return []


def grade_terminal_goal_record_corroboration(snapshot: RunEvidenceSnapshot) -> list[CriterionVerdict]:
    for label, payload in snapshot.block_outputs.items():
        record = _structured_record_payload(payload)
        if record is None:
            continue
        if any(_terminal_goal_record_confirmed(record, family) for family in _TERMINAL_RECORD_FAMILIES):
            return [
                CriterionVerdict(
                    criterion_id=TERMINAL_RECORD_CORROBORATION_CRITERION_ID,
                    state="satisfied",
                    reason_code="evidence_confirms",
                    evidence_ref=f"block_outputs:{label}",
                    grounding_mode="terminal_record",
                    evidence_source="terminal_record",
                )
            ]
    return []


_FALLBACK_FLOOR_CARRIER_SOURCES: frozenset[EvidenceSourceKind] = frozenset(
    {"independent_page_evidence", "registered_artifact_content"}
)


def grade_fallback_floor_reached_end_state_criteria(
    criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
    *,
    carrier_verdicts: tuple[CriterionVerdict, ...] = (),
) -> list[CriterionVerdict]:
    eligible_criteria = [criterion for criterion in criteria if is_fallback_floor_base_criterion(criterion)]
    if not eligible_criteria:
        return []
    floor_degraded = any(criterion.mint_degrade is not None for criterion in eligible_criteria)
    evidence_ref = None
    if not floor_degraded:
        evidence_ref = _fallback_floor_reached_end_state_evidence_ref(snapshot, criteria)
    if evidence_ref is None:
        evidence_ref = _fallback_floor_carrier_evidence_ref(snapshot, carrier_verdicts)
    if evidence_ref is None:
        return []
    return [
        CriterionVerdict(
            criterion_id=criterion.id,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref=evidence_ref,
        )
        for criterion in eligible_criteria
    ]


def _fallback_floor_reached_end_state_evidence_ref(
    snapshot: RunEvidenceSnapshot, criteria: list[CompletionCriterion]
) -> str | None:
    for label, payload in snapshot.block_outputs.items():
        if _fallback_floor_parent_record_poisoned(payload):
            continue
        record = _structured_record_payload(payload)
        if record is not None and any(
            _terminal_goal_record_confirmed(record, family) for family in _TERMINAL_RECORD_FAMILIES
        ):
            return f"block_outputs:{label}"
        if any(
            _validation_review_record_confirmed(candidate, criteria)
            for candidate in _fallback_floor_record_candidates(payload)
        ):
            return f"block_outputs:{label}"
    return None


def _fallback_floor_parent_record_poisoned(payload: Any) -> bool:
    return isinstance(payload, dict) and (
        _terminal_goal_record_has_negative_guard(payload) or _structured_record_contradiction(payload) is not None
    )


def _fallback_floor_carrier_evidence_ref(
    snapshot: RunEvidenceSnapshot, carrier_verdicts: tuple[CriterionVerdict, ...]
) -> str | None:
    if any(_fallback_floor_parent_record_poisoned(payload) for payload in snapshot.block_outputs.values()):
        return None
    for verdict in carrier_verdicts:
        if (
            verdict.state == "satisfied"
            and verdict.reason_code == "evidence_confirms"
            and verdict.evidence_source in _FALLBACK_FLOOR_CARRIER_SOURCES
        ):
            return verdict.evidence_ref or f"carrier:{verdict.criterion_id}"
    return None


def _fallback_floor_record_candidates(payload: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(payload, dict):
        return
    yield payload
    for key, value in payload.items():
        if isinstance(key, str) and key.endswith("_output") and isinstance(value, dict):
            yield value


def _validation_review_record_confirmed(record: dict[str, Any], criteria: list[CompletionCriterion]) -> bool:
    if _terminal_goal_record_has_negative_guard(record):
        return False
    if _structured_record_contradiction(record):
        return False
    if record.get("all_checks_passed") is False:
        return False
    review_values = _normalized_review_values(record)
    if len(review_values) < _VALIDATION_REVIEW_MIN_VALUE_COUNT:
        return False
    evidence_text = record.get("evidence_text")
    if not isinstance(evidence_text, str):
        return False
    haystack = _normalize_present_value(evidence_text)
    if not all(_present_verbatim(value, haystack) for value in review_values):
        return False
    if not _validation_review_satisfies_requested_literals(haystack, criteria):
        return False
    return (
        _has_validation_only_page_evidence(record)
        and _has_review_page_evidence(record)
        and _has_no_submit_page_evidence(record)
    )


def _normalized_review_values(record: dict[str, Any]) -> list[str]:
    review_values = record.get("review_values")
    if not isinstance(review_values, dict):
        review_values = record.get("review_fields")
    if not isinstance(review_values, dict):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for _key, value in _walk_record_scalars(review_values):
        if isinstance(value, bool):
            continue
        candidate = _normalize_present_value(str(value))
        if len(candidate) >= 4 and candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def _validation_review_satisfies_requested_literals(
    normalized_evidence_text: str, criteria: list[CompletionCriterion]
) -> bool:
    literal_groups = [
        _extract_present_value_literals(criterion.outcome)
        for criterion in criteria
        if not is_fallback_floor_base_criterion(criterion)
    ]
    literal_groups = [literals for literals in literal_groups if literals]
    if not literal_groups:
        return True
    return all(
        all(_present_verbatim(literal, normalized_evidence_text) for literal in literals) for literals in literal_groups
    )


def _has_validation_only_page_evidence(record: dict[str, Any]) -> bool:
    for key, value in _walk_record_scalars(record):
        leaf_tokens = _leaf_key_word_tokens(key)
        normalized_value = _normalize_present_value(str(value))
        if leaf_tokens == {"validation", "only"} and value is True:
            return True
        if leaf_tokens == {"submit", "mode"} and normalized_value == "validation_only":
            return True
    return False


def _has_review_page_evidence(record: dict[str, Any]) -> bool:
    has_grounded_review_page = False
    for key, value in _walk_record_scalars(record):
        tokens = _key_word_tokens(key)
        if _is_review_page_signal(tokens):
            if _signal_value_is_explicit_false(value):
                return False
            if _signal_value_is_explicit_positive(value):
                has_grounded_review_page = True
        if _is_review_page_text_field(tokens) and isinstance(value, str):
            if _present_verbatim("review", _normalize_present_value(value)):
                has_grounded_review_page = True
    return has_grounded_review_page


def _final_submit_click_held(record: dict[str, Any]) -> bool:
    held = False
    for key, value in _walk_record_scalars(record):
        tokens = _key_word_tokens(key)
        if not _is_submit_or_finalize_click_signal(tokens):
            continue
        if _signal_value_is_meaningful_positive(value):
            return False
        if _signal_value_is_explicit_false(value):
            held = True
    return held


def _submitted_or_confirmation_held(record: dict[str, Any]) -> bool:
    held = False
    for key, value in _walk_record_scalars(record):
        tokens = _key_word_tokens(key)
        if not (_is_submitted_request_signal(tokens) or _is_confirmation_visible_signal(tokens)):
            continue
        if _signal_value_is_meaningful_positive(value):
            return False
        if _signal_value_is_explicit_false(value):
            held = True
    return held


def _submitted_or_confirmation_reached(record: dict[str, Any]) -> bool:
    for key, value in _walk_record_scalars(record):
        tokens = _key_word_tokens(key)
        if not (_is_submitted_request_signal(tokens) or _is_confirmation_visible_signal(tokens)):
            continue
        if _signal_value_is_meaningful_positive(value):
            return True
    return False


def _review_page_with_final_controls_visible(record: dict[str, Any]) -> bool:
    if not _has_review_page_evidence(record):
        return False
    final_controls = record.get("final_controls_visible")
    expected_controls = {"submit request", "submit", "back"}
    if not isinstance(final_controls, list):
        final_controls = record.get("submit_controls_visible")
        expected_controls = {"submit request", "submit"}
    if isinstance(final_controls, list):
        visible = {_normalize_present_value(str(value)) for value in final_controls if isinstance(value, str)}
        if visible & expected_controls:
            return True
    return any(
        "final" in _key_word_tokens(key)
        and "submit" in _key_word_tokens(key)
        and bool({"control", "controls"} & _key_word_tokens(key))
        and _positive_integer_count(value)
        for key, value in _walk_record_scalars(record)
    )


def _positive_integer_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _has_no_submit_page_evidence(record: dict[str, Any]) -> bool:
    if not _final_submit_click_held(record):
        return False
    if _submitted_or_confirmation_reached(record):
        return False
    if _submitted_or_confirmation_held(record):
        return True
    return _review_page_with_final_controls_visible(record)


def _is_review_page_signal(tokens: set[str]) -> bool:
    return (
        {"review", "visible"} <= tokens
        or {"review", "reached"} <= tokens
        or ({"pre", "submit"} <= tokens and ("visible" in tokens or "reached" in tokens))
    )


def _is_review_page_text_field(tokens: set[str]) -> bool:
    return "page" in tokens and bool(tokens & {"current", "final", "title"})


def _is_submit_or_finalize_click_signal(tokens: set[str]) -> bool:
    return bool(tokens & {"clicked", "click"}) and bool(tokens & {"submit", "finalize", "final"})


def _is_submitted_request_signal(tokens: set[str]) -> bool:
    return {"submitted", "request"} <= tokens


def _is_confirmation_visible_signal(tokens: set[str]) -> bool:
    return "confirmation" in tokens and ("visible" in tokens or "page" in tokens)


def _signal_value_is_explicit_false(value: Any) -> bool:
    if value is False:
        return True
    if not isinstance(value, str):
        return False
    normalized = _normalize_present_value(value)
    return normalized in {
        "false",
        "no",
        "none",
        "absent",
        "hidden",
        "not visible",
        "not reached",
        "not clicked",
        "not submitted",
        "never clicked",
        "never submitted",
    }


def _signal_value_is_explicit_positive(value: Any) -> bool:
    if value is True:
        return True
    if not isinstance(value, str):
        return False
    normalized = _normalize_present_value(value)
    return normalized in {
        "true",
        "yes",
        "visible",
        "shown",
        "displayed",
        "present",
        "reached",
        "loaded",
        "opened",
        "current",
    }


def _signal_value_is_meaningful_positive(value: Any) -> bool:
    return _is_meaningful_record_value(value) and not _signal_value_is_explicit_false(value)


def _terminal_goal_record_confirmed(record: dict[str, Any], family: TerminalActionFamily) -> bool:
    if _terminal_goal_record_has_negative_guard(record):
        return False
    if _structured_record_contradiction(record):
        return False
    action_families = [
        action_family
        for key, value in _walk_record_scalars(record)
        if (action_family := _terminal_action_family_for_key(key)) is not None and value is True
    ]
    if any(
        value is False
        for key, value in _walk_record_scalars(record)
        if _terminal_action_family_for_key(key) is not None
    ):
        return False
    has_family_artifact = _terminal_goal_record_has_artifact_for_family(record, family)
    if not action_families:
        return has_family_artifact and not _terminal_goal_record_has_generic_success_claim(record)
    if family not in _TERMINAL_RECORD_FAMILY_ACTIONS:
        return False
    if not any(action_family in _TERMINAL_RECORD_FAMILY_ACTIONS[family] for action_family in action_families):
        return False
    return has_family_artifact


def _terminal_action_family_for_key(key: str) -> str | None:
    leaf_tokens = _record_key_leaf_tokens(key)
    for key_tokens, family in _TERMINAL_ACTION_KEY_TOKENS:
        if leaf_tokens == key_tokens:
            return family
    return None


def _terminal_goal_record_has_generic_success_claim(record: dict[str, Any]) -> bool:
    generic_success_keys = {
        ("completed",),
        ("succeeded",),
        ("success",),
    }
    for key, value in _walk_record_scalars(record):
        leaf_tokens = _record_key_leaf_tokens(key)
        if value is True and _terminal_action_family_for_key(key) is None and leaf_tokens:
            if leaf_tokens[-1] in _GENERIC_TERMINAL_SUCCESS_LEAF_TOKENS:
                return True
        if leaf_tokens in generic_success_keys and value is True:
            return True
        if "status" in _key_word_tokens(key) and isinstance(value, str):
            polarity = _status_polarity(value)
            if polarity is not None and not polarity[0]:
                return True
    return False


def _terminal_goal_record_has_negative_guard(record: dict[str, Any]) -> bool:
    for key, value in _walk_record_scalars(record):
        tokens = _key_word_tokens(key)
        if tokens & _NEGATIVE_GUARD_TOKENS and _is_meaningful_record_value(value) and value is not False:
            return True
        if "status" in tokens and isinstance(value, str):
            normalized_status = _normalize_present_value(value)
            if normalized_status in _NEGATIVE_TERMINAL_STATUS_VALUES or any(
                phrase in normalized_status for phrase in ("captcha required", "not submitted", "unable")
            ):
                return True
            polarity = _status_polarity(value)
            if polarity is not None and polarity[0]:
                return True
    return False


def _terminal_goal_record_has_artifact_for_family(record: dict[str, Any], family: TerminalActionFamily) -> bool:
    allowed_artifacts = _TERMINAL_RECORD_FAMILY_ARTIFACTS[family]
    for key, value in _walk_record_scalars(record):
        if isinstance(value, bool) or not _is_meaningful_record_value(value):
            continue
        leaf_tokens = _record_key_leaf_tokens(key)
        for key_tokens, artifact_family in _TERMINAL_ARTIFACT_KEY_TOKENS:
            if leaf_tokens == key_tokens and artifact_family in allowed_artifacts:
                return True
    return False


def _record_key_leaf_tokens(key: str) -> tuple[str, ...]:
    leaf = re.sub(r"\[\d+\]$", "", key.rsplit(".", 1)[-1])
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", leaf).casefold()
    return tuple(re.findall(r"[a-z0-9]+", spaced))


def _structured_record_satisfied(criterion_id: str, label: str) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion_id,
        state="satisfied",
        reason_code="evidence_confirms",
        evidence_ref=f"block_outputs:{label}",
    )


def _structured_record_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if (
            isinstance(key, str)
            and key.endswith("_output")
            and isinstance(value, dict)
            and _looks_like_structured_record(value)
        ):
            return value
    if _looks_like_structured_record(payload):
        return payload
    return None


def _looks_like_structured_record(value: dict[str, Any]) -> bool:
    return (
        structured_record_has_identity(value)
        or _structured_record_has_identifier(value)
        or _structured_record_has_group_entries(value)
        or _structured_record_has_status(value)
    )


def structured_record_has_identity(record: dict[str, Any]) -> bool:
    if any(value is True for key, value in record.items() if isinstance(key, str) and key.endswith("_found")):
        return True
    return any(
        isinstance(value, str) and bool(value.strip())
        for key, value in record.items()
        if isinstance(key, str) and _key_has_identity_token(key)
    )


def _key_has_identity_token(key: str) -> bool:
    normalized_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).casefold().replace("_", " ")
    return any(
        re.search(rf"(?:^|[^a-z0-9]){re.escape(token)}(?:$|[^a-z0-9])", normalized_key)
        for token in ("name", "title", "entity", "label")
    )


def structured_record_has_goal_content(record: dict[str, Any]) -> bool:
    """Return True only when a structured record has the full terminal-proof shape."""

    if (
        not structured_record_has_identity(record)
        or not _structured_record_has_identifier(record)
        or not _record_summary_status(record)
    ):
        return False
    return any(
        bool(status.strip())
        and any(
            _is_meaningful_record_value(item_value)
            for item_key, item_value in item.items()
            if not (isinstance(item_key, str) and "status" in item_key.casefold())
        )
        for status, item in _record_row_statuses(record)
    )


def _key_word_tokens(key: str) -> set[str]:
    """Split a (possibly dotted/camelCase) record key path into whole-word tokens."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).casefold()
    return set(re.findall(r"[a-z0-9]+", spaced))


def _leaf_key_word_tokens(key: str) -> set[str]:
    leaf = key.rsplit(".", 1)[-1]
    return _key_word_tokens(leaf)


def _structured_record_has_identifier(record: dict[str, Any]) -> bool:
    for key, value in _walk_record_scalars(record):
        value_text = str(value)
        digit_runs = "".join(ch if ch.isdigit() else " " for ch in value_text).split()
        if any(len(run) >= 6 for run in digit_runs):
            return True
        if _key_word_tokens(key) & {"identifier", "id", "number"} and value_text.strip():
            return True
    return False


def _structured_record_has_group_entries(record: dict[str, Any]) -> bool:
    for value in record.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and any(_is_meaningful_record_value(nested) for nested in item.values()):
                return True
    return False


def _structured_record_has_status(record: dict[str, Any]) -> bool:
    summary_status = _record_summary_status(record)
    row_statuses = _record_row_statuses(record)
    return bool(summary_status and row_statuses)


def _status_consistency_criterion(criteria: list[CompletionCriterion]) -> Iterable[CompletionCriterion]:
    for criterion in criteria:
        normalized = _normalize_present_value(criterion.outcome)
        if "status" in normalized and (
            "consistent" in normalized or "overall" in normalized or "summary" in normalized or "per-" in normalized
        ):
            yield criterion


def _structured_record_contradiction(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if not (_structured_record_has_group_entries(payload) or _structured_record_has_status(payload)):
        return None
    summary_status = _record_summary_status(payload)
    summary_polarity = _status_polarity(summary_status) if summary_status else None
    row_statuses = _record_row_statuses(payload)
    for status, item in row_statuses:
        polarity = _status_polarity(status)
        if polarity is None:
            continue
        is_negative, positive_phrase = polarity
        if not is_negative:
            continue
        non_status_text = " ".join(
            str(value)
            for key, value in item.items()
            if not (isinstance(key, str) and "status" in key.casefold()) and value is not None
        )
        if _contains_positive_status_phrase(non_status_text, positive_phrase):
            return (
                "a parsed row reports a negative status, but non-status fields include the positive status text; "
                "parse name/address/status from cells in the same row"
            )
    if summary_polarity is not None:
        summary_negative, summary_positive_phrase = summary_polarity
        if summary_negative and any(
            (row_polarity := _status_polarity(status)) is not None and row_polarity == (False, summary_positive_phrase)
            for status, _item in row_statuses
        ):
            return "summary status is negative, but a parsed row has the matching positive status"

    evidence_text = payload.get("evidence_text")
    if isinstance(evidence_text, str) and summary_polarity is not None:
        summary_negative, summary_positive_phrase = summary_polarity
        if (
            summary_negative
            and row_statuses
            and not any(_status_polarity(status) == (False, summary_positive_phrase) for status, _item in row_statuses)
            and _contains_positive_status_phrase(evidence_text, summary_positive_phrase)
        ):
            return (
                "evidence_text contains positive status rows, but structured rows and summary report a negative status"
            )
    return None


def _record_summary_status(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized_key = key.casefold()
        if "status" in normalized_key and ("overall" in normalized_key or "summary" in normalized_key):
            return value
    value = record.get("status")
    return value if isinstance(value, str) else None


def _record_row_statuses(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    statuses: list[tuple[str, dict[str, Any]]] = []
    for value in record.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            for item_key, item_value in item.items():
                if isinstance(item_key, str) and "status" in item_key.casefold() and isinstance(item_value, str):
                    statuses.append((item_value, item))
    return statuses


_NEGATIVE_STATUS_POSITIVE_COUNTERPARTS = {
    "expired": "active",
    "inactive": "active",
    "lapsed": "active",
    "pending": "active",
    "revoked": "active",
    "suspended": "active",
    "terminated": "active",
}


def _status_polarity(value: str) -> tuple[bool, str] | None:
    normalized = _normalize_present_value(value)
    if not normalized:
        return None
    if normalized in _NEGATIVE_STATUS_POSITIVE_COUNTERPARTS:
        return True, _NEGATIVE_STATUS_POSITIVE_COUNTERPARTS[normalized]
    for prefix in ("not ", "non-", "non "):
        if normalized.startswith(prefix):
            positive_phrase = normalized[len(prefix) :].strip()
            return (True, positive_phrase) if positive_phrase else None
    return False, normalized


# Negators, temporal qualifiers, and negative-status words that, near a positive-status
# word, flip it into a negative or adjectival usage ("no longer active", "previously
# active", "the active license expired") rather than a positive status assertion.
_STATUS_NEGATION_QUALIFIERS = frozenset(
    {
        "not",
        "non",
        "no",
        "longer",
        "previously",
        "formerly",
        "once",
        "currently",
        "recently",
        "was",
        "were",
        "until",
        "never",
        *_NEGATIVE_STATUS_POSITIVE_COUNTERPARTS,
    }
)


def _status_word_tokens(text: str) -> list[str]:
    return "".join(char if char.isalnum() else " " for char in text.casefold()).split()


def _contains_positive_status_phrase(text: str, positive_phrase: str) -> bool:
    tokens = _status_word_tokens(text)
    phrase_tokens = _status_word_tokens(positive_phrase)
    if not tokens or not phrase_tokens:
        return False
    span = len(phrase_tokens)
    for start in range(len(tokens) - span + 1):
        if tokens[start : start + span] != phrase_tokens:
            continue
        window = set(tokens[max(0, start - 3) : start]) | set(tokens[start + span : start + span + 3])
        if not window & _STATUS_NEGATION_QUALIFIERS:
            return True
    return False


def _walk_record_scalars(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            nested_prefix = f"{prefix}.{key_text}" if prefix else key_text
            yield from _walk_record_scalars(nested, nested_prefix)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_record_scalars(nested, f"{prefix}[{index}]")
    elif value is not None:
        yield prefix, value


def _is_meaningful_record_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _is_meaningful_contingent_antecedent_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _is_meaningful_record_value(value)


_DEFINITION_REASON_PREFIX = "definition_"
_OBSERVED_END_STATE_EVIDENCE_REF = "observed_end_state_url"
_REPERCEPTION_CONTRADICTION_EVIDENCE_REFS = frozenset({"scout_synthesized_browser_steps_output"})
_INDEPENDENT_REQUESTED_OUTPUT_CORROBORATOR_SOURCES = frozenset(
    {"independent_page_evidence", "registered_output_parameter", "registered_artifact_content"}
)
_SELF_EMITTED_EVIDENCE_SOURCES = frozenset({"runtime_output", "same_record_context"})
_FLOOR_REKEYED_DELIVERABLE_CREDIT_SOURCES: frozenset[EvidenceSourceKind] = frozenset({"independent_page_evidence"})


def _is_satisfied_observed_end_state_verdict(verdict: CriterionVerdict) -> bool:
    return (
        verdict.state == "satisfied"
        and verdict.reason_code == "evidence_confirms"
        and verdict.evidence_ref == _OBSERVED_END_STATE_EVIDENCE_REF
    )


def _is_satisfied_terminal_record_verdict(verdict: CriterionVerdict) -> bool:
    return (
        verdict.state == "satisfied"
        and verdict.reason_code == "evidence_confirms"
        and verdict.grounding_mode == "terminal_record"
    )


def _is_reperception_contradiction(verdict: CriterionVerdict) -> bool:
    return (
        verdict.state == "unsatisfied"
        and verdict.reason_code == "evidence_contradicts"
        and verdict.evidence_ref in _REPERCEPTION_CONTRADICTION_EVIDENCE_REFS
    )


def _is_definition_plane_abstention(verdict: CriterionVerdict) -> bool:
    return verdict.state == "unknown" and verdict.reason_code.startswith(_DEFINITION_REASON_PREFIX)


def _is_incomplete_validation_classification_abstention(verdict: CriterionVerdict) -> bool:
    return verdict.state == "unknown" and verdict.reason_code == _INCOMPLETE_VALIDATION_CLASSIFICATION_ABSTENTION_REASON


def _is_structural_requested_output_abstention(verdict: CriterionVerdict) -> bool:
    return verdict.state == "unsatisfied" and verdict.reason_code == _STRUCTURAL_ABSTENTION_REASON_CODE


def _is_corroborated_structural_requested_output_abstention(
    verdict: CriterionVerdict,
    verdict_by_id: dict[str, CriterionVerdict],
    criterion_id: str,
    has_corroboration: bool,
) -> bool:
    if verdict.requested_output_evidence_source == "independent_run_evidence":
        corroborated = _has_independent_satisfied_requested_output_corroborator(verdict_by_id, criterion_id)
    else:
        corroborated = has_corroboration or _has_satisfied_requested_output_corroborator(verdict_by_id, criterion_id)
    return (
        corroborated
        and _is_structural_requested_output_abstention(verdict)
        and bool(verdict.evidence_ref)
        and bool(verdict.output_path)
        and not verdict.has_exact_value
        and verdict.grounding_mode != "judgment_boolean"
    )


def _has_satisfied_requested_output_corroborator(
    verdict_by_id: dict[str, CriterionVerdict],
    criterion_id: str,
) -> bool:
    return any(
        verdict.satisfied and _is_requested_output_corroborator_id(verdict.criterion_id, criterion_id)
        for verdict in verdict_by_id.values()
    )


def _has_independent_satisfied_requested_output_corroborator(
    verdict_by_id: dict[str, CriterionVerdict],
    criterion_id: str,
) -> bool:
    return any(
        verdict.satisfied
        and _is_requested_output_corroborator_id(verdict.criterion_id, criterion_id)
        and verdict.evidence_source in _INDEPENDENT_REQUESTED_OUTPUT_CORROBORATOR_SOURCES
        for verdict in verdict_by_id.values()
    )


def _judgment_tier_satisfied_corroborator(
    verdict_by_id: dict[str, CriterionVerdict],
    criterion_id: str,
) -> CriterionVerdict | None:
    for verdict in verdict_by_id.values():
        if (
            verdict.satisfied
            and _is_requested_output_corroborator_id(verdict.criterion_id, criterion_id)
            and verdict.evidence_source in _FLOOR_REKEYED_DELIVERABLE_CREDIT_SOURCES
        ):
            return verdict
    return None


def _is_requested_output_corroborator_id(candidate_id: str, criterion_id: str) -> bool:
    prefix = f"{criterion_id}__requested_output_corroborator"
    if candidate_id == prefix:
        return True
    if not candidate_id.startswith(f"{prefix}_"):
        return False
    return candidate_id.removeprefix(f"{prefix}_").isdigit()


_REQUESTED_OUTPUT_CORROBORATOR_MARKER = "__requested_output_corroborator"
_TASK_OUTPUT_PARAMETER_SUFFIX = "_output"
_SAME_RECORD_CORROBORATOR_MISSING_EVIDENCE = (
    "requested-output corroborator cited the criterion's own emitted output; independent corroboration is required"
)


def _requested_output_corroborator_source_id(candidate_id: str) -> str | None:
    index = candidate_id.find(_REQUESTED_OUTPUT_CORROBORATOR_MARKER)
    if index <= 0:
        return None
    suffix = candidate_id[index + len(_REQUESTED_OUTPUT_CORROBORATOR_MARKER) :]
    if suffix and not (suffix.startswith("_") and suffix[1:].isdigit()):
        return None
    return candidate_id[:index]


def _evidence_ref_record_label(evidence_ref: str | None) -> str | None:
    if not evidence_ref:
        return None
    ref = evidence_ref.strip().removeprefix("block_outputs:")
    label = ref.split(".", 1)[0].strip()
    return label or None


def _output_record_root(label: str) -> str:
    return label[: -len(_TASK_OUTPUT_PARAMETER_SUFFIX)] if label.endswith(_TASK_OUTPUT_PARAMETER_SUFFIX) else label


def _is_self_emitted_source_verdict(verdict: CriterionVerdict) -> bool:
    return verdict.evidence_source is None or verdict.evidence_source in _SELF_EMITTED_EVIDENCE_SOURCES


def _corroborator_draws_only_from_source_record(
    corroborator: CriterionVerdict,
    verdict_by_id: Mapping[str, CriterionVerdict],
) -> bool:
    source_id = _requested_output_corroborator_source_id(corroborator.criterion_id)
    source = verdict_by_id.get(source_id) if source_id else None
    if source is None or not _is_self_emitted_source_verdict(source):
        return False
    source_label = _evidence_ref_record_label(source.evidence_ref)
    corroborator_label = _evidence_ref_record_label(corroborator.evidence_ref)
    if not source_label or not corroborator_label:
        return False
    return _output_record_root(source_label) == _output_record_root(corroborator_label)


def _enforce_requested_output_corroborator_independence(
    verdicts: list[CriterionVerdict],
) -> list[CriterionVerdict]:
    verdict_by_id = {verdict.criterion_id: verdict for verdict in verdicts}
    updated: list[CriterionVerdict] = []
    for verdict in verdicts:
        if (
            verdict.satisfied
            and verdict.reason_code == "evidence_confirms"
            and _requested_output_corroborator_source_id(verdict.criterion_id) is not None
            and _corroborator_draws_only_from_source_record(verdict, verdict_by_id)
        ):
            updated.append(
                replace(
                    verdict,
                    state="unsatisfied",
                    reason_code="evidence_contradicts",
                    missing_evidence=_SAME_RECORD_CORROBORATOR_MISSING_EVIDENCE,
                )
            )
            continue
        updated.append(verdict)
    return updated


def only_structural_requested_output_abstentions(result: CompletionVerificationResult) -> bool:
    if result.status != "evaluated" or not result.criterion_ids or result.is_fully_satisfied():
        return False
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    unmet: list[CriterionVerdict] = []
    for criterion_id in result.criterion_ids:
        verdict = verdict_by_id.get(criterion_id)
        if verdict is None:
            return False
        if not verdict.satisfied:
            unmet.append(verdict)
    return bool(unmet) and all(_is_structural_requested_output_abstention(verdict) for verdict in unmet)


def _is_contingent_abstention(verdict: CriterionVerdict, abstained_criterion_ids: Collection[str]) -> bool:
    return (
        verdict.criterion_id in abstained_criterion_ids
        and not verdict.satisfied
        and verdict.reason_code in _CONTINGENT_ABSTENTION_REASON_CODES
    )


def run_plane_all_no_evidence(result: CompletionVerificationResult) -> bool:
    """Whether every run-plane verdict in an evaluated result is ``no_evidence``.

    Definition-plane verdicts are excluded: they can never be ``no_evidence`` by
    construction, so including them would permanently disarm the staleness tripwire
    for mixed sets.
    """
    if result.status != "evaluated":
        return False
    run_verdicts = [v for v in result.verdicts if not v.reason_code.startswith(_DEFINITION_REASON_PREFIX)]
    return bool(run_verdicts) and all(v.reason_code == "no_evidence" for v in run_verdicts)


def combine_verification_results(
    criterion_ids: list[str],
    run_result: CompletionVerificationResult | None,
    definition_verdicts: list[CriterionVerdict],
    *,
    contingent_criterion_ids: Iterable[str] = (),
    contingent_on_by_criterion_id: dict[str, str] | None = None,
    contingent_antecedent_output_path_by_criterion_id: dict[str, str] | None = None,
    structural_unfired_criterion_ids: Iterable[str] = (),
    requested_output_criteria_count: int = 0,
) -> CompletionVerificationResult:
    """One result spanning both evidence planes; a judge that could not evaluate
    keeps the whole result unavailable so fail-closed messaging is preserved."""
    contingent_ids = list(contingent_criterion_ids)
    contingent_on_by_id = dict(contingent_on_by_criterion_id or {})
    contingent_path_by_id = dict(contingent_antecedent_output_path_by_criterion_id or {})
    structural_unfired_ids = list(structural_unfired_criterion_ids)
    degraded_ids: list[str] = []
    floor_rekeyed_ids: list[str] = []
    floor_rekeyed_path_by_id: dict[str, str] = {}
    floor_rekeyed_backed_by_id: dict[str, bool] = {}
    contingent_degraded_ids: list[str] = []
    if run_result is not None:
        contingent_ids = list(dict.fromkeys([*contingent_ids, *run_result.contingent_criterion_ids]))
        contingent_on_by_id.update(run_result.contingent_on_by_criterion_id)
        contingent_path_by_id.update(run_result.contingent_antecedent_output_path_by_criterion_id)
        structural_unfired_ids = list(
            dict.fromkeys([*structural_unfired_ids, *run_result.structural_unfired_criterion_ids])
        )
        degraded_ids = list(dict.fromkeys([*degraded_ids, *run_result.degraded_criterion_ids]))
        floor_rekeyed_ids = list(dict.fromkeys([*floor_rekeyed_ids, *run_result.floor_rekeyed_criterion_ids]))
        floor_rekeyed_path_by_id.update(run_result.floor_rekeyed_output_path_by_criterion_id)
        floor_rekeyed_backed_by_id.update(run_result.floor_rekeyed_backed_by_criterion_id)
        contingent_degraded_ids = list(
            dict.fromkeys([*contingent_degraded_ids, *run_result.contingent_degraded_criterion_ids])
        )
    if run_result is not None and run_result.status != "evaluated":
        return CompletionVerificationResult(
            status=run_result.status,
            criterion_ids=list(criterion_ids),
            verdicts=list(run_result.verdicts),
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            structural_unfired_criterion_ids=structural_unfired_ids,
            degraded_criterion_ids=degraded_ids,
            floor_rekeyed_criterion_ids=floor_rekeyed_ids,
            floor_rekeyed_output_path_by_criterion_id=floor_rekeyed_path_by_id,
            floor_rekeyed_backed_by_criterion_id=floor_rekeyed_backed_by_id,
            contingent_degraded_criterion_ids=contingent_degraded_ids,
            requested_output_criteria_count=requested_output_criteria_count,
        )
    if run_result is not None:
        run_result = replace(
            run_result,
            verdicts=_enforce_requested_output_corroborator_independence(run_result.verdicts),
        )
    verdict_by_id = {verdict.criterion_id: verdict for verdict in definition_verdicts}
    if run_result is not None:
        verdict_by_id.update({verdict.criterion_id: verdict for verdict in run_result.verdicts})
    verdicts = [
        verdict_by_id.get(cid, CriterionVerdict(criterion_id=cid, state="unknown", reason_code="unknown"))
        for cid in criterion_ids
    ]
    if run_result is not None:
        verdicts.extend(
            verdict
            for verdict in run_result.verdicts
            if verdict.criterion_id not in criterion_ids and _is_satisfied_terminal_record_verdict(verdict)
        )
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=list(criterion_ids),
        verdicts=verdicts,
        no_gradeable_run_plane=run_result is None,
        contingent_criterion_ids=contingent_ids,
        contingent_on_by_criterion_id=contingent_on_by_id,
        contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
        structural_unfired_criterion_ids=structural_unfired_ids,
        degraded_criterion_ids=degraded_ids,
        floor_rekeyed_criterion_ids=floor_rekeyed_ids,
        floor_rekeyed_output_path_by_criterion_id=floor_rekeyed_path_by_id,
        contingent_degraded_criterion_ids=contingent_degraded_ids,
        requested_output_criteria_count=requested_output_criteria_count,
    )


def degraded_lane_criterion_ids(result: CompletionVerificationResult) -> set[str]:
    return {*result.degraded_criterion_ids, *result.contingent_degraded_criterion_ids}


def only_degraded_blocking(result: CompletionVerificationResult) -> bool:
    """True only when every criterion that still blocks full satisfaction is a degraded
    criterion. A genuine unsatisfied/unknown non-degraded criterion, an empty blocking set,
    or no degraded ids all return False, so this never masks a real runtime failure or a
    mixed degraded+legitimate result."""
    if result.status != "evaluated":
        return False
    degraded = degraded_lane_criterion_ids(result)
    if not degraded:
        return False
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    blocking: set[str] = set()
    for criterion_id in result.criterion_ids:
        verdict = verdict_by_id.get(criterion_id)
        if verdict is not None and verdict.satisfied:
            continue
        if verdict is not None and _is_definition_plane_abstention(verdict):
            continue
        if verdict is not None and _is_incomplete_validation_classification_abstention(verdict):
            continue
        if verdict is not None and result.is_structural_contingent_abstention(verdict):
            continue
        blocking.add(criterion_id)
    if not blocking:
        return False
    return blocking <= degraded


def _delivered_unverified_observed_verdict(verdict: CriterionVerdict) -> bool:
    if not verdict.evidence_ref or not verdict.output_path:
        return False
    normalized_path = verdict.output_path.removeprefix("output.").strip()
    if normalized_path.split(".")[-1] == "evidence_text":
        return False
    if verdict.evidence_source not in _DELIVERED_UNVERIFIED_OUTPUT_SOURCES:
        return False
    if verdict.self_emitted_judgment_not_independent:
        return False
    if verdict.requested_output_evidence_source == "independent_run_evidence":
        return False
    if verdict.grounding_mode in {"shape", "judgment_boolean"}:
        return False
    if verdict.satisfied:
        return (
            verdict.reason_code == "evidence_confirms"
            and verdict.grounding_mode == "exact_value"
            and verdict.has_exact_value
        )
    return _is_structural_requested_output_abstention(verdict) and not verdict.has_exact_value


def degraded_contract_delivered_unverified_terminal_state(
    result: CompletionVerificationResult | None,
    *,
    run_ok: bool,
    workflow_run_id: str | None,
    latest_workflow_run_id: str | None,
    artifact_health_blocked: bool,
    terminal_blocked: bool,
) -> DeliveredUnverifiedTerminalState | None:
    if (
        result is None
        or result.status != "evaluated"
        or result.is_fully_satisfied()
        or not run_ok
        or not workflow_run_id
        or workflow_run_id != latest_workflow_run_id
        or artifact_health_blocked
        or terminal_blocked
    ):
        return None
    degraded = degraded_lane_criterion_ids(result)
    if not degraded:
        return None
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    observed: list[CriterionVerdict] = []
    blocking: set[str] = set()
    for criterion_id in result.criterion_ids:
        verdict = verdict_by_id.get(criterion_id)
        if verdict is not None and _delivered_unverified_observed_verdict(verdict):
            observed.append(verdict)
            continue
        if verdict is not None and verdict.satisfied:
            continue
        if verdict is not None and result.is_structural_contingent_abstention(verdict):
            continue
        blocking.add(criterion_id)
    if not observed or not blocking or not blocking <= degraded:
        return None
    return DeliveredUnverifiedTerminalState(observed_verdicts=tuple(observed))


def zero_requested_output_criteria_credit(
    result: CompletionVerificationResult | None,
    *,
    has_meaningful_registered_output: bool,
) -> bool:
    """A satisfied run-plane verdict crediting completion when no requested-output
    criterion formed to grade the delivered payload is an unverified deliverable,
    not a verified success. Keying on meaningful produced content (not mere payload
    presence) lets a reach-state goal whose only registered output is an empty
    task-output envelope (e.g. login-only) keep crediting normally."""
    return (
        result is not None
        and result.status == "evaluated"
        and result.requested_output_criteria_count == 0
        and has_meaningful_registered_output
        and result.is_fully_satisfied()
    )


@dataclass(frozen=True)
class FloorRekeyedDeliverableCredit:
    criterion_ids: tuple[str, ...]
    evidence_sources: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    output_paths: tuple[str, ...]


def floor_rekeyed_deliverable_credit(
    result: CompletionVerificationResult | None,
) -> FloorRekeyedDeliverableCredit | None:
    """Credit a floor-rekeyed presence-only deliverable on a fully satisfied run when every marked
    criterion carries its own satisfied requested-output corroborator whose derived evidence_source is
    judgment-tier. Registered-output and self-emitted corroboration earn no credit and keep hedging."""
    if result is None or result.status != "evaluated" or not result.is_fully_satisfied():
        return None
    marked_ids = list(dict.fromkeys(result.floor_rekeyed_criterion_ids))
    if not marked_ids:
        return None
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    evidence_sources: list[str] = []
    evidence_refs: list[str] = []
    output_paths: list[str] = []
    for criterion_id in marked_ids:
        marked = verdict_by_id.get(criterion_id)
        if marked is None:
            return None
        corroborator = _judgment_tier_satisfied_corroborator(verdict_by_id, criterion_id)
        if corroborator is None or corroborator.evidence_source is None:
            return None
        evidence_sources.append(corroborator.evidence_source)
        evidence_refs.append(corroborator.evidence_ref or "")
        output_paths.append(result.floor_rekeyed_output_path_by_criterion_id.get(criterion_id, ""))
    return FloorRekeyedDeliverableCredit(
        criterion_ids=tuple(marked_ids),
        evidence_sources=tuple(evidence_sources),
        evidence_refs=tuple(evidence_refs),
        output_paths=tuple(output_paths),
    )


def carry_degraded_criterion_ids(
    result: CompletionVerificationResult,
    criteria: Iterable[CompletionCriterion],
) -> CompletionVerificationResult:
    criteria = list(criteria)
    degraded = [criterion.id for criterion in criteria if is_turn_unsatisfiable_fallback_degraded(criterion)]
    contingent_degraded = [
        criterion.id for criterion in criteria if is_contingent_missing_antecedent_degraded(criterion)
    ]
    if (
        not degraded
        and not contingent_degraded
        and not result.degraded_criterion_ids
        and not result.contingent_degraded_criterion_ids
    ):
        return result
    return replace(
        result,
        degraded_criterion_ids=list(dict.fromkeys([*result.degraded_criterion_ids, *degraded])),
        contingent_degraded_criterion_ids=list(
            dict.fromkeys([*result.contingent_degraded_criterion_ids, *contingent_degraded])
        ),
    )


def carry_floor_rekeyed_criterion_ids(
    result: CompletionVerificationResult,
    criteria: Iterable[CompletionCriterion],
) -> CompletionVerificationResult:
    marked = [criterion for criterion in criteria if criterion.requested_output_floor_rekeyed]
    if not marked and not result.floor_rekeyed_criterion_ids:
        return result
    merged = list(dict.fromkeys([*result.floor_rekeyed_criterion_ids, *(criterion.id for criterion in marked)]))
    output_path_by_id = dict(result.floor_rekeyed_output_path_by_criterion_id)
    for criterion in marked:
        if criterion.floor_rekeyed_from_path:
            output_path_by_id.setdefault(criterion.id, criterion.floor_rekeyed_from_path)
    return replace(
        result,
        floor_rekeyed_criterion_ids=merged,
        floor_rekeyed_output_path_by_criterion_id=output_path_by_id,
    )


def carry_floor_rekeyed_path_backing(
    result: CompletionVerificationResult,
    backing_by_criterion_id: Mapping[str, bool],
) -> CompletionVerificationResult:
    if not backing_by_criterion_id and not result.floor_rekeyed_backed_by_criterion_id:
        return result
    merged = dict(result.floor_rekeyed_backed_by_criterion_id)
    merged.update(backing_by_criterion_id)
    return replace(result, floor_rekeyed_backed_by_criterion_id=merged)


@dataclass(frozen=True)
class FloorRekeyedEmissionWithhold:
    criterion_ids: tuple[str, ...]
    unbacked_output_paths: tuple[str, ...]
    backed_output_paths: tuple[str, ...]


def floor_rekeyed_effective_marked_ids(result: CompletionVerificationResult) -> list[str]:
    """Floor-rekeyed markers that required an emission this run: the marked set minus the
    structurally-unfired contingent (abstained) markers, which needed no emission to fire."""
    abstained = result.abstained_criterion_ids()
    return [criterion_id for criterion_id in result.floor_rekeyed_criterion_ids if criterion_id not in abstained]


def floor_rekeyed_emission_withhold(
    result: CompletionVerificationResult | None,
) -> FloorRekeyedEmissionWithhold | None:
    """A fully satisfied run whose floor-rekeyed emission markers lack a meaningful runtime value at
    their original path delivered no emission that corroboration can substitute for. Keyed on the typed
    marker id-set (never payload emptiness) so never-minted reach-state and structurally-unfired
    contingent markers are untouched, and a missing backing entry reads as unbacked to fail closed."""
    if result is None or result.status != "evaluated" or not result.is_fully_satisfied():
        return None
    effective_ids = floor_rekeyed_effective_marked_ids(result)
    if not effective_ids:
        return None
    unbacked_ids: list[str] = []
    unbacked_paths: list[str] = []
    backed_paths: list[str] = []
    for criterion_id in effective_ids:
        path = result.floor_rekeyed_output_path_by_criterion_id.get(criterion_id, "")
        if result.floor_rekeyed_backed_by_criterion_id.get(criterion_id, False):
            backed_paths.append(path)
        else:
            unbacked_ids.append(criterion_id)
            unbacked_paths.append(path)
    if not unbacked_ids:
        return None
    return FloorRekeyedEmissionWithhold(
        criterion_ids=tuple(unbacked_ids),
        unbacked_output_paths=tuple(unbacked_paths),
        backed_output_paths=tuple(backed_paths),
    )


def floor_rekeyed_emission_lane_fields(result: CompletionVerificationResult | None) -> dict[str, Any] | None:
    """Structured fingerprint fields for the floor-rekeyed emission lane, emitted whenever an
    evaluated result carries markers — on satisfied and unsatisfied results alike, independent of
    outcome."""
    if result is None or result.status != "evaluated" or not result.floor_rekeyed_criterion_ids:
        return None
    effective_ids = floor_rekeyed_effective_marked_ids(result)
    backed_ids = [cid for cid in effective_ids if result.floor_rekeyed_backed_by_criterion_id.get(cid, False)]
    unbacked_ids = [cid for cid in effective_ids if cid not in backed_ids]
    withhold = floor_rekeyed_emission_withhold(result)
    return {
        "criterion_ids": list(result.floor_rekeyed_criterion_ids),
        "effective_criterion_ids": list(effective_ids),
        "abstained_excluded_criterion_ids": sorted(set(result.floor_rekeyed_criterion_ids) - set(effective_ids)),
        "backed_criterion_ids": backed_ids,
        "unbacked_criterion_ids": unbacked_ids,
        "backed_output_paths": [result.floor_rekeyed_output_path_by_criterion_id.get(cid, "") for cid in backed_ids],
        "unbacked_output_paths": [
            result.floor_rekeyed_output_path_by_criterion_id.get(cid, "") for cid in unbacked_ids
        ],
        "engaged": withhold is not None,
    }


async def evaluate_completion_criteria(
    criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
    handler: Any,
) -> CompletionVerificationResult:
    if handler is None or not criteria:
        if not criteria:
            return _UNAVAILABLE
        contingent_ids, contingent_on_by_id, contingent_path_by_id = _contingent_metadata_for_criteria(criteria)
        return CompletionVerificationResult(
            status="unavailable",
            criterion_ids=[criterion.id for criterion in criteria],
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
        )

    criterion_ids = [criterion.id for criterion in criteria]
    contingent_ids, contingent_on_by_id, contingent_path_by_id = _contingent_metadata_for_criteria(criteria)
    structural_unfired_ids = structural_unfired_contingent_criterion_ids(criteria, snapshot)
    prompt = prompt_engine.load_prompt(
        template=PROMPT_TEMPLATE_NAME,
        criteria=escape_code_fences(_render_criteria(criteria)),
        run_evidence=escape_code_fences(snapshot.render_prompt_block()),
    )
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=PROMPT_TEMPLATE_NAME),
            timeout=settings.COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning("completion-verification judge timed out")
        return CompletionVerificationResult(
            status="unavailable",
            criterion_ids=criterion_ids,
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            structural_unfired_criterion_ids=structural_unfired_ids,
        )
    except Exception as exc:
        LOG.warning("completion-verification judge failed", error=str(exc))
        return CompletionVerificationResult(
            status="unavailable",
            criterion_ids=criterion_ids,
            contingent_criterion_ids=contingent_ids,
            contingent_on_by_criterion_id=contingent_on_by_id,
            contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
            structural_unfired_criterion_ids=structural_unfired_ids,
        )

    return _coerce_result(
        raw,
        criterion_ids,
        contingent_criterion_ids=contingent_ids,
        contingent_on_by_criterion_id=contingent_on_by_id,
        contingent_antecedent_output_path_by_criterion_id=contingent_path_by_id,
        structural_unfired_criterion_ids=structural_unfired_ids,
        block_output_sources=snapshot.block_output_sources,
    )
