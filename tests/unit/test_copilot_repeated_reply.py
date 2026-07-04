"""Coverage for the repeated-reply enforcement loop: the post-output
``apply_repeated_reply_guard`` and the AST-walked invariants the plan §4
requires (every ``AgentResult(...)`` construction site populates
``turn_outcome``, and the v2 route forwards it when persisting assistant
rows), plus the history-scan ``summarize_repeated_replies``.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from skyvern.forge.sdk.copilot.repeated_reply_summary import (
    RepeatedReplyKind,
    summarize_repeated_replies,
)
from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentReasonCode
from skyvern.forge.sdk.copilot.turn_outcome import (
    HANDOFF_REPLY,
    IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON,
    apply_repeated_reply_guard,
    build_minimal_turn_outcome,
    escalation_reply_for,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)

_AGENT_PY = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "copilot" / "agent.py"
_ROUTE_PY = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "routes" / "workflow_copilot.py"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _agent_result_calls(source_path: Path) -> list[ast.Call]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == "AgentResult"]


def test_guard_returns_original_when_no_match() -> None:
    text = "Drafted v1 of the workflow."
    final_text, outcome = apply_repeated_reply_guard(
        final_text=text,
        attempted_kind=ResponseKind.BUILD,
        blocked_signatures=["other"],
        reason_code="ok",
    )
    assert final_text == text
    assert outcome.response_kind is ResponseKind.BUILD
    assert outcome.normalized_reply_signature == compute_signature(text)
    assert outcome.blocked_signatures == ["other"]
    assert outcome.reason_code == "ok"


def test_guard_rewrites_to_escalation_when_signature_matches() -> None:
    text = "The file is in the Artifacts section."
    sig = compute_signature(text)
    final_text, outcome = apply_repeated_reply_guard(
        final_text=text,
        attempted_kind=ResponseKind.DIAGNOSE,
        blocked_signatures=[sig],
    )
    assert final_text == escalation_reply_for(ResponseKind.DIAGNOSE)
    assert outcome.response_kind is ResponseKind.RECOVER
    assert outcome.terminal_reason == IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON
    assert sig in outcome.blocked_signatures
    assert outcome.normalized_reply_signature == compute_signature(final_text)


def test_guard_falls_back_to_handoff_on_escalation_self_collision() -> None:
    text = "The file is in the Artifacts section."
    original_sig = compute_signature(text)
    escalation_sig = compute_signature(escalation_reply_for(ResponseKind.DIAGNOSE))
    final_text, outcome = apply_repeated_reply_guard(
        final_text=text,
        attempted_kind=ResponseKind.DIAGNOSE,
        blocked_signatures=[original_sig, escalation_sig],
    )
    assert final_text == HANDOFF_REPLY
    assert outcome.response_kind is ResponseKind.RECOVER
    assert original_sig in outcome.blocked_signatures
    assert escalation_sig in outcome.blocked_signatures


def test_guard_carries_inherited_bans_forward_on_no_match() -> None:
    final_text, outcome = apply_repeated_reply_guard(
        final_text="completely new reply",
        attempted_kind=ResponseKind.BUILD,
        blocked_signatures=["banned_a", "banned_b"],
    )
    assert outcome.blocked_signatures == ["banned_a", "banned_b"]


def test_every_agent_result_construction_site_populates_turn_outcome() -> None:
    calls = _agent_result_calls(_AGENT_PY)
    assert calls, "expected at least one AgentResult(...) call in agent.py"
    failures: list[int] = [call.lineno for call in calls if not any(kw.arg == "turn_outcome" for kw in call.keywords)]
    assert not failures, f"AgentResult(...) sites missing turn_outcome=: lines {failures}"


def test_inherited_blocked_signatures_are_threaded_through_agent_paths() -> None:
    tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
    threaded = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in ("blocked_signatures", "inherited_blocked_signatures") and (
                "ctx.blocked_reply_signatures" in ast.unparse(kw.value)
            ):
                threaded += 1
    assert threaded >= 5, (
        "expected ctx.blocked_reply_signatures to be threaded into apply_repeated_reply_guard / "
        f"build_minimal_turn_outcome at multiple sites; saw {threaded}"
    )


def test_route_persists_turn_outcome_on_v2_assistant_rows() -> None:
    tree = ast.parse(_ROUTE_PY.read_text(encoding="utf-8"))
    forwarded = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "turn_outcome":
                continue
            expr = ast.unparse(kw.value)
            if expr in ("agent_result.turn_outcome", "turn_outcome") or expr.startswith("getattr(agent_result"):
                forwarded += 1
    assert forwarded >= 2, f"expected the v2 route to forward turn_outcome on assistant rows; saw {forwarded}"


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
