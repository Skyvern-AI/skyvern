import base64

import structlog
from jinja2.sandbox import SandboxedEnvironment

from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code
from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun
from skyvern.schemas.scripts import FileEncoding, Script, ScriptFileCreate, ScriptStatus
from skyvern.services import script_service

LOG = structlog.get_logger()
jinja_sandbox_env = SandboxedEnvironment()


async def generate_or_update_pending_workflow_script(
    workflow_run: WorkflowRun,
    workflow: Workflow,
) -> None:
    organization_id = workflow.organization_id
    context = skyvern_context.current()
    if not context:
        return
    script_id = context.script_id
    script = None
    if script_id:
        script = await app.DATABASE.get_script(script_id=script_id, organization_id=organization_id)

    if not script:
        script = await app.DATABASE.create_script(organization_id=organization_id, run_id=workflow_run.workflow_run_id)
        if context:
            context.script_id = script.script_id
            context.script_revision_id = script.script_revision_id

    _, rendered_cache_key_value = await get_workflow_script(
        workflow=workflow,
        workflow_run=workflow_run,
        status=ScriptStatus.pending,
    )
    await generate_workflow_script(
        workflow_run=workflow_run,
        workflow=workflow,
        script=script,
        rendered_cache_key_value=rendered_cache_key_value,
        pending=True,
    )


async def get_workflow_script(
    workflow: Workflow,
    workflow_run: WorkflowRun,
    block_labels: list[str] | None = None,
    status: ScriptStatus = ScriptStatus.published,
) -> tuple[Script | None, str]:
    """
    Check if there's a related workflow script that should be used instead of running the workflow.
    Returns the tuple of (script, rendered_cache_key_value).
    """
    cache_key = workflow.cache_key or ""
    rendered_cache_key_value = ""

    if block_labels:
        # Do not generate script or run script if block_labels is provided
        return None, rendered_cache_key_value

    try:
        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(
            workflow_run_id=workflow_run.workflow_run_id,
        )
        parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

        rendered_cache_key_value = jinja_sandbox_env.from_string(cache_key).render(parameters)

        # Check if there are existing cached scripts for this workflow + cache_key_value
        existing_scripts = await app.DATABASE.get_workflow_scripts_by_cache_key_value(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key_value=rendered_cache_key_value,
            statuses=[status],
        )

        if existing_scripts:
            LOG.info(
                "Found cached script for workflow",
                workflow_id=workflow.workflow_id,
                cache_key_value=rendered_cache_key_value,
                workflow_run_id=workflow_run.workflow_run_id,
                script_count=len(existing_scripts),
            )
            return existing_scripts[0], rendered_cache_key_value

        return None, rendered_cache_key_value

    except Exception as e:
        LOG.warning(
            "Failed to check for workflow script, proceeding with normal workflow execution",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        return None, rendered_cache_key_value


async def generate_workflow_script(
    workflow_run: WorkflowRun,
    workflow: Workflow,
    script: Script,
    rendered_cache_key_value: str,
    pending: bool = False,
) -> None:
    try:
        LOG.info(
            "Generating script for workflow",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.title,
            cache_key_value=rendered_cache_key_value,
        )
        codegen_input = await transform_workflow_run_to_code_gen_input(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow.organization_id,
        )
        python_src = await generate_workflow_script_python_code(
            file_name=codegen_input.file_name,
            workflow_run_request=codegen_input.workflow_run,
            workflow=codegen_input.workflow,
            blocks=codegen_input.workflow_blocks,
            actions_by_task=codegen_input.actions_by_task,
            task_v2_child_blocks=codegen_input.task_v2_child_blocks,
            organization_id=workflow.organization_id,
            script_id=script.script_id,
            script_revision_id=script.script_revision_id,
            pending=pending,
        )
    except Exception:
        LOG.error("Failed to generate workflow script source", exc_info=True)
        return

    # 4) Persist script and files, then record mapping
    content_bytes = python_src.encode("utf-8")
    content_b64 = base64.b64encode(content_bytes).decode("utf-8")
    files = [
        ScriptFileCreate(
            path="main.py",
            content=content_b64,
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        )
    ]

    # Upload script file(s) as artifacts and create rows
    await script_service.build_file_tree(
        files=files,
        organization_id=workflow.organization_id,
        script_id=script.script_id,
        script_version=script.version,
        script_revision_id=script.script_revision_id,
        pending=pending,
    )

    # check if an existing drfat workflow script exists for this workflow run
    existing_pending_workflow_script = None
    status = ScriptStatus.published
    if pending:
        status = ScriptStatus.pending
        existing_pending_workflow_script = await app.DATABASE.get_workflow_script(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            statuses=[status],
        )
    if not existing_pending_workflow_script:
        # Record the workflow->script mapping for cache lookup
        await app.DATABASE.create_workflow_script(
            organization_id=workflow.organization_id,
            script_id=script.script_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=rendered_cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            status=status,
        )
