from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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

from skyvern.cli.mcp_tools.blocks import BLOCK_SUMMARIES
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.copilot_model_usage import copilot_usage_observer
from skyvern.forge.sdk.copilot.cache_envelope import CacheableSystemInstructions
from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock
from skyvern.forge.sdk.workflow.models.block import Block, _validate_response_against_json_schema
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    is_sensitive_workflow_parameter,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.google_sheets import extract_spreadsheet_id
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import (
    BlockResult,
    BlockStatus,
    BlockType,
    BlockYAML,
    CodeBlockYAML,
    DataExportBlockYAML,
    DownloadToS3BlockYAML,
    EmailInboxBlockYAML,
    FileDownloadBlockYAML,
    FileParserBlockYAML,
    FileUploadBlockYAML,
    GoogleSheetsReadBlockYAML,
    GoogleSheetsWriteBlockYAML,
    HttpRequestBlockYAML,
    HumanInteractionBlockYAML,
    LoginBlockYAML,
    PdfFillBlockYAML,
    PDFParserBlockYAML,
    PrintPageBlockYAML,
    SendEmailBlockYAML,
    SplitPdfBlockYAML,
    TaskBlockYAML,
    TextPromptBlockYAML,
    UploadToS3BlockYAML,
    WaitBlockYAML,
    WebSearchBlockYAML,
)
from skyvern.utils.secret_redaction import redact_secrets_from_text

if TYPE_CHECKING:
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

_MAX_TOOL_RESULT_CHARS = 20_000
_NESTED_AGENT_MAX_STEPS = 25
_MASK = "*****"

_INSTRUCTIONS = (
    "You are one step of an unattended workflow run; nobody can answer questions. "
    "Each tool other than finish runs a workflow block, reads this run's data or acts on this run's browser page, "
    "and returns its status, output and error. When you have the answer, call finish with it."
)
_NO_SECOND_AGENT = " Runs on this run's current page with no separate agent."
_FINISH_ONLY = "Only finish can run now. Call finish with the best answer the tool results support."
_NESTED_AGENT = (
    " Each call starts a separate browser agent with its own loop, step budget, recording and run row, "
    "and you get only its result, so a call is slow and costly."
)


def _host(url: str | None) -> str | None:
    return (urlsplit(url).hostname or url) if url else None


def _spreadsheet(url_or_id: str) -> str:
    try:
        return extract_spreadsheet_id(url_or_id)
    except ValueError:
        return url_or_id


@dataclass(frozen=True)
class _BlockTool:
    yaml_class: type[BlockYAML]
    fields: tuple[str, ...]
    # None: reached through run_block.
    tool_name: str | None = None
    description: str | None = None
    nested_agent: bool = False
    # Where the call sends run data; each must appear in the run's inputs.
    destinations: Callable[[Any], list[str | None]] | None = None
    required: tuple[str, ...] = ()


_CONTROL_FLOW = (
    "Control flow: the loop already repeats and decides, and the author places a Conditional or Terminate "
    "after the block."
)
_OVERLAPS = "Covered by browser_task or the page commands; overlapping tools make tool choice worse."

# Every BlockType is a tool here or says why not, so a new block type cannot silently join or miss the tool set.
AGENT_BLOCK_TOOLS: dict[BlockType, _BlockTool | str] = {
    BlockType.TASK: _BlockTool(
        TaskBlockYAML,
        ("url", "navigation_goal", "data_extraction_goal", "data_schema", "max_steps_per_run"),
        tool_name="browser_task",
        description="Act on this run's browser: open url if given, work toward navigation_goal, and return data "
        f"matching data_schema when data_extraction_goal is set. Uses at most {_NESTED_AGENT_MAX_STEPS} steps."
        + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.LOGIN: _BlockTool(
        LoginBlockYAML,
        ("credential", "url"),
        tool_name="login",
        description="Sign in at url with one of this run's credentials." + _NESTED_AGENT,
        nested_agent=True,
        # The nested agent types the credential wherever it signs in, so that site is checked like a send.
        destinations=lambda y: [_host(y.url)],
        required=("url",),
    ),
    BlockType.WEB_SEARCH: _BlockTool(
        WebSearchBlockYAML,
        ("query", "num_results", "prompt", "json_schema"),
        tool_name="web_search",
        description="Search the web. With prompt, and optionally json_schema, the results are read into an answer.",
    ),
    BlockType.CODE: _BlockTool(CodeBlockYAML, ("code",)),
    BlockType.TEXT_PROMPT: _BlockTool(
        TextPromptBlockYAML,
        ("prompt", "json_schema"),
        description="One LLM call with no browser and no tools; without json_schema the output is "
        "{llm_response: <text>}",
    ),
    BlockType.DOWNLOAD_TO_S3: _BlockTool(DownloadToS3BlockYAML, ("url",)),
    BlockType.UPLOAD_TO_S3: _BlockTool(UploadToS3BlockYAML, ("path",)),
    BlockType.FILE_UPLOAD: _BlockTool(
        FileUploadBlockYAML,
        (
            "storage_type",
            "s3_bucket",
            "aws_access_key_id",
            "aws_secret_access_key",
            "region_name",
            "endpoint_url",
            "azure_storage_account_name",
            "azure_storage_account_key",
            "azure_blob_container_name",
            "google_credential_id",
            "google_drive_folder_id",
            "sftp_host",
            "sftp_port",
            "sftp_username",
            "sftp_password",
            "sftp_private_key",
            "sftp_private_key_passphrase",
            "sftp_remote_path",
            "sftp_host_key",
            "prompt",
            "path",
        ),
        description="Upload this run's downloaded files to S3, Azure Blob, Google Drive or SFTP",
        destinations=lambda y: [
            y.s3_bucket,
            _host(y.endpoint_url),
            y.azure_storage_account_name,
            y.google_drive_folder_id,
            y.sftp_host,
        ],
    ),
    BlockType.SEND_EMAIL: _BlockTool(
        SendEmailBlockYAML,
        (
            "sender",
            "recipients",
            "subject",
            "body",
            "body_format",
            "file_attachments",
            "transport",
            "credential_id",
            "cc",
            "bcc",
        ),
        # The SMTP path Bccs the sender.
        destinations=lambda y: [y.sender, *y.recipients, *y.cc, *y.bcc],
    ),
    BlockType.FILE_URL_PARSER: _BlockTool(FileParserBlockYAML, ("file_url", "file_type", "json_schema", "worksheet")),
    BlockType.WAIT: _BlockTool(WaitBlockYAML, ("wait_sec",)),
    BlockType.FILE_DOWNLOAD: _BlockTool(
        FileDownloadBlockYAML,
        ("url", "navigation_goal", "download_timeout"),
        description="Download a file from a page." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.PDF_PARSER: _BlockTool(PDFParserBlockYAML, ("file_url", "json_schema")),
    BlockType.HTTP_REQUEST: _BlockTool(
        HttpRequestBlockYAML,
        (
            "method",
            "url",
            "headers",
            "body",
            "files",
            "timeout",
            "follow_redirects",
            "download_filename",
            "save_response_as_file",
        ),
        destinations=lambda y: [_host(y.url)],
    ),
    BlockType.HUMAN_INTERACTION: _BlockTool(
        HumanInteractionBlockYAML,
        (
            "instructions",
            "positive_descriptor",
            "negative_descriptor",
            "timeout_seconds",
            "recipients",
            "subject",
            "body",
        ),
        destinations=lambda y: y.recipients,
    ),
    BlockType.PRINT_PAGE: _BlockTool(
        PrintPageBlockYAML, ("include_timestamp", "custom_filename", "format", "landscape", "print_background")
    ),
    BlockType.GOOGLE_SHEETS_READ: _BlockTool(
        GoogleSheetsReadBlockYAML, ("spreadsheet_url", "sheet_name", "range", "credential_id", "has_header_row")
    ),
    BlockType.GOOGLE_SHEETS_WRITE: _BlockTool(
        GoogleSheetsWriteBlockYAML,
        (
            "spreadsheet_url",
            "sheet_name",
            "range",
            "credential_id",
            "write_mode",
            "values",
            "column_mapping",
            "create_sheet_if_missing",
        ),
        destinations=lambda y: [_spreadsheet(y.spreadsheet_url)],
    ),
    BlockType.PDF_FILL: _BlockTool(PdfFillBlockYAML, ("file_url", "prompt", "payload")),
    BlockType.SPLIT_PDF: _BlockTool(SplitPdfBlockYAML, ("file_url", "prompt")),
    BlockType.EMAIL_INBOX: _BlockTool(
        EmailInboxBlockYAML,
        (
            "email_client",
            "credential_id",
            "folder",
            "prompt",
            "sender",
            "subject",
            "newer_than_days",
            "max_results",
            "include_body",
            "download_attachments",
        ),
    ),
    BlockType.DATA_EXPORT: _BlockTool(DataExportBlockYAML, ("data", "data_schema", "file_name")),
    BlockType.FOR_LOOP: _CONTROL_FLOW,
    BlockType.WHILE_LOOP: _CONTROL_FLOW,
    BlockType.CONDITIONAL: _CONTROL_FLOW,
    BlockType.TERMINATE: _CONTROL_FLOW,
    BlockType.WORKFLOW_TRIGGER: "Starts a different run, which is outside this run.",
    BlockType.TaskV2: _OVERLAPS,
    BlockType.NAVIGATION: _OVERLAPS,
    BlockType.ACTION: _OVERLAPS,
    BlockType.EXTRACTION: _OVERLAPS,
    BlockType.VALIDATION: _OVERLAPS,
    BlockType.GOTO_URL: _OVERLAPS,
    BlockType.AGENT_STEP: "No recursion.",
}
_RUN_BLOCK_TYPES: dict[str, tuple[BlockType, _BlockTool]] = {
    block_type.value: (block_type, tool)
    for block_type, tool in AGENT_BLOCK_TOOLS.items()
    if isinstance(tool, _BlockTool) and tool.tool_name is None
}


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
    # The prompt as the author wrote it and the workflow parameter values: the only text an outbound destination may
    # come from. The rendered prompt can carry earlier block outputs, which may be text read off a page.
    inputs: str
    credential_keys: list[str]
    ignore_workflow_system_prompt: bool
    calls: list[dict[str, Any]] = field(default_factory=list)
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
    def __init__(self, state: _RunState) -> None:
        self.state = state
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

    async def on_llm_end(self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse) -> None:
        self.usage.add(response.usage)
        self.state.call_order = [item.call_id for item in response.output if isinstance(item, ResponseFunctionToolCall)]
        refusals = [
            part.refusal
            for item in response.output
            if isinstance(item, ResponseOutputMessage)
            for part in item.content
            if isinstance(part, ResponseOutputRefusal)
        ]
        if refusals:
            raise _Refused(" ".join(refusals))


def _record_call(state: _RunState, written: object, **call: str) -> None:
    arguments = redact_secrets_from_text(
        json.dumps(written, default=str), Block._registered_secret_values(state.run_context)
    )
    state.calls.append({**call, "arguments": arguments})


def resolve_agent_model(llm_key: str) -> tuple[str | Model, RunConfig]:
    model_name, copilot_run_config, _, _ = resolve_model_config(None, llm_key_override=llm_key)
    # Copilot's RunConfig also carries session callbacks that read its chat context.
    return model_name, RunConfig(
        model=copilot_run_config.model,
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
        description="Return this step's answer. The arguments are the answer.",
        params_json_schema=schema,
        on_invoke_tool=_in_turn(state, invoke),
        # Strict mode would make every optional property required.
        strict_json_schema=False,
    )


def _arguments_schema(tool: _BlockTool, state: _RunState) -> dict[str, Any]:
    schema = tool.yaml_class.model_json_schema()
    params: dict[str, Any] = {
        "type": "object",
        "properties": {name: schema["properties"][name] for name in tool.fields if name != "credential"},
        "required": [name for name in tool.fields if name in schema.get("required", []) or name in tool.required],
        "additionalProperties": False,
    }
    if "credential" in tool.fields:
        params["properties"]["credential"] = {"type": "string", "enum": state.credential_keys}
        params["required"].append("credential")
    if "$defs" in schema:
        params["$defs"] = schema["$defs"]
    return params


def _yaml_arguments(tool: _BlockTool, written: dict[str, Any], state: _RunState) -> dict[str, Any]:
    if unknown := sorted(set(written) - set(tool.fields)):
        raise ValueError(f"Unknown arguments {unknown}; the allowed arguments are {list(tool.fields)}.")
    if missing := [name for name in tool.required if not written.get(name)]:
        raise ValueError(f"Missing required arguments {missing}.")
    arguments = dict(written)
    if "credential" in tool.fields:
        credential = arguments.pop("credential", None)
        if credential not in state.credential_keys:
            raise ValueError(f"credential must be one of {state.credential_keys}.")
        arguments["parameter_keys"] = [credential]
    if tool.nested_agent:
        steps = arguments.get("max_steps_per_run")
        arguments |= {
            "engine": RunEngine.skyvern_v3,
            "engine_pinned": True,
            "max_steps_per_run": min(steps, _NESTED_AGENT_MAX_STEPS)
            if isinstance(steps, int) and steps > 0
            else _NESTED_AGENT_MAX_STEPS,
        }
    return arguments


def _in_inputs(destination: str, inputs: str) -> bool:
    # Whole-token match, so an input naming example.com.au, devops@example.com, team+ops@example.com or
    # o'ps@example.com does not admit example.com or ps@example.com.
    pattern = rf"(?<![\w.+@-])(?<!\w'){re.escape(destination)}(?![\w-]|\.\w)"
    return re.search(pattern, inputs, re.IGNORECASE) is not None


def _dedicated_tool(block_type: BlockType, tool: _BlockTool, state: _RunState) -> FunctionTool:
    name = tool.tool_name or block_type.value

    async def invoke(arguments: str) -> str:
        try:
            written = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return f"Arguments are not valid JSON ({e})."
        return await _run_child_block(name, block_type, tool, written, state)

    return FunctionTool(
        name=name,
        description=tool.description or BLOCK_SUMMARIES[block_type.value],
        params_json_schema=_arguments_schema(tool, state),
        on_invoke_tool=_in_turn(state, invoke),
        strict_json_schema=False,
    )


def _run_block_tool(state: _RunState) -> FunctionTool:
    lines = [
        f"- {name}({', '.join(tool.fields)}): {(tool.description or BLOCK_SUMMARIES[name]).rstrip('.')}"
        for name, (_, tool) in _RUN_BLOCK_TYPES.items()
    ]

    async def invoke(arguments: str) -> str:
        try:
            call = json.loads(arguments or "{}")
            block_type, tool = _RUN_BLOCK_TYPES[call["block_type"]]
        except (json.JSONDecodeError, KeyError, TypeError):
            return f"Pass block_type, one of {list(_RUN_BLOCK_TYPES)}, and that block's arguments."
        return await _run_child_block("run_block", block_type, tool, call.get("arguments") or {}, state)

    return FunctionTool(
        name="run_block",
        description="Run one workflow block in this run and return its status, output and error. arguments holds "
        "that block's fields; bad arguments return the block's argument schema. Block types:\n" + "\n".join(lines),
        params_json_schema={
            "type": "object",
            "properties": {
                "block_type": {"type": "string", "enum": list(_RUN_BLOCK_TYPES)},
                "arguments": {"type": "object"},
            },
            "required": ["block_type", "arguments"],
            "additionalProperties": False,
        },
        on_invoke_tool=_in_turn(state, invoke),
        strict_json_schema=False,
    )


def _tool_text(report: dict[str, Any], run_context: WorkflowRunContext) -> str:
    text = redact_secrets_from_text(
        json.dumps(run_context.mask_secrets_in_data(report), default=str), Block._registered_secret_values(run_context)
    )
    return text if len(text) <= _MAX_TOOL_RESULT_CHARS else text[:_MAX_TOOL_RESULT_CHARS] + " ...[truncated]"


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
        return _tool_text(report, state.run_context)

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


def _tools(state: _RunState) -> list[FunctionTool]:
    text = {"type": "string"}
    return [
        *(
            _dedicated_tool(block_type, tool, state)
            for block_type, tool in AGENT_BLOCK_TOOLS.items()
            if isinstance(tool, _BlockTool)
            and tool.tool_name
            and ("credential" not in tool.fields or state.credential_keys)
        ),
        _run_block_tool(state),
        _run_tool(
            "read_run_data",
            "Read what this run already has. Without name: the names of its workflow parameters, its earlier blocks "
            "with their status, and its downloaded files. With name: that parameter's value, that block's output and "
            "outcome, or that file's URL. Secret values are masked.",
            {"name": text},
            [],
            _read_run_data,
            state,
        ),
        _run_tool("page_goto", "Open url in this run's browser.", {"url": text}, ["url"], _page_goto, state),
        _run_tool(
            "page_click",
            "Click the element prompt describes." + _NO_SECOND_AGENT,
            {"prompt": text},
            ["prompt"],
            _page_click,
            state,
        ),
        _run_tool(
            "page_fill",
            "Type value into the field prompt describes." + _NO_SECOND_AGENT,
            {"prompt": text, "value": text},
            ["prompt", "value"],
            _page_fill,
            state,
        ),
        _run_tool(
            "page_extract",
            "Read what prompt asks for from the page, shaped by schema, a JSON schema. For a yes/no check, pass "
            '{"type": "boolean"}.' + _NO_SECOND_AGENT,
            {"prompt": text, "schema": {"type": "object"}},
            ["prompt"],
            _page_extract,
            state,
        ),
    ]


async def _run_child_block(
    tool_name: str, block_type: BlockType, tool: _BlockTool, written: object, state: _RunState
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
        destinations = (
            [d.strip() for d in tool.destinations(block_yaml) if d and d.strip()] if tool.destinations else []
        )
        # The send-email converter adds the platform SMTP parameters to this dict.
        parameters = {**state.run_context.parameters, output_parameter.key: output_parameter}
        child = block_yaml_to_block(block_yaml, parameters)  # type: ignore[arg-type]
    except Exception as e:
        report = {
            "status": "invalid_arguments",
            "output": None,
            "error": str(e),
            "argument_schema": _arguments_schema(tool, state),
        }
    else:
        # send_email swaps a recipient that names a run parameter for that parameter's value.
        run_context = state.run_context
        resolved = (str(run_context.get_value_or_none(d)) if run_context.has_parameter(d) else d for d in destinations)
        if outside := next((d for d in resolved if not _in_inputs(d, state.inputs)), None):
            report = {
                "status": "refused",
                "output": None,
                "error": f"{outside!r} is not in this run's inputs, and {block_type.value} may only send to a "
                "destination written in a workflow parameter value or in this agent block's prompt. Use one "
                f"of those, or report that the workflow author must add {outside!r} to them.",
            }
        else:
            # A run resolves its secret parameters when it starts, before the converter added these.
            for parameter in parameters.values():
                if isinstance(parameter, AWSSecretParameter) and not run_context.has_parameter(parameter.key):
                    await run_context.register_aws_secret_parameter_value(parameter)
            report = await _execute_child(child, written, state)
    _record_call(state, written, tool=tool_name, block_type=block_type.value, label=label, status=report["status"])
    return _tool_text(report, state.run_context)


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


def _run_inputs(prompt: str, run_context: WorkflowRunContext) -> str:
    values = [
        run_context.values.get(key)
        for key, parameter in run_context.parameters.items()
        if parameter.parameter_type == ParameterType.WORKFLOW
    ]
    return "\n".join([prompt, *(json.dumps(value, default=str, ensure_ascii=False) for value in values)])


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
        inputs=_run_inputs(block.prompt, run_context),
        credential_keys=_credential_keys(run_context),
        ignore_workflow_system_prompt=block.ignore_workflow_system_prompt,
    )
    effort = LLMConfigRegistry.get_config(llm_key).reasoning_effort
    model, run_config = resolve_agent_model(llm_key)
    finish = _finish_tool(block.data_schema, state)
    agent = Agent(
        name=block.label,
        # LiteLLM's chat-to-Responses bridge drops the served service tier, which leaves GPT calls unpriced;
        # Copilot's cacheable instructions take its direct Responses path, which keeps it.
        instructions=CacheableSystemInstructions(
            _INSTRUCTIONS,
            f"\n\n{block.workflow_system_prompt}" if block.workflow_system_prompt else "",
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
    hooks = _LoopHooks(state)
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
        if organization_id:
            await app.DATABASE.observer.increment_workflow_run_block_llm_cost(
                workflow_run_block_id=workflow_run_block_id, organization_id=organization_id, amount=sum(costs)
            )
        await block.record_output_parameter_value(run_context, workflow_run_id, value)

    try:
        with copilot_usage_observer(lambda event: costs.append(event.cost or 0.0)):
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

    if state.task and state.step:
        await app.ARTIFACT_MANAGER.flush_step_archive(state.step.step_id)
        await app.DATABASE.tasks.update_step(
            task_id=state.task.task_id,
            step_id=state.step.step_id,
            organization_id=organization_id,
            status={BlockStatus.completed: StepStatus.completed, BlockStatus.failed: StepStatus.failed}.get(
                status, StepStatus.canceled
            ),
        )
        await app.DATABASE.tasks.update_task(
            task_id=state.task.task_id, organization_id=organization_id, status=TaskStatus(status.value)
        )

    value = output(stop_reason)
    await save(value)
    return await block.build_block_result(
        success=status == BlockStatus.completed,
        failure_reason=failure_reason
        and redact_secrets_from_text(failure_reason, Block._registered_secret_values(run_context)),
        output_parameter_value=value,
        status=status,
        workflow_run_block_id=workflow_run_block_id,
        organization_id=organization_id,
    )
