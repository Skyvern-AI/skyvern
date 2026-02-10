"""Skyvern MCP block tools — discover block types and schemas for workflow definitions.

Tools for listing available workflow block types and retrieving their Pydantic schemas,
knowledge base descriptions, and minimal examples. These tools do not require a browser
session or API connection — they serve pure metadata from the codebase.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

import structlog
from pydantic import Field, TypeAdapter, ValidationError

from skyvern.schemas.workflows import (
    BLOCK_YAML_TYPES,
    ActionBlockYAML,
    BlockType,
    BlockYAML,
    CodeBlockYAML,
    ConditionalBlockYAML,
    DownloadToS3BlockYAML,
    ExtractionBlockYAML,
    FileDownloadBlockYAML,
    FileParserBlockYAML,
    FileUploadBlockYAML,
    ForLoopBlockYAML,
    HttpRequestBlockYAML,
    HumanInteractionBlockYAML,
    LoginBlockYAML,
    NavigationBlockYAML,
    PDFParserBlockYAML,
    PrintPageBlockYAML,
    SendEmailBlockYAML,
    TaskBlockYAML,
    TaskV2BlockYAML,
    TextPromptBlockYAML,
    UploadToS3BlockYAML,
    UrlBlockYAML,
    ValidationBlockYAML,
    WaitBlockYAML,
)

from ._common import ErrorCode, make_error, make_result

LOG = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Block type → YAML class mapping
# ---------------------------------------------------------------------------

BLOCK_TYPE_MAP: dict[str, type[BlockYAML]] = {
    BlockType.TASK.value: TaskBlockYAML,
    BlockType.TaskV2.value: TaskV2BlockYAML,
    BlockType.FOR_LOOP.value: ForLoopBlockYAML,
    BlockType.CONDITIONAL.value: ConditionalBlockYAML,
    BlockType.CODE.value: CodeBlockYAML,
    BlockType.TEXT_PROMPT.value: TextPromptBlockYAML,
    BlockType.EXTRACTION.value: ExtractionBlockYAML,
    BlockType.ACTION.value: ActionBlockYAML,
    BlockType.NAVIGATION.value: NavigationBlockYAML,
    BlockType.LOGIN.value: LoginBlockYAML,
    BlockType.WAIT.value: WaitBlockYAML,
    BlockType.VALIDATION.value: ValidationBlockYAML,
    BlockType.HTTP_REQUEST.value: HttpRequestBlockYAML,
    BlockType.SEND_EMAIL.value: SendEmailBlockYAML,
    BlockType.FILE_DOWNLOAD.value: FileDownloadBlockYAML,
    BlockType.FILE_UPLOAD.value: FileUploadBlockYAML,
    BlockType.GOTO_URL.value: UrlBlockYAML,
    BlockType.DOWNLOAD_TO_S3.value: DownloadToS3BlockYAML,
    BlockType.UPLOAD_TO_S3.value: UploadToS3BlockYAML,
    BlockType.FILE_URL_PARSER.value: FileParserBlockYAML,
    BlockType.PDF_PARSER.value: PDFParserBlockYAML,
    BlockType.HUMAN_INTERACTION.value: HumanInteractionBlockYAML,
    BlockType.PRINT_PAGE.value: PrintPageBlockYAML,
}

# ---------------------------------------------------------------------------
# One-line summaries
# ---------------------------------------------------------------------------

BLOCK_SUMMARIES: dict[str, str] = {
    "task": "AI agent navigates a page, fills forms, clicks buttons (v1 engine)",
    "task_v2": "AI agent with natural language prompt (v2 engine, recommended for complex tasks)",
    "for_loop": "Iterate over a list, executing nested blocks for each item",
    "conditional": "Branch based on Jinja2 expressions or AI prompts",
    "code": "Run Python code for data transformation",
    "text_prompt": "LLM text generation without a browser",
    "extraction": "Extract structured data from the current page",
    "action": "Perform a single focused action on the current page",
    "navigation": "Navigate to a goal on the current page (Browser Task in UI)",
    "login": "Handle authentication flows including username/password and TOTP/2FA",
    "wait": "Pause workflow execution for a specified duration",
    "validation": "Validate page state with complete/terminate criteria",
    "http_request": "Call an external HTTP API",
    "send_email": "Send an email notification via SMTP",
    "file_download": "Download a file from a page",
    "file_upload": "Upload a file from S3/Azure to a page element",
    "goto_url": "Navigate directly to a URL without additional instructions",
    "download_to_s3": "Download a URL directly to S3 storage",
    "upload_to_s3": "Upload local content to S3",
    "file_url_parser": "Parse a file (CSV/Excel/PDF/image) from a URL",
    "pdf_parser": "Extract structured data from a PDF document",
    "human_interaction": "Pause workflow for human approval via email",
    "print_page": "Print the current page to PDF",
}

# ---------------------------------------------------------------------------
# Minimal examples for common block types
# ---------------------------------------------------------------------------

BLOCK_EXAMPLES: dict[str, dict[str, Any]] = {
    "task": {
        "block_type": "task",
        "label": "fill_form",
        "url": "https://example.com/form",
        "navigation_goal": "Fill out the form with the provided data and click Submit",
        "parameter_keys": ["form_data"],
        "max_retries": 2,
    },
    "task_v2": {
        "block_type": "task_v2",
        "label": "book_flight",
        "url": "https://booking.example.com",
        "prompt": "Book a flight from {{ origin }} to {{ destination }} on {{ date }}",
    },
    "for_loop": {
        "block_type": "for_loop",
        "label": "process_each_url",
        "loop_over_parameter_key": "urls",
        "loop_blocks": [
            {
                "block_type": "goto_url",
                "label": "open_url",
                "url": "{{ current_value }}",
            }
        ],
    },
    "conditional": {
        "block_type": "conditional",
        "label": "route_by_status",
        "branch_conditions": [
            {
                "criteria": {
                    "criteria_type": "jinja2_template",
                    "expression": "{{ status == 'active' }}",
                },
                "next_block_label": "handle_active",
                "is_default": False,
            },
            {"is_default": True, "next_block_label": "handle_inactive"},
        ],
    },
    "extraction": {
        "block_type": "extraction",
        "label": "extract_products",
        "data_extraction_goal": "Extract all products with name, price, and stock status",
        "data_schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                    "in_stock": {"type": "boolean"},
                },
            },
        },
    },
    "navigation": {
        "block_type": "navigation",
        "label": "search_and_open",
        "url": "https://example.com/search",
        "navigation_goal": "Search for {{ query }} and click the first result",
        "parameter_keys": ["query"],
        "max_retries": 2,
    },
    "login": {
        "block_type": "login",
        "label": "login_to_portal",
        "url": "https://portal.example.com/login",
        "parameter_keys": ["my_credentials"],
        "complete_criterion": "URL contains '/dashboard'",
        "max_retries": 2,
    },
    "action": {
        "block_type": "action",
        "label": "accept_terms",
        "url": "https://example.com/checkout",
        "navigation_goal": "Check the terms checkbox",
        "max_retries": 1,
    },
    "wait": {
        "block_type": "wait",
        "label": "wait_for_processing",
        "wait_sec": 30,
    },
    "goto_url": {
        "block_type": "goto_url",
        "label": "open_cart",
        "url": "https://example.com/cart",
    },
}

# ---------------------------------------------------------------------------
# Knowledge base parsing (lazy, cached)
# ---------------------------------------------------------------------------

_KB_PATH = Path(__file__).resolve().parents[2] / "forge" / "prompts" / "skyvern" / "workflow_knowledge_base.txt"

_HEADER_RE = re.compile(r"^\*\*\s+(.+?)\s+\((\w+)\)\s+\*\*$")

_kb_cache: dict[str, dict[str, Any]] | None = None


def _parse_knowledge_base() -> dict[str, dict[str, Any]]:
    """Parse the knowledge base file into per-block-type sections.

    Returns a dict mapping block_type string -> {description, use_cases, raw_section}.
    Results are cached in the module-level ``_kb_cache`` variable.
    """
    global _kb_cache
    if _kb_cache is not None:
        return _kb_cache

    result: dict[str, dict[str, Any]] = {}

    try:
        text = _KB_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        LOG.warning("workflow_knowledge_base_not_found", path=str(_KB_PATH))
        _kb_cache = result
        return result

    sections: list[tuple[str, str]] = []
    current_block_type: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        match = _HEADER_RE.match(line.strip())
        if match:
            if current_block_type is not None:
                sections.append((current_block_type, "\n".join(current_lines)))
            current_block_type = match.group(2).lower()
            current_lines = []
        elif current_block_type is not None:
            current_lines.append(line)

    if current_block_type is not None:
        sections.append((current_block_type, "\n".join(current_lines)))

    for block_type, raw in sections:
        description_lines: list[str] = []
        use_cases: list[str] = []
        in_use_cases = False
        in_purpose = False

        for line in raw.splitlines():
            stripped = line.strip()

            if stripped.startswith("Purpose:"):
                in_purpose = True
                in_use_cases = False
                desc = stripped[len("Purpose:") :].strip()
                if desc:
                    description_lines.append(desc)
                continue

            if stripped == "Use Cases:":
                in_use_cases = True
                in_purpose = False
                continue

            # Any other header-like line ends the current section
            if stripped and stripped.endswith(":") and not stripped.startswith("- "):
                in_use_cases = False
                in_purpose = False
                continue

            if in_purpose and stripped:
                description_lines.append(stripped)

            if in_use_cases and stripped.startswith("- "):
                use_cases.append(stripped[2:].strip())

        result[block_type] = {
            "description": " ".join(description_lines) if description_lines else None,
            "use_cases": use_cases if use_cases else None,
        }

    _kb_cache = result
    return result


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


async def skyvern_block_schema(
    block_type: Annotated[
        str | None,
        Field(
            description="Block type to get schema for (e.g., 'task_v2', 'for_loop'). Omit to list all available types."
        ),
    ] = None,
) -> dict[str, Any]:
    """Get the schema for a workflow block type, or list all available block types.

    Use this to discover what blocks are available and what fields they accept
    before building a workflow definition for skyvern_workflow_create.

    Call with no arguments to see all block types. Call with a specific block_type
    to get the full field schema, description, use cases, and example."""

    action = "skyvern_block_schema"

    if block_type is None:
        return make_result(
            action,
            data={
                "block_types": BLOCK_SUMMARIES,
                "count": len(BLOCK_SUMMARIES),
                "hint": "Call skyvern_block_schema(block_type='task_v2') for the full schema of a specific type",
            },
        )

    normalized = block_type.strip().lower()
    cls = BLOCK_TYPE_MAP.get(normalized)
    if cls is None:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Unknown block type: {block_type!r}",
                f"Available types: {', '.join(sorted(BLOCK_TYPE_MAP.keys()))}",
            ),
        )

    kb = _parse_knowledge_base()
    kb_entry = kb.get(normalized, {})

    return make_result(
        action,
        data={
            "block_type": normalized,
            "summary": BLOCK_SUMMARIES.get(normalized, ""),
            "description": kb_entry.get("description"),
            "use_cases": kb_entry.get("use_cases"),
            "schema": cls.model_json_schema(),
            "example": BLOCK_EXAMPLES.get(normalized),
        },
    )


# ---------------------------------------------------------------------------
# Block validation adapter (lazy)
# ---------------------------------------------------------------------------

# BLOCK_YAML_TYPES is a large Union of ~23 block models; mypy/pyright cannot resolve it as a TypeAdapter generic argument
_block_adapter: TypeAdapter[BLOCK_YAML_TYPES] | None = None  # type: ignore[type-arg]


def _get_block_adapter() -> TypeAdapter[BLOCK_YAML_TYPES]:  # type: ignore[type-arg]
    global _block_adapter
    if _block_adapter is None:
        _block_adapter = TypeAdapter(BLOCK_YAML_TYPES)
    return _block_adapter


# ---------------------------------------------------------------------------
# Validate tool
# ---------------------------------------------------------------------------


async def skyvern_block_validate(
    block_json: Annotated[
        str,
        Field(description="JSON string of a single block definition to validate"),
    ],
) -> dict[str, Any]:
    """Validate a workflow block definition before using it in skyvern_workflow_create.

    Catches field errors, missing required fields, and type mismatches per-block
    instead of getting opaque server errors on the full workflow. Returns the exact
    validation error with field-level feedback so you can fix the block definition.
    """
    action = "skyvern_block_validate"

    try:
        raw = json.loads(block_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid JSON: {exc}",
                "Provide a valid JSON string representing a block definition",
            ),
        )

    if not isinstance(raw, dict):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Expected a JSON object, got {type(raw).__name__}",
                "Provide a JSON object with at least block_type and label fields",
            ),
        )

    adapter = _get_block_adapter()
    try:
        block = adapter.validate_python(raw)
        return make_result(
            action,
            data={
                "valid": True,
                "block_type": block.block_type,
                "label": block.label,
                "field_count": len([f for f in block.model_fields_set if f != "block_type"]),
            },
        )
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = " → ".join(str(p) for p in err["loc"]) if err["loc"] else "(root)"
            errors.append(f"{loc}: {err['msg']}")
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Block validation failed ({len(exc.errors())} error{'s' if len(exc.errors()) != 1 else ''}): "
                + "; ".join(errors[:5]),
                "Fix the fields listed above. Call skyvern_block_schema(block_type='...') to see the correct schema.",
            ),
        )
