"""Structured tool-blocker signal."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
)
from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE, RecordedRunOutcome

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.turn_ownership import TurnClaimant

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


def build_output_source_unobservable_blocker_signal(
    *,
    reason_code: str,
    required_paths: Iterable[str],
    block_label: str,
) -> CopilotToolBlockerSignal:
    """Honest pre-run terminal for an output contract whose requested values have no
    observable extraction source at build time (a click-only trajectory, or an
    actuation ladder exhausted without a keyable structure). The draft is preserved."""
    paths = sorted({str(path).strip() for path in required_paths if str(path).strip()})
    path_text = ", ".join(paths)
    if reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE:
        user_facing = (
            "I can build the steps for this workflow, but the values you asked me to return"
            f"{f' ({path_text})' if path_text else ''} are not observable on the pages this run "
            "visits, so there is nothing for the workflow to read them from. I've kept the draft; "
            "tell me where those values appear and I'll wire them up."
        )
    elif reason_code == OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE:
        user_facing = (
            "I rewrote this workflow several times to return the value(s) you asked for"
            f"{f' ({path_text})' if path_text else ''}, but each version left those outputs "
            "undeclared or unreturned, so the workflow can't reliably hand them back. I've kept "
            "the draft; tell me where each of those values appears and I'll declare and return them."
        )
    else:
        user_facing = (
            "I couldn't shape this workflow so it reliably returns the values you asked for"
            f"{f' ({path_text})' if path_text else ''}. I've kept the current draft; let me know "
            "where those values show up and I'll try a different structure."
        )
    if reason_code == OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE:
        agent_steer = (
            "STOP: the output contract kept failing its declaration/return coverage across repeated "
            f"re-authored candidates for required path(s) [{path_text or '(unknown)'}]. This is not "
            "repairable by re-authoring the same draft. Report the missing output declarations to the "
            "user and ask where the values appear. The prior draft is preserved; do not rerun the blocks."
        )
    else:
        agent_steer = (
            "STOP: the requested output contract has no observable extraction source in the current "
            f"trajectory for required path(s) [{path_text or '(unknown)'}]. This is not repairable by "
            "re-authoring the same draft. Report the missing source to the user and ask where the "
            "values appear. The prior draft is preserved; do not rerun the blocks."
        )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steer,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=reason_code,
        extra={
            "output_contract_terminal_reason_code": reason_code,
            "canonical_required_child_paths": paths,
            "block_label": block_label,
        },
    )


def build_definition_contract_unsatisfied_blocker_signal(
    *, unresolved_parameter_keys: Iterable[str], grounding_unresolved: bool = False
) -> CopilotToolBlockerSignal:
    parameter_keys = sorted({str(key).strip() for key in unresolved_parameter_keys if str(key).strip()})
    if parameter_keys:
        key_text = ", ".join(f"`{key}`" for key in parameter_keys)
        user_facing = (
            f"I kept the workflow draft, but I could not safely connect the current page fields to these reusable "
            f"inputs: {key_text}. No workflow run was started."
            if grounding_unresolved
            else f"I kept the workflow draft, but it cannot run because these reusable inputs are not used: "
            f"{key_text}. Connect each input to the workflow before trying again."
        )
    else:
        user_facing = (
            "I kept the workflow draft, but it does not yet satisfy the required workflow definition. "
            "Complete the missing definition requirements before trying again."
        )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "The exact candidate failed its definition contract before execution. Preserve the draft, stop this "
            "turn, and explain the unresolved reusable inputs without dispatching the candidate."
        ),
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=DEFINITION_CONTRACT_UNSATISFIED_REASON_CODE,
        extra={"unresolved_parameter_keys": parameter_keys, "grounding_unresolved": grounding_unresolved},
    )


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
    outcome_gate_reason: str | None = None
    outcome_gate_workflow_run_id: str | None = None
    recorded_run_outcome: RecordedRunOutcome | None = None
    latest_workflow_run_id: str | None = None
    anti_bot_blocked: bool = False
    has_draft: bool = False


@dataclass(frozen=True)
class LoopBlockerEvidence(TerminalEvidence):
    latest_evaluate_result_composition_steer: LoadedResultCompositionEvidence | None = None


class _LoopEvidenceCtx(Protocol):
    last_outcome_gate_reason: str | None
    last_outcome_gate_workflow_run_id: str | None
    last_run_outcome: RecordedRunOutcome | None
    last_run_blocks_workflow_run_id: str | None
    last_test_anti_bot: str | None
    staged_workflow: Any | None
    staged_workflow_yaml: str | None
    has_staged_proposal: bool
    latest_evaluate_result_composition_steer: LoadedResultCompositionEvidence | None


class _BlockerSignalCtx(_LoopEvidenceCtx, Protocol):
    blocker_signal: CopilotToolBlockerSignal | None
    # Owned here at the stash choke-point: cleared whenever the held signal changes identity,
    # re-assigned only by an owned precedence claim in turn_ownership.
    blocker_signal_claimant: TurnClaimant | None
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None
    tool_blocker_signals: list[CopilotToolBlockerSignal]


class _TerminalEvidenceResetCtx(Protocol):
    last_run_blocks_workflow_run_id: str | None
    last_successful_run_blocks_workflow_run_id: str | None
    recorded_persisted_block_run_workflow_run_id: str | None
    last_run_blocks_block_ids: list[str]
    last_run_blocks_block_labels: list[str]
    last_run_outcome: RecordedRunOutcome | None
    last_run_outcome_block_labels: list[str]
    last_outcome_gate_reason: str | None
    last_outcome_gate_workflow_run_id: str | None
    last_test_anti_bot: str | None
    delivered_unverified_terminal: bool
    delivered_unverified_workflow_run_id: str | None
    delivered_unverified_observed_outputs: dict[str, Any]
    completion_verification_result: Any | None
    outcome_verification_trace_snapshot: dict[str, Any]


def terminal_evidence_from_ctx(ctx: _LoopEvidenceCtx) -> TerminalEvidence:
    recorded = getattr(ctx, "last_run_outcome", None)
    return TerminalEvidence(
        outcome_gate_reason=getattr(ctx, "last_outcome_gate_reason", None),
        outcome_gate_workflow_run_id=getattr(ctx, "last_outcome_gate_workflow_run_id", None),
        recorded_run_outcome=recorded if isinstance(recorded, RecordedRunOutcome) else None,
        latest_workflow_run_id=getattr(ctx, "last_run_blocks_workflow_run_id", None),
        anti_bot_blocked=bool(getattr(ctx, "last_test_anti_bot", None)),
        has_draft=(
            getattr(ctx, "staged_workflow", None) is not None
            or getattr(ctx, "staged_workflow_yaml", None) is not None
            or bool(getattr(ctx, "has_staged_proposal", False))
            or getattr(ctx, "last_workflow", None) is not None
            or getattr(ctx, "last_workflow_yaml", None) is not None
        ),
    )


def clear_terminal_evidence_on_workflow_edit(ctx: _TerminalEvidenceResetCtx) -> None:
    ctx.last_run_blocks_workflow_run_id = None
    ctx.last_successful_run_blocks_workflow_run_id = None
    ctx.recorded_persisted_block_run_workflow_run_id = None
    ctx.last_run_blocks_block_ids = []
    ctx.last_run_blocks_block_labels = []
    ctx.last_run_outcome = None
    ctx.last_run_outcome_block_labels = []
    ctx.last_outcome_gate_reason = None
    ctx.last_outcome_gate_workflow_run_id = None
    ctx.last_test_anti_bot = None
    ctx.delivered_unverified_terminal = False
    ctx.delivered_unverified_workflow_run_id = None
    ctx.delivered_unverified_observed_outputs = {}
    ctx.completion_verification_result = None
    ctx.outcome_verification_trace_snapshot = {}


def loop_blocker_evidence_from_ctx(ctx: _LoopEvidenceCtx) -> LoopBlockerEvidence:
    evidence = terminal_evidence_from_ctx(ctx)
    # Older context snapshots may not carry fields added after the snapshot was created.
    result_steer = getattr(ctx, "latest_evaluate_result_composition_steer", None)
    return LoopBlockerEvidence(
        outcome_gate_reason=evidence.outcome_gate_reason,
        outcome_gate_workflow_run_id=evidence.outcome_gate_workflow_run_id,
        recorded_run_outcome=evidence.recorded_run_outcome,
        latest_workflow_run_id=evidence.latest_workflow_run_id,
        anti_bot_blocked=evidence.anti_bot_blocked,
        has_draft=evidence.has_draft,
        latest_evaluate_result_composition_steer=result_steer,
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
_TERMINAL_CHALLENGE_REPLACEABLE_REASON_CODES = frozenset({"tool_error_post_budget_challenge_result_evidence"})
SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE = "tool_error_synthesized_block_persistence_required"
UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE = "tool_error_uncovered_output_rescout_steer"
RECORDED_OUTCOME_GROUNDING_REASON_CODE = "recorded_outcome_grounding_required"
DEFINITION_CONTRACT_UNSATISFIED_REASON_CODE = "definition_contract_unsatisfied"
SCHEMA_INCOMPATIBILITY_REASON_CODE = "schema_incompatibility"
OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE = "output_contract_reject_budget_exhausted"
_OUTPUT_CONTRACT_TERMINAL_REASON_CODES = frozenset(
    {
        OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
        OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE,
    }
)

# A held blocker whose reason code is in this set must win both the rendered reply and the typed
# halt kind over a later non-terminal trip (e.g. the code-authoring churn backstop).
GENUINELY_TERMINAL_BLOCKER_REASON_CODES: frozenset[str] = frozenset(
    {
        ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
        TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        "tool_error_run_output_terminal_blocker",
        "tool_error_post_budget_challenge_blocker",
        "tool_error_challenge_gated_submit_disabled",
        "probable_site_block_stop",
        SCHEMA_INCOMPATIBILITY_REASON_CODE,
        OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
        OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE,
        "advisory_dispatch_stalled",
        DEFINITION_CONTRACT_UNSATISFIED_REASON_CODE,
        "repair_ceiling_reached",
    }
)


def blocker_signal_is_genuinely_terminal(signal: CopilotToolBlockerSignal | None) -> bool:
    return signal is not None and signal.internal_reason_code in GENUINELY_TERMINAL_BLOCKER_REASON_CODES


def _should_stash_over_existing(
    existing: CopilotToolBlockerSignal | None,
    incoming: CopilotToolBlockerSignal,
) -> bool:
    if not isinstance(existing, CopilotToolBlockerSignal):
        return True
    if (
        incoming.internal_reason_code in _OUTPUT_CONTRACT_TERMINAL_REASON_CODES
        and existing.blocker_kind == "loop_detected"
    ):
        return True
    if (
        incoming.internal_reason_code == ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
        and existing.internal_reason_code in _ACTIVE_TERMINAL_REPLACEABLE_REASON_CODES
    ):
        return True
    if (
        incoming.internal_reason_code == "tool_error_post_budget_challenge_blocker"
        and existing.internal_reason_code in _TERMINAL_CHALLENGE_REPLACEABLE_REASON_CODES
    ):
        return True
    if (
        incoming.internal_reason_code == RECORDED_OUTCOME_GROUNDING_REASON_CODE
        and existing.blocker_kind == "tool_error"
        and not existing.renders_final_reply
    ):
        return True
    if (
        existing.internal_reason_code == RECORDED_OUTCOME_GROUNDING_REASON_CODE
        and not existing.renders_final_reply
        and incoming.renders_final_reply
    ):
        return True
    if (
        existing.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        and not existing.renders_final_reply
        and incoming.renders_final_reply
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
        ctx.blocker_signal_claimant = None


def clear_blocker_signal_for_reason_codes(ctx: _BlockerSignalCtx, internal_reason_codes: frozenset[str]) -> None:
    signal = getattr(ctx, "blocker_signal", None)
    if isinstance(signal, CopilotToolBlockerSignal) and signal.internal_reason_code in internal_reason_codes:
        ctx.blocker_signal = None
        ctx.blocker_signal_claimant = None


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
    stashed = _should_stash_over_existing(existing, signal)
    if stashed:
        ctx.blocker_signal = signal
        ctx.blocker_signal_claimant = None
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
CREDENTIAL_SCOUT_VERIFY_REPLY = (
    "I need to verify the saved-credential login in the browser before I can save or run this code."
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
    "repair_ceiling_reached": (
        "I couldn't get past the same problem after several attempts.",
        "Tell me what to change and I'll try a different approach.",
    ),
    "code_authoring_guardrail_churn": (
        "I kept rewriting the generated code, but the safety checks rejected each version.",
        "Tell me what to change and I'll try a different approach.",
    ),
    "loop_detected_no_forward_progress_interaction": (
        "I couldn't get past this step.",
        "Tell me what to change and I'll try a different approach.",
    ),
}
_LOOP_ANTI_BOT_BLOCKER_COPY = "The site's verification challenge was still keeping the submit/search control disabled."
_LOOP_RESULT_COMPOSITION_BLOCKER_COPY = (
    "I found loaded results on the page, but I got stuck before extracting the requested information."
)
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


def _fresh_recorded_run_outcome(evidence: TerminalEvidence) -> RecordedRunOutcome | None:
    recorded = evidence.recorded_run_outcome
    if recorded is None or not recorded.workflow_run_id or not evidence.latest_workflow_run_id:
        return None
    if recorded.workflow_run_id != evidence.latest_workflow_run_id:
        return None
    return recorded


def _recorded_run_outcome_tier(recorded: RecordedRunOutcome | None) -> str | None:
    if recorded is None:
        return None
    if recorded.verdict == "demonstrated":
        return "The latest run recorded a verified outcome."
    if recorded.verdict == "not_demonstrated":
        if recorded.reason_code == "blocker_reported":
            return "The latest run recorded a blocker instead of a verified result."
        if recorded.reason_code == "no_meaningful_output":
            return "The latest run completed without meaningful output to return."
        return "The latest run recorded workflow output, but it did not verify the requested outcome."
    return "The latest run recorded workflow output, but it has not been verified end-to-end."


def _fresh_outcome_gate_reason(evidence: TerminalEvidence) -> str | None:
    if not evidence.latest_workflow_run_id or not evidence.outcome_gate_workflow_run_id:
        return None
    if evidence.latest_workflow_run_id != evidence.outcome_gate_workflow_run_id:
        return None
    return evidence.outcome_gate_reason


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


def terminal_evidence_has_recorded_state(evidence: TerminalEvidence | None) -> bool:
    if evidence is None:
        return False
    return bool(
        _fresh_outcome_gate_reason(evidence)
        or _fresh_recorded_run_outcome(evidence) is not None
        or evidence.anti_bot_blocked
    )


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

    tier_candidates = (
        ("recorded_run_outcome", _recorded_run_outcome_tier(_fresh_recorded_run_outcome(evidence))),
        ("verdict", _sanitize_loop_verdict_reason(_fresh_outcome_gate_reason(evidence))),
        ("anti_bot", _LOOP_ANTI_BOT_BLOCKER_COPY if evidence.anti_bot_blocked else None),
    )
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
    if internal_reason_code == "credential_priority_authoring_churn":
        return CREDENTIAL_SCOUT_VERIFY_REPLY, draft_tier
    framing, ask = _LOOP_BRANCH_COPY.get(internal_reason_code or "", _LOOP_BRANCH_COPY["loop_detected_generic"])
    result_steer = evidence.latest_evaluate_result_composition_steer if evidence is not None else None
    if (
        internal_reason_code == "loop_detected_consecutive_same_tool"
        and blocked_tool == "evaluate"
        and result_steer is not None
    ):
        # Loaded-result loops use their own framing while preserving the branch-specific ask.
        user_facing, tiers = compose_terminal_evidence_user_facing_reason(
            _LOOP_RESULT_COMPOSITION_BLOCKER_COPY,
            ask,
            evidence,
            blocked_tool=blocked_tool,
        )
        return user_facing, ("loaded_results", *tiers)
    return compose_terminal_evidence_user_facing_reason(framing, ask, evidence, blocked_tool=blocked_tool)


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
    try:
        user_facing, tiers = compose_loop_blocker_user_facing_reason(
            held.internal_reason_code, evidence, blocked_tool=held.blocked_tool
        )
        if not tiers:
            return
        if user_facing == held.user_facing_reason and evidence.has_draft == held.preserves_workflow_draft:
            return
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
    ctx.blocker_signal_claimant = None
    LOG.info("copilot loop blocker signal evidence refreshed", **to_trace_data(refreshed))
