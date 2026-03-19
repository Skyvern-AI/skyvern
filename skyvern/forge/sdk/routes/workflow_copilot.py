import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import Depends, HTTPException, Request, status
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.routes.run_blocks import DEFAULT_LOGIN_PROMPT
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
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
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
)

WORKFLOW_KNOWLEDGE_BASE_PATH = Path("skyvern/forge/prompts/skyvern/workflow_knowledge_base.txt")
CHAT_HISTORY_CONTEXT_MESSAGES = 10

LOG = structlog.get_logger()


@dataclass(frozen=True)
class RunInfo:
    block_label: str | None
    block_type: str
    block_status: str | None
    failure_reason: str | None
    html: str | None


async def _get_debug_artifact(organization_id: str, workflow_run_id: str) -> Artifact | None:
    artifacts = await app.DATABASE.get_artifacts_for_run(
        run_id=workflow_run_id, organization_id=organization_id, artifact_types=[ArtifactType.VISIBLE_ELEMENTS_TREE]
    )
    return artifacts[0] if isinstance(artifacts, list) and artifacts else None


async def _get_debug_run_info(organization_id: str, workflow_run_id: str | None) -> RunInfo | None:
    if not workflow_run_id:
        return None

    blocks = await app.DATABASE.get_workflow_run_blocks(
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
) -> tuple[str, Workflow | None, str | None]:
    chat_history_text = _format_chat_history(chat_history)

    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")

    llm_prompt = prompt_engine.load_prompt(
        template="workflow-copilot",
        workflow_knowledge_base=workflow_knowledge_base,
        workflow_yaml=chat_request.workflow_yaml or "",
        user_message=chat_request.message,
        chat_history=chat_history_text,
        global_llm_context=global_llm_context or "",
        current_datetime=datetime.now(timezone.utc).isoformat(),
        debug_run_info=debug_run_info_text,
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
        llm_prompt_len=len(llm_prompt),
    )
    llm_api_handler = (
        await get_llm_handler_for_prompt_type("workflow-copilot", chat_request.workflow_permanent_id, organization_id)
        or app.LLM_API_HANDLER
    )
    llm_start_time = time.monotonic()
    llm_response = await llm_api_handler(
        prompt=llm_prompt,
        prompt_name="workflow-copilot",
        organization_id=organization_id,
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

        return user_response, updated_workflow, global_llm_context
    elif action_type == "REPLY":
        return user_response, None, global_llm_context
    elif action_type == "ASK_QUESTION":
        return user_response, None, global_llm_context
    else:
        LOG.error(
            "Unknown action type from LLM",
            organization_id=organization_id,
            action_type=action_type,
        )
        return "I received your request but I'm not sure how to help. Could you rephrase?", None, None


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
    llm_prompt = prompt_engine.load_prompt(
        template="workflow-copilot",
        workflow_knowledge_base=workflow_knowledge_base,
        workflow_yaml=workflow_yaml,
        user_message=f"Workflow YAML parsing failed, please fix it: {failure_reason}",
        chat_history=_format_chat_history(new_chat_history),
        global_llm_context=global_llm_context or "",
        current_datetime=datetime.now(timezone.utc).isoformat(),
        debug_run_info=debug_run_info_text,
    )
    llm_start_time = time.monotonic()
    llm_response = await llm_api_handler(
        prompt=llm_prompt,
        prompt_name="workflow-copilot",
        organization_id=organization_id,
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

    Recursively repairs nested ForLoopBlockYAML.loop_blocks at all depths.
    Mutates *blocks* in place.
    """
    if len(blocks) <= 1:
        # Still recurse into loop_blocks even for single-block lists
        for block in blocks:
            if isinstance(block, ForLoopBlockYAML) and block.loop_blocks:
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

    # Phase 3: recursively repair nested ForLoopBlockYAML.loop_blocks.
    for block in blocks:
        if isinstance(block, ForLoopBlockYAML) and block.loop_blocks:
            _repair_next_block_label_chain(block.loop_blocks)


def _process_workflow_yaml(
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    workflow_yaml: str,
) -> Workflow:
    parsed_yaml = yaml.safe_load(workflow_yaml)

    # Fixing trivial common LLM mistakes
    workflow_definition = parsed_yaml.get("workflow_definition", None)
    if workflow_definition:
        blocks = workflow_definition.get("blocks", [])
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


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    async def stream_handler(stream: EventSourceStream) -> None:
        LOG.info(
            "Workflow copilot chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            message=chat_request.message,
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        try:
            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Processing...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            if chat_request.workflow_copilot_chat_id:
                chat = await app.DATABASE.get_workflow_copilot_chat_by_id(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                if not chat:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
                if chat_request.workflow_permanent_id != chat.workflow_permanent_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong workflow permanent ID")
            else:
                chat = await app.DATABASE.create_workflow_copilot_chat(
                    organization_id=organization.organization_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                )

            chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(
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

            if await stream.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected before LLM call",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            user_response, updated_workflow, updated_global_llm_context = await copilot_call_llm(
                stream,
                organization.organization_id,
                chat_request,
                convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                global_llm_context,
                debug_run_info_text,
            )

            if await stream.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected after LLM call",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            if updated_workflow and chat.auto_accept is not True:
                await app.DATABASE.update_workflow_copilot_chat(
                    organization_id=chat.organization_id,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    proposed_workflow=updated_workflow.model_dump(mode="json"),
                )

            await app.DATABASE.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.USER,
                content=chat_request.message,
            )

            assistant_message = await app.DATABASE.create_workflow_copilot_chat_message(
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
                )
            )
        except HTTPException as exc:
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
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="An error occurred. Please try again.",
                )
            )

    return FastAPIEventSourceStream.create(request, stream_handler)


@base_router.get("/workflow/copilot/chat-history", include_in_schema=False)
async def workflow_copilot_chat_history(
    workflow_permanent_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotChatHistoryResponse:
    latest_chat = await app.DATABASE.get_latest_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if latest_chat:
        chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(latest_chat.workflow_copilot_chat_id)
    else:
        chat_messages = []
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=latest_chat.workflow_copilot_chat_id if latest_chat else None,
        chat_history=convert_to_history_messages(chat_messages),
        proposed_workflow=latest_chat.proposed_workflow if latest_chat else None,
        auto_accept=latest_chat.auto_accept if latest_chat else None,
    )


@base_router.post(
    "/workflow/copilot/clear-proposed-workflow", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT
)
async def workflow_copilot_clear_proposed_workflow(
    clear_request: WorkflowCopilotClearProposedWorkflowRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    updated_chat = await app.DATABASE.update_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_copilot_chat_id=clear_request.workflow_copilot_chat_id,
        proposed_workflow=None,
        auto_accept=clear_request.auto_accept,
    )
    if not updated_chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")


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
        parsed_yaml = yaml.safe_load(request.workflow_definition_yaml)
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
