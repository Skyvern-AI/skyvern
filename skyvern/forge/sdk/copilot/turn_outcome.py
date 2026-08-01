"""Builders + deterministic `TurnIntent -> ResponseKind` mapping for `TurnOutcome`.

Schema types live in ``schemas/copilot_turn_outcome.py`` so chat-history
schemas can embed ``TurnOutcome`` without importing copilot business logic.
This module imports both the schema types and ``TurnIntentMode`` — the only
direction allowed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

import structlog

from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome

LOG = structlog.get_logger()

IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON = "identical_reply_blocked"
CopilotComposerMode = Literal["ask", "build", "code"]


def apply_repeated_reply_guard(
    *,
    final_text: str,
    attempted_kind: ResponseKind,
    blocked_signatures: Iterable[str],
    reason_code: str = "",
    terminal_reason: str | None = None,
    turn_intent: TurnIntent | None = None,
    tool_calls: Iterable[str] = (),
) -> tuple[str, TurnOutcome]:
    """Centralized post-output record. Returns ``(final_text, outcome)``.

    The model's reply is never rewritten: a repeat is the turn's true state, and replacing it with
    escalation prose both invents words the model did not say and — when it also minted a terminal
    reason — ended turns over a chat-presentation concern. Signatures are still recorded and carried
    forward so repetition stays visible downstream.

    Pass ``turn_intent`` and ``tool_calls`` to preserve trace metadata on
    the outcome; otherwise the minimal-shape builder is used.
    """
    inherited = list(blocked_signatures)
    tool_calls_list = list(tool_calls)
    original_signature = compute_signature(final_text)
    if inherited and original_signature in inherited:
        LOG.info("copilot_repeated_reply_observed", normalized_reply_signature=original_signature)
    if turn_intent is not None or tool_calls_list:
        return final_text, build_turn_outcome(
            final_text,
            turn_intent=turn_intent,
            response_kind=attempted_kind,
            reason_code=reason_code,
            tool_calls=tool_calls_list,
            terminal_reason=terminal_reason,
            inherited_blocked_signatures=inherited,
        )
    return final_text, build_minimal_turn_outcome(
        final_text,
        response_kind=attempted_kind,
        reason_code=reason_code,
        terminal_reason=terminal_reason,
        inherited_blocked_signatures=inherited,
    )


_RESPONSE_KIND_BY_MODE: dict[TurnIntentMode, ResponseKind] = {
    TurnIntentMode.BUILD: ResponseKind.BUILD,
    TurnIntentMode.EDIT: ResponseKind.BUILD,
    TurnIntentMode.DRAFT_ONLY: ResponseKind.BUILD,
    TurnIntentMode.CLARIFY: ResponseKind.CLARIFY,
    TurnIntentMode.UNKNOWN: ResponseKind.CLARIFY,
    TurnIntentMode.DIAGNOSE: ResponseKind.DIAGNOSE,
    TurnIntentMode.ANSWER: ResponseKind.ANSWER,
    TurnIntentMode.REFUSE: ResponseKind.REFUSE,
}

# Catches the "added a TurnIntentMode but forgot to map it" foot-gun at import
# time rather than letting the new mode silently fall through to CLARIFY.
# Raises explicitly (not ``assert``) so the guard survives ``python -O``.
_missing_modes = set(TurnIntentMode) - set(_RESPONSE_KIND_BY_MODE)
if _missing_modes:
    raise RuntimeError(f"_RESPONSE_KIND_BY_MODE missing entries for: {sorted(m.value for m in _missing_modes)}")


def derive_response_kind(turn_intent: TurnIntent | None) -> ResponseKind:
    """Closed mapping with an effect-based fallback for safe unknown explanations.

    ``RECOVER`` is set only by the enforcement guard.
    """
    mode = getattr(turn_intent, "mode", None)
    if isinstance(turn_intent, TurnIntent) and mode is TurnIntentMode.UNKNOWN and turn_intent.is_inline_only:
        return ResponseKind.ANSWER
    if isinstance(mode, TurnIntentMode):
        return _RESPONSE_KIND_BY_MODE[mode]
    return ResponseKind.CLARIFY


def _dedup_signatures(signatures: Iterable[str]) -> list[str]:
    return sorted({sig for sig in signatures if isinstance(sig, str) and sig})


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def derive_copilot_code_mode_diagnostics(ctx: Any) -> dict[str, Any]:
    pending_capability = _string_or_none(getattr(ctx, "code_native_pending_capability", None))
    return {
        "copilot_last_code_build_failed": bool(
            getattr(ctx, "last_test_ok", None) is False or getattr(ctx, "last_failed_workflow_yaml", None)
        ),
        "copilot_pending_capability": pending_capability,
    }


def with_copilot_code_mode_diagnostics(outcome: TurnOutcome, ctx: Any) -> TurnOutcome:
    return outcome.model_copy(update=derive_copilot_code_mode_diagnostics(ctx))


def with_copilot_code_mode_metadata(
    outcome: TurnOutcome,
    *,
    effective_mode: CopilotComposerMode,
    code_available: bool,
    turn_id: str | None,
) -> TurnOutcome:
    return outcome.model_copy(
        update={
            "copilot_effective_mode": effective_mode,
            "copilot_code_available": code_available,
            "copilot_turn_id": turn_id,
        }
    )


def build_minimal_turn_outcome(
    final_text: str,
    response_kind: ResponseKind,
    reason_code: str = "",
    terminal_reason: str | None = None,
    inherited_blocked_signatures: Iterable[str] = (),
) -> TurnOutcome:
    """Used by every direct-return ``AgentResult`` site so the persisted AI row
    always carries a ``turn_outcome``. Callers that need ban-set inheritance
    pass it in via ``inherited_blocked_signatures``; the route-level
    ``apply_repeated_reply_guard`` is the typical source."""
    return TurnOutcome(
        response_kind=response_kind,
        reason_code=reason_code,
        normalized_reply_signature=compute_signature(final_text),
        terminal_reason=terminal_reason,
        blocked_signatures=_dedup_signatures(inherited_blocked_signatures),
    )


def build_turn_outcome(
    final_text: str,
    *,
    turn_intent: TurnIntent | None,
    response_kind: ResponseKind | None = None,
    reason_code: str = "",
    tool_calls: Iterable[str] = (),
    terminal_reason: str | None = None,
    inherited_blocked_signatures: Iterable[str] = (),
    extra_blocked_signatures: Iterable[str] = (),
) -> TurnOutcome:
    """Used by the translation path. Resolves ``response_kind`` from the turn
    intent when not supplied; merges inherited + extra blocked signatures so
    the enforcement guard can record the original signature it just blocked."""
    resolved_kind = response_kind if response_kind is not None else derive_response_kind(turn_intent)
    intent_summary: dict[str, Any] = {}
    if turn_intent is not None:
        try:
            intent_summary = dict(turn_intent.to_trace_data())
        except Exception as exc:
            LOG.warning(
                "Failed to serialize TurnIntent trace data for TurnOutcome; using empty dict",
                exc_info=exc,
            )
            intent_summary = {}
    return TurnOutcome(
        turn_intent_summary=intent_summary,
        response_kind=resolved_kind,
        reason_code=reason_code,
        normalized_reply_signature=compute_signature(final_text),
        tool_calls=[str(call) for call in tool_calls if call],
        terminal_reason=terminal_reason,
        blocked_signatures=_dedup_signatures(list(inherited_blocked_signatures) + list(extra_blocked_signatures)),
    )
