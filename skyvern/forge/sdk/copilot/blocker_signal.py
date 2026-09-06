"""Structured tool-blocker signal."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import BlockRunIdentity

BlockerKind = Literal[
    "authority_denied",
    "tool_error",
]

RecoveryHint = Literal[
    "retry_with_different_tool",
    "ask_user_clarifying",
    "report_blocker_to_user",
    "stop",
]

LOG = structlog.get_logger()
BROWSER_SESSION_LOST_BLOCKER_REASON_CODE = "tool_error_browser_session_lost"


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
    "block-running tool",
    "block running tool",
)

_INTERNAL_TOOL_NAME_TOKENS: tuple[str, ...] = (
    "update_workflow",
    "update_and_run_blocks",
    "edit_block_and_run",
    "run_blocks_and_collect_debug",
    "get_run_results",
    "inspect_page_for_composition",
    "discover_workflow_entrypoint",
    "search_web",
    "get_browser_screenshot",
    "list_credentials",
    "list_integrations",
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
class TerminalEvidence:
    anti_bot_blocked: bool = False
    has_draft: bool = False


class _TerminalEvidenceCtx(Protocol):
    last_test_anti_bot: str | None
    staged_workflow: Any | None
    staged_workflow_yaml: str | None
    has_staged_proposal: bool


class _BlockerSignalCtx(_TerminalEvidenceCtx, Protocol):
    blocker_signal: CopilotToolBlockerSignal | None
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None
    tool_blocker_signals: list[CopilotToolBlockerSignal]


class _ActiveRunEvidenceResetCtx(Protocol):
    last_run_blocks_workflow_run_id: str | None
    last_successful_run_blocks_workflow_run_id: str | None
    last_run_blocks_browser_session_id: str | None
    recorded_persisted_block_run_workflow_run_id: str | None
    last_run_blocks_block_ids: list[str]
    last_run_blocks_block_labels: list[str]
    last_run_outcome: Any | None
    last_run_outcome_block_labels: list[str]
    last_test_anti_bot: str | None
    completion_verification_result: Any | None
    outcome_verification_trace_snapshot: dict[str, Any]
    pre_run_page_reference: Any | None
    registered_artifact_evidence: Any | None
    post_run_page_observation_tool: str | None
    post_run_page_observation_url: str | None
    post_run_page_observation_workflow_run_id: str | None
    post_run_page_observation_after_failed_test: bool
    post_run_page_observation_generation: int
    post_run_current_page_inspection_workflow_run_id: str | None
    block_state_map: dict[str, str]
    block_started_at_map: dict[str, str]
    block_ended_at_map: dict[str, str]
    block_run_identity_map: dict[str, BlockRunIdentity]


def terminal_evidence_from_ctx(ctx: _TerminalEvidenceCtx) -> TerminalEvidence:
    return TerminalEvidence(
        anti_bot_blocked=bool(getattr(ctx, "last_test_anti_bot", None)),
        has_draft=(
            getattr(ctx, "staged_workflow", None) is not None
            or getattr(ctx, "staged_workflow_yaml", None) is not None
            or bool(getattr(ctx, "has_staged_proposal", False))
            or getattr(ctx, "last_workflow", None) is not None
            or getattr(ctx, "last_workflow_yaml", None) is not None
        ),
    )


def clear_active_run_evidence_on_workflow_edit(ctx: _ActiveRunEvidenceResetCtx) -> None:
    """Detach the edited draft from prior-run pointers without erasing the run archive."""
    ctx.last_run_blocks_workflow_run_id = None
    ctx.last_successful_run_blocks_workflow_run_id = None
    ctx.last_run_blocks_browser_session_id = None
    ctx.recorded_persisted_block_run_workflow_run_id = None
    ctx.last_run_blocks_block_ids = []
    ctx.last_run_blocks_block_labels = []
    ctx.last_run_outcome = None
    ctx.last_run_outcome_block_labels = []
    ctx.last_test_anti_bot = None
    ctx.completion_verification_result = None
    ctx.outcome_verification_trace_snapshot = {}
    ctx.pre_run_page_reference = None
    ctx.registered_artifact_evidence = None
    ctx.post_run_page_observation_tool = None
    ctx.post_run_page_observation_url = None
    ctx.post_run_page_observation_workflow_run_id = None
    ctx.post_run_page_observation_after_failed_test = False
    ctx.post_run_page_observation_generation = 0
    ctx.post_run_current_page_inspection_workflow_run_id = None
    ctx.block_state_map = {}
    ctx.block_started_at_map = {}
    ctx.block_ended_at_map = {}
    ctx.block_run_identity_map = {}


SCHEMA_INCOMPATIBILITY_REASON_CODE = "schema_incompatibility"
# A held blocker whose reason code is in this set must win both the rendered reply and the typed
# halt kind over a later non-terminal trip (e.g. the code-authoring churn backstop).
GENUINELY_TERMINAL_BLOCKER_REASON_CODES: frozenset[str] = frozenset({BROWSER_SESSION_LOST_BLOCKER_REASON_CODE})


def blocker_signal_is_genuinely_terminal(signal: CopilotToolBlockerSignal | None) -> bool:
    return signal is not None and signal.internal_reason_code in GENUINELY_TERMINAL_BLOCKER_REASON_CODES


def _should_stash_over_existing(existing: CopilotToolBlockerSignal | None) -> bool:
    if not isinstance(existing, CopilotToolBlockerSignal):
        return True
    return False


def _tool_success_clears_signal(signal: CopilotToolBlockerSignal, succeeded_tool_name: str) -> bool:
    if succeeded_tool_name in signal.cleared_by_tools:
        return True
    return False


def maybe_clear_blocker_signal_on_tool_success(ctx: _BlockerSignalCtx, succeeded_tool_name: str) -> None:
    signal = getattr(ctx, "blocker_signal", None)
    if isinstance(signal, CopilotToolBlockerSignal) and _tool_success_clears_signal(signal, succeeded_tool_name):
        ctx.blocker_signal = None


def clear_blocker_signal_for_reason_codes(ctx: _BlockerSignalCtx, internal_reason_codes: frozenset[str]) -> None:
    signal = getattr(ctx, "blocker_signal", None)
    if isinstance(signal, CopilotToolBlockerSignal) and signal.internal_reason_code in internal_reason_codes:
        ctx.blocker_signal = None


def clear_tool_blocker_signals_for_reason_codes(ctx: _BlockerSignalCtx, internal_reason_codes: frozenset[str]) -> None:
    clear_blocker_signal_for_reason_codes(ctx, internal_reason_codes)
    # getattr matches stash_blocker_signal's defensive read: real contexts type
    # both fields, but partial test shims may omit them.
    latest = getattr(ctx, "latest_tool_blocker_signal", None)
    if isinstance(latest, CopilotToolBlockerSignal) and latest.internal_reason_code in internal_reason_codes:
        ctx.latest_tool_blocker_signal = None
    history = getattr(ctx, "tool_blocker_signals", None)
    if isinstance(history, list):
        history[:] = [
            entry
            for entry in history
            if not (isinstance(entry, CopilotToolBlockerSignal) and entry.internal_reason_code in internal_reason_codes)
        ]


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
    stashed = _should_stash_over_existing(existing)
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
    return build_llm_tool_error_payload(signal)


CREDENTIAL_SCOUT_VERIFY_REPLY = (
    "I need to verify the saved-credential login in the browser before I can save or run this code."
)
_TERMINAL_ANTI_BOT_BLOCKER_COPY = (
    "The site's verification challenge was still keeping the submit/search control disabled."
)


def terminal_evidence_has_recorded_state(evidence: TerminalEvidence | None) -> bool:
    if evidence is None:
        return False
    return evidence.anti_bot_blocked


def compose_terminal_evidence_user_facing_reason(
    framing: str,
    ask: str,
    evidence: TerminalEvidence | None,
    *,
    blocked_tool: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    template = f"{framing} {ask}"
    draft_tier = ("draft",) if evidence is not None and evidence.has_draft else ()
    if evidence is None:
        return template, draft_tier

    tier_candidates = (("anti_bot", _TERMINAL_ANTI_BOT_BLOCKER_COPY if evidence.anti_bot_blocked else None),)
    parts = [framing]
    tiers: list[str] = []
    for tier, text in tier_candidates:
        if text is None:
            continue
        candidate = " ".join([*parts, text, ask])
        try:
            assert_clean_user_facing_text(candidate, blocked_tool=blocked_tool)
        except ValueError:
            continue
        parts.append(text)
        tiers.append(tier)
    candidate = " ".join([*parts, ask])
    if candidate == template:
        return template, draft_tier
    return candidate, (*tiers, *draft_tier)
