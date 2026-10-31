"""System-notice copy for turns that stopped before the model wrote a reply."""

from __future__ import annotations

from pydantic import BaseModel, Field

from skyvern.forge.sdk.copilot.context import ProposalDisposition
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcomeVerdict
from skyvern.utils.strings import join_phrases

MINIMAL_CANCEL_STOP = "Stopped."
# Only a Stop click is unambiguously the user asking. An Escape may be ambient, so it
# keeps the plain opening rather than claiming an intent the request cannot establish.
CANCEL_STOP_AT_USER_REQUEST = "Stopped at your request."
CANONICAL_ROLLED_BACK = "The saved workflow was rolled back to its state before this turn."
TESTED_DRAFT_PRESERVED = "The tested draft from this turn was preserved for review."
UNTESTED_DRAFT_PRESERVED = "The untested draft from this turn was preserved for review."
DRAFT_PRESERVED = "The draft was preserved for review."
CANCEL_ACCEPT_OR_DISCARD = "Accept it to save, or discard."
DRAFT_AVAILABLE = "The draft is available for review."
TESTED_DRAFT_AVAILABLE = "The tested draft is available for review."
UNTESTED_DRAFT_AVAILABLE = "The untested draft is available for review."
# Both maps cover every disposition, so a preserved draft can never lose its announcement to a
# missing key; a disposition that staged nothing still describes the draft without claiming a test.
_CANCEL_DRAFT_PRESERVED: dict[ProposalDisposition, str] = {
    "no_proposal": DRAFT_PRESERVED,
    "auto_applicable": TESTED_DRAFT_PRESERVED,
    "review_tested": TESTED_DRAFT_PRESERVED,
    "review_untested": UNTESTED_DRAFT_PRESERVED,
}
_INTERRUPTED_DRAFT_AVAILABLE: dict[ProposalDisposition, str] = {
    "no_proposal": DRAFT_AVAILABLE,
    "auto_applicable": TESTED_DRAFT_AVAILABLE,
    "review_tested": TESTED_DRAFT_AVAILABLE,
    "review_untested": UNTESTED_DRAFT_AVAILABLE,
}

INTERRUPTED_TERMINAL_REASON = "interrupted"
INTERRUPTED_TERMINAL_HEADLINE = "This turn was interrupted before it could finish."
INTERRUPTED_TERMINAL_RETRY = "Send your message again to retry."
INTERRUPTED_TERMINAL_MESSAGE = f"{INTERRUPTED_TERMINAL_HEADLINE} {INTERRUPTED_TERMINAL_RETRY}"
INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE = "This test stopped because a newer test in this chat took over the browser."


class InterruptedTurnFacts(BaseModel):
    """What is known about a turn that stopped before it finished; every member is optional because
    each recording path knows a different subset and a guessed value would read as a claim."""

    recorded_at: str | None = None
    iteration: int | None = None
    workflow_permanent_id: str | None = None
    workflow_version: int | None = None
    authored_edits_saved: bool | None = None
    last_recorded_build_test_phase: str | None = None
    run_id: str | None = None
    superseded_by_newer_test: bool = False


_RUN_VERDICT_RECORDED: dict[RecordedRunOutcomeVerdict, str] = {
    "demonstrated": "recorded the requested outcome",
    "not_demonstrated": "did not record the requested outcome",
    "not_evaluated": "recorded no outcome check",
}


class StoppedTurnFacts(BaseModel):
    """What a stopped turn's own tools recorded before the stop. Edits and the run stay separate facts,
    because a run started before a later edit never exercised that edit."""

    edited_block_labels: list[str] = Field(default_factory=list)
    run_id: str | None = None
    run_started_before_last_edit: bool = False
    run_result_recorded: bool = False
    verdict: RecordedRunOutcomeVerdict | None = None
    run_completed: bool | None = None
    # (label, status) in run order; status is the runner's word for the row, not a verdict.
    block_rows: list[tuple[str, str]] = Field(default_factory=list)
    block_rows_omitted: int = 0
    display_reason: str | None = None
    # Blocks this turn changed after the draft it hands back, when that draft is an earlier tested one.
    changes_not_in_draft: list[str] = Field(default_factory=list)


def _recorded_run_sentence(run: str, stopped: StoppedTurnFacts) -> str:
    if not stopped.run_result_recorded:
        return f"{run} started and has no recorded result."
    if stopped.block_rows:
        rows = join_phrases([f"{label} {status}" for label, status in stopped.block_rows], "and")
        return f"{run} recorded {rows}."
    parts = [
        part
        for part in (
            None if stopped.run_completed is None else ("finished" if stopped.run_completed else "did not finish"),
            _RUN_VERDICT_RECORDED[stopped.verdict] if stopped.verdict is not None else None,
        )
        if part
    ]
    return f"{run} {join_phrases(parts, 'and')}." if parts else f"{run} has a recorded result."


def _stopped_turn_sentences(stopped: StoppedTurnFacts, *, stop_button: bool) -> list[str]:
    sentences: list[str] = []
    if stopped.edited_block_labels:
        sentences.append(f"I changed {join_phrases(stopped.edited_block_labels, 'and')}.")
    if stopped.run_id:
        run = f"test run {stopped.run_id}"
        if stopped.run_started_before_last_edit:
            run += ", which started before my last change,"
        sentences.append(_recorded_run_sentence(run, stopped))
        if stopped.block_rows and stopped.block_rows_omitted:
            sentences.append(f"{stopped.block_rows_omitted} more block row(s) between them are not listed.")
        if stopped.run_result_recorded and stopped.display_reason:
            reason = f"Recorded reason: {stopped.display_reason.rstrip()}"
            sentences.append(reason if reason.endswith((".", "!", "?")) else f"{reason}.")
    if not sentences:
        return []
    opening = "Before you stopped," if stop_button else "Before the stop,"
    sentences[0] = f"{opening} {sentences[0]}"
    return [sentence[:1].upper() + sentence[1:] for sentence in sentences]


def cancel_notice(
    *,
    base: str | None = None,
    stop_button: bool,
    preserved_draft: ProposalDisposition | None,
    canonical_rolled_back: bool,
    stopped: StoppedTurnFacts | None = None,
) -> str:
    """User-facing copy for a cancelled turn: whatever reply the turn already wrote (or the stop
    opening when it is empty), then the ``stopped`` facts and the facts of the stop. ``preserved_draft`` is
    the disposition of the draft left on screen, or None when none awaits the user."""
    notice = (base or "").strip() or (CANCEL_STOP_AT_USER_REQUEST if stop_button else MINIMAL_CANCEL_STOP)
    if stopped is not None:
        for sentence in _stopped_turn_sentences(stopped, stop_button=stop_button):
            notice = append_sentence(notice, sentence)
    if preserved_draft is not None:
        omitted = stopped.changes_not_in_draft if stopped is not None else []
        omitted_sentence = (
            f" It does not include my later change{'s' if len(omitted) > 1 else ''} to {join_phrases(omitted, 'and')}."
            if omitted
            else ""
        )
        notice = append_sentence(
            notice, f"{_CANCEL_DRAFT_PRESERVED[preserved_draft]}{omitted_sentence} {CANCEL_ACCEPT_OR_DISCARD}"
        )
    if canonical_rolled_back:
        notice = append_sentence(notice, CANONICAL_ROLLED_BACK)
    return notice


def render_interrupted_message(
    facts: InterruptedTurnFacts | None = None, *, preserved_draft: ProposalDisposition | None = None
) -> str:
    """User-facing copy for an interrupted turn: what is known, and never why it stopped."""
    superseded = facts is not None and facts.superseded_by_newer_test
    message = INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE if superseded else INTERRUPTED_TERMINAL_HEADLINE
    if facts is not None:
        if facts.recorded_at:
            message = append_sentence(message, f"Recorded at {facts.recorded_at}.")
        if facts.iteration is not None:
            message = append_sentence(message, f"It reached iteration {facts.iteration}.")
        if facts.workflow_permanent_id:
            workflow = f"Workflow {facts.workflow_permanent_id}"
            if facts.workflow_version is not None:
                workflow += f", version {facts.workflow_version}"
            message = append_sentence(message, f"{workflow}.")
        if facts.authored_edits_saved is not None:
            saved = "were saved to" if facts.authored_edits_saved else "were not saved to"
            message = append_sentence(message, f"Your edits from this turn {saved} the workflow.")
        if facts.last_recorded_build_test_phase:
            message = append_sentence(
                message, f"Last recorded build-test phase: {facts.last_recorded_build_test_phase}."
            )
    if preserved_draft is not None:
        # The Accept/Discard card stays on screen for the draft, so a message that did not mention
        # it would contradict what the user is looking at.
        message = append_sentence(message, _INTERRUPTED_DRAFT_AVAILABLE[preserved_draft])
    # The newer message is already sent on a superseded turn, so asking for it again is a lie.
    if superseded:
        return message
    return append_sentence(message, INTERRUPTED_TERMINAL_RETRY)


def append_sentence(base: str, text: str) -> str:
    prefix = base.rstrip()
    if prefix and prefix[-1:] not in ".!?":
        prefix += "."
    return f"{prefix} {text}".strip()
