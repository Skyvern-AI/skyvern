import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from datetime import datetime
from typing import Any, cast

import structlog
from fastapi import BackgroundTasks, HTTPException
from jinja2.sandbox import SandboxedEnvironment

from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.core.script_generations.script_run_context_manager import script_run_context_manager
from skyvern.exceptions import ScriptNotFound, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskOutput, TaskStatus
from skyvern.schemas.scripts import CreateScriptResponse, FileNode, ScriptFileCreate
from skyvern.schemas.workflows import BlockStatus, BlockType

LOG = structlog.get_logger(__name__)
jinja_sandbox_env = SandboxedEnvironment()


async def build_file_tree(
    files: list[ScriptFileCreate],
    organization_id: str,
    script_id: str,
    script_version: int,
    script_revision_id: str,
) -> dict[str, FileNode]:
    """Build a hierarchical file tree from a list of files and upload the files to s3 with the same tree structure."""
    file_tree: dict[str, FileNode] = {}

    for file in files:
        # Decode content to calculate size and hash
        content_bytes = base64.b64decode(file.content)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        file_size = len(content_bytes)

        # Create artifact and upload to S3
        try:
            artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=script_id,
                script_version=script_version,
                file_path=file.path,
                data=content_bytes,
            )
            LOG.debug(
                "Created script file artifact",
                artifact_id=artifact_id,
                file_path=file.path,
                script_id=script_id,
                script_version=script_version,
            )
            # create a script file record
            await app.DATABASE.create_script_file(
                script_revision_id=script_revision_id,
                script_id=script_id,
                organization_id=organization_id,
                file_path=file.path,
                file_name=file.path.split("/")[-1],
                file_type="file",
                content_hash=f"sha256:{content_hash}",
                file_size=file_size,
                mime_type=file.mime_type,
                artifact_id=artifact_id,
            )
        except Exception:
            LOG.exception(
                "Failed to create script file artifact",
                file_path=file.path,
                script_id=script_id,
                script_version=script_version,
                script_revision_id=script_revision_id,
            )
            raise

        # Split path into components
        path_parts = file.path.split("/")
        current_level = file_tree

        # Create directory structure
        for _, part in enumerate(path_parts[:-1]):
            if part not in current_level:
                current_level[part] = FileNode(type="directory", created_at=datetime.utcnow(), children={})
            elif current_level[part].type == "file":
                # Convert file to directory if needed
                current_level[part] = FileNode(type="directory", created_at=current_level[part].created_at, children={})

            current_level = current_level[part].children or {}

        # Add the file
        filename = path_parts[-1]
        current_level[filename] = FileNode(
            type="file",
            size=file_size,
            mime_type=file.mime_type,
            content_hash=f"sha256:{content_hash}",
            created_at=datetime.utcnow(),
        )

    return file_tree


async def create_script(
    organization_id: str,
    workflow_id: str | None = None,
    run_id: str | None = None,
    files: list[ScriptFileCreate] | None = None,
) -> CreateScriptResponse:
    LOG.info(
        "Creating script",
        organization_id=organization_id,
        file_count=len(files) if files else 0,
    )

    try:
        if run_id and not await app.DATABASE.get_run(run_id=run_id, organization_id=organization_id):
            raise HTTPException(status_code=404, detail=f"Run_id {run_id} not found")

        script = await app.DATABASE.create_script(
            organization_id=organization_id,
            run_id=run_id,
        )

        file_tree: dict[str, FileNode] = {}
        file_count = 0
        if files:
            file_tree = await build_file_tree(
                files,
                organization_id=organization_id,
                script_id=script.script_id,
                script_version=script.version,
                script_revision_id=script.script_revision_id,
            )
            file_count = len(files)

        return CreateScriptResponse(
            script_id=script.script_id,
            version=script.version,
            run_id=script.run_id,
            file_count=file_count,
            created_at=script.created_at,
            file_tree=file_tree,
        )
    except Exception as e:
        LOG.error("Failed to create script", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create script")


async def execute_script(
    script_id: str,
    organization_id: str,
    parameters: dict[str, Any] | None = None,
    workflow_run_id: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    # TODO: assume the script only has one ScriptFile called main.py
    # step 1: get the script revision
    # step 2: get the script files
    # step 3: copy the script files to the local directory
    # step 4: execute the script
    # step 5: TODO: close all the browser instances

    # step 1: get the script revision
    script = await app.DATABASE.get_script(
        script_id=script_id,
        organization_id=organization_id,
    )
    if not script:
        raise ScriptNotFound(script_id=script_id)

    # step 2: get the script files
    script_files = await app.DATABASE.get_script_files(
        script_revision_id=script.script_revision_id, organization_id=organization_id
    )

    # step 3: copy the script files to the local directory
    for file in script_files:
        # retrieve the artifact
        if not file.artifact_id:
            continue
        artifact = await app.DATABASE.get_artifact_by_id(file.artifact_id, organization_id)
        if not artifact:
            LOG.error("Artifact not found", artifact_id=file.artifact_id, script_id=script_id)
            continue
        file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if not file_content:
            continue
        file_path = os.path.join(script.script_id, file.file_path)
        # create the directory if it doesn't exist
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Determine the encoding to use
        encoding = "utf-8"

        try:
            # Try to decode as text
            if file.mime_type and file.mime_type.startswith("text/"):
                # Text file - decode as string
                with open(file_path, "w", encoding=encoding) as f:
                    f.write(file_content.decode(encoding))
            else:
                # Binary file - write as bytes
                with open(file_path, "wb") as f:
                    f.write(file_content)
        except UnicodeDecodeError:
            # Fallback to binary mode if text decoding fails
            with open(file_path, "wb") as f:
                f.write(file_content)

    # step 4: execute the script
    if workflow_run_id and not parameters:
        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}
        LOG.info("Script run Parameters is using workflow run parameters", parameters=parameters)

    if background_tasks:
        # Execute asynchronously in background
        background_tasks.add_task(
            run_script, parameters=parameters, organization_id=organization_id, workflow_run_id=workflow_run_id
        )
    else:
        # Execute synchronously
        script_path = os.path.join(script.script_id, "main.py")
        if os.path.exists(script_path):
            await run_script(
                script_path, parameters=parameters, organization_id=organization_id, workflow_run_id=workflow_run_id
            )
        else:
            LOG.error("Script main.py not found", script_path=script_path, script_id=script_id)
            raise Exception(f"Script main.py not found at {script_path}")

    LOG.info("Script executed successfully", script_id=script_id)


async def _create_workflow_block_run_and_task(
    block_type: BlockType,
    prompt: str | None = None,
    url: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Create a workflow block run and optionally a task if workflow_run_id is available in context.
    Returns (workflow_run_block_id, task_id) tuple.
    """
    context = skyvern_context.current()
    if not context or not context.workflow_run_id or not context.organization_id:
        return None, None
    workflow_run_id = context.workflow_run_id
    organization_id = context.organization_id

    try:
        # Create workflow run block with appropriate parameters based on block type
        # TODO: support engine in the future
        engine = None
        workflow_run_block = await app.DATABASE.create_workflow_run_block(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            block_type=block_type,
            engine=engine,
        )

        workflow_run_block_id = workflow_run_block.workflow_run_block_id
        task_id = None
        step_id = None

        # Create task for task-based blocks
        if block_type in SCRIPT_TASK_BLOCKS:
            # Create task
            task = await app.DATABASE.create_task(
                # fix HACK: changed the type of url to str | None to support None url. url is not used in the script right now.
                url=url or "",
                title=f"Script {block_type.value} task",
                navigation_goal=prompt,
                data_extraction_goal=prompt if block_type == BlockType.EXTRACTION else None,
                navigation_payload={},
                status="running",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
            )

            task_id = task.task_id

            # create a single step for the task
            step = await app.DATABASE.create_step(
                task_id=task_id,
                order=0,
                retry_index=0,
                organization_id=organization_id,
            )
            step_id = step.step_id

            # Update workflow run block with task_id
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                task_id=task_id,
                organization_id=organization_id,
            )

        context.step_id = step_id
        context.task_id = task_id

        return workflow_run_block_id, task_id

    except Exception as e:
        LOG.warning(
            "Failed to create workflow block run and task",
            error=str(e),
            block_type=block_type,
            workflow_run_id=context.workflow_run_id,
            exc_info=True,
        )
        return None, None


async def _record_output_parameter_value(
    workflow_run_id: str,
    workflow_id: str,
    organization_id: str,
    output: dict[str, Any] | list | str | None,
    label: str | None = None,
) -> None:
    if not label:
        return
    # TODO support this in the future
    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    # get the workflow
    workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    if not workflow:
        return

    # get the output_paramter
    output_parameter = workflow.get_output_parameter(label)
    if not output_parameter:
        return

    await workflow_run_context.register_output_parameter_value_post_execution(
        parameter=output_parameter,
        value=output,
    )
    await app.DATABASE.create_or_update_workflow_run_output_parameter(
        workflow_run_id=workflow_run_id,
        output_parameter_id=output_parameter.output_parameter_id,
        value=output,
    )


async def _update_workflow_block(
    workflow_run_block_id: str,
    status: BlockStatus,
    task_id: str | None = None,
    task_status: TaskStatus = TaskStatus.completed,
    label: str | None = None,
    failure_reason: str | None = None,
    output: dict[str, Any] | list | str | None = None,
) -> None:
    """Update the status of a workflow run block."""
    try:
        context = skyvern_context.current()
        if not context or not context.organization_id or not context.workflow_run_id or not context.workflow_id:
            return
        final_output = output
        if task_id:
            updated_task = await app.DATABASE.update_task(
                task_id=task_id,
                organization_id=context.organization_id,
                status=task_status,
                failure_reason=failure_reason,
                extracted_information=output,
            )
            downloaded_files: list[FileInfo] = []
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    downloaded_files = await app.STORAGE.get_downloaded_files(
                        organization_id=context.organization_id,
                        run_id=context.workflow_run_id,
                    )
            except asyncio.TimeoutError:
                LOG.warning("Timeout getting downloaded files", task_id=task_id)

            task_output = TaskOutput.from_task(updated_task, downloaded_files)
            final_output = task_output.model_dump()
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                organization_id=context.organization_id if context else None,
                status=status,
                failure_reason=failure_reason,
                output=final_output,
            )
        else:
            final_output = None
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                organization_id=context.organization_id if context else None,
                status=status,
                failure_reason=failure_reason,
            )
        await _record_output_parameter_value(
            context.workflow_run_id,
            context.workflow_id,
            context.organization_id,
            final_output,
            label,
        )

    except Exception as e:
        LOG.warning(
            "Failed to update workflow block status",
            workflow_run_block_id=workflow_run_block_id,
            status=status,
            error=str(e),
            exc_info=True,
        )


async def _run_cached_function(cache_key: str) -> Any:
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)
    if cached_fn:
        # TODO: handle exceptions here and fall back to AI run in case of error
        run_context = script_run_context_manager.ensure_run_context()
        return await cached_fn(page=run_context.page, context=run_context)
    else:
        raise Exception(f"Cache key {cache_key} not found")


async def run_task(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    cache_key: str | None = None,
) -> None:
    # Auto-create workflow block run and task if workflow_run_id is available
    workflow_run_block_id, task_id = await _create_workflow_block_run_and_task(
        block_type=BlockType.TASK,
        prompt=prompt,
        url=url,
    )
    # set the prompt in the RunContext
    run_context = script_run_context_manager.ensure_run_context()
    run_context.prompt = prompt

    if cache_key:
        try:
            await _run_cached_function(cache_key)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id, BlockStatus.completed, task_id=task_id, label=cache_key
                )

        except Exception as e:
            # TODO: fallback to AI run in case of error
            # Update block status to failed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    label=cache_key,
                    failure_reason=str(e),
                )
            raise
        finally:
            # clear the prompt in the RunContext
            run_context.prompt = None
    else:
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=task_id,
                task_status=TaskStatus.failed,
                failure_reason="Cache key is required",
            )
        run_context.prompt = None
        raise Exception("Cache key is required to run task block in a script")


async def download(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    cache_key: str | None = None,
) -> None:
    # Auto-create workflow block run and task if workflow_run_id is available
    workflow_run_block_id, task_id = await _create_workflow_block_run_and_task(
        block_type=BlockType.FILE_DOWNLOAD,
        prompt=prompt,
        url=url,
    )
    # set the prompt in the RunContext
    run_context = script_run_context_manager.ensure_run_context()
    run_context.prompt = prompt

    if cache_key:
        try:
            await _run_cached_function(cache_key)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id, BlockStatus.completed, task_id=task_id, label=cache_key
                )

        except Exception as e:
            # Update block status to failed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    label=cache_key,
                    failure_reason=str(e),
                )
            raise
        finally:
            run_context.prompt = None
    else:
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=task_id,
                task_status=TaskStatus.failed,
                failure_reason="Cache key is required",
            )
        run_context.prompt = None
        raise Exception("Cache key is required to run task block in a script")


async def action(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    cache_key: str | None = None,
) -> None:
    # Auto-create workflow block run and task if workflow_run_id is available
    workflow_run_block_id, task_id = await _create_workflow_block_run_and_task(
        block_type=BlockType.ACTION,
        prompt=prompt,
        url=url,
    )
    # set the prompt in the RunContext
    run_context = script_run_context_manager.ensure_run_context()
    run_context.prompt = prompt

    if cache_key:
        try:
            await _run_cached_function(cache_key)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id, BlockStatus.completed, task_id=task_id, label=cache_key
                )

        except Exception as e:
            # Update block status to failed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    label=cache_key,
                    failure_reason=str(e),
                )
            raise
        finally:
            run_context.prompt = None
    else:
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=task_id,
                task_status=TaskStatus.failed,
                failure_reason="Cache key is required",
            )
        run_context.prompt = None
        raise Exception("Cache key is required to run task block in a script")


async def login(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    cache_key: str | None = None,
) -> None:
    # Auto-create workflow block run and task if workflow_run_id is available
    workflow_run_block_id, task_id = await _create_workflow_block_run_and_task(
        block_type=BlockType.LOGIN,
        prompt=prompt,
        url=url,
    )
    # set the prompt in the RunContext
    run_context = script_run_context_manager.ensure_run_context()
    run_context.prompt = prompt

    if cache_key:
        try:
            await _run_cached_function(cache_key)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id, BlockStatus.completed, task_id=task_id, label=cache_key
                )

        except Exception as e:
            # Update block status to failed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    label=cache_key,
                    failure_reason=str(e),
                )
            raise
        finally:
            run_context.prompt = None
    else:
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=task_id,
                task_status=TaskStatus.failed,
                failure_reason="Cache key is required",
            )
        run_context.prompt = None
        raise Exception("Cache key is required to run task block in a script")


async def extract(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    cache_key: str | None = None,
) -> dict[str, Any] | list | str | None:
    # Auto-create workflow block run and task if workflow_run_id is available
    workflow_run_block_id, task_id = await _create_workflow_block_run_and_task(
        block_type=BlockType.EXTRACTION,
        prompt=prompt,
        url=url,
    )
    # set the prompt in the RunContext
    run_context = script_run_context_manager.ensure_run_context()
    run_context.prompt = prompt
    output: dict[str, Any] | list | str | None = None

    if cache_key:
        try:
            output = cast(dict[str, Any] | list | str | None, await _run_cached_function(cache_key))

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    output=output,
                    label=cache_key,
                )
            return output
        except Exception as e:
            # Update block status to failed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    failure_reason=str(e),
                    output=output,
                    label=cache_key,
                )
            raise
        finally:
            run_context.prompt = None
    else:
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=task_id,
                task_status=TaskStatus.failed,
                failure_reason="Cache key is required",
            )
        run_context.prompt = None
        raise Exception("Cache key is required to run task block in a script")


async def wait(seconds: int) -> None:
    # Auto-create workflow block run if workflow_run_id is available (wait block doesn't create tasks)
    workflow_run_block_id, _ = await _create_workflow_block_run_and_task(block_type=BlockType.WAIT)

    try:
        await asyncio.sleep(seconds)

        # Update block status to completed if workflow block was created
        if workflow_run_block_id:
            await _update_workflow_block(workflow_run_block_id, BlockStatus.completed)

    except Exception as e:
        # Update block status to failed if workflow block was created
        if workflow_run_block_id:
            await _update_workflow_block(workflow_run_block_id, BlockStatus.failed, failure_reason=str(e))
        raise


async def run_script(
    path: str,
    parameters: dict[str, Any] | None = None,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    # register the script run
    context = skyvern_context.current()
    if not context:
        context = skyvern_context.ensure_context()
        skyvern_context.set(skyvern_context.SkyvernContext())
    if workflow_run_id and organization_id:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id, organization_id=organization_id
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
        context.workflow_run_id = workflow_run_id
        context.organization_id = organization_id

    # run the script as subprocess; pass the parameters and run_id to the script
    # Dynamically import the script at the given path
    spec = importlib.util.spec_from_file_location("user_script", path)
    if not spec or not spec.loader:
        raise Exception(f"Failed to import script from {path}")
    user_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(user_script)

    # Call run_workflow from the imported module
    if hasattr(user_script, "run_workflow"):
        # If parameters is None, pass an empty dict
        if parameters:
            await user_script.run_workflow(parameters=parameters)
        else:
            await user_script.run_workflow()
    else:
        raise Exception(f"No 'run_workflow' function found in {path}")


async def generate_text(
    text: str | None = None,
    intention: str | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    if text:
        return text
    new_text = text or ""
    if intention and data:
        try:
            run_context = script_run_context_manager.ensure_run_context()
            prompt = run_context.prompt
            # Build the element tree of the current page for the prompt
            payload_str = json.dumps(data) if isinstance(data, (dict, list)) else (data or "")
            script_generation_input_text_prompt = prompt_engine.load_prompt(
                template="script-generation-input-text-generatiion",
                intention=intention,
                data=payload_str,
                goal=prompt,
            )
            json_response = await app.SINGLE_INPUT_AGENT_LLM_API_HANDLER(
                prompt=script_generation_input_text_prompt,
                prompt_name="script-generation-input-text-generatiion",
            )
            new_text = json_response.get("answer", new_text)
        except Exception:
            # If anything goes wrong, fall back to the original text
            pass
    return new_text


def render_template(template: str, data: dict[str, Any] | None = None) -> str:
    """
    Refer to  Block.format_block_parameter_template_from_workflow_run_context

    TODO: complete this function so that block code shares the same template rendering logic
    """
    template_data = data or {}
    jinja_template = jinja_sandbox_env.from_string(template)
    context = skyvern_context.current()
    if context and context.workflow_run_id:
        workflow_run_id = context.workflow_run_id
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        template_data.update(workflow_run_context.values)

    return jinja_template.render(template_data)
