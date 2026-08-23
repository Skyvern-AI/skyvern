"""Typed per-run facts recorded at the run-result seam."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from skyvern.forge.sdk.copilot.challenge_evidence import ChallengeKind, is_carrier_backed_category_entry
from skyvern.forge.sdk.copilot.failure_tracking import ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE, url_origin

RunOutcomeVerdict = Literal["evaluating", "demonstrated", "not_demonstrated", "not_evaluated"]
RecordedRunOutcomeVerdict = Literal["demonstrated", "not_demonstrated", "not_evaluated"]
RunOutcomeRole = Literal["recorded", "adjudicated", "interim_build_test"]
RunOutcomeReasonCode = Literal[
    "blocker_reported",
    "terminal_challenge_blocker",
    "device_approval_challenge_blocker",
    "no_meaningful_output",
]

TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE: RunOutcomeReasonCode = "terminal_challenge_blocker"
DEVICE_APPROVAL_RUN_OUTCOME_REASON_CODE: RunOutcomeReasonCode = "device_approval_challenge_blocker"
TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODES: frozenset[RunOutcomeReasonCode] = frozenset(
    {TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE, DEVICE_APPROVAL_RUN_OUTCOME_REASON_CODE}
)
TERMINAL_CHALLENGE_BLOCKER_REASON_CODE = "tool_error_terminal_challenge_blocker"
DEVICE_APPROVAL_BLOCKER_REASON_CODE = "tool_error_device_approval_challenge_blocker"
TERMINAL_CHALLENGE_BLOCKER_REASON_CODES: frozenset[str] = frozenset(
    {TERMINAL_CHALLENGE_BLOCKER_REASON_CODE, DEVICE_APPROVAL_BLOCKER_REASON_CODE}
)
# Alias the root-cause classifier set so newly added anti-bot challenge aliases
# automatically participate in the terminal-challenge gate.
TERMINAL_CHALLENGE_FAILURE_CATEGORIES = ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES
TERMINAL_CHALLENGE_FAILURE_CATEGORY_MIN_CONFIDENCE = 0.7
_DRAFT_PRESERVED_CLAUSE = "The draft workflow is preserved, but it is not verified end-to-end."
_RESTRAINT_CLAUSE = ", so I stopped instead of retrying the same path"
_SITE_VERIFICATION_WALL = "The page is gated by a site verification challenge"
_DEVICE_APPROVAL_WALL = (
    "This sign-in is gated by a device-approval step that a person has to approve on a separate device"
)
_DEVICE_APPROVAL_FRESH_SESSION_CLAUSE = (
    ", and the test run started in a fresh browser session, so that approval can never complete inside it"
)
_DEVICE_APPROVAL_STRATEGY = (
    "To get past it, re-use an already-approved browser session or profile for the run, leave the login step "
    "out of what the test run replays, or run it with you present to approve the prompt."
)


def _challenge_reason(wall: str, *, restrained: bool, strategy: str = "") -> str:
    claim = f"{wall}{_RESTRAINT_CLAUSE}." if restrained else f"{wall}."
    return " ".join(part for part in (claim, _DRAFT_PRESERVED_CLAUSE, strategy) if part)


TERMINAL_CHALLENGE_USER_FACING_REASON = _challenge_reason(_SITE_VERIFICATION_WALL, restrained=True)

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
    # ``adjudicated`` remains accepted for persisted legacy frames. New
    # interactive authoring frames are factual records.
    role: RunOutcomeRole = "recorded"


@dataclass(frozen=True)
class TerminalChallengeDisposition:
    internal_reason_code: str
    run_outcome_reason_code: RunOutcomeReasonCode
    user_facing_reason: str
    challenge_kind: ChallengeKind | None


def terminal_challenge_disposition(
    *,
    challenge_kind: ChallengeKind | None,
    runs_this_turn: int | None = None,
    used_fresh_run_session: bool | None = None,
) -> TerminalChallengeDisposition:
    """Discriminate one terminal challenge halt: an unclassified kind keeps today's site-verification
    wording, so a captcha or an unrecognized wall degrades to the current behavior rather than borrowing
    the device-approval claim. The fresh-session cause is named only when the run fact affirmatively
    reports one, so an unknown or reused session never has it asserted for it."""
    restrained = runs_this_turn is None or runs_this_turn <= 1
    if challenge_kind is ChallengeKind.DEVICE_APPROVAL:
        wall = _DEVICE_APPROVAL_WALL + (_DEVICE_APPROVAL_FRESH_SESSION_CLAUSE if used_fresh_run_session else "")
        return TerminalChallengeDisposition(
            internal_reason_code=DEVICE_APPROVAL_BLOCKER_REASON_CODE,
            run_outcome_reason_code=DEVICE_APPROVAL_RUN_OUTCOME_REASON_CODE,
            user_facing_reason=_challenge_reason(wall, restrained=restrained, strategy=_DEVICE_APPROVAL_STRATEGY),
            challenge_kind=challenge_kind,
        )
    return TerminalChallengeDisposition(
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        run_outcome_reason_code=TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
        user_facing_reason=_challenge_reason(_SITE_VERIFICATION_WALL, restrained=restrained),
        challenge_kind=challenge_kind,
    )


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
