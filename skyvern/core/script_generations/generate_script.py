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
from enum import StrEnum
from typing import Any

import libcst as cst
import structlog
from libcst import Attribute, Call, Dict, DictElement, FunctionDef, Name, Param

from skyvern.forge import app
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger(__name__)


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
    "wait": "wait",
    "extract": "extract",
}
ACTIONS_WITH_XPATH = [
    "click",
    "input_text",
    "upload_file",
    "select_option",
]

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


def _make_decorator(block: dict[str, Any]) -> cst.Decorator:
    bt = block["block_type"]
    deco_name = {
        "task": "task_block",
        "file_download": "file_download_block",
        "send_email": "email_block",
        "wait": "wait_block",
        "navigation": "navigation_block",
        "for_loop": "for_loop_block",
        "action": "action_block",
        "extraction": "extraction_block",
        "login": "login_block",
        "text_prompt": "text_prompt_block",
        "goto_url": "url_block",
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
        "wait_sec": "seconds",
    }

    for src_key, kw in field_map.items():
        v = block.get(src_key)
        if v not in (None, "", [], {}):
            if isinstance(v, StrEnum):
                v = v.value
            try:
                kwargs.append(cst.Arg(value=_value(v), keyword=Name(kw)))
            except Exception:
                raise

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


def _action_to_stmt(act: dict[str, Any]) -> cst.BaseStatement:
    """
    Turn one Action dict into:

        await page.<method>(xpath=..., intention=..., data=context.parameters)
    """
    method = ACTION_MAP[act["action_type"]]

    args: list[cst.Arg] = []
    if method == "input_text":
        args.append(cst.Arg(keyword=cst.Name("text"), value=_value(act["text"])))
    elif method == "select_option":
        args.append(cst.Arg(keyword=cst.Name("option"), value=_value(act["option"]["value"])))
    elif method == "wait":
        args.append(cst.Arg(keyword=cst.Name("seconds"), value=_value(act["seconds"])))

    args.extend(
        [
            cst.Arg(
                keyword=cst.Name("intention"),
                value=_value(act.get("intention") or act.get("reasoning") or ""),
            ),
            cst.Arg(
                keyword=cst.Name("data"),
                value=cst.Attribute(value=cst.Name("context"), attr=cst.Name("parameters")),
            ),
        ]
    )
    if method in ACTIONS_WITH_XPATH:
        args.append(cst.Arg(keyword=cst.Name("xpath"), value=_value(act["xpath"])))

    call = cst.Call(
        func=cst.Attribute(value=cst.Name("page"), attr=cst.Name(method)),
        args=args,
    )

    # await page.method(...)
    await_expr = cst.Await(call)

    # Wrap in a statement line:  await ...
    return cst.SimpleStatementLine([cst.Expr(await_expr)])


def _build_block_fn(block: dict[str, Any], actions: list[dict[str, Any]]) -> FunctionDef:
    name = _safe_name(block.get("title") or block.get("label") or f"block_{block.get('workflow_run_block_id')}")
    body_stmts: list[cst.BaseStatement] = []

    if block.get("url"):
        body_stmts.append(cst.parse_statement(f"await page.goto({repr(block['url'])})"))

    for act in actions:
        if act["action_type"] in [ActionType.COMPLETE, ActionType.TERMINATE, ActionType.NULL_ACTION]:
            continue
        body_stmts.append(_action_to_stmt(act))

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
        decorators=[_make_decorator(block)],
        body=cst.IndentedBlock(body_stmts),
        returns=None,
        asynchronous=cst.Asynchronous(),
    )


def _build_model(workflow: dict[str, Any]) -> cst.ClassDef:
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


def _build_cached_params(values: dict[str, Any]) -> cst.SimpleStatementLine:
    """
    Make a CST for:
        cached_parameters = WorkflowParameters(ein_info="...", ...)
    """
    call = cst.Call(
        func=cst.Name("WorkflowParameters"),
        args=[cst.Arg(keyword=cst.Name(k), value=_value(v)) for k, v in values.items()],
    )

    assign = cst.Assign(
        targets=[cst.AssignTarget(cst.Name("cached_parameters"))],
        value=call,
    )
    return cst.SimpleStatementLine([assign])


def _build_run_fn(task_titles: list[str], wf_req: dict[str, Any]) -> FunctionDef:
    body = [
        cst.parse_statement("page, context = await skyvern.setup(parameters.model_dump())"),
        *[cst.parse_statement(f"await {_safe_name(t)}(page, context)") for t in task_titles],
    ]

    params = cst.Parameters(
        params=[
            Param(
                name=cst.Name("parameters"),
                annotation=cst.Annotation(cst.Name("WorkflowParameters")),
                default=cst.Name("cached_parameters"),
            ),
            Param(
                name=cst.Name("title"),
                annotation=cst.Annotation(cst.Name("str")),
                default=_value(wf_req.get("title", "")),
            ),
            Param(
                name=cst.Name("webhook_url"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("webhook_url")),
            ),
            Param(
                name=cst.Name("totp_url"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("totp_url")),
            ),
            Param(
                name=cst.Name("totp_identifier"),
                annotation=cst.Annotation(cst.parse_expression("str | None")),
                default=_value(wf_req.get("totp_identifier")),
            ),
        ]
    )

    return FunctionDef(
        name=cst.Name("run_workflow"),
        asynchronous=cst.Asynchronous(),
        decorators=[_workflow_decorator(wf_req)],
        params=params,
        body=cst.IndentedBlock(body),
    )


# --------------------------------------------------------------------- #
# 3. entrypoint                                                         #
# --------------------------------------------------------------------- #


async def generate_workflow_script(
    *,
    file_name: str,
    workflow_run_request: dict[str, Any],
    workflow: dict[str, Any],
    tasks: list[dict[str, Any]],
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
    cached_params_stmt = _build_cached_params(workflow_run_request.get("parameters", {}))

    # --- blocks ---------------------------------------------------------
    block_fns = []
    length_of_tasks = len(tasks)

    # Create script first if organization_id is provided
    for idx, task in enumerate(tasks):
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

    task_titles: list[str] = [
        t.get("title") or t.get("label") or t.get("task_id") or f"unknown_title_{idx}" for idx, t in enumerate(tasks)
    ]

    # --- runner ---------------------------------------------------------
    run_fn = _build_run_fn(task_titles, workflow_run_request)

    module = cst.Module(
        body=[
            *imports,
            cst.EmptyLine(),
            cst.EmptyLine(),
            model_cls,
            cst.EmptyLine(),
            cst.EmptyLine(),
            cached_params_stmt,
            cst.EmptyLine(),
            cst.EmptyLine(),
            *block_fns,
            cst.EmptyLine(),
            cst.EmptyLine(),
            run_fn,
            cst.EmptyLine(),
            cst.EmptyLine(),
            cst.parse_statement("if __name__ == '__main__':\n    asyncio.run(run_workflow())"),
        ]
    )

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
        file_path = f"blocks/{script_block.script_block_id}/{file_name}"

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
