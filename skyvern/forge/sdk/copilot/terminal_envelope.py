from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, model_validator

from skyvern.forge.sdk.copilot.blocker_signal import assert_clean_user_facing_text
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.build_test_outcome import BuildTestFailedOperation
from skyvern.forge.sdk.copilot.context import ProposalDisposition
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome, RunOutcomeRole
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.schemas.copilot_turn_outcome import CopilotCancelSource
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatSender

TerminalNextState = Literal["completed", "proposal_pending", "awaiting_user_input", "stopped"]
TerminalResponseKind = Literal["question", "update", "answer", "stopped"]
TerminalCause = Literal[
    "deadline_expired",
    "max_turns_exceeded",
    "browser_operation_failed",
    "already_closed",
    "provisioning_unavailable",
    "cdp_connect_failed",
    "occupied",
    "empty_completion",
]
_FINAL_RUN_VERDICTS = frozenset({"not_demonstrated", "not_evaluated"})
_REVIEW_PROPOSAL_DISPOSITIONS = frozenset({"review_untested", "review_tested"})
_SHADOW_REASON_TRAILING_PUNCTUATION = ".,;:!?"

MINIMAL_HONEST_STOP = "I stopped without confirming the goal was met."
MINIMAL_CANCEL_STOP = "Stopped."
_INTERIM_RUN_OUTCOME_ROLE: RunOutcomeRole = "interim_build_test"
TESTED_DRAFT_PRESERVED = "The tested draft from this turn was preserved for review."
UNTESTED_DRAFT_PRESERVED = "The untested draft from this turn was preserved for review."
_NO_DRAFT_PRESERVED = "No workflow draft from this turn was preserved."
_CANCEL_DRAFT_DISPOSITIONS = (TESTED_DRAFT_PRESERVED, UNTESTED_DRAFT_PRESERVED, _NO_DRAFT_PRESERVED)
# Only a Stop click is unambiguously the user asking. An Escape may be ambient, so it
# keeps the plain opening rather than claiming an intent the request cannot establish.
CANCEL_STOP_AT_USER_REQUEST = "Stopped at your request."
_CANONICAL_ROLLED_BACK = "The saved workflow was rolled back to its state before this turn."

INTERRUPTED_TERMINAL_REASON = "interrupted"
INTERRUPTED_TERMINAL_HEADLINE = "This turn was interrupted before it could finish."
INTERRUPTED_TERMINAL_RETRY = "Send your message again to retry."
INTERRUPTED_TERMINAL_MESSAGE = f"{INTERRUPTED_TERMINAL_HEADLINE} {INTERRUPTED_TERMINAL_RETRY}"
INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE = "This test stopped because a newer test in this chat took over the browser."


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
    run_id: str | None = None
    superseded_by_newer_test: bool = False


class TerminalOutcomeEnvelope(BaseModel):
    next_state: TerminalNextState
    verified: bool
    workflow_applied: bool = False
    run_verdict: str | None = None
    run_id: str | None = None
    run_completed: bool | None = None
    blocks_run_this_turn: int | None = None
    # ``interim_build_test`` marks a run start with no result behind it, which no
    # surface may read as a resolved outcome.
    run_outcome_role: RunOutcomeRole | None = None
    run_display_reason: str | None = None
    run_output_report: str | None = None
    blocker_reason: str | None = None
    halt_kind: str | None = None
    user_action_required: bool = False
    attempted: str | None = None
    response_kind: TerminalResponseKind
    terminal_cause: TerminalCause | None = None
    failed_operation: BuildTestFailedOperation | None = None
    connect_failure: BuildTestConnectFailure | None = None
    proposal_present: bool = False
    proposal_disposition: ProposalDisposition | None = None
    canonical_rolled_back: bool = False
    interruption: InterruptedTurnFacts | None = None
    # None when the client named no gesture, so the report stays silent rather
    # than naming one the request never carried.
    cancel_source: CopilotCancelSource | None = None
    rendered_from_envelope: bool = False
    envelope_version: int = 1

    @model_validator(mode="after")
    def normalize_failed_operation_state(self) -> TerminalOutcomeEnvelope:
        if self.failed_operation is not None or self.connect_failure is not None:
            self.verified = False
            self.workflow_applied = False
        return self


def chat_awaits_user_input(*, sender: str, narrative_payload: object) -> bool:
    """Whether a chat is blocked on the user, given its most recent message row.

    Keyed on the tail of the conversation, not on the last assistant turn: a user reply
    answers the ask. The cancel path persists the pre-cancel envelope verbatim, so a stop
    still carries ``awaiting_user_input`` and must not read as a question.
    """
    if sender != WorkflowCopilotChatSender.AI:
        return False
    if not isinstance(narrative_payload, dict) or narrative_payload.get("cancelled") is True:
        return False
    envelope = narrative_payload.get("terminalEnvelope")
    if not isinstance(envelope, dict):
        return False
    return envelope.get("rendered_from_envelope") is True and envelope.get("next_state") == "awaiting_user_input"


def assemble_terminal_envelope(
    *,
    response_type: str,
    verified: bool,
    workflow_applied: bool,
    proposal_disposition: ProposalDisposition | None,
    run_outcomes: Sequence[RecordedRunOutcome],
    blocker_reason: str | None,
    halt_kind: str | None,
    attempted: str | None,
    workflow_mutated: bool,
    workflow_attempted: bool,
    terminal_cause: TerminalCause | None = None,
    blocks_run_this_turn: int | None = None,
    failed_operation: BuildTestFailedOperation | None = None,
    connect_failure: BuildTestConnectFailure | None = None,
    proposal_present: bool = False,
    interruption: InterruptedTurnFacts | None = None,
) -> TerminalOutcomeEnvelope | None:
    run_outcome = select_run_outcome_anchor(run_outcomes)
    run_outcome_role = run_outcome.role if run_outcome is not None else None
    run_resolved = run_outcome is not None and not is_interim_run_outcome(run_outcome)
    run_verdict = run_outcome.verdict if run_outcome is not None else None
    run_id = _clean_text(run_outcome.workflow_run_id) if run_outcome is not None else None
    # Lifecycle and identity come from the same archived outcome, which a workflow edit
    # never rewrites, so the two can never name different runs.
    run_completed = run_outcome.run_completed if run_outcome is not None else None
    run_display_reason = _clean_text(run_outcome.display_reason) if run_outcome is not None else None
    run_output_report = _safe_output_report(run_outcome.output_report) if run_outcome is not None else None
    user_action_required = response_type == "ASK_QUESTION"
    if failed_operation is not None or connect_failure is not None:
        verified = False
        workflow_applied = False
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
        explicit_stop=bool(run_resolved or blocker_reason or halt_kind or terminal_cause),
    )
    if failed_operation is not None:
        if not user_action_required:
            next_state = "stopped"
            response_kind = "stopped"
        terminal_cause = terminal_cause or failed_operation.kind
    if connect_failure is not None:
        if not user_action_required:
            next_state = "stopped"
            response_kind = "stopped"
        # Capacity is turn-level evidence and remains authoritative. Otherwise the
        # later acquisition failure owns the user-facing terminal over an older
        # operation failure retained from an earlier build test in the same turn.
        if terminal_cause not in {"deadline_expired", "max_turns_exceeded"}:
            terminal_cause = connect_failure.state
    return TerminalOutcomeEnvelope(
        next_state=next_state,
        verified=verified,
        workflow_applied=workflow_applied,
        run_verdict=run_verdict,
        run_id=run_id,
        run_completed=run_completed,
        blocks_run_this_turn=blocks_run_this_turn,
        run_outcome_role=run_outcome_role,
        run_display_reason=run_display_reason,
        run_output_report=run_output_report,
        blocker_reason=_clean_text(blocker_reason),
        halt_kind=_clean_text(halt_kind),
        user_action_required=user_action_required,
        attempted=_clean_text(attempted),
        response_kind=response_kind,
        terminal_cause=terminal_cause,
        failed_operation=failed_operation,
        connect_failure=connect_failure,
        proposal_present=proposal_present,
        proposal_disposition=proposal_disposition,
        interruption=interruption,
    )


def finalize_applied_state(
    envelope: TerminalOutcomeEnvelope, *, applied: bool, proposal_present: bool = False
) -> TerminalOutcomeEnvelope:
    if envelope.failed_operation is not None or envelope.connect_failure is not None:
        if envelope.user_action_required:
            return envelope.model_copy(
                update={
                    "verified": False,
                    "workflow_applied": False,
                    "next_state": "awaiting_user_input",
                    "response_kind": "question",
                }
            )
        return envelope.model_copy(
            update={"verified": False, "workflow_applied": False, "next_state": "stopped", "response_kind": "stopped"}
        )
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
        run_id=facts.run_id if facts is not None else None,
        interruption=facts,
    )


def render_interrupted_message(facts: InterruptedTurnFacts | None = None, *, proposal_present: bool = False) -> str:
    """User-facing copy for an interrupted turn: what is known, and never why it stopped."""
    superseded = facts is not None and facts.superseded_by_newer_test
    message = INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE if superseded else INTERRUPTED_TERMINAL_HEADLINE
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
    if proposal_present:
        # The Accept/Discard card stays on screen for an untested draft, so a message that did not
        # mention it would contradict what the user is looking at.
        message = _append_sentence(message, "The untested draft is available for review.")
    # The newer message is already sent on a superseded turn, so asking for it again is a lie.
    if superseded:
        return message
    return _append_sentence(message, INTERRUPTED_TERMINAL_RETRY)


def render_terminal_message(envelope: TerminalOutcomeEnvelope, agent_message: str, cancelled: bool) -> tuple[str, bool]:
    if envelope.terminal_cause in {"deadline_expired", "max_turns_exceeded"} and not cancelled:
        return agent_message, False
    output_report = _safe_output_report(envelope.run_output_report)
    if (
        envelope.connect_failure is not None
        and envelope.terminal_cause == envelope.connect_failure.state
        and not cancelled
    ):
        failure = envelope.connect_failure
        message = f"Build testing stopped with browser connection state `{failure.state}`."
        identities = [
            ("workflow run", failure.workflow_run_id),
            ("child run", failure.workflow_run_block_id),
            ("task", failure.task_id),
            ("browser session", failure.browser_session_id),
        ]
        for label, identity in identities:
            if identity:
                message = _append_sentence(message, f"Recorded {label}: {identity}.")
        if envelope.proposal_present:
            message = _append_sentence(message, "The untested draft was preserved for review.")
            message = _append_sentence(message, "Retry in a fresh browser session to test this same draft.")
        if envelope.user_action_required:
            pending_question_intro = "The pending question is quoted below; its premise is not confirmed"
            if _text_contains(agent_message, message) and _text_contains(agent_message, pending_question_intro):
                return agent_message, False
            message = _append_sentence(message, f"{pending_question_intro}: {agent_message}")
        return message, True
    # A crash and a failed test are different endings, and the latch a crash inherits from an
    # earlier build test in the same turn would otherwise render both with the same sentence.
    # A connect failure still outranks this: it names identities the operator needs.
    if envelope.interruption is not None and not cancelled:
        return render_interrupted_message(envelope.interruption, proposal_present=envelope.proposal_present), True
    if envelope.failed_operation is not None and not cancelled:
        block_label = envelope.failed_operation.block_label
        message = (
            f"I stopped after a browser operation failed in `{block_label}` while testing the workflow."
            if block_label
            else "I stopped after a browser operation failed while testing the workflow."
        )
        if envelope.proposal_present:
            message = _append_sentence(
                message,
                "The untested draft is available for review, but the requested work was not confirmed.",
            )
        else:
            message = _append_sentence(message, "The requested work was not confirmed.")
        if envelope.user_action_required:
            pending_question_intro = "The pending question is quoted below; its premise is not confirmed"
            # The route may render an AgentResult again when envelope-authoritative copy is enabled.
            # Preserve the first server-authored rendering instead of quoting that whole rendering as
            # though it were the model's pending question.
            if _text_contains(agent_message, message) and _text_contains(agent_message, pending_question_intro):
                return agent_message, False
            message = _append_sentence(
                message,
                f"{pending_question_intro}: {agent_message}",
            )
        return message, True
    # A stop reports what the turn recorded regardless of derived state: a cancel that
    # preserved a draft derives ``proposal_pending`` and never reaches the branch below.
    if cancelled:
        # This branch renders twice on a stop that also carries a failed operation or a
        # connect failure, so every clause is appended only when it is not already there.
        message = agent_message.strip() or MINIMAL_CANCEL_STOP
        # Replace only the opening sentence, so a stop that also preserved a draft keeps
        # the rest of its seed reply instead of losing the upgrade to a longer prefix.
        if envelope.cancel_source == "stop_button" and message.startswith(MINIMAL_CANCEL_STOP):
            message = CANCEL_STOP_AT_USER_REQUEST + message[len(MINIMAL_CANCEL_STOP) :]
        for sentence in _recorded_run_facts(envelope):
            if not _text_contains(message, sentence):
                message = _append_sentence(message, sentence)
        if not any(_text_contains(message, sentence) for sentence in _CANCEL_DRAFT_DISPOSITIONS):
            message = _append_sentence(message, _cancel_draft_disposition(envelope))
        if envelope.canonical_rolled_back and not _text_contains(message, _CANONICAL_ROLLED_BACK):
            message = _append_sentence(message, _CANONICAL_ROLLED_BACK)
        if envelope.run_display_reason and not _text_contains(message, envelope.run_display_reason):
            message = _append_labeled_sentence(message, label="Reason", text=envelope.run_display_reason)
        if output_report and not _text_contains(message, output_report):
            message = _append_sentence(message, output_report)
        return message, message != agent_message

    # A plain reply with no concrete workflow/run/blocker evidence is an answer
    # even though next_state remains "stopped"; only stopped-kind turns carry the
    # recorded facts.
    if envelope.next_state != "stopped" or envelope.response_kind != "stopped":
        if output_report and not _text_contains(agent_message, output_report):
            return _append_sentence(agent_message, output_report), True
        return agent_message, False

    # Facts are appended to the agent's own text rather than replacing it: a message
    # whose unsupported clause sits beside accurate detail loses that detail if replaced.
    message = agent_message.strip() or MINIMAL_HONEST_STOP
    recorded_facts = _recorded_run_facts(envelope)
    # With no run anchored there is no fact to set beside the agent's text, and silence
    # would let an unsupported success claim stand alone on a turn that stopped.
    if not recorded_facts and message != MINIMAL_HONEST_STOP:
        recorded_facts = [MINIMAL_HONEST_STOP]
    for sentence in recorded_facts:
        message = _append_sentence(message, sentence)

    if envelope.run_display_reason:
        message = _append_labeled_sentence(message, label="Reason", text=envelope.run_display_reason)
    if output_report and not _text_contains(message, output_report):
        message = _append_sentence(message, output_report)

    blocker_reason = envelope.blocker_reason
    if blocker_reason and not _text_contains(message, blocker_reason):
        message = _append_labeled_sentence(message, label="Evidence", text=blocker_reason)
    return message, message != agent_message


def _recorded_run_facts(envelope: TerminalOutcomeEnvelope) -> list[str]:
    facts: list[str] = []
    ran = envelope.blocks_run_this_turn
    if ran is not None:
        facts.append(f"{ran} block{'' if ran == 1 else 's'} ran this turn.")
    elif envelope.run_id is not None:
        facts.append("A run was started, and how many of its blocks ran was not confirmed.")
    # The completion clause rides on the recorded lifecycle fact, never on the
    # verdict: an unevaluated outcome says nothing about whether the run finished.
    lifecycle = "The recorded run completed, and its" if envelope.run_completed else "The recorded run's"
    if envelope.run_verdict == "not_evaluated":
        facts.append(f"{lifecycle} outcome was not evaluated.")
    elif envelope.run_verdict == "not_demonstrated":
        facts.append(f"{lifecycle} outcome did not confirm the goal was met.")
    return facts


def _cancel_draft_disposition(envelope: TerminalOutcomeEnvelope) -> str:
    """Reads the disposition the seed reply was chosen from, so the two cannot disagree."""
    if not envelope.proposal_present:
        return _NO_DRAFT_PRESERVED
    if envelope.proposal_disposition == "review_tested":
        return TESTED_DRAFT_PRESERVED
    return UNTESTED_DRAFT_PRESERVED


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
    # The terminal describes the last run the turn touched, and a result supersedes that run's
    # own start whatever its verdict — including the ``demonstrated`` one no anchor reports.
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
    final_outcomes = [outcome for outcome in live_outcomes if outcome.verdict in _FINAL_RUN_VERDICTS]
    if not final_outcomes:
        return None
    return final_outcomes[-1]


def run_start_unresolved(run_outcomes: Sequence[RecordedRunOutcome]) -> bool:
    """True when the outcome the terminal anchors on is a run start with no result behind it."""
    return is_interim_run_outcome(select_run_outcome_anchor(run_outcomes))


def _derive_next_state(
    *,
    user_action_required: bool,
    verified: bool,
    workflow_applied: bool,
    proposal_disposition: ProposalDisposition | None,
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


def _proposal_requires_review(proposal_disposition: ProposalDisposition | None) -> bool:
    return proposal_disposition in _REVIEW_PROPOSAL_DISPOSITIONS


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
