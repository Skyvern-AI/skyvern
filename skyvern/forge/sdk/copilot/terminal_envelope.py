from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import structlog
from pydantic import BaseModel

from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

LOG = structlog.get_logger(__name__)

TerminalNextState = Literal["completed", "proposal_pending", "awaiting_user_input", "stopped"]
TerminalResponseKind = Literal["question", "update", "answer", "stopped"]
_FINAL_RUN_VERDICTS = frozenset({"demonstrated", "not_demonstrated", "not_evaluated"})
_REVIEW_PROPOSAL_DISPOSITIONS = frozenset({"review_untested", "review_tested"})
_SHADOW_REASON_TRAILING_PUNCTUATION = ".,;:!?"


class TerminalOutcomeEnvelope(BaseModel):
    next_state: TerminalNextState
    verified: bool
    workflow_applied: bool = False
    run_verdict: str | None = None
    run_display_reason: str | None = None
    blocker_reason: str | None = None
    halt_kind: str | None = None
    user_action_required: bool = False
    attempted: str | None = None
    response_kind: TerminalResponseKind
    envelope_version: int = 1


def assemble_terminal_envelope(
    *,
    response_type: str,
    verified: bool,
    workflow_applied: bool,
    proposal_disposition: str | None,
    run_outcomes: Sequence[RecordedRunOutcome],
    blocker_reason: str | None,
    halt_kind: str | None,
    attempted: str | None,
    workflow_mutated: bool,
    turn_outcome_response_kind: str | None,
) -> TerminalOutcomeEnvelope | None:
    run_outcome = _select_run_outcome_anchor(run_outcomes)
    superseding_outcome = _later_demonstrated_after_anchor(run_outcomes, run_outcome)
    if run_outcome is not None and superseding_outcome is not None:
        LOG.info(
            "Terminal envelope anchored a not_demonstrated verdict past a later demonstrated run",
            anchored_workflow_run_id=run_outcome.workflow_run_id,
            anchored_reason_code=run_outcome.reason_code,
            later_workflow_run_id=superseding_outcome.workflow_run_id,
        )
    run_verdict = run_outcome.verdict if run_outcome is not None else None
    run_display_reason = _clean_text(run_outcome.display_reason) if run_outcome is not None else None
    user_action_required = response_type == "ASK_QUESTION"
    next_state = _derive_next_state(
        user_action_required=user_action_required,
        verified=verified,
        workflow_applied=workflow_applied,
        proposal_disposition=proposal_disposition,
    )
    response_kind = _derive_response_kind(
        user_action_required=user_action_required,
        next_state=next_state,
        workflow_mutated=workflow_mutated,
        turn_outcome_response_kind=turn_outcome_response_kind,
    )
    return TerminalOutcomeEnvelope(
        next_state=next_state,
        verified=verified,
        workflow_applied=workflow_applied,
        run_verdict=run_verdict,
        run_display_reason=run_display_reason,
        blocker_reason=_clean_text(blocker_reason),
        halt_kind=_clean_text(halt_kind),
        user_action_required=user_action_required,
        attempted=_clean_text(attempted),
        response_kind=response_kind,
    )


def finalize_applied_state(envelope: TerminalOutcomeEnvelope, *, applied: bool) -> TerminalOutcomeEnvelope:
    if envelope.user_action_required:
        next_state: TerminalNextState = "awaiting_user_input"
    elif envelope.verified and applied:
        next_state = "completed"
    elif envelope.next_state == "proposal_pending":
        next_state = "proposal_pending"
    else:
        next_state = "stopped"
    response_kind = _derive_response_kind(
        user_action_required=envelope.user_action_required,
        next_state=next_state,
        prior_response_kind=envelope.response_kind,
    )
    return envelope.model_copy(
        update={"workflow_applied": applied, "next_state": next_state, "response_kind": response_kind}
    )


def _select_run_outcome_anchor(run_outcomes: Sequence[RecordedRunOutcome]) -> RecordedRunOutcome | None:
    final_outcomes = [outcome for outcome in run_outcomes if outcome.verdict in _FINAL_RUN_VERDICTS]
    if not final_outcomes:
        return None
    last_not_demonstrated = [outcome for outcome in final_outcomes if outcome.verdict == "not_demonstrated"]
    if last_not_demonstrated:
        return last_not_demonstrated[-1]
    return final_outcomes[-1]


def _later_demonstrated_after_anchor(
    run_outcomes: Sequence[RecordedRunOutcome], anchor: RecordedRunOutcome | None
) -> RecordedRunOutcome | None:
    if anchor is None or anchor.verdict != "not_demonstrated":
        return None
    seen_anchor = False
    for outcome in run_outcomes:
        if outcome is anchor:
            seen_anchor = True
            continue
        if seen_anchor and outcome.verdict == "demonstrated":
            return outcome
    return None


def _derive_next_state(
    *,
    user_action_required: bool,
    verified: bool,
    workflow_applied: bool,
    proposal_disposition: str | None,
) -> TerminalNextState:
    if user_action_required:
        return "awaiting_user_input"
    if verified and workflow_applied:
        return "completed"
    if _proposal_requires_review(proposal_disposition):
        return "proposal_pending"
    return "stopped"


def _derive_response_kind(
    *,
    user_action_required: bool,
    next_state: TerminalNextState,
    workflow_mutated: bool | None = None,
    turn_outcome_response_kind: str | None = None,
    prior_response_kind: TerminalResponseKind | None = None,
) -> TerminalResponseKind:
    if user_action_required:
        return "question"
    if next_state in {"completed", "proposal_pending"}:
        return "update"
    if prior_response_kind == "answer":
        return "answer"
    if workflow_mutated is False and turn_outcome_response_kind == "diagnose":
        return "answer"
    return "stopped"


def normalize_shadow_reason_text(text: object, *, strip_trailing_punctuation: bool = False) -> str | None:
    if not isinstance(text, str):
        return None
    normalized = " ".join(text.lower().split())
    if strip_trailing_punctuation:
        normalized = normalized.rstrip(_SHADOW_REASON_TRAILING_PUNCTUATION).strip()
    return normalized or None


def reason_in_reply_shadow(run_display_reason: str | None, final_message: str) -> bool:
    normalized_reason = normalize_shadow_reason_text(run_display_reason, strip_trailing_punctuation=True)
    normalized_reply = normalize_shadow_reason_text(final_message)
    return bool(normalized_reason and normalized_reply and normalized_reason in normalized_reply)


def _proposal_requires_review(proposal_disposition: str | None) -> bool:
    if not isinstance(proposal_disposition, str):
        return False
    return proposal_disposition.strip().lower() in _REVIEW_PROPOSAL_DISPOSITIONS


def _clean_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
