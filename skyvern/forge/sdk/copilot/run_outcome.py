"""Typed per-run facts recorded at the run-result seam."""

from __future__ import annotations

import json
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
RunOutcomeRole = Literal["recorded", "adjudicated", "interim_build_test"]
RunOutcomeReasonCode = Literal[
    "blocker_reported",
    "terminal_challenge_blocker",
    "no_meaningful_output",
]

TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE: RunOutcomeReasonCode = "terminal_challenge_blocker"
# Alias the root-cause classifier set so newly added anti-bot challenge aliases
# automatically participate in the terminal-challenge gate.
TERMINAL_CHALLENGE_FAILURE_CATEGORIES = ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES
TERMINAL_CHALLENGE_FAILURE_CATEGORY_MIN_CONFIDENCE = 0.7


_DISPLAY_REASON_MAX_CHARS = 160
_OUTPUT_REPORT_MAX_CHARS = 1200
_OUTPUT_REPORT_LABEL = "Recorded output from the latest completed run:"
_REDACTED_SECRET = "[REDACTED_SECRET]"


@dataclass(frozen=True)
class RecordedRunOutcome:
    verdict: RecordedRunOutcomeVerdict
    reason_code: RunOutcomeReasonCode | None = None
    display_reason: str | None = None
    workflow_run_id: str | None = None
    output_report: str | None = None
    # Recorded lifecycle, kept apart from ``verdict``: reaching a completed status says
    # nothing about whether the outcome was evaluated. ``None`` on frames predating the field.
    run_completed: bool | None = None
    # ``adjudicated`` remains accepted for persisted legacy frames. New
    # interactive authoring frames are factual records.
    role: RunOutcomeRole = "recorded"


def run_outcome_display_reason(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    reason = redact_raw_secrets_for_prompt(" ".join(text.split()))
    reason = URL_CANDIDATE_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", reason)
    return reason[:_DISPLAY_REASON_MAX_CHARS]


def _registered_output_key_is_secret(key: object) -> bool:
    if not isinstance(key, str) or not key:
        return False
    probe = f"{key}=value"
    return redact_raw_secrets_for_prompt(probe) != probe


def _redact_registered_output(value: Any, *, key: object = None) -> Any:
    if _registered_output_key_is_secret(key):
        return _REDACTED_SECRET
    if isinstance(value, Mapping):
        return {item_key: _redact_registered_output(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_registered_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_registered_output(item) for item in value)
    if isinstance(value, str):
        return redact_raw_secrets_for_prompt(value)
    return value


def recorded_output_report(payloads: object) -> str | None:
    """Render the current run's already-sanitized registered outputs as a factual terminal line."""
    if not isinstance(payloads, list):
        return None
    outputs: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        key = payload.get("output_parameter_key")
        if not isinstance(key, str) or not key.strip() or "value" not in payload:
            continue
        normalized_key = key.strip()
        outputs[normalized_key] = _redact_registered_output(payload.get("value"), key=normalized_key)
    if not outputs:
        return None
    try:
        serialized = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return None
    serialized = URL_CANDIDATE_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", serialized)
    report = f"{_OUTPUT_REPORT_LABEL} {redact_raw_secrets_for_prompt(serialized)}"
    if len(report) > _OUTPUT_REPORT_MAX_CHARS:
        report = report[: _OUTPUT_REPORT_MAX_CHARS - 3].rstrip() + "..."
    return report


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
