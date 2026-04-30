import asyncio
import contextlib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog
import yaml
from fastapi import Depends, HTTPException, Request, status
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from skyvern.config import settings
from skyvern.constants import DEFAULT_LOGIN_PROMPT
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write
from skyvern.forge.sdk.copilot.context import AgentResult
from skyvern.forge.sdk.copilot.output_utils import truncate_output
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotApplyProposedWorkflowRequest,
    WorkflowCopilotCancelRequest,
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
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

LOG = structlog.get_logger()


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


async def _ensure_terminal_frame(stream: EventSourceStream, already_emitted: bool) -> None:
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
                )
            )
        )
    except BaseException:
        pass


def _should_restore_persisted_workflow(auto_accept: bool | None, agent_result: object | None) -> bool:
    """Return True when a persisted draft should be rolled back.

    SKY-9143: when the agent decided not to ship a proposal this turn
    (``updated_workflow is None``) but ``_update_workflow`` already committed
    a YAML to ``workflow_definition``, we must restore the original even under
    ``auto_accept=True`` — otherwise an unverified edit becomes the live
    workflow silently.
    """
    if not bool(getattr(agent_result, "workflow_was_persisted", False)):
        return False
    if getattr(agent_result, "updated_workflow", None) is None:
        return True
    return auto_accept is not True


async def _persist_cancel_turn(
    stream: EventSourceStream,
    chat: Any,
    organization_id: str,
    original_workflow: Workflow | None,
    user_message: str,
    agent_result: AgentResult | None,
) -> None:
    """Persist a cancelled turn and emit a terminal SSE error frame.

    Pass the agent's ``AgentResult`` for cancels during the agent run so
    rollback uses the same ``workflow_was_persisted`` source of truth as
    the success path; pass ``None`` for pre-agent cancels.
    """
    if agent_result is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
        await asyncio.shield(_restore_workflow_definition(original_workflow, organization_id))
    await asyncio.shield(
        app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            sender=WorkflowCopilotChatSender.USER,
            content=user_message,
        )
    )
    await asyncio.shield(
        app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
            organization_id=chat.organization_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            sender=WorkflowCopilotChatSender.AI,
            content="Cancelled by user.",
        )
    )
    # Best-effort: client may have already aborted, in which case stream.send
    # silently drops. The optimistic FE bubble already shows the cancellation.
    with contextlib.suppress(Exception):
        await stream.send(
            WorkflowCopilotStreamErrorUpdate(
                type=WorkflowCopilotStreamMessageType.ERROR,
                error="Cancelled by user.",
            )
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
        await _restore_workflow_definition(original_workflow, organization_id)

    if chat.auto_accept is not True:
        if updated_workflow:
            proposed_data = updated_workflow.model_dump(mode="json")
            if agent_result.workflow_yaml:
                proposed_data["_copilot_yaml"] = agent_result.workflow_yaml
            await app.DATABASE.workflow_params.update_workflow_copilot_chat(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                proposed_workflow=proposed_data,
            )
        elif (
            restored or getattr(agent_result, "clear_proposed_workflow", False)
        ) and chat.proposed_workflow is not None:
            # Null any previously-persisted proposed_workflow so a
            # page reload does not resurrect a stale Accept/Reject
            # card next to an assistant message that just explained
            # why no verified proposal is available. Covers:
            # * feasibility-gate fast-path clarifications, and
            # * SKY-9143 strict-gate turns where a mid-turn draft was
            #   rolled back (``restored=True``).
            await app.DATABASE.workflow_params.update_workflow_copilot_chat(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                proposed_workflow=None,
            )

    await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        sender=WorkflowCopilotChatSender.USER,
        content=chat_request.message,
    )

    assistant_message = await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
        organization_id=chat.organization_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
        sender=WorkflowCopilotChatSender.AI,
        content=user_response,
        global_llm_context=updated_global_llm_context,
    )

    await stream.send(
        WorkflowCopilotStreamResponseUpdate(
            type=WorkflowCopilotStreamMessageType.RESPONSE,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            message=user_response,
            updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
            response_time=assistant_message.created_at,
            total_tokens=getattr(agent_result, "total_tokens", None),
            response_type=getattr(agent_result, "response_type", "REPLY"),
        )
    )


async def _restore_workflow_definition(original_workflow: Workflow | None, organization_id: str) -> None:
    """Roll the workflow back to ``original_workflow``.

    Unconditional restore helper. Callers must first gate this with
    ``_should_restore_persisted_workflow`` so success, disconnect, and exception
    paths all apply the same rollback rule: only restore when the user did not
    opt into auto-accept AND the agent loop actually persisted a mid-request
    draft.
    """
    if not original_workflow:
        return
    try:
        # Forward attribution so rollback reverts it alongside the definition.
        await app.WORKFLOW_SERVICE.update_workflow_definition(
            workflow_id=original_workflow.workflow_id,
            organization_id=organization_id,
            title=original_workflow.title,
            description=original_workflow.description,
            workflow_definition=original_workflow.workflow_definition,
            created_by=original_workflow.created_by,
            edited_by=original_workflow.edited_by,
        )
    except Exception:
        LOG.warning(
            "Failed to restore original workflow",
            workflow_id=original_workflow.workflow_id,
            exc_info=True,
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
    llm_api_handler = (
        await get_llm_handler_for_prompt_type("workflow-copilot", chat_request.workflow_permanent_id, organization_id)
        or app.LLM_API_HANDLER
    )
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
        llm_workflow_yaml = action_data.get("workflow_yaml", "")
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
            corrected_workflow_yaml = await _auto_correct_workflow_yaml(
                llm_api_handler=llm_api_handler,
                organization_id=organization_id,
                user_response=user_response,
                workflow_yaml=llm_workflow_yaml,
                chat_history=chat_history,
                global_llm_context=global_llm_context,
                debug_run_info_text=debug_run_info_text,
                error=e,
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
        workflow_definition = parsed_yaml.get("workflow_definition", None)
        if workflow_definition:
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
        persist_browser_session=workflow_yaml_request.persist_browser_session or False,
        model=workflow_yaml_request.model,
        max_screenshot_scrolls=workflow_yaml_request.max_screenshot_scrolls,
        extra_http_headers=workflow_yaml_request.extra_http_headers,
        run_with=workflow_yaml_request.run_with,
        ai_fallback=workflow_yaml_request.ai_fallback,
        cache_key=workflow_yaml_request.cache_key,
        run_sequentially=workflow_yaml_request.run_sequentially,
        sequential_key=workflow_yaml_request.sequential_key,
        created_at=now,
        modified_at=now,
    )


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

        original_workflow: Workflow | None = None
        chat = None
        agent_result: AgentResult | None = None
        terminal_frame_emitted = False
        cancel_watcher: asyncio.Task[None] | None = None
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

            if chat_request.workflow_copilot_chat_id:
                chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                if not chat:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
                if chat_request.workflow_permanent_id != chat.workflow_permanent_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong workflow permanent ID")
            else:
                chat = await app.DATABASE.workflow_params.create_workflow_copilot_chat(
                    organization_id=organization.organization_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                )

            chat_request.workflow_copilot_chat_id = chat.workflow_copilot_chat_id

            chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            global_llm_context = None
            for message in reversed(chat_messages):
                if message.global_llm_context is not None:
                    global_llm_context = message.global_llm_context
                    break

            if chat.proposed_workflow and chat.proposed_workflow.get("_copilot_yaml"):
                chat_request.workflow_yaml = chat.proposed_workflow["_copilot_yaml"]

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

            llm_api_handler = (
                await get_llm_handler_for_prompt_type(
                    "workflow-copilot", chat_request.workflow_permanent_id, organization.organization_id
                )
                or app.LLM_API_HANDLER
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
                    )
                )
                return

            security_rules = app.AGENT_FUNCTION.get_copilot_security_rules()

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
                    security_rules=security_rules,
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
                )
                terminal_frame_emitted = True
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
        except HTTPException as exc:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                await _restore_workflow_definition(original_workflow, organization.organization_id)
            terminal_frame_emitted = True
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error=exc.detail,
                )
            )
        except LLMProviderError as exc:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                await _restore_workflow_definition(original_workflow, organization.organization_id)
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
                )
            )
        except asyncio.CancelledError:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                await asyncio.shield(_restore_workflow_definition(original_workflow, organization.organization_id))
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
                    )
                )
                terminal_frame_emitted = True
                LOG.info(
                    "Workflow copilot v2 cancelled by user during pre-agent setup",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
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
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                await _restore_workflow_definition(original_workflow, organization.organization_id)
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
                )
            )
        finally:
            if cancel_watcher is not None and not cancel_watcher.done():
                cancel_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cancel_watcher
            await _ensure_terminal_frame(stream, terminal_frame_emitted)

    return FastAPIEventSourceStream.create(request, stream_handler)


COPILOT_V2_FLAG_KEY = "ENABLE_WORKFLOW_COPILOT_V2"


async def _should_use_copilot_v2(organization: Organization, workflow_permanent_id: str) -> bool:
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


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    if await _should_use_copilot_v2(organization, chat_request.workflow_permanent_id):
        return await _new_copilot_chat_post(request, chat_request, organization)

    async def stream_handler(stream: EventSourceStream) -> None:
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

            if chat_request.workflow_copilot_chat_id:
                chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                if not chat:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
                if chat_request.workflow_permanent_id != chat.workflow_permanent_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong workflow permanent ID")
            else:
                chat = await app.DATABASE.workflow_params.create_workflow_copilot_chat(
                    organization_id=organization.organization_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                )

            chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
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
            )

            assistant_message = await app.DATABASE.workflow_params.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.AI,
                content=user_response,
                global_llm_context=updated_global_llm_context,
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
            await _ensure_terminal_frame(stream, terminal_frame_emitted)

    return FastAPIEventSourceStream.create(request, stream_handler)


@base_router.get("/workflow/copilot/chat-history", include_in_schema=False)
async def workflow_copilot_chat_history(
    workflow_permanent_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotChatHistoryResponse:
    latest_chat = await app.DATABASE.workflow_params.get_latest_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if latest_chat:
        chat_messages = await app.DATABASE.workflow_params.get_workflow_copilot_chat_messages(
            latest_chat.workflow_copilot_chat_id
        )
    else:
        chat_messages = []
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=latest_chat.workflow_copilot_chat_id if latest_chat else None,
        chat_history=convert_to_history_messages(chat_messages),
        proposed_workflow=latest_chat.proposed_workflow if latest_chat else None,
        auto_accept=latest_chat.auto_accept if latest_chat else None,
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
            created_at=message.created_at,
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
