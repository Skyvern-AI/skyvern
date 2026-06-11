"""Structured tool-blocker signal."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE

BlockerKind = Literal[
    "authority_denied",
    "loop_detected",
    "phase_gated",
    "tool_error",
    "missing_required_context",
]

RecoveryHint = Literal[
    "retry_with_different_tool",
    "ask_user_clarifying",
    "report_blocker_to_user",
    "stop",
]

LOG = structlog.get_logger()


# Matched case-insensitively. Imperative variants are narrow ("do not run" etc.) so plain "do not worry" prose doesn't false-positive.
_LEAK_DENY_TOKENS: tuple[str, ...] = (
    "safe_reason_code",
    "LOOP DETECTED:",
    "recovery_hint=",
    "do not run",
    "do not call",
    "do not retry",
    "do not start",
    "do not update",
    "do not fetch",
    "do not execute",
    "do not attempt",
    "don't run",
    "don't call",
    "don't retry",
    "don't fetch",
    "don't execute",
    "don't attempt",
    "never run",
    "never call",
    "never retry",
    "must not run",
    "must not call",
    "send me",
    "normal instruction",
    "like 'continue",
    'like "continue',
    "per-tool-call budget",
)

# Raw workflow-run and browser-session identifiers are internal; user-facing
# text must reference runs by what they did, never by id.
_RUN_ID_LEAK_RE = re.compile(r"\b(?:wr|pbs)_[a-z0-9_]+", re.IGNORECASE)

_INTERNAL_GUARD_TOKENS: tuple[str, ...] = (
    "per_tool_budget",
    "per-tool-call budget",
    "active_run_terminal_evidence",
    "block-running tool",
    "block running tool",
)

_INTERNAL_TOOL_NAME_TOKENS: tuple[str, ...] = (
    "update_workflow",
    "update_and_run_blocks",
    "run_blocks_and_collect_debug",
    "get_run_results",
    "inspect_page_for_composition",
    "discover_workflow_entrypoint",
    "get_browser_screenshot",
    "list_credentials",
)


def contains_internal_machinery_leak(value: str | None) -> bool:
    """String-level terminal-output invariant: user-facing text carries no raw
    run ids, internal guard tokens, or agent-directed tool references."""
    if not isinstance(value, str) or not value:
        return False
    if _RUN_ID_LEAK_RE.search(value):
        return True
    lowered = value.lower()
    if any(token in lowered for token in _INTERNAL_GUARD_TOKENS):
        return True
    return any(token in lowered for token in _INTERNAL_TOOL_NAME_TOKENS)


def assert_clean_user_facing_text(value: str, *, blocked_tool: str | None = None) -> None:
    lowered = value.lower()
    for token in _LEAK_DENY_TOKENS:
        if token.lower() in lowered:
            raise ValueError(f"blocker user-facing text leaked token {token!r}: {value!r}")
    if contains_internal_machinery_leak(value):
        raise ValueError(f"blocker user-facing text leaked internal machinery: {value!r}")
    if blocked_tool and blocked_tool.lower() in lowered:
        raise ValueError(f"blocker user-facing text leaked tool name {blocked_tool!r}: {value!r}")


class CopilotToolBlockerSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocker_kind: BlockerKind
    agent_steering_text: str
    user_facing_reason: str
    recovery_hint: RecoveryHint
    cleared_by_tools: frozenset[str] = Field(default_factory=frozenset)
    preserves_workflow_draft: bool = False
    renders_final_reply: bool = True

    internal_reason_code: str | None = None
    blocked_tool: str | None = None
    classifier_mode: str | None = None
    exception_type: str | None = None
    # `Mapping` (not `dict`) signals the immutability contract; `frozen=True` does not freeze the container.
    extra: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_user_facing_clean(self) -> CopilotToolBlockerSignal:
        assert_clean_user_facing_text(self.user_facing_reason, blocked_tool=self.blocked_tool)
        if not isinstance(self.extra, MappingProxyType):
            object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))
        return self

    @field_serializer("extra")
    def _serialize_extra(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


def build_llm_tool_error_payload(signal: CopilotToolBlockerSignal) -> str:
    return signal.agent_steering_text


def to_trace_data(signal: CopilotToolBlockerSignal) -> dict[str, Any]:
    return {
        "blocker_kind": signal.blocker_kind,
        "recovery_hint": signal.recovery_hint,
        "cleared_by_tools": sorted(signal.cleared_by_tools),
        "renders_final_reply": signal.renders_final_reply,
        "internal_reason_code": signal.internal_reason_code,
        "blocked_tool": signal.blocked_tool,
        "classifier_mode": signal.classifier_mode,
        "exception_type": signal.exception_type,
        "extra": dict(signal.extra),
    }


@dataclass(frozen=True)
class LoopBlockerEvidence:
    outcome_gate_reason: str | None = None
    anti_bot_blocked: bool = False
    has_draft: bool = False


class _LoopEvidenceCtx(Protocol):
    last_outcome_gate_reason: str | None
    last_test_anti_bot: str | None
    staged_workflow: Any | None
    staged_workflow_yaml: str | None
    has_staged_proposal: bool


class _BlockerSignalCtx(_LoopEvidenceCtx, Protocol):
    blocker_signal: CopilotToolBlockerSignal | None
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None
    tool_blocker_signals: list[CopilotToolBlockerSignal]


def loop_blocker_evidence_from_ctx(ctx: _LoopEvidenceCtx) -> LoopBlockerEvidence:
    return LoopBlockerEvidence(
        outcome_gate_reason=ctx.last_outcome_gate_reason,
        anti_bot_blocked=bool(ctx.last_test_anti_bot),
        has_draft=ctx.staged_workflow is not None
        or ctx.staged_workflow_yaml is not None
        or bool(ctx.has_staged_proposal),
    )


_LOOP_PROGRESS_TOOL_SUCCESS_REASON_CODES = frozenset(
    {
        "loop_detected_credential_or_parameter_misconfig",
        "loop_detected_repeated_failed_step",
        "loop_detected_generic",
    }
)
_LOOP_PROGRESS_TOOLS = frozenset(
    {
        "discover_workflow_entrypoint",
        "evaluate",
        "get_browser_screenshot",
        "get_run_results",
        "inspect_page_for_composition",
        "navigate_browser",
        "run_blocks_and_collect_debug",
        "update_and_run_blocks",
        "update_workflow",
    }
)
_ACTIVE_TERMINAL_REPLACEABLE_REASON_CODES = frozenset({"tool_error_per_tool_budget_rerun"})


def _should_stash_over_existing(
    existing: CopilotToolBlockerSignal | None,
    incoming: CopilotToolBlockerSignal,
) -> bool:
    if not isinstance(existing, CopilotToolBlockerSignal):
        return True
    if (
        incoming.internal_reason_code == ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
        and existing.internal_reason_code in _ACTIVE_TERMINAL_REPLACEABLE_REASON_CODES
    ):
        return True
    return False


def _tool_success_clears_signal(signal: CopilotToolBlockerSignal, succeeded_tool_name: str) -> bool:
    if succeeded_tool_name in signal.cleared_by_tools:
        return True
    if signal.internal_reason_code == "loop_detected_consecutive_same_tool":
        return True
    if signal.internal_reason_code in _LOOP_PROGRESS_TOOL_SUCCESS_REASON_CODES:
        return succeeded_tool_name in _LOOP_PROGRESS_TOOLS
    return False


def maybe_clear_blocker_signal_on_tool_success(ctx: _BlockerSignalCtx, succeeded_tool_name: str) -> None:
    signal = getattr(ctx, "blocker_signal", None)
    if isinstance(signal, CopilotToolBlockerSignal) and _tool_success_clears_signal(signal, succeeded_tool_name):
        ctx.blocker_signal = None


def clear_blocker_signal_for_reason_codes(ctx: _BlockerSignalCtx, internal_reason_codes: frozenset[str]) -> None:
    signal = getattr(ctx, "blocker_signal", None)
    if isinstance(signal, CopilotToolBlockerSignal) and signal.internal_reason_code in internal_reason_codes:
        ctx.blocker_signal = None


def stash_blocker_signal(ctx: _BlockerSignalCtx, signal: CopilotToolBlockerSignal) -> str:
    """Mostly first-wins stash + observability log; returns the LLM-visible payload."""
    ctx.latest_tool_blocker_signal = signal
    # Keep the defensive guard for tests and partial context shims even though
    # real Copilot contexts type this field as a list.
    history = getattr(ctx, "tool_blocker_signals", None)
    if not isinstance(history, list):
        history = []
        ctx.tool_blocker_signals = history
    history.append(signal)
    if len(history) > 20:
        del history[:-20]
    existing = getattr(ctx, "blocker_signal", None)
    stashed = _should_stash_over_existing(existing, signal)
    if stashed:
        ctx.blocker_signal = signal
    extra: dict[str, Any] = {"stashed": stashed}
    if not stashed and isinstance(existing, CopilotToolBlockerSignal):
        extra["existing_reason_code"] = existing.internal_reason_code
        extra["existing_blocker_kind"] = existing.blocker_kind
    elif stashed and isinstance(existing, CopilotToolBlockerSignal):
        extra["replaced_reason_code"] = existing.internal_reason_code
        extra["replaced_blocker_kind"] = existing.blocker_kind
    LOG.info("copilot tool blocker signal", **extra, **to_trace_data(signal))
    if not stashed:
        refresh_held_loop_blocker_evidence(ctx)
    return build_llm_tool_error_payload(signal)


_LOOP_CREDENTIAL_TEMPLATE = (
    "I couldn't run this with the current credential or parameter setup. Update them and ask me to try again."
)
_LOOP_BRANCH_COPY: dict[str, tuple[str, str]] = {
    "loop_detected_repeated_failed_step": (
        "I retried without making progress.",
        "Tell me what to change and I'll try a different approach.",
    ),
    "loop_detected_consecutive_same_tool": (
        "I'm stuck retrying the same step.",
        "Tell me what to change and I'll try a different approach.",
    ),
    "loop_detected_generic": (
        "I couldn't keep going on this turn.",
        "Tell me what to change and I'll try again.",
    ),
}
_LOOP_ANTI_BOT_BLOCKER_COPY = "The site's verification challenge was still keeping the submit/search control disabled."
_LOOP_VERDICT_MAX_CHARS = 240
_LOOP_VERDICT_FAILED_PREFIX = "failed:"
# Fixed agent-advice tails appended by the recorded-reason producers; repair
# vocabulary aimed at the agent, never user information.
_LOOP_VERDICT_ADVICE_MARKERS = ("Add an end-state confirmation", "Re-run to verify the outcome")
# Raw runtime-error text carries none of the deny-listed tokens, so the verdict
# tier is dropped on error-output shapes rather than relying on the token gate.
_LOOP_VERDICT_RAW_ERROR_RE = re.compile(
    r"={3,}"
    r"|\b[A-Z][a-z]\w*(?:Error|Exception)\b"
    r"|(?i:\btraceback \(most recent call last\))"
    r"|(?i:\bfailed to execute\b)"
    r"|(?i:\btimeout \d+\s*ms\b)"
)


def _sanitize_loop_verdict_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    text = " ".join(reason.split())
    if _LOOP_VERDICT_RAW_ERROR_RE.search(text):
        return None
    if text.lower().startswith(_LOOP_VERDICT_FAILED_PREFIX):
        text = text[len(_LOOP_VERDICT_FAILED_PREFIX) :].strip()
    for marker in _LOOP_VERDICT_ADVICE_MARKERS:
        index = text.find(marker)
        if index != -1:
            text = text[:index].strip()
    if len(text) > _LOOP_VERDICT_MAX_CHARS:
        text = text[:_LOOP_VERDICT_MAX_CHARS].rstrip() + "…"
    if not text:
        return None
    if text[-1] not in ".!?…":
        text += "."
    return text


def compose_loop_blocker_user_facing_reason(
    internal_reason_code: str | None,
    evidence: LoopBlockerEvidence | None,
    *,
    blocked_tool: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Deterministic tier composition; leaky candidates degrade tier-by-tier to
    the branch template instead of raising out of signal construction."""
    draft_tier = ("draft",) if evidence is not None and evidence.has_draft else ()
    if internal_reason_code == "loop_detected_credential_or_parameter_misconfig":
        return _LOOP_CREDENTIAL_TEMPLATE, draft_tier
    framing, ask = _LOOP_BRANCH_COPY.get(internal_reason_code or "", _LOOP_BRANCH_COPY["loop_detected_generic"])
    template = f"{framing} {ask}"
    if evidence is None:
        return template, draft_tier
    verdict = _sanitize_loop_verdict_reason(evidence.outcome_gate_reason)
    blocker = _LOOP_ANTI_BOT_BLOCKER_COPY if evidence.anti_bot_blocked else None
    for include_verdict in (True, False):
        parts = [framing]
        tiers: list[str] = []
        if include_verdict and verdict is not None:
            parts.append(verdict)
            tiers.append("verdict")
        if blocker is not None:
            parts.append(blocker)
            tiers.append("anti_bot")
        parts.append(ask)
        candidate = " ".join(parts)
        if candidate == template:
            return template, draft_tier
        try:
            assert_clean_user_facing_text(candidate, blocked_tool=blocked_tool)
        except ValueError:
            continue
        return candidate, (*tiers, *draft_tier)
    return template, draft_tier


def build_loop_blocker_signal(
    loop_message: str,
    *,
    tool_name: str,
    evidence: LoopBlockerEvidence | None = None,
) -> CopilotToolBlockerSignal:
    # Category markers come first because credential/parameter messages also contain "has already failed".
    if "with CREDENTIAL_ERROR" in loop_message or "with PARAMETER_BINDING_ERROR" in loop_message:
        internal = "loop_detected_credential_or_parameter_misconfig"
        recovery_hint: RecoveryHint = "ask_user_clarifying"
    elif "has already failed" in loop_message:
        internal = "loop_detected_repeated_failed_step"
        recovery_hint = "report_blocker_to_user"
    elif "has been called" in loop_message:
        internal = "loop_detected_consecutive_same_tool"
        recovery_hint = "report_blocker_to_user"
    else:
        internal = "loop_detected_generic"
        recovery_hint = "report_blocker_to_user"
    user_facing, tiers = compose_loop_blocker_user_facing_reason(internal, evidence, blocked_tool=tool_name)
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=loop_message,
        user_facing_reason=user_facing,
        recovery_hint=recovery_hint,
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=evidence is not None and evidence.has_draft,
        internal_reason_code=internal,
        blocked_tool=tool_name,
        extra={"loop_evidence_tiers": list(tiers)} if tiers else {},
    )


def refresh_held_loop_blocker_evidence(ctx: _BlockerSignalCtx) -> None:
    """Recompose the held loop signal's user-facing copy, draft flag, and extra
    from current ctx evidence; identity and lifecycle fields never change."""
    held = getattr(ctx, "blocker_signal", None)
    if not isinstance(held, CopilotToolBlockerSignal) or held.blocker_kind != "loop_detected":
        return
    evidence = loop_blocker_evidence_from_ctx(ctx)
    user_facing, tiers = compose_loop_blocker_user_facing_reason(
        held.internal_reason_code, evidence, blocked_tool=held.blocked_tool
    )
    if not tiers:
        return
    if user_facing == held.user_facing_reason and evidence.has_draft == held.preserves_workflow_draft:
        return
    try:
        refreshed = CopilotToolBlockerSignal(
            blocker_kind=held.blocker_kind,
            agent_steering_text=held.agent_steering_text,
            user_facing_reason=user_facing,
            recovery_hint=held.recovery_hint,
            cleared_by_tools=held.cleared_by_tools,
            preserves_workflow_draft=evidence.has_draft,
            renders_final_reply=held.renders_final_reply,
            internal_reason_code=held.internal_reason_code,
            blocked_tool=held.blocked_tool,
            classifier_mode=held.classifier_mode,
            exception_type=held.exception_type,
            extra={"loop_evidence_tiers": list(tiers)} if tiers else {},
        )
    except ValueError:
        return
    ctx.blocker_signal = refreshed
    LOG.info("copilot loop blocker signal evidence refreshed", **to_trace_data(refreshed))
