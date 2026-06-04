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
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

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

VerificationStatus = Literal["evaluated", "unavailable"]


@dataclass(frozen=True)
class CriterionVerdict:
    criterion_id: str
    satisfied: bool
    reason_code: str
    evidence_ref: str | None = None


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
            "satisfied_count": sum(1 for verdict in self.verdicts if verdict.satisfied),
            "fully_satisfied": self.is_fully_satisfied(),
            "reason_codes": [verdict.reason_code for verdict in self.verdicts],
        }


@dataclass(frozen=True)
class RunEvidenceSnapshot:
    workflow_run_id: str | None = None
    block_outputs: dict[str, Any] = field(default_factory=dict)
    current_url: str | None = None
    page_title: str | None = None
    executed_block_labels: list[str] = field(default_factory=list)
    verified_context_block_labels: list[str] = field(default_factory=list)

    def has_evidence(self) -> bool:
        return bool(self.block_outputs or self.current_url or self.page_title)

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
        # evidence; an "unknown"/"no_evidence"/"contradicts" verdict never passes.
        satisfied = bool(item.get("satisfied")) and reason_code == "evidence_confirms"
        evidence_ref_raw = item.get("evidence_ref")
        evidence_ref = (
            evidence_ref_raw.strip() if isinstance(evidence_ref_raw, str) and evidence_ref_raw.strip() else None
        )
        by_id[criterion_id] = CriterionVerdict(
            criterion_id=criterion_id,
            satisfied=satisfied,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )

    verdicts = [
        by_id.get(criterion_id, CriterionVerdict(criterion_id=criterion_id, satisfied=False, reason_code="no_evidence"))
        for criterion_id in criterion_ids
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
