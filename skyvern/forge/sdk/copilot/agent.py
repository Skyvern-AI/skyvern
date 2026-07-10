"""Copilot agent — multi-turn tool-use agent for workflow building.

Uses the OpenAI Agents SDK with LiteLLM for multi-provider LLM support.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opentelemetry import trace as otel_trace

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.experimentation.llm_prompt_config import LLMAPIHandler
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

import structlog
import yaml
from litellm.exceptions import NotFoundError as LiteLLMNotFoundError
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot import llm_config
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
    clear_terminal_evidence_on_workflow_edit,
    compose_terminal_evidence_user_facing_reason,
    contains_internal_machinery_leak,
    refresh_held_loop_blocker_evidence,
    terminal_evidence_from_ctx,
    terminal_evidence_has_recorded_state,
)
from skyvern.forge.sdk.copilot.blocker_signal import to_trace_data as blocker_signal_to_trace_data
from skyvern.forge.sdk.copilot.build_phase import BuildPhase, initial_build_phase
from skyvern.forge.sdk.copilot.build_test_outcome import (
    _VALUE_EXCERPT_MAX,
    RecordedBuildTestOutcome,
    RecordedOutcomeBindingConstraint,
    RecordedOutcomeGroundingRequirement,
    observed_value_extraction_scaffold_lines,
)
from skyvern.forge.sdk.copilot.code_block_preflight import SANDBOX_UNRESOLVED_NAME_REASON_CODE
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    is_optional_dismissal_only_trajectory,
    render_synthesized_offer_text,
    synthesize_code_block,
    trajectory_has_browser_fill_interaction,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSnapshot,
    apply_requested_output_producer_floor,
    build_turn_state,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import only_structural_requested_output_abstentions
from skyvern.forge.sdk.copilot.config import (
    SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD,
    BlockAuthoringPolicy,
    CopilotConfig,
    normalize_block_authoring_policy,
)
from skyvern.forge.sdk.copilot.context import (
    COPILOT_RESPONSE_TYPES,
    OUTPUT_OWNER_AMBIGUITY_REASON_CODE,
    AgentResult,
    CodeAuthoringRepairContext,
    CopilotContext,
    NarrativeActivityEntry,
    NarrativeBlock,
    NarrativeDraft,
    NarrativeOutcomeAdjudication,
    ResponseType,
    StructuredContext,
    TurnNarrativePayload,
    finalize_discovery_counter_in_global_llm_context,
    render_loaded_result_context_for_prompt,
    sanitize_global_llm_context_for_prompt,
)
from skyvern.forge.sdk.copilot.data_write_defaults import default_data_write_continue_on_failure
from skyvern.forge.sdk.copilot.enforcement import (
    BUILT_UNVERIFIED_REPAIR_INERT_TERMINAL_REASON,
    SCOUTED_SPINE_TURN_HALT_USER_REASON,
    artifact_health_blocked,
    log_scouted_spine_unresolved_at_turn_halt,
    outcome_fully_verified,
    recycle_admits_present_completion_contract_ask,
    synthesized_persistence_reopened,
    synthesized_persistence_reopened_after_failed_run,
    synthesized_trajectory_is_goal_complete,
    verified_goal_claim_authorized,
)
from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY
from skyvern.forge.sdk.copilot.llm_errors import is_retriable_llm_error as _is_retriable_llm_error
from skyvern.forge.sdk.copilot.outcome_verification_trace import (
    finalize_outcome_verification_trace,
    record_criteria_lifecycle,
    record_gate_decision,
)
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
)
from skyvern.forge.sdk.copilot.output_policy import (
    ACTUATION_OBLIGATION_STEER_REASON_CODE,
    ACTUATION_OBLIGATION_UNMET_REASON_CODE,
    UNVALIDATED_DISCLOSURE_PHRASES,
    WORKFLOW_PRESENT_SENTINEL,
    ActuationObligationEvaluation,
    ActuationObligationStatus,
    CannotActReason,
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
    actuation_obligation_key,
    build_output_policy_diagnostics,
    derive_output_kind,
    evaluate_actuation_obligation,
    evaluate_output_policy,
    hard_block_output_policy_verdict,
    normalize_response_scaffolding,
    output_policy_verdict_from_trace_data,
    output_policy_verdict_to_trace_data,
    prior_turn_satisfies_actuation_terminal_condition,
    request_policy_requires_durable_fill,
    turn_intent_requires_actuation,
)
from skyvern.forge.sdk.copilot.output_utils import (
    extract_final_text,
    parse_final_response,
)
from skyvern.forge.sdk.copilot.recoverable_failure import (
    RecoverableFailure,
    build_recoverable_failure,
    clean_recorded_failure_text,
    format_recoverable_failure_reply,
    merge_failure_into_context,
)
from skyvern.forge.sdk.copilot.request_policy import (
    RAW_SECRET_REFUSAL_SENTINEL,
    CompletionCriterion,
    RequestPolicy,
    build_request_policy,
    credential_prompt_reason,
    is_defer_authoring_durable_fill_criterion,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome, run_outcome_display_reason
from skyvern.forge.sdk.copilot.runtime import _browser_context_is_attachable
from skyvern.forge.sdk.copilot.streaming_adapter import (
    emit_turn_start,
    emit_workflow_draft,
    flush_goal_satisfied_tool_result,
    maybe_emit_design_end,
)
from skyvern.forge.sdk.copilot.tracing_setup import _copilot_model_name, ensure_tracing_initialized, is_tracing_enabled
from skyvern.forge.sdk.copilot.turn_context import TurnContextAssembler, TurnContextInputs, TurnContextPacket
from skyvern.forge.sdk.copilot.turn_halt import (
    _INVOLUNTARY_BLOCKER_REASON_CODES,
    CopilotTurnHalt,
    TurnHalt,
    TurnHaltKind,
    raise_if_turn_halt,
    turn_halt_to_trace_data,
)
from skyvern.forge.sdk.copilot.turn_intent import (
    NO_MUTATION_TURN_INTENT_MODES,
    RequiredContextKey,
    TurnIntent,
    TurnIntentClassifierResult,
    TurnIntentExpectedOutput,
    TurnIntentMode,
    TurnIntentReasonCode,
    build_turn_intent,
    classify_turn_intent,
    turn_intent_defers_authoring_live_fill,
)
from skyvern.forge.sdk.copilot.turn_outcome import (
    apply_repeated_reply_guard,
    derive_response_kind,
    with_copilot_code_mode_diagnostics,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.trace import apply_context_attrs
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()

WORKFLOW_KNOWLEDGE_BASE_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "skyvern" / "workflow_knowledge_base.txt"
)

_COPILOT_TURN_SPAN_NAME = "copilot.turn"
_USER_MESSAGE_PREVIEW_MAX_CHARS = 40


def _render_code_only_browser_authoring_prompt() -> str:
    from skyvern.forge.sdk.copilot.tools.banned_blocks import _code_only_browser_authoring_prompt

    return (
        _code_only_browser_authoring_prompt()
        + "\n\nWhen a SYNTHESIZED CODE BLOCK is offered to you, it already encodes the page\n"
        "interactions you scouted as deterministic Playwright. Persist that block VERBATIM\n"
        "via update_workflow / update_and_run_blocks — do not rewrite, reorder, or\n"
        "re-derive its locators. Only hand-author the steps it does not cover, such as the\n"
        "extraction or report block that returns the structured result. Direct browser\n"
        "evaluate is a scouting tool; persisted code blocks must not use page.evaluate,\n"
        "page.evaluate_handle, page.request, or page.context. Use locators and locator\n"
        "DOM-reading methods such as inner_text, text_content, get_attribute, count, and\n"
        "is_visible instead."
    )


@runtime_checkable
class _AgentInstructionsContext(Protocol):
    context: object


def _build_user_message_preview(message: str) -> str:
    flattened = (message or "").replace("\r", " ").replace("\n", " ").strip()
    redacted = redact_raw_secrets_for_prompt(flattened)
    if len(redacted) <= _USER_MESSAGE_PREVIEW_MAX_CHARS:
        return redacted
    return redacted[: _USER_MESSAGE_PREVIEW_MAX_CHARS - 1] + "…"


def _derive_turn_index(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    explicit: int | None,
) -> int:
    # Zero-based to match the wire contract (``WorkflowCopilotTurnStartUpdate``).
    # ``chat_history`` may be a truncated tail of the full message log, so this
    # fallback can undercount long sessions; prefer the explicit count.
    if explicit is not None:
        return explicit
    return sum(1 for m in chat_history if m.sender == WorkflowCopilotChatSender.USER)


@contextlib.contextmanager
def _copilot_turn_span(
    *,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    turn_index: int | None,
    turn_id: str | None = None,
) -> Iterator[Any]:
    tracer = otel_trace.get_tracer("skyvern")
    with tracer.start_as_current_span(_COPILOT_TURN_SPAN_NAME) as span:
        span.set_attribute("skyvern.span.role", "wrapper")
        span.set_attribute("copilot.turn_index", _derive_turn_index(chat_history, turn_index))
        if turn_id is not None:
            span.set_attribute("copilot.turn_id", turn_id)
        preview = _build_user_message_preview(chat_request.message)
        if preview:
            span.set_attribute("copilot.user_message_preview", preview)
        if chat_request.workflow_copilot_chat_id:
            span.set_attribute("copilot.session_id", chat_request.workflow_copilot_chat_id)
        if chat_request.workflow_permanent_id:
            span.set_attribute("workflow_permanent_id", chat_request.workflow_permanent_id)
        apply_context_attrs(span)
        yield span


async def _resolve_request_policy_handler(
    llm_api_handler: LLMAPIHandler | None, workflow_permanent_id: str | None, organization_id: str | None
) -> Any:
    lite_handler = await llm_config.resolve_lite_copilot_handler(workflow_permanent_id, organization_id)
    if lite_handler is not None:
        return lite_handler
    LOG.warning(
        "copilot request policy lite handler unavailable, falling back to main handler",
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    return llm_api_handler


@dataclass(frozen=True)
class RequestPolicyGuardrailInputs:
    user_message: str
    workflow_yaml: str
    chat_history_text: str
    chat_history_messages: list[WorkflowCopilotChatHistoryMessage]
    global_llm_context: str
    organization_id: str
    request_policy_handler: Any
    turn_intent_handler: LLMAPIHandler | None
    previous_user_message: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    workflow_run_id: str | None = None
    browser_session_id: str | None = None
    fix_origin: bool = False
    stored_completion_criteria: StoredCriteriaSnapshot | None = None


class CopilotRequestPolicyMissingError(Exception):
    """Raised when the request-policy guardrail fails before producing a policy."""


def _manager_can_probe_registered_browser_state() -> bool:
    return app.PERSISTENT_SESSIONS_MANAGER.can_probe_registered_browser_state()


async def _registered_browser_state_is_usable(session_id: str, organization_id: str) -> bool:
    if not _manager_can_probe_registered_browser_state():
        return False

    state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
        session_id=session_id,
        organization_id=organization_id,
    )
    return bool(state and _browser_context_is_attachable(state.browser_context))


async def _resolve_live_browser_session_id(
    chat_request: WorkflowCopilotChatRequest,
    organization_id: str,
) -> str | None:
    """Validate against a debug session for the same (org, workflow_permanent_id);
    return None on any failure so the caller falls back to auto-create."""
    requested = chat_request.browser_session_id
    if not requested:
        return None

    try:
        debug_session = await app.DATABASE.debug.get_debug_session_by_browser_session_id(
            browser_session_id=requested,
            organization_id=organization_id,
        )
        if debug_session is None:
            LOG.warning(
                "Copilot received an unknown browser_session_id; ignoring",
                organization_id=organization_id,
                requested_session_id=requested,
            )
            return None
        if debug_session.workflow_permanent_id != chat_request.workflow_permanent_id:
            LOG.warning(
                "Copilot browser_session_id is bound to a different workflow; ignoring",
                organization_id=organization_id,
                requested_session_id=requested,
                expected_wpid=chat_request.workflow_permanent_id,
                actual_wpid=debug_session.workflow_permanent_id,
            )
            return None

        persistent = await app.PERSISTENT_SESSIONS_MANAGER.get_session(requested, organization_id)
        has_browser_address = bool(persistent.browser_address) if persistent else False
        has_registered_browser_state = False
        if persistent is not None and not is_final_status(persistent.status) and not has_browser_address:
            has_registered_browser_state = await _registered_browser_state_is_usable(requested, organization_id)

        if (
            persistent is None
            or is_final_status(persistent.status)
            or (not has_browser_address and not has_registered_browser_state)
        ):
            LOG.warning(
                "Copilot live browser session is not yet usable; falling back to auto-create",
                organization_id=organization_id,
                requested_session_id=requested,
                status=persistent.status if persistent else None,
                has_browser_address=has_browser_address,
                has_registered_browser_state=has_registered_browser_state,
            )
            return None

        LOG.info(
            "Copilot reusing live browser session",
            organization_id=organization_id,
            session_id=requested,
        )
        return requested
    except Exception as exc:
        LOG.warning(
            "Copilot live-session validation raised; falling back to auto-create",
            organization_id=organization_id,
            requested_session_id=requested,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None


def _format_chat_history(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> str:
    if not chat_history:
        return ""
    lines = [f"{msg.sender}: {msg.content}" for msg in chat_history]
    return "\n".join(lines)


def _build_block_goal_main_goal(
    user_message: str,
    chat_history_text: str,
    global_llm_context: str | None,
) -> str:
    raw_current_message = (user_message or "").strip()
    if not raw_current_message:
        return ""
    return escape_code_fences(raw_current_message)


def _request_policy_agent_inputs(
    policy: RequestPolicy,
    *,
    user_message: str,
    chat_history_text: str,
    previous_user_message: str | None,
) -> tuple[str, str]:
    if policy.raw_secret_detected:
        # Raw-secret turns use redacted latest content before skip-test follow-up reuse.
        return redact_raw_secrets_for_prompt(user_message), chat_history_text
    if policy.testing_intent == "skip_test" and len(user_message) < 160 and previous_user_message:
        return (
            f"{user_message}\n\nDraft the workflow requested earlier:\n"
            f"{redact_raw_secrets_for_prompt(previous_user_message)}",
            "",
        )
    return user_message, chat_history_text


def _stored_active_completion_criteria(
    policy_inputs: RequestPolicyGuardrailInputs,
) -> list[CompletionCriterion] | None:
    snapshot = policy_inputs.stored_completion_criteria
    if snapshot is None or snapshot.active is None:
        return None
    return list(snapshot.active.criteria)


def _log_requested_output_producer_floor(rekeyed_paths: tuple[str, ...]) -> None:
    if not rekeyed_paths:
        return
    LOG.info(
        "copilot requested-output producer floor",
        requested_output_floor_rekeyed_paths=list(rekeyed_paths),
        requested_output_floor_rekeyed_count=len(rekeyed_paths),
    )


def _reconcile_completion_criteria_on_context(
    ctx: CopilotContext,
    policy: RequestPolicy,
    policy_inputs: RequestPolicyGuardrailInputs,
) -> None:
    fresh_criteria = list(policy.completion_criteria)
    durable_fill_carriers = [c for c in fresh_criteria if is_defer_authoring_durable_fill_criterion(c)]
    floored_fresh, fresh_floor_rekeyed_paths = apply_requested_output_producer_floor(fresh_criteria)
    if fresh_floor_rekeyed_paths:
        policy.completion_criteria = list(floored_fresh)
    snapshot = policy_inputs.stored_completion_criteria
    if snapshot is None:
        _log_requested_output_producer_floor(fresh_floor_rekeyed_paths)
        _restore_durable_fill_carriers(policy, durable_fill_carriers)
        return
    requested_output_path_aliases = (
        ctx.copilot_config.requested_output_path_aliases if ctx.copilot_config is not None else None
    )
    decision = reconcile_completion_criteria(
        snapshot,
        fresh_criteria,
        actionable=policy.user_response_policy != "ask_clarification",
        requested_output_path_aliases=requested_output_path_aliases,
    )
    ctx.completion_criteria_turn_state = build_turn_state(snapshot, decision)
    record_criteria_lifecycle(ctx, decision.to_trace_data())
    LOG.info("copilot completion criteria reconciled", **decision.to_trace_data())
    floored_criteria, floor_rekeyed_paths = apply_requested_output_producer_floor(decision.criteria)
    if decision.action == "adopt_stored" or floor_rekeyed_paths:
        policy.completion_criteria = list(floored_criteria)
    _log_requested_output_producer_floor(floor_rekeyed_paths)
    _restore_durable_fill_carriers(policy, durable_fill_carriers)


def _restore_durable_fill_carriers(policy: RequestPolicy, carriers: list[CompletionCriterion]) -> None:
    if not carriers:
        return
    present_ids = {criterion.id for criterion in policy.completion_criteria}
    missing = [carrier for carrier in carriers if carrier.id not in present_ids]
    if missing:
        policy.completion_criteria = list(policy.completion_criteria) + missing


def _store_request_policy_on_context(
    ctx: CopilotContext,
    policy: RequestPolicy,
    policy_inputs: RequestPolicyGuardrailInputs,
    turn_intent_classifier_result: TurnIntentClassifierResult | None = None,
) -> None:
    agent_user_message, policy_chat_history_text = _request_policy_agent_inputs(
        policy,
        user_message=policy_inputs.user_message,
        chat_history_text=policy_inputs.chat_history_text,
        previous_user_message=policy_inputs.previous_user_message,
    )
    turn_intent = build_turn_intent(
        user_message=policy_inputs.user_message,
        workflow_yaml=policy_inputs.workflow_yaml,
        chat_history=policy_inputs.chat_history_messages,
        global_llm_context=policy_inputs.global_llm_context,
        request_policy=policy,
        workflow_id=policy_inputs.workflow_id,
        workflow_permanent_id=policy_inputs.workflow_permanent_id,
        workflow_run_id=policy_inputs.workflow_run_id,
        browser_session_id=policy_inputs.browser_session_id,
        classifier_result=turn_intent_classifier_result,
        fix_origin=policy_inputs.fix_origin,
    )
    _reconcile_completion_criteria_on_context(ctx, policy, policy_inputs)
    ctx.request_policy = policy
    ctx.allow_untested_workflow_draft = policy.testing_intent == "skip_test"
    ctx.user_message = agent_user_message
    ctx.block_goal_main_goal = _build_block_goal_main_goal(
        user_message=agent_user_message,
        chat_history_text=policy_chat_history_text,
        global_llm_context=policy_inputs.global_llm_context,
    )
    ctx.turn_intent = turn_intent


def _turn_intent_log_fields(intent: TurnIntent | None) -> dict[str, Any]:
    if not isinstance(intent, TurnIntent):
        return {}
    return {f"turn_intent_{key}": value for key, value in intent.to_trace_data().items()}


def _turn_intent_trace_fields(intent: TurnIntent | None) -> dict[str, str]:
    return {key: str(value) for key, value in _turn_intent_log_fields(intent).items()}


def _turn_context_log_fields(packet: TurnContextPacket | None) -> dict[str, Any]:
    if not isinstance(packet, TurnContextPacket):
        return {}
    return {f"turn_context_{key}": value for key, value in packet.to_trace_data().items()}


def _turn_context_trace_fields(packet: TurnContextPacket | None) -> dict[str, str]:
    return {key: str(value) for key, value in _turn_context_log_fields(packet).items()}


def _store_turn_context_packet_on_context(
    ctx: CopilotContext,
    *,
    request_policy: RequestPolicy,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    debug_run_info_text: str,
    prior_copilot_workflow_yaml: str | None,
) -> None:
    if not isinstance(ctx.turn_intent, TurnIntent):
        return
    ctx.turn_context_packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=ctx.turn_intent,
            request_policy=request_policy,
            user_message=chat_request.message,
            workflow_yaml=chat_request.workflow_yaml or "",
            prior_workflow_yaml=prior_copilot_workflow_yaml or "",
            chat_history=chat_history,
            debug_run_info_text=debug_run_info_text,
        )
    )
    if ctx.turn_context_packet.repeated_reply_context is not None:
        ctx.blocked_reply_signatures = list(ctx.turn_context_packet.repeated_reply_context.blocked_signatures)


def _build_system_prompt(
    tool_usage_guide: str,
    config: CopilotConfig | None = None,
    security_rules: str | None = None,
) -> str:
    copilot_config = config or CopilotConfig(security_rules=security_rules or "")
    template = copilot_config.prompt_template.removesuffix(".j2")
    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    prompt = prompt_engine.load_prompt(
        template=template,
        workflow_knowledge_base=workflow_knowledge_base,
        current_datetime=datetime.now(timezone.utc).isoformat(),
        tool_usage_guide=tool_usage_guide,
        security_rules=copilot_config.security_rules,
    )
    if copilot_config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        prompt = f"{prompt}\n\n{_render_code_only_browser_authoring_prompt()}"
    return prompt


def _runtime_verification_evidence_prompt(ctx: CopilotContext | None) -> str:
    if ctx is None:
        return ""
    evidence = ctx.workflow_verification_evidence
    rendered = evidence.render_prompt_block()
    if not rendered:
        return ""
    return (
        "\n\nRUNTIME VERIFICATION EVIDENCE:\n```yaml\n" + escape_code_fences(rendered) + "\n```\n"
        "Use this structured state before choosing the next action. If "
        "`full_workflow_verified` is false, choose an evidence-grounded next step: split an oversized block, "
        "continue from observed current browser state, run only missing block labels, or report partial verification. "
        "Do not claim end-to-end verification unless `full_workflow_verified` is true."
    )


def _clean_authoring_repair_prompt_atom(value: str, *, max_chars: int = 160) -> str:
    cleaned = redact_raw_secrets_for_prompt(value).replace("\r", " ").replace("\n", " ").strip()
    if contains_internal_machinery_leak(cleaned):
        return ""
    return cleaned[:max_chars]


def _render_authoring_repair_prompt_list(items: list[str], *, max_items: int = 20) -> str:
    cleaned = [_clean_authoring_repair_prompt_atom(item) for item in items[:max_items]]
    return ", ".join(item for item in cleaned if item) or "(none)"


def _render_selector_repair_alternatives(alternatives: list[dict[str, str]], *, max_items: int = 8) -> list[str]:
    lines: list[str] = []
    for alternative in alternatives[:max_items]:
        tool_name = _clean_authoring_repair_prompt_atom(str(alternative.get("tool_name") or ""), max_chars=60)
        role = _clean_authoring_repair_prompt_atom(str(alternative.get("role") or ""), max_chars=80)
        selector = _clean_authoring_repair_prompt_atom(str(alternative.get("selector") or ""), max_chars=180)
        if not selector:
            continue
        parts = [f"tool_name={tool_name or '(unknown)'}"]
        if role:
            parts.append(f"role={role}")
        parts.append(f"selector={selector}")
        lines.append("- " + ", ".join(parts))
    return lines


def _render_unresolved_name_binding_actions(
    unresolved_names: list[str], available_parameter_keys: list[str], *, max_items: int = 20
) -> list[str]:
    available_keys = {
        key
        for raw_key in available_parameter_keys
        for key in [_clean_authoring_repair_prompt_atom(raw_key, max_chars=80)]
        if key
    }
    lines: list[str] = []
    for raw_name in unresolved_names[:max_items]:
        name = _clean_authoring_repair_prompt_atom(raw_name, max_chars=80)
        if not name:
            continue
        if name in available_keys:
            lines.append(
                f"- {name} -> existing workflow parameter key {name} -> parameter_keys -> bare variable {name}"
            )
            continue
        lines.append(
            f"- {name} -> create workflow string parameter key {name} -> parameter_keys -> bare variable {name}"
        )
    return lines


def _code_authoring_repair_context_prompt(ctx: CopilotContext | None) -> str:
    if ctx is None:
        return ""
    if normalize_block_authoring_policy(ctx.block_authoring_policy) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return ""
    repair_context = ctx.last_code_authoring_repair_context
    if not isinstance(repair_context, CodeAuthoringRepairContext):
        return ""
    LOG.info(
        "copilot code authoring repair context rendered",
        reason_code=repair_context.reason_code,
        block_label=repair_context.block_label,
        unresolved_names=repair_context.unresolved_names,
    )
    available_parameter_keys = repair_context.available_parameter_keys
    binding_candidates = repair_context.binding_candidates or repair_context.unresolved_names

    lines = [
        "CODE AUTHORING REPAIR CONTEXT:",
        f"block_label: {_clean_authoring_repair_prompt_atom(repair_context.block_label)}",
        f"reason_code: {_clean_authoring_repair_prompt_atom(repair_context.reason_code)}",
        f"unresolved_names: {_render_authoring_repair_prompt_list(repair_context.unresolved_names)}",
        f"declared_parameter_keys: {_render_authoring_repair_prompt_list(repair_context.parameter_keys)}",
        f"available_parameter_keys: {_render_authoring_repair_prompt_list(available_parameter_keys)}",
        f"binding_candidates: {_render_authoring_repair_prompt_list(binding_candidates)}",
        f"allowed_global_names: {_render_authoring_repair_prompt_list(repair_context.allowed_global_names)}",
    ]
    if repair_context.reason_code == "runtime_missing_output_dependency":
        lines.extend(
            [
                f"missing_output_key: {_clean_authoring_repair_prompt_atom(repair_context.missing_output_key or '')}",
                f"available_output_keys: {_render_authoring_repair_prompt_list(repair_context.available_output_keys)}",
                "output_dependency_failure_class: "
                f"{_clean_authoring_repair_prompt_atom(repair_context.output_dependency_failure_class or '')}",
                "current_block_parameter_keys: "
                f"{_render_authoring_repair_prompt_list(repair_context.current_block_parameter_keys)}",
            ]
        )
    if repair_context.selector:
        lines.append(f"selector: {_clean_authoring_repair_prompt_atom(repair_context.selector)}")
    if repair_context.source_url:
        lines.append(f"source_url: {_clean_authoring_repair_prompt_atom(repair_context.source_url)}")
    if repair_context.refiner_selector:
        lines.append(f"refiner_selector: {_clean_authoring_repair_prompt_atom(repair_context.refiner_selector)}")
    if repair_context.reason_code == "runtime_block_failure":
        if repair_context.runtime_failure_reason:
            lines.append(
                f"runtime_failure_reason: {_clean_authoring_repair_prompt_atom(repair_context.runtime_failure_reason)}"
            )
        if repair_context.runtime_failure_class:
            lines.append(
                f"runtime_failure_class: {_clean_authoring_repair_prompt_atom(repair_context.runtime_failure_class)}"
            )
        if repair_context.failed_block_status:
            lines.append(
                f"failed_block_status: {_clean_authoring_repair_prompt_atom(repair_context.failed_block_status)}"
            )
        if repair_context.workflow_run_id:
            workflow_run_id = _clean_authoring_repair_prompt_atom(repair_context.workflow_run_id)
            if workflow_run_id:
                lines.append(f"workflow_run_id: {workflow_run_id}")
        if repair_context.current_origin:
            lines.append(f"current_origin: {_clean_authoring_repair_prompt_atom(repair_context.current_origin)}")
        lines.append(f"current_url_present: {str(repair_context.current_url_present).lower()}")
        lines.append(f"current_title_present: {str(repair_context.current_title_present).lower()}")
        if repair_context.page_evidence_source:
            page_evidence_source = _clean_authoring_repair_prompt_atom(repair_context.page_evidence_source)
            if page_evidence_source:
                lines.append(f"page_evidence_source: {page_evidence_source}")
        lines.append(f"observed_after_workflow_run: {str(repair_context.observed_after_workflow_run).lower()}")
        if repair_context.page_form_summaries:
            lines.append(f"page_forms: {_render_authoring_repair_prompt_list(repair_context.page_form_summaries)}")
        if repair_context.page_result_summaries:
            lines.append(f"page_results: {_render_authoring_repair_prompt_list(repair_context.page_result_summaries)}")
        if repair_context.page_action_summaries:
            lines.append(f"page_actions: {_render_authoring_repair_prompt_list(repair_context.page_action_summaries)}")
        if repair_context.page_challenge_summaries:
            lines.append(
                f"page_challenges: {_render_authoring_repair_prompt_list(repair_context.page_challenge_summaries)}"
            )
    if repair_context.reason_code == "metadata_reject":
        if repair_context.runtime_failure_reason:
            lines.append(
                f"runtime_failure_reason: {_clean_authoring_repair_prompt_atom(repair_context.runtime_failure_reason)}"
            )
        if repair_context.runtime_failure_class:
            lines.append(
                f"runtime_failure_class: {_clean_authoring_repair_prompt_atom(repair_context.runtime_failure_class)}"
            )
        if repair_context.metadata_contract_source:
            lines.append(
                "metadata_contract_source: "
                f"{_clean_authoring_repair_prompt_atom(repair_context.metadata_contract_source)}"
            )
        if repair_context.metadata_contract_reason_code:
            lines.append(
                "metadata_contract_reason_code: "
                f"{_clean_authoring_repair_prompt_atom(repair_context.metadata_contract_reason_code)}"
            )
        if repair_context.required_goal_value_paths:
            lines.append(
                "required_goal_value_paths: "
                f"{_render_authoring_repair_prompt_list(repair_context.required_goal_value_paths)}"
            )
        if repair_context.required_extraction_schema_paths:
            lines.append(
                "required_extraction_schema_paths: "
                f"{_render_authoring_repair_prompt_list(repair_context.required_extraction_schema_paths)}"
            )
        if repair_context.required_code_return_paths:
            lines.append(
                "required_code_return_paths: "
                f"{_render_authoring_repair_prompt_list(repair_context.required_code_return_paths)}"
            )
    if repair_context.required_block_structure:
        lines.append(
            f"required_block_structure: {_clean_authoring_repair_prompt_atom(repair_context.required_block_structure)}"
        )
        if repair_context.spine_stage_count is not None:
            lines.append(f"spine_stage_count: {repair_context.spine_stage_count}")
        if repair_context.spine_split_blockers:
            lines.append(
                f"spine_split_blockers: {_render_authoring_repair_prompt_list(repair_context.spine_split_blockers)}"
            )
        lines.append(
            "Author one browser-stage code block per scouted mutation stage and a final extraction-only code block "
            "that returns the required output paths; do not collapse the browser spine into the extraction block."
        )
    if repair_context.reason_code == OUTPUT_OWNER_AMBIGUITY_REASON_CODE:
        lines.append(
            "output_owner_candidate_labels: "
            f"{_render_authoring_repair_prompt_list(repair_context.output_owner_candidate_labels)}"
        )
        lines.append(
            "required_output_owner_paths: "
            f"{_render_authoring_repair_prompt_list(repair_context.required_code_return_paths)}"
        )
        lines.append(
            "Designate exactly one code block as the sole output owner for the required paths and declare its "
            "code_artifact_metadata; do not leave the requested output split across or absent from the code blocks."
        )
    selector_alternative_lines = _render_selector_repair_alternatives(repair_context.selector_alternatives)
    if selector_alternative_lines:
        lines.append("same_page_selector_alternatives:")
        lines.extend(selector_alternative_lines)
    if repair_context.allowed_helper_surface:
        lines.append("allowed_helper_surface:")
        for helper_name, attributes in sorted(repair_context.allowed_helper_surface.items()):
            helper = _clean_authoring_repair_prompt_atom(helper_name)
            rendered_attributes = _render_authoring_repair_prompt_list(attributes, max_items=40)
            if helper:
                lines.append(f"{helper}: {rendered_attributes}")
    if repair_context.reason_code == SANDBOX_UNRESOLVED_NAME_REASON_CODE:
        binding_action_lines = _render_unresolved_name_binding_actions(
            repair_context.unresolved_names, available_parameter_keys
        )
        if binding_action_lines:
            lines.append("binding_actions:")
            lines.extend(binding_action_lines)
        lines.append(
            "For workflow-input-like unresolved names, ensure a workflow string parameter exists, "
            "list the exact key in the code block's parameter_keys, reference the exact key as a bare Python "
            "variable in code, do not hardcode the eval value, and rerun via update_and_run_blocks."
        )
    if repair_context.reason_code == "synthesized_parameter_binding_ambiguous":
        binding_action_lines = _render_unresolved_name_binding_actions(
            repair_context.unresolved_names, available_parameter_keys
        )
        if binding_action_lines:
            lines.append("binding_actions:")
            lines.extend(binding_action_lines)
        lines.append(
            "For synthesized parameter binding, declare and use the exact workflow input key, include that exact "
            "key in the code block's parameter_keys, reference it as a bare Python variable in code, do not guess "
            "or hardcode the runtime value, and rerun via update_and_run_blocks."
        )
    if repair_context.reason_code == "ambiguous_bare_selector":
        lines.append(
            "For ambiguous selectors, do not re-emit the bare selector or a positional nth selector. "
            "Use the same-page alternatives when they are stable, or re-scout the same page and choose a "
            "stable role/name/data attribute."
        )
    if repair_context.reason_code == "runtime_block_failure":
        lines.append(
            "For runtime failures, adapt the next code block to the observed page state and do not re-emit "
            "the same failing selector or name path."
        )
    if repair_context.reason_code == "runtime_missing_output_dependency":
        lines.append(
            "For missing prior block outputs, bind to an actual available_output_key or repair the producing/current "
            "code block so the output exists; do not create a workflow parameter for missing_output_key."
        )
    if repair_context.reason_code == "metadata_reject":
        lines.append(
            "For metadata rejects, author code_artifact_metadata with goal_value_paths, valid extraction_schema, "
            "and code return paths matching required requested output child paths; rerun update_and_run_blocks."
        )
    lines.append(_clean_authoring_repair_prompt_atom(repair_context.repair_instruction, max_chars=260))
    return "\n\n" + "\n".join(line for line in lines if line)


def _recorded_build_test_outcome_prompt(ctx: CopilotContext | None) -> str:
    if ctx is None:
        return ""
    if normalize_block_authoring_policy(ctx.block_authoring_policy) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return ""
    outcome = ctx.latest_recorded_build_test_outcome
    if not isinstance(outcome, RecordedBuildTestOutcome) or not outcome.is_authoritative:
        return ""
    LOG.info(
        "copilot recorded build-test outcome rendered",
        phase=outcome.phase,
        reason_code=outcome.reason_code,
        structural_key=outcome.structural_key,
        workflow_run_id=outcome.workflow_run_id,
    )
    lines = [
        "RECORDED BUILD-TEST OUTCOME:",
        f"phase: {_clean_authoring_repair_prompt_atom(outcome.phase)}",
        f"attempted_tool: {_clean_authoring_repair_prompt_atom(outcome.attempted_tool)}",
        f"attempted_target: {_clean_authoring_repair_prompt_atom(outcome.attempted_target)}",
        f"attempted_block_label: {_clean_authoring_repair_prompt_atom(outcome.attempted_block_label)}",
        f"verdict: {_clean_authoring_repair_prompt_atom(outcome.verdict)}",
        f"reason_code: {_clean_authoring_repair_prompt_atom(outcome.reason_code)}",
        f"structural_key: {_clean_authoring_repair_prompt_atom(outcome.structural_key or '')}",
        f"block_labels: {_render_authoring_repair_prompt_list(outcome.block_labels)}",
        f"page_evidence_refs: {_render_authoring_repair_prompt_list(outcome.page_evidence_refs)}",
    ]
    if outcome.missing_requested_output_facts:
        lines.append("missing_requested_output_facts:")
        lines.append(
            "Use the exact output_path values in goal_value_paths and returned output; "
            "output_root is diagnostic grouping only."
        )
        for fact in outcome.missing_requested_output_facts[:8]:
            if not isinstance(fact, dict):
                continue
            fields = []
            for key in ("output_root", "output_path", "value_status", "reason_code"):
                value = fact.get(key)
                if isinstance(value, str) and value.strip():
                    fields.append(f"{key}={_clean_authoring_repair_prompt_atom(value)}")
            if fields:
                lines.append(f"- {'; '.join(fields)}")
    if outcome.workflow_run_id:
        lines.append(f"workflow_run_id: {_clean_authoring_repair_prompt_atom(outcome.workflow_run_id)}")
    if outcome.observed_evidence_summary:
        lines.append(f"observed_evidence: {_clean_authoring_repair_prompt_atom(outcome.observed_evidence_summary)}")
    if outcome.observed_page_value_excerpt:
        rendered_values = _clean_authoring_repair_prompt_atom(
            outcome.observed_page_value_excerpt, max_chars=_VALUE_EXCERPT_MAX
        )
        output_paths = [
            _clean_authoring_repair_prompt_atom(str(fact.get("output_path")))
            for fact in outcome.missing_requested_output_facts
            if isinstance(fact, dict) and isinstance(fact.get("output_path"), str) and fact.get("output_path")
        ]
        scaffold_lines = observed_value_extraction_scaffold_lines(rendered_values, output_paths)
        lines.extend(scaffold_lines)
        LOG.info(
            "copilot_observed_value_scaffold_surfaced",
            excerpt_len=len(rendered_values),
            output_path_count=len(output_paths),
            scaffold_line_count=len(scaffold_lines),
        )
    grounding = getattr(ctx, "recorded_outcome_grounding_requirement", None)
    if isinstance(grounding, RecordedOutcomeGroundingRequirement) and grounding.payload is not None:
        payload = grounding.payload
        LOG.info(
            "copilot recorded outcome grounding rendered",
            repeated_structural_key=payload.repeated_structural_key,
            workflow_run_id=payload.workflow_run_id,
            observed_after_workflow_run=payload.observed_after_workflow_run,
        )
        lines.extend(
            [
                "RECORDED OUTCOME GROUNDING EVIDENCE:",
                f"repeated_structural_key: {_clean_authoring_repair_prompt_atom(payload.repeated_structural_key)}",
                f"source_tool: {payload.source_tool}",
                f"observed_after_workflow_run: {str(payload.observed_after_workflow_run).lower()}",
                f"observed_empty_page: {str(payload.observed_empty_page).lower()}",
                f"challenge_gated: {str(payload.challenge_gated).lower()}",
                f"capture_degraded: {str(payload.capture_degraded).lower()}",
                f"diagnostic_reason: {_clean_authoring_repair_prompt_atom(payload.diagnostic_reason)}",
                f"current_url_present: {str(payload.current_url_present).lower()}",
                f"current_title_present: {str(payload.current_title_present).lower()}",
            ]
        )
        if payload.target_url:
            lines.append(f"target_url: {_clean_authoring_repair_prompt_atom(payload.target_url)}")
        if payload.source_url:
            lines.append(f"source_url: {_clean_authoring_repair_prompt_atom(payload.source_url)}")
        if payload.requirement_workflow_run_id:
            lines.append(f"requirement_workflow_run_id: {payload.requirement_workflow_run_id}")
        if payload.payload_workflow_run_id:
            lines.append(f"payload_workflow_run_id: {payload.payload_workflow_run_id}")
        if payload.workflow_run_id:
            lines.append(f"grounding_workflow_run_id: {payload.workflow_run_id}")
        if payload.current_origin:
            lines.append(f"current_origin: {_clean_authoring_repair_prompt_atom(payload.current_origin)}")
        if payload.form_summaries:
            lines.append(f"forms: {_render_authoring_repair_prompt_list(payload.form_summaries)}")
        if payload.result_container_summaries:
            lines.append(
                f"result_containers: {_render_authoring_repair_prompt_list(payload.result_container_summaries)}"
            )
        if payload.navigation_action_summaries:
            lines.append(
                f"navigation_actions: {_render_authoring_repair_prompt_list(payload.navigation_action_summaries)}"
            )
        if payload.challenge_control_summaries:
            lines.append(
                f"challenge_controls: {_render_authoring_repair_prompt_list(payload.challenge_control_summaries)}"
            )
    binding = ctx.recorded_outcome_binding_constraint
    if isinstance(binding, RecordedOutcomeBindingConstraint):
        lines.extend(
            [
                "RECORDED OUTCOME BINDING CONSTRAINT:",
                f"frontier_facet: {_clean_authoring_repair_prompt_atom(binding.frontier_facet)}",
                f"owning_block_labels: {_render_authoring_repair_prompt_list(binding.owning_block_labels)}",
                f"diagnostic_reason: {_clean_authoring_repair_prompt_atom(binding.diagnostic_reason)}",
                "The next authored change must move the named frontier facet on the owning block(s); an unchanged "
                "frontier is rejected before rerun.",
            ]
        )
    else:
        lines.append(
            "Before saving or rerunning, change the next authored step, selector, extraction, or binding based on "
            "this recorded structure."
        )
    return "\n\n" + "\n".join(line for line in lines if line)


def _synthesized_block_offer_prompt(ctx: CopilotContext | None) -> str:
    """Pre-authoring offer of the synthesized code block.

    Trips the ``synthesized_block_offered`` latch (shared with the post-turn enforcement offer)
    only when a non-None offer is actually rendered, so an empty trajectory leaves it open.
    """
    if ctx is None:
        return ""
    if normalize_block_authoring_policy(ctx.block_authoring_policy) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        LOG.debug("copilot_synthesized_block_offer_skipped", reason="policy_not_code_only_browser")
        return ""
    reopened_after_failed_run = synthesized_persistence_reopened_after_failed_run(ctx)
    reopened = synthesized_persistence_reopened(ctx)
    if ctx.update_workflow_called and not reopened:
        LOG.debug("copilot_synthesized_block_offer_skipped", reason="already_authored")
        return ""
    if not ctx.scout_trajectory:
        LOG.debug("copilot_synthesized_block_offer_skipped", reason="empty_trajectory")
        return ""
    trajectory_len = len(ctx.scout_trajectory)
    previous_offer_len = ctx.synthesized_block_offered_trajectory_len
    trajectory_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
    if (
        ctx.synthesized_block_offered
        and trajectory_len < previous_offer_len + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD
        and (not trajectory_goal_complete or getattr(ctx, "synthesized_block_offered_goal_complete", False))
        and not reopened
    ):
        LOG.debug(
            "copilot_synthesized_block_offer_skipped",
            reason="already_offered",
            previous_trajectory_len=previous_offer_len,
            trajectory_len=trajectory_len,
        )
        return ""
    if is_optional_dismissal_only_trajectory(ctx.scout_trajectory):
        LOG.debug("copilot_synthesized_block_offer_skipped", reason="optional_dismissal_only")
        return ""
    fill_step_count = sum(
        1
        for interaction in ctx.scout_trajectory
        if str(interaction.get("tool_name") or "") in {"type_text", "select_option", "fill_credential_field"}
    )
    LOG.info(
        "copilot_synthesis_input_fill_steps",
        trajectory_len=len(ctx.scout_trajectory),
        fill_step_count=fill_step_count,
    )
    synthesized = synthesize_code_block(ctx.scout_trajectory, reached_download_target=ctx.reached_download_target)
    if synthesized is None:
        LOG.debug(
            "copilot_synthesized_block_offer_skipped",
            reason="synthesis_returned_none",
            trajectory_len=len(ctx.scout_trajectory),
        )
        return ""
    ctx.synthesized_block_offered = True
    ctx.synthesized_block_offered_trajectory_len = trajectory_len
    ctx.synthesized_block_offered_goal_complete = trajectory_goal_complete
    if reopened_after_failed_run:
        ctx.synthesized_block_reopened_after_failed_run = True
    LOG.info(
        "copilot_synthesized_block_offer_rendered",
        trajectory_len=trajectory_len,
        previous_trajectory_len=previous_offer_len,
        code_len=len(synthesized.code),
    )
    goal = ctx.block_goal_main_goal or ctx.user_message or ""
    return "\n\n" + render_synthesized_offer_text(synthesized, ctx.scout_trajectory, goal=goal)


def _build_dynamic_system_prompt(tool_usage_guide: str, config: CopilotConfig) -> Callable[[object, object], str]:
    base_system_prompt = _build_system_prompt(tool_usage_guide=tool_usage_guide, config=config)

    def instructions(context: object, _agent: object) -> str:
        if not isinstance(context, _AgentInstructionsContext):
            return base_system_prompt
        ctx = context.context
        if not isinstance(ctx, CopilotContext):
            return base_system_prompt
        policy = ctx.request_policy
        if not isinstance(policy, RequestPolicy):
            return base_system_prompt
        policy_summary = escape_code_fences(redact_raw_secrets_for_prompt(policy.prompt_summary()))
        prompt = (
            base_system_prompt
            + "\n\nREQUEST POLICY:\n```yaml\n"
            + policy_summary
            + "\n```\nFollow this policy. If `allow_run_blocks` is false, do not call block-running tools. "
            + "Exception: when `clarification_reason` is `workflow_credential_inputs_unbound` or "
            + "`credential_name_unresolved` and "
            + "`allow_missing_credentials_in_draft` is true, call `update_and_run_blocks`; it will save the draft "
            + "workflow and skip the browser run with a credential setup message. "
            + "If `raw_secret_handling` is `redacted_draft`, build only from the redacted request, do not run blocks, "
            + "and tell the user to store the redacted secret as a saved credential before testing. "
            + "If `resolved_credentials` are present, use those `credential_id` values."
        )
        return (
            prompt
            + _runtime_verification_evidence_prompt(ctx)
            + _recorded_build_test_outcome_prompt(ctx)
            + _code_authoring_repair_context_prompt(ctx)
            + _synthesized_block_offer_prompt(ctx)
            + _docs_answer_turn_directive(ctx.turn_intent)
        )

    return instructions


def _docs_answer_turn_directive(turn_intent: TurnIntent | None) -> str:
    """Prompt-side complement to the no-mutation tool gate — keeps a docs-answer
    turn from substituting a routing question or build offer for the inline answer."""
    if not isinstance(turn_intent, TurnIntent) or turn_intent.mode != TurnIntentMode.DOCS_ANSWER:
        return ""
    return (
        "\n\nTURN INTENT: docs_answer\n"
        "This turn is a documentation or explanation question. Answer it inline in the user's language. "
        "Do not ask whether the user wants a workflow change instead, do not re-ask a confirmation the "
        "prior turn already covered, and do not offer to build an example workflow in place of answering."
    )


def _build_user_context(
    workflow_yaml: str,
    chat_history_text: str,
    global_llm_context: str,
    debug_run_info_text: str,
    user_message: str,
    request_policy_summary: str = "",
    user_workflow_change_summary: str = "",
    runnable_draft_summary: str = "",
    repeated_reply_warning: str = "",
) -> str:
    """Render untrusted context into the user message with code fencing.

    Every argument is treated as untrusted and passed through
    ``escape_code_fences`` before the template interpolates it into a
    triple-backtick block. Without this, a value containing a literal
    ``` would close the fence early and let the model see the rest as
    system-level content (the classic code-fence breakout).
    """
    workflow_yaml = redact_raw_secrets_for_prompt(workflow_yaml or "")
    global_llm_context = sanitize_global_llm_context_for_prompt(global_llm_context)
    loaded_result_context = render_loaded_result_context_for_prompt(global_llm_context)
    return prompt_engine.load_prompt(
        template="workflow-copilot-user",
        workflow_yaml=escape_code_fences(workflow_yaml),
        workflow_summary=escape_code_fences(_build_workflow_summary(workflow_yaml)),
        chat_history=escape_code_fences(redact_raw_secrets_for_prompt(chat_history_text)),
        global_llm_context=escape_code_fences(redact_raw_secrets_for_prompt(global_llm_context)),
        loaded_result_context=escape_code_fences(redact_raw_secrets_for_prompt(loaded_result_context)),
        debug_run_info=escape_code_fences(redact_raw_secrets_for_prompt(debug_run_info_text)),
        request_policy_summary=escape_code_fences(redact_raw_secrets_for_prompt(request_policy_summary)),
        user_message=escape_code_fences(redact_raw_secrets_for_prompt(user_message)),
        user_workflow_change_summary=escape_code_fences(user_workflow_change_summary or ""),
        runnable_draft_summary=escape_code_fences(runnable_draft_summary or ""),
        repeated_reply_warning=escape_code_fences(repeated_reply_warning or ""),
    )


def _truncate_summary_text(value: Any, max_chars: int = 240) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _block_summary_lines(blocks: list[Any], *, depth: int = 0) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    for block in blocks:
        if not isinstance(block, dict):
            continue

        label = block.get("label") or "(unlabeled)"
        block_type = block.get("block_type") or "unknown"
        line_parts = [f"{indent}- {label} ({block_type})"]
        next_label = block.get("next_block_label")
        if next_label:
            line_parts.append(f"next={next_label}")

        error_code_mapping = block.get("error_code_mapping")
        if isinstance(error_code_mapping, dict) and error_code_mapping:
            mappings = [f"{code}: {_truncate_summary_text(reason)}" for code, reason in error_code_mapping.items()]
            line_parts.append("error_code_mapping={" + "; ".join(mappings) + "}")

        branch_conditions = block.get("branch_conditions")
        if isinstance(branch_conditions, list) and branch_conditions:
            branch_targets = []
            for branch in branch_conditions:
                if not isinstance(branch, dict):
                    continue
                target = branch.get("next_block_label")
                if target:
                    prefix = "default -> " if branch.get("is_default") else "branch -> "
                    branch_targets.append(prefix + str(target))
            if branch_targets:
                line_parts.append("branches=[" + ", ".join(branch_targets) + "]")

        lines.append("; ".join(line_parts))

        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list) and loop_blocks:
            lines.extend(_block_summary_lines(loop_blocks, depth=depth + 1))

    return lines


def _build_workflow_summary(workflow_yaml: str | None) -> str:
    """Return a compact block index for the model before the full YAML.

    The full workflow YAML remains the source of truth, but large block goals
    can bury later labels and per-block error mappings. This summary gives
    block-specific debug turns a cheap index so an existing label like
    ``block_2`` is not missed before the model inspects details in the YAML.
    """
    if not workflow_yaml:
        return ""
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""

    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return ""
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return ""

    lines = _block_summary_lines(blocks)
    if not lines:
        return ""
    summary = "\n".join(lines)
    max_summary_chars = 12_000
    if len(summary) > max_summary_chars:
        return summary[: max_summary_chars - 80].rstrip() + "\n... workflow summary truncated ..."
    return summary


def _build_tool_usage_guide(tool_names_and_descriptions: list[tuple[str, str]]) -> str:
    if not tool_names_and_descriptions:
        return ""
    return "\n".join(
        f"- **{name}** — {description or 'No description provided.'}"
        for name, description in tool_names_and_descriptions
    )


def _turn_intent_disables_tools(turn_intent: TurnIntent | None) -> bool:
    if not isinstance(turn_intent, TurnIntent) or turn_intent.mode not in NO_MUTATION_TURN_INTENT_MODES:
        return False

    authority = turn_intent.authority
    return not authority.may_update_workflow and not authority.may_run_blocks


_DRAFT_ONLY_MCP_TOOL_ALLOWLIST = frozenset({"get_block_schema", "validate_block"})
_DRAFT_ONLY_NATIVE_TOOL_DENYLIST = frozenset(
    {"discover_workflow_entrypoint", "inspect_page_for_composition", "fill_credential_field"}
)
_MUTATING_BROWSER_SCOUT_TOOLS = frozenset({"click", "press_key", "type_text", "select_option", "fill_credential_field"})
_STRUCTURAL_CANNOT_ACT_REASON_CODES = frozenset(
    {OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE, OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE}
)
_FinalActionDataValue = str | int | float | bool | None
_ACTUATION_OBLIGATION_STEER_REPLY = (
    "I need to act in the browser for this request. Please let me try the browser action, or tell me what blocks it."
)
_ACTUATION_OBLIGATION_UNMET_REPLY = (
    "I still did not act in the browser for this request, so I cannot treat the task as completed. "
    "Please retry or tell me what prevented the browser action."
)


def _request_policy_disables_browser_scout_tools(request_policy: RequestPolicy | None) -> bool:
    return (
        isinstance(request_policy, RequestPolicy)
        and request_policy.allow_update_workflow
        and not request_policy.allow_run_blocks
        and (request_policy.testing_intent == "skip_test" or request_policy.allow_missing_credentials_in_draft)
    )


def _turn_intent_requires_browser_state(turn_intent: TurnIntent | None) -> bool:
    return isinstance(turn_intent, TurnIntent) and RequiredContextKey.BROWSER_STATE in turn_intent.required_context


def _turn_intent_browser_only_tools(turn_intent: TurnIntent | None) -> bool:
    return (
        isinstance(turn_intent, TurnIntent)
        and turn_intent.mode == TurnIntentMode.BUILD
        and RequiredContextKey.BROWSER_STATE in turn_intent.required_context
        and not turn_intent.authority.may_update_workflow
        and not turn_intent.authority.may_run_blocks
    )


def _successful_mutating_browser_action_count(ctx: CopilotContext) -> int:
    return sum(
        1
        for interaction in ctx.scout_trajectory
        if isinstance(interaction, Mapping) and interaction.get("tool_name") in _MUTATING_BROWSER_SCOUT_TOOLS
    )


def _successful_durable_fill_count(ctx: CopilotContext) -> int:
    return int(trajectory_has_browser_fill_interaction(ctx.scout_trajectory))


def _typed_cannot_act_reason(ctx: CopilotContext) -> CannotActReason | None:
    signal = ctx.blocker_signal if isinstance(ctx.blocker_signal, CopilotToolBlockerSignal) else None
    if signal is None:
        return None
    if signal.blocker_kind == "authority_denied":
        return None
    if signal.blocker_kind == "missing_required_context":
        return CannotActReason.MISSING_FIELD_VALUE
    if signal.recovery_hint == "ask_user_clarifying":
        return CannotActReason.AMBIGUOUS_TARGET
    if signal.internal_reason_code in _STRUCTURAL_CANNOT_ACT_REASON_CODES:
        return CannotActReason.STRUCTURAL_BLOCKER
    return None


def _evaluate_actuation_obligation_for_output(
    ctx: CopilotContext,
    action_data: Mapping[str, _FinalActionDataValue],
    response_type: str,
    output_kind: CopilotOutputKind,
) -> ActuationObligationEvaluation:
    cannot_act_reason = _typed_cannot_act_reason(ctx)
    evaluation = evaluate_actuation_obligation(
        turn_intent=ctx.turn_intent,
        response_type=response_type,
        output_kind=output_kind,
        successful_mutating_browser_actions=_successful_mutating_browser_action_count(ctx),
        cannot_act_reason=cannot_act_reason,
        prior_turn_outcome=ctx.prior_turn_outcome,
    )
    if evaluation.status != ActuationObligationStatus.ALLOWED:
        return evaluation
    if (
        not _turn_intent_browser_only_tools(ctx.turn_intent)
        or not request_policy_requires_durable_fill(ctx.request_policy)
        or response_type != "REPLY"
        or output_kind != CopilotOutputKind.INFORMATIONAL_ANSWER
    ):
        return evaluation
    if _successful_durable_fill_count(ctx) > 0 or cannot_act_reason is not None:
        return evaluation
    obligation_key = actuation_obligation_key(ctx.turn_intent)
    if prior_turn_satisfies_actuation_terminal_condition(ctx.prior_turn_outcome, obligation_key):
        return ActuationObligationEvaluation(
            status=ActuationObligationStatus.TERMINAL,
            reason_code=ACTUATION_OBLIGATION_UNMET_REASON_CODE,
            obligation_key=obligation_key,
        )
    return ActuationObligationEvaluation(
        status=ActuationObligationStatus.STEER,
        reason_code=ACTUATION_OBLIGATION_STEER_REASON_CODE,
        obligation_key=obligation_key,
    )


def _actuation_obligation_diagnostics(
    ctx: CopilotContext,
    evaluation: ActuationObligationEvaluation,
) -> dict[str, str | int | bool | None]:
    if (
        not turn_intent_requires_actuation(ctx.turn_intent)
        and evaluation.status == ActuationObligationStatus.ALLOWED
        and evaluation.cannot_act_reason is None
    ):
        return {}
    return {
        "actuation_obligation_status": evaluation.status.value,
        "actuation_obligation_reason_code": evaluation.reason_code or None,
        "actuation_obligation_key": evaluation.obligation_key or None,
        "cannot_act_reason": evaluation.cannot_act_reason.value if evaluation.cannot_act_reason is not None else None,
        "successful_mutating_browser_actions": _successful_mutating_browser_action_count(ctx),
        "durable_fill_required": request_policy_requires_durable_fill(ctx.request_policy),
        "successful_durable_browser_fills": _successful_durable_fill_count(ctx),
    }


def _mcp_tool_surface_for_turn(
    alias_map: dict[str, str],
    overlays: dict[str, Any],
    turn_intent: TurnIntent | None,
    request_policy: RequestPolicy | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    if _turn_intent_disables_tools(turn_intent):
        return {}, {}
    if _request_policy_disables_browser_scout_tools(request_policy):
        return (
            {name: target for name, target in alias_map.items() if name in _DRAFT_ONLY_MCP_TOOL_ALLOWLIST},
            {name: overlay for name, overlay in overlays.items() if name in _DRAFT_ONLY_MCP_TOOL_ALLOWLIST},
        )
    return alias_map, overlays


def _native_tools_for_turn(
    native_tools: list[Any],
    turn_intent: TurnIntent | None,
    request_policy: RequestPolicy | None = None,
) -> list[Any]:
    # Keep native tools registered even when the current turn is not allowed to
    # use them. The tool implementations enforce TurnIntent/RequestPolicy
    # authority and return structured blockers; removing a tool lets the model
    # hit an SDK-level ModelBehaviorError if static prompt text still names it.
    if _request_policy_disables_browser_scout_tools(request_policy):
        return [tool for tool in native_tools if getattr(tool, "name", None) not in _DRAFT_ONLY_NATIVE_TOOL_DENYLIST]
    return list(native_tools)


def _is_explicit_false(value: Any) -> bool:
    # LLMs occasionally serialise JSON booleans as strings; coerce the common spellings.
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}


def _normalize_failure_reason(failure_reason: str | None) -> str:
    if not failure_reason:
        return "The workflow test run failed."

    normalized = failure_reason.split("Call log:", 1)[0].strip()
    normalized = " ".join(normalized.split())
    if len(normalized) > 240:
        normalized = normalized[:237].rstrip() + "..."
    return normalized or "The workflow test run failed."


_FAILURE_FOLLOW_UP = {
    "NAVIGATION_FAILURE": " Can you confirm the URL is correct?",
    "PROXY_ERROR": " Want me to retry with a different proxy location?",
    "PAGE_LOAD_TIMEOUT": " Can you confirm the URL and try again in a moment?",
    "ANTI_BOT_DETECTION": " Want me to retry with a different proxy location?",
    "AUTH_FAILURE": " The site rejected the login — is the stored password still valid?",
    "CREDENTIAL_ERROR": " I couldn't find a credential to use — can you link one in Settings?",
}


def _join_capped_labels(labels: list[str], cap: int = 6) -> str:
    shown = ", ".join(labels[:cap])
    return f"{shown}, ..." if len(labels) > cap else shown


def _partial_verification_response(ctx: CopilotContext) -> str | None:
    evidence = ctx.workflow_verification_evidence
    if not evidence.has_evidence():
        return None
    if evidence.full_workflow_verified:
        return None

    coverage_complete = (
        bool(evidence.block_verified) and not evidence.unverified_block_labels and not evidence.per_tool_budget_on_block
    )
    if coverage_complete:
        count = len(evidence.block_verified)
        block_word = "block" if count == 1 else "blocks"
        labels = _join_capped_labels(evidence.block_verified)
        failure_reason = (evidence.failure_reason or "").strip()
        if failure_reason:
            return (
                f"I saved a draft workflow and ran all {count} {block_word} ({labels}), but the run did not "
                f"confirm the workflow end-to-end: {failure_reason}. Keep the draft to iterate on, or discard."
            )
        return (
            f"I saved a draft workflow and ran all {count} {block_word} ({labels}), but I couldn't confirm the "
            "workflow end-to-end this turn. Keep the draft to iterate on, or discard."
        )

    detail_parts: list[str] = []
    if evidence.block_verified:
        detail_parts.append("verified block(s): " + _join_capped_labels(evidence.block_verified))
    if evidence.live_page_state_verified:
        page = evidence.page_title or evidence.current_url or "the current browser page"
        detail_parts.append(f"verified current browser state: {page}")
    if evidence.per_tool_budget_on_block:
        detail_parts.append("per-tool budget hit on: " + _join_capped_labels(evidence.per_tool_budget_on_block))
    if evidence.unverified_block_labels:
        detail_parts.append("unverified block(s): " + _join_capped_labels(evidence.unverified_block_labels))

    details = " ".join(detail_parts)
    if details:
        return (
            "I saved a draft workflow and verified part of it, but the full workflow chain has not been "
            f"verified end-to-end. {details}. Keep the draft to iterate on, or discard."
        )
    return (
        "I saved a draft workflow, but the full workflow chain has not been verified end-to-end. "
        "Keep the draft to iterate on, or discard."
    )


def _rewrite_failed_test_response(user_response: str, ctx: CopilotContext) -> str:
    has_keepable_draft = ctx.last_workflow is not None and bool(ctx.last_workflow_yaml)
    keep_draft_affordance = " Keep the draft to iterate on, or discard." if has_keepable_draft else ""
    block_count = ctx.last_update_block_count if isinstance(ctx.last_update_block_count, int) else None
    positive_block_count = block_count if block_count is not None and block_count > 0 else None

    if outcome_fully_verified(ctx) and has_keepable_draft:
        if positive_block_count is not None:
            block_word = "block" if positive_block_count == 1 else "blocks"
            return (
                f"I created a workflow with {positive_block_count} {block_word} and verified the requested "
                "outcome from workflow run evidence and the current browser page. The workflow is ready to review."
            )
        return (
            "I built the workflow and verified the requested outcome from workflow run evidence and the current "
            "browser page. The workflow is ready to review."
        )

    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if (
        policy is not None
        and policy.clarification_reason == "workflow_credential_inputs_unbound"
        and ctx.last_workflow is not None
        and block_count is not None
    ):
        if positive_block_count is None:
            draft_phrase = "a draft workflow"
        else:
            block_word = "block" if positive_block_count == 1 else "blocks"
            draft_phrase = f"a draft workflow with {positive_block_count} {block_word}"
        return (
            f"I applied your requested change as {draft_phrase}. "
            f"I couldn't test the modified workflow because I couldn't find the required credentials — "
            f"please add them via the Credentials UI, then I can try again.{keep_draft_affordance}"
        )

    if ctx.last_test_ok is False and block_count is not None:
        if positive_block_count is None:
            draft_phrase = "a draft workflow"
        else:
            block_word = "block" if positive_block_count == 1 else "blocks"
            draft_phrase = f"a draft workflow with {positive_block_count} {block_word}"

        failure_summary = _normalize_failure_reason(ctx.last_test_failure_reason)
        follow_up = _FAILURE_FOLLOW_UP.get(ctx.last_failure_category_top or "", "")
        return (
            f"I created {draft_phrase} and tested it, but the test failed. "
            f"Failure: {failure_summary}.{follow_up}{keep_draft_affordance}"
        )

    if ctx.last_test_ok is True and ctx.last_full_workflow_test_ok is False and has_keepable_draft:
        partial_reply = _partial_verification_response(ctx)
        if partial_reply is not None:
            return partial_reply

    if ctx.last_test_ok is None and block_count is not None and ctx.last_workflow is not None:
        if policy is not None and policy.raw_secret_handling == "redacted_draft":
            return (
                "I drafted the workflow with the pasted secret redacted. "
                "Store the secret as a saved credential before testing; this draft has not been verified end-to-end."
            )
        if ctx.allow_untested_workflow_draft:
            return (
                "I drafted the workflow without testing it, as requested. "
                "You can accept it to save, but it has not been verified end-to-end."
            )
        if has_keepable_draft:
            return (
                "I drafted an update but wasn't able to verify it this turn. "
                "Keep the draft to iterate on it manually, or discard."
            )
        return (
            "I drafted an update but wasn't able to verify it this turn. "
            "Could you share more context about what you'd like me to do?"
        )

    return user_response


def _shape_ask_question_response(user_response: str, ctx: CopilotContext) -> str:
    from skyvern.forge.sdk.copilot.enforcement import build_probable_site_block_user_question

    site_block_question = build_probable_site_block_user_question(ctx)
    if site_block_question is not None:
        return site_block_question
    return user_response


def _completion_contract_not_violated(ctx: CopilotContext) -> bool:
    if artifact_health_blocked(ctx):
        return False
    if outcome_fully_verified(ctx):
        return True
    result = ctx.completion_verification_result
    if result is None:
        return True
    if result.status != "evaluated":
        # Verification was required for this run but could not produce a verdict
        # (unavailable): do not surface the workflow as verified on run status alone.
        return False
    if result.is_fully_satisfied():
        return True
    return only_structural_requested_output_abstentions(result)


def _verified_workflow_or_none(ctx: CopilotContext) -> tuple[Any, str | None]:
    """Surface a proposal when it passed a test this turn, or when the outcome judge
    confirmed the goal from evidence even though the run did not finish cleanly."""
    run_status_clean = ctx.last_test_ok is True and ctx.last_full_workflow_test_ok is True
    if (
        ctx.last_workflow is not None
        and ctx.last_workflow_yaml
        and (run_status_clean or outcome_fully_verified(ctx))
        and _completion_contract_not_violated(ctx)
    ):
        return ctx.last_workflow, ctx.last_workflow_yaml
    return None, None


_BUILT_UNVERIFIED_COMPLETED_REPLY = (
    "I built the workflow and the test run completed, but the goal outcome was not independently verified. "
    "The workflow is available on the canvas for review."
)


def _should_use_built_unverified_completed_reply(
    ctx: CopilotContext,
    *,
    response_type: str,
    updated_workflow: Any,
    validated: bool,
    blocker_active: bool,
) -> bool:
    return (
        response_type == "REPLY"
        and updated_workflow is not None
        and validated
        and not blocker_active
        and ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and not ctx.last_test_suspicious_success
        and not verified_goal_claim_authorized(ctx)
    )


def _make_agent_result(
    ctx: CopilotContext | None,
    *,
    global_llm_context: str | None = None,
    turn_outcome: TurnOutcome | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Sole ``AgentResult`` constructor in this module.

    Routes every ``AgentResult`` through the discovery-counter finalizer so
    the per-chat budget survives every exit path (timeout, cancel, max-turns,
    output-policy block, clarification helpers, non-retriable nav error,
    normal translate-result, missing-SDK fallback, unexpected-error fallback).
    """
    final_context = (
        finalize_discovery_counter_in_global_llm_context(ctx, global_llm_context)
        if ctx is not None
        else global_llm_context
    )
    narrative_payload = kwargs.get("narrative_payload")
    if ctx is not None and narrative_payload is None:
        raise ValueError("_make_agent_result requires narrative_payload when ctx is provided")
    response_type = kwargs.get("response_type", "REPLY")
    proposal_disposition = kwargs.get("proposal_disposition")
    if isinstance(narrative_payload, dict):
        payload_updates: dict[str, Any] = {}
        if "responseType" not in narrative_payload:
            payload_updates["responseType"] = response_type
        if proposal_disposition is not None and "proposalDisposition" not in narrative_payload:
            payload_updates["proposalDisposition"] = proposal_disposition
        if turn_outcome is not None and "responseKind" not in narrative_payload:
            payload_updates["responseKind"] = turn_outcome.response_kind.value
        if "credentialPrompt" not in narrative_payload:
            policy = ctx.request_policy if ctx is not None else None
            reason = credential_prompt_reason(policy, kwargs.get("user_response"))
            if reason:
                payload_updates["credentialPrompt"] = {"reason": reason}
        if ctx is not None and "verifiedSuccess" not in narrative_payload:
            payload_updates["verifiedSuccess"] = bool(verified_goal_claim_authorized(ctx))
        if ctx is not None and "outcomeAdjudication" not in narrative_payload:
            adjudication = _build_outcome_adjudication_payload(ctx)
            if adjudication is not None:
                payload_updates["outcomeAdjudication"] = adjudication
        if payload_updates:
            kwargs["narrative_payload"] = {**narrative_payload, **payload_updates}
    result = AgentResult(global_llm_context=final_context, turn_outcome=turn_outcome, **kwargs)
    if ctx is not None and result.turn_outcome is not None:
        result.turn_outcome = with_copilot_code_mode_diagnostics(result.turn_outcome, ctx)
    if ctx is not None and not result.apply_without_review:
        result.apply_without_review = _should_apply_code_only_success_without_review(ctx, result.proposal_disposition)
    if ctx is not None and result.completion_criteria_turn_state is None:
        result.completion_criteria_turn_state = getattr(ctx, "completion_criteria_turn_state", None)
    if ctx is not None and result.code_artifact_metadata is None:
        evidence_metadata = getattr(
            getattr(ctx, "workflow_verification_evidence", None), "code_artifact_metadata", None
        )
        ctx_metadata = getattr(ctx, "code_artifact_metadata", None)
        if isinstance(evidence_metadata, dict) and evidence_metadata:
            result.code_artifact_metadata = evidence_metadata
        elif isinstance(ctx_metadata, dict) and ctx_metadata:
            result.code_artifact_metadata = ctx_metadata
    return result


def _should_apply_code_only_success_without_review(ctx: CopilotContext, disposition: object) -> bool:
    return (
        disposition == "auto_applicable"
        and verified_goal_claim_authorized(ctx)
        and ctx.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER
        and ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and not ctx.last_test_suspicious_success
        and ctx.has_staged_proposal
        and ctx.staged_workflow is not None
    )


def _build_outcome_adjudication_payload(ctx: CopilotContext) -> NarrativeOutcomeAdjudication | None:
    turn_state = getattr(ctx, "completion_criteria_turn_state", None)
    if turn_state is None:
        return None
    counts = turn_state.last_verdict_state_counts or {}
    payload: NarrativeOutcomeAdjudication = {
        "satisfiedCount": int(counts.get("satisfied", 0)),
        "unsatisfiedCount": int(counts.get("unsatisfied", 0)),
        "unknownCount": int(counts.get("unknown", 0)),
        "claimTier": "verified_goal_satisfied" if verified_goal_claim_authorized(ctx) else "built_unverified",
    }
    if turn_state.decision is not None:
        payload["criteriaEpoch"] = turn_state.decision.epoch
        payload["criteriaLifecycleReason"] = turn_state.decision.reason
    return payload


_BLOCK_STATUS_TO_UI_STATE: dict[str, str] = {
    "running": "running",
    "completed": "completed",
    "skipped": "skipped",
    "failed": "failed",
    "terminated": "failed",
    "timed_out": "failed",
    "canceled": "failed",
    "queued": "queued",
}


def _block_ui_state(raw_status: str | None, *, drafted_fallback: bool) -> str:
    # No status + drafted_fallback => stage-only block, distinct from "queued".
    if raw_status is None:
        return "drafted" if drafted_fallback else "queued"
    return _BLOCK_STATUS_TO_UI_STATE.get(raw_status, "queued")


def _build_narrative_payload(
    ctx: CopilotContext,
    *,
    terminal: str,
    terminal_message: str | None,
    narrative_summary: str | None,
) -> TurnNarrativePayload:
    mode_value = ctx.turn_intent.mode.value if ctx.turn_intent is not None else "unknown"
    narrator_state = ctx.narrator_state
    block_activity: dict[str, list[NarrativeActivityEntry]] = (
        narrator_state.block_activity if narrator_state is not None else {}
    )
    design_activity: list[NarrativeActivityEntry] = narrator_state.design_activity if narrator_state is not None else []
    block_labels: list[str] = []
    blocks: list[NarrativeBlock] = []
    recorded_outcome = ctx.last_run_outcome
    outcome_labels = set(ctx.last_run_outcome_block_labels) if recorded_outcome is not None else set()
    staged = ctx.staged_workflow
    if staged is not None and getattr(staged, "workflow_definition", None) is not None:
        for block in staged.workflow_definition.blocks:
            label = getattr(block, "label", None)
            if not isinstance(label, str) or not label:
                continue
            block_labels.append(label)
            block_type_value = getattr(block, "block_type", None)
            if block_type_value is not None and hasattr(block_type_value, "value"):
                block_type = block_type_value.value
            else:
                block_type = str(block_type_value or "task")
            raw_status = ctx.block_state_map.get(label)
            block_entry: NarrativeBlock = {
                "label": label,
                "blockType": block_type,
                "state": _block_ui_state(
                    raw_status,
                    drafted_fallback=ctx.has_staged_proposal,
                ),
                "lastSeenIteration": 0,
                "activity": list(block_activity.get(label, [])),
                "startedAt": ctx.block_started_at_map.get(label),
                "endedAt": ctx.block_ended_at_map.get(label),
            }
            if recorded_outcome is not None and label in outcome_labels:
                block_entry["outcome"] = recorded_outcome.verdict
                if recorded_outcome.display_reason is not None:
                    block_entry["outcomeReason"] = recorded_outcome.display_reason
            blocks.append(block_entry)
    draft: NarrativeDraft | None = (
        {"blockCount": len(block_labels), "blockLabels": block_labels, "summary": None}
        if ctx.has_staged_proposal
        else None
    )
    # First terminal builder to reach here seals the turn-level end time;
    # later exit paths reuse it so the persisted elapsed matches the live one.
    if ctx.turn_ended_at is None:
        ctx.turn_ended_at = datetime.now(timezone.utc).isoformat()
    return {
        "turnId": ctx.turn_id,
        "turnIndex": ctx.turn_index,
        "mode": mode_value,
        "designStarted": True,
        "designEnded": True,
        "draft": draft,
        "blocks": blocks,
        "terminal": terminal,
        "terminalMessage": terminal_message,
        "narrativeSummary": narrative_summary or terminal_message,
        "priorBlockCount": ctx.prior_block_count,
        "designActivity": list(design_activity),
        "startedAt": ctx.turn_started_at,
        "endedAt": ctx.turn_ended_at,
    }


def _log_output_policy_parity(ctx: CopilotContext, *, has_workflow_proposal: bool, workflow_attempted: bool) -> None:
    LOG.info(
        "copilot.output_policy_parity",
        has_workflow_proposal=has_workflow_proposal,
        workflow_attempted=workflow_attempted,
        **ctx.genuine_attempt_parity_fields(),
    )


def _build_exit_result(
    ctx: CopilotContext,
    user_response: str,
    global_llm_context: str | None,
    cancelled: bool = False,
    terminal_reason: str | None = None,
) -> AgentResult:
    """AgentResult for agent-loop exits that don't go through ``_translate_to_agent_result``."""
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    final_text, outcome = apply_repeated_reply_guard(
        final_text=user_response,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=ctx.blocked_reply_signatures,
        terminal_reason=terminal_reason or ("cancel" if cancelled else None),
    )
    workflow_attempted = ctx.has_genuine_workflow_attempt()
    _log_output_policy_parity(
        ctx, has_workflow_proposal=verified_workflow is not None, workflow_attempted=workflow_attempted
    )
    output_kind = derive_output_kind(
        response_type="REPLY",
        request_policy=ctx.request_policy,
        updated_workflow=verified_workflow,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=False,
    )
    raw_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type="REPLY",
        user_response=final_text,
        global_llm_context=global_llm_context,
        workflow_yaml=verified_yaml,
        has_workflow_proposal=verified_workflow is not None,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=False,
        output_kind=output_kind,
    )
    if not raw_verdict.allowed:
        hard_block_verdict = hard_block_output_policy_verdict(raw_verdict)
        soft_rewrite_reasons = [r for r in raw_verdict.reason_codes if r not in hard_block_verdict.reason_codes]
        return _build_output_policy_blocked_result(
            ctx,
            raw_verdict,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=verified_yaml,
            output_policy_diagnostics=build_output_policy_diagnostics(
                raw_verdict=raw_verdict,
                final_verdict=raw_verdict,
                final_output_kind=_blocked_final_output_kind(raw_verdict),
                hard_block_reason_codes=list(hard_block_verdict.reason_codes),
                soft_rewrite_reason_codes=soft_rewrite_reasons,
            ),
        )
    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_text,
            updated_workflow=verified_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=verified_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            has_staged_proposal=ctx.has_staged_proposal,
            staged_workflow_yaml=ctx.staged_workflow_yaml,
            staged_workflow=ctx.staged_workflow,
            canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
            total_tokens=ctx.total_tokens_used,
            cancelled=cancelled,
            turn_outcome=outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="error" if cancelled or terminal_reason else "response",
                terminal_message=final_text,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site="exit_result",
    )


async def _build_goal_satisfied_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    *,
    terminal_reason: str = "verified_goal_satisfied",
    exit_site: str = "verified_goal_satisfied",
    flush_goal_satisfied: bool = True,
) -> AgentResult:
    # Bypass one extra LLM turn after a full workflow test already satisfies
    # the diagnosis contract.
    if flush_goal_satisfied and ctx.stream is not None:
        try:
            await flush_goal_satisfied_tool_result(ctx.stream, ctx)
        except Exception as flush_err:
            LOG.warning("copilot_goal_satisfied_tool_result_flush_failed", error=str(flush_err))
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    clean_test = ctx.last_test_ok is True and ctx.last_full_workflow_test_ok is True
    if clean_test and verified_goal_claim_authorized(ctx):
        user_response = _verified_workflow_success_reply(ctx)
    elif clean_test:
        user_response = (
            "I built the workflow and the test run completed, but the goal outcome was not "
            "independently verified. Review the draft to confirm it does what you need."
        )
    elif ctx.last_test_ok is False:
        user_response = "I reached the requested outcome, but the workflow test did not finish successfully."
    else:
        user_response = (
            "I reached the requested outcome, but the workflow has not been tested end-to-end. "
            "Review the draft before using it."
        )
    final_text, outcome = apply_repeated_reply_guard(
        final_text=user_response,
        attempted_kind=derive_response_kind(ctx.turn_intent),
        blocked_signatures=ctx.blocked_reply_signatures,
        terminal_reason=terminal_reason,
        turn_intent=ctx.turn_intent,
        tool_calls=[
            str(entry.get("tool") or entry.get("name") or "")
            for entry in ctx.tool_activity
            if isinstance(entry, dict) and (entry.get("tool") or entry.get("name"))
        ],
    )
    structured = StructuredContext.from_json_str(global_llm_context)
    structured.merge_turn_summary(ctx.tool_activity)
    enriched_context = structured.to_json_str()
    output_kind = derive_output_kind(
        response_type="REPLY",
        request_policy=ctx.request_policy,
        updated_workflow=verified_workflow,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=True,
        unvalidated=False,
    )
    raw_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type="REPLY",
        user_response=final_text,
        global_llm_context=enriched_context,
        workflow_yaml=verified_yaml,
        has_workflow_proposal=verified_workflow is not None,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=True,
        unvalidated=False,
        output_kind=output_kind,
    )
    if not raw_verdict.allowed:
        hard_block_verdict = hard_block_output_policy_verdict(raw_verdict)
        soft_rewrite_reasons = [r for r in raw_verdict.reason_codes if r not in hard_block_verdict.reason_codes]
        return _build_output_policy_blocked_result(
            ctx,
            raw_verdict,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=verified_yaml,
            output_policy_diagnostics=build_output_policy_diagnostics(
                raw_verdict=raw_verdict,
                final_verdict=raw_verdict,
                final_output_kind=_blocked_final_output_kind(raw_verdict),
                hard_block_reason_codes=list(hard_block_verdict.reason_codes),
                soft_rewrite_reason_codes=soft_rewrite_reasons,
            ),
        )
    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_text,
            updated_workflow=verified_workflow,
            global_llm_context=enriched_context or None,
            response_type="REPLY",
            workflow_yaml=verified_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            total_tokens=ctx.total_tokens_used,
            proposal_disposition="auto_applicable"
            if verified_workflow is not None and verified_goal_claim_authorized(ctx)
            else "review_tested"
            if verified_workflow is not None
            else "no_proposal",
            turn_outcome=outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="response",
                terminal_message=final_text,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site=exit_site,
    )


async def _build_built_unverified_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return await _build_goal_satisfied_exit_result(
        ctx,
        global_llm_context,
        terminal_reason=BUILT_UNVERIFIED_REPAIR_INERT_TERMINAL_REASON,
        exit_site=BUILT_UNVERIFIED_REPAIR_INERT_TERMINAL_REASON,
        flush_goal_satisfied=False,
    )


_SCOUTED_SPINE_HALT_REPLY_KINDS = frozenset({TurnHaltKind.LOOP_DETECTED, TurnHaltKind.REPAIR_CEILING_REACHED})


def _build_turn_halt_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    halt: TurnHalt,
) -> AgentResult:
    under_build_open = log_scouted_spine_unresolved_at_turn_halt(ctx)
    if halt.kind == TurnHaltKind.DELIVERED_UNVERIFIED:
        reply = _delivered_unverified_reply(ctx) or _BUILT_UNVERIFIED_COMPLETED_REPLY
        return _build_wip_exit_result(
            ctx,
            global_llm_context,
            default_reply=reply,
            unvalidated_reply=reply,
            tested_reply=reply,
            terminal_reason=f"turn_halt:{halt.kind.value}",
        )
    signal = halt.blocker_signal
    if isinstance(signal, CopilotToolBlockerSignal) and signal.blocker_kind == "loop_detected":
        refresh_held_loop_blocker_evidence(ctx)
        signal = ctx.blocker_signal if isinstance(ctx.blocker_signal, CopilotToolBlockerSignal) else signal
    if under_build_open and halt.kind in _SCOUTED_SPINE_HALT_REPLY_KINDS:
        user_response = SCOUTED_SPINE_TURN_HALT_USER_REASON
        # The blocker-override finalizer re-renders from the held signal, so the
        # reframed reason must live there, not just in the local reply.
        if isinstance(ctx.blocker_signal, CopilotToolBlockerSignal):
            ctx.blocker_signal = ctx.blocker_signal.model_copy(update={"user_facing_reason": user_response})
    elif isinstance(signal, CopilotToolBlockerSignal):
        user_response = signal.user_facing_reason
    else:
        user_response = "I could not continue this turn safely. Tell me what to change and I'll try again."
    return _build_exit_result(
        ctx,
        user_response,
        global_llm_context,
        terminal_reason=f"turn_halt:{halt.kind.value}",
    )


_TIMEOUT_REPLY_DEFAULT = "I ran out of time processing your request. Here's what I have so far."
_TIMEOUT_REPLY_UNVALIDATED = (
    "I ran out of time before I could finish testing. I have a draft workflow you can keep — "
    "accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_TIMEOUT_REPLY_TESTED = "I ran out of time, but I have a tested draft for you. Accept it to save, or discard."

_MAX_TURNS_REPLY_DEFAULT = "I've reached the maximum number of steps. Here's what I have so far."
_MAX_TURNS_REPLY_UNVALIDATED = (
    "I've reached the maximum number of steps before I could finish testing. I have a draft "
    "workflow you can keep — accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_MAX_TURNS_REPLY_TESTED = (
    "I've reached the maximum number of steps, but I have a tested draft for you. Accept it to save, or discard."
)
_VERIFIED_WORKFLOW_SUCCESS_REPLY = "I created and tested the workflow successfully."
_UNEXPECTED_ERROR_REPLY_UNVALIDATED = (
    "I hit an unexpected issue before I could finish testing. I have a draft workflow you can keep — "
    "accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_UNEXPECTED_ERROR_REPLY_TESTED = (
    "I hit an unexpected issue, but I have a tested draft for you. Accept it to save, or discard."
)
# Ends with RAW_SECRET_REFUSAL_SENTINEL so transcript redaction recognizes this refusal in history.
_RAW_SECRET_LEAK_REFUSAL = (
    "I can't show or save that output because it appears to include raw credentials or secrets. "
    "Store credentials in the Skyvern Credentials UI and reply with the saved credential name or a "
    f"credential ID beginning with cred_. {RAW_SECRET_REFUSAL_SENTINEL}."
)
_SAVED_DRAFT_OUTPUT_POLICY_SUFFIX = "I only blocked the chat reply; the workflow draft is still saved."
_CANCEL_REPLY_DEFAULT = "Cancelled by user."
_CANCEL_REPLY_UNVALIDATED = (
    "Cancelled. I have a draft workflow you can keep — accept it to save "
    "(note: it hasn't been verified end-to-end), or discard."
)
_CANCEL_REPLY_TESTED = "Cancelled. I have a tested draft for you. Accept it to save, or discard."
_UNBACKED_WORKFLOW_DELIVERY_REPLY = (
    "I wasn't able to produce a workflow proposal in this turn, and I couldn't identify which details were missing "
    "from this turn. Please retry with the target site, page, or workflow requirement."
)
_UNBACKED_WORKFLOW_DELIVERY_PREFIX = "I wasn't able to produce a workflow proposal in this turn."

_INLINE_REJECT_NOTE_FALLBACK = (
    "This draft didn't pass validation against the live page, so I haven't saved it. "
    "I'll revise it before proposing again."
)
_GENERIC_MISSING_CONTEXT_PHRASES = (
    "missing details",
    "one more detail",
)
_REQUIRED_CONTEXT_LABELS = {
    RequiredContextKey.CURRENT_WORKFLOW: "the current workflow",
    RequiredContextKey.PROPOSED_WORKFLOW: "the proposed workflow",
    RequiredContextKey.LATEST_ASSISTANT_PROPOSAL: "the latest workflow proposal",
    RequiredContextKey.WORKFLOW_CHANGE: "the workflow change to apply",
    RequiredContextKey.LATEST_RUN_RESULT: "the latest run result",
    RequiredContextKey.CREDENTIAL_METADATA: "the saved credential metadata",
    RequiredContextKey.DOCS_CONTEXT: "the relevant documentation context",
    RequiredContextKey.BROWSER_STATE: "the current browser tab or page state",
}
_DIAGNOSIS_MISSING_CONTEXT_LABELS = {
    "workflow_run_id": "the workflow run ID",
    "block_results": "the block run results",
    "failure_reason": "the failure reason",
}
_INTERNAL_BLOCK_TAXONOMY_REPLY = (
    "Internal workflow names are not the right interface to use when building with Copilot. "
    "Describe the page action, data to collect, sign-in step, or check you want, and I'll translate that into "
    "a supported workflow update."
)
_INTERNAL_VOCAB_LEAK_REPLY = (
    "Tell me what you'd like to do next — describe the page action, data to collect, sign-in step, "
    "or check you want, and I'll translate that into a supported workflow update."
)
_BLOCK_YAML_IN_REPLY_REWRITE_NO_PROPOSAL = (
    "I drafted a change to the workflow but haven't applied it yet. Want me to update the workflow now?"
)
_BLOCK_YAML_IN_REPLY_REWRITE_WITH_PROPOSAL = "I made the change you described to the workflow."
_PROPOSAL_ACCEPT_UI_ACTION_RE = re.compile(r"\b(?:accept|always\s+accept)\b", re.IGNORECASE)
_PROPOSAL_REJECT_UI_ACTION_RE = re.compile(r"\b(?:reject|discard)\b", re.IGNORECASE)
_UNVALIDATED_PROPOSAL_AFFORDANCE = (
    "I have a draft workflow proposal. Use Review to inspect it, Accept to save it, or Reject to discard it. "
    "It has not been tested or verified end-to-end."
)
_VERIFIED_CLASSIFICATION_CONTEXT_KEYS = (
    "visible_page_path_label",
    "safest_reachable_next_step",
    "recommended_next_action",
)
_VERIFIED_CLASSIFICATION_GATE_KEYS = (
    "observed_gate_phrase",
    "gate_phrase",
    "gate_summary",
    "blocked_by",
)
_VERIFIED_CLASSIFICATION_GATE_PHRASES = ("Sign in or register to continue",)
_VERIFIED_TERMINAL_VALUE_MAX_CHARS = 180


def _terminal_summary_scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned or contains_internal_machinery_leak(cleaned):
        return None
    if len(cleaned) > _VERIFIED_TERMINAL_VALUE_MAX_CHARS:
        cleaned = cleaned[: _VERIFIED_TERMINAL_VALUE_MAX_CHARS - 1].rstrip() + "..."
    return cleaned


def _delivered_unverified_reply(ctx: CopilotContext) -> str | None:
    if getattr(ctx, "delivered_unverified_terminal", False) is not True:
        return None
    parts: list[str] = []
    observed_outputs = getattr(ctx, "delivered_unverified_observed_outputs", {})
    if not isinstance(observed_outputs, dict):
        observed_outputs = {}
    for key, value in observed_outputs.items():
        rendered = _terminal_summary_scalar(value)
        if rendered is None and isinstance(value, list | dict):
            try:
                rendered = redact_raw_secrets_for_prompt(json.dumps(value, sort_keys=True))
            except TypeError:
                rendered = None
        if isinstance(key, str) and key.strip() and rendered and not contains_internal_machinery_leak(rendered):
            parts.append(f"{key}: {rendered[:_VERIFIED_TERMINAL_VALUE_MAX_CHARS]}")
    if parts:
        return (
            "I built and ran the workflow. The latest run returned "
            f"{'; '.join(parts[:4])}. That value was not independently verified, so review the draft before using it."
        )
    return (
        "I built and ran the workflow, and the latest run returned the requested output. "
        "That output was not independently verified, so review the draft before using it."
    )


def _verified_output_value(ctx: CopilotContext, output_key: str | None) -> Any:
    if not output_key:
        return None
    block_outputs = ctx.verified_terminal_block_outputs or ctx.verified_block_outputs or {}
    for output in block_outputs.values():
        if isinstance(output, dict) and output_key in output:
            return output.get(output_key)
    return None


def _verified_adjacent_output_parts(ctx: CopilotContext) -> list[str]:
    parts: list[str] = []
    block_outputs = ctx.verified_terminal_block_outputs or ctx.verified_block_outputs or {}
    login_gate_phrase_found = False

    def append_gate_phrase(value: Any) -> None:
        nonlocal login_gate_phrase_found
        if isinstance(value, str):
            for phrase in _VERIFIED_CLASSIFICATION_GATE_PHRASES:
                if phrase.lower() in value.lower():
                    login_gate_phrase_found = True
                    parts.append(f"observed_gate_phrase={phrase}")
            return
        if isinstance(value, list | tuple | set):
            for item in value:
                append_gate_phrase(item)

    for output in block_outputs.values():
        if not isinstance(output, dict):
            continue
        for key in (*_VERIFIED_CLASSIFICATION_CONTEXT_KEYS, *_VERIFIED_CLASSIFICATION_GATE_KEYS):
            value = _terminal_summary_scalar(output.get(key))
            if value is not None:
                parts.append(f"{key}={value}")
        for value in output.values():
            append_gate_phrase(value)
    if login_gate_phrase_found:
        parts.append("login-gated")
    return list(dict.fromkeys(parts))


def _verified_classification_summary(ctx: CopilotContext) -> str | None:
    if not verified_goal_claim_authorized(ctx) or not outcome_fully_verified(ctx):
        return None
    result = ctx.completion_verification_result
    if result is None or not result.is_fully_satisfied():
        return None
    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if policy is None:
        return None
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    parts: list[str] = []
    login_gated_confirmed = False
    for criterion in policy.completion_criteria:
        if criterion.kind != "validation_classification":
            continue
        verdict = verdict_by_id.get(criterion.id)
        if verdict is None or not verdict.satisfied:
            continue
        output_key = criterion.classification_output_key or verdict.output_path
        actual = _verified_output_value(ctx, output_key)
        display_value = _terminal_summary_scalar(actual)
        if output_key and display_value is not None:
            parts.append(f"{output_key}={display_value}")
        actual_text = actual.strip().lower() if isinstance(actual, str) else None
        if (
            isinstance(output_key, str)
            and output_key in {"login_only", "login_gated", "path_login_only"}
            and actual is True
        ) or actual_text in {"login-gated", "login_gated"}:
            login_gated_confirmed = True
    if login_gated_confirmed:
        parts.append("login-gated")
    parts.extend(_verified_adjacent_output_parts(ctx))
    if not parts:
        return None
    return "; ".join(dict.fromkeys(parts))


def _verified_workflow_success_reply(ctx: CopilotContext) -> str:
    summary = _verified_classification_summary(ctx)
    if summary is None:
        return _VERIFIED_WORKFLOW_SUCCESS_REPLY
    return f"{_VERIFIED_WORKFLOW_SUCCESS_REPLY} Verified result: {summary}."


@dataclass(frozen=True)
class _TypedRunOutcomeReply:
    user_response: str
    demonstrated: bool


def _safe_run_outcome_display_reason(recorded: RecordedRunOutcome) -> str | None:
    reason = run_outcome_display_reason(recorded.display_reason)
    if reason is None or contains_internal_machinery_leak(reason):
        return None
    try:
        assert_clean_user_facing_text(reason)
    except ValueError:
        return None
    return reason.rstrip(".")


def _render_typed_run_outcome_reply(
    ctx: CopilotContext,
    *,
    response_type: ResponseType,
    has_verified_workflow: bool,
    blocker_active: bool,
) -> _TypedRunOutcomeReply | None:
    if blocker_active or response_type != "REPLY":
        return None
    recorded = ctx.last_run_outcome
    if not isinstance(recorded, RecordedRunOutcome):
        return None
    if recorded.verdict == "demonstrated":
        if not has_verified_workflow or not verified_goal_claim_authorized(ctx):
            return None
        return _TypedRunOutcomeReply(
            user_response=f"{_verified_workflow_success_reply(ctx)} The latest run demonstrated the requested outcome.",
            demonstrated=True,
        )

    reason = _safe_run_outcome_display_reason(recorded)
    if recorded.verdict == "not_demonstrated":
        user_response = (
            "I built and ran the workflow, but the latest run did not demonstrate the requested outcome. "
            "Review the draft before using it."
        )
    else:
        user_response = (
            "I built and ran the workflow, but the latest run could not verify the requested outcome. "
            "Review the draft before using it."
        )
    if reason is not None:
        user_response = f"{user_response} Reason: {reason}."
    return _TypedRunOutcomeReply(user_response=user_response, demonstrated=False)


# Pre-validated safe string the finalization shim falls back to when the
# rendered blocker reply somehow trips OutputPolicy. Asserted clean at module
# load time so a future OutputPolicy regression doesn't silently land here.
_FALLBACK_BLOCKER_REPLY = "I couldn't complete that on this turn. Tell me what you'd like me to try next."


def _render_blocker_reply(
    signal: CopilotToolBlockerSignal, *, exit_site: str = "unspecified"
) -> tuple[str, ResponseType]:
    resp_type: ResponseType = "ASK_QUESTION" if signal.recovery_hint == "ask_user_clarifying" else "REPLY"
    user_response = signal.user_facing_reason
    try:
        assert_clean_user_facing_text(user_response, blocked_tool=signal.blocked_tool)
    except ValueError as exc:
        LOG.warning(
            "copilot blocker renderer template leaked; falling back",
            error=str(exc),
            exit_site=exit_site,
            **blocker_signal_to_trace_data(signal),
        )
        user_response = _FALLBACK_BLOCKER_REPLY
    return user_response, resp_type


# Log instead of assert so a regression on the fallback string still boots.
try:
    assert_clean_user_facing_text(_FALLBACK_BLOCKER_REPLY)
except ValueError as _fallback_validation_error:
    LOG.error(
        "copilot _FALLBACK_BLOCKER_REPLY tripped the leak deny list at module load",
        error=str(_fallback_validation_error),
    )


def _verified_terminal_preserve_result(
    ctx: CopilotContext, result: AgentResult, *, exit_site: str
) -> AgentResult | None:
    """When the judge confirmed the outcome, hold the tested proposal and the
    success reply instead of letting an involuntary blocker render over it."""
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    if verified_workflow is None:
        return None
    final_text, outcome = apply_repeated_reply_guard(
        final_text=_verified_workflow_success_reply(ctx),
        attempted_kind=derive_response_kind(ctx.turn_intent),
        blocked_signatures=ctx.blocked_reply_signatures,
        terminal_reason="verified_goal_satisfied",
        turn_intent=ctx.turn_intent,
    )
    output_kind = derive_output_kind(
        response_type="REPLY",
        request_policy=ctx.request_policy,
        updated_workflow=verified_workflow,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=True,
        unvalidated=False,
    )
    verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type="REPLY",
        user_response=final_text,
        global_llm_context=None,
        workflow_yaml=verified_yaml,
        has_workflow_proposal=True,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=True,
        unvalidated=False,
        output_kind=output_kind,
    )
    if not verdict.allowed:
        return None
    LOG.info(
        "copilot verified outcome preserved tested proposal over blocker",
        exit_site=exit_site,
        workflow_permanent_id=ctx.workflow_permanent_id,
    )
    return _make_agent_result(
        ctx,
        user_response=final_text,
        updated_workflow=verified_workflow,
        global_llm_context=result.global_llm_context,
        response_type="REPLY",
        workflow_yaml=verified_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        clear_proposed_workflow=False,
        total_tokens=result.total_tokens,
        cancelled=result.cancelled,
        proposal_disposition="review_tested",
        turn_outcome=outcome,
        turn_id=ctx.turn_id,
        narrative_summary=ctx.narrative_summary,
        narrative_payload=_build_narrative_payload(
            ctx,
            terminal="response",
            terminal_message=final_text,
            narrative_summary=ctx.narrative_summary,
        ),
    )


def _finalize_result_with_blocker_override(
    ctx: CopilotContext, result: AgentResult, *, exit_site: str = "unspecified"
) -> AgentResult:
    # Idempotent + safe to wrap every turn-end exit. OutputPolicy stays the
    # safety net: a hard-block verdict on the rendered text falls back to
    # `_FALLBACK_BLOCKER_REPLY`. `_build_output_policy_blocked_result` skips
    # the shim and enforces "blocker means no proposal" inline.
    local_signal = getattr(ctx, "blocker_signal", None)
    if not isinstance(local_signal, CopilotToolBlockerSignal):
        return result
    if not local_signal.renders_final_reply:
        return result
    if local_signal.internal_reason_code in _INVOLUNTARY_BLOCKER_REASON_CODES and outcome_fully_verified(ctx):
        preserved = _verified_terminal_preserve_result(ctx, result, exit_site=exit_site)
        if preserved is not None:
            return preserved

    rendered_reply, rendered_resp_type = _render_blocker_reply(local_signal, exit_site=exit_site)

    rendered_kind = (
        CopilotOutputKind.CLARIFICATION_REQUEST
        if rendered_resp_type == "ASK_QUESTION"
        else CopilotOutputKind.INFORMATIONAL_ANSWER
    )
    preserve_draft = local_signal.preserves_workflow_draft
    preserved_workflow = None
    preserved_workflow_yaml = None
    if preserve_draft:
        preserved_workflow = result.updated_workflow or result.staged_workflow or ctx.staged_workflow
        if preserved_workflow is not None:
            preserved_workflow_yaml = result.workflow_yaml or result.staged_workflow_yaml or ctx.staged_workflow_yaml
    preserved_proposal = preserve_draft and preserved_workflow is not None
    if preserved_proposal:
        rendered_reply = _ensure_unvalidated_proposal_affordance(rendered_reply)
    rendered_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type=rendered_resp_type,
        user_response=rendered_reply,
        global_llm_context=None,
        workflow_yaml=preserved_workflow_yaml,
        has_workflow_proposal=preserved_proposal,
        workflow_was_persisted=False,
        workflow_attempted=False,
        unvalidated=preserved_proposal,
        output_kind=rendered_kind,
    )
    raw_verdict = _copy_output_policy_verdict(rendered_verdict)
    final_verdict = rendered_verdict
    if not rendered_verdict.allowed:
        LOG.warning(
            "copilot blocker renderer output failed output policy; falling back",
            output_policy_reasons=[code.value for code in rendered_verdict.reason_codes],
            exit_site=exit_site,
            **blocker_signal_to_trace_data(local_signal),
        )
        rendered_reply = _FALLBACK_BLOCKER_REPLY
        final_verdict = evaluate_output_policy(
            request_policy=ctx.request_policy,
            response_type=rendered_resp_type,
            user_response=rendered_reply,
            global_llm_context=None,
            workflow_yaml=None,
            has_workflow_proposal=False,
            workflow_was_persisted=False,
            workflow_attempted=False,
            unvalidated=False,
            output_kind=rendered_kind,
        )
        if not final_verdict.allowed:
            LOG.error(
                "copilot blocker fallback reply failed output policy; suppressing proposal",
                fallback_reasons=[code.value for code in final_verdict.reason_codes],
                exit_site=exit_site,
                **blocker_signal_to_trace_data(local_signal),
            )
            preserve_draft = False
            preserved_workflow = None
            preserved_workflow_yaml = None
            preserved_proposal = False

    # ResponseKind has no "REPLY" member; CLARIFY matches the convention other
    # turn-end exits (timeout, max-turns, cancel, non-retriable-nav) use.
    final_text, turn_outcome = apply_repeated_reply_guard(
        final_text=rendered_reply,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=list(ctx.blocked_reply_signatures),
        reason_code=local_signal.internal_reason_code or "copilot_blocker_renderer",
    )

    LOG.info(
        "copilot blocker renderer finalization shim fired",
        exit_site=exit_site,
        **blocker_signal_to_trace_data(local_signal),
    )
    rendered_diagnostics = build_output_policy_diagnostics(
        raw_verdict=raw_verdict,
        final_verdict=final_verdict,
        final_output_kind=rendered_kind,
        hard_block_reason_codes=list(raw_verdict.reason_codes),
        soft_rewrite_reason_codes=[],
    )
    # A blocker turn is never auto-applicable; even a preserved draft is surfaced as review_untested.
    return _make_agent_result(
        ctx,
        user_response=final_text,
        updated_workflow=preserved_workflow if preserve_draft else None,
        global_llm_context=result.global_llm_context,
        response_type=rendered_resp_type,
        workflow_yaml=preserved_workflow_yaml if preserve_draft else None,
        workflow_was_persisted=result.workflow_was_persisted,
        clear_proposed_workflow=not preserve_draft,
        total_tokens=result.total_tokens,
        cancelled=result.cancelled,
        proposal_disposition="review_untested" if preserved_proposal else "no_proposal",
        output_policy_diagnostics=rendered_diagnostics,
        turn_outcome=turn_outcome,
        turn_id=ctx.turn_id,
        narrative_summary=ctx.narrative_summary,
        narrative_payload=_build_narrative_payload(
            ctx,
            terminal="response",
            terminal_message=final_text,
            narrative_summary=ctx.narrative_summary,
        ),
    )


def _workflow_block_count(ctx: CopilotContext) -> int | None:
    count = getattr(ctx, "last_update_block_count", None)
    if isinstance(count, int) and count > 0:
        return count
    workflow = getattr(ctx, "last_workflow", None)
    definition = getattr(workflow, "workflow_definition", None)
    blocks = getattr(definition, "blocks", None)
    return len(blocks) if isinstance(blocks, list) and blocks else None


def _observed_page_sentence(ctx: CopilotContext) -> str:
    evidence = getattr(ctx, "workflow_verification_evidence", None)
    url = getattr(evidence, "current_url", None)
    if not isinstance(url, str) or not url.strip():
        return ""
    sentence = f" The last page I observed was {url.strip()[:140]}."
    return "" if contains_internal_machinery_leak(sentence) else sentence


def _observed_facts_halt_reply(ctx: CopilotContext) -> str:
    block_count = _workflow_block_count(ctx)
    block_phrase = f"a {block_count}-block draft" if block_count else "a draft"
    observed = _observed_page_sentence(ctx)
    if getattr(ctx, "last_workflow", None) is not None:
        return (
            f"I built {block_phrase} and was still testing it when the turn ran out of time."
            f"{observed} I haven't verified the results, so I'm not claiming them."
        )
    return (
        f"The turn ran out of time before I could finish.{observed}"
        " I haven't verified any results, so I'm not claiming them."
    )


def _halted_mid_progress(ctx: CopilotContext, internal_tool_instruction_failure: bool) -> bool:
    if internal_tool_instruction_failure:
        return True
    return getattr(ctx, "last_failure_category_top", None) == PER_TOOL_BUDGET_FAILURE_CATEGORY


def _clean_recorded_failure_text(value: Any, max_chars: int = 240) -> str:
    # Caller-owned sentence templates add punctuation around these fragments.
    text = clean_recorded_failure_text(value, max_chars=max_chars).rstrip(".")
    if not text:
        return ""
    verdict = evaluate_output_policy(
        request_policy=None,
        response_type="REPLY",
        user_response=text,
        output_kind=CopilotOutputKind.INFORMATIONAL_ANSWER,
    )
    if OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes:
        return "The previous workflow run did not finish before the turn budget expired"
    return text


def _recorded_failure_summary(ctx: CopilotContext) -> tuple[str, str]:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    verification = getattr(contract, "verification_result", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    remaining_blocker = _clean_recorded_failure_text(getattr(verification, "remaining_blocker", None))
    root_cause = _clean_recorded_failure_text(getattr(diagnosis, "root_cause_summary", None))
    fallback_reason = _clean_recorded_failure_text(getattr(ctx, "last_test_failure_reason", None))
    reason = remaining_blocker or root_cause or fallback_reason
    run_status = _clean_recorded_failure_text(getattr(verification, "run_status", None), max_chars=80)
    status_sentence = f" Last run status: {run_status}." if run_status else ""
    return reason, status_sentence


def _recorded_failure_is_internal_tool_instruction(ctx: CopilotContext) -> bool:
    contract = ctx.latest_diagnosis_repair_contract
    if contract is None:
        candidates: tuple[object, ...] = (ctx.last_test_failure_reason,)
    else:
        candidates = (
            contract.verification_result.remaining_blocker,
            contract.diagnosis_result.root_cause_summary,
            ctx.last_test_failure_reason,
        )
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        # Evaluate the redacted form at the same truncation the reply embeds:
        # standard redaction already neutralizes browser-session references,
        # so flag only what would still leak.
        verdict = evaluate_output_policy(
            request_policy=None,
            response_type="REPLY",
            user_response=clean_recorded_failure_text(value, max_chars=240),
            output_kind=CopilotOutputKind.INFORMATIONAL_ANSWER,
        )
        if OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes:
            return True
    return False


def _specific_missing_context_question(value: Any) -> str:
    question = _clean_recorded_failure_text(value, max_chars=320)
    if not question:
        return ""
    lowered = question.lower()
    if any(phrase in lowered for phrase in _GENERIC_MISSING_CONTEXT_PHRASES):
        return ""
    if question[-1] not in ".?!":
        question += "."
    return question


def _join_human_list(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _required_context_label(value: Any) -> str:
    if isinstance(value, RequiredContextKey):
        return _REQUIRED_CONTEXT_LABELS[value]
    if isinstance(value, str):
        if value in _DIAGNOSIS_MISSING_CONTEXT_LABELS:
            return _DIAGNOSIS_MISSING_CONTEXT_LABELS[value]
        try:
            return _REQUIRED_CONTEXT_LABELS[RequiredContextKey(value)]
        except ValueError:
            return _clean_recorded_failure_text(value, max_chars=120)
    return ""


def _turn_context_missing_context_labels(ctx: CopilotContext) -> list[str]:
    packet = getattr(ctx, "turn_context_packet", None)
    omissions = getattr(packet, "omissions", None)
    if not isinstance(omissions, list):
        return []
    labels: list[str] = []
    for omission in omissions:
        label = _required_context_label(getattr(omission, "context_key", None))
        if label:
            labels.append(label)
    return list(dict.fromkeys(labels))


def _diagnosis_missing_context_labels(ctx: CopilotContext) -> list[str]:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    missing_context = getattr(diagnosis, "missing_context", None)
    if not isinstance(missing_context, list):
        return []
    labels = [_required_context_label(item) for item in missing_context]
    return list(dict.fromkeys(label for label in labels if label))


def _unbacked_workflow_delivery_reply(ctx: CopilotContext) -> str:
    turn_intent = getattr(ctx, "turn_intent", None)
    if isinstance(turn_intent, TurnIntent):
        question = _specific_missing_context_question(turn_intent.missing_context_question)
        if question:
            return f"{_UNBACKED_WORKFLOW_DELIVERY_PREFIX} I need this before I can build and test it: {question}"

    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if request_policy is not None:
        question = _specific_missing_context_question(request_policy.clarification_question)
        if question:
            return f"{_UNBACKED_WORKFLOW_DELIVERY_PREFIX} I need this before I can build and test it: {question}"

    missing_context = _diagnosis_missing_context_labels(ctx) or _turn_context_missing_context_labels(ctx)
    if missing_context:
        items = _join_human_list(missing_context)
        return f"{_UNBACKED_WORKFLOW_DELIVERY_PREFIX} Required context was unavailable: {items}."

    reason, status_sentence = _recorded_failure_summary(ctx)
    if reason:
        return f"{_UNBACKED_WORKFLOW_DELIVERY_PREFIX} The recorded blocker was: {reason}.{status_sentence}"

    return _UNBACKED_WORKFLOW_DELIVERY_REPLY


def _last_good_failure_reply(ctx: CopilotContext, tested_reply: str) -> str:
    reason, status_sentence = _recorded_failure_summary(ctx)
    if not reason:
        return tested_reply
    return f"{tested_reply} The latest attempted change did not verify: {reason}.{status_sentence}"


def _recorded_failure_reply(
    ctx: CopilotContext, *, cancelled: bool = False, internal_tool_instruction_failure: bool | None = None
) -> str | None:
    if cancelled or ctx.last_test_ok is True:
        return None

    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    verification = getattr(contract, "verification_result", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    repair_decision = getattr(contract, "repair_decision", None)
    diagnosis_input = getattr(contract, "diagnosis_input", None)
    failure_type = getattr(getattr(diagnosis, "suspected_failure_type", None), "value", None) or getattr(
        diagnosis,
        "suspected_failure_type",
        None,
    )
    next_action = getattr(getattr(repair_decision, "next_action", None), "value", None) or getattr(
        repair_decision,
        "next_action",
        None,
    )
    reason, status_sentence = _recorded_failure_summary(ctx)
    if not reason:
        return None
    if internal_tool_instruction_failure is None:
        internal_tool_instruction_failure = _recorded_failure_is_internal_tool_instruction(ctx)
    # A guard-halted or budget-paced run was interrupted, not disproven; render
    # observed facts instead of a failure verdict built from internal text.
    if _halted_mid_progress(ctx, internal_tool_instruction_failure):
        return _observed_facts_halt_reply(ctx)

    run_status = _clean_recorded_failure_text(getattr(verification, "run_status", None), max_chars=80).lower()
    block_count = _workflow_block_count(ctx)
    block_phrase = f"a {block_count}-block draft" if block_count else "a draft"
    test_attempted = bool(
        getattr(ctx, "test_after_update_done", False)
        or getattr(ctx, "last_test_ok", None) is not None
        or getattr(diagnosis_input, "workflow_run_id", None)
    )
    test_failed = ctx.last_test_ok is False or run_status == "failed"
    unrecoverable_stop = next_action == "stop" or failure_type == "unrecoverable_tool_error"

    if getattr(ctx, "last_workflow", None) is not None:
        if test_attempted and test_failed and not unrecoverable_stop:
            return f"I built {block_phrase} and tested it, but the test failed: {reason}.{status_sentence}"
        if test_attempted:
            return f"I built {block_phrase} and tested it, but the test couldn't finish: {reason}.{status_sentence}"
        return f"I built {block_phrase}, but I couldn't verify it: {reason}.{status_sentence}"
    return f"I couldn't finish the Copilot turn: {reason}.{status_sentence}"


def _ensure_unvalidated_proposal_affordance(user_response: str) -> str:
    lower = user_response.lower()
    has_ui_affordance = bool(
        _PROPOSAL_ACCEPT_UI_ACTION_RE.search(user_response) and _PROPOSAL_REJECT_UI_ACTION_RE.search(user_response)
    )
    has_unvalidated_disclosure = any(phrase in lower for phrase in UNVALIDATED_DISCLOSURE_PHRASES)
    if has_ui_affordance and has_unvalidated_disclosure:
        return user_response
    if user_response.strip():
        return f"{user_response}\n\n{_UNVALIDATED_PROPOSAL_AFFORDANCE}"
    return _UNVALIDATED_PROPOSAL_AFFORDANCE


def _build_wip_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    *,
    default_reply: str,
    unvalidated_reply: str,
    tested_reply: str,
    cancelled: bool = False,
    terminal_reason: str | None = None,
) -> AgentResult:
    """Selected non-success exits surface the most recent successfully parsed workflow."""
    internal_tool_instruction_failure = _recorded_failure_is_internal_tool_instruction(ctx)
    halted_mid_progress = _halted_mid_progress(ctx, internal_tool_instruction_failure)
    recorded_failure_reply = _recorded_failure_reply(
        ctx, cancelled=cancelled, internal_tool_instruction_failure=internal_tool_instruction_failure
    )
    effective_terminal = terminal_reason or ("cancel" if cancelled else None)

    def _guard(text: str) -> tuple[str, TurnOutcome]:
        if contains_internal_machinery_leak(text):
            LOG.warning(
                "copilot terminal output invariant replaced leaked text",
                terminal_reason=effective_terminal,
            )
            text = _observed_facts_halt_reply(ctx)
        return apply_repeated_reply_guard(
            final_text=text,
            attempted_kind=ResponseKind.CLARIFY,
            blocked_signatures=ctx.blocked_reply_signatures,
            terminal_reason=effective_terminal,
        )

    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    if outcome_fully_verified(ctx) and verified_workflow is not None:
        final_text, outcome = _guard(tested_reply)
        proposal_disposition = "auto_applicable" if verified_goal_claim_authorized(ctx) else "review_tested"
        return _finalize_result_with_blocker_override(
            ctx,
            _make_agent_result(
                ctx,
                user_response=final_text,
                updated_workflow=verified_workflow,
                global_llm_context=global_llm_context,
                workflow_yaml=verified_yaml,
                workflow_was_persisted=ctx.workflow_persisted,
                has_staged_proposal=ctx.has_staged_proposal,
                staged_workflow_yaml=ctx.staged_workflow_yaml,
                staged_workflow=ctx.staged_workflow,
                canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
                total_tokens=ctx.total_tokens_used,
                proposal_disposition=proposal_disposition,
                cancelled=cancelled,
                turn_outcome=outcome,
                turn_id=ctx.turn_id,
                narrative_summary=ctx.narrative_summary,
                narrative_payload=_build_narrative_payload(
                    ctx,
                    terminal="response",
                    terminal_message=final_text,
                    narrative_summary=ctx.narrative_summary,
                ),
            ),
            exit_site="wip_verified_terminal_proposal",
        )

    # When an unverified edit/run has overwritten ``last_workflow``, prefer the
    # verified shape while still forcing explicit review.
    if (
        ctx.last_good_workflow is not None
        and ctx.last_good_workflow_yaml
        and ctx.last_workflow is not ctx.last_good_workflow
        and not ctx.last_test_suspicious_success
    ):
        reply = _last_good_failure_reply(ctx, tested_reply) if recorded_failure_reply else tested_reply
        final_text, outcome = _guard(reply)
        return _finalize_result_with_blocker_override(
            ctx,
            _make_agent_result(
                ctx,
                user_response=final_text,
                updated_workflow=ctx.last_good_workflow,
                global_llm_context=global_llm_context,
                workflow_yaml=ctx.last_good_workflow_yaml,
                workflow_was_persisted=ctx.workflow_persisted,
                has_staged_proposal=ctx.has_staged_proposal,
                staged_workflow_yaml=ctx.staged_workflow_yaml,
                staged_workflow=ctx.staged_workflow,
                canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
                total_tokens=ctx.total_tokens_used,
                proposal_disposition="review_tested",
                cancelled=cancelled,
                turn_outcome=outcome,
                turn_id=ctx.turn_id,
                narrative_summary=ctx.narrative_summary,
                narrative_payload=_build_narrative_payload(
                    ctx,
                    terminal="error",
                    terminal_message=final_text,
                    narrative_summary=ctx.narrative_summary,
                ),
            ),
            exit_site="wip_last_good_workflow",
        )
    if (
        ctx.last_workflow is not None
        and ctx.last_workflow_yaml
        and (ctx.last_test_ok is not False or halted_mid_progress)
        and not ctx.last_test_suspicious_success
    ):
        full_test_ok = ctx.last_test_ok is True and ctx.last_full_workflow_test_ok is True
        unvalidated = not full_test_ok
        delivered_reply = _delivered_unverified_reply(ctx)
        if delivered_reply is not None:
            reply = delivered_reply
            unvalidated = True
        elif unvalidated and recorded_failure_reply:
            reply = recorded_failure_reply
            if halted_mid_progress:
                reply = _ensure_unvalidated_proposal_affordance(reply)
        else:
            reply = unvalidated_reply if unvalidated else tested_reply
        final_text, outcome = _guard(reply)
        proposal_disposition = "review_untested" if unvalidated else "review_tested"
        if not unvalidated and verified_goal_claim_authorized(ctx):
            proposal_disposition = "auto_applicable"
        return _finalize_result_with_blocker_override(
            ctx,
            _make_agent_result(
                ctx,
                user_response=final_text,
                updated_workflow=ctx.last_workflow,
                global_llm_context=global_llm_context,
                workflow_yaml=ctx.last_workflow_yaml,
                workflow_was_persisted=ctx.workflow_persisted,
                has_staged_proposal=ctx.has_staged_proposal,
                staged_workflow_yaml=ctx.staged_workflow_yaml,
                staged_workflow=ctx.staged_workflow,
                canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
                total_tokens=ctx.total_tokens_used,
                proposal_disposition=proposal_disposition,
                cancelled=cancelled,
                turn_outcome=outcome,
                turn_id=ctx.turn_id,
                narrative_summary=ctx.narrative_summary,
                narrative_payload=_build_narrative_payload(
                    ctx,
                    terminal="error",
                    terminal_message=final_text,
                    narrative_summary=ctx.narrative_summary,
                ),
            ),
            exit_site="wip_last_workflow",
        )
    return _build_exit_result(
        ctx,
        recorded_failure_reply or default_reply,
        global_llm_context,
        cancelled=cancelled,
        terminal_reason=effective_terminal,
    )


def _merge_exit_context(
    global_llm_context: str | None,
    *,
    failure: RecoverableFailure | None = None,
) -> str | None:
    if failure is None:
        return global_llm_context
    return merge_failure_into_context(global_llm_context, failure)


def _build_timeout_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_TIMEOUT_REPLY_DEFAULT,
        unvalidated_reply=_TIMEOUT_REPLY_UNVALIDATED,
        tested_reply=_TIMEOUT_REPLY_TESTED,
        terminal_reason="timeout",
    )


def _build_cancelled_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    if ctx.copilot_total_timeout_exceeded:
        LOG.info("Copilot cancellation resolved as total timeout")
        return _build_timeout_exit_result(ctx, global_llm_context)
    return _build_cancel_exit_result(ctx, global_llm_context)


def _build_max_turns_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_MAX_TURNS_REPLY_DEFAULT,
        unvalidated_reply=_MAX_TURNS_REPLY_UNVALIDATED,
        tested_reply=_MAX_TURNS_REPLY_TESTED,
        terminal_reason="max_turns",
    )


def _build_unexpected_error_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    error: BaseException | None = None,
    *,
    span: Any | None = None,
) -> AgentResult:
    failure = build_recoverable_failure(
        error,
        workflow_modified=ctx.workflow_persisted,
    )
    default_reply = format_recoverable_failure_reply(failure)
    enriched_context = _merge_exit_context(global_llm_context, failure=failure)
    result = _build_wip_exit_result(
        ctx,
        enriched_context,
        default_reply=default_reply,
        unvalidated_reply=_UNEXPECTED_ERROR_REPLY_UNVALIDATED,
        tested_reply=_UNEXPECTED_ERROR_REPLY_TESTED,
        terminal_reason="unexpected_error",
    )
    LOG.warning(
        "Copilot unexpected error translated to recoverable reply",
        failure_kind=failure.failure_kind,
        internal_error_id=failure.internal_error_id,
        exception_type=failure.exception_type,
        error_type=type(error).__name__ if error else None,
        workflow_permanent_id=getattr(ctx, "workflow_permanent_id", None),
        workflow_copilot_chat_id=getattr(ctx, "workflow_copilot_chat_id", None),
        workflow_modified=failure.workflow_modified,
        has_proposal=result.updated_workflow is not None,
        proposal_disposition=result.proposal_disposition,
        last_test_ok=getattr(ctx, "last_test_ok", None),
    )
    current_span = span or otel_trace.get_current_span()
    current_span.set_attribute("copilot.error_recovered", True)
    current_span.set_attribute("copilot.error_failure_kind", failure.failure_kind)
    current_span.set_attribute("copilot.error_id", failure.internal_error_id)
    if failure.exception_type:
        current_span.set_attribute("copilot.error_exception_type", failure.exception_type)
    current_span.set_attribute("copilot.error_reply_proposal_disposition", result.proposal_disposition)
    current_span.set_attribute("copilot.error_workflow_modified", failure.workflow_modified)
    return result


def _build_cancel_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_CANCEL_REPLY_DEFAULT,
        unvalidated_reply=_CANCEL_REPLY_UNVALIDATED,
        tested_reply=_CANCEL_REPLY_TESTED,
        cancelled=True,
    )


async def _resolve_wrapped_exception_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    *,
    goal_satisfied: bool,
    error: BaseException,
    workflow_permanent_id: str | None,
) -> AgentResult:
    error_type = type(error).__name__
    try:
        raise_if_turn_halt(ctx, verified=outcome_fully_verified(ctx))
    except CopilotTurnHalt as halt_exc:
        LOG.info(
            "Copilot run stopped after typed turn halt from wrapped exception",
            workflow_permanent_id=workflow_permanent_id,
            error_type=error_type,
            **turn_halt_to_trace_data(halt_exc.halt),
        )
        return _build_turn_halt_exit_result(ctx, global_llm_context, halt_exc.halt)
    turn_halt = ctx.turn_halt
    if isinstance(turn_halt, TurnHalt):
        LOG.info(
            "Copilot run stopped after typed turn halt from wrapped exception",
            workflow_permanent_id=workflow_permanent_id,
            error_type=error_type,
            **turn_halt_to_trace_data(turn_halt),
        )
        return _build_turn_halt_exit_result(ctx, global_llm_context, turn_halt)
    if goal_satisfied:
        # The Agents SDK can wrap exceptions raised from hooks; keep this
        # fallback so a verified-goal stop is not rendered as a generic error.
        LOG.info(
            "Copilot run stopped after verified goal satisfaction from wrapped exception",
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=ctx.last_successful_run_blocks_workflow_run_id,
            error_type=error_type,
        )
        return await _build_goal_satisfied_exit_result(ctx, global_llm_context)
    LOG.error("Copilot agent error", error=str(error), exc_info=True)
    return _build_unexpected_error_exit_result(ctx, global_llm_context, error=error)


async def _translate_to_agent_result(
    result: RunResultStreaming,
    ctx: CopilotContext,
    global_llm_context: str | None,
    chat_request: WorkflowCopilotChatRequest,
    organization_id: str,
) -> AgentResult:
    # Deferred tools.py imports here and below: tools.py -> routes.workflow_copilot -> this module (circular at import time).
    from skyvern.forge.sdk.copilot.tools import _process_workflow_yaml

    text = extract_final_text(result)
    if not text:
        text = '{"type": "REPLY", "user_response": "I\'m not sure how to help with that. Could you rephrase?"}'

    action_data = parse_final_response(text)
    user_response = action_data.get("user_response") or "Done."

    resp_type = action_data.get("type", "REPLY")
    if resp_type not in COPILOT_RESPONSE_TYPES:
        resp_type = "REPLY"
    normalized_scaffolding = normalize_response_scaffolding(resp_type, str(user_response))
    resp_type = normalized_scaffolding.response_type
    user_response = normalized_scaffolding.user_response or "Done."

    # Bind the signal to a local so the proposal-cascade gating below can't
    # desync from the inline override if ctx mutates mid-translate.
    local_blocker_signal = ctx.blocker_signal if isinstance(ctx.blocker_signal, CopilotToolBlockerSignal) else None
    render_blocker_reply = local_blocker_signal is not None and local_blocker_signal.renders_final_reply
    blocker_active = render_blocker_reply
    if local_blocker_signal is not None and render_blocker_reply:
        # Override only user-visible text + resp_type so REPLACE_WORKFLOW and ASK_QUESTION gating skip the model's side-effect path; the shim is the sole renderer.
        rendered_reply, rendered_resp_type = _render_blocker_reply(local_blocker_signal)
        user_response = rendered_reply
        resp_type = rendered_resp_type
        LOG.info(
            "copilot blocker renderer inline override",
            **blocker_signal_to_trace_data(local_blocker_signal),
        )

    last_workflow = ctx.last_workflow
    last_workflow_yaml = ctx.last_workflow_yaml

    def _with_inline_reject_note(response: Any, detail: str) -> str:
        note = detail if not contains_internal_machinery_leak(detail) else _INLINE_REJECT_NOTE_FALLBACK
        return f"{response}\n\n(Note: {note})"

    turn_intent = ctx.turn_intent if isinstance(ctx.turn_intent, TurnIntent) else None
    may_update_workflow = turn_intent is None or turn_intent.authority.may_update_workflow
    if resp_type == "REPLACE_WORKFLOW" and turn_intent is not None and not turn_intent.authority.may_update_workflow:
        # A no-mutation turn (e.g. DIAGNOSE) must not stage a draft even via an inline REPLACE_WORKFLOW, which
        # bypasses the may_update_workflow=False authority enforced on the update_workflow tool. Downgrade to a
        # REPLY so the diagnosis still lands but no edit is staged; the user applies it on a follow-up edit turn.
        LOG.info(
            "copilot suppressed inline REPLACE_WORKFLOW on no-mutation turn",
            turn_intent_mode=turn_intent.mode.value,
        )
        user_response = _with_inline_reject_note(
            user_response,
            "Diagnosing a failed run doesn't edit the workflow on its own — confirm and I'll apply the change.",
        )
        resp_type = "REPLY"

    if resp_type == "REPLACE_WORKFLOW":
        LOG.warning("Agent used inline REPLACE_WORKFLOW instead of update_workflow tool")
        workflow_yaml = action_data.get("workflow_yaml", "")
        if workflow_yaml:
            inline_raw_verdict = evaluate_output_policy(
                request_policy=ctx.request_policy,
                response_type=resp_type,
                user_response=str(user_response),
                workflow_yaml=workflow_yaml,
                tool_arguments=action_data,
                has_workflow_proposal=True,
                output_kind=CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL,
            )
            inline_policy_verdict = hard_block_output_policy_verdict(inline_raw_verdict)
            if not inline_policy_verdict.allowed:
                inline_diagnostics = build_output_policy_diagnostics(
                    raw_verdict=inline_raw_verdict,
                    final_verdict=inline_policy_verdict,
                    final_output_kind=_blocked_final_output_kind(inline_policy_verdict),
                    hard_block_reason_codes=list(inline_policy_verdict.reason_codes),
                    soft_rewrite_reason_codes=[],
                )
                return _build_output_policy_blocked_result(
                    ctx,
                    inline_policy_verdict,
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                    output_policy_diagnostics=inline_diagnostics,
                )
            # REPLACE_WORKFLOW bypasses the update_workflow tool guardrail, so
            # policy and post-emission rejects run here before YAML processing.
            # The final-output policy pass still runs below; leave last_workflow
            # / last_workflow_yaml unchanged until this candidate survives the
            # inline checks.
            from skyvern.forge.sdk.copilot.tools import (
                _banned_block_reject_message,
                _detect_new_banned_blocks,
                _detect_stale_block_metadata,
                _record_banned_block_reject_span,
                _stale_block_metadata_message,
                _timing_only_challenge_wait_reject_message,
                composition_page_evidence_error,
                workflow_target_url,
            )
            from skyvern.forge.sdk.copilot.tools.banned_blocks import _copilot_banned_block_types

            wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
            if wait_block_error:
                user_response = _with_inline_reject_note(user_response, wait_block_error)
                ctx.last_test_ok = None
                workflow_yaml = ""
            banned_items = _detect_new_banned_blocks(
                workflow_yaml,
                ctx.last_workflow_yaml,
                banned_types=_copilot_banned_block_types(ctx),
            )
            if banned_items:
                _record_banned_block_reject_span("replace_workflow_inline", banned_items)
                user_response = _with_inline_reject_note(user_response, _banned_block_reject_message(banned_items, ctx))
                workflow_yaml = ""
            stale_metadata = _detect_stale_block_metadata(workflow_yaml, ctx.last_workflow_yaml or ctx.workflow_yaml)
            if stale_metadata:
                user_response = _with_inline_reject_note(user_response, _stale_block_metadata_message(stale_metadata))
                ctx.last_test_ok = None
                workflow_yaml = ""
            composition_evidence_error = composition_page_evidence_error(ctx, workflow_yaml)
            if composition_evidence_error:
                LOG.info(
                    "copilot inline composition page evidence rejected workflow",
                    workflow_permanent_id=ctx.workflow_permanent_id,
                    target_url=workflow_target_url(workflow_yaml),
                )
                user_response = _with_inline_reject_note(user_response, composition_evidence_error)
                ctx.last_test_ok = None
                workflow_yaml = ""
        if workflow_yaml:
            # Inline REPLACE_WORKFLOW bypasses the update_workflow tool, so apply the same default here.
            workflow_yaml = default_data_write_continue_on_failure(
                workflow_yaml, ctx.last_workflow_yaml or ctx.workflow_yaml
            )
            try:
                last_workflow = await _process_workflow_yaml(
                    workflow_id=chat_request.workflow_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    organization_id=organization_id,
                    workflow_yaml=workflow_yaml,
                    settings_fallback_yaml=ctx.last_workflow_yaml or ctx.workflow_yaml,
                )
                last_workflow_yaml = workflow_yaml
            except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
                LOG.warning("Failed to process final workflow YAML", error=str(e))
                user_response = (
                    f"{user_response}\n\n"
                    f"(Note: The proposed workflow had a validation error: {str(e)[:200]}. "
                    f"Please ask me to fix it.)"
                )

    # Inline REPLACE_WORKFLOW bypasses _update_workflow, so ctx.last_workflow
    # is whatever the tool layer last saw. Write the REPLACE candidate onto
    # ctx and invalidate any prior passing test: the REPLACE yaml itself was
    # never run, so a leftover ``last_test_ok is True`` from an earlier tested
    # (but different) yaml must not promote this untested one.
    # ``blocker_active`` should already have rewritten resp_type away from
    # REPLACE_WORKFLOW above, and a no-mutation turn was downgraded to REPLY;
    # the explicit guards here defend against future refactors that re-emit
    # REPLACE_WORKFLOW post-rendering or bypass the downgrade.
    if (
        resp_type == "REPLACE_WORKFLOW"
        and last_workflow is not ctx.last_workflow
        and not blocker_active
        and may_update_workflow
    ):
        ctx.last_workflow = last_workflow
        ctx.last_workflow_yaml = last_workflow_yaml
        ctx.last_test_ok = None
        clear_terminal_evidence_on_workflow_edit(ctx)
        # Inline REPLACE_WORKFLOW is untested by construction; emit a draft
        # envelope without staging onto ctx so terminal auto-accept can't fire,
        # and suppress the workflow payload so the canvas does not render it.
        if last_workflow is not None and ctx.stream is not None:
            try:
                await maybe_emit_design_end(ctx.stream, ctx)
                await emit_workflow_draft(ctx.stream, ctx, last_workflow, include_workflow=False)
            except Exception as emit_err:
                LOG.warning("copilot_narrative_inline_replace_emit_failed", error=str(emit_err))
            ctx.design_start_emitted = False
            ctx.design_end_emitted = False

    # An unverified edit/run sits in ``last_workflow`` after a recorded
    # failure — surface the verified prior shape and skip the failure rewrite
    # (which would describe the failed-shape block count).
    salvaged_reply = (
        resp_type == "REPLY"
        and ctx.last_good_workflow is not None
        and ctx.last_good_workflow_yaml
        and ctx.last_workflow is not ctx.last_good_workflow
        and bool(ctx.last_failed_workflow_yaml or ctx.last_test_ok is False)
        and not ctx.last_test_suspicious_success
        and not blocker_active
    )

    # ASK_QUESTION replies carry a specific clarifying question — often the
    # "stop and ask" unblocker the system prompt now requires when the agent
    # cannot test. The generic rewrite would replace it with a vague
    # "Could you share more context", so skip it for ASK_QUESTION (and for
    # salvaged replies, which already describe the verified prefix).
    if _should_surface_untested_draft_despite_question(ctx, resp_type) and not blocker_active:
        LOG.info(
            "Converting copilot clarification into untested draft proposal",
            workflow_permanent_id=ctx.workflow_permanent_id,
            block_count=ctx.last_update_block_count,
        )
        resp_type = "REPLY"

    # ``blocker_active`` short-circuits the salvage/failure rewrites — the
    # renderer owns the final reply, so reshaping the agent's prose first
    # would be wasted work the finalization shim discards.
    if not blocker_active:
        if resp_type == "ASK_QUESTION":
            user_response = _shape_ask_question_response(str(user_response), ctx)
        elif not salvaged_reply:
            user_response = _rewrite_failed_test_response(str(user_response), ctx)
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    verified_terminal_ready = (
        verified_workflow is not None and verified_goal_claim_authorized(ctx) and not blocker_active
    )
    if verified_terminal_ready:
        resp_type = "REPLY"
        user_response = _verified_workflow_success_reply(ctx)
        agent_admits_incomplete = False
    else:
        # Default-true preserves backwards-compat with stale prompts and missing fields.
        agent_admits_incomplete = _is_explicit_false(
            action_data.get("goal_reached")
        ) and not verified_goal_claim_authorized(ctx)
    typed_outcome_reply = _render_typed_run_outcome_reply(
        ctx,
        response_type=resp_type,
        has_verified_workflow=verified_workflow is not None,
        blocker_active=blocker_active,
    )
    if typed_outcome_reply is not None:
        user_response = typed_outcome_reply.user_response
        agent_admits_incomplete = not typed_outcome_reply.demonstrated

    last_workflow = None
    last_workflow_yaml = None
    unvalidated = False
    if verified_workflow is not None and not agent_admits_incomplete and not blocker_active:
        last_workflow, last_workflow_yaml = verified_workflow, verified_yaml
    elif salvaged_reply:
        last_workflow, last_workflow_yaml = ctx.last_good_workflow, ctx.last_good_workflow_yaml
        unvalidated = True
    elif resp_type == "REPLY" and ctx.last_workflow is not None and ctx.last_workflow_yaml and not blocker_active:
        # Failures are often environmental (captcha, transient block); surface the draft so the user can keep iterating.
        last_workflow = ctx.last_workflow
        last_workflow_yaml = ctx.last_workflow_yaml
        unvalidated = True

    # ASK_QUESTION blocks on user input — never surface a verified workflow
    # under it; auto_accept would silently apply a stepping-stone partial.
    if resp_type == "ASK_QUESTION":
        last_workflow = None
        last_workflow_yaml = None

    llm_context_raw = action_data.get("global_llm_context")
    structured = StructuredContext.from_json_str(global_llm_context)
    if isinstance(llm_context_raw, dict):
        try:
            structured = StructuredContext.model_validate(llm_context_raw)
        except Exception:
            pass
    elif isinstance(llm_context_raw, str):
        structured = StructuredContext.from_json_str(llm_context_raw)
    structured.merge_turn_summary(ctx.tool_activity)
    enriched_context = structured.to_json_str()
    workflow_attempted = ctx.has_genuine_workflow_attempt()
    _log_output_policy_parity(
        ctx, has_workflow_proposal=last_workflow is not None, workflow_attempted=workflow_attempted
    )
    if _should_use_built_unverified_completed_reply(
        ctx,
        response_type=resp_type,
        updated_workflow=last_workflow,
        validated=not unvalidated,
        blocker_active=blocker_active,
    ):
        user_response = _BUILT_UNVERIFIED_COMPLETED_REPLY
    output_kind = derive_output_kind(
        response_type=resp_type,
        request_policy=ctx.request_policy,
        updated_workflow=last_workflow,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=unvalidated,
    )

    raw_output_policy_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type=resp_type,
        user_response=str(user_response),
        global_llm_context=enriched_context,
        workflow_yaml=last_workflow_yaml,
        has_workflow_proposal=last_workflow is not None,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=unvalidated,
        output_kind=output_kind,
    )
    actuation_obligation = _evaluate_actuation_obligation_for_output(ctx, action_data, resp_type, output_kind)
    if actuation_obligation.status == ActuationObligationStatus.STEER:
        raw_output_policy_verdict.add(OutputPolicyReason.ACTUATION_OBLIGATION_STEER)
    actuation_obligation_terminal = actuation_obligation.status == ActuationObligationStatus.TERMINAL
    if actuation_obligation_terminal:
        resp_type = "REPLY"
        user_response = _ACTUATION_OBLIGATION_UNMET_REPLY
        last_workflow = None
        last_workflow_yaml = None
    output_policy_verdict = _copy_output_policy_verdict(raw_output_policy_verdict)
    soft_rewrite_reasons: list[OutputPolicyReason] = []
    unbacked_workflow_delivery_rewritten = False
    # The finalization shim overwrites these on a blocker turn — skip the rewrites.
    if not blocker_active:
        if OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in output_policy_verdict.reason_codes:
            user_response = _INTERNAL_BLOCK_TAXONOMY_REPLY
            soft_rewrite_reasons.append(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
            output_policy_verdict.remove(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
        for _residual_vocab_reason in (
            OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK,
            OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK,
        ):
            if _residual_vocab_reason in output_policy_verdict.reason_codes:
                user_response = _INTERNAL_VOCAB_LEAK_REPLY
                soft_rewrite_reasons.append(_residual_vocab_reason)
                output_policy_verdict.remove(_residual_vocab_reason)
        if OutputPolicyReason.WORKFLOW_YAML_IN_REPLY in output_policy_verdict.reason_codes:
            user_response = (
                _BLOCK_YAML_IN_REPLY_REWRITE_WITH_PROPOSAL
                if last_workflow is not None
                else _BLOCK_YAML_IN_REPLY_REWRITE_NO_PROPOSAL
            )
            soft_rewrite_reasons.append(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)
            output_policy_verdict.remove(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)
        # Preserve the unbacked-proposal correction when both soft rewrites apply:
        # a reply must not imply a workflow exists when no proposal was produced.
        if OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM in output_policy_verdict.reason_codes:
            user_response = _unbacked_workflow_delivery_reply(ctx)
            resp_type = "ASK_QUESTION"
            output_policy_verdict.output_kind = CopilotOutputKind.CLARIFICATION_REQUEST
            unbacked_workflow_delivery_rewritten = True
            soft_rewrite_reasons.append(OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM)
            output_policy_verdict.remove(OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM)
        if OutputPolicyReason.MISSING_PROPOSAL_STATE in output_policy_verdict.reason_codes:
            soft_rewrite_reasons.append(OutputPolicyReason.MISSING_PROPOSAL_STATE)
            output_policy_verdict.remove(OutputPolicyReason.MISSING_PROPOSAL_STATE)
        if OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in output_policy_verdict.reason_codes:
            user_response = _ensure_unvalidated_proposal_affordance(str(user_response))
            soft_rewrite_reasons.append(OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE)
            output_policy_verdict.remove(OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE)
    final_output_kind = (
        _blocked_final_output_kind(output_policy_verdict)
        if not output_policy_verdict.allowed
        else output_policy_verdict.output_kind
    )
    output_policy_diagnostics = build_output_policy_diagnostics(
        raw_verdict=raw_output_policy_verdict,
        final_verdict=output_policy_verdict,
        final_output_kind=final_output_kind,
        hard_block_reason_codes=list(output_policy_verdict.reason_codes),
        soft_rewrite_reason_codes=soft_rewrite_reasons,
    )
    output_policy_diagnostics.update(_actuation_obligation_diagnostics(ctx, actuation_obligation))
    trace_data = output_policy_verdict_to_trace_data(
        output_policy_verdict,
        surface="final_translation",
        response_type=resp_type,
    )
    trace_data.update(output_policy_diagnostics)
    LOG.info(
        "copilot output policy final verdict",
        **trace_data,
    )
    if not output_policy_verdict.allowed:
        return _build_output_policy_blocked_result(
            ctx,
            output_policy_verdict,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
            output_policy_diagnostics=output_policy_diagnostics,
        )

    final_user_response = str(user_response)
    attempted_kind = derive_response_kind(ctx.turn_intent)
    tool_call_names = [
        str(entry.get("tool") or entry.get("name") or "") for entry in ctx.tool_activity if isinstance(entry, dict)
    ]
    reason_codes = (
        [code.value for code in ctx.turn_intent.reason_codes]
        if ctx.turn_intent and ctx.turn_intent.reason_codes
        else []
    )
    reason_code = ",".join(reason_codes)
    terminal_reason = None
    if actuation_obligation_terminal:
        reason_code = ACTUATION_OBLIGATION_UNMET_REASON_CODE
        terminal_reason = ACTUATION_OBLIGATION_UNMET_REASON_CODE

    final_user_response, turn_outcome = apply_repeated_reply_guard(
        final_text=final_user_response,
        attempted_kind=attempted_kind,
        blocked_signatures=ctx.blocked_reply_signatures,
        reason_code=reason_code,
        terminal_reason=terminal_reason,
        turn_intent=ctx.turn_intent,
        tool_calls=[name for name in tool_call_names if name],
    )
    if actuation_obligation.reason_code:
        turn_outcome = turn_outcome.model_copy(update={"actuation_obligation_key": actuation_obligation.obligation_key})

    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_user_response,
            updated_workflow=last_workflow,
            global_llm_context=enriched_context or None,
            response_type=resp_type,
            workflow_yaml=last_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            has_staged_proposal=ctx.has_staged_proposal,
            staged_workflow_yaml=ctx.staged_workflow_yaml,
            staged_workflow=ctx.staged_workflow,
            canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
            total_tokens=ctx.total_tokens_used,
            clear_proposed_workflow=resp_type == "ASK_QUESTION",
            proposal_disposition=(
                "no_proposal"
                if unbacked_workflow_delivery_rewritten and last_workflow is None
                else "review_untested"
                if unvalidated
                else "auto_applicable"
            ),
            output_policy_diagnostics=output_policy_diagnostics,
            turn_outcome=turn_outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="response",
                terminal_message=final_user_response,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site="translate_to_agent_result",
    )


def _structural_infeasibility_question(turn_intent: TurnIntent | None) -> str | None:
    """The clarifying question for a turn the classifier judged structurally infeasible.

    Returns the question only when the turn-intent landed on CLARIFY carrying the
    structurally_infeasible reason and a usable question, so a questionless verdict
    (already failed open in build_turn_intent) cannot trigger the pre-loop bail.
    """
    if not isinstance(turn_intent, TurnIntent):
        return None
    # Defense-in-depth: build_turn_intent already forces CLARIFY or drops a questionless verdict.
    if turn_intent.mode != TurnIntentMode.CLARIFY:
        return None
    if TurnIntentReasonCode.STRUCTURALLY_INFEASIBLE not in turn_intent.reason_codes:
        return None
    question = (turn_intent.missing_context_question or "").strip()
    return question or None


def _infeasibility_bail_question(turn_intent: TurnIntent | None) -> str | None:
    """The pre-loop clarification question, or None when the turn must not bail. A
    defer-authoring live-fill turn decides feasibility post-session at the actuation
    hook, so it never takes the pre-loop bail even when judged structurally infeasible."""
    if turn_intent_defers_authoring_live_fill(turn_intent):
        return None
    return _structural_infeasibility_question(turn_intent)


def _build_infeasibility_clarification_result(
    question: str,
    user_message: str,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
    ctx: CopilotContext,
) -> AgentResult:
    """Construct an AgentResult for the structural-infeasibility fast-path.

    Preserves structured cross-turn context, sets user_goal from the user message
    when unset, and appends a decisions_made entry so a follow-up turn can see that
    a clarification was already asked and proceed instead of re-asking.
    """
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    if not structured.user_goal:
        structured.user_goal = user_message[:300]
    structured.decisions_made.append(f"infeasibility clarification asked: {question}")
    enriched_context = structured.to_json_str()

    final_text, outcome = apply_repeated_reply_guard(
        final_text=question,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=list(ctx.blocked_reply_signatures),
        reason_code="feasibility_clarification",
    )
    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_text,
            updated_workflow=None,
            global_llm_context=enriched_context,
            response_type="ASK_QUESTION",
            workflow_yaml=prior_workflow_yaml or None,
            workflow_was_persisted=False,
            clear_proposed_workflow=not outcome_fully_verified(ctx),
            turn_outcome=outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="response",
                terminal_message=final_text,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site="feasibility_clarification",
    )


_DEFER_AUTHORING_NO_SESSION_MESSAGE = (
    "You asked me to fill the page live now instead of authoring a workflow, but I don't have a live "
    "browser session open on that page to act on. Open the page in a live browser session and I'll fill it directly."
)


def apply_defer_authoring_actuation_entry(
    ctx: CopilotContext,
    validated_browser_session_id: str | None,
    *,
    user_message: str,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
) -> AgentResult | None:
    """With a validated live session, enters COMPOSING and enables live actuation while workflow
    mutation stays blocked by the authority gate; without one, returns a deterministic honest terminal."""
    turn_intent = ctx.turn_intent
    if not isinstance(turn_intent, TurnIntent):
        return None
    if not turn_intent_defers_authoring_live_fill(turn_intent):
        return None
    if not validated_browser_session_id:
        return _build_defer_authoring_no_session_result(
            user_message=user_message,
            prior_global_llm_context=prior_global_llm_context,
            prior_workflow_yaml=prior_workflow_yaml,
            ctx=ctx,
        )
    if turn_intent.mode in NO_MUTATION_TURN_INTENT_MODES:
        turn_intent.mode = TurnIntentMode.BUILD
        turn_intent.expected_output = TurnIntentExpectedOutput.RUN_RESULT
    if RequiredContextKey.BROWSER_STATE not in turn_intent.required_context:
        turn_intent.required_context.append(RequiredContextKey.BROWSER_STATE)
    ctx.build_phase = BuildPhase.COMPOSING
    return None


def _build_defer_authoring_no_session_result(
    *,
    user_message: str,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
    ctx: CopilotContext,
) -> AgentResult:
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    if not structured.user_goal:
        structured.user_goal = user_message[:300]
    structured.decisions_made.append("defer-authoring live fill deferred: no live browser session")

    final_text, outcome = apply_repeated_reply_guard(
        final_text=_DEFER_AUTHORING_NO_SESSION_MESSAGE,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=list(ctx.blocked_reply_signatures),
        reason_code="defer_authoring_no_live_session",
    )
    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_text,
            updated_workflow=None,
            global_llm_context=structured.to_json_str(),
            response_type="ASK_QUESTION",
            workflow_yaml=prior_workflow_yaml or None,
            workflow_was_persisted=False,
            clear_proposed_workflow=not outcome_fully_verified(ctx),
            turn_outcome=outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="response",
                terminal_message=final_text,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site="defer_authoring_no_live_session",
    )


def _fallback_llm_key(config: CopilotConfig, current_llm_key: str) -> str | None:
    fallback_key = config.fallback_llm_key
    if not fallback_key or fallback_key == current_llm_key:
        return None
    return fallback_key


def _build_request_policy_clarification_result(
    policy: RequestPolicy,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
    ctx: CopilotContext,
) -> AgentResult:
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    structured.decisions_made.append(
        f"request-policy clarification required: {policy.credential_input_kind}/{policy.clarification_reason}"
    )
    clarification_text = (
        policy.clarification_question or "I need one more detail before I can build and test this workflow safely."
    )
    final_text, outcome = apply_repeated_reply_guard(
        final_text=clarification_text,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=list(ctx.blocked_reply_signatures),
        reason_code="request_policy_clarification",
    )
    return _finalize_result_with_blocker_override(
        ctx,
        _make_agent_result(
            ctx,
            user_response=final_text,
            updated_workflow=None,
            global_llm_context=structured.to_json_str(),
            response_type="ASK_QUESTION",
            workflow_yaml=prior_workflow_yaml or None,
            workflow_was_persisted=False,
            clear_proposed_workflow=not outcome_fully_verified(ctx),
            turn_outcome=outcome,
            turn_id=ctx.turn_id,
            narrative_summary=ctx.narrative_summary,
            narrative_payload=_build_narrative_payload(
                ctx,
                terminal="response",
                terminal_message=final_text,
                narrative_summary=ctx.narrative_summary,
            ),
        ),
        exit_site="request_policy_clarification",
    )


def _agent_output_to_text(agent_output: Any) -> str:
    if isinstance(agent_output, str):
        return agent_output
    if hasattr(agent_output, "model_dump"):
        try:
            return json.dumps(agent_output.model_dump())
        except Exception:
            return str(agent_output)
    try:
        return json.dumps(agent_output, default=str)
    except TypeError:
        return str(agent_output)


def _should_surface_untested_draft_despite_question(ctx: CopilotContext, response_type: str) -> bool:
    if response_type != "ASK_QUESTION" or ctx.last_workflow is None or not ctx.last_workflow_yaml:
        return False
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if request_policy is None or ctx.last_test_ok is not None:
        return False
    if request_policy.testing_intent == "skip_test" and ctx.allow_untested_workflow_draft:
        return True
    return (
        request_policy.clarification_reason == "workflow_credential_inputs_unbound"
        and not request_policy.allow_run_blocks
        and request_policy.allow_missing_credentials_in_draft
    )


def _copy_output_policy_verdict(verdict: OutputPolicyVerdict) -> OutputPolicyVerdict:
    return OutputPolicyVerdict(
        allowed=verdict.allowed,
        output_kind=verdict.output_kind,
        reason_codes=list(verdict.reason_codes),
    )


def _blocked_final_output_kind(verdict: OutputPolicyVerdict) -> CopilotOutputKind:
    clarification_reasons = {
        OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS,
        OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE,
        OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED,
        OutputPolicyReason.ACTUATION_OBLIGATION_STEER,
    }
    if any(reason in clarification_reasons for reason in verdict.reason_codes):
        return CopilotOutputKind.CLARIFICATION_REQUEST
    return CopilotOutputKind.REFUSAL


def _evaluate_copilot_final_output_policy(
    ctx: CopilotContext,
    agent_output: Any,
) -> tuple[OutputPolicyVerdict, str, dict[str, Any]]:
    text = _agent_output_to_text(agent_output)
    action_data = parse_final_response(text)
    response_type = action_data.get("type", "REPLY")
    if response_type not in COPILOT_RESPONSE_TYPES:
        response_type = "REPLY"
    policy_user_response = str(action_data.get("user_response") or text)
    normalized_scaffolding = normalize_response_scaffolding(response_type, policy_user_response)
    response_type = normalized_scaffolding.response_type
    policy_user_response = normalized_scaffolding.user_response or "Done."

    workflow_yaml = None
    if response_type == "REPLACE_WORKFLOW" and isinstance(action_data.get("workflow_yaml"), str):
        workflow_yaml = action_data["workflow_yaml"]
    elif isinstance(getattr(ctx, "last_workflow_yaml", None), str):
        workflow_yaml = ctx.last_workflow_yaml

    workflow_attempted = ctx.has_genuine_workflow_attempt()
    _log_output_policy_parity(
        ctx,
        has_workflow_proposal=bool(workflow_yaml or ctx.last_workflow is not None),
        workflow_attempted=workflow_attempted,
    )
    surface_untested_draft = _should_surface_untested_draft_despite_question(ctx, response_type)
    policy_response_type = "REPLY" if surface_untested_draft else response_type
    if surface_untested_draft:
        policy_user_response = _rewrite_failed_test_response(policy_user_response, ctx)
    updated_workflow_for_kind = (
        ctx.last_workflow if ctx.last_workflow is not None else WORKFLOW_PRESENT_SENTINEL if workflow_yaml else None
    )
    if surface_untested_draft:
        output_kind = (
            CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL
            if ctx.workflow_persisted
            else CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
        )
    else:
        output_kind = derive_output_kind(
            response_type=response_type,
            request_policy=ctx.request_policy,
            updated_workflow=updated_workflow_for_kind,
            workflow_was_persisted=ctx.workflow_persisted,
            workflow_attempted=workflow_attempted,
            unvalidated=False,
        )
    raw_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type=policy_response_type,
        user_response=policy_user_response,
        global_llm_context=action_data.get("global_llm_context"),
        workflow_yaml=workflow_yaml,
        tool_arguments=None,
        has_workflow_proposal=bool(workflow_yaml or ctx.last_workflow is not None),
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        output_kind=output_kind,
    )
    actuation_obligation = _evaluate_actuation_obligation_for_output(
        ctx,
        action_data,
        policy_response_type,
        output_kind,
    )
    if actuation_obligation.status == ActuationObligationStatus.STEER:
        raw_verdict.add(OutputPolicyReason.ACTUATION_OBLIGATION_STEER)
    elif actuation_obligation.status == ActuationObligationStatus.TERMINAL:
        raw_verdict.add(OutputPolicyReason.ACTUATION_OBLIGATION_UNMET)
    hard_verdict = hard_block_output_policy_verdict(raw_verdict)
    deferred_reason_codes = _defer_avoidable_ask_to_recycle(ctx, hard_verdict, response_type)
    if deferred_reason_codes is not None:
        hard_verdict = OutputPolicyVerdict(allowed=True, output_kind=hard_verdict.output_kind)
    diagnostics = build_output_policy_diagnostics(
        raw_verdict=raw_verdict,
        final_verdict=hard_verdict,
        final_output_kind=_blocked_final_output_kind(hard_verdict)
        if not hard_verdict.allowed
        else hard_verdict.output_kind,
        hard_block_reason_codes=list(hard_verdict.reason_codes),
        soft_rewrite_reason_codes=[],
    )
    diagnostics.update(_actuation_obligation_diagnostics(ctx, actuation_obligation))
    if deferred_reason_codes is not None:
        diagnostics["deferred_to_recycle"] = True
        diagnostics["deferred_reason_codes"] = [reason.value for reason in deferred_reason_codes]
    return hard_verdict, response_type, diagnostics


def _defer_avoidable_ask_to_recycle(
    ctx: CopilotContext,
    hard_verdict: OutputPolicyVerdict,
    response_type: str,
) -> list[OutputPolicyReason] | None:
    if hard_verdict.allowed or response_type != "ASK_QUESTION":
        return None
    if list(hard_verdict.reason_codes) != [OutputPolicyReason.AVOIDABLE_OUTPUT_FIELD_CONFIRMATION]:
        return None
    if not recycle_admits_present_completion_contract_ask(ctx):
        return None
    LOG.info(
        "copilot.output_policy_avoidable_deferred_to_recycle",
        deferred_reason_codes=[reason.value for reason in hard_verdict.reason_codes],
        **ctx.genuine_attempt_parity_fields(),
    )
    return list(hard_verdict.reason_codes)


def _build_copilot_input_guardrails(
    InputGuardrailCls: Any,
    GuardrailFunctionOutputCls: Any,
    *,
    policy_inputs: RequestPolicyGuardrailInputs | None = None,
) -> list[Any]:
    # Guardrail classes are injected after importing the optional Agents SDK in
    # run_copilot_agent, keeping module import safe when the SDK is unavailable.
    async def request_policy_guardrail(context: Any, _agent: Any, _input: Any) -> Any:
        ctx = getattr(context, "context", None)
        policy = getattr(ctx, "request_policy", None)
        if not isinstance(policy, RequestPolicy) and policy_inputs is not None:
            policy = await build_request_policy(
                user_message=policy_inputs.user_message,
                workflow_yaml=policy_inputs.workflow_yaml,
                chat_history=policy_inputs.chat_history_messages,
                global_llm_context=policy_inputs.global_llm_context,
                organization_id=policy_inputs.organization_id,
                handler=policy_inputs.request_policy_handler,
                active_criteria=_stored_active_completion_criteria(policy_inputs),
                config=getattr(ctx, "copilot_config", None) if isinstance(ctx, CopilotContext) else None,
            )
            if isinstance(ctx, CopilotContext):
                turn_intent_classifier_result = None
                if policy.user_response_policy != "ask_clarification" or policy.raw_secret_handling == "redacted_draft":
                    turn_intent_classifier_result = await classify_turn_intent(
                        user_message=policy_inputs.user_message,
                        workflow_yaml=policy_inputs.workflow_yaml,
                        chat_history=policy_inputs.chat_history_messages,
                        global_llm_context=policy_inputs.global_llm_context,
                        request_policy=policy,
                        handler=policy_inputs.turn_intent_handler,
                    )
                _store_request_policy_on_context(
                    ctx,
                    policy,
                    policy_inputs,
                    turn_intent_classifier_result=turn_intent_classifier_result,
                )
        blocked = isinstance(policy, RequestPolicy) and policy.user_response_policy == "ask_clarification"
        if isinstance(policy, RequestPolicy):
            turn_intent = ctx.turn_intent if isinstance(ctx, CopilotContext) else None
            trace_data = {
                "surface": "agent_input",
                "policy_present": True,
                "blocked": blocked,
                "user_response_policy": policy.user_response_policy,
                **policy.to_trace_data(),
                **_turn_intent_log_fields(turn_intent),
            }
        else:
            trace_data = {"surface": "agent_input", "blocked": False, "policy_present": False}
        LOG.info("copilot request policy input guardrail verdict", **trace_data)
        return GuardrailFunctionOutputCls(output_info=trace_data, tripwire_triggered=blocked)

    return [
        InputGuardrailCls(
            guardrail_function=request_policy_guardrail,
            name="request_policy_guardrail",
            run_in_parallel=False,
        )
    ]


def _build_copilot_output_guardrails(
    OutputGuardrailCls: Any,
    GuardrailFunctionOutputCls: Any,
) -> list[Any]:
    # See _build_copilot_input_guardrails for why SDK classes are passed in.
    def copilot_output_policy_guardrail(context: Any, _agent: Any, agent_output: Any) -> Any:
        ctx = getattr(context, "context", None)
        if not isinstance(ctx, CopilotContext):
            LOG.warning("copilot output guardrail missing CopilotContext", context_type=type(ctx).__name__)
            verdict = OutputPolicyVerdict(
                allowed=False,
                reason_codes=[OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING],
            )
            response_type = "REPLY"
            diagnostics = build_output_policy_diagnostics(
                raw_verdict=verdict,
                final_verdict=verdict,
                final_output_kind=_blocked_final_output_kind(verdict),
                hard_block_reason_codes=list(verdict.reason_codes),
                soft_rewrite_reason_codes=[],
            )
        else:
            verdict, response_type, diagnostics = _evaluate_copilot_final_output_policy(ctx, agent_output)
        trace_data = output_policy_verdict_to_trace_data(
            verdict,
            surface="agent_output",
            response_type=response_type,
        )
        trace_data.update(diagnostics)
        LOG.info("copilot output policy guardrail verdict", **trace_data)
        return GuardrailFunctionOutputCls(output_info=trace_data, tripwire_triggered=not verdict.allowed)

    return [
        OutputGuardrailCls(
            guardrail_function=copilot_output_policy_guardrail,
            name="copilot_output_policy_guardrail",
        )
    ]


def _output_policy_verdict_from_guardrail_exception(exc: BaseException) -> OutputPolicyVerdict:
    guardrail_result = getattr(exc, "guardrail_result", None)
    guardrail_output = getattr(guardrail_result, "output", None)
    return output_policy_verdict_from_trace_data(getattr(guardrail_output, "output_info", None))


def _output_policy_diagnostics_from_guardrail_exception(exc: BaseException) -> dict[str, Any] | None:
    guardrail_result = getattr(exc, "guardrail_result", None)
    guardrail_output = getattr(guardrail_result, "output", None)
    data = getattr(guardrail_output, "output_info", None)
    if not isinstance(data, dict):
        return None
    keys = {
        "raw_output_kind",
        "final_output_kind",
        "raw_reason_codes",
        "hard_block_reason_codes",
        "soft_rewrite_reason_codes",
        "raw_would_have_failed",
        "contained_failure",
        "final_output_policy_allowed",
    }
    return {key: data[key] for key in keys if key in data}


def _build_output_policy_blocked_result(
    ctx: CopilotContext,
    verdict: OutputPolicyVerdict,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
    output_policy_diagnostics: dict[str, Any] | None = None,
) -> AgentResult:
    # A blocker turn whose signal owns final rendering never ships a proposal;
    # steering-only blockers should still flow through normal output-policy
    # salvage so internal tool text is scrubbed and saved drafts can surface.
    local_blocker_signal = ctx.blocker_signal if isinstance(ctx.blocker_signal, CopilotToolBlockerSignal) else None
    blocker_active = local_blocker_signal is not None and local_blocker_signal.renders_final_reply
    preserved_workflow = (
        ctx.last_workflow if ctx.last_workflow is not None and ctx.last_workflow_yaml and not blocker_active else None
    )
    preserved_workflow_yaml = ctx.last_workflow_yaml if preserved_workflow is not None else None
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    structured.decisions_made.append(
        "output-policy blocked final output: " + ", ".join(reason.value for reason in verdict.reason_codes)
    )
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    add_saved_draft_copy = False
    fallback_user_response: str | None = None
    composed_from_recorded_evidence = False
    evidence = terminal_evidence_from_ctx(ctx)
    if (
        request_policy is not None
        and request_policy.clarification_question
        and OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS in verdict.reason_codes
    ):
        user_response = request_policy.clarification_question
        add_saved_draft_copy = True
    elif OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes:
        user_response = _RAW_SECRET_LEAK_REFUSAL
        add_saved_draft_copy = True
    elif OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes:
        user_response = (
            "I need you to confirm which saved credential should be used before I can continue. "
            "Please reply with the credential name from the Credentials UI, or adjust the workflow to avoid "
            "using credentials."
        )
        add_saved_draft_copy = True
    elif OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes:
        user_response = (
            "The selected credential is not approved for one of the URLs in this workflow. "
            "Please use a saved credential tested for that URL, update the block URL to match the credential's "
            "tested site, or adjust the workflow to avoid using credentials. If the credential was already in this "
            "workflow without a tracked URL, re-select it so Copilot can confirm its URL scope."
        )
        add_saved_draft_copy = True
    elif OutputPolicyReason.ACTUATION_OBLIGATION_UNMET in verdict.reason_codes:
        user_response = _ACTUATION_OBLIGATION_UNMET_REPLY
    elif OutputPolicyReason.ACTUATION_OBLIGATION_STEER in verdict.reason_codes:
        user_response = _ACTUATION_OBLIGATION_STEER_REPLY
    elif preserved_workflow is not None:
        user_response = (
            "I could not safely return that chat reply, but the workflow draft is still saved. "
            "Please review the draft or adjust the request and try again."
        )
        fallback_user_response = user_response
        if terminal_evidence_has_recorded_state(evidence):
            composed_response, tiers = compose_terminal_evidence_user_facing_reason(
                "I could not safely return that chat reply.",
                "Please review the recorded evidence or adjust the request and try again.",
                evidence,
            )
            if any(tier != "draft" for tier in tiers):
                user_response = composed_response
                add_saved_draft_copy = "draft" in tiers
                composed_from_recorded_evidence = True
    else:
        user_response = "I could not safely return that chat reply. Please adjust the request and try again."
        fallback_user_response = user_response
        if terminal_evidence_has_recorded_state(evidence):
            composed_response, tiers = compose_terminal_evidence_user_facing_reason(
                "I could not safely return that chat reply.",
                "Please review the recorded evidence or adjust the request and try again.",
                evidence,
            )
            if any(tier != "draft" for tier in tiers):
                user_response = composed_response
                composed_from_recorded_evidence = True
    if preserved_workflow is not None and add_saved_draft_copy:
        user_response = f"{user_response} {_SAVED_DRAFT_OUTPUT_POLICY_SUFFIX}"
    has_non_actuation_hard_block = any(
        reason
        not in {
            OutputPolicyReason.ACTUATION_OBLIGATION_STEER,
            OutputPolicyReason.ACTUATION_OBLIGATION_UNMET,
        }
        for reason in verdict.reason_codes
    )
    if has_non_actuation_hard_block:
        blocked_reason_code = "output_policy_block"
        blocked_terminal_reason: str | None = "output_policy_block"
    elif OutputPolicyReason.ACTUATION_OBLIGATION_UNMET in verdict.reason_codes:
        blocked_reason_code = ACTUATION_OBLIGATION_UNMET_REASON_CODE
        blocked_terminal_reason = ACTUATION_OBLIGATION_UNMET_REASON_CODE
    else:
        blocked_reason_code = ACTUATION_OBLIGATION_STEER_REASON_CODE
        blocked_terminal_reason = None
    final_user_response, output_policy_outcome = apply_repeated_reply_guard(
        final_text=user_response,
        attempted_kind=ResponseKind.CLARIFY,
        blocked_signatures=ctx.blocked_reply_signatures,
        reason_code=blocked_reason_code,
        terminal_reason=blocked_terminal_reason,
    )
    if blocked_reason_code in {ACTUATION_OBLIGATION_STEER_REASON_CODE, ACTUATION_OBLIGATION_UNMET_REASON_CODE}:
        key = ""
        if output_policy_diagnostics is not None:
            key = str(output_policy_diagnostics.get("actuation_obligation_key") or "")
        output_policy_outcome = output_policy_outcome.model_copy(
            update={"actuation_obligation_key": key or actuation_obligation_key(ctx.turn_intent)}
        )
    if composed_from_recorded_evidence and fallback_user_response is not None:
        composed_verdict = evaluate_output_policy(
            request_policy=ctx.request_policy,
            response_type="ASK_QUESTION",
            user_response=final_user_response,
            global_llm_context=None,
            workflow_yaml=preserved_workflow_yaml or prior_workflow_yaml,
            has_workflow_proposal=preserved_workflow is not None,
            workflow_was_persisted=ctx.workflow_persisted,
            workflow_attempted=ctx.has_genuine_workflow_attempt(),
            unvalidated=ctx.last_test_ok is not True,
            output_kind=verdict.output_kind,
        )
        if not composed_verdict.allowed:
            LOG.warning(
                "copilot output-policy recorded-evidence fallback failed output policy; using generic fallback",
                output_policy_reasons=[code.value for code in composed_verdict.reason_codes],
            )
            final_user_response, output_policy_outcome = apply_repeated_reply_guard(
                final_text=fallback_user_response,
                attempted_kind=ResponseKind.CLARIFY,
                blocked_signatures=ctx.blocked_reply_signatures,
                reason_code="output_policy_block",
                terminal_reason="output_policy_block",
            )
    return _make_agent_result(
        ctx,
        user_response=final_user_response,
        updated_workflow=preserved_workflow,
        global_llm_context=structured.to_json_str(),
        response_type="ASK_QUESTION",
        workflow_yaml=preserved_workflow_yaml or prior_workflow_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        has_staged_proposal=ctx.has_staged_proposal,
        staged_workflow_yaml=ctx.staged_workflow_yaml,
        staged_workflow=ctx.staged_workflow,
        canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
        total_tokens=ctx.total_tokens_used,
        clear_proposed_workflow=False,
        proposal_disposition=(
            "no_proposal"
            if preserved_workflow is None
            else "review_untested"
            if ctx.last_test_ok is not True
            else "review_tested"
        ),
        output_policy_diagnostics=output_policy_diagnostics
        or build_output_policy_diagnostics(
            raw_verdict=verdict,
            final_verdict=verdict,
            final_output_kind=_blocked_final_output_kind(verdict),
            hard_block_reason_codes=list(verdict.reason_codes),
            soft_rewrite_reason_codes=[],
        ),
        turn_outcome=output_policy_outcome,
        turn_id=ctx.turn_id,
        narrative_summary=ctx.narrative_summary,
        narrative_payload=_build_narrative_payload(
            ctx,
            terminal="response",
            terminal_message=final_user_response,
            narrative_summary=ctx.narrative_summary,
        ),
    )


async def run_copilot_agent(
    stream: EventSourceStream,
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
    llm_api_handler: LLMAPIHandler | None,
    api_key: str | None = None,
    security_rules: str = "",
    config: CopilotConfig | None = None,
    turn_index: int | None = None,
    turn_id: str | None = None,
    prior_copilot_workflow_yaml: str | None = None,
    prior_block_count: int | None = None,
    stored_completion_criteria: StoredCriteriaSnapshot | None = None,
    prior_turn_outcome: TurnOutcome | None = None,
) -> AgentResult:
    # One id per turn — passed to every downstream AgentResult and
    # CopilotContext so the envelope and terminal frames correlate. The
    # default_factory on CopilotContext is only the per-construction fallback.
    if turn_id is None:
        turn_id = uuid.uuid4().hex
    normalized_turn_index = turn_index if turn_index is not None else 0
    try:
        # Initialize tracing before opening the turn span so Logfire's OTel provider
        # is installed; otherwise the very first turn lands the parent span on
        # OTel's no-op ProxyTracer when running locally with COPILOT_TRACING_ENABLED.
        ensure_tracing_initialized()
        ctx_sink: list[CopilotContext] = []
        with _copilot_turn_span(
            chat_request=chat_request,
            chat_history=chat_history,
            turn_index=turn_index,
            turn_id=turn_id,
        ) as turn_span:
            try:
                return await _run_copilot_turn_impl(
                    stream=stream,
                    organization_id=organization_id,
                    chat_request=chat_request,
                    chat_history=chat_history,
                    global_llm_context=global_llm_context,
                    debug_run_info_text=debug_run_info_text,
                    llm_api_handler=llm_api_handler,
                    api_key=api_key,
                    security_rules=security_rules,
                    config=config,
                    turn_id=turn_id,
                    turn_index=normalized_turn_index,
                    prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
                    prior_block_count=prior_block_count,
                    ctx_sink=ctx_sink,
                    stored_completion_criteria=stored_completion_criteria,
                    prior_turn_outcome=prior_turn_outcome,
                )
            except Exception as exc:
                LOG.error(
                    "Copilot turn unhandled error",
                    error_type=type(exc).__name__,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    exc_info=True,
                )
                turn_span.record_exception(exc)
                ctx = CopilotContext(
                    organization_id=organization_id,
                    workflow_id=chat_request.workflow_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    workflow_yaml=chat_request.workflow_yaml or "",
                    browser_session_id=None,
                    stream=stream,
                    api_key=api_key,
                    user_message=chat_request.message,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    turn_id=turn_id,
                    turn_index=normalized_turn_index,
                )
                return _build_unexpected_error_exit_result(ctx, global_llm_context, error=exc, span=turn_span)
            finally:
                finalize_outcome_verification_trace(ctx_sink[0] if ctx_sink else None, turn_span)
    except Exception as exc:
        LOG.error(
            "Copilot turn unhandled error",
            error_type=type(exc).__name__,
            workflow_permanent_id=chat_request.workflow_permanent_id,
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            exc_info=True,
        )
        ctx = CopilotContext(
            organization_id=organization_id,
            workflow_id=chat_request.workflow_id,
            workflow_permanent_id=chat_request.workflow_permanent_id,
            workflow_yaml=chat_request.workflow_yaml or "",
            browser_session_id=None,
            stream=stream,
            api_key=api_key,
            user_message=chat_request.message,
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            turn_id=turn_id,
            turn_index=normalized_turn_index,
        )
        return _build_unexpected_error_exit_result(ctx, global_llm_context, error=exc)


async def _run_copilot_turn_impl(
    *,
    stream: EventSourceStream,
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
    llm_api_handler: LLMAPIHandler | None,
    api_key: str | None,
    security_rules: str,
    config: CopilotConfig | None,
    turn_id: str,
    turn_index: int,
    prior_copilot_workflow_yaml: str | None = None,
    prior_block_count: int | None = None,
    ctx_sink: list[CopilotContext] | None = None,
    stored_completion_criteria: StoredCriteriaSnapshot | None = None,
    prior_turn_outcome: TurnOutcome | None = None,
) -> AgentResult:
    copilot_config = config or CopilotConfig(security_rules=security_rules)
    chat_history_text = _format_chat_history(chat_history)
    safe_chat_history_text = redact_raw_secrets_for_prompt(chat_history_text)
    safe_workflow_yaml = redact_raw_secrets_for_prompt(chat_request.workflow_yaml or "")
    safe_global_llm_context = sanitize_global_llm_context_for_prompt(
        redact_raw_secrets_for_prompt(global_llm_context or "")
    )
    previous_user_messages = [msg.content for msg in chat_history if msg.sender == "user"]
    previous_user_message = previous_user_messages[-1] if previous_user_messages else None

    try:
        from agents import Agent, GuardrailFunctionOutput, InputGuardrail, OutputGuardrail, trace
        from agents.exceptions import (
            InputGuardrailTripwireTriggered,
            MaxTurnsExceeded,
            OutputGuardrailTripwireTriggered,
        )
        from agents.mcp import MCPServerManager
        from agents.run_context import RunContextWrapper
    except ModuleNotFoundError as e:
        if e.name == "agents":
            LOG.error(
                "OpenAI Agents SDK dependency missing",
                error=str(e),
                workflow_permanent_id=chat_request.workflow_permanent_id,
            )
            missing_sdk_reply = (
                "Copilot backend is missing the OpenAI Agents SDK dependency. "
                "Rebuild or redeploy the backend image so `openai-agents` is installed."
            )
            # ctx isn't constructed yet at this exit (deploy-state check fires
            # before CopilotContext allocation), so no inherited bans to thread.
            final_missing_text, missing_sdk_outcome = apply_repeated_reply_guard(
                final_text=missing_sdk_reply,
                attempted_kind=ResponseKind.CLARIFY,
                blocked_signatures=(),
                terminal_reason="missing_sdk",
            )
            return _make_agent_result(
                None,
                user_response=final_missing_text,
                updated_workflow=None,
                global_llm_context=global_llm_context,
                workflow_yaml=chat_request.workflow_yaml or None,
                turn_outcome=missing_sdk_outcome,
                turn_id=turn_id,
            )
        raise

    ctx = CopilotContext(
        organization_id=organization_id,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_yaml=chat_request.workflow_yaml or "",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
        user_message=chat_request.message,
        workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
        turn_id=turn_id,
        turn_index=turn_index,
        prior_block_count=prior_block_count,
        prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
        prior_turn_outcome=prior_turn_outcome,
        block_authoring_policy=copilot_config.block_authoring_policy,
        impose_synthesized_code_block=copilot_config.impose_synthesized_code_block,
        copilot_config=copilot_config,
        target_block_label=getattr(chat_request, "target_block_label", None),
    )
    LOG.info(
        "copilot_block_authoring_policy_resolved",
        block_authoring_policy=normalize_block_authoring_policy(ctx.block_authoring_policy).name,
        block_authoring_policy_value=normalize_block_authoring_policy(ctx.block_authoring_policy).value,
        workflow_permanent_id=ctx.workflow_permanent_id,
        workflow_id=ctx.workflow_id,
        workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
        turn_id=ctx.turn_id,
        impose_synthesized_code_block=ctx.impose_synthesized_code_block,
    )
    # Fail loud if a future caller skips the kwarg and gets a fresh UUID from
    # the default_factory — the envelope and terminal frames would then carry
    # different ids and correlation would silently break. Uses a real
    # conditional so the check survives ``python -O``.
    if ctx.turn_id != turn_id:
        raise RuntimeError(
            f"CopilotContext.turn_id ({ctx.turn_id!r}) diverged from route-supplied turn_id ({turn_id!r})"
        )
    if ctx_sink is not None:
        ctx_sink.append(ctx)
    policy_inputs = RequestPolicyGuardrailInputs(
        user_message=chat_request.message,
        workflow_yaml=safe_workflow_yaml,
        chat_history_text=safe_chat_history_text,
        chat_history_messages=list(chat_history),
        global_llm_context=safe_global_llm_context,
        organization_id=organization_id,
        request_policy_handler=await _resolve_request_policy_handler(
            llm_api_handler,
            chat_request.workflow_permanent_id,
            organization_id,
        ),
        turn_intent_handler=llm_api_handler,
        previous_user_message=previous_user_message,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_run_id=getattr(chat_request, "workflow_run_id", None),
        browser_session_id=getattr(chat_request, "browser_session_id", None),
        fix_origin=getattr(chat_request, "fix_origin", False),
        stored_completion_criteria=stored_completion_criteria,
    )
    request_policy_guardrails = _build_copilot_input_guardrails(
        InputGuardrail,
        GuardrailFunctionOutput,
        policy_inputs=policy_inputs,
    )
    # Run the request-policy guardrail as the authoritative input gate before
    # browser/session setup, model execution, or tool calls.
    # Do not also attach it to the main Agent; the SDK would invoke it again and
    # duplicate policy telemetry.
    request_policy_guardrail_result = await request_policy_guardrails[0].run(
        Agent(name="workflow-copilot-request-policy", instructions=""),
        chat_request.message,
        RunContextWrapper(context=ctx),
    )
    # Emit TURN_START after the guardrail runs so the envelope carries an
    # accurate ``mode`` when ``ctx.turn_intent`` is populated, and falls back
    # to ``UNKNOWN`` defensively otherwise. Best-effort — an emission failure
    # must not abort an otherwise-runnable turn.
    try:
        await emit_turn_start(stream, ctx)
    except Exception as emit_err:
        LOG.warning("copilot_narrative_turn_start_emit_failed", error=str(emit_err))
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if request_policy is not None:
        _store_turn_context_packet_on_context(
            ctx,
            request_policy=request_policy,
            chat_request=chat_request,
            chat_history=chat_history,
            debug_run_info_text=debug_run_info_text,
            prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
        )
    if request_policy is not None and request_policy_guardrail_result.output.tripwire_triggered:
        return _build_request_policy_clarification_result(
            request_policy,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
            ctx=ctx,
        )
    if request_policy is None:
        raise CopilotRequestPolicyMissingError()

    agent_user_message, safe_chat_history_text = _request_policy_agent_inputs(
        request_policy,
        user_message=chat_request.message,
        chat_history_text=safe_chat_history_text,
        previous_user_message=previous_user_message,
    )

    # Hydrate the per-chat discovery counter from the inbound global_llm_context
    # and set the initial build phase. Phase is set once per turn by the
    # orchestrator; transitions happen inside `discover_workflow_entrypoint`
    # and `update_and_run_blocks`, never from a model emission.
    prior_structured_context = StructuredContext.from_json_str(global_llm_context)
    ctx.prior_discovery_calls_made = prior_structured_context.discovery_calls_made
    ctx.prior_page_inspection_calls_made = prior_structured_context.page_inspection_calls_made
    ctx.prior_observed_acted_pages = [page.model_dump() for page in prior_structured_context.observed_acted_pages]
    ctx.prior_fill_carry = [carry.model_dump() for carry in prior_structured_context.fill_carry]
    ctx.build_phase = initial_build_phase(
        ctx.turn_intent,
        chat_request.message or "",
        agent_user_message or "",
        chat_request.workflow_yaml or "",
    )
    LOG.info(
        "copilot.build_phase_initial",
        build_phase=ctx.build_phase.value,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        prior_discovery_calls_made=ctx.prior_discovery_calls_made,
        prior_page_inspection_calls_made=ctx.prior_page_inspection_calls_made,
    )

    # Infeasibility rides on turn_intent: a verdict carrying a question bails to a pre-loop clarification.
    infeasibility_question = _infeasibility_bail_question(ctx.turn_intent)
    if infeasibility_question is not None:
        return _build_infeasibility_clarification_result(
            question=infeasibility_question,
            user_message=agent_user_message,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
            ctx=ctx,
        )

    from skyvern.cli.mcp_tools import mcp as skyvern_mcp
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotBuiltUnverified,
        CopilotGoalSatisfied,
        CopilotNonRetriableNavError,
        CopilotTotalTimeoutError,
        CopilotUnrecoverableToolError,
        gate_decision_trace_fields,
        run_with_enforcement,
    )
    from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
    from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
    from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config
    from skyvern.forge.sdk.copilot.session_factory import create_copilot_session
    from skyvern.forge.sdk.copilot.tools import (
        NATIVE_TOOLS,
        _build_skyvern_mcp_overlays,
        get_skyvern_mcp_alias_map,
    )

    validated_browser_session_id = await _resolve_live_browser_session_id(chat_request, organization_id)
    ctx.browser_session_id = validated_browser_session_id
    defer_authoring_terminal = apply_defer_authoring_actuation_entry(
        ctx,
        validated_browser_session_id,
        user_message=agent_user_message,
        prior_global_llm_context=global_llm_context,
        prior_workflow_yaml=chat_request.workflow_yaml,
    )
    if defer_authoring_terminal is not None:
        return defer_authoring_terminal

    model_name, run_config, llm_key, supports_vision = resolve_model_config(
        llm_api_handler,
        copilot_config=copilot_config,
    )
    ctx.supports_vision = supports_vision
    output_guardrails = _build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

    alias_map = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays(copilot_config.block_authoring_policy)
    alias_map, overlays = _mcp_tool_surface_for_turn(alias_map, overlays, ctx.turn_intent, ctx.request_policy)

    native_tools = _native_tools_for_turn(list(NATIVE_TOOLS), ctx.turn_intent, ctx.request_policy)
    tool_info: list[tuple[str, str]] = [(tool.name, tool.description or "") for tool in native_tools]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())

    tool_usage_guide = _build_tool_usage_guide(tool_info)
    system_prompt = _build_dynamic_system_prompt(
        tool_usage_guide=tool_usage_guide,
        config=copilot_config,
    )

    user_workflow_change_summary = ""
    runnable_draft_summary = ""
    repeated_reply_warning = ""
    if isinstance(ctx.turn_context_packet, TurnContextPacket):
        if ctx.turn_context_packet.workflow_change_context is not None:
            user_workflow_change_summary = ctx.turn_context_packet.workflow_change_context.rendered_summary
        if ctx.turn_context_packet.runnable_draft_context is not None:
            runnable_draft_summary = ctx.turn_context_packet.runnable_draft_context.rendered_summary
        if ctx.turn_context_packet.repeated_reply_context is not None:
            repeated_reply_warning = ctx.turn_context_packet.repeated_reply_context.rendered_summary

    scoped_global_llm_context = safe_global_llm_context
    if ctx.target_block_label:
        # Defang the user-supplied label before embedding it in the instruction: collapse
        # whitespace and drop quotes so it can't break out of the string or inject directives.
        safe_target_block_label = re.sub(r"\s+", " ", ctx.target_block_label).replace('"', "").strip()[:200]
        scoped_global_llm_context = (
            f'CRITICAL: Regenerate ONLY the block labeled "{safe_target_block_label}". '
            "Preserve every other block's code, goal, steps, and configuration exactly as-is.\n\n"
            f"{safe_global_llm_context}"
        )

    user_message = _build_user_context(
        workflow_yaml=safe_workflow_yaml,
        chat_history_text=safe_chat_history_text,
        global_llm_context=scoped_global_llm_context,
        debug_run_info_text=redact_raw_secrets_for_prompt(debug_run_info_text),
        user_message=agent_user_message,
        user_workflow_change_summary=user_workflow_change_summary,
        runnable_draft_summary=runnable_draft_summary,
        repeated_reply_warning=repeated_reply_warning,
    )

    LOG.info(
        "Starting copilot agent loop",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        user_message_len=len(user_message),
        llm_key=llm_key,
    )

    trace_context: Any = contextlib.nullcontext()
    if is_tracing_enabled():
        trace_context = trace(
            workflow_name="Copilot workflow",
            group_id=chat_request.workflow_copilot_chat_id,
            metadata={
                "workflow_permanent_id": chat_request.workflow_permanent_id,
                "organization_id": organization_id,
                "llm_key": llm_key,
                "user_message_len": str(len(user_message)),
                **{f"request_policy_{key}": str(value) for key, value in request_policy.to_trace_data().items()},
                **_turn_intent_trace_fields(ctx.turn_intent),
                **_turn_context_trace_fields(ctx.turn_context_packet),
            },
        )

    chat_id = chat_request.workflow_copilot_chat_id or chat_request.workflow_permanent_id

    async def _run_attempt(
        attempt_model_name: str,
        attempt_run_config: Any,
        attempt_llm_key: str,
    ) -> RunResultStreaming:
        mcp_server = SkyvernOverlayMCPServer(
            transport=skyvern_mcp,
            overlays=overlays,
            alias_map=alias_map,
            allowlist=frozenset(alias_map.values()),
            context_provider=lambda: ctx,
        )
        # The discovery walker reaches the connected FastMCP client through
        # ctx, without exposing private overlay state.
        ctx.discovery_mcp_server = mcp_server
        agent = Agent(
            name="workflow-copilot",
            instructions=system_prompt,
            tools=native_tools,
            mcp_servers=[mcp_server],
            model=attempt_model_name,
            output_guardrails=output_guardrails,
        )
        session = create_copilot_session(chat_id)
        model_token = _copilot_model_name.set(attempt_model_name)
        try:
            async with MCPServerManager([mcp_server]) as manager:
                agent.mcp_servers = list(manager.active_servers)
                attempts = 2 if ctx.allow_untested_workflow_draft else 1
                for attempt in range(attempts):
                    try:
                        result = await run_with_enforcement(
                            agent=agent,
                            initial_input=user_message,
                            ctx=ctx,
                            stream=stream,
                            max_turns=copilot_config.max_turns,
                            hooks=CopilotRunHooks(ctx),
                            run_config=attempt_run_config,
                            session=session,
                            copilot_config=copilot_config,
                        )
                        break
                    except Exception as exc:
                        if (
                            attempt + 1 < attempts
                            and ctx.last_workflow is None
                            and isinstance(exc, LiteLLMNotFoundError)
                        ):
                            LOG.warning("Retrying untested draft agent loop after model lookup failure")
                            continue
                        raise
            LOG.info(
                "Copilot agent model attempt succeeded",
                workflow_permanent_id=chat_request.workflow_permanent_id,
                llm_key=attempt_llm_key,
            )
            return result
        finally:
            _copilot_model_name.reset(model_token)
            session.close()

    try:
        with trace_context:
            try:
                try:
                    result = await _run_attempt(model_name, run_config, llm_key)
                except Exception as primary_error:
                    fallback_llm_key = _fallback_llm_key(copilot_config, llm_key)
                    if fallback_llm_key is None or not _is_retriable_llm_error(primary_error):
                        raise
                    LOG.warning(
                        "Copilot agent model attempt failed; retrying fallback model",
                        workflow_permanent_id=chat_request.workflow_permanent_id,
                        primary_llm_key=llm_key,
                        fallback_llm_key=fallback_llm_key,
                        error_type=type(primary_error).__name__,
                    )
                    fallback_model_name, fallback_run_config, fallback_resolved_key, fallback_supports_vision = (
                        resolve_model_config(
                            llm_api_handler,
                            copilot_config=copilot_config,
                            llm_key_override=fallback_llm_key,
                        )
                    )
                    ctx.supports_vision = fallback_supports_vision
                    result = await _run_attempt(fallback_model_name, fallback_run_config, fallback_resolved_key)
                agent_result = await _translate_to_agent_result(
                    result,
                    ctx,
                    global_llm_context,
                    chat_request,
                    organization_id,
                )
                # Inline ``REPLACE_WORKFLOW`` bypasses the ``update_workflow``
                # tool, so the envelope fires here instead — keeps the FE
                # bubble identical regardless of which path produced the
                # draft. Best-effort.
                if (
                    agent_result.response_type == "REPLACE_WORKFLOW"
                    and agent_result.updated_workflow is not None
                    and ctx.stream is not None
                ):
                    try:
                        await maybe_emit_design_end(ctx.stream, ctx)
                        await emit_workflow_draft(ctx.stream, ctx, agent_result.updated_workflow)
                    except Exception as emit_err:
                        LOG.warning("copilot_narrative_inline_replace_emit_failed", error=str(emit_err))
                    ctx.design_start_emitted = False
                    ctx.design_end_emitted = False
                return agent_result
            except asyncio.CancelledError:
                # Re-raising would leave the route with ``agent_result is None``
                # and skip its ``workflow_was_persisted`` rollback decision.
                LOG.info("Copilot run cancelled")
                return _build_cancelled_exit_result(ctx, global_llm_context)
            except InputGuardrailTripwireTriggered:
                return _build_request_policy_clarification_result(
                    request_policy,
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                    ctx=ctx,
                )
            except OutputGuardrailTripwireTriggered as exc:
                return _build_output_policy_blocked_result(
                    ctx,
                    _output_policy_verdict_from_guardrail_exception(exc),
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                    output_policy_diagnostics=_output_policy_diagnostics_from_guardrail_exception(exc),
                )
            except CopilotGoalSatisfied:
                LOG.info(
                    "Copilot run stopped after verified goal satisfaction",
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    workflow_run_id=ctx.last_successful_run_blocks_workflow_run_id,
                )
                return await _build_goal_satisfied_exit_result(ctx, global_llm_context)
            except CopilotBuiltUnverified:
                LOG.info(
                    "Copilot run stopped after built-unverified repair-inert outcome",
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    workflow_run_id=ctx.last_successful_run_blocks_workflow_run_id,
                )
                return await _build_built_unverified_exit_result(ctx, global_llm_context)
            except CopilotTurnHalt as exc:
                LOG.info(
                    "Copilot run stopped after typed turn halt",
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    **turn_halt_to_trace_data(exc.halt),
                )
                return _build_turn_halt_exit_result(ctx, global_llm_context, exc.halt)
            except MaxTurnsExceeded:
                return _build_max_turns_exit_result(ctx, global_llm_context)
            except CopilotTotalTimeoutError:
                return _build_timeout_exit_result(ctx, global_llm_context)
            except CopilotUnrecoverableToolError as exc:
                LOG.warning(
                    "Copilot run halted on unrecoverable tool error",
                    tool_name=exc.tool_name,
                    error_message=exc.error_message,
                    organization_id=organization_id,
                )
                return _build_unexpected_error_exit_result(ctx, global_llm_context, error=exc)
            except CopilotNonRetriableNavError as exc:
                LOG.warning(
                    "Copilot run halted on non-retriable navigation error",
                    url=exc.url,
                    error_message=exc.error_message,
                    organization_id=organization_id,
                )
                # Non-retriable nav errors prove the current workflow doesn't
                # work; zero the proposal even if other tools succeeded.
                nav_reply = (
                    f"The target URL could not be reached. Error: {exc.error_message}. "
                    "Please verify the URL and try again."
                )
                final_nav_text, nav_outcome = apply_repeated_reply_guard(
                    final_text=nav_reply,
                    attempted_kind=ResponseKind.CLARIFY,
                    blocked_signatures=ctx.blocked_reply_signatures,
                    terminal_reason="non_retriable_nav",
                )
                return _finalize_result_with_blocker_override(
                    ctx,
                    _make_agent_result(
                        ctx,
                        user_response=final_nav_text,
                        updated_workflow=None,
                        global_llm_context=global_llm_context,
                        workflow_yaml=None,
                        workflow_was_persisted=ctx.workflow_persisted,
                        has_staged_proposal=ctx.has_staged_proposal,
                        staged_workflow_yaml=ctx.staged_workflow_yaml,
                        staged_workflow=ctx.staged_workflow,
                        canonical_was_persisted_due_to_param_change=ctx.canonical_was_persisted_due_to_param_change,
                        total_tokens=ctx.total_tokens_used,
                        turn_outcome=nav_outcome,
                        turn_id=ctx.turn_id,
                        narrative_summary=ctx.narrative_summary,
                        narrative_payload=_build_narrative_payload(
                            ctx,
                            terminal="response",
                            terminal_message=final_nav_text,
                            narrative_summary=ctx.narrative_summary,
                        ),
                    ),
                    exit_site="non_retriable_nav",
                )
    except Exception as e:
        try:
            # Terminal-path gate-decision record; the per-tool hook records the
            # in-loop path, and the later write wins on the shared snapshot.
            gate_fields = gate_decision_trace_fields(ctx)
            record_gate_decision(ctx, gate_fields)
            goal_satisfied = gate_fields["gate_satisfied"]
        except Exception:
            LOG.error("Copilot agent error", error=str(e), exc_info=True)
            return _build_unexpected_error_exit_result(ctx, global_llm_context, error=e)
        return await _resolve_wrapped_exception_exit_result(
            ctx,
            global_llm_context,
            goal_satisfied=goal_satisfied,
            error=e,
            workflow_permanent_id=chat_request.workflow_permanent_id,
        )
