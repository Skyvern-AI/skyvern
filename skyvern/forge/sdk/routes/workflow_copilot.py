import asyncio
import contextlib
import hashlib
import hmac
import re
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, cast, get_args
from urllib.parse import urlparse

import structlog
import yaml
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, status
from opentelemetry import trace as otel_trace
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from skyvern import analytics
from skyvern.config import settings
from skyvern.constants import DEFAULT_WORKFLOW_TITLES
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import ArtifactType, LogEntityType
from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionResponse, question_wait_is_live
from skyvern.forge.sdk.copilot.browser_ablation import CopilotEvalMode
from skyvern.forge.sdk.copilot.build_test_connect_failure import SUPERSEDED_BY_NEWER_TEST_REASON
from skyvern.forge.sdk.copilot.canonical_ownership import workflow_content_fingerprint
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import (
    AgentResult,
    ProposalDisposition,
    TurnNarrativePayload,
    clear_proposed_credential,
)
from skyvern.forge.sdk.copilot.credential_pause import (
    CredentialPauseRejection,
    check_credential_pause_resumable,
    resolve_credential_pause,
)
from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.interruption import (
    INTERRUPTED_TERMINAL_REASON,
    InterruptedTurnFacts,
    cancel_notice,
    render_interrupted_message,
)
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler, resolve_raw_secret_safety_handler
from skyvern.forge.sdk.copilot.recoverable_failure import (
    RecoverableFailure,
    build_recoverable_failure,
    format_recoverable_failure_reply,
    merge_failure_into_context,
)
from skyvern.forge.sdk.copilot.repair_origin_run import RepairOriginRefusal, resolve_repair_origin_binding
from skyvern.forge.sdk.copilot.request_policy import _screen_raw_secret_safety
from skyvern.forge.sdk.copilot.review_gate import parse_execution_receipts, serialize_execution_receipts
from skyvern.forge.sdk.copilot.runtime import close_browser_session_quietly
from skyvern.forge.sdk.copilot.turn_outcome import (
    CopilotComposerMode,
    build_minimal_turn_outcome,
    with_copilot_code_mode_metadata,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _normalize_copilot_yaml as _normalize_copilot_yaml
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as _copilot_process_workflow_yaml
from skyvern.forge.sdk.copilot.workflow_yaml import _repair_next_block_label_chain as _repair_next_block_label_chain
from skyvern.forge.sdk.copilot.workflow_yaml import with_workflow_yaml_title
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.db.exceptions import DuplicateCopilotTurnError, NotFoundError
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.copilot_turn_outcome import PersistedCopilotComposerMode, ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    TURN_OPENER_SENDERS,
    CopilotCancelSource,
    CopilotFailureKind,
    CopilotPendingTurn,
    WorkflowCopilotApplyProposedWorkflowRequest,
    WorkflowCopilotAudioUploadResponse,
    WorkflowCopilotBrowserAblationResponseUpdate,
    WorkflowCopilotCancelRequest,
    WorkflowCopilotChat,
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotChatSummary,
    WorkflowCopilotClearProposedWorkflowRequest,
    WorkflowCopilotCredentialResponseRequest,
    WorkflowCopilotProcessingUpdate,
    WorkflowCopilotQuestionResponseRequest,
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
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.workflows import (
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
)
from skyvern.utils.secret_headers import merge_masked_headers
from skyvern.utils.url_validators import is_blocked_host
from skyvern.utils.yaml_loader import safe_load_no_dates

CHAT_HISTORY_CONTEXT_MESSAGES = 10

# Compatibility export for callers and tests that predate ownership moving to
# copilot.workflow_yaml. The legacy Ask runtime itself remains deleted.
_process_workflow_yaml = _copilot_process_workflow_yaml

# Wall clock, unlike TOTAL_TIMEOUT_SECONDS: that budget excludes time parked in a
# credential pause, so a legitimately live turn can outlast it in real time.
RECONCILE_ABANDON_AFTER_SECONDS = (
    TOTAL_TIMEOUT_SECONDS + settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS + 120
)
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


async def _resolve_copilot_agent_handler(
    workflow_permanent_id: str,
    organization_id: str,
) -> LLMAPIHandler:
    handler = await resolve_main_copilot_handler(workflow_permanent_id, organization_id)
    return handler or app.LLM_API_HANDLER


def _workflow_copilot_ingress_log_fields(message: str) -> dict[str, int]:
    return {"message_length": len(message or "")}


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
UNSCREENED_MESSAGE_PLACEHOLDER = "[Message unavailable because safety screening did not complete]"
COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON = "copilot_recoverable_failure"
USER_CANCELLED_TERMINAL_REASON = "user_cancelled"
TEST_END_TO_END_TURN_MESSAGE = "Test this workflow end to end."
DIAGNOSE_RUN_TURN_MESSAGE = "Diagnose run {run_id} and repair the workflow."


# The id is interpolated into a row the transcript renders as a product utterance, so anything
# that could read as prose is refused rather than echoed.
_WORKFLOW_RUN_ID_RE = re.compile(r"\Awr_[A-Za-z0-9]{1,40}\Z")

_UNUSABLE_DIAGNOSE_RUN_REFUSALS = frozenset(
    {
        RepairOriginRefusal.RUN_NOT_FOUND,
        RepairOriginRefusal.FOREIGN_ORGANIZATION,
        RepairOriginRefusal.WORKFLOW_MISMATCH,
    }
)


def _turn_opener_sender(chat_request: WorkflowCopilotChatRequest) -> WorkflowCopilotChatSender:
    if chat_request.product_action == "diagnose_run":
        return WorkflowCopilotChatSender.PRODUCT
    return WorkflowCopilotChatSender.USER


async def _apply_diagnose_run_action(
    chat_request: WorkflowCopilotChatRequest,
    *,
    organization_id: str,
    workflow_permanent_id: str,
) -> None:
    """Replace the caller's prose with the server's own receipt, or refuse the action.

    The receipt is written before the first await, so no failure inside this function can reach a
    writer while the request still holds caller text under the product sender.
    """
    if chat_request.product_action != "diagnose_run":
        return
    if not chat_request.workflow_run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workflow_run_id is required to diagnose a run.",
        )
    if not _WORKFLOW_RUN_ID_RE.match(chat_request.workflow_run_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workflow_run_id is not a valid run id.",
        )
    # The id is validated, so the receipt can be written now — ahead of the first await, whose
    # failure would otherwise reach the recovery writer with the caller's text still in place.
    chat_request.message = DIAGNOSE_RUN_TURN_MESSAGE.format(run_id=chat_request.workflow_run_id)
    binding = await resolve_repair_origin_binding(
        workflow_run_id=chat_request.workflow_run_id,
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if binding.refusal in _UNUSABLE_DIAGNOSE_RUN_REFUSALS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow_run_id does not name a run of this workflow.",
        )
    # Only a settled run has a packet to hydrate; without this the turn would reach the model
    # carrying nothing but the receipt.
    if not binding.finished:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow_run_id names a run that is still in progress.",
        )


def _effective_copilot_build_mode(
    chat_request: WorkflowCopilotChatRequest,
    *,
    code_mode_fallback: bool = False,
) -> CopilotComposerMode:
    if chat_request.code_block is not None:
        return "code" if chat_request.code_block is True else "build"
    return "code" if code_mode_fallback else "build"


def _prior_global_llm_context(chat_messages: list[WorkflowCopilotChatMessage]) -> str | None:
    latest = next(
        (message.global_llm_context for message in reversed(chat_messages) if message.global_llm_context is not None),
        None,
    )
    # Credential proposals grant authority for one adjacent turn only. An
    # interrupted assistant row persists a null context, so do not scan past it
    # and revive an older proposal on the following turn.
    last_assistant_context = next(
        (message.global_llm_context for message in reversed(chat_messages) if message.turn_outcome is not None),
        None,
    )
    if last_assistant_context is None:
        return clear_proposed_credential(latest)
    return latest


def _latest_assistant_turn_outcome(chat_messages: list[WorkflowCopilotChatMessage]) -> TurnOutcome | None:
    for message in reversed(chat_messages):
        if message.sender == WorkflowCopilotChatSender.AI and message.turn_outcome is not None:
            return message.turn_outcome
    return None


def _assistant_execution_receipts(
    chat_messages: list[WorkflowCopilotChatMessage],
) -> dict[str, set[str]]:
    receipts: dict[str, set[str]] = {}
    for message in chat_messages:
        payload = message.narrative_payload
        if message.sender != WorkflowCopilotChatSender.AI or not isinstance(payload, dict):
            continue
        for label, fingerprints in parse_execution_receipts(payload.get("testedBlockFingerprints")).items():
            receipts.setdefault(label, set()).update(fingerprints)
    return receipts


def _should_emit_copilot_code_mode_opt_out(
    *,
    prior_turn_outcome: TurnOutcome | None,
    to_mode: CopilotComposerMode,
) -> bool:
    if prior_turn_outcome is None:
        return False
    from_mode = prior_turn_outcome.copilot_effective_mode
    if from_mode is None or from_mode == to_mode:
        return False
    return from_mode == "code" and to_mode == "build"


def _reason_category_for_copilot_code_mode_opt_out(
    prior_turn_outcome: TurnOutcome,
) -> str:
    if (
        prior_turn_outcome.copilot_last_code_build_failed
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
    workflow_copilot_chat_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    turn_id: str | None,
) -> None:
    if prior_turn_outcome is None or not _should_emit_copilot_code_mode_opt_out(
        prior_turn_outcome=prior_turn_outcome,
        to_mode=to_mode,
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


async def _resolve_copilot_request_config(
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
) -> CopilotConfig:
    copilot_config = await app.AGENT_FUNCTION.get_copilot_config_for_request(
        organization_id,
        code_block_mode=chat_request.code_block,
    )
    return copilot_config or CopilotConfig(
        block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE,
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


COPILOT_CANCEL_TTL = timedelta(minutes=5)
# Polling cadence for the cancel-watcher sidecar. Worst-case latency from a
# user's Stop click to ``handler_task.cancel()`` is one cadence period plus
# the Redis round-trip — well under the 5-minute scenario this feature
# exists to fix, and far below any client-side timeout budget.
COPILOT_CANCEL_POLL_SECONDS = 1.5


def _copilot_cancel_key(organization_id: str, cancel_token: str) -> str:
    return f"copilot_cancel:{organization_id}:{cancel_token}"


def _copilot_idempotency_digest(
    organization_id: str,
    workflow_copilot_chat_id: str,
    idempotency_key: str | None,
) -> str | None:
    if not idempotency_key:
        return None
    scoped_value = f"{organization_id}\0{workflow_copilot_chat_id}\0{idempotency_key}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), scoped_value, hashlib.sha256).hexdigest()


def _coerce_cancel_source(flag: str | bytes | None) -> CopilotCancelSource | None:
    """Read the cancel source out of the flag value the cancel route wrote.

    Rows written before the source existed hold ``"1"``, which names no gesture.
    """
    if isinstance(flag, bytes):
        flag = flag.decode(errors="replace")
    if flag in get_args(CopilotCancelSource):
        return cast(CopilotCancelSource, flag)
    return None


async def _watch_for_cancel(
    cache: Any,
    organization_id: str,
    cancel_token: str,
    handler_task: asyncio.Task,
    observed: list[bool],
    observed_source: list[CopilotCancelSource | None] | None = None,
) -> None:
    """Cancel ``handler_task`` when the matching Redis flag flips truthy.

    Sets ``observed[0] = True`` before issuing the cancel so the handler's
    ``except CancelledError`` block can tell a user cancel apart from
    server shutdown — only the user path writes a stop-report row.
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
            source = _coerce_cancel_source(flag)
            LOG.info(
                "Copilot cancel signal observed; cancelling handler task",
                cancel_token=cancel_token,
                organization_id=organization_id,
                cancel_source=source,
            )
            observed[0] = True
            if observed_source is not None:
                observed_source[0] = source
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
                    failure_kind="server",
                    turn_id=turn_id,
                    narrative_summary=None,
                )
            )
        )
    except BaseException:
        pass


_KNOWN_PROPOSAL_DISPOSITIONS: frozenset[str] = frozenset(get_args(ProposalDisposition))


def _proposal_disposition(agent_result: AgentResult | None) -> ProposalDisposition:
    if agent_result is None:
        return "no_proposal"
    disposition = agent_result.proposal_disposition
    # ``_make_agent_result`` forwards untyped kwargs, so an out-of-vocabulary value can reach here;
    # downstream copy indexes this hard, and the review-gated fallback never grants an auto-apply.
    if disposition in _KNOWN_PROPOSAL_DISPOSITIONS:
        return disposition
    return "no_proposal" if agent_result.updated_workflow is None else "review_untested"


def _preserved_draft_disposition(
    agent_result: AgentResult | None = None, *, draft_present: bool
) -> ProposalDisposition | None:
    """The disposition naming the draft awaiting the user, or None when none is. Only a draft this
    turn authored may borrow this turn's disposition; one carried over from an earlier turn is named
    without it, since this turn's disposition says nothing about how that draft was reached."""
    if not draft_present:
        return None
    turn_authored_the_draft = agent_result is not None and agent_result.updated_workflow is not None
    return _proposal_disposition(agent_result) if turn_authored_the_draft else "no_proposal"


def _effective_auto_accept(auto_accept: bool | None, agent_result: AgentResult | None) -> bool:
    """Only auto-applicable proposals may honor an explicit ``auto_accept=True``; a verified build
    never commits on the user's behalf, it lands as a pending proposal for the review gate. A
    recorded build-test failure downgrades the disposition before the result reaches this gate."""
    if agent_result is None or agent_result.cancelled is True:
        return False
    if agent_result.updated_workflow is None and not agent_result.has_staged_proposal:
        return False
    if _proposal_disposition(agent_result) != "auto_applicable":
        return False
    return auto_accept is True


def _should_restore_persisted_workflow(auto_accept: bool | None, agent_result: AgentResult | None) -> bool:
    """Restore when a mid-turn canonical write isn't covered by an accepted proposal."""
    if agent_result is None:
        return False
    if not (agent_result.workflow_was_persisted or agent_result.canonical_was_persisted_due_to_param_change):
        return False
    if agent_result.updated_workflow is None:
        return True
    return not _effective_auto_accept(auto_accept, agent_result)


def _should_commit_staged_workflow(auto_accept: bool | None, agent_result: AgentResult | None) -> bool:
    """Auto-accept commits the final staged workflow even after a mid-turn degraded
    write: a later blocks-only edit stages without persisting, so only this terminal
    commit reconciles canonical with the proposal the user sees."""
    if agent_result is None or not _effective_auto_accept(auto_accept, agent_result):
        return False
    return agent_result.has_staged_proposal


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


def _http_exception_failure_kind(exc: HTTPException) -> CopilotFailureKind | None:
    """A 5xx is ours (the LLM returned nothing usable, or the route broke); a 4xx is a real refusal
    the caller should see reflected as the product's own answer."""
    return "server" if exc.status_code >= 500 else None


def _make_error_narrative_payload(turn_id: str | None, turn_index: int | None, message: str) -> TurnNarrativePayload:
    return {
        "turnId": turn_id,
        "turnIndex": turn_index if turn_index is not None else 0,
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
    prior_turn_outcome: TurnOutcome | None = None,
) -> tuple[AgentResult, RecoverableFailure]:
    failure = build_recoverable_failure(error, workflow_modified=workflow_modified)
    user_response = format_recoverable_failure_reply(failure)
    connected_account_choices = prior_turn_outcome.connected_account_choices if prior_turn_outcome is not None else None
    narrative_payload = _make_error_narrative_payload(turn_id, turn_index, user_response)
    if connected_account_choices:
        narrative_payload["connectedAccountChoices"] = [
            choice.model_dump(mode="json") for choice in connected_account_choices
        ]
    agent_result = AgentResult(
        user_response=user_response,
        updated_workflow=None,
        global_llm_context=clear_proposed_credential(merge_failure_into_context(global_llm_context, failure)),
        workflow_was_persisted=False,
        proposal_disposition="no_proposal",
        clear_proposed_workflow=clear_proposed_workflow,
        turn_id=turn_id,
        narrative_payload=narrative_payload,
        turn_outcome=build_minimal_turn_outcome(
            user_response,
            response_kind=ResponseKind.RECOVER,
            reason_code=failure.failure_kind,
            terminal_reason=COPILOT_RECOVERABLE_FAILURE_TERMINAL_REASON,
        ).model_copy(update={"connected_account_choices": connected_account_choices}),
    )
    _record_recoverable_failure_span_attrs(failure, proposal_disposition="no_proposal")
    return agent_result, failure


async def _clear_proposed_workflow(chat: Any) -> None:
    await app.DATABASE.workflow_params.update_workflow_copilot_chat(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        proposed_workflow=None,
    )
    # Keep the in-memory chat in sync — a same-turn recovery retry reads
    # chat.proposed_workflow again and must not see the pre-write value.
    chat.proposed_workflow = None


def _build_proposed_workflow_data(updated_workflow: Workflow, agent_result: AgentResult) -> dict[str, Any]:
    proposed_data = dict(updated_workflow.model_dump(mode="json"))
    if agent_result.workflow_yaml:
        # Accept reparses this YAML and never passes through _process_workflow_yaml, so the
        # title it carries has to be the effective one rather than whatever the model typed.
        proposed_data["_copilot_yaml"] = with_workflow_yaml_title(agent_result.workflow_yaml, updated_workflow.title)
    code_artifact_metadata = getattr(agent_result, "code_artifact_metadata", None)
    if code_artifact_metadata:
        proposed_data["_copilot_code_artifact_metadata"] = code_artifact_metadata
    if _proposal_disposition(agent_result) == "review_untested":
        proposed_data["_copilot_unvalidated"] = True
    if agent_result.executed_block_fingerprints:
        proposed_data["_copilot_tested_block_fingerprints"] = serialize_execution_receipts(
            agent_result.executed_block_fingerprints
        )
    return proposed_data


def _output_policy_blocked_final_response(agent_result: AgentResult) -> bool:
    diagnostics = getattr(agent_result, "output_policy_diagnostics", None)
    return isinstance(diagnostics, dict) and diagnostics.get("final_output_policy_allowed") is False


async def _persist_proposed_workflow_state(
    chat: Any, agent_result: AgentResult, restored: bool, keep_pending_proposal: bool = False
) -> None:
    updated_workflow = agent_result.updated_workflow
    auto_accept_effective = _effective_auto_accept(chat.auto_accept, agent_result)
    if not auto_accept_effective and updated_workflow:
        proposed_workflow_data = _build_proposed_workflow_data(updated_workflow, agent_result)
        await app.DATABASE.workflow_params.update_workflow_copilot_chat(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            proposed_workflow=proposed_workflow_data,
        )
        # Keep the in-memory chat in sync — a same-turn recovery retry reads
        # chat.proposed_workflow again and must not see the pre-write value.
        chat.proposed_workflow = proposed_workflow_data
    elif chat.proposed_workflow is not None and (
        (restored and not keep_pending_proposal)
        or agent_result.clear_proposed_workflow
        or _should_commit_staged_workflow(chat.auto_accept, agent_result)
    ):
        # Null any persisted proposed_workflow the assistant just invalidated
        # so a reload does not resurrect a stale Accept/Reject card. Runs
        # under both auto_accept values — a stale proposal can survive an
        # auto-accept toggle. The staged-commit clause always wins over
        # keep_pending_proposal: this turn's own auto-accept commit already
        # overwrote canonical, so an earlier pending proposal is now stale
        # regardless of the client's preservation request.
        await _clear_proposed_workflow(chat)
    elif (
        # This intentionally checks the raw setting, not
        # ``auto_accept_effective``: no-proposal OutputPolicy blocks are not
        # auto-applicable, but ordinary auto-accept turns still need to clear a
        # stale unvalidated card once no UI renders a review panel for it.
        # keep_pending_proposal still wins: auto_accept doesn't cover
        # review_untested/review_tested, so a gate-worthy card can coexist
        # with auto_accept=True and must survive the same as any other bypass.
        chat.auto_accept is True
        and not keep_pending_proposal
        and chat.proposed_workflow is not None
        and chat.proposed_workflow.get("_copilot_unvalidated") is True
        and not _output_policy_blocked_final_response(agent_result)
    ):
        # The leftover unvalidated proposal is no longer attached to the chat
        # tail; clear it so reload doesn't resurrect a stale Accept/Reject card.
        await _clear_proposed_workflow(chat)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _assistant_row_for_turn(chat: WorkflowCopilotChat, turn_id: str) -> WorkflowCopilotChatMessage | None:
    # Reads the whole chat to find one turn's row, on every finalize. Fine at current per-chat message
    # counts; if histories grow, index turn_outcome->>'copilot_turn_id' and query for it directly.
    messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
    )
    return next(
        (
            message
            for message in messages
            if message.sender == WorkflowCopilotChatSender.AI
            and message.turn_outcome is not None
            and message.turn_outcome.copilot_turn_id == turn_id
        ),
        None,
    )


async def _assistant_row_exists_for_turn(chat: WorkflowCopilotChat, turn_id: str) -> bool:
    return await _assistant_row_for_turn(chat, turn_id) is not None


def _is_interrupted_recovery_row(message: WorkflowCopilotChatMessage) -> bool:
    return message.turn_outcome is not None and message.turn_outcome.terminal_reason == INTERRUPTED_TERMINAL_REASON


async def _clear_pending_turn(chat: WorkflowCopilotChat, turn_id: str) -> None:
    # A failed clear is self-healing: the reconcile pass sees the turn already
    # answered, skips recovery and retries the clear.
    with contextlib.suppress(Exception):
        await app.DATABASE.workflow_params.clear_pending_copilot_turn(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            turn_id=turn_id,
        )


async def _persist_turn_messages(
    *,
    chat: WorkflowCopilotChat,
    turn_id: str | None,
    user_message: str,
    audio_artifact_id: str | None,
    user_row_already_persisted: bool,
    assistant_content: str,
    global_llm_context: str | None,
    turn_outcome: TurnOutcome | None,
    narrative_payload: TurnNarrativePayload | None,
    sender: WorkflowCopilotChatSender,
) -> WorkflowCopilotChatMessage | None:
    """The only writer of a copilot turn's chat rows; idempotent per ``turn_id``.

    Returns None when the assistant row for this turn already exists, and always
    drops the turn's pending marker last so a crash mid-write stays recoverable.
    """
    if turn_id is not None and turn_outcome is not None and not turn_outcome.copilot_turn_id:
        turn_outcome = turn_outcome.model_copy(update={"copilot_turn_id": turn_id})

    if turn_id is not None:
        stored_chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
            chat.organization_id, chat.workflow_copilot_chat_id
        )
        if isinstance(stored_chat, WorkflowCopilotChat):
            pending = stored_chat.pending_turns.get(turn_id)
            if pending is not None and pending.question_interactions:
                if narrative_payload is None:
                    narrative_payload = _make_error_narrative_payload(turn_id, None, assistant_content)
                narrative_payload["questionInteractions"] = [
                    item.model_dump(mode="json") for item in pending.question_interactions
                ]

    if not user_row_already_persisted:
        await asyncio.shield(
            app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=sender,
                content=user_message,
                audio_artifact_id=audio_artifact_id,
            )
        )

    assistant_message: WorkflowCopilotChatMessage | None = None
    existing = await _assistant_row_for_turn(chat, turn_id) if turn_id is not None else None
    superseding_recovery = (
        existing is not None
        and _is_interrupted_recovery_row(existing)
        and turn_outcome is not None
        and turn_outcome.terminal_reason != INTERRUPTED_TERMINAL_REASON
    )
    if existing is not None and superseding_recovery:
        # Recovery reached this turn first and wrote an interrupted row. The turn then finished,
        # so its real reply is the truth and replaces that row rather than being dropped.
        LOG.info(
            "Copilot turn finished after being recovered; replacing the interrupted row with its reply",
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            turn_id=turn_id,
        )
        assistant_message = await asyncio.shield(
            app.DATABASE.workflow_params.replace_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_message_id=existing.workflow_copilot_chat_message_id,
                content=assistant_content,
                global_llm_context=global_llm_context,
                turn_outcome=turn_outcome,
                narrative_payload=narrative_payload,
            )
        )
    elif existing is not None:
        LOG.info(
            "Copilot turn already has an assistant row; skipping duplicate persist",
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            turn_id=turn_id,
        )
    else:
        assistant_message = await asyncio.shield(
            app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.AI,
                content=assistant_content,
                global_llm_context=global_llm_context,
                turn_outcome=turn_outcome,
                narrative_payload=narrative_payload,
            )
        )

    if turn_id is not None:
        await _clear_pending_turn(chat, turn_id)
    return assistant_message


async def _marked_superseded(
    chat: WorkflowCopilotChat, facts: InterruptedTurnFacts | None
) -> InterruptedTurnFacts | None:
    """Stamp the supersede marker when the run this turn was testing already carries it.

    Read inside the persist coroutine so a cancelled handler still writes the row it shields.
    """
    if facts is None or not facts.run_id or facts.superseded_by_newer_test:
        return facts
    try:
        run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=facts.run_id,
            organization_id=chat.organization_id,
        )
    except Exception:
        LOG.warning("Could not read the interrupted turn's run", workflow_run_id=facts.run_id, exc_info=True)
        return facts
    if run is None or run.failure_reason != SUPERSEDED_BY_NEWER_TEST_REASON:
        return facts
    return facts.model_copy(update={"superseded_by_newer_test": True})


def _interruption_facts(
    chat: WorkflowCopilotChat,
    workflow: Workflow | None,
    agent_result: AgentResult | None,
    *,
    authored_edits_saved: bool | None,
) -> InterruptedTurnFacts:
    run_id = agent_result.cancellation_workflow_run_id if agent_result is not None else None
    return InterruptedTurnFacts(
        recorded_at=datetime.now(timezone.utc).isoformat(),
        iteration=agent_result.cancellation_iteration if agent_result is not None else None,
        workflow_permanent_id=chat.workflow_permanent_id,
        workflow_version=workflow.version if workflow is not None else None,
        authored_edits_saved=authored_edits_saved,
        last_recorded_build_test_phase=(
            agent_result.cancellation_last_recorded_phase if agent_result is not None else None
        ),
        run_id=run_id,
    )


def _interrupted_turn_outcome(
    turn_id: str | None,
    *,
    idempotency_digest: str | None,
    prior_turn_outcome: TurnOutcome | None,
    effective_mode: PersistedCopilotComposerMode | None = None,
    code_available: bool | None = None,
) -> TurnOutcome:
    return TurnOutcome(
        response_kind=ResponseKind.RECOVER,
        reason_code=INTERRUPTED_TERMINAL_REASON,
        terminal_reason=INTERRUPTED_TERMINAL_REASON,
        copilot_runtime="agent",
        copilot_turn_id=turn_id,
        idempotency_digest=idempotency_digest,
        connected_account_choices=(
            prior_turn_outcome.connected_account_choices if prior_turn_outcome is not None else None
        ),
        copilot_effective_mode=effective_mode,
        copilot_code_available=code_available or False,
    )


async def _persist_interrupted_turn(
    chat: WorkflowCopilotChat,
    turn_id: str,
    *,
    facts: InterruptedTurnFacts | None,
    idempotency_digest: str | None = None,
    user_message: str = "",
    user_row_already_persisted: bool = True,
    sender: WorkflowCopilotChatSender = WorkflowCopilotChatSender.USER,
    effective_mode: PersistedCopilotComposerMode | None = None,
    code_available: bool | None = None,
) -> None:
    """Write the assistant row for a turn that stopped before it finished.

    Idempotent per ``turn_id`` through ``_persist_turn_messages``, so the live cancel
    exits and a later reconcile pass over the same turn cannot both leave a row.
    """
    chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
    )
    facts = await _marked_superseded(chat, facts)
    message = render_interrupted_message(
        facts, preserved_draft=_preserved_draft_disposition(draft_present=chat.proposed_workflow is not None)
    )
    narrative_payload = _with_terminal_narrative_metadata(
        _make_error_narrative_payload(turn_id, None, message),
        # An interrupted turn halted; it did not fail. The FE reads this flag to
        # keep the row out of failure treatment (derivePhases, copilotPhases.ts).
        cancelled=True,
        proposal_disposition=_proposal_disposition(None),
    )
    await _persist_turn_messages(
        chat=chat,
        turn_id=turn_id,
        user_message=user_message,
        audio_artifact_id=None,
        user_row_already_persisted=user_row_already_persisted,
        sender=sender,
        assistant_content=message,
        global_llm_context=None,
        turn_outcome=_interrupted_turn_outcome(
            turn_id,
            idempotency_digest=idempotency_digest,
            prior_turn_outcome=_latest_assistant_turn_outcome(chat_messages),
            effective_mode=effective_mode,
            code_available=code_available,
        ),
        narrative_payload=narrative_payload,
    )


async def _persist_cancel_turn(
    stream: EventSourceStream,
    chat: WorkflowCopilotChat,
    organization_id: str,
    original_workflow: Workflow | None,
    user_message: str,
    agent_result: AgentResult | None,
    audio_artifact_id: str | None = None,
    turn_id: str | None = None,
    keep_pending_proposal: bool = False,
    prior_global_llm_context: str | None = None,
    user_row_already_persisted: bool = False,
    record_as_interrupted: bool = False,
    cancel_source: CopilotCancelSource | None = None,
    sender: WorkflowCopilotChatSender = WorkflowCopilotChatSender.USER,
    effective_mode: PersistedCopilotComposerMode | None = None,
    code_available: bool | None = None,
) -> None:
    """Persist a cancelled turn and emit a terminal SSE response frame.

    Pass the agent's ``AgentResult`` for cancels during the agent run so
    rollback uses the same ``workflow_was_persisted`` source of truth as
    the success path; pass ``None`` for pre-agent cancels. A pre-agent cancel
    carries ``prior_global_llm_context`` forward so durable state survives.

    ``record_as_interrupted`` keeps the rollback and proposal handling but records the
    turn as interrupted, for a cancellation no user asked for.
    """
    turn_outcome: TurnOutcome | None
    narrative_summary: str | None
    narrative_payload: TurnNarrativePayload | None
    workflow_applied = False
    canonical_rolled_back = False
    if agent_result is None:
        user_response = cancel_notice(
            stop_button=cancel_source == "stop_button",
            # Only a draft that survives the clear below is still awaiting the user.
            preserved_draft=_preserved_draft_disposition(
                draft_present=chat.proposed_workflow is not None and keep_pending_proposal
            ),
            canonical_rolled_back=False,
        )
        updated_workflow = None
        updated_global_llm_context = clear_proposed_credential(prior_global_llm_context)
        total_tokens = None
        response_type = "REPLY"
        resolved_model = None
        output_policy_diagnostics = None
        turn_outcome = TurnOutcome(
            response_kind=ResponseKind.RECOVER,
            reason_code=USER_CANCELLED_TERMINAL_REASON,
            terminal_reason=USER_CANCELLED_TERMINAL_REASON,
            copilot_runtime="agent",
            copilot_turn_id=turn_id,
            cancel_source=cancel_source,
            copilot_effective_mode=effective_mode,
            copilot_code_available=code_available or False,
        )
        response_turn_id = turn_id
        narrative_summary = user_response
        narrative_payload = _make_error_narrative_payload(turn_id, None, user_response)
        if chat.proposed_workflow is not None and not keep_pending_proposal:
            await asyncio.shield(_clear_proposed_workflow(chat))
    else:
        restored = _should_restore_persisted_workflow(chat.auto_accept, agent_result)
        restore_failed = False
        if restored:
            try:
                await asyncio.shield(_restore_workflow_definition(original_workflow, organization_id))
            except Exception:
                LOG.warning(
                    "Workflow restore failed inside cancel-turn handler",
                    organization_id=organization_id,
                    exc_info=True,
                )
                restore_failed = True
        canonical_rolled_back = restored and not restore_failed
        # A failed rollback means canonical may still hold the mid-turn write — don't
        # honor keep_pending_proposal against an unverified "nothing changed" state.
        effective_keep_pending_proposal = keep_pending_proposal and not restore_failed
        # Broader than "restored-alone" (any no-new-proposal cancel); an agent-explicit
        # clear_proposed_workflow still wins below when this branch is skipped instead.
        if (
            agent_result.updated_workflow is None
            and chat.proposed_workflow is not None
            and not effective_keep_pending_proposal
        ):
            await asyncio.shield(_clear_proposed_workflow(chat))
        else:
            await asyncio.shield(
                _persist_proposed_workflow_state(
                    chat, agent_result, restored, keep_pending_proposal=effective_keep_pending_proposal
                )
            )
        user_response = agent_result.user_response
        updated_workflow = agent_result.updated_workflow
        updated_global_llm_context = agent_result.global_llm_context
        total_tokens = agent_result.total_tokens
        response_type = agent_result.response_type
        resolved_model = agent_result.resolved_model
        output_policy_diagnostics = agent_result.output_policy_diagnostics
        turn_outcome = agent_result.turn_outcome
        if turn_outcome is not None and cancel_source is not None:
            turn_outcome = turn_outcome.model_copy(update={"cancel_source": cancel_source})
        response_turn_id = turn_id or agent_result.turn_id
        narrative_summary = agent_result.narrative_summary
        narrative_payload = agent_result.narrative_payload
        workflow_applied = _effective_auto_accept(chat.auto_accept, agent_result)
        # An interrupted turn overwrites this notice below with its own, so composing one here
        # would build a string that never ships.
        if agent_result.cancelled is True and not record_as_interrupted:
            user_response = cancel_notice(
                base=user_response,
                stop_button=cancel_source == "stop_button",
                preserved_draft=_preserved_draft_disposition(
                    agent_result, draft_present=chat.proposed_workflow is not None
                ),
                canonical_rolled_back=canonical_rolled_back,
            )
        # The reload leg reads the payload text, so it must name the same report the row stores.
        if narrative_payload is not None and narrative_payload.get("terminalMessage") != user_response:
            narrative_payload = {
                **narrative_payload,
                "terminalMessage": user_response,
                "narrativeSummary": user_response,
            }
            narrative_summary = user_response

    if record_as_interrupted:
        facts = await _marked_superseded(
            chat,
            _interruption_facts(
                chat,
                original_workflow,
                agent_result,
                authored_edits_saved=False if canonical_rolled_back else None,
            ),
        )
        user_response = render_interrupted_message(
            facts,
            preserved_draft=_preserved_draft_disposition(
                agent_result, draft_present=chat.proposed_workflow is not None
            ),
        )
        turn_outcome = _interrupted_turn_outcome(
            turn_id or response_turn_id,
            idempotency_digest=turn_outcome.idempotency_digest if turn_outcome is not None else None,
            prior_turn_outcome=turn_outcome,
            effective_mode=effective_mode,
            code_available=code_available,
        )
        # Hydration prefers narrativeSummary, so a cancel-rendered summary left in place
        # would outlive the reply it was rendered for.
        narrative_summary = user_response
        narrative_payload = (
            {**narrative_payload, "terminalMessage": user_response, "narrativeSummary": user_response}
            if narrative_payload is not None
            else _make_error_narrative_payload(turn_id or response_turn_id, None, user_response)
        )

    proposal_disposition = _proposal_disposition(agent_result)
    narrative_payload = _with_terminal_narrative_metadata(
        narrative_payload,
        cancelled=True,
        proposal_disposition=proposal_disposition,
    )

    assistant_message = await _persist_turn_messages(
        chat=chat,
        turn_id=turn_id,
        user_message=user_message,
        audio_artifact_id=audio_artifact_id,
        user_row_already_persisted=user_row_already_persisted,
        sender=sender,
        assistant_content=user_response,
        global_llm_context=updated_global_llm_context,
        turn_outcome=turn_outcome,
        narrative_payload=narrative_payload,
    )
    response_time = assistant_message.created_at if assistant_message else datetime.now(timezone.utc)
    try:
        await asyncio.shield(
            stream.send(
                WorkflowCopilotStreamResponseUpdate(
                    type=WorkflowCopilotStreamMessageType.RESPONSE,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    message=user_response,
                    updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
                    response_time=response_time,
                    total_tokens=total_tokens,
                    response_type=response_type,
                    resolved_model=resolved_model,
                    proposal_disposition=proposal_disposition,
                    workflow_applied=workflow_applied,
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
    turn_id: str | None = None,
    user_row_already_persisted: bool = False,
) -> None:
    """Atomic post-agent finalisation: rollback, proposal, chat rows, RESPONSE.

    Wrapped by the caller in ``asyncio.shield`` so a late user cancel cannot
    interrupt these writes mid-way and leave chat history with a partial turn
    (e.g. proposed_workflow updated but no AI message persisted).
    """
    user_response = agent_result.user_response
    updated_workflow = agent_result.updated_workflow
    updated_global_llm_context = agent_result.global_llm_context
    idempotency_digest = _copilot_idempotency_digest(
        organization_id,
        chat.workflow_copilot_chat_id,
        getattr(chat_request, "idempotency_key", None),
    )
    if idempotency_digest and agent_result.turn_outcome is not None:
        agent_result.turn_outcome = agent_result.turn_outcome.model_copy(
            update={"idempotency_digest": idempotency_digest}
        )

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
    restore_failed = False
    if restored:
        try:
            await _restore_workflow_definition(original_workflow, organization_id)
        except Exception:
            LOG.warning("copilot restore failed in _finalise_normal_turn", exc_info=True)
            restore_failed = True

    if _should_commit_staged_workflow(chat.auto_accept, agent_result):
        try:
            await _commit_staged_workflow(
                organization_id=organization_id,
                workflow_id=chat_request.workflow_id,
                staged_workflow=agent_result.staged_workflow,
                clear_persisted_completion_contract=bool(
                    getattr(agent_result, "clear_persisted_completion_contract", False)
                ),
            )
        except Exception:
            # Undo any mid-turn degraded write so a failed commit fails the turn
            # atomically instead of leaving canonical on a partial intermediate.
            LOG.warning("copilot auto-accept commit failed; rolling back canonical", exc_info=True)
            with contextlib.suppress(Exception):
                await _restore_workflow_definition(original_workflow, organization_id)
            raise

    await _persist_proposed_workflow_state(
        chat,
        agent_result,
        restored,
        # A failed rollback means canonical may still hold the mid-turn write —
        # don't honor keep_pending_proposal against an unverified "nothing changed" state.
        keep_pending_proposal=chat_request.keep_pending_proposal and not restore_failed,
    )
    proposal_disposition = _proposal_disposition(agent_result)
    workflow_applied = _effective_auto_accept(chat.auto_accept, agent_result)
    narrative_payload = _with_terminal_narrative_metadata(
        agent_result.narrative_payload,
        cancelled=False,
        proposal_disposition=proposal_disposition,
    )
    narrative_summary = agent_result.narrative_summary

    assistant_message = await _persist_turn_messages(
        chat=chat,
        turn_id=turn_id,
        user_message=chat_request.message,
        audio_artifact_id=chat_request.audio_artifact_id,
        user_row_already_persisted=user_row_already_persisted,
        sender=_turn_opener_sender(chat_request),
        assistant_content=user_response,
        global_llm_context=updated_global_llm_context,
        turn_outcome=agent_result.turn_outcome,
        narrative_payload=narrative_payload,
    )

    response_data = {
        "type": WorkflowCopilotStreamMessageType.RESPONSE,
        "workflow_copilot_chat_id": chat.workflow_copilot_chat_id,
        "message": user_response,
        "updated_workflow": updated_workflow.model_dump(mode="json") if updated_workflow else None,
        "response_time": assistant_message.created_at if assistant_message else datetime.now(timezone.utc),
        "total_tokens": agent_result.total_tokens,
        "response_type": agent_result.response_type,
        "resolved_model": agent_result.resolved_model,
        "proposal_disposition": proposal_disposition,
        "workflow_applied": workflow_applied,
        "output_policy_diagnostics": agent_result.output_policy_diagnostics,
        "turn_id": agent_result.turn_id,
        "narrative_summary": narrative_summary,
        "narrative_payload": narrative_payload,
    }
    browser_ablation_metadata = (
        agent_result.browser_ablation_metadata if isinstance(agent_result, AgentResult) else None
    )
    if isinstance(browser_ablation_metadata, dict):
        await stream.send(
            WorkflowCopilotBrowserAblationResponseUpdate(
                **response_data,
                **browser_ablation_metadata,
            )
        )
    else:
        await stream.send(WorkflowCopilotStreamResponseUpdate(**response_data))


async def _commit_staged_workflow(
    *,
    organization_id: str,
    workflow_id: str,
    staged_workflow: Workflow | None,
    clear_persisted_completion_contract: bool = False,
) -> None:
    """Overwrite the current workflow version in place (auto-accept path).

    Manual Accept via /workflow/copilot/apply-proposed-workflow creates a new
    version instead. Field list must stay in lockstep with ``_update_workflow``.
    """
    if staged_workflow is None:
        return
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
        reuse_browser_session=staged_workflow.reuse_browser_session,
        mask_secrets=staged_workflow.mask_secrets,
        pin_saved_session_ip=staged_workflow.pin_saved_session_ip,
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
        enable_self_healing=staged_workflow.enable_self_healing,
        code_version=staged_workflow.code_version,
        run_sequentially=staged_workflow.run_sequentially,
        sequential_key=staged_workflow.sequential_key,
        edited_by="copilot",
        preserve_completion_contract=not clear_persisted_completion_contract,
    )


async def _restore_workflow_definition(original_workflow: Workflow | None, organization_id: str) -> None:
    """Roll the workflow back to ``original_workflow``.

    Field list must stay in lockstep with ``_update_workflow``. May raise; callers
    treat a restore failure as best-effort (log and continue), not a hard error.
    """
    if not original_workflow:
        return
    # Rolling a canvas back must not un-name the agent: naming is a separate, one-shot
    # write that only ever fires on a placeholder, so restoring the pre-turn placeholder
    # over a name would leave the user watching their agent revert to "New Agent".
    restored_title = original_workflow.title
    if restored_title in DEFAULT_WORKFLOW_TITLES:
        # Best-effort: preserving a name must never be the reason a rollback fails, and this
        # lookup raises rather than returning None when the workflow is gone.
        try:
            current = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=original_workflow.workflow_permanent_id,
                organization_id=organization_id,
            )
        except Exception:
            current = None
        if current is not None and current.title not in DEFAULT_WORKFLOW_TITLES:
            restored_title = current.title
    await app.WORKFLOW_SERVICE.update_workflow_definition(
        workflow_id=original_workflow.workflow_id,
        organization_id=organization_id,
        title=restored_title,
        description=original_workflow.description,
        workflow_definition=original_workflow.workflow_definition,
        proxy_location=original_workflow.proxy_location,
        webhook_callback_url=original_workflow.webhook_callback_url,
        totp_verification_url=original_workflow.totp_verification_url,
        totp_identifier=original_workflow.totp_identifier,
        persist_browser_session=original_workflow.persist_browser_session,
        reuse_browser_session=original_workflow.reuse_browser_session,
        mask_secrets=original_workflow.mask_secrets,
        pin_saved_session_ip=original_workflow.pin_saved_session_ip,
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
        enable_self_healing=original_workflow.enable_self_healing,
        code_version=original_workflow.code_version,
        run_sequentially=original_workflow.run_sequentially,
        sequential_key=original_workflow.sequential_key,
        created_by=original_workflow.created_by,
        edited_by=original_workflow.edited_by,
        preserve_completion_contract=False,
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


def _run_grant_workflow_yaml(workflow: Workflow | None) -> str | None:
    """The YAML the pre-dispatch credential gate may grant a run from.

    Derived only from the stored workflow row. The submitted YAML is the live canvas and carries
    copilot proposals the user has not accepted, so it must never reach this value.
    """
    if workflow is None:
        return None
    definition = workflow.workflow_definition
    if definition is None or not definition.blocks:
        return None
    return _workflow_to_copilot_yaml(workflow)


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
        "Copilot agent chat request had no workflow blocks; using persisted workflow YAML",
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
    *,
    eval_mode: CopilotEvalMode | None = None,
    eval_entrypoint_url: str | None = None,
) -> EventSourceResponse:
    """Run the OpenAI Agents SDK copilot and
    streams responses in the same SSE shape the frontend consumes. On
    mid-stream failure (HTTPException, LLMProviderError, asyncio.CancelledError,
    or unexpected exception), rolls the workflow definition back to
    ``original_workflow`` via ``_restore_workflow_definition`` to avoid leaving
    a half-persisted draft.
    """

    async def stream_handler(stream: EventSourceStream) -> None:
        LOG.info(
            "Workflow copilot agent chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            **_workflow_copilot_ingress_log_fields(chat_request.message),
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        # Canonical turn_id for the whole HTTP request. Generated before any
        # try-block so route-level error paths and the agent's TURN_START
        # envelope all carry the same identifier.
        turn_id = uuid.uuid4().hex

        original_workflow: Workflow | None = None
        chat = None
        turn_started = False
        # Snapshot before any write this turn — lets a recovery handler tell
        # "this turn's own write already superseded it" from "the write itself
        # is what failed", which agent_result.updated_workflow alone can't.
        proposed_workflow_at_turn_start: Any = None
        agent_result: AgentResult | None = None
        global_llm_context: str | None = None
        terminal_frame_emitted = False
        # Set before the shielded _finalise_normal_turn. A cancel arriving mid-write
        # raises at that await while the shielded write continues, so without this the
        # cancel handler would insert a second row for the same turn concurrently --
        # the read-then-create idempotency has no unique constraint to catch it.
        finalise_started = False
        cancel_watcher: asyncio.Task[None] | None = None
        current_code_available = False
        turn_index = 0
        effective_mode = _effective_copilot_build_mode(chat_request)
        prior_turn_outcome: TurnOutcome | None = None

        def capture_code_mode_opt_out_after_persist() -> None:
            if chat is None:
                return
            _capture_copilot_code_mode_opt_out(
                prior_turn_outcome=prior_turn_outcome,
                to_mode=effective_mode,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                workflow_permanent_id=chat.workflow_permanent_id,
                organization_id=organization.organization_id,
                turn_id=turn_id,
            )

        async def _recover_from_route_exception(
            exc: BaseException,
            *,
            restore_log_context: str,
            translated_log_message: str,
            none_chat_log_message: str,
            none_chat_user_message: str,
            failure_kind: CopilotFailureKind,
        ) -> None:
            """Shared by the LLMProviderError and generic Exception handlers below —
            only their log/user-facing strings and failure kind differ."""
            nonlocal terminal_frame_emitted
            restored = chat is not None and _should_restore_persisted_workflow(
                chat.auto_accept,
                agent_result,
            )
            restore_failed = False
            if restored:
                try:
                    await _restore_workflow_definition(original_workflow, organization.organization_id)
                except Exception:
                    LOG.warning(
                        f"Workflow restore failed inside {restore_log_context}",
                        organization_id=organization.organization_id,
                        exc_info=True,
                    )
                    restore_failed = True
            if chat is not None:
                workflow_modified = bool(getattr(agent_result, "workflow_was_persisted", False)) and not restored
                # Pre-bake restored here: the recovered AgentResult has workflow_was_persisted=False,
                # so _persist_proposed_workflow_state's own restored check would always read False.
                recovered_result, failure = _build_recoverable_route_agent_result(
                    exc,
                    workflow_modified=workflow_modified,
                    # agent_result is the real completed result when the exception hit AFTER
                    # run_copilot_agent returned (e.g. mid-finalisation) — honor its own
                    # explicit clear decision the same way the non-recovery path does.
                    clear_proposed_workflow=(
                        (restored and not chat_request.keep_pending_proposal)
                        or workflow_modified
                        or getattr(agent_result, "clear_proposed_workflow", False)
                        # A staged commit that already succeeded before this exception fired
                        # still invalidates a stale kept proposal, same as the non-recovery path.
                        or _should_commit_staged_workflow(chat.auto_accept, agent_result)
                        # A failed rollback leaves canonical's true state unverified — don't
                        # honor keep_pending_proposal against an assumption that didn't hold.
                        or restore_failed
                        # Compares against the turn-start snapshot, not agent_result.updated_workflow:
                        # that field only means a write was ATTEMPTED, not that it SUCCEEDED — if the
                        # write itself is what raised, chat.proposed_workflow never changed and an
                        # older, legitimately keep_pending_proposal-protected proposal must survive.
                        or chat.proposed_workflow is not proposed_workflow_at_turn_start
                    ),
                    global_llm_context=global_llm_context,
                    turn_id=turn_id,
                    turn_index=turn_index,
                    prior_turn_outcome=prior_turn_outcome,
                )
                recovered_result.turn_outcome = _with_current_copilot_code_mode_metadata(
                    recovered_result.turn_outcome,
                    effective_mode=effective_mode,
                    code_available=current_code_available,
                    turn_id=turn_id,
                )
                LOG.error(
                    translated_log_message,
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    failure_kind=failure.failure_kind,
                    internal_error_id=failure.internal_error_id,
                    exception_type=failure.exception_type,
                    exc_info=True,
                )
                opener_is_server_authored = _turn_opener_sender(chat_request) == WorkflowCopilotChatSender.PRODUCT
                recovery_chat_request = (
                    chat_request
                    if turn_started or opener_is_server_authored
                    else chat_request.model_copy(update={"message": UNSCREENED_MESSAGE_PLACEHOLDER})
                )
                await asyncio.shield(
                    _finalise_normal_turn(
                        stream=stream,
                        chat=chat,
                        organization_id=organization.organization_id,
                        original_workflow=original_workflow,
                        chat_request=recovery_chat_request,
                        agent_result=recovered_result,
                        turn_id=turn_id,
                        user_row_already_persisted=turn_started,
                    )
                )
                terminal_frame_emitted = True
                capture_code_mode_opt_out_after_persist()
            else:
                LOG.error(
                    none_chat_log_message,
                    organization_id=organization.organization_id,
                    error=str(exc),
                    exc_info=True,
                )
                terminal_frame_emitted = True
                await stream.send(
                    WorkflowCopilotStreamErrorUpdate(
                        type=WorkflowCopilotStreamMessageType.ERROR,
                        error=none_chat_user_message,
                        failure_kind=failure_kind,
                        turn_id=turn_id,
                        narrative_summary=None,
                    )
                )

        # Single-element list used as a closure flag (mutable bool by reference).
        # The watcher sets [0] = True before issuing handler_task.cancel() so the
        # except CancelledError block can distinguish a user-driven cancel from
        # operational cancels (server shutdown / deploy drain) and only persist
        # a stop-report chat row in the user case.
        user_cancel_observed: list[bool] = [False]
        # Which gesture asked for it, when the client said; None for a legacy
        # client or a cancel that named no source.
        user_cancel_source: list[CopilotCancelSource | None] = [None]

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
            proposed_workflow_at_turn_start = chat.proposed_workflow
            chat_request.workflow_copilot_chat_id = chat.workflow_copilot_chat_id
            # Before any await that could raise into the recovery writer: that writer opens the row
            # with the product sender, so the caller's prose must already be gone.
            await _apply_diagnose_run_action(
                chat_request,
                organization_id=chat.organization_id,
                workflow_permanent_id=chat.workflow_permanent_id,
            )
            chat_request.audio_artifact_id = await _validate_copilot_audio_artifact_id(
                audio_artifact_id=chat_request.audio_artifact_id,
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )

            chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            prior_turn_outcome = _latest_assistant_turn_outcome(chat_messages)
            copilot_config = await _resolve_copilot_request_config(
                organization.organization_id,
                chat_request,
            )
            current_code_available = copilot_config.code_block_available
            effective_mode = "code" if copilot_config.effective_code_block_mode else "build"
            global_llm_context = _prior_global_llm_context(chat_messages)

            blockless_fallback = _blockless_submission_fallback(
                proposed_workflow=chat.proposed_workflow,
                submitted_workflow_yaml=chat_request.workflow_yaml,
            )
            if blockless_fallback is not None:
                chat_request.workflow_yaml = blockless_fallback

            if chat_request.product_action == "test_end_to_end":
                # The button posts a structured action, so the message is the server's own
                # receipt line rather than client prose the turn would have to interpret.
                chat_request.message = TEST_END_TO_END_TURN_MESSAGE
                pending_proposal_yaml = _prior_copilot_workflow_yaml(
                    proposed_workflow=chat.proposed_workflow,
                    persisted_workflow_yaml=None,
                )
                # The action runs a definition end to end with real side effects, so it may only ever
                # run the server's own pending proposal. Falling through would execute whatever YAML
                # the caller sent, unreviewed.
                if pending_proposal_yaml is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No pending proposal to test end to end.",
                    )
                chat_request.workflow_yaml = pending_proposal_yaml

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
            persisted_workflow_yaml = _run_grant_workflow_yaml(original_workflow)

            _ensure_copilot_workflow_yaml(
                chat_request,
                original_workflow,
                persisted_workflow_yaml=persisted_workflow_yaml,
            )

            prior_copilot_workflow_yaml = _prior_copilot_workflow_yaml(
                proposed_workflow=chat.proposed_workflow,
                persisted_workflow_yaml=persisted_workflow_yaml,
            )
            prior_executed_block_fingerprints = _assistant_execution_receipts(chat_messages)
            proposal_receipts = parse_execution_receipts(
                chat.proposed_workflow.get("_copilot_tested_block_fingerprints")
                if isinstance(chat.proposed_workflow, dict)
                else None
            )
            for label, fingerprints in proposal_receipts.items():
                prior_executed_block_fingerprints.setdefault(label, set()).update(fingerprints)

            llm_api_handler = await _resolve_copilot_agent_handler(
                chat_request.workflow_permanent_id, organization.organization_id
            )
            raw_secret_safety_handler = await resolve_raw_secret_safety_handler(
                chat_request.workflow_permanent_id,
                organization.organization_id,
            )

            api_key = request.headers.get("x-api-key")
            if not api_key:
                api_key = await app.AGENT_FUNCTION.resolve_org_api_key(organization.organization_id)

            if not api_key:
                LOG.warning(
                    "Copilot cannot resolve an org API token; refusing to start the agent",
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
                        failure_kind="configuration",
                        turn_id=turn_id,
                        narrative_summary=None,
                    )
                )
                return

            # Zero-based turn ordinal. The current user message has not been
            # appended to chat_messages at this point, so ``sum(...)`` already
            # counts only prior user turns and equals the index of the
            # about-to-start turn.
            turn_index = sum(1 for m in chat_messages if m.sender in TURN_OPENER_SENDERS)

            # Prefer the FE-submitted yaml — canonical still has 0 blocks
            # mid-iteration before Accept and would mis-classify the chip.
            prior_block_count: int | None = None
            submitted_count = _workflow_yaml_block_count(chat_request.workflow_yaml)
            if submitted_count > 0:
                prior_block_count = submitted_count
            elif original_workflow is not None and original_workflow.workflow_definition is not None:
                prior_block_count = len(original_workflow.workflow_definition.blocks or [])

            try:
                pending_user_message = await app.DATABASE.workflow_params.start_copilot_turn(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    pending_turn=CopilotPendingTurn(
                        turn_id=turn_id,
                        started_at=datetime.now(timezone.utc),
                        cancel_token=chat_request.cancel_token,
                        pre_turn_workflow=original_workflow.model_dump(mode="json"),
                        pre_turn_proposed_workflow=proposed_workflow_at_turn_start,
                        keep_pending_proposal=chat_request.keep_pending_proposal,
                        copilot_effective_mode=effective_mode,
                        copilot_code_available=current_code_available,
                        idempotency_digest=_copilot_idempotency_digest(
                            organization.organization_id,
                            chat.workflow_copilot_chat_id,
                            chat_request.idempotency_key,
                        ),
                    ),
                    # The semantic safety screen runs inside the agent guardrail. Persisting the
                    # literal before that screen would leave a cross-turn disclosure path if the
                    # dedicated classifier finds a secret outside deterministic redaction patterns.
                    user_message=UNSCREENED_MESSAGE_PLACEHOLDER,
                    audio_artifact_id=chat_request.audio_artifact_id,
                    sender=_turn_opener_sender(chat_request),
                )
            except DuplicateCopilotTurnError as exc:
                terminal_frame_emitted = True
                await stream.send(
                    WorkflowCopilotStreamErrorUpdate(
                        type=WorkflowCopilotStreamMessageType.ERROR,
                        error="That account selection is already being processed.",
                        turn_id=exc.turn_id,
                        narrative_summary=None,
                    )
                )
                return
            turn_started = True

            # Cancellation becomes user-actionable only after the durable safety
            # placeholder exists. This ordering closes the race where Stop could
            # otherwise persist the unscreened request while starting the turn.
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
                            user_cancel_source,
                        )
                    )

            async def persist_canonical_user_message(content: str) -> None:
                await app.DATABASE.workflow_params.replace_workflow_copilot_chat_message(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_message_id=pending_user_message.workflow_copilot_chat_message_id,
                    content=content,
                    global_llm_context=None,
                    turn_outcome=None,
                    narrative_payload=None,
                )

            with bind_copilot_session_id(chat.workflow_copilot_chat_id):
                agent_result = await run_copilot_agent(
                    stream=stream,
                    organization_id=organization.organization_id,
                    chat_request=chat_request,
                    chat_history=convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                    prior_user_messages=convert_to_history_messages(chat_messages),
                    global_llm_context=global_llm_context,
                    llm_api_handler=llm_api_handler,
                    raw_secret_safety_handler=raw_secret_safety_handler,
                    api_key=api_key,
                    config=copilot_config,
                    turn_index=turn_index,
                    turn_id=turn_id,
                    prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
                    prior_block_count=prior_block_count,
                    stored_completion_criteria=None,
                    prior_turn_outcome=prior_turn_outcome,
                    persist_canonical_user_message=persist_canonical_user_message,
                    persisted_workflow_yaml=persisted_workflow_yaml,
                    prior_executed_block_fingerprints=prior_executed_block_fingerprints,
                    eval_capture_case_id=(
                        request.headers.get("x-copilot-eval-case") if settings.ENV == "local" else None
                    ),
                    eval_mode=eval_mode,
                    eval_entrypoint_url=eval_entrypoint_url,
                    auto_accept=chat.auto_accept,
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
                # Nobody pressed Stop on a cancellation the user never asked for, so
                # that turn is recorded as interrupted rather than as their intent.
                await _persist_cancel_turn(
                    stream=stream,
                    chat=chat,
                    organization_id=organization.organization_id,
                    original_workflow=original_workflow,
                    user_message=chat_request.message,
                    agent_result=agent_result,
                    audio_artifact_id=chat_request.audio_artifact_id,
                    turn_id=turn_id,
                    keep_pending_proposal=chat_request.keep_pending_proposal,
                    user_row_already_persisted=turn_started,
                    sender=_turn_opener_sender(chat_request),
                    record_as_interrupted=not user_cancel_observed[0],
                    cancel_source=user_cancel_source[0],
                    effective_mode=effective_mode,
                    code_available=current_code_available,
                )
                terminal_frame_emitted = True
                capture_code_mode_opt_out_after_persist()
                LOG.info(
                    "Workflow copilot agent turn cancelled",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    user_cancel_observed=user_cancel_observed[0],
                    cancel_source=user_cancel_source[0],
                )
                return

            # Atomic finalisation — a late cancel that fires here cannot tear
            # the success-path writes apart mid-way (no half-written turn,
            # no duplicate user/AI rows).
            finalise_started = True
            await asyncio.shield(
                _finalise_normal_turn(
                    stream=stream,
                    chat=chat,
                    organization_id=organization.organization_id,
                    original_workflow=original_workflow,
                    chat_request=chat_request,
                    agent_result=agent_result,
                    turn_id=turn_id,
                    user_row_already_persisted=turn_started,
                )
            )
            terminal_frame_emitted = True
            capture_code_mode_opt_out_after_persist()
        except HTTPException as exc:
            if chat is not None and _should_restore_persisted_workflow(
                chat.auto_accept,
                agent_result,
            ):
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
                    failure_kind=_http_exception_failure_kind(exc),
                    turn_id=turn_id,
                    narrative_summary=None,
                )
            )
        except LLMProviderError as exc:
            await _recover_from_route_exception(
                exc,
                restore_log_context="LLMProviderError handler",
                translated_log_message="LLM provider error translated to recoverable workflow copilot reply",
                none_chat_log_message="LLM provider error (workflow copilot)",
                none_chat_user_message="Failed to process your request. Please try again.",
                failure_kind="provider",
            )
        except asyncio.CancelledError:
            if chat is not None and _should_restore_persisted_workflow(
                chat.auto_accept,
                agent_result,
            ):
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
                        keep_pending_proposal=chat_request.keep_pending_proposal,
                        prior_global_llm_context=global_llm_context,
                        user_row_already_persisted=turn_started,
                        cancel_source=user_cancel_source[0],
                        sender=_turn_opener_sender(chat_request),
                        effective_mode=effective_mode,
                        code_available=current_code_available,
                    )
                )
                terminal_frame_emitted = True
                LOG.info(
                    "Workflow copilot agent cancelled by user during pre-agent setup",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    cancel_source=user_cancel_source[0],
                )
                return
            else:
                # Operational cancel (worker shutdown, deploy drain). Don't manufacture
                # a stop-report chat row — chat history should not record an
                # operational cancel as user intent. When finalisation already started,
                # its shielded write owns this turn's row and wins; if it dies before
                # committing, the pending turn is left for reconcile-on-read to recover.
                LOG.info(
                    "Workflow copilot agent task cancelled (operational or post-finalisation)",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                    user_cancel_observed=user_cancel_observed[0],
                    finalise_started=finalise_started,
                )
                if chat is not None and turn_started and not finalise_started:
                    # Shielded so the row lands even though this await re-raises the
                    # cancellation immediately; nothing may follow it in this branch.
                    await asyncio.shield(
                        _persist_interrupted_turn(
                            chat,
                            turn_id,
                            facts=_interruption_facts(
                                chat,
                                original_workflow,
                                agent_result,
                                authored_edits_saved=None,
                            ),
                            user_message=chat_request.message,
                            user_row_already_persisted=turn_started,
                            sender=_turn_opener_sender(chat_request),
                            effective_mode=effective_mode,
                            code_available=current_code_available,
                        )
                    )
                raise
        except Exception as exc:
            await _recover_from_route_exception(
                exc,
                restore_log_context="generic-error handler",
                translated_log_message="Unexpected workflow copilot error translated to recoverable reply",
                none_chat_log_message="Unexpected error in workflow copilot",
                none_chat_user_message="An error occurred. Please try again.",
                failure_kind="server",
            )
        finally:
            if cancel_watcher is not None and not cancel_watcher.done():
                cancel_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cancel_watcher
            await _ensure_terminal_frame(stream, terminal_frame_emitted, turn_id=turn_id)
            if eval_mode == CopilotEvalMode.BROWSER_ABLATION and agent_result is not None:
                metadata = agent_result.browser_ablation_metadata
                browser_session_id = metadata.get("browser_session_id") if isinstance(metadata, dict) else None
                if isinstance(browser_session_id, str) and browser_session_id:
                    await close_browser_session_quietly(
                        organization.organization_id,
                        browser_session_id,
                        reason=BrowserSessionCloseReason.user_requested,
                    )

    return FastAPIEventSourceStream.create(request, stream_handler)


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


def _validated_eval_entrypoint_url(
    request: Request, chat_request: WorkflowCopilotChatRequest, organization: Organization
) -> str | None:
    """A silently dropped seed produces a benchmark run that looks seeded and never was, so every
    rejected condition raises instead of falling back to the default resolution path."""
    if chat_request.eval_entrypoint_url is None:
        return None
    eval_entrypoint_url = chat_request.eval_entrypoint_url
    if (
        not settings.WORKFLOW_COPILOT_ODYSSEYS_EVAL_INPUTS_ENABLED
        or organization.organization_id not in settings.WORKFLOW_COPILOT_ODYSSEYS_EVAL_ORGANIZATION_IDS
        or request.headers.get("x-copilot-eval") != "odysseys"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="eval_entrypoint_url is not accepted on this deployment",
        )
    try:
        parsed = urlparse(eval_entrypoint_url)
        accepted = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and not any(char.isspace() for char in eval_entrypoint_url)
            and (parsed.port is None or 0 < parsed.port < 65536)
            # Same host policy every other URL-accepting entry point applies, so a metadata or
            # RFC1918 host is refused here rather than only at the navigation guard. A local
            # fixture lane passes by listing its own host in ALLOWED_HOSTS, as it already must.
            and not is_blocked_host(parsed.hostname or "")
        )
    except ValueError:
        accepted = False
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="eval_entrypoint_url must be an http(s) URL",
        )
    return eval_entrypoint_url


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    eval_entrypoint_url = _validated_eval_entrypoint_url(request, chat_request, organization)
    raw_eval_mode = request.headers.get("x-copilot-eval-mode")
    if raw_eval_mode is not None:
        try:
            eval_mode = CopilotEvalMode(raw_eval_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported Copilot eval mode",
            ) from exc
        if (
            not settings.WORKFLOW_COPILOT_BROWSER_ABLATION_ENABLED
            or organization.organization_id not in settings.WORKFLOW_COPILOT_BROWSER_ABLATION_ORGANIZATION_IDS
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Copilot eval mode is disabled")
        return await _new_copilot_chat_post(
            request,
            chat_request,
            organization,
            eval_mode=eval_mode,
            eval_entrypoint_url=eval_entrypoint_url,
        )

    return await _new_copilot_chat_post(
        request,
        chat_request,
        organization,
        eval_entrypoint_url=eval_entrypoint_url,
    )


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


async def _restore_canonical_after_interrupted_turn(
    chat: WorkflowCopilotChat,
    organization_id: str,
    entry: CopilotPendingTurn,
) -> bool:
    """Undo an interrupted turn's canonical write, stashing the displaced draft as the proposal.

    Only fires while canonical still carries the fingerprint this turn recorded when it wrote.
    Anyone writing after it — another chat, a manual edit, a later turn — forfeits the rollback.
    Returns whether the rollback fired.
    """
    if entry.pre_turn_workflow is None or entry.canonical_write_fingerprint is None:
        return False
    current = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=chat.workflow_permanent_id,
        organization_id=organization_id,
    )
    if current is None:
        return False
    if workflow_content_fingerprint(current.model_dump(mode="json")) != entry.canonical_write_fingerprint:
        return False

    if not (entry.keep_pending_proposal and entry.pre_turn_proposed_workflow is not None):
        stashed_draft = current.model_dump(mode="json")
        await app.DATABASE.workflow_params.update_workflow_copilot_chat(
            organization_id=organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            proposed_workflow=stashed_draft,
        )
        chat.proposed_workflow = stashed_draft

    pre_turn_workflow = Workflow.model_validate(entry.pre_turn_workflow)
    # The snapshot was taken with model_dump, which masks cdp_connect_headers, so restoring it
    # verbatim would overwrite the live browser credentials with the mask string.
    pre_turn_workflow.cdp_connect_headers = merge_masked_headers(
        pre_turn_workflow.cdp_connect_headers, current.cdp_connect_headers
    )
    await _restore_workflow_definition(pre_turn_workflow, organization_id)
    return True


async def _recover_interrupted_copilot_turn(
    chat: WorkflowCopilotChat,
    organization_id: str,
    entry: CopilotPendingTurn,
) -> None:
    if await _assistant_row_exists_for_turn(chat, entry.turn_id):
        await _clear_pending_turn(chat, entry.turn_id)
        return

    try:
        canonical_rolled_back = await _restore_canonical_after_interrupted_turn(chat, organization_id, entry)
    except Exception:
        # Answering the turn here would strand canonical on the half-written draft
        # while history claims it was recovered; leave the marker for a later read.
        LOG.warning(
            "Canonical restore failed while recovering an interrupted copilot turn",
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            turn_id=entry.turn_id,
            exc_info=True,
        )
        return

    pre_turn_version = (entry.pre_turn_workflow or {}).get("version")
    await _persist_interrupted_turn(
        chat,
        entry.turn_id,
        facts=InterruptedTurnFacts(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            workflow_permanent_id=chat.workflow_permanent_id,
            workflow_version=pre_turn_version if isinstance(pre_turn_version, int) else None,
            authored_edits_saved=False if canonical_rolled_back else None,
        ),
        idempotency_digest=entry.idempotency_digest,
        effective_mode=entry.copilot_effective_mode,
        code_available=entry.copilot_code_available,
    )
    LOG.info(
        "Recovered an interrupted copilot turn on read",
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        turn_id=entry.turn_id,
    )


async def _reconcile_interrupted_copilot_turns(chat: WorkflowCopilotChat, organization_id: str) -> None:
    if not chat.pending_turns:
        return
    abandoned_before = datetime.now(timezone.utc) - timedelta(seconds=RECONCILE_ABANDON_AFTER_SECONDS)
    for entry in sorted(chat.pending_turns.values(), key=lambda item: _as_utc(item.started_at)):
        pending_questions = [item for item in entry.question_interactions if item.status == "pending"]
        if pending_questions:
            if question_wait_is_live(entry.question_heartbeat_at, datetime.now(timezone.utc)):
                continue
            for item in pending_questions:
                recorded = await app.DATABASE.workflow_params.interrupt_copilot_question(
                    organization_id, chat.workflow_copilot_chat_id, item.interaction_id, stale_only=True
                )
                if recorded is not None:
                    entry.question_interactions = [
                        recorded if prior.interaction_id == item.interaction_id else prior
                        for prior in entry.question_interactions
                    ]
        last_activity = max(
            [
                _as_utc(entry.started_at),
                *[item.resolved_at for item in entry.question_interactions if item.resolved_at is not None],
            ]
        )
        if last_activity > abandoned_before:
            continue
        claimed = await app.DATABASE.workflow_params.claim_pending_copilot_turn(
            organization_id=organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            turn_id=entry.turn_id,
            claim_before=abandoned_before,
        )
        if not claimed:
            continue
        try:
            await _recover_interrupted_copilot_turn(chat, organization_id, entry)
        except Exception:
            LOG.warning(
                "Failed to recover an interrupted copilot turn",
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                turn_id=entry.turn_id,
                exc_info=True,
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
        if any(
            item.status == "pending" for entry in chat.pending_turns.values() for item in entry.question_interactions
        ):
            chat.pending_turns = await app.DATABASE.workflow_params.refresh_copilot_question_client(
                organization.organization_id, chat.workflow_copilot_chat_id
            )
        await _reconcile_interrupted_copilot_turns(chat, organization.organization_id)
        chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
            chat.workflow_copilot_chat_id
        )
    else:
        chat_messages = []
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id if chat else None,
        question_interactions=list(
            {
                item.interaction_id: item
                for item in (
                    [
                        QuestionInteraction.model_validate(raw)
                        for message in chat_messages
                        if message.narrative_payload is not None
                        for raw in message.narrative_payload.get("questionInteractions", [])
                    ]
                    + [
                        item
                        for entry in (chat.pending_turns.values() if chat else [])
                        for item in entry.question_interactions
                    ]
                )
            }.values()
        ),
        pending_question_cancel_token=next(
            (
                entry.cancel_token
                for entry in (chat.pending_turns.values() if chat else [])
                if any(item.status == "pending" for item in entry.question_interactions)
            ),
            None,
        ),
        chat_history=convert_to_history_messages(chat_messages),
        proposed_workflow=chat.proposed_workflow if chat else None,
        auto_accept=chat.auto_accept if chat else None,
    )


@base_router.post("/workflow/copilot/question-response", include_in_schema=False)
async def workflow_copilot_question_response(
    question_response: WorkflowCopilotQuestionResponseRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> QuestionInteraction:
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization.organization_id, question_response.workflow_copilot_chat_id
    )
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown Copilot chat")
    response = QuestionResponse(
        answers=question_response.answers, text=question_response.text, skipped=question_response.skipped
    )

    async def resolve(*, preflight_only: bool = False) -> QuestionInteraction:
        try:
            return await app.DATABASE.workflow_params.resolve_copilot_question(
                organization.organization_id,
                chat.workflow_copilot_chat_id,
                question_response.interaction_id,
                response,
                preflight_only=preflight_only,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Unknown Copilot question") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    recorded = await resolve(preflight_only=True)
    if recorded.status == "resolved":
        return recorded
    if response.text is not None or any(answer.text is not None for answer in response.answers):
        handler = await resolve_raw_secret_safety_handler(chat.workflow_permanent_id, organization.organization_id)

        async def screen_text(text: str) -> str:
            safety = await _screen_raw_secret_safety(text, handler, organization_id=organization.organization_id)
            if safety.status == "blocked":
                raise HTTPException(
                    status_code=503, detail="The safety screen is unavailable. Please retry your answer."
                )
            return safety.canonical_user_message

        if response.text is not None:
            response.text = await screen_text(response.text)
        for answer in response.answers:
            if answer.text is not None:
                answer.text = await screen_text(answer.text)
    return await resolve()


@base_router.post("/workflow/copilot/cancel", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT)
async def workflow_copilot_cancel(
    cancel_request: WorkflowCopilotCancelRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    """Hard-cancel an in-progress workflow copilot turn.

    Sets a per-token Redis flag the SSE handler's cancel-watcher polls; the
    watcher cancels the handler task, propagating ``CancelledError`` into
    whichever ``await`` is currently parked (LLM chunk, browser action, DB
    write). Returns 503 when ``app.CACHE`` is absent — the FE Stop button
    still aborts client-side, but the backend can't signal the running handler.
    """
    cancelled_questions = False
    if cancel_request.workflow_copilot_chat_id:
        try:
            cancelled_questions = await app.DATABASE.workflow_params.cancel_copilot_questions(
                organization.organization_id, cancel_request.workflow_copilot_chat_id, cancel_request.cancel_token
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Unknown Copilot chat") from exc
    cache = getattr(app, "CACHE", None)
    if cache is None:
        if cancelled_questions:
            return
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
        # A client too old to name its gesture still has to set a truthy flag.
        cancel_request.source or "1",
        ex=COPILOT_CANCEL_TTL,
    )


@base_router.post(
    "/workflow/copilot/credential-response", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT
)
async def workflow_copilot_credential_response(
    response_request: WorkflowCopilotCredentialResponseRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    """Resume a turn paused on ``credential_required`` with the user's card response.

    The resume path is not authorized by org auth + ``turn_id`` alone: the caller
    must echo back the one-time ``resume_token`` and ``workflow_copilot_chat_id``
    from the frame, which ``resolve_credential_pause`` checks against a still-pending
    active-pause record before it writes the flag the paused loop polls. Returns
    503 when ``app.CACHE`` is absent, 422 when ``action="connected"`` omits a
    ``credential_id``, 404 when that ID doesn't resolve in this organization or no
    active pause matches, 403 on a bad token, and 409 once the pause is consumed.
    """
    cache = getattr(app, "CACHE", None)
    if cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential response not supported in this environment",
        )

    # Validate the resume token BEFORE the credential-id lookup below: org auth
    # alone doesn't authorize this turn, and checking token validity first avoids
    # using an unauthorized-for-this-turn caller's credential_id as a small
    # authenticated existence oracle (a bogus token still gets a real 404/not-found).
    try:
        await check_credential_pause_resumable(
            cache,
            organization_id=organization.organization_id,
            workflow_copilot_chat_id=response_request.workflow_copilot_chat_id,
            turn_id=response_request.turn_id,
            resume_token=response_request.resume_token,
        )
    except CredentialPauseRejection as rejection:
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)

    credential_id = response_request.credential_id
    if response_request.action == "connected":
        if not credential_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="credential_id is required when action is 'connected'",
            )
        existing = await app.DATABASE.credentials.get_credentials_by_ids(
            [credential_id], organization_id=organization.organization_id
        )
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Credential {credential_id} not found")

    try:
        await resolve_credential_pause(
            cache,
            organization_id=organization.organization_id,
            workflow_copilot_chat_id=response_request.workflow_copilot_chat_id,
            turn_id=response_request.turn_id,
            resume_token=response_request.resume_token,
            action=response_request.action,
            credential_id=credential_id,
        )
    except CredentialPauseRejection as rejection:
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail)


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

    try:
        yaml_request = _normalize_copilot_yaml(copilot_yaml)
        clears_headers = (
            "cdp_connect_headers" in yaml_request.model_fields_set and yaml_request.cdp_connect_headers is None
        )
        if "workflow_definition" in proposal:
            # The proposal carries resolved settings from earlier edits that its final YAML
            # may omit. Preserve explicit nulls too, rather than inheriting saved settings.
            resolved = Workflow.model_validate(proposal).model_dump(mode="json")
            if (
                "max_elapsed_time_minutes" not in yaml_request.model_fields_set
                and resolved["max_elapsed_time_minutes"] is None
            ):
                # Pre-upgrade proposals used null for an omitted limit; retain save-service inheritance.
                resolved.pop("max_elapsed_time_minutes")
            resolved.update(yaml_request.model_dump(exclude_unset=True))
            if "cdp_connect_headers" in yaml_request.model_fields_set:
                resolved["cdp_connect_headers"] = yaml_request.cdp_connect_headers
            yaml_request = WorkflowCreateYAMLRequest.model_validate(resolved)
        if clears_headers:
            # The save service uses an empty mapping to clear headers; None inherits them.
            yaml_request.cdp_connect_headers = {}

    except (yaml.YAMLError, ValidationError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Proposed copilot YAML is invalid: {e}",
        )

    new_workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization=organization,
        request=yaml_request,
        workflow_permanent_id=chat.workflow_permanent_id,
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
