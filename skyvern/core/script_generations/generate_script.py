# skyvern_codegen_cst.py
"""
Generate a runnable Skyvern workflow script.

"""

from __future__ import annotations

import asyncio
import hashlib
import keyword
import re
from dataclasses import dataclass
from typing import Any

import libcst as cst
import structlog
from libcst import Attribute, Call, Dict, DictElement, FunctionDef, Name, Param

from skyvern.config import settings
from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS, SCRIPT_TASK_BLOCKS_WITH_COMPLETE_ACTION
from skyvern.core.script_generations.generate_workflow_parameters import (
    CUSTOM_FIELD_ACTIONS,
    generate_workflow_parameters_schema,
    hydrate_input_text_actions_with_field_names,
)
from skyvern.forge import app
from skyvern.schemas.workflows import FileStorageType
from skyvern.utils.strings import sanitize_identifier
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger(__name__)
GENERATE_CODE_AI_MODE_PROACTIVE = "proactive"
GENERATE_CODE_AI_MODE_FALLBACK = "fallback"


@dataclass
class ScriptBlockSource:
    label: str
    code: str
    run_signature: str | None
    workflow_run_id: str | None
    workflow_run_block_id: str | None
    input_fields: list[str] | None
    requires_agent: bool | None = None


def _build_existing_field_assignments(
    blocks: list[dict[str, Any]],
    actions_by_task: dict[str, list[dict[str, Any]]],
    cached_blocks: dict[str, ScriptBlockSource],
    updated_block_labels: set[str],
) -> dict[int, str]:
    """
    Build a mapping of action index (1-based) to existing field names for unchanged blocks.

    This is used to tell the LLM which field names must be preserved when regenerating
    the workflow parameters schema, preventing schema mismatches with cached block code.

    Args:
        blocks: List of block dictionaries from the workflow
        actions_by_task: Dictionary mapping task IDs to lists of action dictionaries
        cached_blocks: Dictionary mapping block labels to their cached ScriptBlockSource
        updated_block_labels: Set of block labels that have been updated (should not preserve)

    Returns:
        Dictionary mapping action index (1-based) to the existing field name that must be preserved
    """
    # Build mapping of block label -> task_id
    block_label_to_task_id: dict[str, str] = {}
    for idx, block in enumerate(blocks):
        if block.get("block_type") not in SCRIPT_TASK_BLOCKS:
            continue
        label = block.get("label") or block.get("title") or block.get("task_id") or f"task_{idx}"
        task_id = block.get("task_id")
        if task_id:
            block_label_to_task_id[label] = task_id

    # Build mapping of task_id -> list of existing field names (for unchanged blocks)
    task_id_to_existing_fields: dict[str, list[str]] = {}
    for label, cached_source in cached_blocks.items():
        # Skip blocks that have been updated - they need new field names
        if label in updated_block_labels:
            continue
        # Skip blocks without input_fields
        if not cached_source.input_fields:
            continue
        # Find the task_id for this block
        task_id = block_label_to_task_id.get(label)
        if task_id:
            task_id_to_existing_fields[task_id] = list(cached_source.input_fields)

    # Now iterate through actions in the same order as generate_workflow_parameters_schema
    # to build the action index -> field name mapping
    existing_field_assignments: dict[int, str] = {}
    action_counter = 1

    # Track position within each task's field list
    task_field_position: dict[str, int] = {}

    for task_id, actions in actions_by_task.items():
        for action in actions:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            # Check if this task has existing field names to preserve
            if task_id in task_id_to_existing_fields:
                existing_fields = task_id_to_existing_fields[task_id]
                position = task_field_position.get(task_id, 0)

                if position < len(existing_fields):
                    existing_field_assignments[action_counter] = existing_fields[position]
                    task_field_position[task_id] = position + 1

            action_counter += 1

    return existing_field_assignments


# --------------------------------------------------------------------- #
# 1. helpers                                                            #
# --------------------------------------------------------------------- #


def sanitize_variable_name(name: str) -> str:
    """
    Sanitize a string to be a valid Python variable name.

    - Converts to snake_case
    - Removes invalid characters (via shared sanitize_identifier)
    - Ensures it doesn't start with a number
    - Handles Python keywords by appending underscore
    - Converts to lowercase
    """
    # Convert to snake_case: handle camelCase and PascalCase
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)

    # Convert to lowercase before sanitizing
    name = name.lower()

    # Use shared sanitize_identifier for core cleanup (uses "_" prefix for digit-leading names)
    name = sanitize_identifier(name, default="param")

    # For script variable names, use "param_" prefix instead of bare "_" for digit-leading names
    if name.startswith("_") and len(name) > 1 and name[1].isdigit():
        name = f"param{name}"

    # Handle Python keywords
    if keyword.iskeyword(name):
        name = f"{name}_"

    return name


ACTION_MAP = {
    "click": "click",
    "hover": "hover",
    "input_text": "fill",
    "upload_file": "upload_file",
    "select_option": "select_option",
    "goto": "goto",
    "scroll": "scroll",
    "keypress": "keypress",
    "type": "type",
    "move": "move",
    "drag": "drag",
    "solve_captcha": "solve_captcha",
    "verification_code": "verification_code",
    "wait": "wait",
    "extract": "extract",
    "complete": "complete",
    "download_file": "download_file",
}
ACTIONS_WITH_XPATH = [
    "click",
    "hover",
    "input_text",
    "type",
    "fill",
    "upload_file",
    "select_option",
]


def _build_semantic_selector(act: dict[str, Any]) -> str | None:
    """Build a semantic CSS selector from element data.

    Priority order:
    1. aria-label (most reliable semantic identifier)
    2. placeholder (for inputs)
    3. name attribute (common form field identifier)
    4. text content (for buttons, links)
    5. Fall back to None (use ai='fallback' with only prompt=)
    """
    element_data = act.get("skyvern_element_data") or {}
    attrs = element_data.get("attributes", {})
    tag = element_data.get("tagName", "")
    text = (element_data.get("text") or "").strip()

    # Try aria-label first
    aria_label = attrs.get("aria-label", "")
    if aria_label:
        escaped = aria_label.replace('"', '\\"')
        return f'{tag}[aria-label="{escaped}"]' if tag else f'[aria-label="{escaped}"]'

    # Try placeholder (for input/textarea)
    placeholder = attrs.get("placeholder", "")
    if placeholder and tag in ("input", "textarea"):
        escaped = placeholder.replace('"', '\\"')
        return f'{tag}[placeholder="{escaped}"]'

    # Try name attribute
    name = attrs.get("name", "")
    if name and tag in ("input", "textarea", "select"):
        escaped = name.replace('"', '\\"')
        return f'{tag}[name="{escaped}"]'

    # Try text content (for buttons, links, labels)
    if text and tag in ("button", "a"):
        short_text = text[:50].replace('"', '\\"')
        return f'{tag}:has-text("{short_text}")'

    # No good semantic selector — return None (caller will use ai-only mode)
    return None


ACTIONS_OPT_OUT_INTENTION_FOR_PROMPT = ["extract"]

INDENT = " " * 4
DOUBLE_INDENT = " " * 8

# Minimum length for a parameter value to be eligible for substitution in click prompts.
# Short values (e.g. "1", "No", "CA") cause too many false-positive replacements.
MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB = 4


def _build_value_to_param_lookup(
    actions_by_task: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """Build a mapping from literal action values to their parameter field names.

    After ``hydrate_input_text_actions_with_field_names`` runs, custom-field actions
    carry both the original literal value (``text``, ``option``, or ``file_url``) and
    the ``field_name`` assigned by the LLM.  This function collects those pairs so
    that click-prompt generation can replace matching literals with
    ``context.parameters['field_name']`` f-string references.

    The returned dict is sorted by *descending value length* so that callers who
    iterate in order will replace longer matches first, avoiding partial collisions.
    """
    raw: dict[str, str] = {}
    for _task_id, actions in actions_by_task.items():
        for action in actions:
            field_name = action.get("field_name")
            if not field_name:
                continue
            action_type = action.get("action_type", "")
            if action_type == ActionType.INPUT_TEXT:
                value = action.get("text", "")
            elif action_type == ActionType.UPLOAD_FILE:
                value = action.get("file_url", "")
            elif action_type == ActionType.SELECT_OPTION:
                value = action.get("option", "")
            else:
                continue
            if value and len(value) >= MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB:
                # First writer wins — the field-level name is more specific than a
                # workflow-level parameter that may happen to share the same value.
                if value not in raw:
                    raw[value] = field_name

    # Sort by descending value length so longer matches are attempted first.
    return dict(sorted(raw.items(), key=lambda kv: len(kv[0]), reverse=True))


def _escape_for_fstring_text(text: str) -> str:
    """Escape ``{`` and ``}`` so they survive inside a ``FormattedStringText`` node.

    Jinja2 templates (e.g. ``{{param}}``) would otherwise be interpreted as
    f-string expressions.  Doubling the braces turns them into literal braces
    in the rendered f-string.
    """
    return text.replace("{", "{{").replace("}", "}}")


def _build_parameterized_prompt_cst(
    intention: str,
    value_to_param: dict[str, str],
) -> cst.BaseExpression | None:
    """If *intention* contains any literal parameter values, return a ``FormattedString``
    CST node (an f-string) with those values replaced by
    ``context.parameters['field_name']`` expressions.

    Returns ``None`` when no substitution is needed (the caller should fall back to
    emitting a plain string literal).
    """
    # Identify all non-overlapping matches, preferring longer values (dict is
    # already sorted by descending length).
    # Each match is (start, end, field_name).
    matches: list[tuple[int, int, str]] = []
    for value, field_name in value_to_param.items():
        start = 0
        while True:
            idx = intention.find(value, start)
            if idx == -1:
                break
            end = idx + len(value)
            # Check overlap with already-accepted matches.
            overlaps = any(not (end <= ms or idx >= me) for ms, me, _ in matches)
            if not overlaps:
                matches.append((idx, end, field_name))
            start = end

    if not matches:
        return None

    # Sort matches by position so we can build the f-string left-to-right.
    matches.sort(key=lambda m: m[0])

    parts: list[cst.BaseFormattedStringContent] = []
    cursor = 0
    for start, end, field_name in matches:
        # Text segment before this match.
        if start > cursor:
            parts.append(cst.FormattedStringText(_escape_for_fstring_text(intention[cursor:start])))
        # The {context.parameters['field_name']} expression.
        parts.append(
            cst.FormattedStringExpression(
                expression=cst.Subscript(
                    value=cst.Attribute(
                        value=cst.Name("context"),
                        attr=cst.Name("parameters"),
                    ),
                    slice=[
                        cst.SubscriptElement(
                            slice=cst.Index(value=_value(field_name)),
                        )
                    ],
                )
            )
        )
        cursor = end

    # Trailing text after last match.
    if cursor < len(intention):
        parts.append(cst.FormattedStringText(_escape_for_fstring_text(intention[cursor:])))

    # Use triple-quote f-string when the content contains newlines or quotes
    # (run_task prompts always have newlines from the appended navigation_payload).
    raw_text = intention
    if "\n" in raw_text or '"' in raw_text or "'" in raw_text:
        quote = '"""'
    else:
        quote = '"'

    return cst.FormattedString(parts=parts, start=f"f{quote}", end=quote)


def _requires_mini_agent(act: dict[str, Any]) -> bool:
    """
    Determine whether an input/select action should be forced into proactive mode.
    Mirrors runtime logic that treats some inputs as mini-agent flows or TOTP-sensitive.

    NOTE: Multi-field TOTP sequences do NOT require proactive mode because we use
    get_totp_digit() to provide the exact digit value. Using proactive mode would
    cause the AI to override our value with its own generated one.
    """
    if act.get("has_mini_agent", False):
        return True

    # context = act.get("input_or_select_context") or {}
    # if isinstance(context, dict) and any(
    #     context.get(flag) for flag in ("is_location_input", "is_date_related", "date_format")
    # ):
    #     return True

    # Multi-field TOTP sequences should NOT use proactive mode - we provide the
    # exact digit via get_totp_digit() and want that value used directly
    # if act.get("totp_timing_info") and act.get("totp_timing_info", {}).get("is_totp_sequence"):
    #     return True

    return False


def _annotate_multi_field_totp_sequence(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Detect and annotate multi-field TOTP sequences in the action list.

    Multi-field TOTP is when a 6-digit code needs to be split across 6 individual input fields.
    This function identifies such sequences and adds totp_timing_info with the action_index
    so that each field gets the correct digit (e.g., totp_code[0], totp_code[1], etc.).

    Args:
        actions: List of actions to analyze and annotate

    Returns:
        The same actions list with totp_timing_info added to multi-field TOTP actions
    """
    if len(actions) < 4:
        return actions

    # Identify consecutive runs of single-digit TOTP inputs
    # A multi-field TOTP sequence is 4+ consecutive INPUT_TEXT actions with single-digit text
    # and the same field_name (typically 'totp_code')
    consecutive_start = None
    consecutive_count = 0
    totp_field_name = None

    for idx, act in enumerate(actions):
        is_single_digit_totp = (
            act.get("action_type") == ActionType.INPUT_TEXT
            and act.get("field_name")
            and act.get("text")
            and len(str(act.get("text", ""))) == 1
            and str(act.get("text", "")).isdigit()
        )

        if is_single_digit_totp:
            current_field = act.get("field_name")
            if consecutive_start is None:
                # Start a new sequence
                consecutive_start = idx
                totp_field_name = current_field
                consecutive_count = 1
            elif current_field == totp_field_name:
                # Same field, continue the sequence
                consecutive_count += 1
            else:
                # Different field - finalize current sequence if valid, then start new one
                if consecutive_count >= 4:
                    for seq_idx in range(consecutive_count):
                        actions[consecutive_start + seq_idx]["totp_timing_info"] = {
                            "is_totp_sequence": True,
                            "action_index": seq_idx,
                            "total_digits": consecutive_count,
                            "field_name": totp_field_name,
                        }
                    LOG.debug(
                        "Annotated multi-field TOTP sequence (field change)",
                        start_idx=consecutive_start,
                        count=consecutive_count,
                        field_name=totp_field_name,
                    )
                # Start new sequence with different field
                consecutive_start = idx
                totp_field_name = current_field
                consecutive_count = 1
        else:
            # End of consecutive sequence - check if it was a multi-field TOTP
            if consecutive_count >= 4 and consecutive_start is not None:
                # Annotate all actions in this sequence
                for seq_idx in range(consecutive_count):
                    actions[consecutive_start + seq_idx]["totp_timing_info"] = {
                        "is_totp_sequence": True,
                        "action_index": seq_idx,
                        "total_digits": consecutive_count,
                        "field_name": totp_field_name,
                    }
                LOG.debug(
                    "Annotated multi-field TOTP sequence for script generation",
                    start_idx=consecutive_start,
                    count=consecutive_count,
                    field_name=totp_field_name,
                )
            consecutive_start = None
            consecutive_count = 0
            totp_field_name = None

    # Handle sequence at end of actions list
    if consecutive_count >= 4 and consecutive_start is not None:
        for seq_idx in range(consecutive_count):
            actions[consecutive_start + seq_idx]["totp_timing_info"] = {
                "is_totp_sequence": True,
                "action_index": seq_idx,
                "total_digits": consecutive_count,
                "field_name": totp_field_name,
            }
        LOG.debug(
            "Annotated multi-field TOTP sequence for script generation (at end)",
            start_idx=consecutive_start,
            count=consecutive_count,
            field_name=totp_field_name,
        )

    return actions


def _safe_name(label: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in label).lower()
    if not s or s[0].isdigit() or keyword.iskeyword(s):
        s = f"_{s}"
    while "__" in s:
        s = s.replace("__", "_")
    return s


def _value(value: Any) -> cst.BaseExpression:
    """Convert simple Python objects to CST expressions."""
    if isinstance(value, str):
        if "\n" in value:
            # For multi-line strings, use repr() which handles all escaping properly
            # This will use triple quotes when appropriate and escape them when needed
            return cst.SimpleString(repr(value))
        return cst.SimpleString(repr(value))
    if isinstance(value, (int, float, bool)) or value is None:
        return cst.parse_expression(repr(value))
    if isinstance(value, dict):
        return Dict(
            [
                DictElement(
                    key=_value(k),
                    value=_value(v),
                )
                for k, v in value.items()
            ]
        )
    if isinstance(value, (list, tuple)):
        elts = [cst.Element(_value(v)) for v in value]
        return cst.List(elts) if isinstance(value, list) else cst.Tuple(elts)
    # fallback
    return cst.SimpleString(repr(str(value)))


def _render_value(
    prompt_text: str | None = None,
    data_variable_name: str | None = None,
    render_func_name: str = "render_template",
) -> cst.BaseExpression:
    """Create a prompt value with template rendering logic if needed."""
    if not prompt_text:
        return cst.SimpleString("")
    if "{{" in prompt_text and "}}" in prompt_text:
        args = [cst.Arg(value=_value(prompt_text))]
        if data_variable_name:
            args.append(
                cst.Arg(
                    keyword=cst.Name("data"),
                    value=cst.Name(data_variable_name),
                )
            )
        return cst.Call(
            func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name(render_func_name)),
            args=args,
        )
    else:
        # Return the prompt as a simple string value
        return _value(prompt_text)


# --------------------------------------------------------------------- #
# 2. utility builders                                                   #
# --------------------------------------------------------------------- #


def _workflow_decorator(wf_req: dict[str, Any]) -> cst.Decorator:
    """
    Build  @skyvern.workflow(
               title="...", totp_url=..., totp_identifier=..., webhook_callback_url=..., max_steps=...
           )
    """

    # helper that skips “None” so the output is concise
    def kw(key: str, value: Any) -> cst.Arg | None:
        if value is None:
            return None
        return cst.Arg(keyword=cst.Name(key), value=_value(value))

    args: list = list(
        filter(
            None,
            [
                kw("title", wf_req.get("title", "")),
                kw("totp_url", wf_req.get("totp_url")),
                kw("totp_identifier", wf_req.get("totp_identifier")),
                kw("webhook_url", wf_req.get("webhook_url")),
                kw("max_steps", wf_req.get("max_steps")),
            ],
        )
    )

    return cst.Decorator(
        decorator=cst.Call(
            func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("workflow")),
            args=args,
        )
    )


def _make_decorator(block_label: str, block: dict[str, Any]) -> cst.Decorator:
    kwargs = [
        cst.Arg(
            keyword=cst.Name("cache_key"),
            value=_value(block_label),
        )
    ]
    return cst.Decorator(
        decorator=Call(
            func=Attribute(value=cst.Name("skyvern"), attr=cst.Name("cached")),
            args=kwargs,
        )
    )


def _action_to_stmt(
    act: dict[str, Any],
    task: dict[str, Any],
    assign_to_output: bool = False,
    value_to_param: dict[str, str] | None = None,
    use_semantic_selectors: bool = False,
) -> cst.BaseStatement:
    """
    Turn one Action dict into:

        await page.<method>(selector=..., prompt=..., data=context.parameters)

    Or if assign_to_output is True for extract actions:

        output = await page.extract(...)

    When *value_to_param* is provided, click prompt strings that contain literal
    parameter values will be emitted as f-strings referencing
    ``context.parameters['field_name']`` instead of hardcoded text.
    """
    method = ACTION_MAP[act["action_type"]]

    args: list[cst.Arg] = []
    if method in ACTIONS_WITH_XPATH:
        if use_semantic_selectors:
            semantic = _build_semantic_selector(act)
            if semantic:
                args.append(
                    cst.Arg(
                        keyword=cst.Name("selector"),
                        value=_value(semantic),
                        whitespace_after_arg=cst.ParenthesizedWhitespace(
                            indent=True,
                            last_line=cst.SimpleWhitespace(INDENT),
                        ),
                    )
                )
            # If no semantic selector, skip selector arg — ai with prompt= handles it
        else:
            args.append(
                cst.Arg(
                    keyword=cst.Name("selector"),
                    value=_value(f"xpath={act['xpath']}"),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )

    if method == "click":
        if use_semantic_selectors:
            # With semantic selectors, try selector first, AI only if miss
            ai_mode = GENERATE_CODE_AI_MODE_FALLBACK
        else:
            ai_mode = GENERATE_CODE_AI_MODE_PROACTIVE
            click_context = act.get("click_context")
            if click_context and isinstance(click_context, dict) and click_context.get("single_option_click"):
                ai_mode = GENERATE_CODE_AI_MODE_FALLBACK
        args.append(
            cst.Arg(
                keyword=cst.Name("ai"),
                value=_value(ai_mode),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    elif method == "hover":
        hold_seconds = act.get("hold_seconds")
        if hold_seconds and hold_seconds > 0:
            args.append(
                cst.Arg(
                    keyword=cst.Name("hold_seconds"),
                    value=_value(hold_seconds),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
    elif method in ["type", "fill"]:
        # Use context.parameters if field_name is available, otherwise fallback to direct value
        if act.get("field_name"):
            # Check if this is a multi-field TOTP sequence that needs digit indexing
            totp_info = act.get("totp_timing_info") or {}
            if totp_info.get("is_totp_sequence") and "action_index" in totp_info:
                # Generate: await page.get_totp_digit(context, 'field_name', digit_index)
                # This method properly resolves the TOTP code from credentials and returns the specific digit
                text_value = cst.Await(
                    expression=cst.Call(
                        func=cst.Attribute(
                            value=cst.Name("page"),
                            attr=cst.Name("get_totp_digit"),
                        ),
                        args=[
                            cst.Arg(value=cst.Name("context")),
                            cst.Arg(value=_value(act["field_name"])),
                            cst.Arg(value=_value(totp_info["action_index"])),
                        ],
                    )
                )
            else:
                text_value = cst.Subscript(
                    value=cst.Attribute(
                        value=cst.Name("context"),
                        attr=cst.Name("parameters"),
                    ),
                    slice=[cst.SubscriptElement(slice=cst.Index(value=_value(act["field_name"])))],
                )
        else:
            text_value = _value(act["text"])

        ai_mode = GENERATE_CODE_AI_MODE_FALLBACK
        if _requires_mini_agent(act):
            ai_mode = GENERATE_CODE_AI_MODE_PROACTIVE

        args.append(
            cst.Arg(
                keyword=cst.Name("value"),
                value=text_value,
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        args.append(
            cst.Arg(
                keyword=cst.Name("ai"),
                value=_value(ai_mode),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        if act.get("totp_code_required"):
            if task.get("totp_identifier"):
                args.append(
                    cst.Arg(
                        keyword=cst.Name("totp_identifier"),
                        value=_value(task.get("totp_identifier")),
                        whitespace_after_arg=cst.ParenthesizedWhitespace(
                            indent=True,
                            last_line=cst.SimpleWhitespace(INDENT),
                        ),
                    )
                )
            if task.get("totp_url"):
                args.append(
                    cst.Arg(
                        keyword=cst.Name("totp_url"),
                        value=_value(task.get("totp_verification_url")),
                        whitespace_after_arg=cst.ParenthesizedWhitespace(
                            indent=True,
                            last_line=cst.SimpleWhitespace(INDENT),
                        ),
                    )
                )
    elif method == "select_option":
        option = act.get("option", {})
        value = option.get("value")
        label = option.get("label")
        value = value or label
        if value:
            # TODO: consider supporting fallback mode for select_option actions
            # ai_mode = GENERATE_CODE_AI_MODE_FALLBACK
            # if _requires_mini_agent(act):
            ai_mode = GENERATE_CODE_AI_MODE_PROACTIVE
            if act.get("field_name"):
                option_value = cst.Subscript(
                    value=cst.Attribute(
                        value=cst.Name("context"),
                        attr=cst.Name("parameters"),
                    ),
                    slice=[cst.SubscriptElement(slice=cst.Index(value=_value(act["field_name"])))],
                )
            else:
                option_value = _value(value)
            args.append(
                cst.Arg(
                    keyword=cst.Name("value"),
                    value=option_value,
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                ),
            )
            args.append(
                cst.Arg(
                    keyword=cst.Name("ai"),
                    value=_value(ai_mode),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
    elif method == "upload_file":
        if act.get("field_name"):
            file_url_value = cst.Subscript(
                value=cst.Attribute(
                    value=cst.Name("context"),
                    attr=cst.Name("parameters"),
                ),
                slice=[cst.SubscriptElement(slice=cst.Index(value=_value(act["field_name"])))],
            )
        else:
            file_url_value = _value(act["file_url"])
        args.append(
            cst.Arg(
                keyword=cst.Name("files"),
                value=file_url_value,
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        args.append(
            cst.Arg(
                keyword=cst.Name("ai"),
                value=_value(GENERATE_CODE_AI_MODE_PROACTIVE),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    elif method == "keypress":
        args.append(
            cst.Arg(
                keyword=cst.Name("keys"),
                value=_value(act.get("keys", ["Enter"])),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        if act.get("hold"):
            args.append(
                cst.Arg(
                    keyword=cst.Name("hold"),
                    value=_value(act["hold"]),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
        if act.get("duration"):
            args.append(
                cst.Arg(
                    keyword=cst.Name("duration"),
                    value=_value(act["duration"]),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
    elif method == "wait":
        args.append(
            cst.Arg(
                keyword=cst.Name("seconds"),
                value=_value(act["seconds"]),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    elif method == "download_file":
        args.append(
            cst.Arg(
                keyword=cst.Name("file_name"),
                value=_value(act.get("file_name", "")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        if act.get("download_url"):
            args.append(
                cst.Arg(
                    keyword=cst.Name("download_url"),
                    value=_value(act["download_url"]),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
    elif method == "extract":
        args.append(
            cst.Arg(
                keyword=cst.Name("prompt"),
                value=_value(act["data_extraction_goal"]),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
        if act.get("data_extraction_schema"):
            args.append(
                cst.Arg(
                    keyword=cst.Name("schema"),
                    value=_value(act["data_extraction_schema"]),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                    comma=cst.Comma(),
                )
            )
    action_context = act.get("input_or_select_context")
    if action_context and action_context.get("date_format") and method in ["type", "fill", "select_option"]:
        date_format_value = action_context.get("date_format")
        data = {"date_format": date_format_value}
        args.append(
            cst.Arg(
                keyword=cst.Name("data"),
                value=_value(data),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    intention = act.get("intention") or act.get("reasoning") or ""
    if intention and method not in ACTIONS_OPT_OUT_INTENTION_FOR_PROMPT:
        # Try to parameterize the prompt for click actions so cached scripts
        # don't embed recording-specific values (e.g. a patient ID).
        prompt_value: cst.BaseExpression | None = None
        if value_to_param:
            prompt_value = _build_parameterized_prompt_cst(intention, value_to_param)
        if prompt_value is None:
            prompt_value = _value(intention)

        args.extend(
            [
                cst.Arg(
                    keyword=cst.Name("prompt"),
                    value=prompt_value,
                    whitespace_after_arg=cst.ParenthesizedWhitespace(indent=True),
                    comma=cst.Comma(),
                ),
            ]
        )
    _mark_last_arg_as_comma(args)

    # Only use indented parentheses if we have arguments
    if args:
        call = cst.Call(
            func=cst.Attribute(value=cst.Name("page"), attr=cst.Name(method)),
            args=args,
            whitespace_before_args=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        )
    else:
        call = cst.Call(
            func=cst.Attribute(value=cst.Name("page"), attr=cst.Name(method)),
            args=args,
        )

    # await page.method(...)
    await_expr = cst.Await(call)

    # If this is an extract action and we want to assign to output
    if assign_to_output and method == "extract":
        # output = await page.extract(...)
        assign = cst.Assign(
            targets=[cst.AssignTarget(cst.Name("output"))],
            value=await_expr,
        )
        return cst.SimpleStatementLine([assign])
    else:
        # Wrap in a statement line:  await ...
        return cst.SimpleStatementLine([cst.Expr(await_expr)])


def _collect_block_input_fields(
    block: dict[str, Any],
    actions_by_task: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """
    Gather the sequence of workflow parameter field names referenced by custom field actions within a block.
    """
    task_id = block.get("task_id")
    if not task_id:
        return []

    all_fields: list[str] = []

    for action in actions_by_task.get(task_id, []):
        action_type = action.get("action_type")

        # Keep in sync with CUSTOM_FIELD_ACTIONS used for schema generation
        if action_type not in CUSTOM_FIELD_ACTIONS:
            continue
        field_name = action.get("field_name")
        if not field_name or not isinstance(field_name, str):
            continue
        all_fields.append(field_name)

    return all_fields


def _is_form_filling_block(
    actions: list[dict[str, Any]],
) -> bool:
    """Check if a block's actions indicate a form-filling pattern (4+ input/select actions)."""
    form_action_count = sum(
        1 for a in actions if a.get("action_type") in (ActionType.INPUT_TEXT, ActionType.SELECT_OPTION)
    )
    return form_action_count >= 4


def _build_form_filling_block_fn(
    block: dict[str, Any],
    actions: list[dict[str, Any]],
    value_to_param: dict[str, str] | None = None,
) -> FunctionDef:
    """Generate a FIELD_MAP-based block function from V1 actions.

    Instead of procedural page.fill()/page.select_option() calls, produces a
    FIELD_MAP dictionary + page.fill_form() call for dynamic form filling.
    """
    name = _safe_name(block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}")
    cache_key = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"

    # Build FIELD_MAP entries from actions
    field_map_lines: list[str] = []
    seen_keys: set[str] = set()

    navigation_goal = block.get("navigation_goal") or "Fill out the form"

    for act in actions:
        action_type = act.get("action_type")
        if action_type not in (ActionType.INPUT_TEXT, ActionType.SELECT_OPTION):
            continue

        intention = act.get("intention") or act.get("reasoning") or ""
        # field_name is set by hydrate_input_text_actions_with_field_names()
        # but may not match actual workflow parameter keys. Use value_to_param
        # to look up the real parameter key from the literal value.
        field_name = act.get("field_name")

        # Look up the actual workflow parameter key via the literal action value
        actual_param: str | None = None
        if value_to_param:
            if action_type == ActionType.INPUT_TEXT:
                literal_value = act.get("text", "")
            elif action_type == ActionType.SELECT_OPTION:
                option = act.get("option", {})
                literal_value = option.get("value") or option.get("label") or ""
            else:
                literal_value = ""
            if literal_value and literal_value in value_to_param:
                actual_param = value_to_param[literal_value]

        # Determine the snake_case key for this field
        if field_name:
            key = sanitize_variable_name(field_name)
        elif intention:
            key = sanitize_variable_name(intention[:40])
        else:
            continue

        if key in seen_keys:
            continue
        seen_keys.add(key)

        # Determine action type for FIELD_MAP
        if action_type == ActionType.SELECT_OPTION:
            fm_action = "select"
        else:
            fm_action = "fill"

        # Build the label from intention/reasoning
        label = intention.lower().strip() if intention else key.replace("_", " ")
        if len(label) > 60:
            label = label[:60]

        # Use actual_param (from value_to_param lookup) if found, else fall back to field_name.
        # If neither exists, this field has no parameter → use ai='proactive'.
        param = actual_param or field_name
        param_str = repr(param) if param else "None"
        ai_str = repr("proactive") if not param else repr("fallback")
        prompt = intention or f"Fill the {key.replace('_', ' ')} field"

        entry = (
            f"        {repr(key)}: {{\n"
            f'            "param": {param_str},\n'
            f'            "action": {repr(fm_action)},\n'
            f'            "ai": {ai_str},\n'
            f'            "labels": [{repr(label)}],\n'
            f'            "prompt": {repr(prompt)},\n'
            f"        }},"
        )
        field_map_lines.append(entry)

    # Build the function body as a string, then parse it
    field_map_body = "\n".join(field_map_lines)

    # Include page.goto(url) if the block has a URL, just like _build_block_fn does
    goto_line = ""
    if block.get("url"):
        goto_line = f"    await page.goto({repr(block['url'])})\n"

    func_code = (
        f"async def {name}(page: SkyvernPage, context: RunContext):\n"
        f"{goto_line}"
        f"    FIELD_MAP = {{\n"
        f"{field_map_body}\n"
        f"    }}\n"
        f"    await page.fill_form(FIELD_MAP, context, navigation_goal={repr(navigation_goal)})\n"
        f"    if not await page.structural_validate():\n"
        f"        await page.complete()\n"
    )

    # Parse and add decorator
    parsed = cst.parse_module(func_code)
    func_def = parsed.body[0]
    assert isinstance(func_def, FunctionDef)

    # Add the @skyvern.cached decorator
    decorated = func_def.with_changes(decorators=[_make_decorator(cache_key, block)])
    return decorated


def _build_block_fn(
    block: dict[str, Any],
    actions: list[dict[str, Any]],
    value_to_param: dict[str, str] | None = None,
    use_semantic_selectors: bool = False,
) -> FunctionDef:
    # Check if this block is form-filling (4+ input_text/select actions)
    if use_semantic_selectors and _is_form_filling_block(actions):
        return _build_form_filling_block_fn(block, actions, value_to_param)

    name = _safe_name(block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}")
    cache_key = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"
    body_stmts: list[cst.BaseStatement] = []

    # Detect and annotate multi-field TOTP sequences so each fill gets the correct digit index
    actions = _annotate_multi_field_totp_sequence(actions)

    if block.get("url"):
        body_stmts.append(cst.parse_statement(f"await page.goto({repr(block['url'])})"))

    for act in actions:
        if act["action_type"] in [
            ActionType.COMPLETE,
            ActionType.TERMINATE,
            ActionType.NULL_ACTION,
        ]:
            continue

        # For extraction blocks, assign extract action results to output variable
        assign_to_output = act["action_type"] == "extract"
        body_stmts.append(
            _action_to_stmt(
                act,
                block,
                assign_to_output=assign_to_output,
                value_to_param=value_to_param,
                use_semantic_selectors=use_semantic_selectors,
            )
        )

    # add complete action
    block_type = block.get("block_type")
    if block_type in SCRIPT_TASK_BLOCKS_WITH_COMPLETE_ACTION:
        complete_action = {"action_type": "complete"}
        body_stmts.append(_action_to_stmt(complete_action, block))

    # For extraction blocks, add return output statement if we have actions
    if any(
        act["action_type"] == "extract"
        for act in actions
        if act["action_type"] not in [ActionType.COMPLETE, ActionType.TERMINATE, ActionType.NULL_ACTION]
    ):
        body_stmts.append(cst.parse_statement("return output"))
    elif not body_stmts:
        body_stmts.append(cst.parse_statement("return None"))

    return FunctionDef(
        name=Name(name),
        params=cst.Parameters(
            params=[
                Param(name=Name("page"), annotation=cst.Annotation(cst.Name("SkyvernPage"))),
                Param(name=Name("context"), annotation=cst.Annotation(cst.Name("RunContext"))),
            ]
        ),
        decorators=[_make_decorator(cache_key, block)],
        body=cst.IndentedBlock(body_stmts),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


def _build_task_v2_block_fn(block: dict[str, Any], child_blocks: list[dict[str, Any]]) -> FunctionDef:
    """Build a cached function for task_v2 blocks that calls child workflow sub-tasks."""
    cache_key = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"
    name = _safe_name(block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}")
    body_stmts: list[cst.BaseStatement] = []

    # Add calls to child workflow sub-tasks
    has_extract_block = False
    for child_block in child_blocks:
        is_extract_block = child_block.get("block_type") == "extraction"
        if is_extract_block:
            has_extract_block = True
        stmt = _build_block_statement(child_block, assign_output=is_extract_block)
        body_stmts.append(stmt)

    if not body_stmts:
        body_stmts.append(cst.parse_statement("return None"))
    elif has_extract_block:
        body_stmts.append(cst.parse_statement("return output"))

    return FunctionDef(
        name=Name(name),
        params=cst.Parameters(
            params=[
                Param(name=Name("page"), annotation=cst.Annotation(cst.Name("SkyvernPage"))),
                Param(name=Name("context"), annotation=cst.Annotation(cst.Name("RunContext"))),
            ]
        ),
        decorators=[_make_decorator(cache_key, block)],
        body=cst.IndentedBlock(body_stmts),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


def _build_model(workflow: dict[str, Any]) -> cst.ClassDef:
    """
    class WorkflowParameters(BaseModel):
        param1: str
        param2: str
        ...
    """
    ann_lines: list[cst.BaseStatement] = []

    for parameter in workflow["workflow_definition"]["parameters"]:
        if parameter["parameter_type"] != "workflow":
            continue

        ann = cst.AnnAssign(
            target=cst.Name(sanitize_variable_name(parameter["key"])),
            annotation=cst.Annotation(cst.Name("str")),
            value=None,
        )
        ann_lines.append(cst.SimpleStatementLine([ann]))

    if not ann_lines:  # no parameters
        ann_lines.append(cst.SimpleStatementLine([cst.Pass()]))

    return cst.ClassDef(
        name=cst.Name("WorkflowParameters"),
        bases=[cst.Arg(cst.Name("BaseModel"))],
        body=cst.IndentedBlock(ann_lines),  # ← wrap in block
    )


def _build_generated_model_from_schema(schema_code: str) -> cst.ClassDef | None:
    """
    Parse the generated schema code and return a ClassDef, or None if parsing fails.
    """
    try:
        # Parse the schema code and extract just the class definition
        parsed_module = cst.parse_module(schema_code)

        # Find the GeneratedWorkflowParameters class in the parsed module
        for node in parsed_module.body:
            if isinstance(node, cst.ClassDef) and node.name.value == "GeneratedWorkflowParameters":
                return node

        # If no class found, return None
        return None
    except Exception as e:
        LOG.warning("Failed to parse generated schema code", error=str(e))
        return None


# --------------------------------------------------------------------- #
# 3. statement builders                                                 #
# --------------------------------------------------------------------- #


def _build_run_task_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    value_to_param: dict[str, str] | None = None,
) -> cst.SimpleStatementLine:
    """Build a skyvern.run_task statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("run_task")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_download_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    value_to_param: dict[str, str] | None = None,
) -> cst.SimpleStatementLine:
    """Build a skyvern.download statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("download")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_action_statement(
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.action statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_value(block.get("navigation_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]
    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("label"):
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                ),
                comma=cst.Comma(),
            )
        )
    _mark_last_arg_as_comma(args)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("action")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_login_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    value_to_param: dict[str, str] | None = None,
) -> cst.SimpleStatementLine:
    """Build a skyvern.login statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("login")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_extract_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    assign_output: bool = True,
) -> cst.SimpleStatementLine:
    """Build a skyvern.extract statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_value(block.get("data_extraction_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("schema"),
            value=_value(block.get("data_schema", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]
    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("label"):
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block_title),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                ),
                comma=cst.Comma(),
            )
        )
    _mark_last_arg_as_comma(args)

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("extract")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    if assign_output:
        return cst.SimpleStatementLine(
            [
                cst.Assign(
                    targets=[cst.AssignTarget(target=cst.Name("output"))],
                    value=cst.Await(call),
                )
            ]
        )
    else:
        return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_navigate_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    value_to_param: dict[str, str] | None = None,
) -> cst.SimpleStatementLine:
    """Build a skyvern.navigate statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("run_task")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_send_email_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.send_email statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("sender"),
            value=_value(block.get("sender", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("recipients"),
            value=_value(block.get("recipients", [])),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("subject"),
            value=_value(block.get("subject", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("body"),
            value=_value(block.get("body", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("file_attachments"),
            value=_value(block.get("file_attachments", [])),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block.get("label", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("send_email")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_validate_statement(
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.validate statement."""
    args = []

    # Add complete_criterion if it exists
    if block.get("complete_criterion") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("complete_criterion"),
                value=_value(block.get("complete_criterion")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    # Add terminate_criterion if it exists
    if block.get("terminate_criterion") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("terminate_criterion"),
                value=_value(block.get("terminate_criterion")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    # Add error_code_mapping if it exists
    if block.get("error_code_mapping") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("error_code_mapping"),
                value=_value(block.get("error_code_mapping")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    # Add label if it exists
    if block.get("label") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                ),
            )
        )

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("validate")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_human_interaction_statement(
    block: dict[str, Any],
) -> cst.SimpleStatementLine:
    LOG.warning("Human interaction code generation is not yet implemented.", block=block)
    return cst.SimpleStatementLine([cst.Expr(cst.Comment("# TODO: Implement human interaction logic"))])


def _build_wait_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.wait statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("seconds"),
            value=_value(block.get("wait_sec", 1)),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block.get("label")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("wait")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_goto_statement(block: dict[str, Any], data_variable_name: str | None = None) -> cst.SimpleStatementLine:
    """Build a skyvern.goto statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("url"),
            value=_value(block.get("url", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("goto")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_code_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.run_code statement."""
    parameters = block.get("parameters", [])
    parameter_list = [parameter["key"] for parameter in parameters]

    args = [
        cst.Arg(
            keyword=cst.Name("code"),
            value=_value(block.get("code", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("parameters"),
            value=_value(parameter_list),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("run_code")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_file_upload_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.upload_file statement."""
    parameters = block.get("parameters", [])
    parameter_list = [parameter["key"] for parameter in parameters]

    args = [
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block.get("label", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("parameters"),
            value=_value(parameter_list),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("storage_type"),
            value=_value(str(block.get("storage_type", FileStorageType.S3))),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]
    for key in [
        "s3_bucket",
        "aws_access_key_id",
        "aws_secret_access_key",
        "region_name",
        "azure_storage_account_name",
        "azure_storage_account_key",
        "azure_blob_container_name",
        "path",
    ]:
        if block.get(key) is not None:
            args.append(
                cst.Arg(
                    keyword=cst.Name(key),
                    value=_value(block.get(key, "")),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                )
            )
    _mark_last_arg_as_comma(args)

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("upload_file")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_pdf_parser_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.parse_pdf statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("file_url"),
            value=_value(block.get("file_url", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]

    if block.get("json_schema") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("schema"),
                value=_value(block.get("json_schema")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("label") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    _mark_last_arg_as_comma(args)

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("parse_pdf")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )
    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_file_url_parser_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.parse_file statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("file_url"),
            value=_value(block.get("file_url", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("file_type"),
            value=_value(str(block.get("file_type"))),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]

    # Add optional parameters if they exist
    if block.get("json_schema") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("schema"),
                value=_value(block.get("json_schema")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("label") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    _mark_last_arg_as_comma(args)

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("parse_file")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_http_request_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.http_request statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("method"),
            value=_value(block.get("method", "GET")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("url"),
            value=_value(block.get("url", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]

    # Add optional parameters if they exist
    if block.get("headers") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("headers"),
                value=_value(block.get("headers")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("body") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("body"),
                value=_value(block.get("body")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("timeout") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("timeout"),
                value=_value(block.get("timeout")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("follow_redirects") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("follow_redirects"),
                value=_value(block.get("follow_redirects")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("label") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    _mark_last_arg_as_comma(args)

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("http_request")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_prompt_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.prompt statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_value(block.get("prompt", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]

    # Add optional parameters if they exist
    if block.get("json_schema") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("schema"),
                value=_value(block.get("json_schema")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("label") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("label"),
                value=_value(block.get("label")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("parameters"):
        parameters = block.get("parameters", [])
        parameter_list = [parameter["key"] for parameter in parameters]
        args.append(
            cst.Arg(
                keyword=cst.Name("parameters"),
                value=_value(parameter_list),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    _mark_last_arg_as_comma(args)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("prompt")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_for_loop_statement(block_title: str, block: dict[str, Any]) -> cst.For:
    """
    Build a for loop statement.
    All the blocks within the for loop block statement will run without cache_key.

    An example of a for loop statement:
    ```
    async for current_value in skyvern.loop(context.parameters["urls"]):
        await skyvern.goto(
            url=current_value,
            label="block_4",
        )
        await skyvern.extract(
            prompt="Get a summary of the page",
            schema={
                  "type": "object",
                  "properties": {
                      "summary": {
                          "type": "string",
                          "description": "A concise summary of the main content or purpose of the page"
                      }
                  },
                  "required": [
                        "summary"
                  ]
             },
             label="block_5",
        )
    ```
    """
    # Extract loop configuration
    loop_over_parameter_key = block.get("loop_variable_reference", "")
    loop_blocks = block.get("loop_blocks", [])

    # Create the for loop target (current_value)
    target = cst.Name("current_value")

    # Build body statements from loop_blocks
    body_statements = []

    # Add loop_data assignment as the first statement
    for loop_block in loop_blocks:
        stmt = _build_block_statement(loop_block)
        body_statements.append(stmt)

    # create skyvern.loop(loop_over_parameter_key, label=block_title)
    loop_call_args = [cst.Arg(keyword=cst.Name("values"), value=_value(loop_over_parameter_key))]
    if block.get("complete_if_empty"):
        loop_call_args.append(
            cst.Arg(keyword=cst.Name("complete_if_empty"), value=_value(block.get("complete_if_empty")))
        )
    loop_call_args.append(cst.Arg(keyword=cst.Name("label"), value=_value(block_title)))
    loop_call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("loop")),
        args=loop_call_args,
    )

    # Create the async for loop
    for_loop = cst.For(
        target=target,
        iter=loop_call,
        body=cst.IndentedBlock(body=body_statements),
        asynchronous=cst.Asynchronous(),
        whitespace_after_for=cst.SimpleWhitespace(" "),
        whitespace_before_in=cst.SimpleWhitespace(" "),
        whitespace_after_in=cst.SimpleWhitespace(" "),
        whitespace_before_colon=cst.SimpleWhitespace(""),
    )

    return for_loop


def _mark_last_arg_as_comma(args: list[cst.Arg]) -> None:
    if not args:
        return

    last_arg = args.pop()
    new_arg = cst.Arg(
        keyword=last_arg.keyword,
        value=last_arg.value,
        comma=cst.Comma(),
        whitespace_after_arg=cst.ParenthesizedWhitespace(
            indent=True,
        ),
    )
    args.append(new_arg)


def __build_base_task_statement(
    block_title: str,
    block: dict[str, Any],
    data_variable_name: str | None = None,
    value_to_param: dict[str, str] | None = None,
) -> list[cst.Arg]:
    block_type = block.get("block_type")
    prompt = block.get("prompt") if block_type == "task_v2" else block.get("navigation_goal")
    # add parameters to prompt
    parameters = block.get("parameters", [])
    navigation_payload = {}
    # make all parameters as jinja2 template parameters in the generated code
    for parameter in parameters:
        parameter_key = parameter["key"]
        navigation_payload[parameter_key] = "{{" + parameter_key + "}}"

    if navigation_payload:
        prompt = prompt or ""
        prompt = f"{prompt}\n{navigation_payload}"

    # Try to parameterize PII in the prompt with f-string context.parameters refs.
    prompt_value: cst.BaseExpression | None = None
    if value_to_param and prompt:
        prompt_value = _build_parameterized_prompt_cst(prompt, value_to_param)
    if prompt_value is None:
        prompt_value = _value(prompt)

    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=prompt_value,
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]
    if block.get("url"):
        args.append(
            cst.Arg(
                keyword=cst.Name("url"),
                value=_value(block.get("url", "")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    max_steps = block.get("max_steps") if block_type == "task_v2" else block.get("max_steps_per_run")
    if max_steps:
        args.append(
            cst.Arg(
                keyword=cst.Name("max_steps"),
                value=_value(max_steps or settings.MAX_STEPS_PER_RUN),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("download_suffix"):
        args.append(
            cst.Arg(
                keyword=cst.Name("download_suffix"),
                value=_value(block.get("download_suffix")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("totp_identifier"):
        args.append(
            cst.Arg(
                keyword=cst.Name("totp_identifier"),
                value=_value(block.get("totp_identifier", "")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("totp_verification_url"):
        args.append(
            cst.Arg(
                keyword=cst.Name("totp_url"),
                value=_value(block.get("totp_verification_url", "")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("model"):
        args.append(
            cst.Arg(
                keyword=cst.Name("model"),
                value=_value(block.get("model")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    # Add error_code_mapping if it exists
    if block.get("error_code_mapping") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("error_code_mapping"),
                value=_value(block.get("error_code_mapping")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if block.get("block_type") == "task_v2":
        args.append(
            cst.Arg(
                keyword=cst.Name("engine"),
                value=_value("skyvern-2.0"),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    args.append(
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        )
    )
    return args


# --------------------------------------------------------------------- #
# 4. function builders                                                  #
# --------------------------------------------------------------------- #


def _build_block_statement(
    block: dict[str, Any],
    data_variable_name: str | None = None,
    assign_output: bool = False,
    value_to_param: dict[str, str] | None = None,
) -> cst.SimpleStatementLine:
    """Build a block statement."""
    block_type = block.get("block_type")
    block_title = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"

    if block_type in SCRIPT_TASK_BLOCKS:
        # For task blocks, call the custom function with cache_key
        if block_type == "task":
            stmt = _build_run_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
        elif block_type == "file_download":
            stmt = _build_download_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
        elif block_type == "action":
            stmt = _build_action_statement(block_title, block, data_variable_name)
        elif block_type == "login":
            stmt = _build_login_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
        elif block_type == "extraction":
            stmt = _build_extract_statement(block_title, block, data_variable_name, assign_output)
        elif block_type == "navigation":
            stmt = _build_navigate_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    elif block_type == "validation":
        stmt = _build_validate_statement(block_title, block, data_variable_name)
    elif block_type == "human_interaction":
        stmt = _build_human_interaction_statement(block)
    elif block_type == "task_v2":
        stmt = _build_run_task_statement(block_title, block, data_variable_name, value_to_param=value_to_param)
    elif block_type == "send_email":
        stmt = _build_send_email_statement(block)
    elif block_type == "text_prompt":
        stmt = _build_prompt_statement(block)
    elif block_type == "wait":
        stmt = _build_wait_statement(block)
    elif block_type == "for_loop":
        stmt = _build_for_loop_statement(block_title, block)
    elif block_type == "goto_url":
        stmt = _build_goto_statement(block, data_variable_name)
    elif block_type == "code":
        stmt = _build_code_statement(block)
    elif block_type == "file_upload":
        stmt = _build_file_upload_statement(block)
    elif block_type == "file_url_parser":
        stmt = _build_file_url_parser_statement(block)
    elif block_type == "http_request":
        stmt = _build_http_request_statement(block)
    elif block_type == "pdf_parser":
        stmt = _build_pdf_parser_statement(block)
    elif block_type == "conditional":
        # Conditional blocks are evaluated at runtime by the workflow engine.
        # Generate a descriptive comment showing this is a runtime branch point.
        # The blocks inside conditional branches are processed separately when executed.
        branches = block.get("branches") or block.get("ordered_branches") or []
        branch_info_lines = []
        for i, branch in enumerate(branches):
            next_label = branch.get("next_block_label", "?")
            condition = branch.get("condition", "")
            # Truncate long conditions for readability
            if len(condition) > 50:
                condition = condition[:47] + "..."
            branch_info_lines.append(f"#   Branch {i + 1}: {condition!r} → {next_label}")

        if branch_info_lines:
            branch_info = "\n".join(branch_info_lines)
            comment_text = f"# === CONDITIONAL: {block_title} ===\n# Evaluated at runtime by workflow engine. One branch executes:\n{branch_info}"
        else:
            comment_text = f"# === CONDITIONAL: {block_title} ===\n# Evaluated at runtime by workflow engine."

        stmt = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(repr(comment_text)))])
    else:
        # Default case for unknown block types - use quoted string literal to avoid libcst validation error
        stmt = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(f"'# Unknown block type: {block_type}'"))])

    return stmt


def _build_run_fn(blocks: list[dict[str, Any]], wf_req: dict[str, Any]) -> FunctionDef:
    body = [
        cst.parse_statement(
            "parameters = parameters.model_dump() if isinstance(parameters, WorkflowParameters) else parameters"
        ),
        cst.parse_statement("page, context = await skyvern.setup(parameters, GeneratedWorkflowParameters)"),
    ]

    for block in blocks:
        stmt = _build_block_statement(block, assign_output=False)
        body.append(stmt)

    params = cst.Parameters(
        params=[
            Param(
                name=cst.Name("parameters"),
                annotation=cst.Annotation(
                    cst.BinaryOperation(
                        left=cst.Name("WorkflowParameters"),
                        operator=cst.BitOr(
                            whitespace_before=cst.SimpleWhitespace(" "),
                            whitespace_after=cst.SimpleWhitespace(" "),
                        ),
                        right=cst.Subscript(
                            value=cst.Name("dict"),
                            slice=[
                                cst.SubscriptElement(
                                    slice=cst.Index(value=cst.Name("str")),
                                    comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")),
                                ),
                                cst.SubscriptElement(
                                    slice=cst.Index(value=cst.Name("Any")),
                                ),
                            ],
                        ),
                    )
                ),
                whitespace_after_param=cst.ParenthesizedWhitespace(),
                comma=cst.Comma(),
            ),
        ]
    )

    return FunctionDef(
        name=cst.Name("run_workflow"),
        asynchronous=cst.Asynchronous(),
        decorators=[_workflow_decorator(wf_req)],
        params=params,
        body=cst.IndentedBlock(body),
        whitespace_before_params=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )


# --------------------------------------------------------------------- #
# 5. entrypoint                                                         #
# --------------------------------------------------------------------- #


async def generate_workflow_script_python_code(
    *,
    file_name: str,
    workflow_run_request: dict[str, Any],
    workflow: dict[str, Any],
    blocks: list[dict[str, Any]],
    actions_by_task: dict[str, list[dict[str, Any]]],
    task_v2_child_blocks: dict[str, list[dict[str, Any]]] | None = None,
    organization_id: str | None = None,
    run_id: str | None = None,
    script_id: str | None = None,
    script_revision_id: str | None = None,
    pending: bool = False,
    cached_blocks: dict[str, ScriptBlockSource] | None = None,
    updated_block_labels: set[str] | None = None,
    use_semantic_selectors: bool = False,
    adaptive_caching: bool = False,
) -> str:
    """
    Build a LibCST Module and emit .code (PEP-8-formatted source).

    Cached script blocks can be reused by providing them via `cached_blocks`. Any labels present in
    `updated_block_labels` will be regenerated from the latest workflow run execution data.
    """
    cached_blocks = cached_blocks or {}
    updated_block_labels = set(updated_block_labels or [])

    # Drop cached entries that do not have usable source
    cached_blocks = {label: source for label, source in cached_blocks.items() if source.code}
    # Always regenerate the orchestrator block so it stays aligned with the workflow definition
    cached_blocks.pop(settings.WORKFLOW_START_BLOCK_LABEL, None)

    if task_v2_child_blocks is None:
        task_v2_child_blocks = {}
    # --- imports --------------------------------------------------------
    imports: list[cst.BaseStatement] = [
        cst.SimpleStatementLine([cst.Import(names=[cst.ImportAlias(cst.Name("asyncio"))])]),
        cst.SimpleStatementLine([cst.Import(names=[cst.ImportAlias(cst.Name("pydantic"))])]),
        cst.SimpleStatementLine(
            [
                cst.ImportFrom(
                    module=cst.Name("typing"),
                    names=[
                        cst.ImportAlias(cst.Name("Any")),
                    ],
                )
            ]
        ),
        cst.SimpleStatementLine(
            [
                cst.ImportFrom(
                    module=cst.Name("pydantic"),
                    names=[
                        cst.ImportAlias(cst.Name("BaseModel")),
                        cst.ImportAlias(cst.Name("Field")),
                    ],
                )
            ]
        ),
        cst.SimpleStatementLine([cst.Import(names=[cst.ImportAlias(cst.Name("skyvern"))])]),
        cst.SimpleStatementLine(
            [
                cst.ImportFrom(
                    module=cst.Name("skyvern"),
                    names=[
                        cst.ImportAlias(cst.Name("RunContext")),
                        cst.ImportAlias(cst.Name("SkyvernPage")),
                    ],
                )
            ]
        ),
    ]

    # --- generate schema and hydrate actions ---------------------------
    # Build existing field assignments from cached blocks to preserve field names
    # for unchanged blocks, preventing schema mismatches with cached code
    existing_field_assignments = _build_existing_field_assignments(
        blocks=blocks,
        actions_by_task=actions_by_task,
        cached_blocks=cached_blocks,
        updated_block_labels=updated_block_labels,
    )
    generated_schema, field_mappings = await generate_workflow_parameters_schema(
        actions_by_task, existing_field_assignments
    )
    actions_by_task = hydrate_input_text_actions_with_field_names(actions_by_task, field_mappings)

    # Build a lookup from literal parameter values to field names so that click
    # prompt strings can be parameterized (e.g. patient ID → context.parameters[...]).
    value_to_param = _build_value_to_param_lookup(actions_by_task)

    # --- class + cached params -----------------------------------------
    model_cls = _build_model(workflow)
    generated_model_cls = _build_generated_model_from_schema(generated_schema)

    # --- blocks ---------------------------------------------------------
    block_fns: list[cst.CSTNode] = []
    task_v1_blocks = [block for block in blocks if block["block_type"] in SCRIPT_TASK_BLOCKS]
    task_v2_blocks = [block for block in blocks if block["block_type"] == "task_v2"]

    def append_block_code(block_code: str) -> None:
        nonlocal block_fns
        parsed = cst.parse_module(block_code)
        if block_fns:
            block_fns.append(cst.EmptyLine())
            block_fns.append(cst.EmptyLine())
        block_fns.extend(parsed.body)

    # Handle task v1 blocks (excluding child blocks of task_v2)
    for idx, task in enumerate(task_v1_blocks):
        if task.get("parent_task_v2_label"):
            continue

        block_name = task.get("label") or task.get("title") or task.get("task_id") or f"task_{idx}"
        cached_source = cached_blocks.get(block_name)
        use_cached = cached_source is not None and block_name not in updated_block_labels
        input_fields = _collect_block_input_fields(task, actions_by_task)
        if not input_fields and cached_source and cached_source.input_fields:
            input_fields = cached_source.input_fields

        if use_cached:
            assert cached_source is not None
            block_code = cached_source.code
            run_signature = cached_source.run_signature
            block_workflow_run_id = cached_source.workflow_run_id
            block_workflow_run_block_id = cached_source.workflow_run_block_id
        else:
            block_fn_def = _build_block_fn(
                task,
                actions_by_task.get(task.get("task_id", ""), []),
                value_to_param=value_to_param,
                use_semantic_selectors=use_semantic_selectors,
            )
            temp_module = cst.Module(body=[block_fn_def])
            block_code = temp_module.code

            # run_signature is executed in a scope without `context`, so don't
            # parameterize the prompt — use literal strings instead.
            block_stmt = _build_block_statement(task, value_to_param=None)
            run_signature_module = cst.Module(body=[block_stmt])
            run_signature = run_signature_module.code.strip()

            block_workflow_run_id = task.get("workflow_run_id") or run_id
            block_workflow_run_block_id = task.get("workflow_run_block_id")

        if script_id and script_revision_id and organization_id:
            try:
                await create_or_update_script_block(
                    block_code=block_code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=block_name,
                    update=pending,
                    run_signature=run_signature,
                    workflow_run_id=block_workflow_run_id,
                    workflow_run_block_id=block_workflow_run_block_id,
                    input_fields=input_fields,
                )
            except Exception as e:
                LOG.error("Failed to create script block", error=str(e), exc_info=True)

        append_block_code(block_code)

    # Handle task_v2 blocks
    for task_v2 in task_v2_blocks:
        task_v2_label = task_v2.get("label") or f"task_v2_{task_v2.get('workflow_run_block_id')}"
        child_blocks = task_v2_child_blocks.get(task_v2_label, [])

        cached_source = cached_blocks.get(task_v2_label)
        use_cached = cached_source is not None and task_v2_label not in updated_block_labels
        input_fields = _collect_block_input_fields(task_v2, actions_by_task)
        if not input_fields and cached_source and cached_source.input_fields:
            input_fields = cached_source.input_fields

        block_code = ""
        run_signature = None
        block_workflow_run_id = task_v2.get("workflow_run_id") or run_id
        block_workflow_run_block_id = task_v2.get("workflow_run_block_id")

        if use_cached:
            assert cached_source is not None
            block_code = cached_source.code
            run_signature = cached_source.run_signature
            block_workflow_run_id = cached_source.workflow_run_id
            block_workflow_run_block_id = cached_source.workflow_run_block_id
        else:
            task_v2_fn_def = _build_task_v2_block_fn(task_v2, child_blocks)
            task_v2_block_body: list[cst.CSTNode] = [task_v2_fn_def]

            for child_block in child_blocks:
                if child_block.get("block_type") in SCRIPT_TASK_BLOCKS and child_block.get("block_type") != "task_v2":
                    child_fn_def = _build_block_fn(
                        child_block,
                        actions_by_task.get(child_block.get("task_id", ""), []),
                        value_to_param=value_to_param,
                        use_semantic_selectors=use_semantic_selectors,
                    )
                    task_v2_block_body.append(cst.EmptyLine())
                    task_v2_block_body.append(cst.EmptyLine())
                    task_v2_block_body.append(child_fn_def)

            temp_module = cst.Module(body=task_v2_block_body)
            block_code = temp_module.code

            task_v2_stmt = _build_block_statement(task_v2, value_to_param=None)
            run_signature = cst.Module(body=[task_v2_stmt]).code.strip()

        if script_id and script_revision_id and organization_id:
            try:
                await create_or_update_script_block(
                    block_code=block_code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=task_v2_label,
                    update=pending,
                    run_signature=run_signature,
                    workflow_run_id=block_workflow_run_id,
                    workflow_run_block_id=block_workflow_run_block_id,
                    input_fields=input_fields,
                )
            except Exception as e:
                LOG.error("Failed to create task_v2 script block", error=str(e), exc_info=True)

        append_block_code(block_code)

    # Handle for_loop blocks
    # ForLoop blocks need script_block entries with run_signature so they can be executed via cached scripts
    for_loop_blocks = [block for block in blocks if block["block_type"] == "for_loop"]
    for for_loop_block in for_loop_blocks:
        for_loop_label = for_loop_block.get("label") or f"for_loop_{for_loop_block.get('workflow_run_block_id')}"

        cached_source = cached_blocks.get(for_loop_label)
        use_cached = cached_source is not None and for_loop_label not in updated_block_labels

        block_workflow_run_id = for_loop_block.get("workflow_run_id") or run_id
        block_workflow_run_block_id = for_loop_block.get("workflow_run_block_id")

        if use_cached:
            assert cached_source is not None
            block_code = cached_source.code
            run_signature = cached_source.run_signature
            block_workflow_run_id = cached_source.workflow_run_id
            block_workflow_run_block_id = cached_source.workflow_run_block_id
        else:
            # Build the for loop statement
            for_loop_stmt = _build_for_loop_statement(for_loop_label, for_loop_block)
            temp_module = cst.Module(body=[for_loop_stmt])
            block_code = temp_module.code
            run_signature = block_code.strip()

        if script_id and script_revision_id and organization_id:
            try:
                await create_or_update_script_block(
                    block_code=block_code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=for_loop_label,
                    update=pending,
                    run_signature=run_signature,
                    workflow_run_id=block_workflow_run_id,
                    workflow_run_block_id=block_workflow_run_block_id,
                    input_fields=None,
                )
            except Exception as e:
                LOG.error("Failed to create for_loop script block", error=str(e), exc_info=True)

        # NOTE: Do NOT call append_block_code() for for_loop blocks.
        # Unlike task blocks (which produce function definitions valid at module level),
        # for_loop blocks produce bare `async for` statements that cause SyntaxError
        # at module level ("async for outside async function"). The for-loop code is
        # already correctly inlined inside run_workflow() via _build_block_statement().

    # --- agent-required blocks (adaptive caching) -----------------------
    # Structural blocks (conditional, text_prompt, wait) can't be code-generated
    # initially. Create script_block entries with requires_agent=True so the runtime
    # knows to execute them via agent even when ai_fallback=False. The script reviewer
    # can later upgrade these to code by setting requires_agent=False and providing
    # a run_signature.
    _AGENT_REQUIRED_BLOCK_TYPES = {"conditional", "text_prompt", "wait"}
    if adaptive_caching:
        agent_required_blocks = [b for b in blocks if b["block_type"] in _AGENT_REQUIRED_BLOCK_TYPES]
        for arb in agent_required_blocks:
            arb_label = arb.get("label") or f"{arb['block_type']}_{arb.get('workflow_run_block_id')}"
            # Check if the reviewer has already provided code for this block
            cached_source = cached_blocks.get(arb_label)
            if cached_source and cached_source.run_signature and not cached_source.requires_agent:
                # Reviewer upgraded this block to code — preserve it
                if script_id and script_revision_id and organization_id:
                    try:
                        await create_or_update_script_block(
                            block_code=cached_source.code,
                            script_revision_id=script_revision_id,
                            script_id=script_id,
                            organization_id=organization_id,
                            block_label=arb_label,
                            update=pending,
                            run_signature=cached_source.run_signature,
                            workflow_run_id=cached_source.workflow_run_id,
                            workflow_run_block_id=cached_source.workflow_run_block_id,
                            requires_agent=False,
                        )
                    except Exception as e:
                        LOG.error("Failed to preserve coded agent block", error=str(e), exc_info=True)
                    append_block_code(cached_source.code)
            else:
                # Create a requires_agent entry (no code, no run_signature)
                placeholder_code = f"# Block '{arb_label}' ({arb['block_type']}) — executed via agent"
                if script_id and script_revision_id and organization_id:
                    try:
                        await create_or_update_script_block(
                            block_code=placeholder_code,
                            script_revision_id=script_revision_id,
                            script_id=script_id,
                            organization_id=organization_id,
                            block_label=arb_label,
                            update=pending,
                            run_signature=None,
                            requires_agent=True,
                        )
                    except Exception as e:
                        LOG.error("Failed to create agent-required script block", error=str(e), exc_info=True)

    # --- preserve cached blocks from unexecuted branches ----------------
    # When a workflow has conditional blocks, not all branches execute in a single run.
    # transform_workflow_run_to_code_gen_input() only returns blocks that executed,
    # so cached blocks from unexecuted branches would be lost during regeneration
    # if we don't explicitly preserve them here.
    processed_labels: set[str] = set()
    for task in task_v1_blocks:
        label = task.get("label") or task.get("title") or task.get("task_id")
        if label:
            processed_labels.add(label)
    for task_v2 in task_v2_blocks:
        label = task_v2.get("label") or f"task_v2_{task_v2.get('workflow_run_block_id')}"
        processed_labels.add(label)
    for flb in for_loop_blocks:
        label = flb.get("label") or f"for_loop_{flb.get('workflow_run_block_id')}"
        processed_labels.add(label)
    if adaptive_caching:
        for arb in [b for b in blocks if b["block_type"] in _AGENT_REQUIRED_BLOCK_TYPES]:
            arb_label = arb.get("label") or f"{arb['block_type']}_{arb.get('workflow_run_block_id')}"
            processed_labels.add(arb_label)

    preserved_count = 0
    for cached_label, cached_source in cached_blocks.items():
        if cached_label in processed_labels:
            continue  # Already processed above
        if not cached_source.code or not cached_source.run_signature:
            continue  # Skip entries without usable code/metadata

        if script_id and script_revision_id and organization_id:
            try:
                await create_or_update_script_block(
                    block_code=cached_source.code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=cached_label,
                    update=pending,
                    run_signature=cached_source.run_signature,
                    workflow_run_id=cached_source.workflow_run_id,
                    workflow_run_block_id=cached_source.workflow_run_block_id,
                    input_fields=cached_source.input_fields,
                )
            except Exception as e:
                LOG.error(
                    "Failed to preserve cached script block from unexecuted branch",
                    error=str(e),
                    block_label=cached_label,
                    exc_info=True,
                )

        append_block_code(cached_source.code)
        preserved_count += 1

    if preserved_count > 0:
        LOG.info(
            "Preserved cached blocks from unexecuted branches during regeneration",
            preserved_count=preserved_count,
            preserved_labels=[
                label
                for label in cached_blocks
                if label not in processed_labels and cached_blocks[label].code and cached_blocks[label].run_signature
            ],
        )

    # --- runner ---------------------------------------------------------
    run_fn = _build_run_fn(blocks, workflow_run_request)

    # --- create __start_block__ -----------------------------------------
    # Build the __start_block__ content that combines imports, model classes, and run function
    start_block_body = [
        *imports,
        cst.EmptyLine(),
        cst.EmptyLine(),
        model_cls,
        cst.EmptyLine(),
        cst.EmptyLine(),
    ]

    # Add generated model class if available
    if generated_model_cls:
        start_block_body.extend(
            [
                generated_model_cls,
                cst.EmptyLine(),
                cst.EmptyLine(),
            ]
        )

    # Add run function to start block
    start_block_body.extend(
        [
            run_fn,
            cst.EmptyLine(),
            cst.EmptyLine(),
        ]
    )

    # Create script block for __start_block__ if we have script context
    if script_id and script_revision_id and organization_id:
        try:
            # Create a temporary module to convert the start block content to a function
            start_block_module = cst.Module(body=start_block_body)
            start_block_code = start_block_module.code

            await create_or_update_script_block(
                block_code=start_block_code,
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                block_label=settings.WORKFLOW_START_BLOCK_LABEL,
                update=pending,
            )
        except Exception as e:
            LOG.error("Failed to create __start_block__", error=str(e), exc_info=True)
            # Continue without script block creation if it fails

    # Build module body with the start block content and other blocks
    module_body = [
        *start_block_body,
        *block_fns,
    ]

    module = cst.Module(body=module_body)
    return module.code


async def create_or_update_script_block(
    block_code: str | bytes,
    script_revision_id: str,
    script_id: str,
    organization_id: str,
    block_label: str,
    update: bool = False,
    run_signature: str | None = None,
    workflow_run_id: str | None = None,
    workflow_run_block_id: str | None = None,
    input_fields: list[str] | None = None,
    requires_agent: bool | None = None,
) -> None:
    """
    Create a script block in the database and save the block code to a script file.
    If update is True, the script block will be updated instead of created.

    Args:
        block_code: The code to save
        script_revision_id: The script revision ID
        script_id: The script ID
        organization_id: The organization ID
        block_label: Optional custom name for the block (defaults to function name)
        update: Whether to update the script block instead of creating a new one
        run_signature: The function call code to execute this block (e.g., "await skyvern.action(...)")
        workflow_run_id: The workflow run that generated this cached block
        workflow_run_block_id: The workflow run block that generated this cached block
        input_fields: Workflow parameter field names referenced by this block's cached actions
        requires_agent: Whether this block must be executed via agent (None = don't change on update)
    """
    block_code_bytes = block_code if isinstance(block_code, bytes) else block_code.encode("utf-8")
    try:
        # Step 3: Create script block in database
        script_block = await app.DATABASE.get_script_block_by_label(
            organization_id=organization_id,
            script_revision_id=script_revision_id,
            script_block_label=block_label,
        )
        if not script_block:
            script_block = await app.DATABASE.create_script_block(
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                script_block_label=block_label,
                run_signature=run_signature,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                input_fields=input_fields,
                requires_agent=requires_agent if requires_agent is not None else False,
            )
        elif any(
            value is not None
            for value in [run_signature, workflow_run_id, workflow_run_block_id, input_fields, requires_agent]
        ):
            # Update metadata when new values are provided
            script_block = await app.DATABASE.update_script_block(
                script_block_id=script_block.script_block_id,
                organization_id=organization_id,
                run_signature=run_signature,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                input_fields=input_fields,
                requires_agent=requires_agent,
            )

        # Step 4: Create script file for the block
        # Generate a unique filename for the block
        file_name = f"{block_label}.skyvern"
        file_path = f"blocks/{file_name}"

        # Create artifact and upload to S3
        artifact_id = None
        if update and script_block.script_file_id:
            script_file = await app.DATABASE.get_script_file_by_id(
                script_revision_id,
                script_block.script_file_id,
                organization_id,
            )
            if script_file and script_file.artifact_id:
                artifact = await app.DATABASE.get_artifact_by_id(script_file.artifact_id, organization_id)
                if artifact:
                    asyncio.create_task(app.STORAGE.store_artifact(artifact, block_code_bytes))
            else:
                LOG.error("Script file or artifact not found", script_file_id=script_block.script_file_id)
        else:
            artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=script_id,
                script_version=1,  # Assuming version 1 for now
                file_path=file_path,
                data=block_code_bytes,
            )

            # Create script file record
            script_file = await app.DATABASE.create_script_file(
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                file_path=file_path,
                file_name=file_name,
                file_type="file",
                content_hash=f"sha256:{hashlib.sha256(block_code_bytes).hexdigest()}",
                file_size=len(block_code_bytes),
                mime_type="text/x-python",
                artifact_id=artifact_id,
            )

            # update script block with script file id
            await app.DATABASE.update_script_block(
                script_block_id=script_block.script_block_id,
                organization_id=organization_id,
                script_file_id=script_file.file_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )

    except Exception as e:
        # Log error but don't fail the entire generation process
        LOG.error("Failed to create script block", error=str(e), exc_info=True)
        # For now, just log the error and continue
        # In production, you might want to handle this differently
