from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import pydantic_core
import structlog
from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelResponse,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunErrorHandlerResult,
    RunHooks,
    Runner,
    ToolErrorFormatterArgs,
    ToolsToFinalOutputResult,
    TResponseInputItem,
    Usage,
)
from agents._tool_identity import build_function_tool_lookup_map, get_function_tool_lookup_key_for_call
from agents.models.interface import Model
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputRefusal
from openai.types.shared import Reasoning
from pydantic import JsonValue, ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.copilot_model_usage import copilot_usage_observer
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.cache_envelope import CacheableSystemInstructions
from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config
from skyvern.forge.sdk.copilot.model_telemetry import (
    CopilotLitellmModel,
    current_model_attempt_telemetry,
    model_attempt_telemetry_scope,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.executor.run_liveness import run_write_authority
from skyvern.forge.sdk.integrations.connected_accounts import (
    OrgConnection,
    org_connections_by_id,
    settable_connected_account_fields,
)
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import AgentStepToolCall, WorkflowRunBlock
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext, _is_platform_smtp_aws_key
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock
from skyvern.forge.sdk.workflow.models.agent_step_tools import (
    DEDICATED_BLOCK_TOOLS,
    FILES_DESCRIPTION,
    FINISH_DESCRIPTION,
    RUN_BLOCK_TYPES,
    RUN_TOOL_DESCRIPTIONS,
    BlockTool,
    block_description,
    run_block_description,
)
from skyvern.forge.sdk.workflow.models.block import Block, _validate_response_against_json_schema
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
    is_sensitive_workflow_parameter,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.forge.taskv3.handoff_redaction import sanitize_handoff_url, string_leaves
from skyvern.forge.taskv3.opaque_refs import _TOKEN_PREFIX, mask_opaque_urls
from skyvern.schemas.agent_step_tool_names import ALWAYS_OFFERED_TOOLS
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.secret_redaction import redact_secrets_from_text
from skyvern.webeye.actions.actions import UploadFileAction

if TYPE_CHECKING:
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

LOG = structlog.get_logger()
_MAX_TOOL_RESULT_CHARS = 20_000
_MAX_TRAIL_ARGUMENT_CHARS = 4_000
_MAX_TRAIL_BLOCK_CHARS = 64_000
_MAX_TRAIL_BYTES = 512 * 1024
_TRAIL_WRITE_SECONDS = 2
_MASK = "*****"
_Fact = str | list[dict[str, str]]

_INSTRUCTIONS = (
    "You are one step of an unattended workflow run; nobody can answer questions. "
    "Each tool other than finish runs a workflow block, reads this run's data or acts on this run's browser page, "
    "and returns its status, output and error. When you have the answer, call finish with it."
)
_FINISH_ONLY = "Only finish can run now. Call finish with the best answer the tool results support."


class _RunEnded(Exception):
    def __init__(self, status: BlockStatus) -> None:
        super().__init__(status)
        self.status = status


class _Refused(Exception):
    pass


@dataclass
class _RunState:
    label: str
    workflow_id: str
    workflow_run_id: str
    workflow_run_block_id: str
    organization_id: str | None
    browser_session_id: str | None
    run_context: WorkflowRunContext
    credential_keys: list[str]
    ignore_workflow_system_prompt: bool
    connected_accounts: list[OrgConnection] = field(default_factory=list)
    workflow_system_prompt: str | None = None
    tools: list[str] | None = None
    file_keys: list[str] = field(default_factory=list)
    # Kept out of run_context.parameters, which outlives this execution while child labels repeat in the next one.
    child_outputs: dict[str, OutputParameter] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    # The report each child block returned, by label, so read_run_data can page the text the model was shown.
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    answer: dict[str, Any] | None = None
    drained: bool = False
    # The SDK starts one response's tool calls as concurrent tasks even with parallel_tool_calls=False, and providers
    # still return several calls. They share the page, so each waits for its turn in the order the model wrote them.
    call_order: list[str] = field(default_factory=list)
    turn: asyncio.Condition = field(default_factory=asyncio.Condition)
    page: ScriptSkyvernPage | None = None
    task: Task | None = None
    step: Step | None = None
    # The tool_calls column holds every call a tool ran, even ones not recorded in calls, so only calls feeds billing.
    # One background task writes the newest snapshot, so a tool never waits on that write.
    trail: list[AgentStepToolCall] = field(default_factory=list)
    running: AgentStepToolCall | None = None
    pending_trail: list[AgentStepToolCall] | None = None
    trail_writer: asyncio.Task[None] | None = None


def _capped(text: str) -> str:
    if len(text) <= _MAX_TRAIL_ARGUMENT_CHARS:
        return text
    return f"{text[:_MAX_TRAIL_ARGUMENT_CHARS]}...[truncated]"


def _child_label(state: _RunState, block_type: BlockType) -> str:
    return f"{state.label}_{block_type.value}_{len(state.trail) + 1}"


def _publish_trail(state: _RunState) -> asyncio.Task[None]:
    state.pending_trail = [*state.trail, *([state.running] if state.running else [])]
    if state.trail_writer is None or state.trail_writer.done():
        state.trail_writer = asyncio.create_task(_write_trail(state))
    return state.trail_writer


def _stored_size(entry: AgentStepToolCall) -> int:
    # The column's json.dumps escapes non-ASCII, and the ", " between list items makes the sum exact.
    return len(json.dumps(entry.model_dump(mode="json", exclude_none=True))) + 2


async def _write_trail(state: _RunState) -> None:
    while (trail := state.pending_trail) is not None:
        state.pending_trail = None
        try:
            stored = list(trail)
            sizes = [_stored_size(entry) for entry in stored]
            for i, entry in enumerate(stored):
                if sum(sizes) <= _MAX_TRAIL_BYTES:
                    break
                stored[i] = entry.model_copy(update={"block": None, "arguments": ""})
                sizes[i] = _stored_size(stored[i])
            async with asyncio.timeout(_TRAIL_WRITE_SECONDS):
                await app.DATABASE.observer.update_workflow_run_block_tool_calls(
                    workflow_run_block_id=state.workflow_run_block_id,
                    organization_id=state.organization_id,
                    tool_calls=stored,
                )
        except Exception:
            LOG.warning(
                "Failed to write the agent step's tool calls",
                workflow_run_block_id=state.workflow_run_block_id,
                exc_info=True,
            )


async def _raise_if_run_ended(state: _RunState) -> None:
    run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=state.workflow_run_id, organization_id=state.organization_id
    )
    if run is not None and run.status.is_final():
        raise _RunEnded(BlockStatus(run.status.value))


def _running_entry(
    state: _RunState,
    tool: str,
    arguments: str,
    describe: Callable[[JsonValue], tuple[BlockType | None, JsonValue]] | None,
) -> AgentStepToolCall:
    block_type: BlockType | None = None
    try:
        shown = json.loads(arguments or "{}")
        if describe is not None:
            block_type, shown = describe(shown)
        displayed = _redacted_json(state, shown)
    except Exception:
        # Redacted before encoding: a secret the model already escaped would not match once escaped again.
        block_type, displayed = None, json.dumps(_scrubbed(state, arguments))
    return AgentStepToolCall(
        index=len(state.trail) + 1,
        tool=tool,
        status=BlockStatus.running,
        arguments=_capped(displayed),
        block_type=block_type,
        label=_child_label(state, block_type) if block_type else None,
        started_at=datetime.now(UTC),
    )


def _in_turn(
    state: _RunState,
    invoke: Callable[[str], Awaitable[str]],
    describe: Callable[[JsonValue], tuple[BlockType | None, JsonValue]] | None = None,
) -> Callable[[ToolContext[Any], str], Awaitable[str]]:
    async def ordered(ctx: ToolContext[Any], arguments: str) -> str:
        call_id = ctx.tool_call_id
        async with state.turn:
            await state.turn.wait_for(lambda: call_id not in state.call_order or state.call_order[0] == call_id)
            started = False
            status: BlockStatus | Literal["invalid_arguments"] = "invalid_arguments"
            try:
                await _raise_if_run_ended(state)
                try:
                    state.running = _running_entry(state, ctx.tool_name, arguments, describe)
                    started = True
                    _publish_trail(state)
                except Exception:
                    LOG.warning(
                        "Failed to show an agent step call",
                        workflow_run_block_id=state.workflow_run_block_id,
                        exc_info=True,
                    )
                if state.answer is not None:
                    status = BlockStatus.skipped
                    return "finish already returned this step's answer, so this call did not run."
                # The drain keeps every tool listed because Anthropic rejects replayed thinking once the tool list changes.
                if state.drained and ctx.tool_name != "finish":
                    status = BlockStatus.skipped
                    return _FINISH_ONLY
                return await invoke(arguments)
            except (asyncio.CancelledError, _RunEnded):
                status = BlockStatus.canceled
                raise
            except Exception:
                status = BlockStatus.failed
                raise
            finally:
                # A call no tool recorded (bad arguments, refused, or cut off by an exception) is shown but never billed.
                if started:
                    with contained_effect(
                        "agent step tool call entry", workflow_run_block_id=state.workflow_run_block_id
                    ):
                        if state.running is not None:
                            ended = state.running.model_copy(
                                update={"status": status, "finished_at": datetime.now(UTC)}
                            )
                            state.running = None
                            state.trail.append(ended)
                        _publish_trail(state)
                if call_id in state.call_order:
                    state.call_order.remove(call_id)
                state.turn.notify_all()

    return ordered


class _LoopHooks(RunHooks[Any]):
    # Canceling a run only writes its row, so the loop re-reads the run before every model call and tool.
    def __init__(self, state: _RunState, block_row: WorkflowRunBlock) -> None:
        self.state = state
        self.block_row = block_row
        # Per model call, so usage survives a run that raises and sums the retry and max-turns runs.
        self.usage = Usage()

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        await _raise_if_run_ended(self.state)
        tools = [
            {"name": tool.name, "description": tool.description, "parameters": tool.params_json_schema}
            for tool in agent.tools
            if isinstance(tool, FunctionTool)
        ]
        await self._store(
            ArtifactType.LLM_REQUEST, {"system_prompt": system_prompt, "tools": tools, "input": input_items}
        )

    async def on_llm_end(self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse) -> None:
        self.usage.add(response.usage)
        await self._store(ArtifactType.LLM_RESPONSE, {"output": response.output, "usage": response.usage})
        # A call the SDK cannot resolve to a tool never runs, so in the queue it would hold every call after it.
        # The SDK's own lookup key decides that, so a namespaced call is unresolved even when its bare name is offered.
        offered = build_function_tool_lookup_map([tool for tool in agent.tools if isinstance(tool, FunctionTool)])
        self.state.call_order = [
            item.call_id
            for item in response.output
            if isinstance(item, ResponseFunctionToolCall) and get_function_tool_lookup_key_for_call(item) in offered
        ]
        refusals = [
            part.refusal
            for item in response.output
            if isinstance(item, ResponseOutputMessage)
            for part in item.content
            if isinstance(part, ResponseOutputRefusal)
        ]
        stops = current_model_attempt_telemetry()
        # LiteLLM keeps a refusal that came with text as plain text; only the content_filter finish reason marks it.
        if not refusals and stops and stops.latest_stop_metadata.content_filter:
            refusals = ["The provider's content filter stopped the response."]
        if refusals:
            raise _Refused(" ".join(refusals))

    async def _store(self, artifact_type: ArtifactType, payload: dict[str, object]) -> None:
        try:
            data = json.dumps(pydantic_core.to_jsonable_python(payload)).encode("utf-8")
            await app.ARTIFACT_MANAGER.create_workflow_run_block_artifacts(
                workflow_run_block=self.block_row, artifacts=[(artifact_type, data)]
            )
        except Exception:
            LOG.warning(
                "Failed to store agent step model call",
                artifact_type=artifact_type,
                workflow_run_block_id=self.block_row.workflow_run_block_id,
                exc_info=True,
            )


def _scrubbed(state: _RunState, text: str) -> str:
    return redact_secrets_from_text(
        state.run_context.mask_platform_smtp_for_agent(text), state.run_context.agent_redactable_secret_values()
    )


def _redacted_json(state: _RunState, value: JsonValue) -> str:
    return redact_secrets_from_text(
        json.dumps(
            state.run_context.mask_platform_smtp_for_agent(value),
            default=lambda item: state.run_context.mask_platform_smtp_for_agent(str(item)),
        ),
        state.run_context.agent_redactable_secret_values(),
    )


def _record_call(
    state: _RunState,
    written: JsonValue,
    facts: dict[str, _Fact] | None = None,
    *,
    block: str | None = None,
    child_workflow_run_block_id: str | None = None,
    **call: str,
) -> None:
    arguments = _redacted_json(state, written)
    redacted = {
        key: _scrubbed(state, fact)
        if isinstance(fact, str)
        else [{k: _scrubbed(state, v) for k, v in row.items()} for row in fact]
        for key, fact in (facts or {}).items()
    }
    state.calls.append({**call, "arguments": arguments, **redacted})
    finished_at = datetime.now(UTC)
    try:
        state.trail.append(
            AgentStepToolCall.model_validate(
                {
                    **state.calls[-1],
                    "index": len(state.trail) + 1,
                    "arguments": _capped(arguments),
                    "started_at": state.running.started_at if state.running else finished_at,
                    "finished_at": finished_at,
                    "block": block if block is not None and len(block) <= _MAX_TRAIL_BLOCK_CHARS else None,
                    "child_workflow_run_block_id": child_workflow_run_block_id,
                }
            )
        )
    except Exception:
        LOG.warning(
            "Failed to show an agent step call", workflow_run_block_id=state.workflow_run_block_id, exc_info=True
        )
        # Keeping the started entry keeps the trail's length, which numbers the next child label.
        if state.running is not None:
            status = call.get("status")
            known = status in {*BlockStatus, "invalid_arguments"}
            state.trail.append(
                state.running.model_copy(update={"status": status if known else None, "finished_at": finished_at})
            )
    state.running = None


def resolve_agent_model(llm_key: str) -> tuple[str | Model, RunConfig]:
    model_name, copilot_run_config, _, _ = resolve_model_config(None, llm_key_override=llm_key)
    model = copilot_run_config.model
    # Copilot's RunConfig also carries session callbacks that read its chat context, and an input filter that rewrites
    # recent tool outputs. Without that filter this history only grows, so its newest tool output can be cached.
    if isinstance(model, CopilotLitellmModel):
        model.history_append_only = True
    return model_name, RunConfig(
        model=model,
        model_provider=copilot_run_config.model_provider,
        model_settings=copilot_run_config.model_settings,
        tracing_disabled=True,
    )


def _finish_tool(schema: dict[str, Any], state: _RunState) -> FunctionTool:
    async def invoke(arguments: str) -> str:
        try:
            answer = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            _record_call(state, arguments, tool="finish", status="invalid_arguments")
            return f"finish arguments are not valid JSON ({e}). Call finish again."
        error = _validate_response_against_json_schema(answer, schema, "finish")
        _record_call(state, answer, tool="finish", status="invalid_arguments" if error else "completed")
        if error:
            return f"{error.rstrip('.')}. Call finish again with arguments that match its schema."
        state.answer = answer
        return "Answer recorded."

    return FunctionTool(
        name="finish",
        description=FINISH_DESCRIPTION,
        params_json_schema=schema,
        on_invoke_tool=_in_turn(state, invoke),
        # Strict mode would make every optional property required.
        strict_json_schema=False,
    )


def _pseudo_fields(tool: BlockTool) -> set[str]:
    # A real YAML field of the same name wins: http_request has its own `files` dict, which must not become a name list.
    return {"credential", "files"} & set(tool.fields) - set(tool.yaml_class.model_fields)


def _arguments_schema(tool: BlockTool, state: _RunState) -> dict[str, Any]:
    schema = tool.yaml_class.model_json_schema()
    pseudo = _pseudo_fields(tool)
    params: dict[str, Any] = {
        "type": "object",
        "properties": {name: schema["properties"][name] for name in tool.fields if name not in pseudo},
        "required": [name for name in tool.fields if name in schema.get("required", [])],
        "additionalProperties": False,
    }
    if "credential" in pseudo:
        params["properties"]["credential"] = {"type": "string", "enum": state.credential_keys}
        params["required"].append("credential")
    if "files" in pseudo:
        params["properties"]["files"] = {"type": "array", "items": {"type": "string"}, "description": FILES_DESCRIPTION}
    account_fields = settable_connected_account_fields(tool.yaml_class, tool.fields)
    for name in account_fields:
        described = schema["properties"][name].get("description")
        params["properties"][name] = {
            "$ref": f"#/$defs/{_ACCOUNT_DEF}",
            **({"description": described} if described else {}),
        }
    # Only the definitions a kept field reaches; the rest describe fields the model never sees.
    defs = schema.get("$defs", {})
    used: dict[str, Any] = {}
    reached = json.dumps(params["properties"])
    while names := [name for name in defs if name not in used and f'"#/$defs/{name}"' in reached]:
        used |= {name: defs[name] for name in names}
        reached = json.dumps([defs[name] for name in names])
    if account_fields:
        used[_ACCOUNT_DEF] = {
            "type": "string",
            "description": f"The id of {_account_choices(state)}",
            **({"enum": [account.id for account in state.connected_accounts]} if state.connected_accounts else {}),
        }
    if used:
        params["$defs"] = used
    return params


_ACCOUNT_DEF = "ConnectedAccountId"


def _account_choices(state: _RunState) -> str:
    if not state.connected_accounts:
        return "a connected account, and the run's owner has no active connected accounts that could be read"
    choices = [{k: v for k, v in a.facts().items() if k != "state"} for a in state.connected_accounts]
    return f"one of the run's connected accounts: {json.dumps(choices)}"


def _yaml_arguments(tool: BlockTool, written: dict[str, Any], state: _RunState) -> dict[str, Any]:
    if unknown := sorted(set(written) - set(tool.arguments)):
        raise ValueError(f"Unknown arguments {unknown}; the allowed arguments are {list(tool.arguments)}.")
    arguments = dict(written)
    account_ids = {account.id for account in state.connected_accounts}
    for name in settable_connected_account_fields(tool.yaml_class, tool.fields):
        value = arguments.get(name)
        if value is not None and not (isinstance(value, str) and value in account_ids):
            raise ValueError(f"{name} must be the id of {_account_choices(state)}.")
    pseudo = _pseudo_fields(tool)
    parameter_keys: list[str] = []
    if "credential" in pseudo:
        credential = arguments.pop("credential", None)
        if credential not in state.credential_keys:
            raise ValueError(f"credential must be one of {state.credential_keys}.")
        parameter_keys.append(credential)
    files = arguments.pop("files", []) if "files" in pseudo else []
    if not isinstance(files, list) or not all(isinstance(name, str) and name in state.file_keys for name in files):
        raise ValueError(f"files must be names from {state.file_keys}.")
    parameter_keys += dict.fromkeys(files)
    if parameter_keys:
        arguments["parameter_keys"] = parameter_keys
    if tool.nested_agent:
        arguments |= {"engine": RunEngine.skyvern_v3, "engine_pinned": True}
    return arguments


def _dedicated_tool(block_type: BlockType, tool: BlockTool, state: _RunState) -> FunctionTool:
    name = tool.tool_name or block_type.value

    async def invoke(arguments: str) -> str:
        try:
            written = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return f"Arguments are not valid JSON ({e})."
        return await _run_child_block(name, block_type, tool, written, state)

    return FunctionTool(
        name=name,
        description=block_description(block_type, tool),
        params_json_schema=_arguments_schema(tool, state),
        on_invoke_tool=_in_turn(state, invoke, lambda written: (block_type, written)),
        strict_json_schema=False,
    )


def _run_block_schema(state: _RunState, block_types: list[str]) -> dict[str, Any]:
    branches: list[dict[str, Any]] = []
    # Each block's references read "#/$defs/X", which resolve against this tool's root, not the branch.
    defs: dict[str, Any] = {}
    for name in block_types:
        branch = _arguments_schema(RUN_BLOCK_TYPES[name][1], state)
        defs.update(branch.pop("$defs", {}))
        branches.append(branch)
    return {
        "type": "object",
        "properties": {
            "block_type": {"type": "string", "enum": block_types},
            "arguments": branches[0] if len(branches) == 1 else {"anyOf": branches},
        },
        "required": ["block_type", "arguments"],
        "additionalProperties": False,
        **({"$defs": defs} if defs else {}),
    }


def _run_block_tool(state: _RunState, block_types: list[str]) -> FunctionTool:
    async def invoke(arguments: str) -> str:
        try:
            call = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            call = None
        if not isinstance(call, dict) or call.get("block_type") not in block_types:
            return f"Pass block_type, one of {block_types}, and that block's arguments."
        block_type, tool = RUN_BLOCK_TYPES[call["block_type"]]
        return await _run_child_block("run_block", block_type, tool, call.get("arguments") or {}, state)

    def describe(call: JsonValue) -> tuple[BlockType | None, JsonValue]:
        if isinstance(call, dict) and call.get("block_type") in block_types:
            return RUN_BLOCK_TYPES[call["block_type"]][0], call.get("arguments") or {}
        return None, call

    return FunctionTool(
        name="run_block",
        description=run_block_description(block_types),
        params_json_schema=_run_block_schema(state, block_types),
        on_invoke_tool=_in_turn(state, invoke, describe),
        strict_json_schema=False,
    )


def _masked_text(report: dict[str, Any], run_context: WorkflowRunContext) -> str:
    data = run_context.mask_secrets_in_data(
        run_context.mask_platform_smtp_for_agent(report),
        excluded_secret_ids=run_context.agent_unmasked_platform_secret_ids(),
    )
    return redact_secrets_from_text(
        json.dumps(data, default=lambda value: run_context.mask_platform_smtp_for_agent(str(value))),
        run_context.agent_redactable_secret_values(),
    )


def _window(text: str, offset: int = 0, name: str | None = None) -> str:
    end = offset + _MAX_TOOL_RESULT_CHARS
    if offset == 0 and end >= len(text):
        return text
    notice = f"characters {offset} to {min(end, len(text))} of {len(text)}."
    if name is not None and end < len(text):
        notice += f" read_run_data(name={json.dumps(name)}, offset={end}) returns the next ones."
    return f"{text[offset:end]} ...[{notice}]"


def _template_literal(text: str) -> str:
    # Page AI renders this text as Jinja against the run's values, and returns a value in its place when the text
    # equals that value's key. A Jinja string literal renders back to exactly the text and equals no key.
    return "{{ " + json.dumps(text, ensure_ascii=False) + " }}"


def _run_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    run: Callable[..., Awaitable[Any]],
    state: _RunState,
) -> FunctionTool:
    async def invoke(arguments: str) -> str:
        try:
            written = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return f"Arguments are not valid JSON ({e})."
        try:
            state.run_context.reject_platform_smtp_references(written)
            report = {"status": "completed", "output": await run(state, **written), "error": None}
        except InvalidWorkflowDefinition:
            raise
        except Exception as e:
            report = {"status": "failed", "output": None, "error": f"{type(e).__name__}: {e}"}
        _record_call(state, written, tool=name, status=report["status"])
        # A read_run_data window is already masked text cut from a longer result.
        if name == "read_run_data" and report["status"] == "completed":
            return report["output"]
        return _window(_masked_text(report, state.run_context))

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        on_invoke_tool=_in_turn(state, invoke),
        strict_json_schema=False,
    )


async def _read_run_data(state: _RunState, name: str | None = None) -> object:
    run_context = state.run_context
    platform_keys = run_context.platform_smtp_parameter_keys()
    parameters = {
        key: parameter
        for key, parameter in run_context.parameters.items()
        if parameter.parameter_type != ParameterType.OUTPUT and key not in platform_keys
    }
    if name in run_context.workflow_run_outputs:
        outcome = run_context.get_block_outcome(name)
        return {"output": run_context.workflow_run_outputs[name], "outcome": asdict(outcome) if outcome else None}
    if name in parameters:
        return _MASK if is_sensitive_workflow_parameter(parameters[name]) else run_context.values.get(name)
    files = (
        await app.STORAGE.get_downloaded_files(state.organization_id, state.workflow_run_id)
        if state.organization_id
        else []
    )
    if name is None:
        return {
            "parameters": list(parameters),
            "blocks": {
                label: outcome.status if (outcome := run_context.get_block_outcome(label)) else None
                for label in run_context.workflow_run_outputs
            },
            "downloaded_files": [file.filename for file in files],
        }
    if file := next((file for file in files if file.filename == name), None):
        return file.model_dump(mode="json")
    raise ValueError(f"Nothing in this run is named {name!r}. Call read_run_data without a name for the names.")


async def _read_run_data_window(state: _RunState, name: str | None = None, offset: int = 0) -> str:
    report = state.results.get(name) if name is not None else None
    if report is None:
        report = {"status": "completed", "output": await _read_run_data(state, name), "error": None}
    text = _masked_text(report, state.run_context)
    if not isinstance(offset, int) or not 0 <= offset < len(text):
        raise ValueError(f"offset must be from 0 to {len(text) - 1} for this result.")
    return _window(text, offset, name)


@asynccontextmanager
async def _run_page(state: _RunState) -> AsyncIterator[ScriptSkyvernPage]:
    # Imported here because importing it at module level cycles through script_service.
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

    task, step = state.task, state.step
    if task is None or step is None:
        task = state.task = await app.DATABASE.tasks.create_task(
            url="",
            title=f"{state.label} page commands",
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            status=TaskStatus.running,
            organization_id=state.organization_id,
            workflow_run_id=state.workflow_run_id,
            attempt_number=app.WORKFLOW_CONTEXT_MANAGER.get_attempt_number(state.workflow_run_id),
        )
        step = state.step = await app.DATABASE.tasks.create_step(
            task_id=task.task_id,
            order=0,
            retry_index=0,
            organization_id=state.organization_id,
            status=StepStatus.running,
        )
        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=state.workflow_run_block_id,
            organization_id=state.organization_id,
            task_id=task.task_id,
        )
    # Page AI acts and records actions only with a task and step in the context, and child blocks leave their own.
    context = skyvern_context.ensure_context()
    previous = context.task_id, context.step_id, context.workflow_run_block_id
    context.task_id, context.step_id, context.workflow_run_block_id = (
        task.task_id,
        step.step_id,
        state.workflow_run_block_id,
    )
    try:
        if state.page is not None:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(state.workflow_run_id)
            if browser_state is None or await browser_state.get_working_page() is not state.page.page:
                state.page = None
        if state.page is None:
            state.page = await ScriptSkyvernPage.create(browser_session_id=state.browser_session_id)
        yield state.page
    finally:
        context.task_id, context.step_id, context.workflow_run_block_id = previous


async def _page_goto(state: _RunState, url: str) -> dict[str, str]:
    async with _run_page(state) as page:
        await page.goto(_template_literal(url))
        return {"url": page.page.url}


async def _page_click(state: _RunState, prompt: str) -> dict[str, str]:
    async with _run_page(state) as page:
        await page.click(prompt=prompt, ai="proactive")
        return {"url": page.page.url}


async def _page_fill(state: _RunState, prompt: str, value: str) -> dict[str, str]:
    async with _run_page(state) as page:
        await page.fill(prompt=prompt, value=value, ai="proactive")
        return {"url": page.page.url}


async def _page_extract(
    state: _RunState, prompt: str, schema: dict[str, Any] | None = None
) -> dict[str, Any] | list | str | None:
    # The page's AI has no block label, so an omitted system prompt resolves to the run-wide one.
    opt_out: dict[str, str | None] = (
        {"system_prompt": None}
        if state.ignore_workflow_system_prompt
        else {"system_prompt": state.workflow_system_prompt}
        if state.workflow_system_prompt is not None
        else {}
    )
    async with _run_page(state) as page:
        return await page.extract(prompt=_template_literal(prompt), schema=schema, **opt_out)


_TEXT = {"type": "string"}
_RUN_TOOLS: tuple[tuple[str, dict[str, Any], list[str], Callable[..., Awaitable[Any]]], ...] = (
    ("read_run_data", {"name": _TEXT, "offset": {"type": "integer", "minimum": 0}}, [], _read_run_data_window),
    ("page_goto", {"url": _TEXT}, ["url"], _page_goto),
    ("page_click", {"prompt": _TEXT}, ["prompt"], _page_click),
    ("page_fill", {"prompt": _TEXT, "value": _TEXT}, ["prompt", "value"], _page_fill),
    ("page_extract", {"prompt": _TEXT, "schema": {"type": "object"}}, ["prompt"], _page_extract),
)
if {name for name, *_ in _RUN_TOOLS} != set(RUN_TOOL_DESCRIPTIONS):
    raise RuntimeError("_RUN_TOOLS and RUN_TOOL_DESCRIPTIONS must name the same tools.")


def _offers(state: _RunState, name: str) -> bool:
    return name in ALWAYS_OFFERED_TOOLS or name in (state.tools or ())


def _run_block_types(state: _RunState) -> list[str]:
    return [name for name in RUN_BLOCK_TYPES if _offers(state, name)]


def _tools(state: _RunState) -> list[FunctionTool]:
    block_types = _run_block_types(state)
    return [
        *(
            _dedicated_tool(block_type, tool, state)
            for name, (block_type, tool) in DEDICATED_BLOCK_TOOLS.items()
            if _offers(state, name) and ("credential" not in _pseudo_fields(tool) or state.credential_keys)
        ),
        *([_run_block_tool(state, block_types)] if block_types else []),
        *(
            _run_tool(name, RUN_TOOL_DESCRIPTIONS[name], properties, required, run, state)
            for name, properties, required, run in _RUN_TOOLS
            if _offers(state, name)
        ),
    ]


def _tool_not_found_formatter(
    state: _RunState, block_types: list[str], names: list[str]
) -> Callable[[ToolErrorFormatterArgs[Any]], str | None]:
    def format_error(args: ToolErrorFormatterArgs[Any]) -> str | None:
        if args.kind != "tool_not_found":
            return None
        LOG.warning(
            "Agent step model called a tool it was not given",
            label=state.label,
            tool_name=args.tool_name,
            workflow_run_id=state.workflow_run_id,
            workflow_run_block_id=state.workflow_run_block_id,
        )
        if args.tool_name in block_types:
            return (
                f'{args.tool_name} is a block type; call run_block with block_type="{args.tool_name}" '
                "and its arguments."
            )
        return f"There is no tool named {args.tool_name}. The tools are {names}."

    return format_error


async def _run_child_block(
    tool_name: str, block_type: BlockType, tool: BlockTool, written: JsonValue, state: _RunState
) -> str:
    label = _child_label(state, block_type)
    now = datetime.now(UTC)
    output_parameter = OutputParameter(
        output_parameter_id=str(uuid.uuid4()),
        key=f"{label}_output",
        workflow_id=state.workflow_id,
        created_at=now,
        modified_at=now,
    )
    report: dict[str, Any]
    facts: dict[str, _Fact] = {}
    block: str | None = None
    child_block_id: str | None = None
    try:
        if not isinstance(written, dict):
            raise ValueError("arguments must be a JSON object.")
        state.run_context.reject_platform_smtp_references(written)
        block_yaml = tool.yaml_class(
            **_yaml_arguments(tool, written, state),
            label=label,
            ignore_workflow_system_prompt=state.ignore_workflow_system_prompt,
        )
        # The send-email converter adds the platform SMTP parameters to this dict.
        parameters = {**state.child_outputs, **state.run_context.parameters, output_parameter.key: output_parameter}
        child = block_yaml_to_block(block_yaml, parameters)  # type: ignore[arg-type]
    except InvalidWorkflowDefinition:
        raise
    except Exception as e:
        report = {
            "status": "invalid_arguments",
            "output": None,
            # Pydantic's text repeats the whole input, including fields the model never wrote, and a docs URL.
            "error": "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'arguments'}: {error['msg']}"
                for error in e.errors(include_url=False, include_input=False)
            )
            if isinstance(e, ValidationError)
            else str(e),
            "argument_schema": _arguments_schema(tool, state),
        }
    else:
        block = _redacted_json(state, block_yaml.model_dump(mode="json", exclude_none=True))
        # A run resolves its secret parameters when it starts, before the converter added these.
        temporary_smtp_keys = [
            parameter.key
            for parameter in parameters.values()
            if isinstance(parameter, AWSSecretParameter)
            and _is_platform_smtp_aws_key(parameter.aws_key)
            and not state.run_context.has_parameter(parameter.key)
        ]
        try:
            for parameter in parameters.values():
                if not isinstance(parameter, AWSSecretParameter):
                    continue
                if _is_platform_smtp_aws_key(parameter.aws_key) and state.run_context.has_parameter(parameter.key):
                    if block_type == BlockType.SEND_EMAIL and not state.run_context._platform_smtp_only_usage(
                        parameter, child
                    ):
                        raise InvalidWorkflowDefinition(
                            "Platform-managed secrets are restricted to platform email delivery."
                        )
                elif not state.run_context.has_parameter(parameter.key):
                    await state.run_context.register_aws_secret_parameter_value(parameter, platform_smtp_block=child)
            page_url = await _page_url_at_start(state) if tool.nested_agent else None
            report, child_block_id = await _execute_child(child, written, state)
            if page_url is not None:
                facts["page_url_at_start"] = page_url
            if "files" in tool.fields:
                attempts = await _upload_attempts(state, child_block_id, written.get("files") or [])
                if attempts is not None:
                    facts["upload_attempts"] = attempts
            # Ahead of output, so the window that cuts a long output keeps them.
            report = {"status": report["status"], **facts, **report}
            if (
                report["status"] == BlockStatus.completed
                and state.run_context.has_value(output_parameter.key)
                and output_parameter.key not in state.run_context.parameters
            ):
                state.child_outputs[output_parameter.key] = output_parameter
                state.file_keys.append(output_parameter.key)
        finally:
            for key in temporary_smtp_keys:
                state.run_context.parameters.pop(key, None)
                state.run_context.values.pop(key, None)
    _record_call(
        state,
        written,
        facts,
        block=block,
        child_workflow_run_block_id=child_block_id,
        tool=tool_name,
        block_type=block_type.value,
        label=label,
        status=report["status"],
    )
    state.results[label] = report
    return _window(_masked_text(report, state.run_context), 0, label)


async def _execute_child(child: Block, written: dict[str, Any], state: _RunState) -> tuple[dict[str, Any], str | None]:
    child._agent_smtp_isolation = True
    child.mark_fields_prerendered(written)
    # Children write their own LLM cost to their own rows.
    with copilot_usage_observer(None):
        result = await child.execute_safe(
            workflow_run_id=state.workflow_run_id,
            parent_workflow_run_block_id=state.workflow_run_block_id,
            organization_id=state.organization_id,
            browser_session_id=state.browser_session_id,
        )
    report = {"status": result.status, "output": result.output_parameter_value, "error": result.failure_reason}
    return report, result.workflow_run_block_id


def _url_fact(url: str) -> str:
    if url == "about:blank" or url.startswith(_TOKEN_PREFIX):
        return url
    return sanitize_handoff_url(url) or "unparseable url"


async def _page_url_at_start(state: _RunState) -> str | None:
    try:
        browser_state = app.BROWSER_MANAGER.get_for_workflow_run(state.workflow_run_id)
        page = await browser_state.get_working_page(prune_excess_pages=False) if browser_state else None
        return _url_fact(page.url) if page else None
    except Exception:
        LOG.warning("Could not read the page url before a nested agent", label=state.label, exc_info=True)
        return None


async def _upload_attempts(
    state: _RunState, workflow_run_block_id: str | None, files: list[str]
) -> list[dict[str, str]] | None:
    try:
        if workflow_run_block_id is None:
            return None
        run_block = await app.DATABASE.observer.get_workflow_run_block(
            workflow_run_block_id, organization_id=state.organization_id
        )
        if run_block.task_id is None:
            return None
        actions = await app.DATABASE.tasks.get_task_actions_hydrated(
            run_block.task_id, organization_id=state.organization_id
        )
        handed = {key: state.run_context.values.get(key) for key in files}
        attempts = []
        for action in actions:
            if not isinstance(action, UploadFileAction):
                continue
            # Task V3 stores the file argument as its model wrote it, so a handed signed URL is its opaque_url_ token.
            parameter = next(
                (
                    key
                    for key, value in handed.items()
                    if mask_opaque_urls({key: value}).resolve(action.file_url) in string_leaves(value)
                ),
                None,
            )
            named = {"parameter": parameter} if parameter else {"source": _url_fact(action.file_url)}
            attempts.append({**named, "status": action.status.value})
        return attempts
    except Exception:
        LOG.warning("Could not read a nested agent's uploads", label=state.label, exc_info=True)
        return None


def _credential_keys(parameters: list[PARAMETER_TYPE]) -> list[str]:
    return [
        parameter.key
        for parameter in parameters
        if parameter.parameter_type.is_login_credential()
        or (isinstance(parameter, WorkflowParameter) and parameter.workflow_parameter_type.is_credential_type())
    ]


async def _connected_accounts(organization_id: str | None, workflow_run_id: str) -> list[OrgConnection]:
    """The accounts the run's owner may use, which every block tool's connected-account argument chooses from."""
    if organization_id is None:
        return []
    workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id, organization_id)
    connections = await org_connections_by_id(organization_id, workflow_run.created_by)
    # An author access setting on the step would narrow this list here.
    return [connection for connection in connections if connection.active]


async def run_agent_step(
    block: AgentStepBlock,
    llm_key: str,
    prompt: str,
    workflow_run_id: str,
    workflow_run_block_id: str,
    organization_id: str | None,
    browser_session_id: str | None,
    block_row: WorkflowRunBlock,
) -> BlockResult:
    run_context = block.get_workflow_run_context(workflow_run_id)
    state = _RunState(
        label=block.label,
        workflow_id=block.output_parameter.workflow_id,
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        organization_id=organization_id,
        browser_session_id=browser_session_id,
        run_context=run_context,
        credential_keys=_credential_keys(block.parameters),
        file_keys=[
            key
            for key, parameter in run_context.parameters.items()
            if isinstance(parameter, WorkflowParameter)
            and parameter.workflow_parameter_type == WorkflowParameterType.FILE_URL
        ],
        ignore_workflow_system_prompt=block.ignore_workflow_system_prompt,
        workflow_system_prompt=block.workflow_system_prompt,
        tools=block.tools,
    )
    if any(
        _offers(state, name) and settable_connected_account_fields(tool.yaml_class, tool.fields)
        for name, (_, tool) in {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}.items()
    ):
        state.connected_accounts = await _connected_accounts(organization_id, workflow_run_id)
    effort = LLMConfigRegistry.get_config(llm_key).reasoning_effort
    model, run_config = resolve_agent_model(llm_key)
    finish = _finish_tool(block.data_schema, state)
    tools = [*_tools(state), finish]
    run_config = replace(
        run_config,
        tool_not_found_behavior="return_error_to_model",
        tool_error_formatter=_tool_not_found_formatter(state, _run_block_types(state), [tool.name for tool in tools]),
    )
    agent = Agent(
        name=block.label,
        # LiteLLM's chat-to-Responses bridge drops the served service tier, which leaves GPT calls unpriced;
        # Copilot's cacheable instructions take its direct Responses path, which keeps it.
        # web_search needs today's date, which follows the cached prefix and stays fixed for the run.
        instructions=CacheableSystemInstructions(
            _INSTRUCTIONS,
            f"\n\nCurrent UTC datetime: {datetime.now(UTC).isoformat(timespec='seconds')}"
            + (f"\n\n{block.workflow_system_prompt}" if block.workflow_system_prompt else ""),
            cache_namespace=workflow_run_block_id,
        ),
        tools=tools,
        model=model,
        # Without reasoning set, the SDK drops Claude's thinking blocks from later turns and Anthropic rejects them.
        # model_construct because Claude keys can pin "max", which openai's Reasoning type does not list.
        model_settings=ModelSettings(parallel_tool_calls=False, reasoning=Reasoning.model_construct(effort=effort)),
        tool_use_behavior=lambda _ctx, _results: ToolsToFinalOutputResult(
            is_final_output=state.answer is not None, final_output=state.answer
        ),
    )
    hooks = _LoopHooks(state, block_row)
    costs: list[float] = []
    stop_reason, status = "completed", BlockStatus.completed
    failure_reason: str | None = None

    async def drain(history: list[TResponseInputItem], reason: str) -> RunErrorHandlerResult:
        nonlocal stop_reason
        stop_reason = reason
        state.drained = True
        with suppress(MaxTurnsExceeded):
            await Runner.run(
                agent,
                [*history, {"role": "user", "content": _FINISH_ONLY}],
                max_turns=1,
                run_config=run_config,
                hooks=hooks,
            )
        return RunErrorHandlerResult(final_output=state.answer)

    def output(stop_reason: str) -> dict[str, Any]:
        usage = hooks.usage
        return {
            "output": state.answer,
            "stop_reason": stop_reason,
            "tool_calls": state.calls,
            "usage": {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.input_tokens_details.cached_tokens,
                "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
            },
            "model": {"llm_key": llm_key, "reasoning_effort": effort},
        }

    async def save(value: dict[str, Any]) -> None:
        try:
            if organization_id:
                await app.DATABASE.observer.increment_workflow_run_block_llm_cost(
                    workflow_run_block_id=workflow_run_block_id, organization_id=organization_id, amount=sum(costs)
                )
            await block.record_output_parameter_value(run_context, workflow_run_id, value)
            # Shielded so a timeout during the normal exit does not cancel the model-call uploads still in flight.
            try:
                await asyncio.shield(app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([workflow_run_block_id]))
            except Exception:
                LOG.warning(
                    "Failed to upload agent step model calls",
                    workflow_run_block_id=workflow_run_block_id,
                    exc_info=True,
                )
        finally:
            try:
                await app.AGENT_FUNCTION.post_agent_step_execution(
                    organization_id,
                    workflow_run_id,
                    workflow_run_block_id,
                    decisions=sum(1 for call in state.calls if call["tool"] != "finish"),
                )
            finally:
                # The display-only trail goes last, so cost, output and billing never wait on it.
                with contained_effect("agent step final tool calls", workflow_run_block_id=workflow_run_block_id):
                    # One write may be in flight ahead of this final one.
                    async with asyncio.timeout(2 * _TRAIL_WRITE_SECONDS):
                        await asyncio.shield(_publish_trail(state))

    try:
        with copilot_usage_observer(lambda event: costs.append(event.cost or 0.0)), model_attempt_telemetry_scope():
            result = await Runner.run(
                agent,
                prompt,
                max_turns=block.max_turns,
                run_config=run_config,
                hooks=hooks,
                error_handlers={"max_turns": lambda data: drain(data.run_data.history, "max_turns")},
            )
            # The SDK takes a plain-text reply as the run's final output, so the max_turns handler never sees it.
            if state.answer is None and not state.drained:
                await drain(result.to_input_list(), "no_tool_calls")
    except asyncio.CancelledError:
        # The run's elapsed timeout cancels the block, and the run's cascade then sets the row, task and step statuses.
        await asyncio.shield(save(output("run_ended")))
        raise
    except _Refused as e:
        stop_reason, status, failure_reason = "refused", BlockStatus.failed, f"The model refused: {e}"
    except Exception as e:
        # A hook raises _RunEnded as is; the SDK re-raises it from a tool hook as UserError("Error running tool ...").
        ended = e if isinstance(e, _RunEnded) else e.__cause__
        if isinstance(ended, _RunEnded):
            stop_reason, status = "run_ended", ended.status
            failure_reason = f"The workflow run became {status} while the agent block ran."
        else:
            stop_reason, status, failure_reason = "error", BlockStatus.failed, f"{type(e).__name__}: {e}"
    else:
        if state.answer is None:
            status = BlockStatus.failed
            failure_reason = (
                f"The agent block used its {block.max_turns} turns without a valid finish."
                if stop_reason == "max_turns"
                else "The agent block ended without calling finish."
            )

    value = output(stop_reason)
    try:
        await save(value)
    finally:
        if state.task and state.step:
            await app.ARTIFACT_MANAGER.flush_step_archive(state.step.step_id)
            await app.DATABASE.tasks.update_step(
                task_id=state.task.task_id,
                step_id=state.step.step_id,
                organization_id=organization_id,
                write_authority_for_run=run_write_authority,
                status={BlockStatus.completed: StepStatus.completed, BlockStatus.failed: StepStatus.failed}.get(
                    status, StepStatus.canceled
                ),
            )
            await app.DATABASE.tasks.update_task(
                task_id=state.task.task_id, organization_id=organization_id, status=TaskStatus(status.value)
            )

    return await block.build_block_result(
        success=status == BlockStatus.completed,
        failure_reason=failure_reason
        and redact_secrets_from_text(failure_reason, run_context.agent_redactable_secret_values()),
        output_parameter_value=value,
        status=status,
        workflow_run_block_id=workflow_run_block_id,
        organization_id=organization_id,
    )
