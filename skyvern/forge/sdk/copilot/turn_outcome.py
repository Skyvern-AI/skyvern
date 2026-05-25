"""Builders + closed `TurnIntentMode -> ResponseKind` mapping for `TurnOutcome`.

Schema types live in ``schemas/copilot_turn_outcome.py`` so chat-history
schemas can embed ``TurnOutcome`` without importing copilot business logic.
This module imports both the schema types and ``TurnIntentMode`` — the only
direction allowed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

from skyvern.forge.sdk.copilot.signature import compute_signature
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome

LOG = structlog.get_logger()

_RESPONSE_KIND_BY_MODE: dict[TurnIntentMode, ResponseKind] = {
    TurnIntentMode.BUILD: ResponseKind.BUILD,
    TurnIntentMode.EDIT: ResponseKind.BUILD,
    TurnIntentMode.DRAFT_ONLY: ResponseKind.BUILD,
    TurnIntentMode.CLARIFY: ResponseKind.CLARIFY,
    TurnIntentMode.UNKNOWN: ResponseKind.CLARIFY,
    TurnIntentMode.DIAGNOSE: ResponseKind.DIAGNOSE,
    TurnIntentMode.DOCS_ANSWER: ResponseKind.DIAGNOSE,
    TurnIntentMode.REFUSE: ResponseKind.REFUSE,
}

# Catches the "added a TurnIntentMode but forgot to map it" foot-gun at import
# time rather than letting the new mode silently fall through to CLARIFY.
# Raises explicitly (not ``assert``) so the guard survives ``python -O``.
_missing_modes = set(TurnIntentMode) - set(_RESPONSE_KIND_BY_MODE)
if _missing_modes:
    raise RuntimeError(f"_RESPONSE_KIND_BY_MODE missing entries for: {sorted(m.value for m in _missing_modes)}")


def derive_response_kind(turn_intent: TurnIntent | None) -> ResponseKind:
    """Closed mapping. ``RECOVER`` is set only by the enforcement guard."""
    mode = getattr(turn_intent, "mode", None)
    if isinstance(mode, TurnIntentMode):
        return _RESPONSE_KIND_BY_MODE[mode]
    return ResponseKind.CLARIFY


def _dedup_signatures(signatures: Iterable[str]) -> list[str]:
    return sorted({sig for sig in signatures if isinstance(sig, str) and sig})


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
