"""Copilot agent — multi-turn tool-use agent for workflow building.

Uses the OpenAI Agents SDK with LiteLLM for multi-provider LLM support.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.experimentation.llm_prompt_config import LLMAPIHandler
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

import structlog
import yaml
from pydantic import ValidationError

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tracing_setup import ensure_tracing_initialized, is_tracing_enabled
from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
)
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.workflow import Workflow

LOG = structlog.get_logger()

WORKFLOW_KNOWLEDGE_BASE_PATH = Path("skyvern/forge/prompts/skyvern/workflow_knowledge_base.txt")

MAX_TURNS = 25


@dataclass
class AgentResult:
    user_response: str
    updated_workflow: Workflow | None
    global_llm_context: str | None
    response_type: str = "REPLY"
    workflow_yaml: str | None = None
    workflow_was_persisted: bool = False


@dataclass
class CopilotContext(AgentContext):
    """Unified context for the copilot agent run.

    Extends AgentContext with enforcement state, tool tracking, and
    workflow state needed by the SDK-based agent loop.
    """

    # Enforcement state
    navigate_called: bool = False
    observation_after_navigate: bool = False
    navigate_enforcement_done: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    post_update_nudge_count: int = 0
    premature_completion_nudge_done: bool = False
    intermediate_nudge_count: int = 0
    user_message: str = ""

    # Tool tracking
    consecutive_tool_tracker: list[str] = field(default_factory=list)
    tool_activity: list[dict[str, Any]] = field(default_factory=list)

    # Workflow state
    last_workflow: Workflow | None = None
    last_workflow_yaml: str | None = None
    workflow_persisted: bool = False
    last_update_block_count: int | None = None
    last_test_ok: bool | None = None
    last_test_failure_reason: str | None = None
    failed_test_nudge_count: int = 0
    explore_without_workflow_nudge_count: int = 0
    last_failed_workflow_yaml: str | None = None


def _format_chat_history(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> str:
    if not chat_history:
        return ""
    lines = [f"{msg.sender}: {msg.content}" for msg in chat_history]
    return "\n".join(lines)


def _build_system_prompt(
    workflow_yaml: str,
    chat_history_text: str,
    global_llm_context: str,
    debug_run_info_text: str,
    tool_usage_guide: str,
) -> str:
    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    return prompt_engine.load_prompt(
        template="workflow-copilot-agent",
        workflow_knowledge_base=workflow_knowledge_base,
        workflow_yaml=workflow_yaml or "",
        chat_history=chat_history_text,
        global_llm_context=global_llm_context or "",
        current_datetime=datetime.now(timezone.utc).isoformat(),
        debug_run_info=debug_run_info_text,
        tool_usage_guide=tool_usage_guide,
    )


def _parse_final_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return {"type": "REPLY", "user_response": text}


def _build_tool_usage_guide(tool_names_and_descriptions: list[tuple[str, str]]) -> str:
    if not tool_names_and_descriptions:
        return ""
    return "\n".join(
        f"- **{name}** — {description or 'No description provided.'}"
        for name, description in tool_names_and_descriptions
    )


def _extract_final_text(result: RunResultStreaming) -> str:
    if result.final_output is not None:
        if isinstance(result.final_output, str):
            return result.final_output
        if hasattr(result.final_output, "model_dump"):
            return json.dumps(result.final_output.model_dump())
        return json.dumps(result.final_output)

    for item in reversed(result.new_items):
        if hasattr(item, "output") and isinstance(item.output, list):
            for part in item.output:
                text = None
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                elif hasattr(part, "type") and getattr(part, "type", None) == "text":
                    text = getattr(part, "text", "")
                if text:
                    return text
        if hasattr(item, "text") and item.text:
            return item.text
    return ""


def _normalize_failure_reason(failure_reason: str | None) -> str:
    if not failure_reason:
        return "The workflow test run failed."

    normalized = failure_reason.split("Call log:", 1)[0].strip()
    normalized = " ".join(normalized.split())
    if len(normalized) > 240:
        normalized = normalized[:237].rstrip() + "..."
    return normalized or "The workflow test run failed."


def _rewrite_failed_test_response(user_response: str, ctx: CopilotContext) -> str:
    if ctx.last_update_block_count is None or ctx.last_test_ok is not False:
        return user_response

    if ctx.last_update_block_count <= 0:
        draft_phrase = "a draft workflow"
    else:
        block_word = "block" if ctx.last_update_block_count == 1 else "blocks"
        draft_phrase = f"a draft workflow with {ctx.last_update_block_count} {block_word}"

    failure_summary = _normalize_failure_reason(ctx.last_test_failure_reason)
    return f"I created {draft_phrase} and tested it, but the test failed. Failure: {failure_summary}"


def _translate_to_agent_result(
    result: RunResultStreaming,
    ctx: CopilotContext,
    global_llm_context: str | None,
    chat_request: WorkflowCopilotChatRequest,
    organization_id: str,
) -> AgentResult:
    from skyvern.forge.sdk.copilot.tools import process_workflow_yaml

    text = _extract_final_text(result)
    if not text:
        text = '{"type": "REPLY", "user_response": "I\'m not sure how to help with that. Could you rephrase?"}'

    action_data = _parse_final_response(text)
    user_response = action_data.get("user_response") or "Done."

    last_workflow = ctx.last_workflow
    last_workflow_yaml = ctx.last_workflow_yaml

    if action_data.get("type") == "REPLACE_WORKFLOW":
        LOG.warning("Agent used inline REPLACE_WORKFLOW instead of update_workflow tool")
        workflow_yaml = action_data.get("workflow_yaml", "")
        if workflow_yaml:
            try:
                last_workflow = process_workflow_yaml(
                    workflow_id=chat_request.workflow_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    organization_id=organization_id,
                    workflow_yaml=workflow_yaml,
                )
            except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
                LOG.warning("Failed to process final workflow YAML", error=str(e))
                user_response = (
                    f"{user_response}\n\n"
                    f"(Note: The proposed workflow had a validation error: {str(e)[:200]}. "
                    f"Please ask me to fix it.)"
                )

    user_response = _rewrite_failed_test_response(str(user_response), ctx)

    if ctx.last_test_ok is False:
        last_workflow = None
        last_workflow_yaml = None

    resp_type = action_data.get("type", "REPLY")
    if resp_type not in ("REPLY", "ASK_QUESTION", "REPLACE_WORKFLOW"):
        resp_type = "REPLY"

    llm_context_raw = action_data.get("global_llm_context")
    if isinstance(llm_context_raw, dict):
        try:
            structured = StructuredContext.model_validate(llm_context_raw)
        except Exception:
            structured = StructuredContext.from_json_str(global_llm_context)
    elif isinstance(llm_context_raw, str):
        structured = StructuredContext.from_json_str(llm_context_raw)
    else:
        structured = StructuredContext.from_json_str(global_llm_context)
    structured.merge_turn_summary(ctx.tool_activity)
    enriched_context = structured.to_json_str()

    return AgentResult(
        user_response=str(user_response),
        updated_workflow=last_workflow,
        global_llm_context=enriched_context or None,
        response_type=resp_type,
        workflow_yaml=last_workflow_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
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
) -> AgentResult:
    try:
        from agents import Agent, trace
        from agents.exceptions import MaxTurnsExceeded
        from agents.mcp import MCPServerManager
    except ModuleNotFoundError as e:
        if e.name == "agents":
            LOG.error(
                "OpenAI Agents SDK dependency missing",
                error=str(e),
                workflow_permanent_id=chat_request.workflow_permanent_id,
            )
            return AgentResult(
                user_response=(
                    "Copilot backend is missing the OpenAI Agents SDK dependency. "
                    "Rebuild or redeploy the backend image so `openai-agents` is installed."
                ),
                updated_workflow=None,
                global_llm_context=global_llm_context,
                workflow_yaml=chat_request.workflow_yaml or None,
            )
        raise

    from skyvern.cli.mcp_tools import mcp as skyvern_mcp
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotClientDisconnectedError,
        CopilotTotalTimeoutError,
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

    ctx = CopilotContext(
        organization_id=organization_id,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_yaml=chat_request.workflow_yaml or "",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
        user_message=chat_request.message,
    )

    model_name, run_config, llm_key, supports_vision = resolve_model_config(llm_api_handler)
    ctx.supports_vision = supports_vision
    ensure_tracing_initialized()

    alias_map = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()

    mcp_server = SkyvernOverlayMCPServer(
        transport=skyvern_mcp,
        overlays=overlays,
        alias_map=alias_map,
        allowlist=frozenset(alias_map.values()),
        context_provider=lambda: ctx,
    )

    tool_info: list[tuple[str, str]] = [(tool.name, tool.description or "") for tool in NATIVE_TOOLS]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())

    chat_history_text = _format_chat_history(chat_history)
    tool_usage_guide = _build_tool_usage_guide(tool_info)
    system_prompt = _build_system_prompt(
        workflow_yaml=chat_request.workflow_yaml or "",
        chat_history_text=chat_history_text,
        global_llm_context=global_llm_context or "",
        debug_run_info_text=debug_run_info_text,
        tool_usage_guide=tool_usage_guide,
    )

    agent = Agent(
        name="workflow-copilot",
        instructions=system_prompt,
        tools=list(NATIVE_TOOLS),
        mcp_servers=[mcp_server],
        model=model_name,
    )

    user_message = chat_request.message

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
            },
        )

    chat_id = chat_request.workflow_copilot_chat_id or chat_request.workflow_permanent_id
    session = create_copilot_session(chat_id)
    try:
        with trace_context:
            try:
                async with MCPServerManager([mcp_server]) as manager:
                    agent.mcp_servers = list(manager.active_servers)
                    result = await run_with_enforcement(
                        agent=agent,
                        initial_input=user_message,
                        ctx=ctx,
                        stream=stream,
                        max_turns=MAX_TURNS,
                        hooks=CopilotRunHooks(ctx),
                        run_config=run_config,
                        session=session,
                    )
                return _translate_to_agent_result(
                    result,
                    ctx,
                    global_llm_context,
                    chat_request,
                    organization_id,
                )
            except (CopilotClientDisconnectedError, asyncio.CancelledError):
                LOG.info("Copilot client disconnected")
                return AgentResult(
                    user_response="Request cancelled.",
                    updated_workflow=ctx.last_workflow,
                    global_llm_context=global_llm_context,
                    workflow_yaml=ctx.last_workflow_yaml,
                    workflow_was_persisted=ctx.workflow_persisted,
                )
            except MaxTurnsExceeded:
                return AgentResult(
                    user_response="I've reached the maximum number of steps. Here's what I have so far.",
                    updated_workflow=ctx.last_workflow,
                    global_llm_context=global_llm_context,
                    workflow_yaml=ctx.last_workflow_yaml,
                    workflow_was_persisted=ctx.workflow_persisted,
                )
            except CopilotTotalTimeoutError:
                return AgentResult(
                    user_response="I ran out of time processing your request. Here's what I have so far.",
                    updated_workflow=ctx.last_workflow,
                    global_llm_context=global_llm_context,
                    workflow_yaml=ctx.last_workflow_yaml,
                    workflow_was_persisted=ctx.workflow_persisted,
                )
    except Exception as e:
        LOG.error("Copilot agent error", error=str(e), exc_info=True)
        return AgentResult(
            user_response=f"An error occurred: {str(e)[:300]}",
            updated_workflow=ctx.last_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=ctx.last_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
        )
    finally:
        session.close()
