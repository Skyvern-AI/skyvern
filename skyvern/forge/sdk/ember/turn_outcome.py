"""Builders for the persisted per-turn narrative record.

Schema types live in ``schemas/ember_turn_outcome.py`` so chat-history
schemas can embed ``TurnOutcome`` without importing copilot business logic.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import structlog

from skyvern.forge.sdk.ember.budget_expiry import BudgetExpiryState
from skyvern.forge.sdk.ember.signature import compute_signature
from skyvern.forge.sdk.schemas.ember_turn_outcome import ConnectedAccountChoice, ResponseKind, TurnOutcome

LOG = structlog.get_logger()

IDENTICAL_REPLY_BLOCKED_TERMINAL_REASON = "identical_reply_blocked"
# Agent-loop exits spell a user cancel "cancel"; the route layer spells its own "user_cancelled"
# and stamps RECOVER itself, so that value never reaches the labelers here. See
# ROUTE_OWNED_TERMINAL_REASONS in dev_scripts/replay_turn_outcome_kind.py for the full split.
CANCEL_TERMINAL_REASON = "cancel"
UNEXPECTED_ERROR_TERMINAL_REASON = "unexpected_error"
EmberComposerMode = Literal["build", "code"]


def selected_connected_account_id(outcome: TurnOutcome | None, current_user_message: str) -> str | None:
    """Return the exact account selected from the prior server-owned choice set."""
    choices = outcome.connected_account_choices if outcome is not None else None
    if not choices:
        return None
    return next(
        (choice.connection_id for choice in choices if choice.connection_id == current_user_message),
        None,
    )


@dataclass(frozen=True)
class AccountPickTestResume:
    proposal_owner_turn_id: str
    proposal_revision: int
    proposal_workflow_run_id: str | None
    connection_id: str
    picker_canvas_fingerprint: str | None = None


def account_pick_test_resume(
    outcome: TurnOutcome | None, current_user_message: str, explicit_selection: str | None
) -> AccountPickTestResume | None:
    """A pick answering the picker a Test end-to-end turn offered, bound to the proposal that turn left stored."""
    if outcome is None or outcome.test_proposal_owner_turn_id is None or outcome.test_proposal_revision is None:
        return None
    pick = selected_connected_account_id(outcome, current_user_message)
    if pick is None or explicit_selection not in (None, pick):
        return None
    return AccountPickTestResume(
        outcome.test_proposal_owner_turn_id,
        outcome.test_proposal_revision,
        outcome.test_proposal_workflow_run_id,
        pick,
        outcome.test_proposal_canvas_fingerprint,
    )


class CarriedAccountChoiceFields(TypedDict, total=False):
    connected_account_choices: list[ConnectedAccountChoice] | None
    test_proposal_owner_turn_id: str | None
    test_proposal_revision: int | None
    test_proposal_workflow_run_id: str | None
    test_proposal_canvas_fingerprint: str | None


def carried_account_choice_fields(prior_turn_outcome: TurnOutcome | None) -> CarriedAccountChoiceFields:
    if prior_turn_outcome is None:
        return {}
    return {
        "connected_account_choices": prior_turn_outcome.connected_account_choices,
        "test_proposal_owner_turn_id": prior_turn_outcome.test_proposal_owner_turn_id,
        "test_proposal_revision": prior_turn_outcome.test_proposal_revision,
        "test_proposal_workflow_run_id": prior_turn_outcome.test_proposal_workflow_run_id,
        "test_proposal_canvas_fingerprint": prior_turn_outcome.test_proposal_canvas_fingerprint,
    }


def connected_account_choice_context(
    outcome: TurnOutcome | None,
    *,
    explicit_selected_connection_id: str | None = None,
    explicit_selected_connection_scopes: Sequence[str] = (),
    explicit_selected_connection_provider: str | None = None,
    from_pending_proposal: bool = False,
) -> str:
    """Expose the validated picker selection as context; authorization remains in request policy."""
    choices = outcome.connected_account_choices if outcome is not None else None
    if not choices and explicit_selected_connection_id is None:
        return ""
    payload: dict[str, Any] = {}
    if choices:
        payload["connected_account_choices"] = [choice.model_dump(mode="json") for choice in choices]
    if explicit_selected_connection_id is not None:
        payload["selection_source"] = "pending_proposal_binding" if from_pending_proposal else "user_picker"
        payload["selected_connection_id"] = explicit_selected_connection_id
        payload["selected_connection"] = {
            "connection_id": explicit_selected_connection_id,
            "provider": explicit_selected_connection_provider,
            "state": "active",
            # A Nango account reports no scopes, so they read as unknown rather than none.
            "scopes_granted": None
            if explicit_selected_connection_provider == "nango"
            else list(explicit_selected_connection_scopes),
        }
    return json.dumps(payload, separators=(",", ":"))


def stopped_exit_response_kind(terminal_reason: str | None) -> ResponseKind:
    """A user cancel halted the turn, so it is recorded as a stop; other turn-end exits keep the clarify label."""
    return ResponseKind.RECOVER if terminal_reason == CANCEL_TERMINAL_REASON else ResponseKind.CLARIFY


def apply_repeated_reply_guard(
    *,
    final_text: str,
    attempted_kind: ResponseKind,
    blocked_signatures: Iterable[str],
    reason_code: str = "",
    terminal_reason: str | None = None,
) -> tuple[str, TurnOutcome]:
    """Centralized post-output record. Returns ``(final_text, outcome)``.

    The model's reply is never rewritten: a repeat is the turn's true state, and replacing it with
    escalation prose both invents words the model did not say and — when it also minted a terminal
    reason — ended turns over a chat-presentation concern. Signatures are still recorded and carried
    forward so repetition stays visible downstream.
    """
    inherited = list(blocked_signatures)
    original_signature = compute_signature(final_text)
    if inherited and original_signature in inherited:
        LOG.info("ember_repeated_reply_observed", normalized_reply_signature=original_signature)
    return final_text, build_turn_outcome(
        final_text,
        response_kind=attempted_kind,
        reason_code=reason_code,
        terminal_reason=terminal_reason,
        inherited_blocked_signatures=inherited,
    )


def _dedup_signatures(signatures: Iterable[str]) -> list[str]:
    return sorted({sig for sig in signatures if isinstance(sig, str) and sig})


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def derive_ember_code_mode_diagnostics(ctx: Any) -> dict[str, Any]:
    pending_capability = _string_or_none(getattr(ctx, "code_native_pending_capability", None))
    return {
        "copilot_last_code_build_failed": bool(
            getattr(ctx, "last_test_ok", None) is False or getattr(ctx, "last_failed_workflow_yaml", None)
        ),
        "copilot_pending_capability": pending_capability,
    }


def with_ember_code_mode_diagnostics(outcome: TurnOutcome, ctx: Any) -> TurnOutcome:
    return outcome.model_copy(update=derive_ember_code_mode_diagnostics(ctx))


def with_budget_expiry(outcome: TurnOutcome, state: BudgetExpiryState) -> TurnOutcome:
    if state.source is None:
        return outcome
    return outcome.model_copy(
        update={
            "terminal_reason": "timeout" if state.source == "deadline" else "max_turns",
            "budget_expired": True,
            "budget_expiry_source": state.source,
            "budget_expiry_report_produced": state.report_produced,
            "budget_expiry_staged_draft_id": state.staged_draft_id,
            "drain_fingerprint": state.drain_fingerprint,
        }
    )


def with_ember_code_mode_metadata(
    outcome: TurnOutcome,
    *,
    effective_mode: EmberComposerMode | None,
    code_available: bool,
    turn_id: str | None,
) -> TurnOutcome:
    return outcome.model_copy(
        update={
            "copilot_runtime": "agent",
            "copilot_effective_mode": effective_mode,
            "copilot_code_available": code_available,
            "copilot_turn_id": turn_id,
        }
    )


def with_turn_id(outcome: TurnOutcome, turn_id: str) -> TurnOutcome:
    return outcome.model_copy(update={"copilot_turn_id": turn_id})


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
    response_kind: ResponseKind,
    reason_code: str = "",
    tool_calls: Iterable[str] = (),
    terminal_reason: str | None = None,
    inherited_blocked_signatures: Iterable[str] = (),
    extra_blocked_signatures: Iterable[str] = (),
) -> TurnOutcome:
    """Used by the translation path. Merges inherited + extra blocked signatures
    so the enforcement guard can record the original signature it just blocked."""
    return TurnOutcome(
        response_kind=response_kind,
        reason_code=reason_code,
        normalized_reply_signature=compute_signature(final_text),
        tool_calls=[str(call) for call in tool_calls if call],
        terminal_reason=terminal_reason,
        blocked_signatures=_dedup_signatures(list(inherited_blocked_signatures) + list(extra_blocked_signatures)),
    )
