from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
    ToolsToFinalOutputResult,
    TResponseInputItem,
    Usage,
)
from agents.models.interface import Model
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputRefusal
from openai.types.shared import Reasoning
from pydantic import ValidationError

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
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock
from skyvern.forge.sdk.workflow.models.agent_step_tools import (
    DEDICATED_BLOCK_TOOLS,
    FINISH_DESCRIPTION,
    RUN_BLOCK_TYPES,
    RUN_TOOL_DESCRIPTIONS,
    BlockTool,
    block_description,
    run_block_description,
)
from skyvern.forge.sdk.workflow.models.block import Block, _validate_response_against_json_schema
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    is_sensitive_workflow_parameter,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.secret_redaction import redact_secrets_from_text

if TYPE_CHECKING:
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

LOG = structlog.get_logger()
_MAX_TOOL_RESULT_CHARS = 20_000
_MASK = "*****"

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
    # None offers every tool.
    tools: list[str] | None = None
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


async def _raise_if_run_ended(state: _RunState) -> None:
    run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=state.workflow_run_id, organization_id=state.organization_id
    )
    if run is not None and run.status.is_final():
        raise _RunEnded(BlockStatus(run.status.value))


def _in_turn(
    state: _RunState, invoke: Callable[[str], Awaitable[str]]
) -> Callable[[ToolContext[Any], str], Awaitable[str]]:
    async def ordered(ctx: ToolContext[Any], arguments: str) -> str:
        call_id = ctx.tool_call_id
        async with state.turn:
            await state.turn.wait_for(lambda: call_id not in state.call_order or state.call_order[0] == call_id)
            try:
                await _raise_if_run_ended(state)
                if state.answer is not None:
                    return "finish already returned this step's answer, so this call did not run."
                # The drain keeps every tool listed because Anthropic rejects replayed thinking once the tool list changes.
                if state.drained and ctx.tool_name != "finish":
                    return _FINISH_ONLY
                return await invoke(arguments)
            finally:
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
        self.state.call_order = [item.call_id for item in response.output if isinstance(item, ResponseFunctionToolCall)]
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


def _record_call(state: _RunState, written: object, **call: str) -> None:
    arguments = redact_secrets_from_text(
        json.dumps(written, default=str), Block._registered_secret_values(state.run_context)
    )
    state.calls.append({**call, "arguments": arguments})


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


def _arguments_schema(tool: BlockTool, state: _RunState) -> dict[str, Any]:
    schema = tool.yaml_class.model_json_schema()
    params: dict[str, Any] = {
        "type": "object",
        "properties": {name: schema["properties"][name] for name in tool.fields if name != "credential"},
        "required": [name for name in tool.fields if name in schema.get("required", [])],
        "additionalProperties": False,
    }
    if "credential" in tool.fields:
        params["properties"]["credential"] = {"type": "string", "enum": state.credential_keys}
        params["required"].append("credential")
    # Only the definitions a kept field reaches; the rest describe fields the model never sees.
    defs = schema.get("$defs", {})
    used: dict[str, Any] = {}
    reached = json.dumps(params["properties"])
    while names := [name for name in defs if name not in used and f'"#/$defs/{name}"' in reached]:
        used |= {name: defs[name] for name in names}
        reached = json.dumps([defs[name] for name in names])
    if used:
        params["$defs"] = used
    return params


def _yaml_arguments(tool: BlockTool, written: dict[str, Any], state: _RunState) -> dict[str, Any]:
    if unknown := sorted(set(written) - set(tool.fields)):
        raise ValueError(f"Unknown arguments {unknown}; the allowed arguments are {list(tool.fields)}.")
    arguments = dict(written)
    if "credential" in tool.fields:
        credential = arguments.pop("credential", None)
        if credential not in state.credential_keys:
            raise ValueError(f"credential must be one of {state.credential_keys}.")
        arguments["parameter_keys"] = [credential]
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
        on_invoke_tool=_in_turn(state, invoke),
        strict_json_schema=False,
    )


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

    return FunctionTool(
        name="run_block",
        description=run_block_description(block_types),
        params_json_schema={
            "type": "object",
            "properties": {
                "block_type": {"type": "string", "enum": block_types},
                "arguments": {"type": "object"},
            },
            "required": ["block_type", "arguments"],
            "additionalProperties": False,
        },
        on_invoke_tool=_in_turn(state, invoke),
        strict_json_schema=False,
    )


def _masked_text(report: dict[str, Any], run_context: WorkflowRunContext) -> str:
    return redact_secrets_from_text(
        json.dumps(run_context.mask_secrets_in_data(report), default=str), Block._registered_secret_values(run_context)
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
            report = {"status": "completed", "output": await run(state, **written), "error": None}
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
    parameters = {
        key: parameter
        for key, parameter in run_context.parameters.items()
        if parameter.parameter_type != ParameterType.OUTPUT
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
    opt_out = {"system_prompt": None} if state.ignore_workflow_system_prompt else {}
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
    return state.tools is None or name in state.tools


def _tools(state: _RunState) -> list[FunctionTool]:
    block_types = [name for name in RUN_BLOCK_TYPES if _offers(state, name)]
    return [
        *(
            _dedicated_tool(block_type, tool, state)
            for name, (block_type, tool) in DEDICATED_BLOCK_TOOLS.items()
            if _offers(state, name) and ("credential" not in tool.fields or state.credential_keys)
        ),
        *([_run_block_tool(state, block_types)] if block_types else []),
        *(
            _run_tool(name, RUN_TOOL_DESCRIPTIONS[name], properties, required, run, state)
            for name, properties, required, run in _RUN_TOOLS
            if _offers(state, name)
        ),
    ]


async def _run_child_block(
    tool_name: str, block_type: BlockType, tool: BlockTool, written: object, state: _RunState
) -> str:
    label = f"{state.label}_{block_type.value}_{len(state.calls) + 1}"
    now = datetime.now(UTC)
    output_parameter = OutputParameter(
        output_parameter_id=str(uuid.uuid4()),
        key=f"{label}_output",
        workflow_id=state.workflow_id,
        created_at=now,
        modified_at=now,
    )
    report: dict[str, Any]
    try:
        if not isinstance(written, dict):
            raise ValueError("arguments must be a JSON object.")
        block_yaml = tool.yaml_class(
            **_yaml_arguments(tool, written, state),
            label=label,
            ignore_workflow_system_prompt=state.ignore_workflow_system_prompt,
        )
        # The send-email converter adds the platform SMTP parameters to this dict.
        parameters = {**state.run_context.parameters, output_parameter.key: output_parameter}
        child = block_yaml_to_block(block_yaml, parameters)  # type: ignore[arg-type]
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
        # A run resolves its secret parameters when it starts, before the converter added these.
        for parameter in parameters.values():
            if isinstance(parameter, AWSSecretParameter) and not state.run_context.has_parameter(parameter.key):
                await state.run_context.register_aws_secret_parameter_value(parameter)
        report = await _execute_child(child, written, state)
    _record_call(state, written, tool=tool_name, block_type=block_type.value, label=label, status=report["status"])
    state.results[label] = report
    return _window(_masked_text(report, state.run_context), 0, label if _offers(state, "read_run_data") else None)


async def _execute_child(child: Block, written: dict[str, Any], state: _RunState) -> dict[str, Any]:
    child.mark_fields_prerendered(written)
    # Children write their own LLM cost to their own rows.
    with copilot_usage_observer(None):
        result = await child.execute_safe(
            workflow_run_id=state.workflow_run_id,
            parent_workflow_run_block_id=state.workflow_run_block_id,
            organization_id=state.organization_id,
            browser_session_id=state.browser_session_id,
        )
    return {"status": result.status, "output": result.output_parameter_value, "error": result.failure_reason}


def _credential_keys(run_context: WorkflowRunContext) -> list[str]:
    return [
        key
        for key, parameter in run_context.parameters.items()
        if parameter.parameter_type.is_login_credential()
        or (isinstance(parameter, WorkflowParameter) and parameter.workflow_parameter_type.is_credential_type())
    ]


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
        credential_keys=_credential_keys(run_context),
        ignore_workflow_system_prompt=block.ignore_workflow_system_prompt,
        tools=block.tools,
    )
    effort = LLMConfigRegistry.get_config(llm_key).reasoning_effort
    model, run_config = resolve_agent_model(llm_key)
    finish = _finish_tool(block.data_schema, state)
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
        tools=[*_tools(state), finish],
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
            await app.AGENT_FUNCTION.post_agent_step_execution(
                organization_id,
                workflow_run_id,
                workflow_run_block_id,
                decisions=sum(1 for call in state.calls if call["tool"] != "finish"),
            )

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
        and redact_secrets_from_text(failure_reason, Block._registered_secret_values(run_context)),
        output_parameter_value=value,
        status=status,
        workflow_run_block_id=workflow_run_block_id,
        organization_id=organization_id,
    )
