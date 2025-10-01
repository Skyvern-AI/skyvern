# skyvern_codegen_cst.py
"""
Generate a runnable Skyvern workflow script.

"""

from __future__ import annotations

import asyncio
import hashlib
import keyword
import re
from typing import Any

import libcst as cst
import structlog
from libcst import Attribute, Call, Dict, DictElement, FunctionDef, Name, Param

from skyvern.config import settings
from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.core.script_generations.generate_workflow_parameters import (
    generate_workflow_parameters_schema,
    hydrate_input_text_actions_with_field_names,
)
from skyvern.forge import app
from skyvern.schemas.workflows import FileStorageType
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger(__name__)


# --------------------------------------------------------------------- #
# 1. helpers                                                            #
# --------------------------------------------------------------------- #


def sanitize_variable_name(name: str) -> str:
    """
    Sanitize a string to be a valid Python variable name.

    - Converts to snake_case
    - Removes invalid characters
    - Ensures it doesn't start with a number
    - Handles Python keywords by appending underscore
    - Removes empty spaces
    """
    # Remove leading/trailing whitespace and replace internal spaces with underscores
    name = name.strip().replace(" ", "_")

    # Convert to snake_case: handle camelCase and PascalCase
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)

    # Remove any characters that aren't alphanumeric or underscore
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Convert to lowercase
    name = name.lower()

    # Remove consecutive underscores
    name = re.sub(r"_+", "_", name)

    # Remove leading/trailing underscores
    name = name.strip("_")

    # Ensure it doesn't start with a number
    if name and name[0].isdigit():
        name = f"param_{name}"

    # Handle empty string or invalid names
    if not name or name == "_":
        name = "param"

    # Handle Python keywords
    if keyword.iskeyword(name):
        name = f"{name}_"

    return name


ACTION_MAP = {
    "click": "click",
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
}
ACTIONS_WITH_XPATH = [
    "click",
    "input_text",
    "type",
    "fill",
    "upload_file",
    "select_option",
]

INDENT = " " * 4
DOUBLE_INDENT = " " * 8


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


def _action_to_stmt(act: dict[str, Any], task: dict[str, Any], assign_to_output: bool = False) -> cst.BaseStatement:
    """
    Turn one Action dict into:

        await page.<method>(xpath=..., intention=..., data=context.parameters)

    Or if assign_to_output is True for extract actions:

        output = await page.extract(...)
    """
    method = ACTION_MAP[act["action_type"]]

    args: list[cst.Arg] = []
    if method in ACTIONS_WITH_XPATH:
        args.append(
            cst.Arg(
                keyword=cst.Name("xpath"),
                value=_value(act["xpath"]),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )

    if method in ["type", "fill"]:
        # Use context.parameters if field_name is available, otherwise fallback to direct value
        if act.get("field_name"):
            text_value = cst.Subscript(
                value=cst.Attribute(
                    value=cst.Name("context"),
                    attr=cst.Name("parameters"),
                ),
                slice=[cst.SubscriptElement(slice=cst.Index(value=_value(act["field_name"])))],
            )
        else:
            text_value = _value(act["text"])

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
                keyword=cst.Name("ai_infer"),
                value=cst.Name("True"),
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
        if value:
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
                    keyword=cst.Name("ai_infer"),
                    value=cst.Name("True"),
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
                keyword=cst.Name("ai_infer"),
                value=cst.Name("True"),
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

    args.extend(
        [
            cst.Arg(
                keyword=cst.Name("intention"),
                value=_value(act.get("intention") or act.get("reasoning") or ""),
                whitespace_after_arg=cst.ParenthesizedWhitespace(indent=True),
                comma=cst.Comma(),
            ),
        ]
    )

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("page"), attr=cst.Name(method)),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
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


def _build_block_fn(block: dict[str, Any], actions: list[dict[str, Any]]) -> FunctionDef:
    name = block.get("label") or _safe_name(block.get("title") or f"block_{block.get('workflow_run_block_id')}")
    body_stmts: list[cst.BaseStatement] = []
    is_extraction_block = block.get("block_type") == "extraction"

    if block.get("url"):
        body_stmts.append(cst.parse_statement(f"await page.goto({repr(block['url'])})"))

    for act in actions:
        if act["action_type"] in [ActionType.COMPLETE, ActionType.TERMINATE, ActionType.NULL_ACTION]:
            continue

        # For extraction blocks, assign extract action results to output variable
        assign_to_output = is_extraction_block and act["action_type"] == "extract"
        body_stmts.append(_action_to_stmt(act, block, assign_to_output=assign_to_output))

    # For extraction blocks, add return output statement if we have actions
    if is_extraction_block and any(
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
        decorators=[_make_decorator(name, block)],
        body=cst.IndentedBlock(body_stmts),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


def _build_task_v2_block_fn(block: dict[str, Any], child_blocks: list[dict[str, Any]]) -> FunctionDef:
    """Build a cached function for task_v2 blocks that calls child workflow sub-tasks."""
    name = block.get("label") or _safe_name(block.get("title") or f"block_{block.get('workflow_run_block_id')}")
    body_stmts: list[cst.BaseStatement] = []

    # Add calls to child workflow sub-tasks
    for child_block in child_blocks:
        stmt = _build_block_statement(child_block)
        body_stmts.append(stmt)

    if not body_stmts:
        body_stmts.append(cst.parse_statement("return None"))

    return FunctionDef(
        name=Name(name),
        params=cst.Parameters(
            params=[
                Param(name=Name("page"), annotation=cst.Annotation(cst.Name("SkyvernPage"))),
                Param(name=Name("context"), annotation=cst.Annotation(cst.Name("RunContext"))),
            ]
        ),
        decorators=[_make_decorator(name, block)],
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
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.run_task statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name)
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
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.download statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_value(block.get("navigation_goal") or ""),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
    ]
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
    args.append(
        cst.Arg(
            keyword=cst.Name("cache_key"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        )
    )

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
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

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
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.login statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name)
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
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
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
        cst.Arg(
            keyword=cst.Name("label"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("extract")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_navigate_statement(
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
) -> cst.SimpleStatementLine:
    """Build a skyvern.navigate statement."""
    args = __build_base_task_statement(block_title, block, data_variable_name)
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
            value=_value(block.get("parameters", None)),
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
            value=_value(block.get("parameters", None)),
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

    if block.get("parameters") is not None:
        args.append(
            cst.Arg(
                keyword=cst.Name("parameters"),
                value=_value(block.get("parameters")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                ),
            )
        )

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


def _build_goto_statement_for_loop(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.goto statement for use within loops, handling current_value template."""
    url_value = block.get("url", "")

    # Handle {{current_value}} template by replacing it with the current_value variable
    if url_value == "{{current_value}}":
        url_expr = cst.Name("current_value")
    else:
        url_expr = _value(url_value)

    args = [
        cst.Arg(
            keyword=cst.Name("url"),
            value=url_expr,
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
    block_title: str, block: dict[str, Any], data_variable_name: str | None = None
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

    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_value(prompt),
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


def _build_block_statement(block: dict[str, Any], data_variable_name: str | None = None) -> cst.SimpleStatementLine:
    """Build a block statement."""
    block_type = block.get("block_type")
    block_title = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"

    if block_type in SCRIPT_TASK_BLOCKS:
        # For task blocks, call the custom function with cache_key
        if block_type == "task":
            stmt = _build_run_task_statement(block_title, block, data_variable_name)
        elif block_type == "file_download":
            stmt = _build_download_statement(block_title, block, data_variable_name)
        elif block_type == "action":
            stmt = _build_action_statement(block_title, block, data_variable_name)
        elif block_type == "login":
            stmt = _build_login_statement(block_title, block, data_variable_name)
        elif block_type == "extraction":
            stmt = _build_extract_statement(block_title, block, data_variable_name)
        elif block_type == "navigation":
            stmt = _build_navigate_statement(block_title, block, data_variable_name)
    elif block_type == "validation":
        stmt = _build_validate_statement(block_title, block, data_variable_name)
    elif block_type == "task_v2":
        stmt = _build_run_task_statement(block_title, block, data_variable_name)
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
    else:
        # Default case for unknown block types
        stmt = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(f"# Unknown block type: {block_type}"))])

    return stmt


def _build_run_fn(blocks: list[dict[str, Any]], wf_req: dict[str, Any]) -> FunctionDef:
    body = [
        cst.parse_statement(
            "parameters = parameters.model_dump() if isinstance(parameters, WorkflowParameters) else parameters"
        ),
        cst.parse_statement("page, context = await skyvern.setup(parameters, GeneratedWorkflowParameters)"),
    ]

    for block in blocks:
        stmt = _build_block_statement(block)
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
) -> str:
    """
    Build a LibCST Module and emit .code (PEP-8-formatted source).
    """
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
    generated_schema, field_mappings = await generate_workflow_parameters_schema(actions_by_task)
    actions_by_task = hydrate_input_text_actions_with_field_names(actions_by_task, field_mappings)

    # --- class + cached params -----------------------------------------
    model_cls = _build_model(workflow)
    generated_model_cls = _build_generated_model_from_schema(generated_schema)

    # --- blocks ---------------------------------------------------------
    block_fns = []
    task_v1_blocks = [block for block in blocks if block["block_type"] in SCRIPT_TASK_BLOCKS]
    task_v2_blocks = [block for block in blocks if block["block_type"] == "task_v2"]

    if task_v2_child_blocks is None:
        task_v2_child_blocks = {}

    # Handle task v1 blocks (excluding child blocks of task_v2)
    for idx, task in enumerate(task_v1_blocks):
        # Skip if this is a child block of a task_v2 block
        if task.get("parent_task_v2_label"):
            continue

        block_fn_def = _build_block_fn(task, actions_by_task.get(task.get("task_id", ""), []))

        # Create script block if we have script context
        if script_id and script_revision_id and organization_id:
            try:
                block_name = task.get("label") or task.get("title") or task.get("task_id") or f"task_{idx}"
                temp_module = cst.Module(body=[block_fn_def])
                block_code = temp_module.code
                await create_or_update_script_block(
                    block_code=block_code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=block_name,
                    update=pending,
                )
            except Exception as e:
                LOG.error("Failed to create script block", error=str(e), exc_info=True)
                # Continue without script block creation if it fails

        block_fns.append(block_fn_def)
        if idx < len(task_v1_blocks) - 1:
            block_fns.append(cst.EmptyLine())
            block_fns.append(cst.EmptyLine())

    # Handle task_v2 blocks
    for idx, task_v2 in enumerate(task_v2_blocks):
        task_v2_label = task_v2.get("label") or f"task_v2_{task_v2.get('workflow_run_block_id')}"
        child_blocks = task_v2_child_blocks.get(task_v2_label, [])

        # Create the task_v2 function
        task_v2_fn_def = _build_task_v2_block_fn(task_v2, child_blocks)

        # Create script block for task_v2 that includes both the main function and child functions
        if script_id and script_revision_id and organization_id:
            try:
                # Build the complete module for this task_v2 block
                task_v2_block_body = [task_v2_fn_def]

                # Add child block functions
                for child_block in child_blocks:
                    if (
                        child_block.get("block_type") in SCRIPT_TASK_BLOCKS
                        and child_block.get("block_type") != "task_v2"
                    ):
                        child_fn_def = _build_block_fn(
                            child_block, actions_by_task.get(child_block.get("task_id", ""), [])
                        )
                        task_v2_block_body.append(cst.EmptyLine())
                        task_v2_block_body.append(cst.EmptyLine())
                        task_v2_block_body.append(child_fn_def)

                # Create the complete module for this task_v2 block
                temp_module = cst.Module(body=task_v2_block_body)
                task_v2_block_code = temp_module.code

                block_name = task_v2.get("label") or task_v2.get("title") or f"task_v2_{idx}"

                await create_or_update_script_block(
                    block_code=task_v2_block_code,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_label=block_name,
                )
            except Exception as e:
                LOG.error("Failed to create task_v2 script block", error=str(e), exc_info=True)
                # Continue without script block creation if it fails

        block_fns.append(task_v2_fn_def)

        # Create individual functions for child blocks
        for child_block in child_blocks:
            if child_block.get("block_type") in SCRIPT_TASK_BLOCKS and child_block.get("block_type") != "task_v2":
                child_fn_def = _build_block_fn(child_block, actions_by_task.get(child_block.get("task_id", ""), []))
                block_fns.append(cst.EmptyLine())
                block_fns.append(cst.EmptyLine())
                block_fns.append(child_fn_def)

        if idx < len(task_v2_blocks) - 1:
            block_fns.append(cst.EmptyLine())
            block_fns.append(cst.EmptyLine())

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
    """
    block_code_bytes = block_code if isinstance(block_code, bytes) else block_code.encode("utf-8")
    try:
        # Step 3: Create script block in database
        script_block = None
        if update:
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
            )

    except Exception as e:
        # Log error but don't fail the entire generation process
        LOG.error("Failed to create script block", error=str(e), exc_info=True)
        # For now, just log the error and continue
        # In production, you might want to handle this differently
