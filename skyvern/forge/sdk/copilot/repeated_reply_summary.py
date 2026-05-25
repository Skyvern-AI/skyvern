"""Typed signal: is the copilot looping on the same answer?

Reads persisted ``TurnOutcome`` rows from the last assistant turns plus the
current ``TurnIntent``'s ``USER_NON_PROGRESS`` reason code, and emits a ban
set of normalized signatures. The prompt block surfaces the ban; the
enforcement guard in ``_translate_to_agent_result`` blocks a re-emission of
any signature in the set.

Why TurnIntent owns the "user is on the same problem" signal: the
M1.5 architecture decision is that classifiers emit typed intent/reason
codes and deterministic resolvers consume them — duplicating a semantic
regex here would be the "distributed-intent" anti-pattern.

Durability: a signature stays banned across turns as long as TurnIntent
keeps emitting ``USER_NON_PROGRESS``. A topic switch (classifier drops the
code) unfreezes the inherited set. A timeout / error / cancel interruption
does not unfreeze it — inherited bans carry through intermediate
``CLARIFY``-terminal outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentReasonCode
from skyvern.forge.sdk.schemas.copilot_turn_outcome import TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage, WorkflowCopilotChatSender

# Fresh-trip detection needs exactly 2 consecutive assistant turns with the
# same (response_kind, signature); older bans carry forward through the
# inherited blocked_signatures union, so widening the window is unnecessary.
_RECENT_OUTCOME_WINDOW = 2


class RepeatedReplyKind(str, Enum):
    NO_REPEAT = "no_repeat"
    REPEATED_REPLY_DETECTED = "repeated_reply_detected"


@dataclass(frozen=True)
class RepeatedReplySummary:
    kind: RepeatedReplyKind
    blocked_signatures: tuple[str, ...] = ()
    repeat_count: int = 0

    def render_prompt_block(self) -> str:
        if self.kind is not RepeatedReplyKind.REPEATED_REPLY_DETECTED:
            return ""
        sig_list = ", ".join(self.blocked_signatures) if self.blocked_signatures else "(none)"
        return (
            "repeated_reply_detected: your last replies in this conversation"
            " repeated the same normalized signature and the user is still"
            f" signaling non-progress. Blocked signatures: {sig_list}. Do not"
            " produce a reply whose normalized signature matches one of these."
            " Change approach: give a concretely different explanation, take a"
            " different action, surface the limitation honestly, or hand off."
        )


def _recent_assistant_outcomes(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
) -> list[TurnOutcome | None]:
    outcomes: list[TurnOutcome | None] = []
    for message in reversed(chat_history):
        if message.sender != WorkflowCopilotChatSender.AI:
            continue
        if not (message.content or "").strip():
            continue
        outcomes.append(message.turn_outcome)
        if len(outcomes) >= _RECENT_OUTCOME_WINDOW:
            break
    return outcomes


def _inherited_block_set(outcomes: list[TurnOutcome | None]) -> set[str]:
    inherited: set[str] = set()
    for outcome in outcomes:
        if outcome is None:
            continue
        inherited.update(sig for sig in outcome.blocked_signatures if sig)
    return inherited


def _fresh_trip_signature(outcomes: list[TurnOutcome | None]) -> str | None:
    if len(outcomes) < _RECENT_OUTCOME_WINDOW:
        return None
    first, second = outcomes[0], outcomes[1]
    if first is None or second is None:
        return None
    if (
        first.response_kind == second.response_kind
        and first.normalized_reply_signature == second.normalized_reply_signature
        and first.normalized_reply_signature
    ):
        return first.normalized_reply_signature
    return None


def _user_on_same_problem(turn_intent: TurnIntent | None) -> bool:
    if turn_intent is None:
        return False
    return TurnIntentReasonCode.USER_NON_PROGRESS in turn_intent.reason_codes


def summarize_repeated_replies(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    turn_intent: TurnIntent | None,
) -> RepeatedReplySummary:
    if not chat_history:
        return RepeatedReplySummary(kind=RepeatedReplyKind.NO_REPEAT)

    if not _user_on_same_problem(turn_intent):
        return RepeatedReplySummary(kind=RepeatedReplyKind.NO_REPEAT)

    outcomes = _recent_assistant_outcomes(chat_history)
    block_set = _inherited_block_set(outcomes)
    fresh = _fresh_trip_signature(outcomes)
    if fresh is not None:
        block_set.add(fresh)

    if not block_set:
        return RepeatedReplySummary(kind=RepeatedReplyKind.NO_REPEAT)

    blocked = tuple(sorted(block_set))
    repeat_count = sum(
        1 for outcome in outcomes if outcome is not None and outcome.normalized_reply_signature in block_set
    )
    repeat_count = max(repeat_count, 2) if fresh is not None else max(repeat_count, 1)
    return RepeatedReplySummary(
        kind=RepeatedReplyKind.REPEATED_REPLY_DETECTED,
        blocked_signatures=blocked,
        repeat_count=repeat_count,
    )
