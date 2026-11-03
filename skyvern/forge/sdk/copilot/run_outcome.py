"""Typed per-run facts recorded at the run-result seam."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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


_INTERIM_RUN_OUTCOME_ROLE: RunOutcomeRole = "interim_build_test"

_DISPLAY_REASON_MAX_CHARS = 160
_DISPLAY_REASON_ELISION = "..."


@dataclass(frozen=True)
class RecordedRunOutcome:
    verdict: RecordedRunOutcomeVerdict
    reason_code: RunOutcomeReasonCode | None = None
    display_reason: str | None = None
    workflow_run_id: str | None = None
    # Recorded lifecycle, kept apart from ``verdict``: reaching a completed status says
    # nothing about whether the outcome was evaluated. ``None`` on frames predating the field.
    run_completed: bool | None = None
    # ``adjudicated`` remains accepted for persisted legacy frames. New
    # interactive authoring frames are factual records.
    role: RunOutcomeRole = "recorded"


def _net_error_spans(text: str) -> list[tuple[int, int]]:
    prefix = "net::ERR_"
    spans: list[tuple[int, int]] = []
    search_from = 0
    while (start := text.find(prefix, search_from)) >= 0:
        suffix_start = start + len(prefix)
        end = suffix_start
        while end < len(text) and (text[end] == "_" or "A" <= text[end] <= "Z" or "0" <= text[end] <= "9"):
            end += 1
        left_is_identifier = start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_")
        right_is_identifier = end < len(text) and (text[end].isalnum() or text[end] == "_")
        if end > suffix_start and not left_is_identifier and not right_is_identifier:
            spans.append((start, end))
        search_from = max(end, suffix_start)
    return spans


def _protected_machine_spans(text: str) -> list[tuple[int, int]]:
    spans = [
        (match.start(), match.end())
        for match in URL_CANDIDATE_RE.finditer(text)
        if url_origin(match.group(0)) == match.group(0)
    ]
    spans.extend(_net_error_spans(text))
    return sorted(spans)


def _span_intersecting_cut(spans: Sequence[tuple[int, int]], cut: int) -> tuple[int, int] | None:
    return next((span for span in spans if span[0] < cut < span[1]), None)


def _prefix_with_elision(
    text: str,
    *,
    cut: int,
    spans: Sequence[tuple[int, int]],
    max_chars: int,
) -> str:
    suffix = f" {_DISPLAY_REASON_ELISION}"
    cut = min(cut, max_chars - len(suffix))
    while intersected_span := _span_intersecting_cut(spans, cut):
        cut = intersected_span[0]
    prefix = text[:cut].rstrip()
    return f"{prefix}{suffix}" if prefix else _DISPLAY_REASON_ELISION


def _elide_protected_span(
    text: str,
    *,
    span: tuple[int, int],
    spans: Sequence[tuple[int, int]],
    max_chars: int,
) -> str:
    token = text[span[0] : span[1]]
    separator = f" {_DISPLAY_REASON_ELISION} "
    prefix_budget = max_chars - len(separator) - len(token)
    if prefix_budget <= 0:
        marked_token = f"{_DISPLAY_REASON_ELISION} {token}"
        if len(marked_token) <= max_chars:
            return marked_token
        return _prefix_with_elision(text, cut=span[0], spans=spans, max_chars=max_chars)

    cut = prefix_budget
    while intersected_span := _span_intersecting_cut(spans, cut):
        cut = intersected_span[0]
    prefix = text[:cut].rstrip()
    if not prefix:
        return f"{_DISPLAY_REASON_ELISION} {token}"
    return f"{prefix}{separator}{token}"


def _elide_protected_machine_span(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    spans = _protected_machine_spans(text)
    intersected_span = _span_intersecting_cut(spans, max_chars)
    if intersected_span is None:
        return text[:max_chars]
    return _elide_protected_span(text, span=intersected_span, spans=spans, max_chars=max_chars)


def run_outcome_display_reason(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    reason = redact_raw_secrets_for_prompt(" ".join(text.split()))
    reason = URL_CANDIDATE_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", reason)
    return _elide_protected_machine_span(reason, _DISPLAY_REASON_MAX_CHARS)


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


def is_interim_run_outcome(outcome: RecordedRunOutcome | None) -> bool:
    """A run start with no result behind it, which no surface may read as a resolved outcome."""
    return outcome is not None and outcome.role == _INTERIM_RUN_OUTCOME_ROLE


def interim_run_start_outcome(workflow_run_id: str) -> RecordedRunOutcome:
    """The run-start record a mid-run stop reads: a run exists, its lifecycle and blocks are unresolved."""
    return RecordedRunOutcome(
        verdict="not_evaluated",
        workflow_run_id=workflow_run_id,
        run_completed=None,
        role=_INTERIM_RUN_OUTCOME_ROLE,
    )


def select_run_outcome_anchor(run_outcomes: Sequence[RecordedRunOutcome]) -> RecordedRunOutcome | None:
    """The latest run the turn touched; a result supersedes that run's own start, whatever its verdict."""
    resolved_run_ids = {
        outcome.workflow_run_id
        for outcome in run_outcomes
        if outcome.workflow_run_id is not None and not is_interim_run_outcome(outcome)
    }
    live_outcomes = [
        outcome
        for outcome in run_outcomes
        if not (is_interim_run_outcome(outcome) and outcome.workflow_run_id in resolved_run_ids)
    ]
    return live_outcomes[-1] if live_outcomes else None


def run_start_unresolved(run_outcomes: Sequence[RecordedRunOutcome]) -> bool:
    """True when the outcome the turn anchors on is a run start with no result behind it."""
    return is_interim_run_outcome(select_run_outcome_anchor(run_outcomes))
