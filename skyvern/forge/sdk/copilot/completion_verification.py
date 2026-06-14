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
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
import yaml

from skyvern.config import settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, redact_raw_secrets_for_prompt
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()

PROMPT_TEMPLATE_NAME = "workflow-copilot-completion-verification"
_EVIDENCE_VALUE_MAX_CHARS = 2000
_MAX_BLOCK_OUTPUTS = 20
_REASON_CODES = frozenset({"evidence_confirms", "no_evidence", "evidence_contradicts", "unknown"})
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


def resolve_unknown(state: CriterionState) -> CriterionState:
    """Effective verdict state for consumers: with persisted-criteria off, ``unknown``
    collapses to ``unsatisfied`` (the pre-tri-state equivalent of a missing verdict)."""
    if state == "unknown" and not settings.COPILOT_PERSISTED_COMPLETION_CRITERIA_ENABLED:
        return "unsatisfied"
    return state


@dataclass(frozen=True)
class CriterionVerdict:
    criterion_id: str
    state: CriterionState
    reason_code: str
    evidence_ref: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.state == "satisfied"


@dataclass(frozen=True)
class CompletionVerificationResult:
    status: VerificationStatus
    criterion_ids: list[str] = field(default_factory=list)
    verdicts: list[CriterionVerdict] = field(default_factory=list)

    def is_fully_satisfied(self) -> bool:
        if self.status != "evaluated" or not self.criterion_ids:
            return False
        satisfied = {verdict.criterion_id for verdict in self.verdicts if verdict.satisfied}
        return all(criterion_id in satisfied for criterion_id in self.criterion_ids)

    def to_trace_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "criterion_count": len(self.criterion_ids),
            "satisfied_count": sum(1 for verdict in self.verdicts if verdict.state == "satisfied"),
            "unsatisfied_count": sum(1 for verdict in self.verdicts if verdict.state == "unsatisfied"),
            "unknown_count": sum(1 for verdict in self.verdicts if verdict.state == "unknown"),
            "fully_satisfied": self.is_fully_satisfied(),
            "reason_codes": [verdict.reason_code for verdict in self.verdicts],
        }

    def verdict_state_counts(self) -> dict[str, int]:
        return {
            "satisfied": sum(1 for verdict in self.verdicts if verdict.state == "satisfied"),
            "unsatisfied": sum(1 for verdict in self.verdicts if verdict.state == "unsatisfied"),
            "unknown": sum(1 for verdict in self.verdicts if verdict.state == "unknown"),
        }


@dataclass(frozen=True)
class RunEvidenceSnapshot:
    workflow_run_id: str | None = None
    block_outputs: dict[str, Any] = field(default_factory=dict)
    current_url: str | None = None
    page_title: str | None = None
    executed_block_labels: list[str] = field(default_factory=list)
    verified_context_block_labels: list[str] = field(default_factory=list)
    page_evidence: dict[str, Any] = field(default_factory=dict)

    def has_evidence(self) -> bool:
        return bool(self.block_outputs or self.current_url or self.page_title or self.page_evidence)

    def render_prompt_block(self) -> str:
        lines: list[str] = []
        if self.workflow_run_id:
            lines.append(f"workflow_run_id: {self.workflow_run_id}")
        if self.current_url:
            lines.append(f"observed_end_state_url: {self.current_url}")
        if self.page_title:
            lines.append(f"observed_end_state_page_title: {self.page_title}")
        if self.verified_context_block_labels:
            lines.append(
                "verified_context_block_labels: " + ", ".join(self.verified_context_block_labels[:_MAX_BLOCK_OUTPUTS])
            )
        if self.executed_block_labels:
            lines.append("executed_block_labels: " + ", ".join(self.executed_block_labels[:_MAX_BLOCK_OUTPUTS]))
        if self.block_outputs:
            lines.append("produced_block_outputs:")
            for label, payload in list(self.block_outputs.items())[:_MAX_BLOCK_OUTPUTS]:
                serialized = payload if isinstance(payload, str) else json.dumps(payload, default=str)
                serialized = " ".join(serialized.split())[:_EVIDENCE_VALUE_MAX_CHARS]
                lines.append(f"  - {label}: {serialized}")
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
        parts.append(f"- {criterion.id}: {criterion.outcome}{flags}")
    return "\n".join(parts)


def _coerce_result(raw: Any, criterion_ids: list[str]) -> CompletionVerificationResult:
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
        reason_code = reason if reason in _REASON_CODES else "unknown"
        # A criterion counts as satisfied only when the judge cites confirming
        # evidence; "no_evidence"/"contradicts" are affirmative negatives, while an
        # incoherent or "unknown" verdict stays unknown — it never passes and never fails.
        if bool(item.get("satisfied")) and reason_code == "evidence_confirms":
            state: CriterionState = "satisfied"
        elif reason_code in ("no_evidence", "evidence_contradicts"):
            state = "unsatisfied"
        else:
            state = "unknown"
        evidence_ref_raw = item.get("evidence_ref")
        evidence_ref = (
            evidence_ref_raw.strip() if isinstance(evidence_ref_raw, str) and evidence_ref_raw.strip() else None
        )
        by_id[criterion_id] = CriterionVerdict(
            criterion_id=criterion_id,
            state=state,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )

    # A criterion the judge never returned a verdict for was not evaluated —
    # that is absent signal (unknown), not affirmative no_evidence.
    verdicts = [
        by_id.get(criterion_id, CriterionVerdict(criterion_id=criterion_id, state="unknown", reason_code="unknown"))
        for criterion_id in criterion_ids
    ]
    return CompletionVerificationResult(status="evaluated", criterion_ids=list(criterion_ids), verdicts=verdicts)


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
        if defined and referenced:
            # When the criterion names specific inputs, each must match a defined
            # parameter key; one stray parameter must not satisfy a multi-input ask.
            # An unmatchable name degrades to unknown, not unsatisfied — extraction
            # is heuristic and a false negative would drive repair of correct YAML.
            named = _named_definition_inputs(criterion.outcome)
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


_DEFINITION_REASON_PREFIX = "definition_"


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
) -> CompletionVerificationResult:
    """One result spanning both evidence planes; a judge that could not evaluate
    keeps the whole result unavailable so fail-closed messaging is preserved."""
    if run_result is not None and run_result.status != "evaluated":
        return run_result
    verdict_by_id = {verdict.criterion_id: verdict for verdict in definition_verdicts}
    if run_result is not None:
        verdict_by_id.update({verdict.criterion_id: verdict for verdict in run_result.verdicts})
    verdicts = [
        verdict_by_id.get(cid, CriterionVerdict(criterion_id=cid, state="unknown", reason_code="unknown"))
        for cid in criterion_ids
    ]
    return CompletionVerificationResult(status="evaluated", criterion_ids=list(criterion_ids), verdicts=verdicts)


async def evaluate_completion_criteria(
    criteria: list[CompletionCriterion],
    snapshot: RunEvidenceSnapshot,
    handler: Any,
) -> CompletionVerificationResult:
    if handler is None or not criteria:
        return _UNAVAILABLE

    criterion_ids = [criterion.id for criterion in criteria]
    prompt = prompt_engine.load_prompt(
        template=PROMPT_TEMPLATE_NAME,
        criteria=escape_code_fences(_render_criteria(criteria)),
        run_evidence=escape_code_fences(snapshot.render_prompt_block()),
    )
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=PROMPT_TEMPLATE_NAME),
            timeout=settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning("completion-verification judge timed out")
        return _UNAVAILABLE
    except Exception as exc:
        LOG.warning("completion-verification judge failed", error=str(exc))
        return _UNAVAILABLE

    return _coerce_result(raw, criterion_ids)
