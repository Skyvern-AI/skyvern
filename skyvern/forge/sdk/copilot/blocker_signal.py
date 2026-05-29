"""Structured tool-blocker signal."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

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
)


def assert_clean_user_facing_text(value: str, *, blocked_tool: str | None = None) -> None:
    lowered = value.lower()
    for token in _LEAK_DENY_TOKENS:
        if token.lower() in lowered:
            raise ValueError(f"blocker user-facing text leaked token {token!r}: {value!r}")
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


class _BlockerSignalCtx(Protocol):
    blocker_signal: CopilotToolBlockerSignal | None


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
    """First-wins stash + observability log; returns the LLM-visible payload."""
    existing = getattr(ctx, "blocker_signal", None)
    stashed = not isinstance(existing, CopilotToolBlockerSignal)
    if stashed:
        ctx.blocker_signal = signal
    extra: dict[str, Any] = {"stashed": stashed}
    if not stashed and isinstance(existing, CopilotToolBlockerSignal):
        extra["existing_reason_code"] = existing.internal_reason_code
        extra["existing_blocker_kind"] = existing.blocker_kind
    LOG.info("copilot tool blocker signal", **extra, **to_trace_data(signal))
    return build_llm_tool_error_payload(signal)


def build_loop_blocker_signal(loop_message: str, *, tool_name: str) -> CopilotToolBlockerSignal:
    # Category markers come first because credential/parameter messages also contain "has already failed".
    if "with CREDENTIAL_ERROR" in loop_message or "with PARAMETER_BINDING_ERROR" in loop_message:
        internal = "loop_detected_credential_or_parameter_misconfig"
        user_facing = (
            "I couldn't run this with the current credential or parameter setup. Update them and ask me to try again."
        )
        recovery_hint: RecoveryHint = "ask_user_clarifying"
    elif "has already failed" in loop_message:
        internal = "loop_detected_repeated_failed_step"
        user_facing = "I retried without making progress. Tell me what to change and I'll try a different approach."
        recovery_hint = "report_blocker_to_user"
    elif "has been called" in loop_message:
        internal = "loop_detected_consecutive_same_tool"
        user_facing = "I'm stuck retrying the same step. Tell me what to change and I'll try a different approach."
        recovery_hint = "report_blocker_to_user"
    else:
        internal = "loop_detected_generic"
        user_facing = "I couldn't keep going on this turn. Tell me what to change and I'll try again."
        recovery_hint = "report_blocker_to_user"
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=loop_message,
        user_facing_reason=user_facing,
        recovery_hint=recovery_hint,
        cleared_by_tools=frozenset(),
        internal_reason_code=internal,
        blocked_tool=tool_name,
    )
