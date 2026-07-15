"""Typed per-run outcome verdict recorded at the run-result seam; rendering
surfaces consume it instead of re-deriving success from raw block status."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from skyvern.forge.sdk.copilot.challenge_evidence import is_carrier_backed_category_entry
from skyvern.forge.sdk.copilot.failure_tracking import ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE, url_origin

RunOutcomeVerdict = Literal["evaluating", "demonstrated", "not_demonstrated", "not_evaluated"]
RecordedRunOutcomeVerdict = Literal["demonstrated", "not_demonstrated", "not_evaluated"]
RunOutcomeReasonCode = Literal[
    "blocker_reported",
    "terminal_challenge_blocker",
    "no_meaningful_output",
    "outcome_not_demonstrated",
]

TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE: RunOutcomeReasonCode = "terminal_challenge_blocker"
TERMINAL_CHALLENGE_BLOCKER_REASON_CODE = "tool_error_terminal_challenge_blocker"
# Alias the root-cause classifier set so newly added anti-bot challenge aliases
# automatically participate in the terminal-challenge gate.
TERMINAL_CHALLENGE_FAILURE_CATEGORIES = ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES
TERMINAL_CHALLENGE_FAILURE_CATEGORY_MIN_CONFIDENCE = 0.7
TERMINAL_CHALLENGE_USER_FACING_REASON = (
    "The page is gated by a site verification challenge, so I stopped instead of retrying the same path. "
    "The draft workflow is preserved, but it is not verified end-to-end."
)

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
    reason = redact_raw_secrets_for_prompt(" ".join(text.split()))
    reason = URL_CANDIDATE_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", reason)
    return reason[:_DISPLAY_REASON_MAX_CHARS]


def trusted_terminal_challenge_category_name(entry: Mapping[str, Any]) -> str | None:
    category = entry.get("category")
    if not isinstance(category, str) or category not in TERMINAL_CHALLENGE_FAILURE_CATEGORIES:
        return None
    if not is_carrier_backed_category_entry(entry):
        return None
    confidence = entry.get("confidence_float")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        numeric_confidence = float(confidence)
        if (
            not math.isfinite(numeric_confidence)
            or numeric_confidence < TERMINAL_CHALLENGE_FAILURE_CATEGORY_MIN_CONFIDENCE
        ):
            return None
    return category
