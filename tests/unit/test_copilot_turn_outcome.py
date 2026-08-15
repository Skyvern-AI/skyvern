from __future__ import annotations

from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_outcome import (
    build_minimal_turn_outcome,
    build_turn_outcome,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome


def test_build_minimal_turn_outcome_sets_signature_and_inherited() -> None:
    outcome = build_minimal_turn_outcome(
        "The file is in the Artifacts section.",
        response_kind=ResponseKind.DIAGNOSE,
        reason_code="diagnose_reply",
        terminal_reason=None,
        inherited_blocked_signatures=["aaaa", "bbbb", "aaaa"],
    )
    assert outcome.response_kind is ResponseKind.DIAGNOSE
    assert outcome.normalized_reply_signature == compute_signature("The file is in the Artifacts section.")
    assert outcome.blocked_signatures == ["aaaa", "bbbb"]
    assert outcome.reason_code == "diagnose_reply"


def test_build_turn_outcome_merges_inherited_and_extra() -> None:
    outcome = build_turn_outcome(
        "drafted v1",
        response_kind=ResponseKind.BUILD,
        tool_calls=["update_workflow", "run_blocks_and_collect_debug"],
        inherited_blocked_signatures=["aaaa"],
        extra_blocked_signatures=["bbbb"],
    )
    assert outcome.response_kind is ResponseKind.BUILD
    assert outcome.tool_calls == ["update_workflow", "run_blocks_and_collect_debug"]
    assert outcome.blocked_signatures == ["aaaa", "bbbb"]


def test_turn_outcome_json_round_trip() -> None:
    outcome = build_minimal_turn_outcome(
        "answer",
        response_kind=ResponseKind.CLARIFY,
        inherited_blocked_signatures=["xyz"],
    )
    payload = outcome.model_dump(mode="json")
    restored = TurnOutcome.model_validate(payload)
    assert restored == outcome
