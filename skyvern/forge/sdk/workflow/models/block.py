from __future__ import annotations

import ast
import json
import re
from typing import Annotated, Any, Union

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import Field

from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.constants import OUTPUT_PARAMETER_MAX_VALUE_BYTES
from skyvern.forge.sdk.workflow.models.block_base import (  # noqa: F401  (re-exported for tests/back-compat)
    CURRENT_DATE_FORMAT,
    MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
    Block,
    capture_block_download_baseline,
    jinja_sandbox_env,
    warn_if_file_download_max_steps_low,
)
from skyvern.schemas.workflows import (  # noqa: F401  # FileType re-exported for callers importing it from this module
    AIFallbackMode,
    BlockResult,
    BlockStatus,
    BlockType,
    FileType,
)

LOG = structlog.get_logger()


# Mapping from TaskV2Status to the corresponding BlockStatus. Declared once at
# import time so it is not recreated on each block execution.
TASKV2_TO_BLOCK_STATUS: dict[TaskV2Status, BlockStatus] = {
    TaskV2Status.completed: BlockStatus.completed,
    TaskV2Status.terminated: BlockStatus.terminated,
    TaskV2Status.failed: BlockStatus.failed,
    TaskV2Status.canceled: BlockStatus.canceled,
    TaskV2Status.timed_out: BlockStatus.timed_out,
}

TASK_TO_BLOCK_STATUS: dict[TaskStatus, BlockStatus] = {
    TaskStatus.completed: BlockStatus.completed,
    TaskStatus.terminated: BlockStatus.terminated,
    TaskStatus.failed: BlockStatus.failed,
    TaskStatus.canceled: BlockStatus.canceled,
    TaskStatus.timed_out: BlockStatus.timed_out,
}


def extract_file_url_from_block_output(value: Any) -> str | None:
    """Extract a file URL from block output values that wrap downloaded files."""
    if isinstance(value, dict):
        downloaded_files = value.get("downloaded_files")
        if isinstance(downloaded_files, list) and downloaded_files:
            first_file = downloaded_files[0]
            if isinstance(first_file, dict):
                return first_file.get("url") or first_file.get("file_path") or None

        for key in ("artifact_url", "file_url", "file_path"):
            extracted = value.get(key)
            if isinstance(extracted, str) and extracted:
                return extracted
        return None

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return extract_file_url_from_block_output(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, dict):
                return extract_file_url_from_block_output(parsed)
        except (ValueError, SyntaxError):
            pass
    return None


def sanitize_filename(filename: str, default: str = "document") -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename).strip(". ")
    return sanitized[:200] if sanitized else default


def _format_payload_path_segment(key: str) -> str:
    """Plain identifiers render as `.key`; anything else (dots, brackets, spaces,
    quotes) renders as a bracketed JSON-escaped string so paths stay unambiguous
    against keys that contain `.` or `[`."""
    if key.isidentifier():
        return f".{key}"
    return f"[{json.dumps(key)}]"


# ForLoop constants
DEFAULT_MAX_LOOP_ITERATIONS = 500
MAX_LOOP_OVER_VALUE_LOG_CHARS = 2000
# Persist accumulated loop output to DB every N iterations to survive timeouts.
# Trades up to N-1 iterations of data loss for O(N/K) writes instead of O(N).
PERSIST_LOOP_OUTPUT_INTERVAL = 10
DEFAULT_MAX_STEPS_PER_ITERATION = 50


def _maybe_truncate_loop_outputs(
    outputs_with_loop_values: list[list[dict[str, Any]]],
    *,
    workflow_run_id: str,
    output_parameter_id: str | None,
) -> None:
    """Fail-open in-memory cap for loop accumulators; preserves per-entry schema (SKY-9779)."""
    try:
        size_bytes = len(json.dumps(outputs_with_loop_values, default=str).encode("utf-8"))
    except Exception:
        LOG.warning(
            "Failed to measure loop output size; skipping truncation",
            workflow_run_id=workflow_run_id,
            output_parameter_id=output_parameter_id,
            exc_info=True,
        )
        return

    if size_bytes <= OUTPUT_PARAMETER_MAX_VALUE_BYTES:
        return

    summarized_through = len(outputs_with_loop_values) - 1
    summary_entry = [
        {
            "loop_value": None,
            "output_parameter": None,
            "output_value": {
                "truncated": True,
                "reason": "loop_output_size_exceeded",
                "iterations_summarized_through": summarized_through,
            },
        }
    ]
    LOG.warning(
        "Truncating loop output accumulator",
        workflow_run_id=workflow_run_id,
        output_parameter_id=output_parameter_id,
        size_bytes=size_bytes,
        limit_bytes=OUTPUT_PARAMETER_MAX_VALUE_BYTES,
        iterations_summarized_through=summarized_through,
    )
    last = outputs_with_loop_values[-1]
    outputs_with_loop_values.clear()
    outputs_with_loop_values.append(summary_entry)
    outputs_with_loop_values.append(last)


SCHEMA_VALIDATION_MAX_ATTEMPTS = 2
SCHEMA_VALIDATION_MAX_ERRORS = 5


def _default_structured_output_schema(description: str) -> dict[str, Any]:
    # The output field is optional to preserve the legacy permissive default schema.
    return {
        "type": "object",
        "properties": {
            "output": {
                "type": "object",
                "description": description,
            }
        },
    }


def _default_text_prompt_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "llm_response": {
                "type": "string",
                "description": "Your response to the prompt",
            }
        },
    }


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _schema_type_description(schema_type: Any) -> str:
    if isinstance(schema_type, list):
        return " or ".join(str(t) for t in schema_type)
    return str(schema_type)


def _schema_path(error: ValidationError) -> str:
    schema_path = list(error.absolute_schema_path)
    path_parts: list[str] = []
    index = 0
    while index < len(schema_path):
        part = schema_path[index]
        if part == "properties" and index + 1 < len(schema_path):
            path_parts.append(str(schema_path[index + 1]))
            index += 2
            continue
        if part == "items":
            path_parts.append("[]")
            index += 1
            continue
        if part == "additionalProperties":
            path_parts.append("<map value>")
            index += 1
            continue
        if part == "patternProperties":
            path_parts.append("<map value>")
            index += 2 if index + 1 < len(schema_path) else 1
            continue
        index += 1

    return "root" + "".join(f"{part}" if part == "[]" else f".{part}" for part in path_parts)


def _format_schema_validation_error(error: ValidationError) -> str:
    path = _schema_path(error)
    actual_type = _json_type_name(error.instance)

    if error.validator == "type":
        expected_type = _schema_type_description(error.validator_value)
        return f"{path}: expected type {expected_type}, got {actual_type}"

    if error.validator == "required":
        match = re.match(r"'([^']+)' is a required property", error.message)
        if match:
            return f"{path}: missing required property {match.group(1)}"
        return f"{path}: missing required property"

    if error.validator == "additionalProperties":
        unexpected_count: int | None = None
        schema_properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        if isinstance(error.instance, dict) and isinstance(schema_properties, dict):
            unexpected_count = sum(1 for field in error.instance if field not in schema_properties)
        if unexpected_count is not None:
            return f"{path}: has {unexpected_count} unexpected properties"
        return f"{path}: has unexpected properties"

    if error.validator in {"minItems", "maxItems"} and isinstance(error.instance, list):
        return f"{path}: violates {error.validator}={error.validator_value}; item count={len(error.instance)}"

    if error.validator in {"minLength", "maxLength"} and isinstance(error.instance, str):
        return f"{path}: violates {error.validator}={error.validator_value}; string length={len(error.instance)}"

    if error.validator == "enum":
        allowed_count = len(error.validator_value) if isinstance(error.validator_value, list) else "configured"
        return f"{path}: value is not one of {allowed_count} allowed values; got {actual_type}"

    return f"{path}: violates {error.validator} constraint; got {actual_type}"


def _validate_response_against_json_schema(
    response: Any,
    json_schema: dict[str, Any] | None,
    schema_label: str,
    max_errors: int = SCHEMA_VALIDATION_MAX_ERRORS,
) -> str | None:
    if not json_schema:
        return None

    if not validate_schema(json_schema):
        return f"{schema_label} JSON schema is invalid."

    try:
        validator = Draft202012Validator(json_schema)
        validation_errors = [_format_schema_validation_error(error) for error in validator.iter_errors(response)]
    except Exception as e:
        LOG.warning(
            "Failed to validate LLM response against JSON schema",
            schema_label=schema_label,
            error_type=type(e).__name__,
            exc_info=True,
        )
        return f"{schema_label} JSON schema validation failed ({type(e).__name__})."

    validation_errors = list(dict.fromkeys(validation_errors))
    if not validation_errors:
        return None

    return f"LLM response does not match {schema_label.lower()} JSON schema: " + "; ".join(
        validation_errors[:max_errors]
    )


def _is_schema_configuration_failure(failure_reason: str) -> bool:
    return "JSON schema is invalid" in failure_reason or "JSON schema validation failed" in failure_reason


def _llm_response_format_failure_reason(error: Exception) -> str:
    return f"LLM response could not be parsed or coerced into the required JSON shape ({type(error).__name__})."


def _build_schema_validation_retry_prompt(prompt: str, failure_reason: str) -> str:
    return (
        f"{prompt}\n\n"
        "Your previous response failed JSON schema validation.\n"
        f"Validation error: {failure_reason}\n\n"
        "Retry the task. Return only valid JSON that exactly matches the schema. "
        "Do not include markdown, code fences, explanatory text, or extra fields."
    )


def get_all_blocks(blocks: list[BlockTypeVar]) -> list[BlockTypeVar]:
    """
    Recursively get "all blocks" in a workflow definition.

    Blocks can be nested via ForLoop and WhileLoop blocks. This function returns
    all blocks, flattened.
    """

    all_blocks: list[BlockTypeVar] = []

    for block in blocks:
        all_blocks.append(block)

        if block.block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
            nested_blocks = get_all_blocks(block.loop_blocks)
            all_blocks.extend(nested_blocks)

    return all_blocks


# isort: off
# branching.py is a leaf module (it only imports Block-module names lazily at call time),
# so this import is cycle-safe in any import order. It lives down here with the other
# submodule imports to keep the facade re-export section in one place.
# ``_evaluate_prompt_branch_conditions_batch`` is re-exported because WhileLoopBlock (which
# stays here) drives while-loop conditions through it. ``ConditionalBlock`` is defined below
# rather than in branching.py because it subclasses ``Block``; a module-level Block import in
# branching.py would make it un-importable on its own (see PR #6979 review).
from skyvern.forge.sdk.workflow.models.branching import (  # noqa: E402
    DECISION_BLOCK_FIELD_MAX_BYTES,  # noqa: F401 - re-exported for facade compatibility
    BranchCondition,  # noqa: F401 - re-exported for facade compatibility
    BranchCriteria,  # noqa: F401 - re-exported for facade compatibility
    BranchCriteriaSubclasses,  # noqa: F401 - re-exported for facade compatibility
    BranchCriteriaTypeVar,  # noqa: F401 - re-exported for facade compatibility
    BranchEvaluationContext,  # noqa: F401 - re-exported for facade compatibility
    JinjaBranchCriteria,  # noqa: F401 - re-exported for facade compatibility
    PromptBranchCriteria,  # noqa: F401 - re-exported for facade compatibility
    _build_branch_evaluation_schema,  # noqa: F401 - re-exported for facade compatibility
    _cap_debug_field,  # noqa: F401 - re-exported for facade compatibility
    _coerce_condition_index,  # noqa: F401 - re-exported for facade compatibility
    _evaluate_prompt_branch_conditions_batch,  # noqa: F401 - re-exported for facade compatibility
    _make_empty_params_explicit,  # noqa: F401 - re-exported for facade compatibility
    _render_jinja_expression_for_display,  # noqa: F401 - re-exported for facade compatibility
    _trim_branch_evaluations,  # noqa: F401 - re-exported for facade compatibility
)

# Late import: these sibling modules import Block from block_base, so this re-export lives at the
# bottom; every name below is re-exported for zero call-site changes.
from skyvern.forge.sdk.workflow.models.code_block import (  # noqa: E402, F401
    CodeBlock,
    CodeBlockOTPError,
    CodeBlockStep,
    Credential,
    _bind_code_block_otp,
    _code_block_otp_builtin,
    _register_code_block_secret,
    _resolve_code_block_otp,
)
from skyvern.forge.sdk.workflow.models.parser_blocks import (  # noqa: E402
    FileParserBlock,
    PDFParserBlock,
)
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import (  # noqa: E402
    GoogleSheetsReadBlock,
    GoogleSheetsWriteBlock,
)
from skyvern.forge.sdk.workflow.models.pdf_fill_block import PdfFillBlock  # noqa: E402
from skyvern.forge.sdk.workflow.models.storage_blocks import (  # noqa: E402
    DownloadToS3Block,
    FileUploadBlock,
    UploadToS3Block,
)
from skyvern.forge.sdk.workflow.models.misc_blocks import (  # noqa: E402, F401
    SECRET_RESPONSE_BODY_REDACTED,
    HttpRequestBlock,
    PrintPageBlock,
    SendEmailBlock,
    TaskV2Block,
    TextPromptBlock,
    WaitBlock,
    WorkflowTriggerBlock,
    _apply_secret_response_paths,
    _secret_path_suffix,
)
from skyvern.forge.sdk.workflow.models.task_blocks import (  # noqa: E402, F401  (re-exported for tests/back-compat)
    ActionBlock,
    BaseTaskBlock,
    ExtractionBlock,
    FileDownloadBlock,
    HumanInteractionBlock,
    LoginBlock,
    NavigationBlock,
    TaskBlock,
    UrlBlock,
    ValidationBlock,
    _should_skip_retry_on_anti_bot_detection,
)
from skyvern.forge.sdk.workflow.models.control_flow_blocks import (  # noqa: E402, F401
    ConditionalBlock,
    ForLoopBlock,
    LoopBlockExecutedResult,
    WhileLoopBlock,
    compute_conditional_scopes,
)
# isort: on


BlockSubclasses = Union[
    ConditionalBlock,
    ForLoopBlock,
    WhileLoopBlock,
    TaskBlock,
    CodeBlock,
    TextPromptBlock,
    DownloadToS3Block,
    UploadToS3Block,
    SendEmailBlock,
    FileParserBlock,
    PDFParserBlock,
    ValidationBlock,
    ActionBlock,
    NavigationBlock,
    ExtractionBlock,
    LoginBlock,
    WaitBlock,
    HumanInteractionBlock,
    FileDownloadBlock,
    UrlBlock,
    TaskV2Block,
    FileUploadBlock,
    HttpRequestBlock,
    PrintPageBlock,
    WorkflowTriggerBlock,
    GoogleSheetsReadBlock,
    GoogleSheetsWriteBlock,
    PdfFillBlock,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]

# ForLoopBlock/WhileLoopBlock live in control_flow_blocks.py and type ``loop_blocks`` as
# ``list[BlockTypeVar]``; the discriminated union is only complete here. Surface it in their
# module namespace and rebuild their schemas so pydantic can resolve the forward reference
# (mirrors the monolith, where all block classes shared this module).
import skyvern.forge.sdk.workflow.models.control_flow_blocks as _control_flow_blocks  # noqa: E402

_control_flow_blocks.BlockTypeVar = BlockTypeVar
_control_flow_blocks.ForLoopBlock.model_rebuild(force=True)
_control_flow_blocks.WhileLoopBlock.model_rebuild(force=True)


def resolve_conditional_merge_edges(
    blocks: list[BlockTypeVar],
    label_to_block: dict[str, BlockTypeVar],
    default_next_map: dict[str, str | None],
) -> None:
    """Point each conditional branch chain's terminal block at the conditional's successor (merge point).

    SKY-8571: iterates to convergence so an outer conditional patched on one pass can let an inner
    conditional's branch terminals be patched on the next. Mutates default_next_map in place.
    """
    changed = True
    while changed:
        changed = False
        for block in blocks:
            if not isinstance(block, ConditionalBlock):
                continue
            successor = default_next_map.get(block.label)
            if not successor:
                continue
            for branch in block.ordered_branches:
                target = branch.next_block_label
                if not target or target == successor:
                    continue
                cur: str | None = target
                visited: set[str] = set()
                while cur and cur in label_to_block and cur not in visited:
                    if cur == successor:
                        break
                    visited.add(cur)
                    nxt = default_next_map.get(cur)
                    if nxt is None:
                        default_next_map[cur] = successor
                        changed = True
                        break
                    cur = nxt
