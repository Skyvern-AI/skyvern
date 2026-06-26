import asyncio
import contextlib
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog
import yaml
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, status
from opentelemetry import trace as otel_trace
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from skyvern import analytics
from skyvern.config import settings
from skyvern.constants import DEFAULT_LOGIN_PROMPT
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write, resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.code_block_steps import apply_derived_code_block_steps, derive_code_block_steps_in_yaml
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    CRITERIA_SET_STATUS_ACTIVE,
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    criteria_from_json,
    criteria_to_json,
    plan_persistence,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import AgentResult, ProposalDisposition, TurnNarrativePayload
from skyvern.forge.sdk.copilot.data_write_defaults import default_data_write_continue_on_failure
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler
from skyvern.forge.sdk.copilot.output_utils import truncate_output
from skyvern.forge.sdk.copilot.recoverable_failure import (
    RecoverableFailure,
    build_recoverable_failure,
    format_recoverable_failure_reply,
    merge_failure_into_context,
)
from skyvern.forge.sdk.copilot.turn_outcome import (
    CopilotComposerMode,
    build_minimal_turn_outcome,
    with_copilot_code_mode_metadata,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotApplyProposedWorkflowRequest,
    WorkflowCopilotAudioUploadResponse,
    WorkflowCopilotCancelRequest,
    WorkflowCopilotChat,
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotChatSummary,
    WorkflowCopilotClearProposedWorkflowRequest,
    WorkflowCopilotProcessingUpdate,
    WorkflowCopilotStreamErrorUpdate,
    WorkflowCopilotStreamMessageType,
    WorkflowCopilotStreamResponseUpdate,
    WorkflowYAMLConversionRequest,
    WorkflowYAMLConversionResponse,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import (
    BlockYAML,
    BranchConditionYAML,
    ConditionalBlockYAML,
    ForLoopBlockYAML,
    LoginBlockYAML,
    WhileLoopBlockYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
)
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

WORKFLOW_KNOWLEDGE_BASE_PATH = Path("skyvern/forge/prompts/skyvern/workflow_knowledge_base.txt")
CHAT_HISTORY_CONTEXT_MESSAGES = 10
ALLOWED_WORKFLOW_COPILOT_AUDIO_CONTENT_TYPES = {
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/wave",
    "audio/webm",
    "audio/x-wav",
}

LOG = structlog.get_logger()


def _proxy_location_alias_key(value: str) -> str:
    return "_".join(value.strip().upper().replace("-", "_").split())


def _build_copilot_proxy_location_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}

    def add(alias: str, proxy_location: ProxyLocation) -> None:
        aliases[_proxy_location_alias_key(alias)] = proxy_location.value

    for proxy_location in ProxyLocation:
        add(proxy_location.name, proxy_location)
        add(proxy_location.value, proxy_location)

    for proxy_location in ProxyLocation.residential_country_locations():
        add(ProxyLocation.get_country_code(proxy_location), proxy_location)

    add("USA", ProxyLocation.RESIDENTIAL)
    add("United States", ProxyLocation.RESIDENTIAL)
    add("United States of America", ProxyLocation.RESIDENTIAL)
    add("RESIDENTIAL_US", ProxyLocation.RESIDENTIAL)
    add("UK", ProxyLocation.RESIDENTIAL_GB)
    add("United Kingdom", ProxyLocation.RESIDENTIAL_GB)

    return aliases


_COPILOT_PROXY_LOCATION_ALIASES = _build_copilot_proxy_location_aliases()


def _canonicalize_copilot_proxy_location(parsed_yaml: dict[str, Any]) -> None:
    if "proxy_location" not in parsed_yaml:
        return

    proxy_location = parsed_yaml.get("proxy_location")
    if not isinstance(proxy_location, str):
        return

    canonical = _COPILOT_PROXY_LOCATION_ALIASES.get(_proxy_location_alias_key(proxy_location))
    if canonical is None:
        return

    parsed_yaml["proxy_location"] = canonical


def _canonicalize_copilot_block_type_aliases(value: Any) -> None:
    if isinstance(value, dict):
        block_type = value.get("block_type")
        if isinstance(block_type, str):
            value["block_type"] = normalize_copilot_block_type_alias(block_type)
        for child in value.values():
            _canonicalize_copilot_block_type_aliases(child)
    elif isinstance(value, list):
        for item in value:
            _canonicalize_copilot_block_type_aliases(item)


async def _resolve_copilot_agent_handler(
    workflow_permanent_id: str,
    organization_id: str,
) -> LLMAPIHandler:
    handler = await resolve_main_copilot_handler(workflow_permanent_id, organization_id)
    return handler or app.LLM_API_HANDLER


@contextmanager
def bind_copilot_session_id(chat_id: str | None) -> Iterator[None]:
    # In-place mutation (not scoped()) preserves request-scoped fields the FastAPI middleware wrote.
    ctx = skyvern_context.current()
    if ctx is None or chat_id is None:
        yield
        return
    prev = ctx.copilot_session_id
    ctx.copilot_session_id = chat_id
    try:
        yield
    finally:
        ctx.copilot_session_id = prev


COPILOT_CODE_MODE_OPT_OUT_EVENT = "copilot_code_mode_opt_out"
COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON = "copilot_recoverable_failure"


def _effective_copilot_composer_mode(
    chat_request: WorkflowCopilotChatRequest,
    *,
    uses_v2: bool,
    code_mode_fallback: bool = False,
) -> CopilotComposerMode:
    if chat_request.mode == "ask":
        return "ask"
    if chat_request.mode == "build":
        return "code" if chat_request.code_block is True else "build"
    if uses_v2:
        if chat_request.code_block is not None:
            return "code" if chat_request.code_block is True else "build"
        return "code" if code_mode_fallback else "build"
    return "ask"


def _latest_assistant_turn_outcome(chat_messages: list[WorkflowCopilotChatMessage]) -> TurnOutcome | None:
    for message in reversed(chat_messages):
        if message.sender == WorkflowCopilotChatSender.AI and message.turn_outcome is not None:
            return message.turn_outcome
    return None


def _should_emit_copilot_code_mode_opt_out(
    *,
    prior_turn_outcome: TurnOutcome | None,
    to_mode: CopilotComposerMode,
    current_code_available: bool,
) -> bool:
    if prior_turn_outcome is None:
        return False
    from_mode = prior_turn_outcome.copilot_effective_mode
    if from_mode is None or from_mode == to_mode:
        return False
    if from_mode == "code" and to_mode != "code":
        return True
    return (
        from_mode == "build"
        and to_mode == "ask"
        and (prior_turn_outcome.copilot_code_available or current_code_available)
    )


def _reason_category_for_copilot_code_mode_opt_out(
    prior_turn_outcome: TurnOutcome,
) -> str:
    if (
        prior_turn_outcome.copilot_last_code_build_failed
        or prior_turn_outcome.copilot_repair_ceiling_hit
        or prior_turn_outcome.terminal_reason == COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON
    ):
        return "failure"
    if prior_turn_outcome.copilot_pending_capability:
        return "missing_capability"
    return "confusion"


def _capture_copilot_code_mode_opt_out(
    *,
    prior_turn_outcome: TurnOutcome | None,
    to_mode: CopilotComposerMode,
    current_code_available: bool,
    workflow_copilot_chat_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    turn_id: str | None,
) -> None:
    if prior_turn_outcome is None or not _should_emit_copilot_code_mode_opt_out(
        prior_turn_outcome=prior_turn_outcome,
        to_mode=to_mode,
        current_code_available=current_code_available,
    ):
        return
    try:
        analytics.capture(
            COPILOT_CODE_MODE_OPT_OUT_EVENT,
            data={
                "from_mode": prior_turn_outcome.copilot_effective_mode,
                "to_mode": to_mode,
                "reason_category": _reason_category_for_copilot_code_mode_opt_out(prior_turn_outcome),
                "last_code_build_failed": prior_turn_outcome.copilot_last_code_build_failed,
                "repair_ceiling_hit": prior_turn_outcome.copilot_repair_ceiling_hit,
                "pending_capability": prior_turn_outcome.copilot_pending_capability,
                "org_id": organization_id,
                "workflow_permanent_id": workflow_permanent_id,
                "workflow_copilot_chat_id": workflow_copilot_chat_id,
                "turn_id": turn_id,
                "prior_turn_id": prior_turn_outcome.copilot_turn_id,
            },
            distinct_id=workflow_copilot_chat_id,
        )
    except Exception:
        LOG.warning(
            "Failed to capture copilot code mode opt-out event",
            workflow_copilot_chat_id=workflow_copilot_chat_id,
            organization_id=organization_id,
            exc_info=True,
        )


async def _resolve_copilot_code_available(
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
) -> bool:
    try:
        has_code_block_access = await app.AGENT_FUNCTION.has_code_block_access(organization_id)
    except Exception:
        LOG.warning("Failed to resolve copilot code block access", organization_id=organization_id, exc_info=True)
        return False
    if not has_code_block_access:
        return False
    if chat_request.code_block is not None:
        return True
    try:
        copilot_config = await app.AGENT_FUNCTION.get_copilot_config_for_request(
            organization_id,
            code_block_mode=None,
        )
    except Exception:
        LOG.warning("Failed to resolve copilot code mode availability", organization_id=organization_id, exc_info=True)
        return False
    return (
        normalize_block_authoring_policy(getattr(copilot_config, "block_authoring_policy", None))
        == BlockAuthoringPolicy.CODE_ONLY_BROWSER
    )


def _with_current_copilot_code_mode_metadata(
    turn_outcome: TurnOutcome | None,
    *,
    effective_mode: CopilotComposerMode,
    code_available: bool,
    turn_id: str | None,
) -> TurnOutcome | None:
    if turn_outcome is None:
        return None
    return with_copilot_code_mode_metadata(
        turn_outcome,
        effective_mode=effective_mode,
        code_available=code_available,
        turn_id=turn_id,
    )


@dataclass(frozen=True)
class RunInfo:
    block_label: str | None
    block_type: str
    block_status: str | None
    failure_reason: str | None
    html: str | None


# New-copilot richer block shape (used only from the ENABLE_WORKFLOW_COPILOT_V2
# dispatch path). Kept side-by-side with the old RunInfo so the old-copilot
# body stays untouched; consolidation is SKY-8916's job.
@dataclass(frozen=True)
class BlockRunInfo:
    block_label: str | None
    block_type: str
    block_status: str | None
    failure_reason: str | None
    output: str | None


COPILOT_CANCEL_TTL = timedelta(minutes=5)
# Polling cadence for the cancel-watcher sidecar. Worst-case latency from a
# user's Stop click to ``handler_task.cancel()`` is one cadence period plus
# the Redis round-trip — well under the 5-minute scenario this feature
# exists to fix, and far below any client-side timeout budget.
COPILOT_CANCEL_POLL_SECONDS = 1.5


def _copilot_cancel_key(organization_id: str, cancel_token: str) -> str:
    return f"copilot_cancel:{organization_id}:{cancel_token}"


async def _watch_for_cancel(
    cache: Any,
    organization_id: str,
    cancel_token: str,
    handler_task: asyncio.Task,
    observed: list[bool],
) -> None:
    """Cancel ``handler_task`` when the matching Redis flag flips truthy.

    Sets ``observed[0] = True`` before issuing the cancel so the handler's
    ``except CancelledError`` block can tell a user cancel apart from
    server shutdown — only the user path writes a ``Cancelled by user.`` row.
    """
    key = _copilot_cancel_key(organization_id, cancel_token)
    while not handler_task.done():
        await asyncio.sleep(COPILOT_CANCEL_POLL_SECONDS)
        try:
            flag = await cache.get(key)
        except Exception:
            LOG.debug("Copilot cancel-watcher get failed; will retry", exc_info=True)
            continue
        if flag:
            LOG.info(
                "Copilot cancel signal observed; cancelling handler task",
                cancel_token=cancel_token,
                organization_id=organization_id,
            )
            observed[0] = True
            handler_task.cancel()
            return


async def _ensure_terminal_frame(
    stream: EventSourceStream,
    already_emitted: bool,
    turn_id: str | None = None,
) -> None:
    """Emit a fallback ERROR frame if the turn hasn't sent a terminal one.

    Shielded so cancellation on the outer scope doesn't abort the send;
    swallows BaseException so a failed cleanup never masks the original.
    """
    if already_emitted:
        return
    try:
        await asyncio.shield(
            stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="The assistant didn't finish this turn. Please try again.",
                    turn_id=turn_id,
                    narrative_summary=None,
                )
            )
        )
    except BaseException:
        pass


def _proposal_disposition(agent_result: object | None) -> ProposalDisposition:
    if agent_result is None:
        return "no_proposal"
    disposition = getattr(agent_result, "proposal_disposition", None)
    if disposition in ("no_proposal", "auto_applicable", "review_untested", "review_tested"):
        return disposition
    if getattr(agent_result, "updated_workflow", None) is None:
        return "no_proposal"
    return "auto_applicable"


def _effective_auto_accept(auto_accept: bool | None, agent_result: object | None) -> bool:
    """Only auto-applicable proposals may honor ``auto_accept=True``."""
    if getattr(agent_result, "cancelled", False) is True or _proposal_disposition(agent_result) != "auto_applicable":
        return False
    return auto_accept is True or getattr(agent_result, "apply_without_review", False) is True


def _should_restore_persisted_workflow(auto_accept: bool | None, agent_result: object | None) -> bool:
    """Restore when a mid-turn canonical write isn't covered by an accepted proposal."""
    legacy_persisted = bool(getattr(agent_result, "workflow_was_persisted", False))
    degraded_persisted = bool(getattr(agent_result, "canonical_was_persisted_due_to_param_change", False))
    if not (legacy_persisted or degraded_persisted):
        return False
    if getattr(agent_result, "updated_workflow", None) is None:
        return True
    return not _effective_auto_accept(auto_accept, agent_result)


def _should_commit_staged_workflow(auto_accept: bool | None, agent_result: object | None) -> bool:
    """Auto-accept commits the final staged workflow even after a mid-turn degraded
    write: a later blocks-only edit stages without persisting, so only this terminal
    commit reconciles canonical with the proposal the user sees."""
    if not _effective_auto_accept(auto_accept, agent_result):
        return False
    return bool(getattr(agent_result, "has_staged_proposal", False))


def _record_recoverable_failure_span_attrs(
    failure: RecoverableFailure,
    *,
    proposal_disposition: ProposalDisposition,
) -> None:
    current_span = otel_trace.get_current_span()
    current_span.set_attribute("copilot.error_recovered", True)
    current_span.set_attribute("copilot.error_failure_kind", failure.failure_kind)
    current_span.set_attribute("copilot.error_id", failure.internal_error_id)
    if failure.exception_type:
        current_span.set_attribute("copilot.error_exception_type", failure.exception_type)
    current_span.set_attribute("copilot.error_workflow_modified", failure.workflow_modified)
    current_span.set_attribute("copilot.error_reply_proposal_disposition", proposal_disposition)


def _make_error_narrative_payload(turn_id: str | None, turn_index: int | None, message: str) -> TurnNarrativePayload:
    return {
        "turnId": turn_id,
        "turnIndex": turn_index if turn_index is not None else 0,
        "mode": "unknown",
        "designStarted": False,
        "designEnded": True,
        "draft": None,
        "blocks": [],
        "terminal": "error",
        "terminalMessage": message,
        "narrativeSummary": message,
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }


def _with_terminal_narrative_metadata(
    narrative_payload: TurnNarrativePayload | None,
    *,
    cancelled: bool,
    proposal_disposition: ProposalDisposition,
) -> TurnNarrativePayload | None:
    if narrative_payload is None:
        return None
    return {
        **narrative_payload,
        "cancelled": cancelled,
        "proposalDisposition": proposal_disposition,
    }


def _build_recoverable_route_agent_result(
    error: BaseException,
    *,
    workflow_modified: bool,
    clear_proposed_workflow: bool,
    global_llm_context: str | None,
    turn_id: str | None = None,
    turn_index: int | None = None,
) -> tuple[AgentResult, RecoverableFailure]:
    failure = build_recoverable_failure(error, workflow_modified=workflow_modified)
    user_response = format_recoverable_failure_reply(failure)
    agent_result = AgentResult(
        user_response=user_response,
        updated_workflow=None,
        global_llm_context=merge_failure_into_context(global_llm_context, failure),
        workflow_was_persisted=False,
        proposal_disposition="no_proposal",
        clear_proposed_workflow=clear_proposed_workflow,
        turn_id=turn_id,
        narrative_payload=_make_error_narrative_payload(turn_id, turn_index, user_response),
        turn_outcome=build_minimal_turn_outcome(
            user_response,
            response_kind=ResponseKind.RECOVER,
            reason_code=failure.failure_kind,
            terminal_reason=COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON,
        ),
    )
    _record_recoverable_failure_span_attrs(failure, proposal_disposition="no_proposal")
    return agent_result, failure


async def _clear_proposed_workflow(chat: Any) -> None:
    await app.DATABASE.workflow_params.update_workflow_copilot_chat(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        proposed_workflow=None,
    )


async def _load_completion_criteria_snapshot(chat: Any) -> StoredCriteriaSnapshot | None:
    """None disables the lifecycle for this turn on load failure, which degrades to
    today's per-turn regeneration rather than risking a duplicate-epoch write."""
    try:
        latest = await app.DATABASE.workflow_params.get_latest_workflow_copilot_completion_criteria_set(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        )
    except Exception:
        LOG.warning("copilot completion criteria snapshot load failed", exc_info=True)
        return None
    if latest is None:
        return StoredCriteriaSnapshot()
    active = None
    if latest.status == CRITERIA_SET_STATUS_ACTIVE:
        active = StoredCriteriaSet(
            set_id=latest.completion_criteria_set_id,
            goal_epoch=latest.goal_epoch,
            criteria=criteria_from_json(latest.criteria),
            consecutive_all_no_evidence=latest.consecutive_all_no_evidence,
            tripwire_fired=latest.tripwire_fired,
            last_fully_satisfied_workflow_yaml=latest.last_fully_satisfied_workflow_yaml,
        )
    return StoredCriteriaSnapshot(active=active, next_epoch=latest.goal_epoch + 1)


async def _persist_completion_criteria_state(chat: Any, agent_result: AgentResult, user_message: str) -> None:
    plan = plan_persistence(getattr(agent_result, "completion_criteria_turn_state", None))
    if plan is None:
        return
    try:
        if plan.creates_set:
            new_set = await app.DATABASE.workflow_params.create_workflow_copilot_completion_criteria_set(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                goal_epoch=plan.create_epoch or 1,
                criteria=criteria_to_json(plan.create_criteria),
                source_turn_id=agent_result.turn_id,
                source_goal_text=user_message[:2000] if user_message else None,
                consecutive_all_no_evidence=plan.counter_value,
                last_fully_satisfied_workflow_yaml=plan.fully_satisfied_workflow_yaml,
            )
            if plan.supersede_set_id:
                await app.DATABASE.workflow_params.supersede_workflow_copilot_completion_criteria_set(
                    organization_id=chat.organization_id,
                    completion_criteria_set_id=plan.supersede_set_id,
                    supersede_reason=plan.supersede_reason or "not_subset",
                    superseded_by_set_id=new_set.completion_criteria_set_id,
                )
        else:
            if plan.counter_set_id:
                await app.DATABASE.workflow_params.update_workflow_copilot_completion_criteria_set_state(
                    organization_id=chat.organization_id,
                    completion_criteria_set_id=plan.counter_set_id,
                    consecutive_all_no_evidence=plan.counter_value,
                    tripwire_fired=plan.tripwire_fired,
                    last_fully_satisfied_workflow_yaml=plan.fully_satisfied_workflow_yaml,
                )
            if plan.supersede_set_id:
                await app.DATABASE.workflow_params.supersede_workflow_copilot_completion_criteria_set(
                    organization_id=chat.organization_id,
                    completion_criteria_set_id=plan.supersede_set_id,
                    supersede_reason=plan.supersede_reason or "tripwire",
                )
        LOG.info(
            "copilot completion criteria persisted",
            criteria_set_created=plan.creates_set,
            criteria_epoch=plan.create_epoch,
            criteria_superseded_set_id=plan.supersede_set_id,
            criteria_supersede_reason=plan.supersede_reason,
            criteria_consecutive_all_no_evidence=plan.counter_value,
            criteria_tripwire_fired=plan.tripwire_fired,
        )
    except Exception:
        # A failed persist degrades to per-turn regeneration on the next turn.
        LOG.warning("copilot completion criteria persist failed", exc_info=True)


def _build_proposed_workflow_data(updated_workflow: Workflow, agent_result: AgentResult) -> dict[str, Any]:
    proposed_data = dict(updated_workflow.model_dump(mode="json"))
    if agent_result.workflow_yaml:
        proposed_data["_copilot_yaml"] = agent_result.workflow_yaml
    if _proposal_disposition(agent_result) == "review_untested":
        proposed_data["_copilot_unvalidated"] = True
    return proposed_data


def _output_policy_blocked_final_response(agent_result: AgentResult) -> bool:
    diagnostics = getattr(agent_result, "output_policy_diagnostics", None)
    return isinstance(diagnostics, dict) and diagnostics.get("final_output_policy_allowed") is False


async def _persist_proposed_workflow_state(chat: Any, agent_result: AgentResult, restored: bool) -> None:
    updated_workflow = agent_result.updated_workflow
    auto_accept_effective = _effective_auto_accept(chat.auto_accept, agent_result)
    if not auto_accept_effective and updated_workflow:
        await app.DATABASE.workflow_params.update_workflow_copilot_chat(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            proposed_workflow=_build_proposed_workflow_data(updated_workflow, agent_result),
        )
    elif chat.proposed_workflow is not None and (
        restored
        or agent_result.clear_proposed_workflow
        or (
            getattr(agent_result, "apply_without_review", False) is True
            and auto_accept_effective
            and not _output_policy_blocked_final_response(agent_result)
        )
    ):
        # Null any persisted proposed_workflow the assistant just invalidated
        # so a reload does not resurrect a stale Accept/Reject card. Runs
        # under both auto_accept values — a stale proposal can survive an
        # auto-accept toggle.
        await _clear_proposed_workflow(chat)
    elif (
        # This intentionally checks the raw setting, not
        # ``auto_accept_effective``: no-proposal OutputPolicy blocks are not
        # auto-applicable, but ordinary auto-accept turns still need to clear a
        # stale unvalidated card because the UI has no review panel for it.
        chat.auto_accept is True
        and chat.proposed_workflow is not None
        and chat.proposed_workflow.get("_copilot_unvalidated") is True
        and not _output_policy_blocked_final_response(agent_result)
    ):
        # The leftover unvalidated proposal is no longer attached to the chat
        # tail; clear it so reload doesn't resurrect a stale Accept/Reject card.
        await _clear_proposed_workflow(chat)


async def _persist_cancel_turn(
    stream: EventSourceStream,
    chat: Any,
    organization_id: str,
    original_workflow: Workflow | None,
    user_message: str,
    agent_result: AgentResult | None,
    audio_artifact_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Persist a cancelled turn and emit a terminal SSE response frame.

    Pass the agent's ``AgentResult`` for cancels during the agent run so
    rollback uses the same ``workflow_was_persisted`` source of truth as
    the success path; pass ``None`` for pre-agent cancels.
    """
    turn_outcome: TurnOutcome | None
    if agent_result is None:
        user_response = "Cancelled by user."
        updated_workflow = None
        updated_global_llm_context = None
        total_tokens = None
        response_type = "REPLY"
        output_policy_diagnostics = None
        turn_outcome = None
        response_turn_id = turn_id
        narrative_summary = None
        narrative_payload = None
        if chat.proposed_workflow is not None:
            await asyncio.shield(_clear_proposed_workflow(chat))
    else:
        restored = _should_restore_persisted_workflow(chat.auto_accept, agent_result)
        if restored:
            try:
                await asyncio.shield(_restore_workflow_definition(original_workflow, organization_id))
            except Exception:
                LOG.warning(
                    "Workflow restore failed inside cancel-turn handler",
                    organization_id=organization_id,
                    exc_info=True,
                )
        if agent_result.updated_workflow is None and chat.proposed_workflow is not None:
            await asyncio.shield(_clear_proposed_workflow(chat))
        else:
            await asyncio.shield(_persist_proposed_workflow_state(chat, agent_result, restored))
        user_response = agent_result.user_response
        updated_workflow = agent_result.updated_workflow
        updated_global_llm_context = agent_result.global_llm_context
        total_tokens = agent_result.total_tokens
        response_type = agent_result.response_type
        output_policy_diagnostics = agent_result.output_policy_diagnostics
        turn_outcome = agent_result.turn_outcome
        response_turn_id = turn_id or agent_result.turn_id
        narrative_summary = agent_result.narrative_summary
        narrative_payload = agent_result.narrative_payload

    proposal_disposition = _proposal_disposition(agent_result)
    narrative_payload = _with_terminal_narrative_metadata(
        narrative_payload,
        cancelled=True,
        proposal_disposition=proposal_disposition,
    )

    await asyncio.shield(
        app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            sender=WorkflowCopilotChatSender.USER,
            content=user_message,
            audio_artifact_id=audio_artifact_id,
        )
    )
    assistant_message = await asyncio.shield(
        app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            sender=WorkflowCopilotChatSender.AI,
            content=user_response,
            global_llm_context=updated_global_llm_context,
            turn_outcome=turn_outcome,
            narrative_payload=narrative_payload,
        )
    )
    try:
        await asyncio.shield(
            stream.send(
                WorkflowCopilotStreamResponseUpdate(
                    type=WorkflowCopilotStreamMessageType.RESPONSE,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    message=user_response,
                    updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
                    response_time=assistant_message.created_at,
                    total_tokens=total_tokens,
                    response_type=response_type,
                    proposal_disposition=proposal_disposition,
                    cancelled=True,
                    output_policy_diagnostics=output_policy_diagnostics,
                    turn_id=response_turn_id,
                    narrative_summary=narrative_summary,
                    narrative_payload=narrative_payload,
                )
            )
        )
    except BaseException:
        LOG.warning(
            "Failed to send cancel RESPONSE frame; persistence already committed",
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            exc_info=True,
        )


async def _finalise_normal_turn(
    stream: EventSourceStream,
    chat: Any,
    organization_id: str,
    original_workflow: Workflow | None,
    chat_request: WorkflowCopilotChatRequest,
    agent_result: AgentResult,
) -> None:
    """Atomic post-agent finalisation: rollback, proposal, chat rows, RESPONSE.

    Wrapped by the caller in ``asyncio.shield`` so a late user cancel cannot
    interrupt these writes mid-way and leave chat history with a partial turn
    (e.g. proposed_workflow updated but no AI message persisted).
    """
    user_response = agent_result.user_response
    updated_workflow = agent_result.updated_workflow
    updated_global_llm_context = agent_result.global_llm_context

    # Persist rollback / proposed-workflow state and the chat
    # messages regardless of whether the SSE client is still
    # connected: the user needs to see the reply on reconnect.
    # SKY-8986: client disconnect used to short-circuit this block
    # and leave the chat history without the AI response.
    #
    # SKY-9143: restore runs outside the auto_accept wrapper so
    # auto-accept turns that ended without a viable proposal still
    # roll back a mid-turn _update_workflow write. The Accept/Reject
    # panel state below stays gated on auto_accept — the frontend
    # applies proposals via applyWorkflowUpdate when auto-accept is
    # on.
    restored = _should_restore_persisted_workflow(chat.auto_accept, agent_result)
    if restored:
        try:
            await _restore_workflow_definition(original_workflow, organization_id)
        except Exception:
            LOG.warning("copilot restore failed in _finalise_normal_turn", exc_info=True)

    if _should_commit_staged_workflow(chat.auto_accept, agent_result):
        try:
            await _commit_staged_workflow(
                organization_id=organization_id,
                workflow_id=chat_request.workflow_id,
                staged_workflow=agent_result.staged_workflow,
            )
        except Exception:
            # Undo any mid-turn degraded write so a failed commit fails the turn
            # atomically instead of leaving canonical on a partial intermediate.
            LOG.warning("copilot auto-accept commit failed; rolling back canonical", exc_info=True)
            with contextlib.suppress(Exception):
                await _restore_workflow_definition(original_workflow, organization_id)
            raise

    await _persist_proposed_workflow_state(chat, agent_result, restored)
    await _persist_completion_criteria_state(chat, agent_result, chat_request.message)
    proposal_disposition = _proposal_disposition(agent_result)
    workflow_applied = _effective_auto_accept(chat.auto_accept, agent_result)
    narrative_payload = _with_terminal_narrative_metadata(
        agent_result.narrative_payload,
        cancelled=False,
        proposal_disposition=proposal_disposition,
    )

    await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        sender=WorkflowCopilotChatSender.USER,
        content=chat_request.message,
        audio_artifact_id=chat_request.audio_artifact_id,
    )

    assistant_message = await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        sender=WorkflowCopilotChatSender.AI,
        content=user_response,
        global_llm_context=updated_global_llm_context,
        turn_outcome=agent_result.turn_outcome,
        narrative_payload=narrative_payload,
    )

    await stream.send(
        WorkflowCopilotStreamResponseUpdate(
            type=WorkflowCopilotStreamMessageType.RESPONSE,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            message=user_response,
            updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
            response_time=assistant_message.created_at,
            total_tokens=agent_result.total_tokens,
            response_type=agent_result.response_type,
            proposal_disposition=proposal_disposition,
            workflow_applied=workflow_applied,
            output_policy_diagnostics=agent_result.output_policy_diagnostics,
            turn_id=agent_result.turn_id,
            narrative_summary=agent_result.narrative_summary,
            narrative_payload=narrative_payload,
        )
    )


async def _commit_staged_workflow(
    *,
    organization_id: str,
    workflow_id: str,
    staged_workflow: Workflow | None,
) -> None:
    """Overwrite the current workflow version in place (auto-accept path).

    Manual Accept via /workflow/copilot/apply-proposed-workflow creates a new
    version instead. Field list must stay in lockstep with ``_update_workflow``.
    """
    if staged_workflow is None:
        return
    created_by_stamp = await resolve_copilot_created_by_stamp(workflow_id, organization_id)
    await app.WORKFLOW_SERVICE.update_workflow_definition(
        workflow_id=workflow_id,
        organization_id=organization_id,
        title=staged_workflow.title,
        description=staged_workflow.description,
        workflow_definition=staged_workflow.workflow_definition,
        proxy_location=staged_workflow.proxy_location,
        webhook_callback_url=staged_workflow.webhook_callback_url,
        totp_verification_url=staged_workflow.totp_verification_url,
        totp_identifier=staged_workflow.totp_identifier,
        persist_browser_session=staged_workflow.persist_browser_session,
        browser_profile_id=staged_workflow.browser_profile_id,
        browser_profile_key=staged_workflow.browser_profile_key,
        model=staged_workflow.model,
        max_screenshot_scrolling_times=staged_workflow.max_screenshot_scrolls,
        extra_http_headers=staged_workflow.extra_http_headers,
        cdp_connect_headers=staged_workflow.cdp_connect_headers,
        run_with=staged_workflow.run_with,
        ai_fallback=staged_workflow.ai_fallback,
        cache_key=staged_workflow.cache_key,
        adaptive_caching=staged_workflow.adaptive_caching,
        code_version=staged_workflow.code_version,
        run_sequentially=staged_workflow.run_sequentially,
        sequential_key=staged_workflow.sequential_key,
        created_by=created_by_stamp,
        edited_by="copilot",
    )


async def _restore_workflow_definition(original_workflow: Workflow | None, organization_id: str) -> None:
    """Roll the workflow back to ``original_workflow``.

    Field list must stay in lockstep with ``_update_workflow``. May raise; callers
    treat a restore failure as best-effort (log and continue), not a hard error.
    """
    if not original_workflow:
        return
    await app.WORKFLOW_SERVICE.update_workflow_definition(
        workflow_id=original_workflow.workflow_id,
        organization_id=organization_id,
        title=original_workflow.title,
        description=original_workflow.description,
        workflow_definition=original_workflow.workflow_definition,
        proxy_location=original_workflow.proxy_location,
        webhook_callback_url=original_workflow.webhook_callback_url,
        totp_verification_url=original_workflow.totp_verification_url,
        totp_identifier=original_workflow.totp_identifier,
        persist_browser_session=original_workflow.persist_browser_session,
        browser_profile_id=original_workflow.browser_profile_id,
        browser_profile_key=original_workflow.browser_profile_key,
        model=original_workflow.model,
        max_screenshot_scrolling_times=original_workflow.max_screenshot_scrolls,
        extra_http_headers=original_workflow.extra_http_headers,
        cdp_connect_headers=original_workflow.cdp_connect_headers,
        run_with=original_workflow.run_with,
        ai_fallback=original_workflow.ai_fallback,
        cache_key=original_workflow.cache_key,
        adaptive_caching=original_workflow.adaptive_caching,
        code_version=original_workflow.code_version,
        run_sequentially=original_workflow.run_sequentially,
        sequential_key=original_workflow.sequential_key,
        created_by=original_workflow.created_by,
        edited_by=original_workflow.edited_by,
    )


async def _get_debug_artifact(organization_id: str, workflow_run_id: str) -> Artifact | None:
    artifacts = await app.DATABASE.artifacts.get_artifacts_for_run(
        run_id=workflow_run_id, organization_id=organization_id, artifact_types=[ArtifactType.VISIBLE_ELEMENTS_TREE]
    )
    return artifacts[0] if isinstance(artifacts, list) and artifacts else None


async def _get_debug_run_info(organization_id: str, workflow_run_id: str | None) -> RunInfo | None:
    if not workflow_run_id:
        return None

    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    if not blocks:
        return None

    block = blocks[0]

    artifact = await _get_debug_artifact(organization_id, workflow_run_id)
    if artifact:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        html = artifact_bytes.decode("utf-8") if artifact_bytes else None
    else:
        html = None

    return RunInfo(
        block_label=block.label,
        block_type=block.block_type.name,
        block_status=block.status,
        failure_reason=block.failure_reason,
        html=html,
    )


async def _get_new_copilot_block_infos(
    organization_id: str, workflow_run_id: str | None
) -> tuple[list[BlockRunInfo], str | None]:
    """Variant of _get_debug_run_info used by the ENABLE_WORKFLOW_COPILOT_V2 path.

    Returns a list of per-block records plus the run's VISIBLE_ELEMENTS_TREE
    HTML artifact. Coexists with _get_debug_run_info which returns the
    simpler single-block shape used by the old-copilot path.
    """
    if not workflow_run_id:
        return [], None

    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    if not blocks:
        return [], None

    block_infos: list[BlockRunInfo] = []
    for block in blocks:
        block_type_name = block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type)
        block_infos.append(
            BlockRunInfo(
                block_label=block.label,
                block_type=block_type_name,
                block_status=block.status,
                failure_reason=block.failure_reason,
                output=truncate_output(getattr(block, "output", None)),
            )
        )

    artifact = await _get_debug_artifact(organization_id, workflow_run_id)
    html: str | None = None
    if artifact:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        html = artifact_bytes.decode("utf-8") if artifact_bytes else None

    return block_infos, html


def _format_chat_history(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> str:
    chat_history_text = ""
    if chat_history:
        history_lines = [f"{msg.sender}: {msg.content}" for msg in chat_history]
        chat_history_text = "\n".join(history_lines)
    return chat_history_text


def _parse_llm_response(llm_response: dict[str, Any] | Any) -> Any:
    if isinstance(llm_response, dict) and "output" in llm_response:
        action_data = llm_response["output"]
    else:
        action_data = llm_response

    if not isinstance(action_data, dict):
        LOG.error(
            "LLM response is not valid JSON",
            response_type=type(action_data).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from LLM",
        )
    return action_data


async def copilot_call_llm(
    stream: EventSourceStream,
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
) -> tuple[str, Workflow | None, str | None, str | None]:
    """Returns (user_response, updated_workflow, global_llm_context, workflow_yaml).

    workflow_yaml is the raw YAML used to build updated_workflow — callers stash
    it on the persisted proposal so /apply-proposed-workflow can re-create the
    workflow version. Without it the V1 proposal can't be applied (SKY-9206).
    """
    chat_history_text = _format_chat_history(chat_history)

    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")

    # Render system prompt (trusted content only, security rules injected via AgentFunction)
    security_rules = app.AGENT_FUNCTION.get_copilot_security_rules()
    system_prompt = prompt_engine.load_prompt(
        template="workflow-copilot-system",
        workflow_knowledge_base=workflow_knowledge_base,
        current_datetime=datetime.now(timezone.utc).isoformat(),
        security_rules=security_rules,
    )

    # Render user prompt (untrusted content, each variable in code fences)
    # Escape triple backticks to prevent code fence breakout
    user_prompt = prompt_engine.load_prompt(
        template="workflow-copilot-user",
        workflow_yaml=escape_code_fences(chat_request.workflow_yaml or ""),
        user_message=escape_code_fences(chat_request.message),
        chat_history=escape_code_fences(chat_history_text),
        global_llm_context=escape_code_fences(global_llm_context or ""),
        debug_run_info=escape_code_fences(debug_run_info_text),
    )

    LOG.info(
        "Calling LLM",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_id=chat_request.workflow_id,
        user_message_len=len(chat_request.message),
        user_message=chat_request.message,
        workflow_yaml_len=len(chat_request.workflow_yaml or ""),
        workflow_yaml=chat_request.workflow_yaml or "",
        chat_history_len=len(chat_history_text),
        chat_history=chat_history_text,
        global_llm_context_len=len(global_llm_context or ""),
        global_llm_context=global_llm_context or "",
        workflow_knowledge_base_len=len(workflow_knowledge_base),
        debug_run_info_len=len(debug_run_info_text),
        system_prompt_len=len(system_prompt),
        user_prompt_len=len(user_prompt),
    )
    llm_api_handler = await _resolve_copilot_agent_handler(chat_request.workflow_permanent_id, organization_id)
    llm_start_time = time.monotonic()
    llm_response = await llm_api_handler(
        prompt=user_prompt,
        prompt_name="workflow-copilot",
        organization_id=organization_id,
        system_prompt=system_prompt,
    )
    LOG.info(
        "LLM response",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_id=chat_request.workflow_id,
        duration_seconds=time.monotonic() - llm_start_time,
        user_message_len=len(chat_request.message),
        workflow_yaml_len=len(chat_request.workflow_yaml or ""),
        chat_history_len=len(chat_history_text),
        global_llm_context_len=len(global_llm_context or ""),
        debug_run_info_len=len(debug_run_info_text),
        workflow_knowledge_base_len=len(workflow_knowledge_base),
        llm_response_len=len(llm_response),
        llm_response=llm_response,
    )

    action_data = _parse_llm_response(llm_response)

    action_type = action_data.get("type")
    user_response_value = action_data.get("user_response")
    if user_response_value is None:
        user_response = "I received your request but I'm not sure how to help. Could you rephrase?"
    else:
        user_response = str(user_response_value)
    LOG.info(
        "LLM response received",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_id=chat_request.workflow_id,
        organization_id=organization_id,
        action_type=action_type,
    )

    global_llm_context = action_data.get("global_llm_context")
    if global_llm_context is not None:
        global_llm_context = str(global_llm_context)

    if action_type == "REPLACE_WORKFLOW":
        llm_workflow_yaml = default_data_write_continue_on_failure(
            action_data.get("workflow_yaml", ""), chat_request.workflow_yaml
        )
        applied_workflow_yaml = llm_workflow_yaml
        try:
            updated_workflow = _process_workflow_yaml(
                workflow_id=chat_request.workflow_id,
                workflow_permanent_id=chat_request.workflow_permanent_id,
                organization_id=organization_id,
                workflow_yaml=llm_workflow_yaml,
            )
        except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Validating workflow definition...",
                    timestamp=datetime.now(timezone.utc),
                )
            )
            corrected_workflow_yaml = default_data_write_continue_on_failure(
                await _auto_correct_workflow_yaml(
                    llm_api_handler=llm_api_handler,
                    organization_id=organization_id,
                    user_response=user_response,
                    workflow_yaml=llm_workflow_yaml,
                    chat_history=chat_history,
                    global_llm_context=global_llm_context,
                    debug_run_info_text=debug_run_info_text,
                    error=e,
                ),
                chat_request.workflow_yaml,
            )
            updated_workflow = _process_workflow_yaml(
                workflow_id=chat_request.workflow_id,
                workflow_permanent_id=chat_request.workflow_permanent_id,
                organization_id=organization_id,
                workflow_yaml=corrected_workflow_yaml,
            )
            applied_workflow_yaml = corrected_workflow_yaml

        return user_response, updated_workflow, global_llm_context, applied_workflow_yaml
    elif action_type == "REPLY":
        return user_response, None, global_llm_context, None
    elif action_type == "ASK_QUESTION":
        return user_response, None, global_llm_context, None
    else:
        LOG.error(
            "Unknown action type from LLM",
            organization_id=organization_id,
            action_type=action_type,
        )
        return "I received your request but I'm not sure how to help. Could you rephrase?", None, None, None


async def _auto_correct_workflow_yaml(
    llm_api_handler: LLMAPIHandler,
    organization_id: str,
    user_response: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
    error: Exception,
) -> str:
    failure_reason = f"{error.__class__.__name__}: {error}"

    new_chat_history = chat_history[:]
    new_chat_history.append(
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.AI,
            content=user_response,
            created_at=datetime.now(timezone.utc),
        )
    )

    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")

    security_rules = app.AGENT_FUNCTION.get_copilot_security_rules()
    system_prompt = prompt_engine.load_prompt(
        template="workflow-copilot-system",
        workflow_knowledge_base=workflow_knowledge_base,
        current_datetime=datetime.now(timezone.utc).isoformat(),
        security_rules=security_rules,
    )

    user_prompt = prompt_engine.load_prompt(
        template="workflow-copilot-user",
        workflow_yaml=escape_code_fences(workflow_yaml),
        user_message=escape_code_fences(f"Workflow YAML parsing failed, please fix it: {failure_reason}"),
        chat_history=escape_code_fences(_format_chat_history(new_chat_history)),
        global_llm_context=escape_code_fences(global_llm_context or ""),
        debug_run_info=escape_code_fences(debug_run_info_text),
    )

    llm_start_time = time.monotonic()
    llm_response = await llm_api_handler(
        prompt=user_prompt,
        prompt_name="workflow-copilot",
        organization_id=organization_id,
        system_prompt=system_prompt,
    )
    LOG.info(
        "Auto-correction LLM response",
        duration_seconds=time.monotonic() - llm_start_time,
        llm_response_len=len(llm_response),
        llm_response=llm_response,
    )
    action_data = _parse_llm_response(llm_response)

    return action_data.get("workflow_yaml", workflow_yaml)


def _collect_reachable(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
    reachable: set[str],
) -> None:
    """Walk the next_block_label chain from start_label, collecting all reachable labels.

    For conditional blocks, also follows branch target chains recursively.

    The ``current not in reachable`` loop guard means the main-chain walk
    stops early if we hit a node already collected via a branch recursion.
    This is correct — those downstream nodes and their successors are
    already in ``reachable`` — but callers should be aware of the coupling.
    """
    current: str | None = start_label
    while current and current in label_to_block and current not in reachable:
        reachable.add(current)
        block = label_to_block[current]
        if isinstance(block, ConditionalBlockYAML):
            for branch in block.branch_conditions:
                if branch.next_block_label and branch.next_block_label not in reachable:
                    _collect_reachable(branch.next_block_label, label_to_block, reachable)
        current = block.next_block_label


def _break_cycles(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
) -> bool:
    """Detect and break circular references in the block chain using DFS.

    Uses a recursion stack to distinguish true back-edges (cycles) from merge
    points (two branches converging on the same block).  When a back-edge is
    found the offending ``next_block_label`` is set to ``None``, breaking the
    cycle.  Handles both the main chain and conditional branch chains.

    Note: this function operates on a single level of blocks.  It does **not**
    recurse into ``ForLoopBlockYAML.loop_blocks``; nested loops are handled
    by the recursive ``_repair_next_block_label_chain`` call in Phase 3.

    Returns True if at least one cycle was broken.
    """
    visited: set[str] = set()
    rec_stack: set[str] = set()
    found_cycle = False

    def _follow_edge(target: str | None, edge_owner: BlockYAML | BranchConditionYAML, parent_label: str) -> None:
        """Follow an edge to *target*.  *edge_owner* is the object whose
        ``next_block_label`` will be set to ``None`` when the target forms a
        back-edge.  *parent_label* is the block label that owns this edge
        for logging."""
        nonlocal found_cycle
        if not target or target not in label_to_block:
            return
        if target in rec_stack:
            is_branch = hasattr(edge_owner, "criteria")
            LOG.warning(
                "Copilot produced circular block chain, breaking cycle",
                cycle_target=target,
                broken_at=parent_label,
                is_branch_condition=is_branch,
                branch_expression=getattr(getattr(edge_owner, "criteria", None), "expression", None),
            )
            edge_owner.next_block_label = None
            found_cycle = True
            return
        if target in visited:
            return  # merge point — not a cycle
        _dfs(target)

    def _dfs(label: str) -> None:
        visited.add(label)
        rec_stack.add(label)
        block = label_to_block[label]

        if isinstance(block, ConditionalBlockYAML):
            for branch in block.branch_conditions:
                _follow_edge(branch.next_block_label, branch, label)

        _follow_edge(block.next_block_label, block, label)
        rec_stack.discard(label)

    if start_label in label_to_block:
        _dfs(start_label)
    return found_cycle


def _find_terminal_label(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
    all_labels: set[str],
) -> str | None:
    """Find the terminal block by walking the main chain from start_label."""
    visited: set[str] = set()
    current: str | None = start_label
    while current and current in label_to_block and current not in visited:
        visited.add(current)
        block = label_to_block[current]
        if block.next_block_label is None or block.next_block_label not in all_labels:
            return current
        current = block.next_block_label
    return None


def _order_orphaned_blocks(
    orphaned_labels: set[str],
    label_to_block: dict[str, BlockYAML],
    all_labels: set[str],
    blocks: list[BlockYAML],
) -> list[str]:
    """Order orphaned blocks by following their internal next_block_label chains.

    Multiple disconnected orphan sub-chains are concatenated in the order their
    chain-start appears in the original blocks list.
    """
    pointed_to: set[str] = set()
    for label in orphaned_labels:
        block = label_to_block[label]
        if block.next_block_label and block.next_block_label in orphaned_labels:
            pointed_to.add(block.next_block_label)

    # Chain starts are orphans not pointed to by another orphan.
    # Preserve original array order for deterministic stitching.
    chain_starts = [b.label for b in blocks if b.label in orphaned_labels and b.label not in pointed_to]

    # If all orphans point to each other (cycle), pick the first in array order.
    if not chain_starts:
        chain_starts = [next(b.label for b in blocks if b.label in orphaned_labels)]

    ordered: list[str] = []
    visited: set[str] = set()
    for start in chain_starts:
        current: str | None = start
        while current and current in orphaned_labels and current not in visited:
            visited.add(current)
            ordered.append(current)
            current = label_to_block[current].next_block_label

    # Append any remaining orphans not reached (multiple cycles).
    for block in blocks:
        if block.label in orphaned_labels and block.label not in visited:
            ordered.append(block.label)

    # Re-link the orphan chain so it forms a single connected path.
    # This may overwrite an orphan's original next_block_label that pointed to a
    # reachable block (a merge/join pattern).  Log when this happens.
    for i in range(len(ordered) - 1):
        old_target = label_to_block[ordered[i]].next_block_label
        new_target = ordered[i + 1]
        if old_target and old_target != new_target and old_target not in orphaned_labels:
            LOG.info(
                "Orphan re-link overwrites cross-chain reference",
                block=ordered[i],
                old_target=old_target,
                new_target=new_target,
            )
        label_to_block[ordered[i]].next_block_label = new_target
    if ordered:
        old_last_target = label_to_block[ordered[-1]].next_block_label
        if old_last_target and old_last_target not in orphaned_labels:
            LOG.info(
                "Orphan chain terminal overwrites cross-chain reference",
                block=ordered[-1],
                old_target=old_last_target,
            )
        label_to_block[ordered[-1]].next_block_label = None

    return ordered


def _repair_next_block_label_chain(blocks: list[BlockYAML]) -> None:
    """Ensure all top-level blocks form a single acyclic chain from blocks[0].

    Repairs two classes of LLM mistakes:
    1. Circular references — breaks cycles so the chain has a proper terminal block.
    2. Disconnected paths — stitches orphaned blocks onto the end of the reachable chain.

    Recursively repairs nested loop block ``loop_blocks`` at all depths.
    Mutates *blocks* in place.
    """
    if len(blocks) <= 1:
        # Still recurse into loop_blocks even for single-block lists
        for block in blocks:
            if isinstance(block, (ForLoopBlockYAML, WhileLoopBlockYAML)) and block.loop_blocks:
                _repair_next_block_label_chain(block.loop_blocks)
        return

    # Warn on duplicate labels — the dict comprehension silently keeps the last
    # occurrence, so earlier blocks with the same label become invisible.
    seen_labels: set[str] = set()
    for block in blocks:
        if block.label in seen_labels:
            LOG.warning("Copilot produced duplicate block label", label=block.label)
        seen_labels.add(block.label)

    label_to_block: dict[str, BlockYAML] = {block.label: block for block in blocks}
    all_labels = set(label_to_block.keys())

    # Phase 1: break any circular references reachable from the first block.
    # Note: cycles among orphaned blocks (unreachable from blocks[0]) are handled
    # implicitly by _order_orphaned_blocks via its visited set and re-linking logic.
    _break_cycles(blocks[0].label, label_to_block)

    # Phase 2: find orphaned (unreachable) blocks and stitch them to the end.
    reachable: set[str] = set()
    _collect_reachable(blocks[0].label, label_to_block, reachable)

    orphaned_labels = all_labels - reachable
    if orphaned_labels:
        LOG.warning(
            "Copilot produced disconnected workflow blocks, repairing chain",
            orphaned_labels=sorted(orphaned_labels),
            reachable_labels=sorted(reachable),
        )

        terminal_label = _find_terminal_label(blocks[0].label, label_to_block, all_labels)
        ordered_orphan_labels = _order_orphaned_blocks(orphaned_labels, label_to_block, all_labels, blocks)

        if terminal_label and ordered_orphan_labels:
            label_to_block[terminal_label].next_block_label = ordered_orphan_labels[0]

    # Phase 3: recursively repair nested loop block ``loop_blocks``.
    for block in blocks:
        if isinstance(block, (ForLoopBlockYAML, WhileLoopBlockYAML)) and block.loop_blocks:
            _repair_next_block_label_chain(block.loop_blocks)


def _normalize_copilot_yaml(workflow_yaml: str) -> WorkflowCreateYAMLRequest:
    parsed_yaml = safe_load_no_dates(workflow_yaml)

    # Fixing trivial common LLM mistakes; non-dict YAML falls through to model_validate.
    if isinstance(parsed_yaml, dict):
        # title is schema-required; coerce rather than force a self-healing round-trip.
        parsed_yaml.setdefault("title", "")
        _canonicalize_copilot_proxy_location(parsed_yaml)
        workflow_definition = parsed_yaml.get("workflow_definition", None)
        if workflow_definition:
            _canonicalize_copilot_block_type_aliases(workflow_definition)
            blocks = workflow_definition.get("blocks", []) or []
            for block in blocks:
                block["title"] = block.get("title", "")

    workflow_yaml_request = WorkflowCreateYAMLRequest.model_validate(parsed_yaml)

    # Post-processing
    for block in workflow_yaml_request.workflow_definition.blocks:
        if isinstance(block, LoginBlockYAML) and not block.navigation_goal:
            block.navigation_goal = DEFAULT_LOGIN_PROMPT

    workflow_yaml_request.workflow_definition.parameters = [
        p for p in workflow_yaml_request.workflow_definition.parameters if p.parameter_type != ParameterType.OUTPUT
    ]

    _repair_next_block_label_chain(workflow_yaml_request.workflow_definition.blocks)

    return workflow_yaml_request


def _process_workflow_yaml(
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    workflow_yaml: str,
) -> Workflow:
    # Single seam every copilot YAML->Workflow conversion passes through, so code
    # blocks get their plain-view steps regardless of which path produced the YAML
    # (the update_workflow tool derives them upstream; the inline REPLACE_WORKFLOW
    # fallbacks would otherwise surface "No steps yet").
    workflow_yaml = derive_code_block_steps_in_yaml(workflow_yaml)
    workflow_yaml_request = _normalize_copilot_yaml(workflow_yaml)

    updated_workflow_definition = convert_workflow_definition(
        workflow_definition_yaml=workflow_yaml_request.workflow_definition,
        workflow_id=workflow_id,
    )

    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id=organization_id,
        title=workflow_yaml_request.title or "",
        workflow_permanent_id=workflow_permanent_id,
        version=1,
        is_saved_task=workflow_yaml_request.is_saved_task,
        description=workflow_yaml_request.description,
        workflow_definition=updated_workflow_definition,
        proxy_location=workflow_yaml_request.proxy_location,
        webhook_callback_url=workflow_yaml_request.webhook_callback_url,
        totp_verification_url=workflow_yaml_request.totp_verification_url,
        totp_identifier=workflow_yaml_request.totp_identifier,
        persist_browser_session=workflow_yaml_request.persist_browser_session or False,
        browser_profile_id=workflow_yaml_request.browser_profile_id,
        browser_profile_key=workflow_yaml_request.browser_profile_key,
        model=workflow_yaml_request.model,
        max_screenshot_scrolls=workflow_yaml_request.max_screenshot_scrolls,
        extra_http_headers=workflow_yaml_request.extra_http_headers,
        cdp_connect_headers=workflow_yaml_request.cdp_connect_headers,
        run_with=workflow_yaml_request.run_with,
        ai_fallback=workflow_yaml_request.ai_fallback,
        cache_key=workflow_yaml_request.cache_key,
        adaptive_caching=workflow_yaml_request.adaptive_caching,
        code_version=workflow_yaml_request.code_version,
        run_sequentially=workflow_yaml_request.run_sequentially,
        sequential_key=workflow_yaml_request.sequential_key,
        created_at=now,
        modified_at=now,
    )


def _blockless_submission_fallback(
    *,
    proposed_workflow: dict[str, Any] | None,
    submitted_workflow_yaml: str | None,
) -> str | None:
    """Return a hydration YAML when the frontend submitted nothing usable. Only
    fires for truly empty submissions (``None`` or empty string); a non-empty
    YAML with ``blocks: []`` is treated as an explicit user deletion."""
    if submitted_workflow_yaml is not None and submitted_workflow_yaml.strip() != "":
        return None
    if not isinstance(proposed_workflow, dict):
        return None
    candidate = proposed_workflow.get("_copilot_yaml")
    if not isinstance(candidate, str) or _workflow_yaml_block_count(candidate) == 0:
        return None
    return candidate


def _prior_copilot_workflow_yaml(
    *,
    proposed_workflow: dict[str, Any] | None,
    persisted_workflow_yaml: str | None,
) -> str | None:
    """Return the YAML the copilot last saw — the basis for the user-modified
    diff. Preference: the persisted proposal (`_copilot_yaml`) → the on-disk
    workflow. Returns ``None`` only when neither carries usable blocks."""
    if isinstance(proposed_workflow, dict):
        candidate = proposed_workflow.get("_copilot_yaml")
        if isinstance(candidate, str) and _workflow_yaml_block_count(candidate) > 0:
            return candidate
    if persisted_workflow_yaml and _workflow_yaml_block_count(persisted_workflow_yaml) > 0:
        return persisted_workflow_yaml
    return None


def _workflow_yaml_block_count(workflow_yaml: str | None) -> int:
    if not workflow_yaml:
        return 0
    try:
        parsed_yaml = safe_load_no_dates(workflow_yaml)
    except Exception:
        return 0
    if not isinstance(parsed_yaml, dict):
        return 0

    workflow_definition = parsed_yaml.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return 0
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list):
        return 0
    return len(blocks)


def _strip_runtime_block_fields(block: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(block)
    cleaned.pop("output_parameter", None)
    cleaned.pop("workflow_system_prompt", None)

    parameters = cleaned.pop("parameters", None)
    if isinstance(parameters, list) and "parameter_keys" not in cleaned:
        parameter_keys = [
            parameter.get("key")
            for parameter in parameters
            if isinstance(parameter, dict)
            and parameter.get("key")
            and parameter.get("parameter_type") != ParameterType.OUTPUT.value
        ]
        if parameter_keys:
            cleaned["parameter_keys"] = parameter_keys

    loop_over = cleaned.pop("loop_over", None)
    if isinstance(loop_over, dict) and "loop_over_parameter_key" not in cleaned:
        loop_over_parameter_key = loop_over.get("key")
        if loop_over_parameter_key:
            cleaned["loop_over_parameter_key"] = loop_over_parameter_key

    loop_blocks = cleaned.get("loop_blocks")
    if isinstance(loop_blocks, list):
        cleaned["loop_blocks"] = [
            _strip_runtime_block_fields(loop_block) if isinstance(loop_block, dict) else loop_block
            for loop_block in loop_blocks
        ]
    return cleaned


def _workflow_to_copilot_yaml(workflow: Workflow) -> str:
    workflow_data = workflow.model_dump(mode="json", exclude_none=True)
    workflow_definition = deepcopy(workflow_data.get("workflow_definition") or {})

    parameters = workflow_definition.get("parameters")
    if isinstance(parameters, list):
        workflow_definition["parameters"] = [
            parameter
            for parameter in parameters
            if not (isinstance(parameter, dict) and parameter.get("parameter_type") == ParameterType.OUTPUT.value)
        ]

    blocks = workflow_definition.get("blocks")
    if isinstance(blocks, list):
        workflow_definition["blocks"] = [
            _strip_runtime_block_fields(block) if isinstance(block, dict) else block for block in blocks
        ]

    request_data = {
        key: workflow_data[key]
        for key in WorkflowCreateYAMLRequest.model_fields
        if key != "workflow_definition" and key in workflow_data
    }
    request_data["workflow_definition"] = workflow_definition

    try:
        workflow_request = WorkflowCreateYAMLRequest.model_validate(request_data)
        yaml_data = workflow_request.model_dump(mode="json", exclude_none=True)
    except ValidationError:
        LOG.warning(
            "Persisted workflow did not round-trip through copilot YAML schema; using best-effort workflow dump",
            workflow_id=workflow.workflow_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            exc_info=True,
        )
        yaml_data = request_data
    return yaml.safe_dump(yaml_data, sort_keys=False)


def _ensure_copilot_workflow_yaml(
    chat_request: WorkflowCopilotChatRequest,
    original_workflow: Workflow,
    *,
    persisted_workflow_yaml: str | None = None,
) -> None:
    if _workflow_yaml_block_count(chat_request.workflow_yaml) > 0:
        return
    workflow_definition = original_workflow.workflow_definition
    if workflow_definition is None or not workflow_definition.blocks:
        return

    if persisted_workflow_yaml is None:
        persisted_workflow_yaml = _workflow_to_copilot_yaml(original_workflow)
    if not persisted_workflow_yaml:
        return

    LOG.warning(
        "Copilot V2 chat request had no workflow blocks; using persisted workflow YAML",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_id=original_workflow.workflow_id,
        submitted_workflow_yaml_length=len(chat_request.workflow_yaml or ""),
        persisted_workflow_yaml_length=len(persisted_workflow_yaml),
        persisted_block_count=len(workflow_definition.blocks),
    )
    chat_request.workflow_yaml = persisted_workflow_yaml


async def _new_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization,
) -> EventSourceResponse:
    """ENABLE_WORKFLOW_COPILOT_V2 dispatch target.

    Runs the openai-agents-SDK copilot (skyvern.forge.sdk.copilot.agent) and
    streams responses in the same SSE shape the frontend consumes. On
    mid-stream failure (HTTPException, LLMProviderError, asyncio.CancelledError,
    or unexpected exception), rolls the workflow definition back to
    ``original_workflow`` via ``_restore_workflow_definition`` to avoid leaving
    a half-persisted draft.
    """

    async def stream_handler(stream: EventSourceStream) -> None:
        LOG.info(
            "Workflow copilot v2 chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            message=chat_request.message,
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        # Canonical turn_id for the whole HTTP request. Generated before any
        # try-block so route-level error paths and the agent's TURN_START
        # envelope all carry the same identifier.
        turn_id = uuid.uuid4().hex

        original_workflow: Workflow | None = None
        chat = None
        agent_result: AgentResult | None = None
        global_llm_context: str | None = None
        terminal_frame_emitted = False
        cancel_watcher: asyncio.Task[None] | None = None
        current_code_available = False
        effective_mode = _effective_copilot_composer_mode(chat_request, uses_v2=True)
        prior_turn_outcome: TurnOutcome | None = None

        def capture_code_mode_opt_out_after_persist() -> None:
            if chat is None:
                return
            _capture_copilot_code_mode_opt_out(
                prior_turn_outcome=prior_turn_outcome,
                to_mode=effective_mode,
                current_code_available=current_code_available,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                workflow_permanent_id=chat.workflow_permanent_id,
                organization_id=organization.organization_id,
                turn_id=turn_id,
            )

        # Single-element list used as a closure flag (mutable bool by reference).
        # The watcher sets [0] = True before issuing handler_task.cancel() so the
        # except CancelledError block can distinguish a user-driven cancel from
        # operational cancels (server shutdown / deploy drain) and only persist
        # a "Cancelled by user." chat row in the user case.
        user_cancel_observed: list[bool] = [False]

        try:
            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Processing...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            chat = await _get_or_create_workflow_copilot_chat(
                organization_id=organization.organization_id,
                workflow_permanent_id=chat_request.workflow_permanent_id,
                workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            )
            chat_request.workflow_copilot_chat_id = chat.workflow_copilot_chat_id
            chat_request.audio_artifact_id = await _validate_copilot_audio_artifact_id(
                audio_artifact_id=chat_request.audio_artifact_id,
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )

            chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            prior_turn_outcome = _latest_assistant_turn_outcome(chat_messages)
            current_code_available = await _resolve_copilot_code_available(
                organization.organization_id,
                chat_request,
            )
            effective_mode = _effective_copilot_composer_mode(
                chat_request,
                uses_v2=True,
                code_mode_fallback=current_code_available,
            )
            for message in reversed(chat_messages):
                if message.global_llm_context is not None:
                    global_llm_context = message.global_llm_context
                    break

            blockless_fallback = _blockless_submission_fallback(
                proposed_workflow=chat.proposed_workflow,
                submitted_workflow_yaml=chat_request.workflow_yaml,
            )
            if blockless_fallback is not None:
                chat_request.workflow_yaml = blockless_fallback

            block_infos, debug_html = await _get_new_copilot_block_infos(
                organization.organization_id, chat_request.workflow_run_id
            )

            debug_run_info_text = ""
            if block_infos:
                parts: list[str] = []
                for bi in block_infos:
                    block_text = f"Block: {bi.block_label} ({bi.block_type}) — {bi.block_status}"
                    if bi.failure_reason:
                        block_text += f"\n  Failure Reason: {bi.failure_reason}"
                    if bi.output:
                        block_text += f"\n  Output: {bi.output}"
                    parts.append(block_text)
                debug_run_info_text = "\n".join(parts)
                if debug_html:
                    debug_run_info_text += f"\n\nVisible Elements Tree (HTML):\n{debug_html}"

            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Thinking...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            # No early exit on disconnect (SKY-8986): the agent runs to
            # completion even after the SSE stream drops so its reply is
            # persisted to the chat history and visible after reconnect.

            original_workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
                workflow_permanent_id=chat_request.workflow_permanent_id,
                organization_id=organization.organization_id,
            )

            if not original_workflow:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

            chat_request.workflow_id = original_workflow.workflow_id
            persisted_workflow_yaml: str | None = None
            persisted_definition = original_workflow.workflow_definition
            if persisted_definition is not None and persisted_definition.blocks:
                persisted_workflow_yaml = _workflow_to_copilot_yaml(original_workflow)

            _ensure_copilot_workflow_yaml(
                chat_request,
                original_workflow,
                persisted_workflow_yaml=persisted_workflow_yaml,
            )

            prior_copilot_workflow_yaml = _prior_copilot_workflow_yaml(
                proposed_workflow=chat.proposed_workflow,
                persisted_workflow_yaml=persisted_workflow_yaml,
            )

            llm_api_handler = await _resolve_copilot_agent_handler(
                chat_request.workflow_permanent_id, organization.organization_id
            )

            api_key = request.headers.get("x-api-key")
            if not api_key:
                api_key = await app.AGENT_FUNCTION.resolve_org_api_key(organization.organization_id)

            if not api_key:
                LOG.warning(
                    "Copilot V2 cannot resolve an org API token; refusing to start the agent",
                    organization_id=organization.organization_id,
                    workflow_permanent_id=chat.workflow_permanent_id,
                )
                # Mark the terminal frame before sending so a send failure cannot
                # trigger a second terminal frame from the outer exception handler.
                terminal_frame_emitted = True
                await stream.send(
                    WorkflowCopilotStreamErrorUpdate(
                        type=WorkflowCopilotStreamMessageType.ERROR,
                        error="Copilot is not configured for this organization. Contact support.",
                        turn_id=turn_id,
                        narrative_summary=None,
                    )
                )
                return

            copilot_config = (
                await app.AGENT_FUNCTION.get_copilot_config_for_request(
                    organization.organization_id, code_block_mode=chat_request.code_block
                )
            ) or CopilotConfig()

            # Spawn the cancel watcher only after the chat row exists; cancels
            # that land during pre-agent setup are not user-cancellable
            # (setup is short and the watcher needs a chat row to attach
            # any cancellation messages to).
            cache = getattr(app, "CACHE", None)
            if chat_request.cancel_token and cache is not None:
                handler_task = asyncio.current_task()
                if handler_task is not None:
                    cancel_watcher = asyncio.create_task(
                        _watch_for_cancel(
                            cache,
                            organization.organization_id,
                            chat_request.cancel_token,
                            handler_task,
                            user_cancel_observed,
                        )
                    )

            # Zero-based turn ordinal. The current user message has not been
            # appended to chat_messages at this point, so ``sum(...)`` already
            # counts only prior user turns and equals the index of the
            # about-to-start turn.
            turn_index = sum(1 for m in chat_messages if m.sender == WorkflowCopilotChatSender.USER)

            # Prefer the FE-submitted yaml — canonical still has 0 blocks
            # mid-iteration before Accept and would mis-classify the chip.
            prior_block_count: int | None = None
            submitted_count = _workflow_yaml_block_count(chat_request.workflow_yaml)
            if submitted_count > 0:
                prior_block_count = submitted_count
            elif original_workflow is not None and original_workflow.workflow_definition is not None:
                prior_block_count = len(original_workflow.workflow_definition.blocks or [])

            stored_completion_criteria = await _load_completion_criteria_snapshot(chat)

            with bind_copilot_session_id(chat.workflow_copilot_chat_id):
                agent_result = await run_copilot_agent(
                    stream=stream,
                    organization_id=organization.organization_id,
                    chat_request=chat_request,
                    chat_history=convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                    global_llm_context=global_llm_context,
                    debug_run_info_text=debug_run_info_text,
                    llm_api_handler=llm_api_handler,
                    api_key=api_key,
                    config=copilot_config,
                    turn_index=turn_index,
                    turn_id=turn_id,
                    prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
                    prior_block_count=prior_block_count,
                    stored_completion_criteria=stored_completion_criteria,
                )

            agent_result.turn_outcome = _with_current_copilot_code_mode_metadata(
                agent_result.turn_outcome,
                effective_mode=effective_mode,
                code_available=current_code_available,
                turn_id=turn_id,
            )

            if getattr(agent_result, "cancelled", False):
                # The agent absorbed the CancelledError and returned a result
                # carrying ``workflow_was_persisted`` so rollback proceeds normally.
                await _persist_cancel_turn(
                    stream=stream,
                    chat=chat,
                    organization_id=organization.organization_id,
                    original_workflow=original_workflow,
                    user_message=chat_request.message,
                    agent_result=agent_result,
                    audio_artifact_id=chat_request.audio_artifact_id,
                    turn_id=turn_id,
                )
                terminal_frame_emitted = True
                capture_code_mode_opt_out_after_persist()
                LOG.info(
                    "Workflow copilot v2 cancelled by user",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            # Atomic finalisation — a late cancel that fires here cannot tear
            # the success-path writes apart mid-way (no half-written turn,
            # no duplicate user/AI rows).
            await asyncio.shield(
                _finalise_normal_turn(
                    stream=stream,
                    chat=chat,
                    organization_id=organization.organization_id,
                    original_workflow=original_workflow,
                    chat_request=chat_request,
                    agent_result=agent_result,
                )
            )
            terminal_frame_emitted = True
            capture_code_mode_opt_out_after_persist()
        except HTTPException as exc:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                try:
                    await _restore_workflow_definition(original_workflow, organization.organization_id)
                except Exception:
                    LOG.warning(
                        "Workflow restore failed inside HTTPException handler",
                        organization_id=organization.organization_id,
                        exc_info=True,
                    )
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error=exc.detail,
                    turn_id=turn_id,
                    narrative_summary=None,
                )
            )
        except LLMProviderError as exc:
            restored = chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result)
            if restored:
                try:
                    await _restore_workflow_definition(original_workflow, organization.organization_id)
                except Exception:
                    LOG.warning(
                        "Workflow restore failed inside LLMProviderError handler",
                        organization_id=organization.organization_id,
                        exc_info=True,
                    )
            if chat is not None:
                workflow_modified = bool(getattr(agent_result, "workflow_was_persisted", False)) and not restored
                recovered_result, failure = _build_recoverable_route_agent_result(
                    exc,
                    workflow_modified=workflow_modified,
                    clear_proposed_workflow=restored or workflow_modified,
                    global_llm_context=global_llm_context,
                    turn_id=turn_id,
                    turn_index=turn_index,
                )
                recovered_result.turn_outcome = _with_current_copilot_code_mode_metadata(
                    recovered_result.turn_outcome,
                    effective_mode=effective_mode,
                    code_available=current_code_available,
                    turn_id=turn_id,
                )
                LOG.error(
                    "LLM provider error translated to recoverable workflow copilot v2 reply",
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    failure_kind=failure.failure_kind,
                    internal_error_id=failure.internal_error_id,
                    exception_type=failure.exception_type,
                    exc_info=True,
                )
                await asyncio.shield(
                    _finalise_normal_turn(
                        stream=stream,
                        chat=chat,
                        organization_id=organization.organization_id,
                        original_workflow=original_workflow,
                        chat_request=chat_request,
                        agent_result=recovered_result,
                    )
                )
                terminal_frame_emitted = True
                capture_code_mode_opt_out_after_persist()
            else:
                LOG.error(
                    "LLM provider error (copilot v2)",
                    organization_id=organization.organization_id,
                    error=str(exc),
                    exc_info=True,
                )
                terminal_frame_emitted = True
                await stream.send(
                    WorkflowCopilotStreamErrorUpdate(
                        type=WorkflowCopilotStreamMessageType.ERROR,
                        error="Failed to process your request. Please try again.",
                        turn_id=turn_id,
                        narrative_summary=None,
                    )
                )
        except asyncio.CancelledError:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                try:
                    await asyncio.shield(_restore_workflow_definition(original_workflow, organization.organization_id))
                except Exception:
                    LOG.warning(
                        "Workflow restore failed inside cancel-error handler",
                        organization_id=organization.organization_id,
                        exc_info=True,
                    )
            if user_cancel_observed[0] and chat is not None and agent_result is None:
                # User cancel landed before the agent started running, so
                # the agent_result.cancelled branch above couldn't run.
                # _persist_cancel_turn skips rollback when agent_result is None.
                await asyncio.shield(
                    _persist_cancel_turn(
                        stream=stream,
                        chat=chat,
                        organization_id=organization.organization_id,
                        original_workflow=None,
                        user_message=chat_request.message,
                        agent_result=None,
                        audio_artifact_id=chat_request.audio_artifact_id,
                        turn_id=turn_id,
                    )
                )
                terminal_frame_emitted = True
                LOG.info(
                    "Workflow copilot v2 cancelled by user during pre-agent setup",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return
            else:
                # Operational cancel (worker shutdown, deploy drain) or a
                # cancel that arrived after _finalise_normal_turn started
                # its shielded write. Don't manufacture a "Cancelled by
                # user." chat row — chat history should not record an
                # operational cancel as user intent.
                LOG.info(
                    "Workflow copilot v2 task cancelled (operational or post-finalisation)",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    user_cancel_observed=user_cancel_observed[0],
                )
                raise
        except Exception as exc:
            restored = chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result)
            if restored:
                try:
                    await _restore_workflow_definition(original_workflow, organization.organization_id)
                except Exception:
                    LOG.warning(
                        "Workflow restore failed inside generic-error handler",
                        organization_id=organization.organization_id,
                        exc_info=True,
                    )
            if chat is not None:
                workflow_modified = bool(getattr(agent_result, "workflow_was_persisted", False)) and not restored
                recovered_result, failure = _build_recoverable_route_agent_result(
                    exc,
                    workflow_modified=workflow_modified,
                    clear_proposed_workflow=restored or workflow_modified,
                    global_llm_context=global_llm_context,
                    turn_id=turn_id,
                    turn_index=turn_index,
                )
                recovered_result.turn_outcome = _with_current_copilot_code_mode_metadata(
                    recovered_result.turn_outcome,
                    effective_mode=effective_mode,
                    code_available=current_code_available,
                    turn_id=turn_id,
                )
                LOG.error(
                    "Unexpected workflow copilot v2 error translated to recoverable reply",
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    failure_kind=failure.failure_kind,
                    internal_error_id=failure.internal_error_id,
                    exception_type=failure.exception_type,
                    exc_info=True,
                )
                await asyncio.shield(
                    _finalise_normal_turn(
                        stream=stream,
                        chat=chat,
                        organization_id=organization.organization_id,
                        original_workflow=original_workflow,
                        chat_request=chat_request,
                        agent_result=recovered_result,
                    )
                )
                terminal_frame_emitted = True
                capture_code_mode_opt_out_after_persist()
            else:
                LOG.error(
                    "Unexpected error in workflow copilot v2",
                    organization_id=organization.organization_id,
                    error=str(exc),
                    exc_info=True,
                )
                terminal_frame_emitted = True
                await stream.send(
                    WorkflowCopilotStreamErrorUpdate(
                        type=WorkflowCopilotStreamMessageType.ERROR,
                        error="An error occurred. Please try again.",
                        turn_id=turn_id,
                        narrative_summary=None,
                    )
                )
        finally:
            if cancel_watcher is not None and not cancel_watcher.done():
                cancel_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cancel_watcher
            await _ensure_terminal_frame(stream, terminal_frame_emitted, turn_id=turn_id)

    return FastAPIEventSourceStream.create(request, stream_handler)


COPILOT_V2_FLAG_KEY = "ENABLE_WORKFLOW_COPILOT_V2"


async def _should_use_copilot_v2(
    organization: Organization, workflow_permanent_id: str, mode: str | None = None
) -> bool:
    if mode is not None:
        return mode == "build"
    if settings.ENABLE_WORKFLOW_COPILOT_V2:
        return True
    try:
        # distinct_id is the org (not a run id) because this gate is an org-sticky rollout:
        # copilot chat may not have a stable run at dispatch time, and we want each org to
        # see the same path across sessions. Contrast with backend.md's default of run-level
        # ids for per-run randomized experiments.
        return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            COPILOT_V2_FLAG_KEY,
            distinct_id=organization.organization_id,
            properties={"organization_id": organization.organization_id},
        )
    except Exception:
        LOG.exception(
            "Failed to evaluate copilot-v2 feature flag; falling back to legacy copilot",
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        return False


async def _get_or_create_workflow_copilot_chat(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    workflow_copilot_chat_id: str | None,
) -> WorkflowCopilotChat:
    if workflow_copilot_chat_id:
        chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
            organization_id=organization_id,
            workflow_copilot_chat_id=workflow_copilot_chat_id,
        )
        if not chat:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
        if workflow_permanent_id != chat.workflow_permanent_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong workflow permanent ID")
        return chat

    return await app.DATABASE.workflow_params.create_workflow_copilot_chat(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )


async def _validate_copilot_audio_artifact_id(
    *,
    audio_artifact_id: str | None,
    organization_id: str,
    workflow_copilot_chat_id: str,
) -> str | None:
    if not audio_artifact_id:
        return None

    artifact = await app.DATABASE.artifacts.get_artifact_by_id(
        audio_artifact_id,
        organization_id=organization_id,
    )
    if not artifact:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio artifact not found")
    if artifact.artifact_type != ArtifactType.AUDIO:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio artifact type")

    # Audio artifacts are written through create_log_artifact for this chat, so
    # the log URI carries the chat id. Keep this in sync with artifact storage
    # path construction if that layout changes.
    log_marker = f"/logs/{LogEntityType.WORKFLOW_COPILOT_CHAT}/{workflow_copilot_chat_id}/"
    if log_marker not in (artifact.uri or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Audio artifact is not linked to this chat",
        )
    return audio_artifact_id


@base_router.post("/workflow/copilot/chat-audio", include_in_schema=False)
async def workflow_copilot_chat_audio(
    workflow_permanent_id: str = Form(...),
    workflow_copilot_chat_id: str | None = Form(None),
    file: UploadFile = File(...),
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotAudioUploadResponse:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_WORKFLOW_COPILOT_AUDIO_CONTENT_TYPES:
        await file.close()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported audio format")

    max_upload_bytes = settings.MAX_UPLOAD_FILE_SIZE
    try:
        audio_bytes = await file.read(max_upload_bytes + 1)
    except Exception as exc:
        LOG.exception("Failed to read workflow copilot dictation audio upload")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to read audio file"
        ) from exc
    finally:
        await file.close()

    if not audio_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio file is empty")

    if len(audio_bytes) > max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Audio file exceeds the maximum allowed size ({max_upload_bytes / 1024 / 1024:.0f} MB)",
        )

    chat = await _get_or_create_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_copilot_chat_id=workflow_copilot_chat_id,
    )

    LOG.info(
        "Workflow copilot dictation audio upload",
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization.organization_id,
        audio_bytes=len(audio_bytes),
    )

    audio_artifact_id = await app.ARTIFACT_MANAGER.create_log_artifact(
        log_entity_type=LogEntityType.WORKFLOW_COPILOT_CHAT,
        log_entity_id=chat.workflow_copilot_chat_id,
        artifact_type=ArtifactType.AUDIO,
        organization_id=organization.organization_id,
        data=audio_bytes,
    )
    await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([chat.workflow_copilot_chat_id])

    return WorkflowCopilotAudioUploadResponse(
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        audio_artifact_id=audio_artifact_id,
    )


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    if await _should_use_copilot_v2(organization, chat_request.workflow_permanent_id, mode=chat_request.mode):
        return await _new_copilot_chat_post(request, chat_request, organization)

    async def stream_handler(stream: EventSourceStream) -> None:
        turn_id = uuid.uuid4().hex
        LOG.info(
            "Workflow copilot chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            message=chat_request.message,
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        terminal_frame_emitted = False
        try:
            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Processing...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            chat = await _get_or_create_workflow_copilot_chat(
                organization_id=organization.organization_id,
                workflow_permanent_id=chat_request.workflow_permanent_id,
                workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            )
            chat_request.workflow_copilot_chat_id = chat.workflow_copilot_chat_id
            chat_request.audio_artifact_id = await _validate_copilot_audio_artifact_id(
                audio_artifact_id=chat_request.audio_artifact_id,
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )

            chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            prior_turn_outcome = _latest_assistant_turn_outcome(chat_messages)
            current_code_available = await _resolve_copilot_code_available(
                organization.organization_id,
                chat_request,
            )
            effective_mode = _effective_copilot_composer_mode(
                chat_request,
                uses_v2=False,
                code_mode_fallback=current_code_available,
            )
            global_llm_context = None
            for message in reversed(chat_messages):
                if message.global_llm_context is not None:
                    global_llm_context = message.global_llm_context
                    break

            debug_run_info = await _get_debug_run_info(organization.organization_id, chat_request.workflow_run_id)

            # Format debug run info for prompt
            debug_run_info_text = ""
            if debug_run_info:
                debug_run_info_text = f"Block Label: {debug_run_info.block_label}"
                debug_run_info_text += f" Block Type: {debug_run_info.block_type}"
                debug_run_info_text += f" Status: {debug_run_info.block_status}"
                if debug_run_info.failure_reason:
                    debug_run_info_text += f"\nFailure Reason: {debug_run_info.failure_reason}"
                if debug_run_info.html:
                    debug_run_info_text += f"\n\nVisible Elements Tree (HTML):\n{debug_run_info.html}"

            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Thinking...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            # SKY-8986: do not short-circuit on client disconnect. The LLM
            # call and the DB persistence below must complete so the reply
            # is in the chat history when the user reconnects.
            with bind_copilot_session_id(chat.workflow_copilot_chat_id):
                (
                    user_response,
                    updated_workflow,
                    updated_global_llm_context,
                    updated_workflow_yaml,
                ) = await copilot_call_llm(
                    stream,
                    organization.organization_id,
                    chat_request,
                    convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                    global_llm_context,
                    debug_run_info_text,
                )

            legacy_turn_outcome = _with_current_copilot_code_mode_metadata(
                build_minimal_turn_outcome(
                    user_response,
                    response_kind=ResponseKind.CLARIFY if effective_mode == "ask" else ResponseKind.BUILD,
                ),
                effective_mode=effective_mode,
                code_available=current_code_available,
                turn_id=turn_id,
            )

            if updated_workflow and chat.auto_accept is not True:
                proposed_data = updated_workflow.model_dump(mode="json")
                # _copilot_yaml is what /apply-proposed-workflow re-parses into
                # WorkflowCreateYAMLRequest. Without it, Accept 400s.
                if updated_workflow_yaml:
                    proposed_data["_copilot_yaml"] = updated_workflow_yaml
                await app.DATABASE.workflow_params.update_workflow_copilot_chat(
                    organization_id=chat.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    proposed_workflow=proposed_data,
                )

            await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.USER,
                content=chat_request.message,
                audio_artifact_id=chat_request.audio_artifact_id,
            )

            assistant_message = await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.AI,
                content=user_response,
                global_llm_context=updated_global_llm_context,
                turn_outcome=legacy_turn_outcome,
            )

            _capture_copilot_code_mode_opt_out(
                prior_turn_outcome=prior_turn_outcome,
                to_mode=effective_mode,
                current_code_available=current_code_available,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                workflow_permanent_id=chat.workflow_permanent_id,
                organization_id=organization.organization_id,
                turn_id=turn_id,
            )
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamResponseUpdate(
                    type=WorkflowCopilotStreamMessageType.RESPONSE,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    message=user_response,
                    updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
                    response_time=assistant_message.created_at,
                )
            )
        except HTTPException as exc:
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error=exc.detail,
                )
            )
        except LLMProviderError as exc:
            LOG.error(
                "LLM provider error",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="Failed to process your request. Please try again.",
                )
            )
        except Exception as exc:
            LOG.error(
                "Unexpected error in workflow copilot",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="An error occurred. Please try again.",
                )
            )
        finally:
            await _ensure_terminal_frame(stream, terminal_frame_emitted, turn_id=turn_id)

    return FastAPIEventSourceStream.create(request, stream_handler)


@base_router.get("/workflow/copilot/chats", include_in_schema=False)
async def list_workflow_copilot_chats(
    workflow_permanent_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowCopilotChatSummary]:
    return await app.DATABASE.workflow_params.get_workflow_copilot_chats(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        page=page,
        page_size=page_size,
        search=search,
    )


@base_router.get("/workflow/copilot/chat-history", include_in_schema=False)
async def workflow_copilot_chat_history(
    workflow_permanent_id: str | None = None,
    workflow_copilot_chat_id: str | None = None,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotChatHistoryResponse:
    if workflow_copilot_chat_id:
        chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
            organization_id=organization.organization_id,
            workflow_copilot_chat_id=workflow_copilot_chat_id,
        )
    elif workflow_permanent_id:
        chat = await app.DATABASE.workflow_params.get_latest_workflow_copilot_chat(
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="workflow_permanent_id or workflow_copilot_chat_id is required",
        )
    if chat:
        chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
            chat.workflow_copilot_chat_id
        )
    else:
        chat_messages = []
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id if chat else None,
        chat_history=convert_to_history_messages(chat_messages),
        proposed_workflow=chat.proposed_workflow if chat else None,
        auto_accept=chat.auto_accept if chat else None,
    )


@base_router.post("/workflow/copilot/cancel", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT)
async def workflow_copilot_cancel(
    cancel_request: WorkflowCopilotCancelRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    """Hard-cancel an in-progress workflow copilot v2 turn.

    Sets a per-token Redis flag the SSE handler's cancel-watcher polls; the
    watcher cancels the handler task, propagating ``CancelledError`` into
    whichever ``await`` is currently parked (LLM chunk, browser action, DB
    write). Returns 503 when ``app.CACHE`` is absent — the FE Stop button
    still aborts client-side, but the backend can't signal the running handler.
    """
    cache = getattr(app, "CACHE", None)
    if cache is None:
        LOG.warning(
            "Workflow copilot cancel attempted without cache",
            organization_id=organization.organization_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cancel not supported in this environment",
        )
    await cache.set(
        _copilot_cancel_key(organization.organization_id, cancel_request.cancel_token),
        "1",
        ex=COPILOT_CANCEL_TTL,
    )


@base_router.post(
    "/workflow/copilot/clear-proposed-workflow", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT
)
async def workflow_copilot_clear_proposed_workflow(
    clear_request: WorkflowCopilotClearProposedWorkflowRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    updated_chat = await app.DATABASE.workflow_params.update_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_copilot_chat_id=clear_request.workflow_copilot_chat_id,
        proposed_workflow=None,
        auto_accept=clear_request.auto_accept,
    )
    if not updated_chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")


@base_router.post("/workflow/copilot/apply-proposed-workflow", include_in_schema=False)
async def workflow_copilot_apply_proposed_workflow(
    apply_request: WorkflowCopilotApplyProposedWorkflowRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    """Accept a copilot proposal: stamp v1, write a new copilot-attributed version, clear the proposal."""
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=organization.organization_id,
        workflow_copilot_chat_id=apply_request.workflow_copilot_chat_id,
    )
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")

    proposal = chat.proposed_workflow
    if not proposal:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No proposed workflow to apply")

    copilot_yaml = proposal.get("_copilot_yaml") if isinstance(proposal, dict) else None
    if not copilot_yaml:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Proposed workflow has no copilot YAML to apply",
        )

    copilot_yaml = await apply_derived_code_block_steps(copilot_yaml)

    try:
        yaml_request = _normalize_copilot_yaml(copilot_yaml)
    except (yaml.YAMLError, ValidationError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Proposed copilot YAML is invalid: {e}",
        )

    current_workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=chat.workflow_permanent_id,
        organization_id=organization.organization_id,
    )
    created_by_stamp = "copilot" if is_copilot_born_initial_write(current_workflow) else None

    if created_by_stamp == "copilot" and current_workflow is not None:
        # Stamp v1 too so MIN(created_at)-per-WPID queries see copilot-born.
        await app.WORKFLOW_SERVICE.update_workflow_definition(
            workflow_id=current_workflow.workflow_id,
            organization_id=organization.organization_id,
            created_by="copilot",
            edited_by="copilot",
        )

    new_workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=organization,
        request=yaml_request,
        workflow_permanent_id=chat.workflow_permanent_id,
        created_by=created_by_stamp,
        edited_by="copilot",
    )

    try:
        # Best-effort: a 500 here would invite a retry that creates a duplicate version.
        await app.DATABASE.workflow_params.update_workflow_copilot_chat(
            organization_id=organization.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            proposed_workflow=None,
            auto_accept=apply_request.auto_accept,
        )
    except Exception:
        LOG.warning(
            "Failed to clear copilot proposal after applying it; new workflow version was created",
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            new_workflow_id=new_workflow.workflow_id,
            exc_info=True,
        )

    return new_workflow


def convert_to_history_messages(
    messages: list[WorkflowCopilotChatMessage],
) -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        WorkflowCopilotChatHistoryMessage(
            sender=message.sender,
            content=message.content,
            audio_artifact_id=message.audio_artifact_id,
            turn_outcome=message.turn_outcome,
            created_at=message.created_at,
            narrative_payload=message.narrative_payload,
        )
        for message in messages
    ]


@base_router.post("/workflow/copilot/convert-yaml-to-blocks", include_in_schema=False)
async def workflow_copilot_convert_yaml_to_blocks(
    request: WorkflowYAMLConversionRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowYAMLConversionResponse:
    """
    Convert workflow definition YAML to blocks format for comparison view.
    This endpoint is used by the frontend to convert YAML to the proper blocks structure
    that the comparison panel expects.
    """
    try:
        parsed_yaml = safe_load_no_dates(request.workflow_definition_yaml)
        workflow_definition_yaml = WorkflowDefinitionYAML.model_validate(parsed_yaml)

        _repair_next_block_label_chain(workflow_definition_yaml.blocks)

        workflow_definition = convert_workflow_definition(
            workflow_definition_yaml=workflow_definition_yaml,
            workflow_id=request.workflow_id,
        )

        return WorkflowYAMLConversionResponse(workflow_definition=workflow_definition.model_dump(mode="json"))
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to convert workflow YAML: {str(e)}",
        )
