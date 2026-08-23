from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from skyvern.forge.sdk.copilot.blocker_signal import assert_clean_user_facing_text
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt

TerminalNextState = Literal["completed", "proposal_pending", "awaiting_user_input", "stopped"]
TerminalResponseKind = Literal["question", "update", "answer", "stopped"]
TerminalCause = Literal["deadline_expired", "max_turns_exceeded"]
_FINAL_RUN_VERDICTS = frozenset({"not_demonstrated", "not_evaluated"})
_REVIEW_PROPOSAL_DISPOSITIONS = frozenset({"review_untested", "review_tested"})
_SHADOW_REASON_TRAILING_PUNCTUATION = ".,;:!?"

INTERRUPTED_TERMINAL_REASON = "interrupted"
INTERRUPTED_TERMINAL_HEADLINE = "This turn was interrupted before it could finish."
INTERRUPTED_TERMINAL_RETRY = "Send your message again to retry."
INTERRUPTED_TERMINAL_MESSAGE = f"{INTERRUPTED_TERMINAL_HEADLINE} {INTERRUPTED_TERMINAL_RETRY}"


class InterruptedTurnFacts(BaseModel):
    """What is known about a turn that stopped before it finished.

    Every member is optional because each path that records an interruption knows a
    different subset, and a guessed value would read as a claim about the turn.
    """

    recorded_at: str | None = None
    iteration: int | None = None
    workflow_permanent_id: str | None = None
    workflow_version: int | None = None
    authored_edits_saved: bool | None = None
    last_recorded_build_test_phase: str | None = None


class TerminalOutcomeEnvelope(BaseModel):
    next_state: TerminalNextState
    verified: bool
    workflow_applied: bool = False
    run_verdict: str | None = None
    run_display_reason: str | None = None
    run_output_report: str | None = None
    blocker_reason: str | None = None
    halt_kind: str | None = None
    user_action_required: bool = False
    attempted: str | None = None
    response_kind: TerminalResponseKind
    terminal_cause: TerminalCause | None = None
    interruption: InterruptedTurnFacts | None = None
    rendered_from_envelope: bool = False
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
    workflow_attempted: bool,
    terminal_cause: TerminalCause | None = None,
) -> TerminalOutcomeEnvelope | None:
    run_outcome = _select_run_outcome_anchor(run_outcomes)
    run_verdict = run_outcome.verdict if run_outcome is not None else None
    run_display_reason = _clean_text(run_outcome.display_reason) if run_outcome is not None else None
    run_output_report = _safe_output_report(run_outcome.output_report) if run_outcome is not None else None
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
        workflow_attempted=workflow_attempted,
        explicit_stop=bool(run_outcome or blocker_reason or halt_kind or terminal_cause),
    )
    return TerminalOutcomeEnvelope(
        next_state=next_state,
        verified=verified,
        workflow_applied=workflow_applied,
        run_verdict=run_verdict,
        run_display_reason=run_display_reason,
        run_output_report=run_output_report,
        blocker_reason=_clean_text(blocker_reason),
        halt_kind=_clean_text(halt_kind),
        user_action_required=user_action_required,
        attempted=_clean_text(attempted),
        response_kind=response_kind,
        terminal_cause=terminal_cause,
    )


def finalize_applied_state(
    envelope: TerminalOutcomeEnvelope, *, applied: bool, proposal_present: bool = False
) -> TerminalOutcomeEnvelope:
    if envelope.user_action_required:
        next_state: TerminalNextState = "awaiting_user_input"
    elif envelope.verified and applied:
        next_state = "completed"
    # A verified un-applied proposal is pending review even though its
    # auto_applicable disposition would otherwise fall through to "stopped":
    # verified fixes no longer auto-commit. Unverified builds keep the
    # built-unverified stop.
    elif envelope.next_state == "proposal_pending" or (proposal_present and not applied and envelope.verified):
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


def interrupted_terminal_envelope(facts: InterruptedTurnFacts | None = None) -> TerminalOutcomeEnvelope:
    """Envelope for a turn that stopped before it finished — stopped, but never user-cancelled."""
    return TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        workflow_applied=facts is not None and facts.authored_edits_saved is True,
        response_kind="stopped",
        halt_kind=INTERRUPTED_TERMINAL_REASON,
        interruption=facts,
    )


def render_interrupted_message(facts: InterruptedTurnFacts | None = None) -> str:
    """User-facing copy for an interrupted turn: what is known, and never why it stopped."""
    message = INTERRUPTED_TERMINAL_HEADLINE
    if facts is not None:
        if facts.recorded_at:
            message = _append_sentence(message, f"Recorded at {facts.recorded_at}.")
        if facts.iteration is not None:
            message = _append_sentence(message, f"It reached iteration {facts.iteration}.")
        if facts.workflow_permanent_id:
            workflow = f"Workflow {facts.workflow_permanent_id}"
            if facts.workflow_version is not None:
                workflow += f", version {facts.workflow_version}"
            message = _append_sentence(message, f"{workflow}.")
        if facts.authored_edits_saved is not None:
            saved = "were saved to" if facts.authored_edits_saved else "were not saved to"
            message = _append_sentence(message, f"Your edits from this turn {saved} the workflow.")
        if facts.last_recorded_build_test_phase:
            message = _append_sentence(
                message, f"Last recorded build-test phase: {facts.last_recorded_build_test_phase}."
            )
    return _append_sentence(message, INTERRUPTED_TERMINAL_RETRY)


def render_terminal_message(envelope: TerminalOutcomeEnvelope, agent_message: str, cancelled: bool) -> tuple[str, bool]:
    output_report = _safe_output_report(envelope.run_output_report)
    # A deadline-expired turn already authored copy naming time and the draft's
    # state; replaced=True is what syncs it to the surfaces hydration prefers.
    # "completed" is excluded because replaced=True also overwrites a distinct
    # narrativeSummary, and an applied turn's summary is not the terminal text.
    # "awaiting_user_input" needs no exclusion: it requires ASK_QUESTION, and a deadline
    # always exits through _build_wip_exit_result, which only ever builds REPLY results.
    if envelope.terminal_cause == "deadline_expired" and not cancelled and envelope.next_state != "completed":
        if output_report and not _text_contains(agent_message, output_report):
            return _append_sentence(agent_message, output_report), True
        return agent_message, True

    # A plain reply with no concrete workflow/run/blocker evidence is an answer
    # even though next_state remains "stopped"; only stopped-kind turns get the
    # honest-stop replacement.
    if cancelled:
        return agent_message, False

    if envelope.next_state != "stopped" or envelope.response_kind != "stopped":
        if output_report and not _text_contains(agent_message, output_report):
            return _append_sentence(agent_message, output_report), True
        return agent_message, False

    if envelope.run_verdict == "not_demonstrated":
        message = "I ran the workflow, but I could not confirm the goal was met."
    elif envelope.run_verdict == "not_evaluated":
        message = "I ran the workflow. The latest recorded run completed."
    else:
        message = "I stopped without confirming the goal was met."

    if envelope.run_display_reason:
        message = _append_labeled_sentence(message, label="Reason", text=envelope.run_display_reason)
    if output_report and not _text_contains(message, output_report):
        message = _append_sentence(message, output_report)

    blocker_reason = envelope.blocker_reason
    if blocker_reason and not _text_contains(message, blocker_reason):
        message = _append_labeled_sentence(message, label="Evidence", text=blocker_reason)
    return message, True


def _select_run_outcome_anchor(run_outcomes: Sequence[RecordedRunOutcome]) -> RecordedRunOutcome | None:
    final_outcomes = [outcome for outcome in run_outcomes if outcome.verdict in _FINAL_RUN_VERDICTS]
    if not final_outcomes:
        return None
    # The terminal reports the run record in order. An interim event is not a terminal
    # run when a recorded run exists; otherwise the latest interim event is the only
    # run fact available.
    recorded = [outcome for outcome in final_outcomes if outcome.role != "interim_build_test"]
    if recorded:
        return recorded[-1]
    return final_outcomes[-1]


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
    workflow_attempted: bool = False,
    explicit_stop: bool = False,
    prior_response_kind: TerminalResponseKind | None = None,
) -> TerminalResponseKind:
    if user_action_required:
        return "question"
    if next_state in {"completed", "proposal_pending"}:
        return "update"
    if prior_response_kind == "answer":
        return "answer"
    if prior_response_kind is not None:
        return "stopped"
    if workflow_mutated or workflow_attempted or explicit_stop:
        return "stopped"
    return "answer"


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


def _safe_output_report(value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None or redact_raw_secrets_for_prompt(text) != text:
        return None
    try:
        assert_clean_user_facing_text(text)
    except ValueError:
        return None
    return text


def _append_sentence(base: str, text: str) -> str:
    prefix = base.rstrip()
    if prefix and prefix[-1:] not in ".!?":
        prefix += "."
    return f"{prefix} {text}".strip()


def _append_labeled_sentence(base: str, *, label: str, text: str) -> str:
    prefix = base if base.endswith((".", "!", "?")) else f"{base}."
    return f"{prefix} {label}: {text}"


def _text_contains(text: str, fragment: str) -> bool:
    normalized_text = normalize_shadow_reason_text(text)
    normalized_fragment = normalize_shadow_reason_text(fragment)
    return bool(normalized_text and normalized_fragment and normalized_fragment in normalized_text)
