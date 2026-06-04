"""Coverage for the post-output ``apply_repeated_reply_guard`` and the
AST-walked invariants the plan §4 requires: every ``AgentResult(...)``
construction site populates ``turn_outcome``, and the v2 route forwards it
when persisting assistant rows.
"""

from __future__ import annotations

import ast
from pathlib import Path

from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_outcome import (
    HANDOFF_REPLY,
    IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON,
    apply_repeated_reply_guard,
    escalation_reply_for,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind

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
