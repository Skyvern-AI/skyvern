from __future__ import annotations

from datetime import datetime, timezone

from skyvern.forge.sdk.copilot.repeated_reply_summary import (
    RepeatedReplyKind,
    summarize_repeated_replies,
)
from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentReasonCode
from skyvern.forge.sdk.copilot.turn_outcome import build_minimal_turn_outcome
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


def _user(content: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.USER,
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _ai(content: str, outcome: TurnOutcome | None) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.AI,
        content=content,
        turn_outcome=outcome,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _outcome(
    content: str,
    *,
    response_kind: ResponseKind = ResponseKind.DIAGNOSE,
    blocked: list[str] | None = None,
) -> TurnOutcome:
    return build_minimal_turn_outcome(
        content,
        response_kind=response_kind,
        inherited_blocked_signatures=blocked or [],
    )


def _intent(*, stuck: bool) -> TurnIntent:
    reason_codes = [TurnIntentReasonCode.REQUEST_POLICY_DERIVED]
    if stuck:
        reason_codes.append(TurnIntentReasonCode.USER_NON_PROGRESS)
    return TurnIntent(reason_codes=reason_codes)


_ANSWER = "The file is stored in the Artifacts section of the run results page."


def test_empty_history_is_no_repeat() -> None:
    assert summarize_repeated_replies([], _intent(stuck=True)).kind is RepeatedReplyKind.NO_REPEAT


def test_no_user_non_progress_signal_does_not_trip() -> None:
    outcome = _outcome(_ANSWER)
    history = [
        _user("where is my file"),
        _ai(_ANSWER, outcome),
        _user("i cannot see it"),
        _ai(_ANSWER, outcome),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=False))
    assert summary.kind is RepeatedReplyKind.NO_REPEAT


def test_fresh_trip_fires_when_signatures_match_and_intent_marks_non_progress() -> None:
    outcome = _outcome(_ANSWER)
    history = [
        _user("where is my file"),
        _ai(_ANSWER, outcome),
        _user("i cannot see it"),
        _ai(_ANSWER, outcome),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert summary.kind is RepeatedReplyKind.REPEATED_REPLY_DETECTED
    assert outcome.normalized_reply_signature in summary.blocked_signatures


def test_distinct_replies_do_not_fire_even_with_non_progress_signal() -> None:
    history = [
        _user("where is my file"),
        _ai(_ANSWER, _outcome(_ANSWER)),
        _user("i still cannot see it"),
        _ai("I have emailed it to your account.", _outcome("I have emailed it.")),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert summary.kind is RepeatedReplyKind.NO_REPEAT


def test_inherited_ban_survives_a_timeout_interruption() -> None:
    inherited = compute_signature(_ANSWER)
    history = [
        _user("where is my file"),
        _ai(_ANSWER, _outcome(_ANSWER, blocked=[inherited])),
        _user("i still cannot see it"),
        _ai("I ran out of time. Here's what I have so far.", None),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert summary.kind is RepeatedReplyKind.REPEATED_REPLY_DETECTED
    assert inherited in summary.blocked_signatures


def test_signature_only_in_one_outcome_does_not_create_fresh_trip() -> None:
    history = [
        _user("hi"),
        _ai(_ANSWER, _outcome(_ANSWER)),
        _user("i cannot see it"),
        _ai("Different reply.", _outcome("Different reply.")),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert summary.kind is RepeatedReplyKind.NO_REPEAT


def test_inherited_ban_clears_when_intent_drops_the_signal() -> None:
    inherited = compute_signature(_ANSWER)
    history = [
        _user("where is my file"),
        _ai(_ANSWER, _outcome(_ANSWER, blocked=[inherited])),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=False))
    assert summary.kind is RepeatedReplyKind.NO_REPEAT


def test_blocked_signatures_dedup_across_outcomes() -> None:
    inherited = compute_signature(_ANSWER)
    history = [
        _user("hi"),
        _ai(_ANSWER, _outcome(_ANSWER, blocked=[inherited, inherited])),
        _user("i cannot see it"),
        _ai(_ANSWER, _outcome(_ANSWER, blocked=[inherited])),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert summary.kind is RepeatedReplyKind.REPEATED_REPLY_DETECTED
    assert summary.blocked_signatures.count(inherited) == 1


def test_render_prompt_block_only_populated_when_detected() -> None:
    outcome = _outcome(_ANSWER)
    history = [
        _user("where is my file"),
        _ai(_ANSWER, outcome),
        _user("i cannot see it"),
        _ai(_ANSWER, outcome),
    ]
    summary = summarize_repeated_replies(history, _intent(stuck=True))
    assert "repeated_reply_detected" in summary.render_prompt_block()

    empty = summarize_repeated_replies([], _intent(stuck=True))
    assert empty.render_prompt_block() == ""
