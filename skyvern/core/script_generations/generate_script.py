# skyvern_codegen_cst.py
"""
Generate a runnable Skyvern workflow script.

Example
-------
generated_code = generate_workflow_script(
    file_name="workflow.py",
    workflow_run_request=workflow_run_request,
    workflow=workflow,
    tasks=tasks,
    actions_by_task=actions_by_task,
)
Path("workflow.py").write_text(src)
"""

from __future__ import annotations

import hashlib
import keyword
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
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger(__name__)


# --------------------------------------------------------------------- #
# 1. helpers                                                            #
# --------------------------------------------------------------------- #

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
            return cst.SimpleString('"""' + value.replace('"""', '\\"\\"\\"') + '"""')
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


def _render_value(prompt_text: str) -> cst.BaseExpression:
    """Create a prompt value with template rendering logic if needed."""
    if "{{" in prompt_text and "}}" in prompt_text:
        # Generate code for: render_template(prompt_text)
        return cst.Call(
            func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("render_template")),
            args=[cst.Arg(value=_value(prompt_text))],
        )
    else:
        # Return the prompt as a simple string value
        return _value(prompt_text)


def _generate_text_call(text_value: str, intention: str, parameter_key: str) -> cst.BaseExpression:
    """Create a generate_text function call CST expression."""
    return cst.Await(
        expression=cst.Call(
            func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("generate_text")),
            whitespace_before_args=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(DOUBLE_INDENT),
            ),
            args=[
                # First positional argument: context.generated_parameters['parameter_key']
                cst.Arg(
                    value=cst.Subscript(
                        value=cst.Attribute(
                            value=cst.Name("context"),
                            attr=cst.Name("generated_parameters"),
                        ),
                        slice=[cst.SubscriptElement(slice=cst.Index(value=_value(parameter_key)))],
                    ),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(DOUBLE_INDENT),
                    ),
                ),
                # intention keyword argument
                cst.Arg(
                    keyword=cst.Name("intention"),
                    value=_value(intention),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(DOUBLE_INDENT),
                    ),
                ),
                # data keyword argument
                cst.Arg(
                    keyword=cst.Name("data"),
                    value=cst.Attribute(
                        value=cst.Name("context"),
                        attr=cst.Name("parameters"),
                    ),
                    whitespace_after_arg=cst.ParenthesizedWhitespace(
                        indent=True,
                        last_line=cst.SimpleWhitespace(INDENT),
                    ),
                    comma=cst.Comma(),
                ),
            ],
        )
    )


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


def _action_to_stmt(act: dict[str, Any], assign_to_output: bool = False) -> cst.BaseStatement:
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
        # Get intention from action
        intention = act.get("intention") or act.get("reasoning") or ""

        # Use generate_text call if field_name is available, otherwise fallback to direct value
        if act.get("field_name"):
            text_value = _generate_text_call(
                text_value=act["text"], intention=intention, parameter_key=act["field_name"]
            )
        else:
            text_value = _value(act["text"])

        args.append(
            cst.Arg(
                keyword=cst.Name("text"),
                value=text_value,
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    elif method == "select_option":
        args.append(
            cst.Arg(
                keyword=cst.Name("option"),
                value=_value(act["option"]["value"]),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
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

    args.extend(
        [
            cst.Arg(
                keyword=cst.Name("intention"),
                value=_value(act.get("intention") or act.get("reasoning") or ""),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
            cst.Arg(
                keyword=cst.Name("data"),
                value=cst.Attribute(value=cst.Name("context"), attr=cst.Name("parameters")),
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
        body_stmts.append(_action_to_stmt(act, assign_to_output=assign_to_output))

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


def _build_model(workflow: dict[str, Any]) -> cst.ClassDef:
    """
    class WorkflowParameters(BaseModel):
        param1: str
        param2: str
        ...
    """
    ann_lines: list[cst.BaseStatement] = []

    for p in workflow["workflow_definition"]["parameters"]:
        if p["parameter_type"] != "workflow":
            continue

        ann = cst.AnnAssign(
            target=cst.Name(p["key"]),
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


def _build_run_task_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.run_task statement."""
    args = __build_base_task_statement(block_title, block)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("run_task")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_download_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.download statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("complete_on_download"),
            value=_value(block.get("complete_on_download", False)),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("download_suffix"),
            value=_render_value(block.get("download_suffix", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("cache_key"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("download")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_action_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.action statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("cache_key"),
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


def _build_login_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.login statement."""
    args = __build_base_task_statement(block_title, block)
    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("login")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_extract_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.extract statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("data_extraction_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("cache_key"),
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


def _build_navigate_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.navigate statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
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
        cst.Arg(
            keyword=cst.Name("max_steps"),
            value=_value(block.get("max_steps_per_run", settings.MAX_STEPS_PER_RUN)),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("cache_key"),
            value=_value(block_title),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

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
            keyword=cst.Name("recipients"),
            value=_value(block.get("recipients", [])),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("attach_downloaded_files"),
            value=_value(block.get("attach_downloaded_files", False)),
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


def _build_validate_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.validate statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

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


def _build_for_loop_statement(block_title: str, block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.for_loop statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
                last_line=cst.SimpleWhitespace(INDENT),
            ),
        ),
        cst.Arg(
            keyword=cst.Name("max_steps"),
            value=_value(block.get("max_steps_per_run", settings.MAX_STEPS_PER_RUN)),
            whitespace_after_arg=cst.ParenthesizedWhitespace(
                indent=True,
            ),
            comma=cst.Comma(),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("for_loop")),
        args=args,
        whitespace_before_args=cst.ParenthesizedWhitespace(
            indent=True,
            last_line=cst.SimpleWhitespace(INDENT),
        ),
    )

    return cst.SimpleStatementLine([cst.Expr(cst.Await(call))])


def _build_goto_statement(block: dict[str, Any]) -> cst.SimpleStatementLine:
    """Build a skyvern.goto statement."""
    args = [
        cst.Arg(
            keyword=cst.Name("url"),
            value=_value(block.get("url", "")),
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


def __build_base_task_statement(block_title: str, block: dict[str, Any]) -> list[cst.Arg]:
    args = [
        cst.Arg(
            keyword=cst.Name("prompt"),
            value=_render_value(block.get("navigation_goal", "")),
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
                value=_render_value(block.get("url", "")),
                whitespace_after_arg=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            )
        )
    if block.get("max_steps_per_run"):
        args.append(
            cst.Arg(
                keyword=cst.Name("max_steps"),
                value=_render_value(block.get("max_steps_per_run", settings.MAX_STEPS_PER_RUN)),
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
                value=_render_value(block.get("totp_identifier", "")),
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
                value=_render_value(block.get("totp_verification_url", "")),
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
    return args


# --------------------------------------------------------------------- #
# 4. function builders                                                  #
# --------------------------------------------------------------------- #


def _build_run_fn(blocks: list[dict[str, Any]], wf_req: dict[str, Any]) -> FunctionDef:
    body = [
        cst.parse_statement(
            "parameters = parameters.model_dump() if isinstance(parameters, WorkflowParameters) else parameters"
        ),
        cst.parse_statement("page, context = await skyvern.setup(parameters, GeneratedWorkflowParameters)"),
    ]

    for block in blocks:
        block_type = block.get("block_type")
        block_title = block.get("label") or block.get("title") or f"block_{block.get('workflow_run_block_id')}"

        if block_type in SCRIPT_TASK_BLOCKS:
            # For task blocks, call the custom function with cache_key
            if block_type == "task":
                stmt = _build_run_task_statement(block_title, block)
            elif block_type == "file_download":
                stmt = _build_download_statement(block_title, block)
            elif block_type == "action":
                stmt = _build_action_statement(block_title, block)
            elif block_type == "login":
                stmt = _build_login_statement(block_title, block)
            elif block_type == "extraction":
                stmt = _build_extract_statement(block_title, block)
            elif block_type == "navigation":
                stmt = _build_navigate_statement(block_title, block)
        elif block_type == "send_email":
            stmt = _build_send_email_statement(block)
        elif block_type == "text_prompt":
            stmt = _build_validate_statement(block)
        elif block_type == "wait":
            stmt = _build_wait_statement(block)
        elif block_type == "for_loop":
            stmt = _build_for_loop_statement(block_title, block)
        elif block_type == "goto_url":
            stmt = _build_goto_statement(block)
        else:
            # Default case for unknown block types
            stmt = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(f"# Unknown block type: {block_type}"))])

        body.append(stmt)

    # Add a final validation step if not already present
    has_validation = any(block.get("block_type") == "text_prompt" for block in blocks)
    has_task_blocks = any(block.get("block_type") in SCRIPT_TASK_BLOCKS for block in blocks)
    if not has_validation and not has_task_blocks:
        # Build the final validation statement using LibCST components
        args = [
            cst.Arg(
                keyword=cst.Name("prompt"),
                value=cst.SimpleString(
                    '"Your goal is to validate that the workflow completed successfully. COMPLETE if successful, TERMINATE if there are issues."'
                ),
            ),
        ]

        call = cst.Call(
            func=cst.Attribute(value=cst.Name("skyvern"), attr=cst.Name("validate")),
            args=args,
        )

        validation_stmt = cst.SimpleStatementLine([cst.Expr(cst.Await(call))])
        body.append(validation_stmt)

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
                whitespace_after_param=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
            Param(
                name=cst.Name("title"),
                annotation=cst.Annotation(cst.Name("str")),
                default=_value(wf_req.get("title", "")),
                whitespace_after_param=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
            Param(
                name=cst.Name("webhook_url"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("webhook_url")),
                whitespace_after_param=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
            Param(
                name=cst.Name("totp_url"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("totp_url")),
                whitespace_after_param=cst.ParenthesizedWhitespace(
                    indent=True,
                    last_line=cst.SimpleWhitespace(INDENT),
                ),
            ),
            Param(
                name=cst.Name("totp_identifier"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("totp_identifier")),
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


async def generate_workflow_script(
    *,
    file_name: str,
    workflow_run_request: dict[str, Any],
    workflow: dict[str, Any],
    blocks: list[dict[str, Any]],
    actions_by_task: dict[str, list[dict[str, Any]]],
    organization_id: str | None = None,
    run_id: str | None = None,
    script_id: str | None = None,
    script_revision_id: str | None = None,
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
    task_blocks = [block for block in blocks if block["block_type"] in SCRIPT_TASK_BLOCKS]
    length_of_tasks = len(task_blocks)

    # Create script first if organization_id is provided
    for idx, task in enumerate(task_blocks):
        block_fn_def = _build_block_fn(task, actions_by_task.get(task.get("task_id", ""), []))

        # Create script block if we have script context
        if script_id and script_revision_id and organization_id:
            try:
                block_name = task.get("title") or task.get("label") or task.get("task_id") or f"task_{idx}"
                block_description = f"Generated block for task: {block_name}"
                await create_script_block(
                    block_fn_def=block_fn_def,
                    script_revision_id=script_revision_id,
                    script_id=script_id,
                    organization_id=organization_id,
                    block_name=block_name,
                    block_description=block_description,
                )
            except Exception as e:
                LOG.error("Failed to create script block", error=str(e), exc_info=True)
                # Continue without script block creation if it fails

        block_fns.append(block_fn_def)
        if idx < length_of_tasks - 1:
            block_fns.append(cst.EmptyLine())
            block_fns.append(cst.EmptyLine())

    # --- runner ---------------------------------------------------------
    run_fn = _build_run_fn(blocks, workflow_run_request)

    # Build module body with optional generated model class
    module_body = [
        *imports,
        cst.EmptyLine(),
        cst.EmptyLine(),
        model_cls,
        cst.EmptyLine(),
        cst.EmptyLine(),
    ]

    # Add generated model class if available
    if generated_model_cls:
        module_body.extend(
            [
                generated_model_cls,
                cst.EmptyLine(),
                cst.EmptyLine(),
            ]
        )

    # Continue with the rest of the module
    module_body.extend(
        [
            *block_fns,
            cst.EmptyLine(),
            cst.EmptyLine(),
            run_fn,
            cst.EmptyLine(),
            cst.EmptyLine(),
            cst.parse_statement("if __name__ == '__main__':\n    asyncio.run(run_workflow())"),
        ]
    )

    module = cst.Module(body=module_body)

    with open(file_name, "w") as f:
        f.write(module.code)
    return module.code


async def create_script_block(
    block_fn_def: FunctionDef,
    script_revision_id: str,
    script_id: str,
    organization_id: str,
    block_name: str,
    block_description: str | None = None,
) -> None:
    """
    Create a script block in the database and save the block code to a script file.

    Args:
        block_fn_def: The LibCST function definition to save
        script_revision_id: The script revision ID
        script_id: The script ID
        organization_id: The organization ID
        block_name: Optional custom name for the block (defaults to function name)
        block_description: Optional description for the block
    """
    try:
        # Step 1: Transform the block function definition to a string
        # Create a temporary module to convert FunctionDef to source code
        temp_module = cst.Module(body=[block_fn_def])
        block_code = temp_module.code

        # Step 3: Create script block in database
        script_block = await app.DATABASE.create_script_block(
            script_revision_id=script_revision_id,
            script_id=script_id,
            organization_id=organization_id,
            script_block_label=block_name,
        )

        # Step 4: Create script file for the block
        # Generate a unique filename for the block
        file_name = f"{block_name}.skyvern"
        file_path = f"blocks/{file_name}"

        # Create artifact and upload to S3
        artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
            organization_id=organization_id,
            script_id=script_id,
            script_version=1,  # Assuming version 1 for now
            file_path=file_path,
            data=block_code.encode("utf-8"),
        )

        # Create script file record
        script_file = await app.DATABASE.create_script_file(
            script_revision_id=script_revision_id,
            script_id=script_id,
            organization_id=organization_id,
            file_path=file_path,
            file_name=file_name,
            file_type="file",
            content_hash=f"sha256:{hashlib.sha256(block_code.encode('utf-8')).hexdigest()}",
            file_size=len(block_code.encode("utf-8")),
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
