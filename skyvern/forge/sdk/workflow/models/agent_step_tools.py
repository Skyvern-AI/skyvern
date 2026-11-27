"""The agent step's tool table, kept out of agent_step_loop so Ember's tools and the agent-protocol route can read
it without loading the loop."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from skyvern.cli.mcp_tools.blocks import BLOCK_SUMMARIES
from skyvern.forge.sdk.integrations.catalog import SKYVERN_INTEGRATIONS, IntegrationAction, IntegrationEntry
from skyvern.forge.sdk.integrations.connected_accounts import settable_connected_account_fields
from skyvern.schemas.agent_step_tool_names import (
    AGENT_STEP_TOOL_NAMES,
    ALWAYS_OFFERED_TOOLS,
    CATALOG_TOOL_NAMES,
    LEGACY_AGENT_STEP_TOOL_NAMES,
    catalog_tool_name,
)
from skyvern.schemas.workflows import (
    BlockType,
    BlockYAML,
    CodeBlockYAML,
    DataExportBlockYAML,
    DownloadToS3BlockYAML,
    EmailInboxBlockYAML,
    FileDownloadBlockYAML,
    FileParserBlockYAML,
    FileUploadBlockYAML,
    HttpRequestBlockYAML,
    HumanInteractionBlockYAML,
    LoginBlockYAML,
    PdfFillBlockYAML,
    PrintPageBlockYAML,
    SendEmailBlockYAML,
    SplitPdfBlockYAML,
    TaskBlockYAML,
    TextPromptBlockYAML,
    UploadToS3BlockYAML,
    WaitBlockYAML,
    WebSearchBlockYAML,
)

_NO_SECOND_AGENT = " Runs on this run's current page with no separate agent."
_NESTED_AGENT = (
    " Each call starts a separate browser agent with its own loop, step budget, recording and run row, "
    "and you get only its result, so a call is slow and costly."
)


@dataclass(frozen=True)
class BlockTool:
    yaml_class: type[BlockYAML]
    fields: tuple[str, ...]
    # None: reached through run_block.
    tool_name: str | None = None
    description: str | None = None
    nested_agent: bool = False

    @property
    def arguments(self) -> tuple[str, ...]:
        """fields, plus every field the block's schema marks as a connected account that the caller can reach."""
        return (*self.fields, *settable_connected_account_fields(self.yaml_class, self.fields))


_CONTROL_FLOW = "Control flow: the step's own loop already repeats and decides."
_OVERLAPS = "Covered by browser_task or the page commands; overlapping tools make tool choice worse."
_CATALOG = "Offered as its integration catalog actions, named <integration id>__<action id>."

# Every BlockType is a tool here or says why not, so a new block type cannot silently join or miss the tool set.
AGENT_BLOCK_TOOLS: dict[BlockType, BlockTool | str] = {
    BlockType.TASK: BlockTool(
        TaskBlockYAML,
        ("url", "navigation_goal", "data_extraction_goal", "data_schema", "files"),
        tool_name="browser_task",
        description="Act on this run's browser: open url if given, work toward navigation_goal, and return data "
        "matching data_schema when data_extraction_goal is set." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.LOGIN: BlockTool(
        LoginBlockYAML,
        ("credential", "url"),
        tool_name="login",
        description="Sign in at url with one of this step's credentials." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.WEB_SEARCH: BlockTool(
        WebSearchBlockYAML,
        ("query", "num_results", "prompt", "json_schema"),
        tool_name="web_search",
        description="Search the web. With prompt, and optionally json_schema, the results are read into an answer.",
    ),
    BlockType.CODE: BlockTool(
        CodeBlockYAML,
        ("code",),
        description="Run Python in a sandbox. `page` is this run's browser page (Playwright Page API), and "
        "`await open_page(page, url)` opens another tab in it. `await page.context.request.get(url)` fetches a URL "
        "with the page's cookies; it can't reach loopback or private hosts. `await response.body()` is its bytes. "
        "`await read_run_file(name)` returns the bytes of one of this run's files (names from read_run_data). "
        "`await publish_file(filename, data=<bytes> | text=<str> | folder={path: bytes or text})` saves a new run "
        "file (folder= makes a ZIP) and returns its name and size. Imports: asyncio, datetime, html, json, re. "
        "open() reads and writes only under /tmp. The code's local variables are the output.",
    ),
    BlockType.TEXT_PROMPT: BlockTool(
        TextPromptBlockYAML,
        ("prompt", "json_schema"),
        description="One LLM call with no browser and no tools; without json_schema the output is "
        "{llm_response: <text>}",
    ),
    BlockType.DOWNLOAD_TO_S3: BlockTool(DownloadToS3BlockYAML, ("url",)),
    BlockType.UPLOAD_TO_S3: BlockTool(UploadToS3BlockYAML, ("path",)),
    BlockType.FILE_UPLOAD: BlockTool(
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
    ),
    BlockType.SEND_EMAIL: BlockTool(
        SendEmailBlockYAML,
        (
            "sender",
            "recipients",
            "subject",
            "body",
            "body_format",
            "file_attachments",
            "transport",
            "cc",
            "bcc",
        ),
    ),
    BlockType.FILE_URL_PARSER: BlockTool(FileParserBlockYAML, ("file_url", "file_type", "json_schema", "worksheet")),
    BlockType.WAIT: BlockTool(WaitBlockYAML, ("wait_sec",)),
    BlockType.FILE_DOWNLOAD: BlockTool(
        FileDownloadBlockYAML,
        ("url", "navigation_goal", "download_timeout"),
        description="Download a file from a page." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.PDF_PARSER: "Covered by file_url_parser, which also OCRs scanned PDFs.",
    BlockType.HTTP_REQUEST: BlockTool(
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
    ),
    BlockType.HUMAN_INTERACTION: BlockTool(
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
    ),
    BlockType.PRINT_PAGE: BlockTool(
        PrintPageBlockYAML, ("include_timestamp", "custom_filename", "format", "landscape", "print_background")
    ),
    BlockType.GOOGLE_SHEETS_READ: _CATALOG,
    BlockType.GOOGLE_SHEETS_WRITE: _CATALOG,
    BlockType.PDF_FILL: BlockTool(PdfFillBlockYAML, ("file_url", "prompt", "payload")),
    BlockType.SPLIT_PDF: BlockTool(SplitPdfBlockYAML, ("file_url", "prompt")),
    BlockType.EMAIL_INBOX: BlockTool(
        EmailInboxBlockYAML,
        (
            "email_client",
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
    BlockType.DATA_EXPORT: BlockTool(DataExportBlockYAML, ("data", "data_schema", "file_name")),
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
    BlockType.POWERBI_QUERY: "Power BI queries require a user-authorized connection and are not available in agent-step authoring.",
    BlockType.AGENT_STEP: "No recursion.",
    BlockType.INTEGRATION_ACTION: "Runs an existing integration block, which is offered as its own tool.",
    BlockType.COMPUTER_USE: "Acts on the run's leased desktop machine, which an agent step does not hold.",
}
DEDICATED_BLOCK_TOOLS: dict[str, tuple[BlockType, BlockTool]] = {
    tool.tool_name: (block_type, tool)
    for block_type, tool in AGENT_BLOCK_TOOLS.items()
    if isinstance(tool, BlockTool) and tool.tool_name
}
RUN_BLOCK_TYPES: dict[str, tuple[BlockType, BlockTool]] = {
    block_type.value: (block_type, tool)
    for block_type, tool in AGENT_BLOCK_TOOLS.items()
    if isinstance(tool, BlockTool) and tool.tool_name is None
}


def offered_as_tool(action: IntegrationAction) -> bool:
    # Only actions backed by an existing block run here; a Nango-backed one runs only as a saved block.
    return action.agent_step == "tool" and action.runs_via.startswith("block:")


def action_description(entry: IntegrationEntry, action: IntegrationAction) -> str:
    return f"{entry.name}: {action.name}. {action.description}"


def action_block_type(action: IntegrationAction) -> BlockType:
    return BlockType(action.runs_via.partition(":")[2])


# The catalog actions a step can call: built-in only, so Copilot's account gating below covers every one offered.
CATALOG_ACTION_TOOLS: dict[str, tuple[IntegrationEntry, IntegrationAction]] = {
    catalog_tool_name(entry.id, action.id): (entry, action)
    for entry in SKYVERN_INTEGRATIONS
    for action in entry.actions
    if offered_as_tool(action)
}
# Listed by name so the step can tell the model to place a saved block instead.
SAVED_BLOCK_ONLY_TOOLS: dict[str, tuple[IntegrationEntry, IntegrationAction]] = {
    catalog_tool_name(entry.id, action.id): (entry, action)
    for entry in SKYVERN_INTEGRATIONS
    for action in entry.actions
    if action.agent_step == "saved_block_only"
}
# The step is offered every active account of the run's owner for these tools' marked fields.
ACCOUNT_TOOL_NAMES: frozenset[str] = frozenset(
    name
    for name, (_, tool) in {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}.items()
    if len(tool.arguments) > len(tool.fields)
) | frozenset(CATALOG_ACTION_TOOLS)
ACTING_ACCOUNT_TOOL_NAMES: frozenset[str] = frozenset(
    name
    for name, (_, tool) in {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}.items()
    if settable_connected_account_fields(tool.yaml_class, tool.fields, acting_only=True)
) | frozenset(name for name, (_, action) in CATALOG_ACTION_TOOLS.items() if action.acts)
RUN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_run_data": "Read what this run already has. Without name: the names of its workflow parameters, its "
    "block outputs with their status, and its files; a null status marks a block output handed in from an earlier "
    "run. With name: that parameter's value, that block's output and outcome (name a block by its label or its "
    "<label>_output key), or that file's URL. Secret values are masked. With offset, a cut result from that "
    "character on.",
    "page_goto": "Open url in this run's browser.",
    "page_click": "Click the element prompt describes." + _NO_SECOND_AGENT,
    "page_fill": "Type value into the field prompt describes." + _NO_SECOND_AGENT,
    "page_extract": "Read what prompt asks for from the page, shaped by schema, a JSON schema. For a yes/no check, "
    'pass {"type": "boolean"}.' + _NO_SECOND_AGENT,
    "load_skill": "Return the instructions and other file paths of the skill named name. read_skill_resource(name, "
    "path) returns one of those files. Offered only when this run has skills.",
}
READ_SKILL_RESOURCE_DESCRIPTION = "Return the text of the file at path in the skill named name."
FILES_DESCRIPTION = (
    "files: names of this run's files (see read_run_data) to hand to the browser agent; it receives their values and "
    "can upload them."
)
FINISH_DESCRIPTION = "Return this step's answer. The arguments are the answer."
FAIL_DESCRIPTION = "End this step as failed. The block fails with reason as its failure reason."

# Catalog action names are listed in CATALOG_TOOL_NAMES instead, a names-only copy checked here against the catalog.
if {*DEDICATED_BLOCK_TOOLS, *RUN_BLOCK_TYPES, *RUN_TOOL_DESCRIPTIONS, "finish", "fail"} != AGENT_STEP_TOOL_NAMES:
    raise RuntimeError("AGENT_STEP_TOOL_NAMES must list exactly the agent step's tool table.")
if (catalog_names := {*CATALOG_ACTION_TOOLS, *SAVED_BLOCK_ONLY_TOOLS}) != CATALOG_TOOL_NAMES or (
    CATALOG_TOOL_NAMES & AGENT_STEP_TOOL_NAMES
):
    raise RuntimeError(
        "CATALOG_TOOL_NAMES in skyvern/schemas/agent_step_tool_names.py must list exactly the catalog actions a step "
        f"can list: add {sorted(catalog_names - CATALOG_TOOL_NAMES)}, remove {sorted(CATALOG_TOOL_NAMES - catalog_names)}."
    )
if set(LEGACY_AGENT_STEP_TOOL_NAMES) & AGENT_STEP_TOOL_NAMES or not set(
    LEGACY_AGENT_STEP_TOOL_NAMES.values()
) <= AGENT_STEP_TOOL_NAMES | set(CATALOG_ACTION_TOOLS):
    raise RuntimeError("LEGACY_AGENT_STEP_TOOL_NAMES must map retired tool names to current ones.")


def block_description(block_type: BlockType, tool: BlockTool) -> str:
    return tool.description or BLOCK_SUMMARIES[block_type.value]


def run_block_description(
    block_types: Iterable[str], table: Mapping[str, tuple[BlockType, BlockTool]] = RUN_BLOCK_TYPES
) -> str:
    return (
        "Run one workflow block in this run and return its status, output and error. arguments holds "
        "that block's fields; bad arguments return the block's argument schema. Block types:\n"
        + "\n".join(
            f"- {name}({', '.join(tool.arguments)}): {block_description(block_type, tool).rstrip('.')}"
            for name in block_types
            for block_type, tool in [table[name]]
        )
    )


def tool_descriptions() -> dict[str, str]:
    """Every name a step's tools list may hold. A block type is called through run_block; login needs a credential."""
    return {
        **{name: block_description(block_type, tool) for name, (block_type, tool) in DEDICATED_BLOCK_TOOLS.items()},
        **{name: block_description(block_type, tool) for name, (block_type, tool) in RUN_BLOCK_TYPES.items()},
        **{name: action_description(entry, action) for name, (entry, action) in CATALOG_ACTION_TOOLS.items()},
        **RUN_TOOL_DESCRIPTIONS,
        "finish": FINISH_DESCRIPTION,
        "fail": FAIL_DESCRIPTION,
    }


class AgentStepToolOption(BaseModel):
    name: str
    description: str
    # None for the page tools, which are not blocks.
    block_type: BlockType | None


def tool_options() -> list[AgentStepToolOption]:
    """The tools an author chooses from."""
    blocks = {
        **{name: block_type for name, (block_type, _) in {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}.items()},
        **{name: action_block_type(action) for name, (_, action) in CATALOG_ACTION_TOOLS.items()},
    }
    return [
        AgentStepToolOption(name=name, description=description, block_type=blocks.get(name))
        for name, description in sorted(tool_descriptions().items())
        if name not in ALWAYS_OFFERED_TOOLS
    ]
