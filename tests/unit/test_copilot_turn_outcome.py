from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode
from skyvern.forge.sdk.copilot.turn_outcome import (
    build_minimal_turn_outcome,
    build_turn_outcome,
    derive_response_kind,
    escalation_reply_for,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome


@pytest.mark.parametrize(
    "mode,expected",
    [
        (TurnIntentMode.BUILD, ResponseKind.BUILD),
        (TurnIntentMode.EDIT, ResponseKind.BUILD),
        (TurnIntentMode.DRAFT_ONLY, ResponseKind.BUILD),
        (TurnIntentMode.CLARIFY, ResponseKind.CLARIFY),
        (TurnIntentMode.UNKNOWN, ResponseKind.CLARIFY),
        (TurnIntentMode.DIAGNOSE, ResponseKind.DIAGNOSE),
        (TurnIntentMode.DOCS_ANSWER, ResponseKind.DIAGNOSE),
        (TurnIntentMode.REFUSE, ResponseKind.REFUSE),
    ],
)
def test_derive_response_kind_covers_every_mode(mode: TurnIntentMode, expected: ResponseKind) -> None:
    intent = TurnIntent(mode=mode)
    assert derive_response_kind(intent) is expected


def test_derive_response_kind_handles_none_intent() -> None:
    assert derive_response_kind(None) is ResponseKind.CLARIFY


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
    intent = TurnIntent(mode=TurnIntentMode.BUILD)
    outcome = build_turn_outcome(
        "drafted v1",
        turn_intent=intent,
        tool_calls=["update_workflow", "run_blocks_and_collect_debug"],
        inherited_blocked_signatures=["aaaa"],
        extra_blocked_signatures=["bbbb"],
    )
    assert outcome.response_kind is ResponseKind.BUILD
    assert outcome.tool_calls == ["update_workflow", "run_blocks_and_collect_debug"]
    assert outcome.blocked_signatures == ["aaaa", "bbbb"]
    assert "mode" in outcome.turn_intent_summary


def test_turn_outcome_json_round_trip() -> None:
    outcome = build_minimal_turn_outcome(
        "answer",
        response_kind=ResponseKind.CLARIFY,
        inherited_blocked_signatures=["xyz"],
    )
    payload = outcome.model_dump(mode="json")
    restored = TurnOutcome.model_validate(payload)
    assert restored == outcome


def test_escalation_reply_for_covers_derivable_kinds() -> None:
    for kind in (ResponseKind.BUILD, ResponseKind.CLARIFY, ResponseKind.DIAGNOSE, ResponseKind.REFUSE):
        text = escalation_reply_for(kind)
        assert isinstance(text, str)
        assert text


def test_escalation_reply_for_recover_falls_back_to_clarify_text() -> None:
    assert escalation_reply_for(ResponseKind.RECOVER) == escalation_reply_for(ResponseKind.CLARIFY)
