from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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
from jsonschema import Draft202012Validator
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputRefusal
from openai.types.shared import Reasoning
from pydantic import JsonValue, ValidationError

from skyvern.config import settings
from skyvern.constants import EMBER_RUN_CREATOR
from skyvern.exceptions import ScrapingFailed, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.agents.skills import (
    EVERYONE_SKILL_LABEL,
    LOAD_SKILL_TOOL_NAME,
    READ_SKILL_RESOURCE_TOOL_NAME,
    SKILL_TOOL_NAMES,
    SkillLoadResult,
    SkillReadResult,
    SkillSession,
    render_skills_catalog,
)
from skyvern.forge.sdk.api.llm.agents_model import current_model_attempt_telemetry, model_attempt_telemetry_scope
from skyvern.forge.sdk.api.llm.agents_model_resolver import resolve_agents_model
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.model_usage import model_usage_observer
from skyvern.forge.sdk.api.llm.prompt_cache_envelope import CacheableSystemInstructions
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.executor.run_liveness import run_write_authority
from skyvern.forge.sdk.integrations.account_fit import block_fit, block_needs, connection_fit
from skyvern.forge.sdk.integrations.catalog import IntegrationAction, IntegrationEntry
from skyvern.forge.sdk.integrations.connected_accounts import (
    OrgConnection,
    org_connections_by_id,
    run_connection_ids,
    settable_connected_account_fields,
)
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import AgentStepToolCall, WorkflowRunBlock
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext, _is_platform_smtp_aws_key
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.agent_step_tools import (
    CATALOG_ACTION_TOOLS,
    DEDICATED_BLOCK_TOOLS,
    FILES_DESCRIPTION,
    FINISH_DESCRIPTION,
    READ_SKILL_RESOURCE_DESCRIPTION,
    RUN_BLOCK_TYPES,
    RUN_TOOL_DESCRIPTIONS,
    SAVED_BLOCK_ONLY_TOOLS,
    BlockTool,
    action_description,
    block_description,
    run_block_description,
)
from skyvern.forge.sdk.workflow.models.block import Block, WorkflowTriggerBlock, _validate_response_against_json_schema
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
from skyvern.schemas.agent_step_tool_names import (
    ALWAYS_OFFERED_TOOLS,
    CATALOG_TOOL_NAMES,
    LEGACY_AGENT_STEP_TOOL_NAMES,
    is_known_tool_name,
)
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, IntegrationActionBlockYAML
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.secret_redaction import redact_secrets_from_text
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, GotoUrlAction, UploadFileAction, reasoning_is_turn_scoped

if TYPE_CHECKING:
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
    from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock

LOG = structlog.get_logger()
_MAX_TOOL_RESULT_CHARS = 20_000
_MAX_TRAIL_ARGUMENT_CHARS = 4_000
_MAX_TRAIL_BLOCK_CHARS = 64_000
_MAX_TRAIL_BYTES = 512 * 1024
_TRAIL_WRITE_SECONDS = 2
_PAGE_TITLE_SECONDS = 2
_MASK = "*****"
_Fact = str | list[dict[str, str]]
_OPAQUE_TOKEN = re.compile(rf"{_TOKEN_PREFIX}[0-9a-f]{{8}}")

_INSTRUCTIONS = (
    "You are one step of an unattended workflow run; nobody can answer questions. "
    "Each tool other than finish runs a workflow block, reads this run's data or acts on this run's browser page, "
    "and returns its status, output and error. When you have the answer, call finish with it."
)
_FINISH_ONLY = "Only finish can run now. Call finish with the best answer the tool results support."
_SKILL_LIST_HEADER = "load_skill(name) returns a skill's instructions and file list."
_HIDDEN_SKILL_ARGUMENTS = json.dumps({"name": EVERYONE_SKILL_LABEL})


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
    # The owner has a Nango Gmail account, which only a saved block can use (ADR 0033 point 8).
    saved_block_gmail: bool = False
    # Catalog actions offered as tools, by tool name, each with the accounts that may run it.
    actions: dict[str, tuple[IntegrationEntry, IntegrationAction, list[OrgConnection]]] = field(default_factory=dict)
    workflow_system_prompt: str | None = None
    tools: list[str] | None = None
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
    skills: SkillSession | None = None
    # The exact text appended to the instructions, so stored model requests can swap it for the public list.
    skill_list: str | None = None


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
        block_type = None
        displayed = _HIDDEN_SKILL_ARGUMENTS if tool in SKILL_TOOL_NAMES else json.dumps(_scrubbed(state, arguments))
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
            data = json.dumps(_stored_view(self.state, pydantic_core.to_jsonable_python(payload))).encode("utf-8")
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


def _skill_call_name(arguments: JsonValue) -> JsonValue:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else None
    except json.JSONDecodeError:
        return None
    return parsed.get("name") if isinstance(parsed, dict) else None


def _stored_view(state: _RunState, data: JsonValue) -> JsonValue:
    """Hide everyone skills from a stored model call; data is a fresh copy, so it is edited in place."""
    session, skill_list = state.skills, state.skill_list
    if session is None or skill_list is None or not isinstance(data, dict):
        return data
    system_prompt = data.get("system_prompt")
    if isinstance(system_prompt, str):
        data["system_prompt"] = system_prompt.replace(
            skill_list, "\n".join([_SKILL_LIST_HEADER, *session.public_catalog_lines()])
        )
    items = [
        item
        for key in ("input", "output")
        if isinstance(listed := data.get(key), list)
        for item in listed
        if isinstance(item, dict)
    ]
    hidden_calls: set[str] = set()
    for item in items:
        if (
            item.get("type") == "function_call"
            and item.get("name") in SKILL_TOOL_NAMES
            and not session.shows_name(_skill_call_name(item.get("arguments")))
        ):
            item["arguments"] = _HIDDEN_SKILL_ARGUMENTS
            if isinstance(call_id := item.get("call_id"), str):
                hidden_calls.add(call_id)
    for item in items:
        if item.get("type") == "function_call_output" and item.get("call_id") in hidden_calls:
            item["output"] = _HIDDEN_SKILL_ARGUMENTS
    return data


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
    resolved = resolve_agents_model(llm_key, agent_name="agent_step")
    return resolved.model_name, RunConfig(
        model=resolved.model,
        model_provider=resolved.provider,
        model_settings=resolved.model_settings,
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
    accounts = _tool_accounts(tool, state) if account_fields else []
    # run_block merges every branch's $defs, so a list narrowed for one block type needs a name of its own.
    narrowed = bool(account_fields) and accounts != state.connected_accounts
    account_def = f"{_ACCOUNT_DEF}_{_block_type(tool).value}" if narrowed else _ACCOUNT_DEF
    for name in account_fields:
        described = schema["properties"][name].get("description")
        params["properties"][name] = {
            "$ref": f"#/$defs/{account_def}",
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
        used[account_def] = {
            "type": "string",
            "description": f"The id of {_account_choices(accounts, state, _tool_needs(tool, state))}",
            **({"enum": [account.id for account in accounts]} if accounts else {}),
        }
    if used:
        params["$defs"] = used
    return params


_ACCOUNT_DEF = "ConnectedAccountId"


def _block_type(tool: BlockTool) -> BlockType:
    return BlockType(tool.yaml_class.model_fields["block_type"].default)


def _tool_accounts(tool: BlockTool, state: _RunState) -> list[OrgConnection]:
    """The run's accounts minus those known to lack the permission this tool's block needs; unknown ones stay."""
    return [account for account in state.connected_accounts if block_fit(account, _block_type(tool)) != "no"]


def _tool_needs(tool: BlockTool, state: _RunState) -> list[str]:
    return block_needs(_block_type(tool), {account.provider for account in state.connected_accounts})


def _account_choices(accounts: list[OrgConnection], state: _RunState, needs: Sequence[str] = ()) -> str:
    if not accounts and state.connected_accounts:
        # Several scopes come from different providers' actions, so any one of them is enough.
        listed = needs[0] if len(needs) == 1 else f"one of {', '.join(needs)}"
        permission = f"the permission this block needs ({listed})" if needs else "the permission it needs"
        return (
            f"a connected account with {permission}, and none of the run's connected accounts has it; reconnect an "
            "account with that permission on the Integrations page"
        )
    if not accounts:
        return "a connected account, and no connected account is available to this run"
    choices = [{k: v for k, v in a.facts().items() if k != "state"} for a in accounts]
    return f"one of the run's connected accounts: {json.dumps(choices)}"


async def _yaml_arguments(
    tool: BlockTool, written: dict[str, Any], state: _RunState, output_key: str
) -> tuple[dict[str, Any], dict[str, FileInfo]]:
    """The block's YAML arguments, and the run files that reach it only as a parameter for this one call."""
    if unknown := sorted(set(written) - set(tool.arguments)):
        raise ValueError(f"Unknown arguments {unknown}; the allowed arguments are {list(tool.arguments)}.")
    arguments = dict(written)
    account_fields = settable_connected_account_fields(tool.yaml_class, tool.fields)
    accounts = _tool_accounts(tool, state) if account_fields else []
    account_ids = {account.id for account in accounts}
    for name in account_fields:
        value = arguments.get(name)
        if value is not None and not (isinstance(value, str) and value in account_ids):
            raise ValueError(f"{name} must be the id of {_account_choices(accounts, state, _tool_needs(tool, state))}.")
    pseudo = _pseudo_fields(tool)
    parameter_keys: list[str] = []
    if "credential" in pseudo:
        credential = arguments.pop("credential", None)
        if credential not in state.credential_keys:
            raise ValueError(f"credential must be one of {state.credential_keys}.")
        parameter_keys.append(credential)
    files = arguments.pop("files", []) if "files" in pseudo else []
    handed: dict[str, FileInfo] = {}
    if files != []:
        run_files = await _run_files(state)
        accepted = [*run_files, *state.child_outputs]
        if not isinstance(files, list) or not all(isinstance(name, str) and name in accepted for name in files):
            raise ValueError(f"files must be names from {accepted}.")
        for name in files:
            if name in state.child_outputs or state.run_context.has_parameter(name):
                continue
            if state.run_context.has_value(name) or name == output_key:
                raise ValueError(f"The file {name!r} can't be handed over: the run already has a value named {name!r}.")
            handed[name] = run_files[name]
    parameter_keys += dict.fromkeys(files)
    if parameter_keys:
        arguments["parameter_keys"] = parameter_keys
    if tool.nested_agent:
        arguments |= {"engine": RunEngine.skyvern_v3, "engine_pinned": True}
    return arguments, handed


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


def _run_block_schema(
    state: _RunState, block_types: list[str], table: Mapping[str, tuple[BlockType, BlockTool]] = RUN_BLOCK_TYPES
) -> dict[str, Any]:
    branches: list[dict[str, Any]] = []
    # Each block's references read "#/$defs/X", which resolve against this tool's root, not the branch.
    defs: dict[str, Any] = {}
    for name in block_types:
        branch = _arguments_schema(table[name][1], state)
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
            # A prompt written before a block type became catalog tools still names it; point to the new tool.
            renamed = LEGACY_AGENT_STEP_TOOL_NAMES.get(str(call.get("block_type"))) if isinstance(call, dict) else None
            if renamed in state.actions:
                return f"{call['block_type']} is now the tool {renamed}; call it directly with its own arguments."
            if renamed in CATALOG_TOOL_NAMES:
                return f"{call['block_type']} is now the tool {renamed}, which this run does not offer."
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
    describe: Callable[[JsonValue], tuple[BlockType | None, JsonValue]] | None = None,
) -> FunctionTool:
    async def invoke(arguments: str) -> str:
        try:
            written = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return f"Arguments are not valid JSON ({e})."
        try:
            state.run_context.reject_platform_smtp_references(written)
            if name in _PAGE_COMMANDS:
                report = await _page_report(state, name, run, written)
            else:
                report = {"status": "completed", "output": await run(state, **written), "error": None}
        except InvalidWorkflowDefinition:
            raise
        except Exception as e:
            report = {"status": "failed", "output": None, "error": _error_text(e)}
        _record_call(state, describe(written)[1] if describe else written, tool=name, status=report["status"])
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
        on_invoke_tool=_in_turn(state, invoke, describe),
        strict_json_schema=False,
    )


def _reserved_names(state: _RunState) -> frozenset[str]:
    return frozenset([*state.results, *state.child_outputs])


async def _run_files(state: _RunState) -> dict[str, FileInfo]:
    return await state.run_context.run_files(reserved=_reserved_names(state))


def _error_text(e: Exception) -> str:
    text = f"{type(e).__name__}: {e}"
    return f"{text} {e.reason}" if isinstance(e, ScrapingFailed) and e.reason else text


async def _page_report(
    state: _RunState, name: str, run: Callable[..., Awaitable[Any]], written: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    # A call the command can't take never reaches the page, so a page left from an earlier command isn't its page.
    try:
        inspect.signature(run).bind(state, **written)
    except TypeError as e:
        return {"status": "failed", "output": None, "error": _error_text(e)}
    status = "completed"
    output: JsonValue = None
    error: str | None = None
    scrape_url: str | None = None
    try:
        if name == "page_extract":
            output, scrape_url = await run(state, **written)
        else:
            await run(state, **written)
    except InvalidWorkflowDefinition:
        raise
    except Exception as e:
        status, error = "failed", _error_text(e)
    report: dict[str, JsonValue] = {"status": status}
    if name == "page_extract" and status == "completed":
        if scrape_url is not None:
            report["url"] = scrape_url
    elif state.page is not None:
        report["url"] = state.page.page.url
        if name != "page_extract" and (title := await _page_title(state, state.page)) is not None:
            report["title"] = title
    return {**report, "output": output, "error": error}


async def _page_title(state: _RunState, page: ScriptSkyvernPage) -> str | None:
    # The browser defers page evaluation while a main-frame navigation is pending, so a hung goto would hang this too.
    try:
        async with asyncio.timeout(_PAGE_TITLE_SECONDS):
            return await page.page.title()
    except Exception:
        LOG.warning("Could not read the page title after a page command", label=state.label, exc_info=True)
        return None


async def _read_run_data(state: _RunState, name: str | None = None) -> object:
    run_context = state.run_context
    platform_keys = run_context.platform_smtp_parameter_keys()
    file_keys = run_context.file_url_parameter_keys()
    parameters = {
        key: parameter
        for key, parameter in run_context.parameters.items()
        if parameter.parameter_type != ParameterType.OUTPUT and key not in platform_keys and key not in file_keys
    }
    if name in run_context.workflow_run_outputs:
        outcome = run_context.get_block_outcome(name)
        return {"output": run_context.workflow_run_outputs[name], "outcome": asdict(outcome) if outcome else None}
    if name in parameters:
        return _MASK if is_sensitive_workflow_parameter(parameters[name]) else run_context.values.get(name)
    files = await _run_files(state)
    if name is None:
        return {
            "parameters": list(parameters),
            "blocks": {
                label: outcome.status if (outcome := run_context.get_block_outcome(label)) else None
                for label in run_context.workflow_run_outputs
            },
            "files": list(files),
        }
    if file := files.get(name):
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
            page, state.page = state.page, None
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(state.workflow_run_id)
            if browser_state is not None and await browser_state.get_working_page() is page.page:
                state.page = page
        if state.page is None:
            state.page = await ScriptSkyvernPage.create(browser_session_id=state.browser_session_id)
        yield state.page
    finally:
        context.task_id, context.step_id, context.workflow_run_block_id = previous


async def _page_goto(state: _RunState, url: str) -> None:
    async with _run_page(state) as page:
        await page.goto(_template_literal(url), max_retries=1)


async def _page_click(state: _RunState, prompt: str) -> None:
    async with _run_page(state) as page:
        await page.click(prompt=prompt, ai="proactive")


async def _page_fill(state: _RunState, prompt: str, value: str) -> None:
    async with _run_page(state) as page:
        await page.fill(prompt=prompt, value=value, ai="proactive")


async def _page_extract(
    state: _RunState, prompt: str, schema: dict[str, Any] | None = None
) -> tuple[JsonValue, str | None]:
    # The page's AI has no block label, so an omitted system prompt resolves to the run-wide one.
    opt_out: dict[str, str | None] = (
        {"system_prompt": None}
        if state.ignore_workflow_system_prompt
        else {"system_prompt": state.workflow_system_prompt}
        if state.workflow_system_prompt is not None
        else {}
    )
    async with _run_page(state) as page:
        value = await page.extract(prompt=_template_literal(prompt), schema=schema, **opt_out)
        return value, _scrape_url(state, page)


def _scrape_url(state: _RunState, page: ScriptSkyvernPage) -> str | None:
    # Imported here for the same import cycle as ScriptSkyvernPage in _run_page.
    from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi

    # The extract re-scrapes into its page AI's scraped_page and gives that scrape's URL to its model.
    ai = page.page_ai
    if isinstance(ai, RealSkyvernPageAi):
        return ai.scraped_page.url
    LOG.warning("Page extract ran without a scraping page AI", label=state.label, ai=type(ai).__name__)
    return None


_TEXT = {"type": "string"}
_RUN_TOOLS: tuple[tuple[str, dict[str, Any], list[str], Callable[..., Awaitable[Any]]], ...] = (
    ("read_run_data", {"name": _TEXT, "offset": {"type": "integer", "minimum": 0}}, [], _read_run_data_window),
    ("page_goto", {"url": _TEXT}, ["url"], _page_goto),
    ("page_click", {"prompt": _TEXT}, ["prompt"], _page_click),
    ("page_fill", {"prompt": _TEXT, "value": _TEXT}, ["prompt", "value"], _page_fill),
    ("page_extract", {"prompt": _TEXT, "schema": {"type": "object"}}, ["prompt"], _page_extract),
)
_PAGE_COMMANDS = frozenset(name for name, *_ in _RUN_TOOLS) - {"read_run_data"}
if {name for name, *_ in _RUN_TOOLS} != set(RUN_TOOL_DESCRIPTIONS) - {LOAD_SKILL_TOOL_NAME}:
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
            _action_tool(name, entry, action, accounts, state)
            for name, (entry, action, accounts) in state.actions.items()
        ),
        *(
            _run_tool(name, RUN_TOOL_DESCRIPTIONS[name], properties, required, run, state)
            for name, properties, required, run in _RUN_TOOLS
            if _offers(state, name)
        ),
    ]


_INTEGRATION_ACTION_TOOL = BlockTool(IntegrationActionBlockYAML, ("integration_id", "action_id", "inputs"))


def _withheld_note(withheld: list[str], saved_block_gmail: bool = False) -> str:
    """Tells the model why listed catalog tools are missing, so it can say so instead of guessing."""
    saved_only = [name for name in withheld if name in SAVED_BLOCK_ONLY_TOOLS]
    nango_gmail = [
        name
        for name in withheld
        if saved_block_gmail and name in CATALOG_ACTION_TOOLS and CATALOG_ACTION_TOOLS[name][0].id == "gmail"
    ]
    # A listed name the step cannot offer at all, such as a saved block only one, is not told as an account problem.
    others = [name for name in withheld if name in CATALOG_ACTION_TOOLS and name not in nango_gmail]
    note = "".join(
        f"\n\n{name} runs only as a saved block, not from this step; if the task needs it, say that a saved "
        f"{entry.name}: {action.name} block must be placed before or after this step."
        for name in saved_only
        for entry, action in [SAVED_BLOCK_ONLY_TOOLS[name]]
    )
    note += "".join(
        f"\n\n{name} is not available in this run: the run owner's connected Gmail is a Nango account, which "
        "only a saved block can use, and no other account has the permission it needs. If the task needs it, say "
        f"that a saved "
        f"{entry.name}: {action.name} block must be placed before or after this step, or that a Google account "
        "with that permission must be connected."
        for name in nango_gmail
        for entry, action in [CATALOG_ACTION_TOOLS[name]]
    )
    if others:
        note += (
            "\n\nThese tools this step lists are not available in this run, usually because no connected account "
            "this run may use has the permission they need; if the task needs one, say so in your answer: "
            f"{', '.join(others)}."
        )
    return note


def _catalog_actions(state: _RunState) -> dict[str, tuple[IntegrationEntry, IntegrationAction, list[OrgConnection]]]:
    """The catalog actions the author listed, each with the run's accounts that are not known to lack its permission.
    An action no account can run is not offered."""
    actions = {}
    for name, (entry, action) in CATALOG_ACTION_TOOLS.items():
        if entry.status != "live" or not _offers(state, name):
            continue
        accounts = [
            account
            for account in state.connected_accounts
            if account.provider == entry.oauth_provider and connection_fit(account, action) != "no"
        ]
        if accounts:
            actions[name] = (entry, action, accounts)
    return actions


def _action_schema(action: IntegrationAction, accounts: list[OrgConnection], state: _RunState) -> dict[str, Any]:
    # A const input is fixed by the action (e.g. which email client), so the model never sets it.
    properties = {name: schema for name, schema in action.inputs.get("properties", {}).items() if "const" not in schema}
    # Built on the action's own schema, so any other keyword it uses (allOf, oneOf, if) still holds the model's call;
    # such a keyword must not name a fixed input, which the model never sees.
    return {
        **action.inputs,
        "type": "object",
        "properties": {
            **properties,
            "connection_id": {
                "type": "string",
                "enum": [account.id for account in accounts],
                "description": f"The id of {_account_choices(accounts, state, action.needs)}",
            },
        },
        "required": [*(name for name in action.inputs.get("required", []) if name in properties), "connection_id"],
        "additionalProperties": False,
    }


def _action_arguments_error(
    written: JsonValue,
    schema: dict[str, Any],
    validator: Draft202012Validator,
    accounts: list[OrgConnection],
    state: _RunState,
    needs: Sequence[str],
) -> str | None:
    if not isinstance(written, dict):
        return "arguments must be a JSON object."
    if written.get("connection_id") not in schema["properties"]["connection_id"]["enum"]:
        return f"connection_id must be the id of {_account_choices(accounts, state, needs)}."
    # An agent's text is never a template, so every value is held to the schema as written.
    errors = sorted(validator.iter_errors(written), key=lambda e: [str(p) for p in e.path])
    if not errors:
        return None
    error = errors[0]
    field = ".".join(str(part) for part in error.path) or "arguments"
    # These two messages name only keys; every other one repeats the value, which can be the model's whole input.
    if error.validator in ("required", "additionalProperties"):
        return f"{field}: {error.message}"
    return f"{field} must satisfy {error.validator}: {json.dumps(error.validator_value, default=str)[:200]}"


def _action_tool(
    name: str, entry: IntegrationEntry, action: IntegrationAction, accounts: list[OrgConnection], state: _RunState
) -> FunctionTool:
    schema = _action_schema(action, accounts, state)
    validator = Draft202012Validator(schema)

    async def invoke(arguments: str) -> str:
        try:
            written = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return f"Arguments are not valid JSON ({e})."
        # Checked here so a model's slip comes back with the schema instead of becoming a failed child block.
        error = _action_arguments_error(written, schema, validator, accounts, state, action.needs)
        if error is not None or not isinstance(written, dict):
            report = {"status": "invalid_arguments", "output": None, "error": error, "argument_schema": schema}
            _record_call(state, written, tool=name, status="invalid_arguments")
            return _window(_masked_text(report, state.run_context))
        inputs = {key: value for key, value in written.items() if key != "connection_id"}
        call = {
            "integration_id": entry.id,
            "action_id": action.id,
            "connection_id": written["connection_id"],
            "inputs": inputs,
        }
        return await _run_child_block(name, BlockType.INTEGRATION_ACTION, _INTEGRATION_ACTION_TOOL, call, state)

    return FunctionTool(
        name=name,
        description=action_description(entry, action),
        params_json_schema=schema,
        on_invoke_tool=_in_turn(state, invoke, lambda written: (BlockType.INTEGRATION_ACTION, written)),
        strict_json_schema=False,
    )


def _skill_tools(state: _RunState, session: SkillSession) -> list[FunctionTool]:
    async def load(_state: _RunState, name: str) -> SkillLoadResult:
        return session.load(name)

    async def read(_state: _RunState, name: str, path: str) -> SkillReadResult:
        return session.read_resource(name, path)

    def describe(written: JsonValue) -> tuple[BlockType | None, JsonValue]:
        return None, session.public_arguments(written if isinstance(written, dict) else {})

    return [
        _run_tool(
            LOAD_SKILL_TOOL_NAME,
            RUN_TOOL_DESCRIPTIONS[LOAD_SKILL_TOOL_NAME],
            {"name": _TEXT},
            ["name"],
            load,
            state,
            describe,
        ),
        _run_tool(
            READ_SKILL_RESOURCE_TOOL_NAME,
            READ_SKILL_RESOURCE_DESCRIPTION,
            {"name": _TEXT, "path": _TEXT},
            ["name", "path"],
            read,
            state,
            describe,
        ),
    ]


async def _skill_session(state: _RunState) -> SkillSession | None:
    if not _offers(state, LOAD_SKILL_TOOL_NAME) or state.organization_id is None:
        return None
    try:
        skills = await app.AGENT_FUNCTION.agent_skills(
            organization_id=state.organization_id,
            workflow_permanent_id=state.run_context.workflow_permanent_id,
            agent="agent_step",
        )
    except Exception:
        LOG.warning(
            "Failed to list the agent step's skills", workflow_run_block_id=state.workflow_run_block_id, exc_info=True
        )
        return None
    if not skills:
        return None
    return SkillSession(
        skills,
        agent="agent_step",
        organization_id=state.organization_id,
        workflow_run_id=state.workflow_run_id,
        workflow_run_block_id=state.workflow_run_block_id,
    )


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
        arguments, handed = await _yaml_arguments(tool, written, state, output_parameter.key)
        block_yaml = tool.yaml_class(
            **arguments,
            label=label,
            ignore_workflow_system_prompt=state.ignore_workflow_system_prompt,
        )
        handed_parameters = {
            name: WorkflowParameter(
                workflow_parameter_id=str(uuid.uuid4()),
                workflow_parameter_type=WorkflowParameterType.FILE_URL,
                workflow_id=state.workflow_id,
                key=name,
                created_at=now,
                modified_at=now,
            )
            for name in handed
        }
        # The send-email converter adds the platform SMTP parameters to this dict.
        parameters = {
            **state.child_outputs,
            **handed_parameters,
            **state.run_context.parameters,
            output_parameter.key: output_parameter,
        }
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
            for name, file in handed.items():
                state.run_context.values[name] = file.url
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
            # The note stays off the call's tool_calls entry, which names the child's row that already holds it.
            note: dict[str, str] = {}
            if tool.nested_agent:
                files = (written.get("files") or []) if "files" in tool.fields else None
                action_facts, finish_reason = await _child_action_facts(state, child_block_id, files, report["status"])
                facts.update(action_facts)
                note = {"finish_reason": finish_reason} if finish_reason else {}
            # Ahead of output, so the window that cuts a long output keeps them.
            report = {"status": report["status"], **facts, **note, **report}
            if (
                report["status"] == BlockStatus.completed
                and state.run_context.has_value(output_parameter.key)
                and output_parameter.key not in state.run_context.parameters
            ):
                state.child_outputs[output_parameter.key] = output_parameter
        finally:
            for key in temporary_smtp_keys:
                state.run_context.parameters.pop(key, None)
                state.run_context.values.pop(key, None)
            for name in handed:
                state.run_context.values.pop(name, None)
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
    child._run_file_reserved_names = _reserved_names(state)
    child.mark_fields_prerendered(written)
    # Children write their own LLM cost to their own rows.
    with model_usage_observer(None):
        result = await child.execute_safe(
            workflow_run_id=state.workflow_run_id,
            parent_workflow_run_block_id=state.workflow_run_block_id,
            organization_id=state.organization_id,
            browser_session_id=state.browser_session_id,
        )
    report = {"status": result.status, "output": result.output_parameter_value, "error": result.failure_reason}
    return report, result.workflow_run_block_id


def _url_fact(url: str) -> str:
    if url == "about:blank" or _OPAQUE_TOKEN.fullmatch(url):
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


async def _child_action_facts(
    state: _RunState, workflow_run_block_id: str | None, files: list[str] | None, status: BlockStatus
) -> tuple[dict[str, _Fact], str | None]:
    if workflow_run_block_id is None:
        return {}, None
    try:
        run_block = await app.DATABASE.observer.get_workflow_run_block(
            workflow_run_block_id, organization_id=state.organization_id
        )
    except Exception:
        LOG.warning("Could not read a nested agent's block", label=state.label, exc_info=True)
        return {}, None
    # A failed or terminated child's note is its failure_reason, which the result already gives as error.
    finish_reason = run_block.finish_reason if status == BlockStatus.completed else None
    if run_block.task_id is None:
        return {}, finish_reason
    try:
        actions = await app.DATABASE.tasks.get_task_actions_hydrated(
            run_block.task_id, organization_id=state.organization_id
        )
        facts: dict[str, _Fact] = {} if files is None else {"upload_attempts": _upload_attempts(state, actions, files)}
        return facts | _recorded_actions(actions), finish_reason
    except Exception:
        LOG.warning("Could not read a nested agent's actions", label=state.label, exc_info=True)
        return {}, finish_reason


def _upload_attempts(state: _RunState, actions: list[Action], files: list[str]) -> list[dict[str, str]]:
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


def _recorded_actions(actions: list[Action]) -> dict[str, _Fact]:
    # Only Task V3 rows carry an LLM-free target label in intention; a navigation's intention holds its raw URL.
    if not all(reasoning_is_turn_scoped(action.description) for action in actions):
        return {}
    entries = []
    for action in actions:
        if action.action_type == ActionType.GOTO_URL:
            entry = {"action": "navigate"}
            if isinstance(action, GotoUrlAction):
                entry["url"] = _url_fact(action.url)
        else:
            entry = {"action": action.intention or action.action_type.value}
        entries.append({**entry, "status": action.status.value})
    return {"recorded_actions": entries}


def _credential_keys(parameters: list[PARAMETER_TYPE]) -> list[str]:
    return [
        parameter.key
        for parameter in parameters
        if parameter.parameter_type.is_login_credential()
        or (isinstance(parameter, WorkflowParameter) and parameter.workflow_parameter_type.is_credential_type())
    ]


# Above the trigger block's own depth limit, so a chain it allows is always followed to its top.
_MAX_PARENT_RUN_HOPS = 2 * WorkflowTriggerBlock.MAX_TRIGGER_DEPTH


async def _connected_accounts(organization_id: str | None, workflow_run_id: str) -> list[OrgConnection]:
    """The active accounts the run's owner may use, Nango ones included although only a saved block can use those."""
    if organization_id is None:
        return []
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    if workflow_run is None:
        raise WorkflowRunNotFound(workflow_run_id)
    # A run a workflow_trigger block started is held to the record of the run at the top of its chain.
    root = workflow_run
    chain_unresolved = True
    for _ in range(_MAX_PARENT_RUN_HOPS + 1):
        if not root.parent_workflow_run_id:
            chain_unresolved = False
            break
        parent = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=root.parent_workflow_run_id, organization_id=organization_id
        )
        if parent is None:
            break
        root = parent
    if chain_unresolved and settings.EMBER_NARROW_AGENT_STEP_ACCOUNTS:
        # The top run's creator is unknown, so whether a record holds this run is too.
        LOG.warning(
            "agent_step_run_chain_unresolved", workflow_run_id=workflow_run_id, last_run_id=root.workflow_run_id
        )
        return []
    connections = await org_connections_by_id(organization_id, workflow_run.created_by)
    if root.created_by == EMBER_RUN_CREATOR:
        allowed = await run_connection_ids(organization_id, root.workflow_run_id)
        # A record is always enforced. Without one, narrowing on means none; off, the dispatching API predates
        # the record and required every account, so every account stays offered. That is why every worker turns
        # the setting on before any API does.
        if allowed is None:
            # Also what a worker without the API's shared cache sees, so this is the misconfiguration signal.
            LOG.warning(
                "agent_step_run_accounts_missing",
                workflow_run_id=workflow_run_id,
                record_run_id=root.workflow_run_id,
                cache_shared=app.CACHE.is_shared,
            )
        if allowed is not None or settings.EMBER_NARROW_AGENT_STEP_ACCOUNTS:
            connections = [connection for connection in connections if connection.id in (allowed or set())]
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
        ignore_workflow_system_prompt=block.ignore_workflow_system_prompt,
        workflow_system_prompt=block.workflow_system_prompt,
        tools=block.tools,
    )
    listed = [name for name in block.tools or () if name in CATALOG_TOOL_NAMES]
    withheld: list[str] = []
    # Saving refuses unknown names, but a stored step keeps a name the catalog later dropped without a legacy entry.
    if unknown := sorted(name for name in block.tools or () if not is_known_tool_name(name)):
        LOG.warning("agent_step_unknown_tools", tools=unknown, workflow_run_id=workflow_run_id)
    if listed or any(
        _offers(state, name) and settable_connected_account_fields(tool.yaml_class, tool.fields)
        for name, (_, tool) in {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}.items()
    ):
        accounts = await _connected_accounts(organization_id, workflow_run_id)
        # Every block tool's connected-account argument chooses from these.
        state.connected_accounts = [account for account in accounts if account.agent_step_usable]
        # The owner's Nango accounts are their connected Gmail (own_connected_gmail_connections).
        state.saved_block_gmail = any(account.provider == "nango" for account in accounts)
    if listed:
        state.actions = _catalog_actions(state)
        if withheld := sorted(set(listed) - set(state.actions)):
            # No account of the run can run it, or it runs only as a saved block.
            LOG.warning("agent_step_catalog_tools_not_offered", tools=withheld, workflow_run_id=workflow_run_id)
    effort = LLMConfigRegistry.get_config(llm_key).reasoning_effort
    model, run_config = resolve_agent_model(llm_key)
    state.skills = await _skill_session(state)
    if state.skills:
        state.skill_list = f"{_SKILL_LIST_HEADER}\n{render_skills_catalog(state.skills)}"
    finish = _finish_tool(block.data_schema, state)
    tools = [*_tools(state), *(_skill_tools(state, state.skills) if state.skills else []), finish]
    run_config = replace(
        run_config,
        tool_not_found_behavior="return_error_to_model",
        tool_error_formatter=_tool_not_found_formatter(state, _run_block_types(state), [tool.name for tool in tools]),
    )
    agent = Agent(
        name=block.label,
        # LiteLLM's chat-to-Responses bridge drops the served service tier, which leaves GPT calls unpriced;
        # Cacheable instructions take the model's direct Responses path, which keeps it.
        # web_search needs today's date, which follows the cached prefix and stays fixed for the run.
        instructions=CacheableSystemInstructions(
            _INSTRUCTIONS,
            f"\n\nCurrent UTC datetime: {datetime.now(UTC).isoformat(timespec='seconds')}"
            + (f"\n\n{block.workflow_system_prompt}" if block.workflow_system_prompt else "")
            + (f"\n\n{state.skill_list}" if state.skill_list else "")
            + _withheld_note(withheld, state.saved_block_gmail),
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
        with model_usage_observer(lambda event: costs.append(event.cost or 0.0)), model_attempt_telemetry_scope():
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
