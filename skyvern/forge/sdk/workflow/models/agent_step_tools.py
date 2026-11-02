"""The agent step's tool table, kept out of agent_step_loop: the loop imports Copilot's model resolver, which imports
mcp_hooks, and mcp_hooks reads this table at load."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from skyvern.cli.mcp_tools.blocks import BLOCK_SUMMARIES
from skyvern.schemas.agent_step_tool_names import AGENT_STEP_TOOL_NAMES, ALWAYS_OFFERED_TOOLS
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


_CONTROL_FLOW = "Control flow: the step's own loop already repeats and decides."
_OVERLAPS = "Covered by browser_task or the page commands; overlapping tools make tool choice worse."

# Every BlockType is a tool here or says why not, so a new block type cannot silently join or miss the tool set.
AGENT_BLOCK_TOOLS: dict[BlockType, BlockTool | str] = {
    BlockType.TASK: BlockTool(
        TaskBlockYAML,
        ("url", "navigation_goal", "data_extraction_goal", "data_schema"),
        tool_name="browser_task",
        description="Act on this run's browser: open url if given, work toward navigation_goal, and return data "
        "matching data_schema when data_extraction_goal is set." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.LOGIN: BlockTool(
        LoginBlockYAML,
        ("credential", "url"),
        tool_name="login",
        description="Sign in at url with one of this run's credentials." + _NESTED_AGENT,
        nested_agent=True,
    ),
    BlockType.WEB_SEARCH: BlockTool(
        WebSearchBlockYAML,
        ("query", "num_results", "prompt", "json_schema"),
        tool_name="web_search",
        description="Search the web. With prompt, and optionally json_schema, the results are read into an answer.",
    ),
    BlockType.CODE: BlockTool(CodeBlockYAML, ("code",)),
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
            "credential_id",
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
    BlockType.PDF_PARSER: BlockTool(PDFParserBlockYAML, ("file_url", "json_schema")),
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
    BlockType.GOOGLE_SHEETS_READ: BlockTool(
        GoogleSheetsReadBlockYAML, ("spreadsheet_url", "sheet_name", "range", "credential_id", "has_header_row")
    ),
    BlockType.GOOGLE_SHEETS_WRITE: BlockTool(
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
    ),
    BlockType.PDF_FILL: BlockTool(PdfFillBlockYAML, ("file_url", "prompt", "payload")),
    BlockType.SPLIT_PDF: BlockTool(SplitPdfBlockYAML, ("file_url", "prompt")),
    BlockType.EMAIL_INBOX: BlockTool(
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
RUN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_run_data": "Read what this run already has. Without name: the names of its workflow parameters, its "
    "earlier blocks with their status, and its downloaded files. With name: that parameter's value, that block's "
    "output and outcome, or that file's URL. Secret values are masked. With offset, a cut result from that "
    "character on.",
    "page_goto": "Open url in this run's browser.",
    "page_click": "Click the element prompt describes." + _NO_SECOND_AGENT,
    "page_fill": "Type value into the field prompt describes." + _NO_SECOND_AGENT,
    "page_extract": "Read what prompt asks for from the page, shaped by schema, a JSON schema. For a yes/no check, "
    'pass {"type": "boolean"}.' + _NO_SECOND_AGENT,
}
FINISH_DESCRIPTION = "Return this step's answer. The arguments are the answer."

if {*DEDICATED_BLOCK_TOOLS, *RUN_BLOCK_TYPES, *RUN_TOOL_DESCRIPTIONS, "finish"} != AGENT_STEP_TOOL_NAMES:
    raise RuntimeError("AGENT_STEP_TOOL_NAMES must list exactly the agent step's tool table.")


def block_description(block_type: BlockType, tool: BlockTool) -> str:
    return tool.description or BLOCK_SUMMARIES[block_type.value]


def run_block_description(block_types: Iterable[str]) -> str:
    return (
        "Run one workflow block in this run and return its status, output and error. arguments holds "
        "that block's fields; bad arguments return the block's argument schema. Block types:\n"
        + "\n".join(
            f"- {name}({', '.join(tool.fields)}): {block_description(block_type, tool).rstrip('.')}"
            for name in block_types
            for block_type, tool in [RUN_BLOCK_TYPES[name]]
        )
    )


def tool_descriptions() -> dict[str, str]:
    """Every name a step's tools list may hold. A block type is called through run_block; login needs a credential."""
    return {
        **{name: block_description(block_type, tool) for name, (block_type, tool) in DEDICATED_BLOCK_TOOLS.items()},
        **{name: block_description(block_type, tool) for name, (block_type, tool) in RUN_BLOCK_TYPES.items()},
        **RUN_TOOL_DESCRIPTIONS,
        "finish": FINISH_DESCRIPTION,
    }


class AgentStepToolOption(BaseModel):
    name: str
    description: str
    # None for the page tools, which are not blocks.
    block_type: BlockType | None


def tool_options() -> list[AgentStepToolOption]:
    """The tools an author chooses from."""
    blocks = {**DEDICATED_BLOCK_TOOLS, **RUN_BLOCK_TYPES}
    return [
        AgentStepToolOption(name=name, description=description, block_type=blocks[name][0] if name in blocks else None)
        for name, description in sorted(tool_descriptions().items())
        if name not in ALWAYS_OFFERED_TOOLS
    ]
