# skyvern_codegen_cst.py
"""
Generate a runnable Skyvern workflow script **with LibCST**.

Example
-------
from skyvern_codegen_cst import generate_workflow_script

src = generate_workflow_script(
    workflow=workflow_dict,
    tasks=[task1, task2, ...],
    actions_by_task={
        task1["task_id"]: task1_actions,
        task2["task_id"]: task2_actions,
    },
)
Path("workflow.py").write_text(src)
"""

from __future__ import annotations

import keyword
from typing import Any, Iterable, Mapping

import libcst as cst
from libcst import Attribute, Call, Dict, DictElement, FunctionDef, Name, Param

# --------------------------------------------------------------------- #
# 1. helpers                                                            #
# --------------------------------------------------------------------- #

ACTION_MAP = {
    "click": "click",
    "input_text": "input_text",
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
}

INDENT = " " * 4


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


# --------------------------------------------------------------------- #
# 2. builders                                                           #
# --------------------------------------------------------------------- #


def _make_decorator(block: Mapping[str, Any]) -> cst.Decorator:
    bt = block["block_type"]
    deco_name = {
        "task": "task_block",
        "file_download": "file_download_block",
        "send_email": "email_block",
    }[bt]

    kwargs = []
    field_map = {
        "title": "title",
        "navigation_goal": "prompt",
        "url": "url",
        "engine": "engine",
        "model": "model",
        "totp_identifier": "totp_identifier",
        "webhook_callback_url": "webhook_callback_url",
        "max_steps_per_run": "max_steps",
    }

    for src_key, kw in field_map.items():
        v = block.get(src_key)
        if v not in (None, "", [], {}):
            kwargs.append(cst.Arg(value=_value(v), keyword=Name(kw)))

    # booleans
    if block.get("complete_on_download"):
        kwargs.append(cst.Arg(value=Name("True"), keyword=Name("complete_on_download")))
    if block.get("download_suffix"):
        kwargs.append(cst.Arg(value=_value(block["download_suffix"]), keyword=Name("download_suffix")))

    return cst.Decorator(
        decorator=Call(
            func=Attribute(value=Name("skyvern"), attr=Name(deco_name)),
            args=kwargs,
        )
    )


def _action_to_stmt(act: Mapping[str, Any]) -> cst.BaseStatement:
    """
    Turn one Action dict into:

        await page.<method>(xpath=..., intention=..., data=context.parameters)
    """
    method = ACTION_MAP[act["action_type"]]

    args = [
        cst.Arg(keyword=cst.Name("xpath"), value=_value(act["xpath"])),
        cst.Arg(
            keyword=cst.Name("intention"),
            value=_value(act.get("intention") or act.get("reasoning") or ""),
        ),
        cst.Arg(
            keyword=cst.Name("data"),
            value=cst.Attribute(value=cst.Name("context"), attr=cst.Name("parameters")),
        ),
    ]

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("page"), attr=cst.Name(method)),
        args=args,
    )

    # await page.method(...)
    await_expr = cst.Await(call)

    # Wrap in a statement line:  await ...
    return cst.SimpleStatementLine([cst.Expr(await_expr)])


def _build_block_fn(block: Mapping[str, Any], actions: Iterable[Mapping[str, Any]]) -> FunctionDef:
    name = _safe_name(block["title"])
    body_stmts: list[cst.BaseStatement] = []

    if block.get("url"):
        body_stmts.append(cst.parse_statement(f"await page.goto({repr(block['url'])})"))

    for act in actions:
        body_stmts.append(_action_to_stmt(act))

    if not body_stmts:
        body_stmts.append(cst.parse_statement("return None"))

    return FunctionDef(
        name=Name(name),
        params=cst.Parameters(
            params=[
                Param(name=Name("page")),
                Param(name=Name("context")),
            ]
        ),
        decorators=[_make_decorator(block)],
        body=cst.IndentedBlock(body_stmts),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


def _build_model(workflow: Mapping[str, Any]) -> cst.ClassDef:
    """
    class WorkflowParameters(BaseModel):
        ein_info: str
        company_name: str
        ...
    """
    ann_lines: list[cst.BaseStatement] = []

    for p in workflow["workflow_definition"]["parameters"]:
        if p["parameter_type"] != "workflow":
            continue

        # ein_info: str
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


def _build_cached_params() -> cst.SimpleStatementLine:
    src = "cached_parameters = WorkflowParameters(**{k: f'<{k}>' for k in WorkflowParameters.model_fields})"
    return cst.parse_statement(src)


def _build_run_fn(task_fns: list[str]) -> FunctionDef:
    body = [cst.parse_statement("page, context = await skyvern.setup(parameters.model_dump())")] + [
        cst.parse_statement(f"await {_safe_name(t)}(page, context)") for t in task_fns
    ]

    return FunctionDef(
        name=Name("run_workflow"),
        decorators=[cst.Decorator(Attribute(value=Name("skyvern"), attr=Name("workflow")))],
        params=cst.Parameters(
            params=[
                Param(
                    name=Name("parameters"),
                    default=Name("cached_parameters"),
                    annotation=cst.Annotation(Name("WorkflowParameters")),
                )
            ]
        ),
        body=cst.IndentedBlock(body),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


# --------------------------------------------------------------------- #
# 3. entrypoint                                                         #
# --------------------------------------------------------------------- #


def generate_workflow_script(
    *,
    workflow: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    actions_by_task: Mapping[str, Iterable[Mapping[str, Any]]],
) -> str:
    """
    Build a LibCST Module and emit .code (PEP-8-formatted source).
    """
    # --- imports --------------------------------------------------------
    imports: list[cst.BaseStatement] = [
        cst.SimpleStatementLine([cst.Import(names=[cst.ImportAlias(cst.Name("pydantic"))])]),
        cst.SimpleStatementLine(
            [
                cst.ImportFrom(
                    module=cst.Name("pydantic"),
                    names=[cst.ImportAlias(cst.Name("BaseModel"))],
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

    # --- class + cached params -----------------------------------------
    model_cls = _build_model(workflow)
    cached_params_stmt = _build_cached_params()

    # --- blocks ---------------------------------------------------------
    block_fns: list[FunctionDef] = []
    task_titles = []
    for t in tasks:
        fn = _build_block_fn(t, actions_by_task.get(t["task_id"], []))
        block_fns.append(fn)
        task_titles.append(t["title"])

    # --- runner ---------------------------------------------------------
    run_fn = _build_run_fn(task_titles)

    module = cst.Module(
        body=[
            *imports,
            cst.EmptyLine(),
            model_cls,
            cst.EmptyLine(),
            cached_params_stmt,
            cst.EmptyLine(),
            *block_fns,
            cst.EmptyLine(),
            run_fn,
            cst.EmptyLine(),
        ]
    )
    return module.code
