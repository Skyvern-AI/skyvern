"""Typed per-run outcome verdict recorded at the run-result seam; rendering
surfaces consume it instead of re-deriving success from raw block status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunOutcomeVerdict = Literal["evaluating", "demonstrated", "not_demonstrated", "not_evaluated"]
RecordedRunOutcomeVerdict = Literal["demonstrated", "not_demonstrated", "not_evaluated"]
RunOutcomeReasonCode = Literal["blocker_reported", "no_meaningful_output", "outcome_not_demonstrated"]

_DISPLAY_REASON_MAX_CHARS = 160


@dataclass(frozen=True)
class RecordedRunOutcome:
    verdict: RecordedRunOutcomeVerdict
    reason_code: RunOutcomeReasonCode | None = None
    display_reason: str | None = None
    workflow_run_id: str | None = None


def run_outcome_display_reason(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return " ".join(text.split())[:_DISPLAY_REASON_MAX_CHARS]
