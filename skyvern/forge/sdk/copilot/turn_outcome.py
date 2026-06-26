"""Builders + closed `TurnIntentMode -> ResponseKind` mapping for `TurnOutcome`.

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

REPEATED_REPLY_ESCALATION_TEMPLATES: dict[ResponseKind, str] = {
    ResponseKind.BUILD: (
        "I've offered the same change twice and it isn't moving us forward."
        " Tell me one concrete edit you want to make to the workflow"
        " (e.g. a step to add, a page to open, a field to extract) and I'll"
        " build that exact thing."
    ),
    ResponseKind.CLARIFY: (
        "I've asked the same clarification twice. Let me try a different"
        " angle — please confirm the single next concrete step you'd like me"
        " to take, and I'll act on that."
    ),
    ResponseKind.DIAGNOSE: (
        "I've explained that the same way twice. Either the explanation"
        " isn't matching what you're seeing, or I'm missing context — could"
        " you share what you actually see, or paste the exact error, so I can"
        " try a different angle?"
    ),
    ResponseKind.REFUSE: (
        "I've refused that the same way twice. If you want me to reconsider,"
        " tell me what's different about this case and I'll re-evaluate."
    ),
}


HANDOFF_REPLY = (
    "I haven't been able to help with this through repeated attempts. Please"
    " rephrase the request with more specifics, or use the Help link so a"
    " teammate can take a closer look."
)


def escalation_reply_for(attempted_kind: ResponseKind) -> str:
    return REPEATED_REPLY_ESCALATION_TEMPLATES.get(
        attempted_kind, REPEATED_REPLY_ESCALATION_TEMPLATES[ResponseKind.CLARIFY]
    )


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
    """Centralized post-output guard. Returns ``(final_text, outcome)``.

    When the signature of ``final_text`` is in ``blocked_signatures``, rewrites
    to the escalation template keyed by ``attempted_kind`` and returns a
    ``RECOVER`` outcome that records the original signature in its
    ``blocked_signatures`` so the ban survives to the next turn. If the
    escalation template itself collides with an already-banned signature
    (a back-to-back ``RECOVER`` loop), falls back to a generic hand-off so
    the system cannot self-amplify on the same recovery text. Otherwise
    returns the original text with a record carrying the inherited bans
    forward. The rewritten signature is also added to ``blocked_signatures``
    so a future turn cannot re-emit the same escalation text.

    Pass ``turn_intent`` and ``tool_calls`` to preserve trace metadata on
    the outcome; otherwise the minimal-shape builder is used.
    """
    inherited = list(blocked_signatures)
    tool_calls_list = list(tool_calls)
    original_signature = compute_signature(final_text)
    if inherited and original_signature in inherited:
        rewritten = escalation_reply_for(attempted_kind)
        rewritten_signature = compute_signature(rewritten)
        if rewritten_signature in inherited:
            rewritten = HANDOFF_REPLY
            rewritten_signature = compute_signature(rewritten)
        intent_summary: dict[str, Any] = {}
        if turn_intent is not None:
            try:
                intent_summary = dict(turn_intent.to_trace_data())
            except Exception as exc:
                LOG.warning(
                    "Failed to serialize TurnIntent trace data for RECOVER outcome; using empty dict",
                    exc_info=exc,
                )
        return rewritten, TurnOutcome(
            turn_intent_summary=intent_summary,
            response_kind=ResponseKind.RECOVER,
            reason_code=IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON,
            normalized_reply_signature=rewritten_signature,
            tool_calls=[str(c) for c in tool_calls_list if c],
            terminal_reason=IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON,
            blocked_signatures=_dedup_signatures([*inherited, original_signature, rewritten_signature]),
        )
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


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _schema_incompatibility_summary(ctx: Any) -> dict[str, Any] | None:
    incompatibility = getattr(ctx, "latest_schema_incompatibility", None)
    to_summary = getattr(incompatibility, "to_summary_dict", None)
    if not callable(to_summary):
        return None
    summary = to_summary()
    return summary if isinstance(summary, dict) else None


def derive_copilot_code_mode_diagnostics(ctx: Any) -> dict[str, Any]:
    turn_halt_kind = getattr(getattr(ctx, "turn_halt", None), "kind", None)
    turn_halt_kind_value = getattr(turn_halt_kind, "value", turn_halt_kind)
    pending_capability = _string_or_none(getattr(ctx, "code_native_pending_capability", None))
    return {
        "copilot_last_code_build_failed": bool(
            getattr(ctx, "last_test_ok", None) is False or getattr(ctx, "last_failed_workflow_yaml", None)
        ),
        "copilot_repair_ceiling_hit": turn_halt_kind_value == "repair_ceiling_reached",
        "copilot_pending_capability": pending_capability,
        "copilot_schema_incompatibility": _schema_incompatibility_summary(ctx),
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
