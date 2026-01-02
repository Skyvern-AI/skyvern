import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Sequence, cast

import libcst as cst
import structlog
from fastapi import BackgroundTasks, HTTPException
from jinja2.sandbox import SandboxedEnvironment

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.core.script_generations.generate_script import _build_block_fn, create_or_update_script_block
from skyvern.core.script_generations.script_skyvern_page import script_run_context_manager
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import ScriptNotFound, ScriptTerminationException, StepTerminationError, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    CodeBlock,
    ExtractionBlock,
    FileDownloadBlock,
    FileParserBlock,
    FileUploadBlock,
    ForLoopBlock,
    HttpRequestBlock,
    LoginBlock,
    NavigationBlock,
    PDFParserBlock,
    SendEmailBlock,
    TaskBlock,
    TextPromptBlock,
    UrlBlock,
    ValidationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.scripts import (
    CreateScriptResponse,
    FileEncoding,
    FileNode,
    Script,
    ScriptFile,
    ScriptFileCreate,
    ScriptStatus,
)
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, FileStorageType, FileType
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.scraper.scraped_page import ElementTreeFormat

LOG = structlog.get_logger()
jinja_sandbox_env = SandboxedEnvironment()


class SkyvernLoopItem:
    def __init__(
        self,
        index: int,
        value: Any,
    ):
        self.current_index = index
        self.current_value = value
        self.current_item = value

    def __repr__(self) -> str:
        return f"SkyvernLoopItem(current_value={self.current_value}, current_index={self.current_index})"


async def build_file_tree(
    files: list[ScriptFileCreate],
    organization_id: str,
    script_id: str,
    script_version: int,
    script_revision_id: str,
    pending: bool = False,
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
            if pending:
                # get the script file object
                script_file = await app.DATABASE.get_script_file_by_path(
                    script_revision_id=script_revision_id,
                    file_path=file.path,
                    organization_id=organization_id,
                )
                if script_file:
                    if not script_file.artifact_id:
                        LOG.error(
                            "Failed to update file. An existing script file has no artifact id",
                            script_file_id=script_file.file_id,
                        )
                        continue
                    artifact = await app.DATABASE.get_artifact_by_id(script_file.artifact_id, organization_id)
                    if artifact:
                        # override the actual file in the storage
                        asyncio.create_task(app.STORAGE.store_artifact(artifact, content_bytes))
                    else:
                        artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                            organization_id=organization_id,
                            script_id=script_id,
                            script_version=script_version,
                            file_path=file.path,
                            data=content_bytes,
                        )
                        # update the artifact_id in the script file
                        await app.DATABASE.update_script_file(
                            script_file_id=script_file.file_id,
                            organization_id=organization_id,
                            artifact_id=artifact_id,
                        )
                else:
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
            else:
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


async def load_scripts(
    script: Script,
    script_files: list[ScriptFile],
) -> None:
    organization_id = script.organization_id
    for file in script_files:
        # retrieve the artifact
        if not file.artifact_id:
            continue
        artifact = await app.DATABASE.get_artifact_by_id(file.artifact_id, organization_id)
        if not artifact:
            LOG.error("Artifact not found", artifact_id=file.artifact_id, script_id=script.script_id)
            continue
        file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if not file_content:
            continue
        file_path = os.path.join(settings.TEMP_PATH, script.script_id, file.file_path)
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


async def execute_script(
    script_id: str,
    organization_id: str,
    parameters: dict[str, Any] | None = None,
    workflow_run_id: str | None = None,
    browser_session_id: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> None:
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
    await load_scripts(script, script_files)

    # step 4: execute the script
    if workflow_run_id and not parameters:
        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}
        LOG.info("Script run Parameters is using workflow run parameters", parameters=parameters)

    script_path = os.path.join(settings.TEMP_PATH, script.script_id, "main.py")

    if background_tasks:
        # Execute asynchronously in background
        background_tasks.add_task(
            run_script,
            script_path,
            parameters=parameters,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            browser_session_id=browser_session_id,
            script_id=script_id,
            script_revision_id=script.script_revision_id,
        )
    else:
        # Execute synchronously
        if os.path.exists(script_path):
            await run_script(
                script_path,
                parameters=parameters,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                browser_session_id=browser_session_id,
                script_id=script_id,
                script_revision_id=script.script_revision_id,
            )
        else:
            LOG.error("Script main.py not found", script_path=script_path, script_id=script_id)
            raise Exception(f"Script main.py not found at {script_path}")

    LOG.info("Script executed successfully", script_id=script_id)


async def _take_workflow_run_block_screenshot(
    workflow_run_id: str,
    organization_id: str,
    workflow_run_block: WorkflowRunBlock,
) -> None:
    """
    This function is a copy of the block screenshot logic from the execute_safe function in the block.py file.
    """
    browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
    if not browser_state:
        LOG.warning("No browser state found when creating workflow_run_block", workflow_run_id=workflow_run_id)
    else:
        screenshot = await browser_state.take_fullpage_screenshot()
        if screenshot:
            await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                workflow_run_block=workflow_run_block,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
                data=screenshot,
            )


async def _create_workflow_block_run_and_task(
    block_type: BlockType,
    prompt: str | None = None,
    schema: dict[str, Any] | list | str | None = None,
    url: str | None = None,
    label: str | None = None,
    model: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Create a workflow block run and optionally a task if workflow_run_id is available in context.
    Returns (workflow_run_block_id, task_id) tuple.
    """
    context = skyvern_context.current()
    if not context or not context.workflow_run_id or not context.organization_id:
        return None, None, None
    workflow_run_id = context.workflow_run_id
    organization_id = context.organization_id

    # if there's a parent_workflow_run_block_id and loop_metadata, update_block_metadata
    if context.parent_workflow_run_block_id and context.loop_metadata and label:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        workflow_run_context.update_block_metadata(label, context.loop_metadata)

    workflow_run_block = await app.DATABASE.create_workflow_run_block(
        workflow_run_id=workflow_run_id,
        parent_workflow_run_block_id=context.parent_workflow_run_block_id,
        organization_id=organization_id,
        block_type=block_type,
        label=label,
    )

    workflow_run_block_id = workflow_run_block.workflow_run_block_id

    try:
        # Create workflow run block with appropriate parameters based on block type
        # TODO: support engine in the future
        task_id = None
        step_id = None

        # Create task for task-based blocks
        if block_type in SCRIPT_TASK_BLOCKS:
            # Create task
            if prompt:
                prompt = _render_template_with_label(prompt, label)
            if url:
                url = _render_template_with_label(url, label)
            task = await app.DATABASE.create_task(
                # fix HACK: changed the type of url to str | None to support None url. url is not used in the script right now.
                url=url or "",
                title=f"Script {block_type.value} task",
                navigation_goal=prompt,
                data_extraction_goal=prompt if block_type == BlockType.EXTRACTION else None,
                extracted_information_schema=schema,
                navigation_payload=None,
                status="running",
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                model=model,
            )

            task_id = task.task_id

            # create a single step for the task
            step = await app.DATABASE.create_step(
                task_id=task_id,
                order=0,
                retry_index=0,
                organization_id=organization_id,
                status=StepStatus.running,
                created_by=created_by,
            )
            step_id = step.step_id
            # reset the action order to 0
            context.action_order = 0
            await _create_video_artifact(
                task=task,
                step=step,
            )

            # Update workflow run block with task_id
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                task_id=task_id,
                organization_id=organization_id,
            )

        await _take_workflow_run_block_screenshot(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_run_block=workflow_run_block,
        )

        context.step_id = step_id
        context.task_id = task_id

        return workflow_run_block_id, task_id, step_id

    except Exception as e:
        LOG.warning(
            "Failed to create workflow block run and task",
            error=str(e),
            block_type=block_type,
            workflow_run_id=context.workflow_run_id,
            exc_info=True,
        )
        return None, None, None


async def _create_video_artifact(
    task: Task,
    step: Step,
) -> None:
    workflow_run_id = task.workflow_run_id
    if not workflow_run_id:
        return None
    browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
    if not browser_state:
        return None
    if browser_state.browser_artifacts:
        video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
            task_id=task.task_id, browser_state=browser_state
        )
        for idx, video_artifact in enumerate(video_artifacts):
            if video_artifact.video_artifact_id:
                continue
            video_artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.RECORDING,
                data=video_artifact.video_data,
            )
            video_artifacts[idx].video_artifact_id = video_artifact_id
        app.BROWSER_MANAGER.set_video_artifact_for_task(task, video_artifacts)


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
        # NOT sure if this is legit hack to create output parameter like this
        label = label or f"block_{uuid.uuid4()}"
        output_parameter = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"{label}_output",
            workflow_id=workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
        )

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
    step_id: str | None = None,
    step_status: StepStatus = StepStatus.completed,
    is_last: bool | None = True,
    label: str | None = None,
    failure_reason: str | None = None,
    output: dict[str, Any] | list | str | None = None,
    ai_fallback_triggered: bool = False,
) -> None:
    """Update the status of a workflow run block."""
    try:
        context = skyvern_context.current()
        if not context or not context.organization_id or not context.workflow_run_id or not context.workflow_id:
            return
        final_output = output
        if task_id:
            if step_id:
                await app.DATABASE.update_step(
                    step_id=step_id,
                    task_id=task_id,
                    organization_id=context.organization_id,
                    status=step_status,
                    is_last=is_last,
                )
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

            task_screenshots = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_urls(
                organization_id=context.organization_id,
                task_id=task_id,
            )
            workflow_screenshots = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_urls(
                workflow_run_id=context.workflow_run_id,
                organization_id=context.organization_id,
            )

            task_output = TaskOutput.from_task(
                updated_task,
                downloaded_files,
                task_screenshots=task_screenshots,
                workflow_screenshots=workflow_screenshots,
            )
            final_output = task_output.model_dump()
            step_for_billing: Step | None = None
            if step_id:
                step_for_billing = await app.DATABASE.get_step(
                    step_id=step_id,
                    organization_id=context.organization_id,
                )
            if step_for_billing:
                try:
                    if not ai_fallback_triggered:
                        await app.AGENT_FUNCTION.post_cache_step_execution(
                            updated_task,
                            step_for_billing,
                        )
                except StepTerminationError as billing_error:
                    LOG.warning(
                        "Cached step billing failed; marking workflow block as failed.",
                        organization_id=context.organization_id,
                        task_id=task_id,
                        step_id=step_id,
                        error=str(billing_error),
                    )
                    status = BlockStatus.failed
                    failure_reason = str(billing_error)
                    final_output = None
        else:
            final_output = None

        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=context.organization_id if context else None,
            status=status,
            failure_reason=failure_reason,
            output=final_output,
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


async def _run_cached_function(cached_fn: Callable) -> Any:
    run_context = script_run_context_manager.ensure_run_context()
    return await cached_fn(page=run_context.page, context=run_context)


def _determine_action_ai_mode(
    action: Action,
    merged_value: str | None,
) -> str:
    """
    Decide whether to run an input/select action in proactive or fallback mode.
    """
    if action.has_mini_agent:
        return "proactive"
    # context = action.input_or_select_context
    # if isinstance(context, dict) and any(
    #     context.get(flag) for flag in ("is_location_input", "is_date_related", "date_format")
    # ):
    #     return "proactive"
    # if getattr(action, "totp_code_required", False):
    #     return "proactive"
    if action.totp_timing_info and action.totp_timing_info.get("is_totp_sequence"):
        return "proactive"
    if merged_value and str(merged_value).strip():
        return "fallback"
    return "proactive"


def _clear_cached_block_overrides(cache_key: str) -> None:
    context = skyvern_context.current()
    if not context:
        return
    context.action_ai_overrides.pop(cache_key, None)
    context.action_counters.pop(cache_key, None)


async def _prepare_cached_block_inputs(cache_key: str, prompt: str | None, step_id: str | None = None) -> None:
    """
    Fetch merged LLM inputs for a cached block and seed action-level AI overrides/parameters.
    """
    context = skyvern_context.current()
    if not context or not context.organization_id or not context.script_revision_id:
        return

    try:
        script_block = await app.DATABASE.get_script_block_by_label(
            organization_id=context.organization_id,
            script_revision_id=context.script_revision_id,
            script_block_label=cache_key,
        )
    except Exception:
        return

    input_fields: list[str] = []
    workflow_run_block_id = None
    if script_block:
        input_fields = script_block.input_fields or []
        workflow_run_block_id = script_block.workflow_run_block_id

    if not input_fields or not workflow_run_block_id:
        return

    try:
        source_block = await app.DATABASE.get_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=context.organization_id,
        )
    except Exception:
        return

    task_id = source_block.task_id
    if not task_id:
        return

    try:
        # actios are ordered by created_at
        actions = await app.DATABASE.get_task_actions_hydrated(task_id=task_id, organization_id=context.organization_id)
    except Exception:
        return

    input_actions = [action for action in actions if action.action_type in {ActionType.INPUT_TEXT}]
    # TODO: how to support select_option actions?
    # input_actions = [
    #     action for action in actions if action.action_type in {ActionType.INPUT_TEXT, ActionType.SELECT_OPTION}
    # ]

    if not input_actions:
        return

    # Map actions to field names using stored field_name when present; otherwise consume in order from input_fields.
    field_iter = iter(input_fields)
    action_entries: list[tuple[Action, str | None]] = []
    for action in input_actions:
        field_name = None
        try:
            field_name = next(field_iter, None)
        except StopIteration:
            field_name = None
        action_entries.append((action, field_name))

    merged_values: dict[str, Any] = {}
    run_context = script_run_context_manager.get_run_context()
    if not run_context:
        return

    try:
        parameters = {key: str(value) for key, value in run_context.parameters.items() if value}
        serialized_params = json.dumps(parameters)
        field_prompts = []
        for action, field_name in action_entries:
            if not field_name:
                continue
            prompt_text = action.intention or action.reasoning or ""
            if action.input_or_select_context and action.input_or_select_context.intention:
                prompt_text = action.input_or_select_context.intention
            field_prompts.append({"name": field_name, "prompt": prompt_text})

        if field_prompts:
            merged_prompt = (
                "You are helping fill web form fields for a workflow block.\n"
                f"Block prompt/context:\n{prompt or ''}\n\n"
                f"Workflow parameters (as JSON):\n{serialized_params}\n\n"
                "Return a JSON object mapping field_name -> value for the following fields.\n"
                "Leave value empty string if it cannot be determined.\n"
                f"Fields:\n{json.dumps(field_prompts)}"
            )
            step = None
            if step_id:
                step = await app.DATABASE.get_step(step_id=step_id, organization_id=context.organization_id)
            llm_response = await app.SCRIPT_GENERATION_LLM_API_HANDLER(
                prompt=merged_prompt,
                prompt_name="merged-block-inputs",
                step=step,
            )
            if isinstance(llm_response, dict):
                merged_values = llm_response
            elif isinstance(llm_response, str):
                try:
                    merged_values = json.loads(llm_response)
                except Exception:
                    merged_values = {}
            else:
                merged_values = {}
    except Exception:
        merged_values = {}

    overrides: dict[int, str] = {}
    for idx, (action, field_name) in enumerate(action_entries, start=1):
        merged_value = merged_values.get(field_name, "") if field_name else ""
        ai_mode = _determine_action_ai_mode(action, merged_value)
        overrides[idx] = ai_mode

        if ai_mode == "fallback" and field_name and isinstance(merged_value, str):
            # Seed the run context parameters with merged values for cached execution.
            run_context.parameters[field_name] = merged_value

    # if overrides:
    #     context.action_ai_overrides[cache_key] = overrides
    #     context.action_counters[cache_key] = 0


async def _detect_user_defined_errors(
    task: Task,
    step: Step,
    workflow_run_id: str,
    error_code_mapping: dict[str, str],
    prompt: str | None = None,
) -> list[UserDefinedError]:
    """
    Detect user-defined errors using LLM when error_code_mapping is provided.
    Returns a list of UserDefinedError objects if any errors are detected.
    """
    try:
        run_context = script_run_context_manager.ensure_run_context()
        skyvern_page = run_context.page
        scraped_page = await skyvern_page.scraped_page.refresh()
        skyvern_page.scraped_page = scraped_page
        current_url = scraped_page.url

        # Build element tree
        element_tree_format = ElementTreeFormat.HTML
        elements = scraped_page.build_element_tree(element_tree_format)

        screenshots = scraped_page.screenshots

        # Build the prompt using the surface-user-defined-errors template
        context = skyvern_context.current()
        tz_info = datetime.now().astimezone().tzinfo
        if context and context.tz_info:
            tz_info = context.tz_info
        prompt_name = "surface-user-defined-errors"
        error_detection_prompt = prompt_engine.load_prompt(
            prompt_name,
            error_code_mapping_str=json.dumps(error_code_mapping),
            navigation_goal=prompt or task.navigation_goal or "",
            navigation_payload_str=json.dumps(task.navigation_payload or {}),
            elements=elements,
            current_url=current_url,
            action_history=[],
            local_datetime=datetime.now(tz_info).isoformat(),
            reasoning=None,
        )

        # Call LLM to detect errors
        json_response = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=error_detection_prompt,
            screenshots=screenshots,
            step=step,
            prompt_name=prompt_name,
        )

        # Parse the response and extract errors
        errors_list = json_response.get("errors", [])
        user_defined_errors = []

        for error_dict in errors_list:
            try:
                user_defined_error = UserDefinedError.model_validate(error_dict)
                user_defined_errors.append(user_defined_error)
            except Exception:
                LOG.warning(
                    "Failed to validate user-defined error",
                    error_dict=error_dict,
                )

        LOG.info(
            "Detected user-defined errors",
            task_id=task.task_id,
            step_id=step.step_id,
            error_count=len(user_defined_errors),
            errors=[e.error_code for e in user_defined_errors],
        )

        return user_defined_errors

    except Exception as e:
        LOG.exception(
            "Failed to detect user-defined errors",
            task_id=task.task_id,
            step_id=step.step_id,
            error=str(e),
        )
        return []


async def _fallback_to_ai_run(
    block_type: BlockType,
    cache_key: str,
    prompt: str | None = None,
    url: str | None = None,
    engine: RunEngine = RunEngine.skyvern_v1,
    complete_criterion: str | None = None,
    terminate_criterion: str | None = None,
    data_extraction_goal: str | None = None,
    schema: dict[str, Any] | list | str | None = None,
    error_code_mapping: dict[str, str] | None = None,
    max_steps: int | None = None,
    complete_on_download: bool = False,
    download_suffix: str | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    complete_verification: bool = True,
    include_action_history_in_verification: bool = False,
    error: Exception | None = None,
    workflow_run_block_id: str | None = None,
) -> None:
    context = skyvern_context.current()
    if not (
        context
        and context.organization_id
        and context.workflow_run_id
        and context.workflow_id
        and context.task_id
        and context.step_id
    ):
        return
    organization_id = context.organization_id
    workflow_id = context.workflow_id
    workflow_run_id = context.workflow_run_id
    workflow_permanent_id = context.workflow_permanent_id
    task_id = context.task_id
    script_step_id = context.step_id
    try:
        LOG.info(
            "Script trying to fallback to AI run",
            cache_key=cache_key,
            organization_id=organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            step_id=script_step_id,
        )
        # 1. fail the previous step
        previous_step = await app.DATABASE.update_step(
            step_id=script_step_id,
            task_id=task_id,
            organization_id=organization_id,
            status=StepStatus.failed,
        )
        # 2. run execute_step
        organization = await app.DATABASE.get_organization(organization_id=organization_id)
        if not organization:
            raise Exception(f"Organization is missing organization_id={organization_id}")
        task = await app.DATABASE.get_task(task_id=context.task_id, organization_id=organization_id)
        if not task:
            raise Exception(f"Task is missing task_id={context.task_id}")
        workflow = await app.DATABASE.get_workflow(workflow_id=context.workflow_id, organization_id=organization_id)
        if not workflow:
            return
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id, organization_id=organization_id
        )
        if not workflow_run:
            return
        # Use workflow_run.ai_fallback if explicitly set, otherwise fall back to workflow.ai_fallback
        effective_ai_fallback = (
            workflow_run.ai_fallback if workflow_run.ai_fallback is not None else workflow.ai_fallback
        )
        if not effective_ai_fallback:
            LOG.info(
                "AI fallback is not enabled for the workflow",
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
            )

            # If error_code_mapping is provided, detect user-defined errors using LLM
            detected_errors: list[UserDefinedError] = []
            if error_code_mapping:
                LOG.info(
                    "Error code mapping provided, detecting user-defined errors",
                    workflow_run_id=workflow_run_id,
                    task_id=task_id,
                )
                detected_errors = await _detect_user_defined_errors(
                    task=task,
                    step=previous_step,
                    workflow_run_id=workflow_run_id,
                    error_code_mapping=error_code_mapping,
                    prompt=prompt,
                )

                # Update task errors if any errors were detected
                if detected_errors:
                    task_errors = task.errors or []
                    task_errors.extend([error.model_dump() for error in detected_errors])
                    await app.DATABASE.update_task(
                        task_id=task_id,
                        organization_id=organization_id,
                        errors=task_errors,
                    )
                    LOG.info(
                        "Updated task with detected user-defined errors",
                        task_id=task_id,
                        error_codes=[e.error_code for e in detected_errors],
                    )

            # Update workflow block with failure reason (include detected errors if any)
            task_failure_reason = str(error)
            if detected_errors:
                error_codes = [e.error_code for e in detected_errors]
                task_failure_reason = f"{task_failure_reason}. Detected errors: {', '.join(error_codes)}"

            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.failed,
                    task_id=task_id,
                    task_status=TaskStatus.failed,
                    failure_reason=task_failure_reason,
                    step_id=script_step_id,
                    step_status=StepStatus.failed,
                    label=cache_key,
                )
            return

        # 2. create a new step for ai run
        ai_step = await app.DATABASE.create_step(
            task_id=task_id,
            organization_id=organization_id,
            order=previous_step.order + 1,
            retry_index=0,
        )
        context.step_id = ai_step.step_id
        ai_step_id = ai_step.step_id

        # get the output_paramter
        output_parameter = workflow.get_output_parameter(cache_key)
        if not output_parameter:
            # NOT sure if this is legit hack to create output parameter like this
            output_parameter = OutputParameter(
                output_parameter_id=str(uuid.uuid4()),
                key=f"{cache_key}_output",
                workflow_id=workflow_id,
                created_at=datetime.now(),
                modified_at=datetime.now(),
                parameter_type=ParameterType.OUTPUT,
            )
        LOG.info(
            "Script starting to fallback to AI run",
            cache_key=cache_key,
            organization_id=organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            step_id=script_step_id,
        )

        task_block = TaskBlock(
            label=cache_key,
            url=task.url,
            navigation_goal=prompt,
            output_parameter=output_parameter,
            title=cache_key,
            engine=engine,
            complete_criterion=complete_criterion,
            terminate_criterion=terminate_criterion,
            data_extraction_goal=data_extraction_goal,
            data_schema=schema,
            error_code_mapping=error_code_mapping,
            max_steps_per_run=max_steps,
            complete_on_download=complete_on_download,
            download_suffix=download_suffix,
            totp_verification_url=totp_url,
            totp_identifier=totp_identifier,
            complete_verification=complete_verification,
            include_action_history_in_verification=include_action_history_in_verification,
        )
        await app.agent.execute_step(
            organization=organization,
            task=task,
            step=ai_step,
            task_block=task_block,
        )

        # update workflow run to indicate that there's a script run
        if workflow_run_id:
            await app.DATABASE.update_workflow_run(
                workflow_run_id=workflow_run_id,
                ai_fallback_triggered=True,
            )

        # Update block status to completed if workflow block was created
        if workflow_run_block_id:
            # refresh the task
            failure_reason = None
            refreshed_task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
            if refreshed_task:
                task = refreshed_task
            if task.status in [TaskStatus.terminated, TaskStatus.failed]:
                failure_reason = task.failure_reason
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus(task.status.value),
                failure_reason=failure_reason,
                label=cache_key,
                ai_fallback_triggered=True,
            )

        # 5. After successful AI execution, regenerate the script block and create new version
        try:
            await _regenerate_script_block_after_ai_fallback(
                block_type=block_type,
                cache_key=cache_key,
                task_id=context.task_id,
                script_step_id=ai_step_id,
                ai_step_id=ai_step_id,
                organization_id=organization_id,
                workflow=workflow,
                workflow_run_id=context.workflow_run_id,
                prompt=prompt,
                url=url,
                engine=engine,
                complete_criterion=complete_criterion,
                terminate_criterion=terminate_criterion,
                data_extraction_goal=data_extraction_goal,
                schema=schema,
                error_code_mapping=error_code_mapping,
                max_steps=max_steps,
                complete_on_download=complete_on_download,
                download_suffix=download_suffix,
                totp_verification_url=totp_url,
                totp_identifier=totp_identifier,
                complete_verification=complete_verification,
                include_action_history_in_verification=include_action_history_in_verification,
            )
        except Exception as e:
            LOG.warning("Failed to regenerate script block after AI fallback", error=str(e), exc_info=True)
            # Don't fail the entire fallback process if script regeneration fails
    except Exception as e:
        LOG.warning("Failed to fallback to AI run", cache_key=cache_key, exc_info=True)
        # Update block status to failed if workflow block was created
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                task_id=context.task_id,
                task_status=TaskStatus.failed,
                label=cache_key,
                failure_reason=str(e),
            )
        raise e


async def _regenerate_script_block_after_ai_fallback(
    block_type: BlockType,
    cache_key: str,
    task_id: str,
    script_step_id: str,
    ai_step_id: str,
    organization_id: str,
    workflow: Workflow,
    workflow_run_id: str,
    prompt: str | None = None,
    url: str | None = None,
    engine: RunEngine = RunEngine.skyvern_v1,
    complete_criterion: str | None = None,
    terminate_criterion: str | None = None,
    data_extraction_goal: str | None = None,
    schema: dict[str, Any] | list | str | None = None,
    error_code_mapping: dict[str, str] | None = None,
    max_steps: int | None = None,
    complete_on_download: bool = False,
    download_suffix: str | None = None,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
    complete_verification: bool = True,
    include_action_history_in_verification: bool = False,
) -> None:
    """
    Regenerate the script block after a successful AI fallback and create a new script version.
    Only the specific block that fell back to AI is regenerated; all other blocks remain unchanged.

    1. get the latest cashed script for the workflow
    2. create a completely new script, with only the current block's script being different as it's newly generated.
      -
    """
    LOG.info("skipping script regeneration after AI fallback")
    return None
    try:
        # Get the current script for this workflow and cache key value
        # Render the cache_key_value from workflow run parameters (same logic as generate_script_for_workflow)
        cache_key_value = ""
        if workflow.cache_key:
            try:
                parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
                parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}
                cache_key_value = jinja_sandbox_env.from_string(workflow.cache_key).render(parameters)
            except Exception as e:
                LOG.warning("Failed to render cache key for script regeneration", error=str(e), exc_info=True)
                # Fallback to using cache_key as cache_key_value
                cache_key_value = cache_key

        if not cache_key_value:
            cache_key_value = cache_key  # Fallback

        existing_script = await app.DATABASE.get_workflow_script_by_cache_key_value(
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key_value=cache_key_value,
            cache_key=workflow.cache_key,
            statuses=[ScriptStatus.published],
        )

        if not existing_script:
            LOG.error("No existing script found to regenerate", cache_key=cache_key, cache_key_value=cache_key_value)
            return

        current_script = existing_script
        LOG.info(
            "Regenerating script block after AI fallback",
            script_id=current_script.script_id,
            script_version=current_script.version,
            cache_key=cache_key,
            cache_key_value=cache_key_value,
        )

        # Create a new script version
        new_script = await app.DATABASE.create_script(
            organization_id=organization_id,
            run_id=workflow_run_id,
            script_id=current_script.script_id,  # Use same script_id for versioning
            version=current_script.version + 1,
        )

        # deprecate the current workflow script
        await app.DATABASE.delete_workflow_cache_key_value(
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key_value=cache_key_value,
        )

        # Create workflow script mapping for the new version
        await app.DATABASE.create_workflow_script(
            organization_id=organization_id,
            script_id=new_script.script_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run_id,
        )

        # Get all existing script blocks from the previous version
        existing_script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
            script_revision_id=current_script.script_revision_id,
            organization_id=organization_id,
        )

        # Copy all existing script blocks to the new version (except the one we're regenerating)
        block_file_contents = []
        starter_block_file_content_bytes = b""
        block_file_content: bytes | str = ""
        for existing_block in existing_script_blocks:
            if existing_block.script_block_label == cache_key:
                # Skip this block - we'll regenerate it
                block_file_content = await _generate_block_code_from_task(
                    block_type=block_type,
                    cache_key=cache_key,
                    task_id=task_id,
                    script_step_id=script_step_id,
                    ai_step_id=ai_step_id,
                    organization_id=organization_id,
                    workflow=workflow,
                    workflow_run_id=workflow_run_id,
                )
            else:
                # Copy the existing block to the new version
                # Get the script file content for this block and copy a new script block for it
                if existing_block.script_file_id:
                    script_file = await app.DATABASE.get_script_file_by_id(
                        script_revision_id=current_script.script_revision_id,
                        file_id=existing_block.script_file_id,
                        organization_id=organization_id,
                    )

                    if script_file and script_file.artifact_id:
                        # Retrieve the artifact content
                        artifact = await app.DATABASE.get_artifact_by_id(script_file.artifact_id, organization_id)
                        if artifact:
                            file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                            if file_content:
                                block_file_content = file_content
                            else:
                                LOG.warning(
                                    "Failed to retrieve artifact content for existing block",
                                    block_label=existing_block.script_block_label,
                                )
                        else:
                            LOG.warning(
                                "Artifact not found for existing block", block_label=existing_block.script_block_label
                            )
                    else:
                        LOG.warning(
                            "Script file or artifact not found for existing block",
                            block_label=existing_block.script_block_label,
                        )
                else:
                    LOG.warning("No script file ID for existing block", block_label=existing_block.script_block_label)

            if not block_file_content:
                LOG.warning(
                    "No block file content found for existing block", block_label=existing_block.script_block_label
                )
                continue

            await create_or_update_script_block(
                block_code=block_file_content,
                script_revision_id=new_script.script_revision_id,
                script_id=new_script.script_id,
                organization_id=organization_id,
                block_label=existing_block.script_block_label,
                workflow_run_id=existing_block.workflow_run_id,
                workflow_run_block_id=existing_block.workflow_run_block_id,
                input_fields=existing_block.input_fields,
            )
            block_file_content_bytes = (
                block_file_content if isinstance(block_file_content, bytes) else block_file_content.encode("utf-8")
            )
            if existing_block.script_block_label == settings.WORKFLOW_START_BLOCK_LABEL:
                starter_block_file_content_bytes = block_file_content_bytes
            else:
                block_file_contents.append(block_file_content_bytes)

        if starter_block_file_content_bytes:
            block_file_contents.insert(0, starter_block_file_content_bytes)
        else:
            LOG.error("Starter block file content not found")

        # 4) Persist script and files, then record mapping
        python_src = "\n\n".join([block_file_content.decode("utf-8") for block_file_content in block_file_contents])
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
        await build_file_tree(
            files=files,
            organization_id=workflow.organization_id,
            script_id=new_script.script_id,
            script_version=new_script.version,
            script_revision_id=new_script.script_revision_id,
        )

    except Exception as e:
        LOG.error("Failed to regenerate script block after AI fallback", error=str(e), exc_info=True)
        raise


async def _get_block_definition_by_label(
    label: str, workflow: Workflow, task_id: str, organization_id: str
) -> dict[str, Any] | None:
    final_dump = None
    for block in workflow.workflow_definition.blocks:
        if block.label == label:
            final_dump = block.model_dump()
            break
    if not final_dump:
        return None

    task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
    if task:
        task_dump = task.model_dump()
        final_dump.update({k: v for k, v in task_dump.items() if k not in final_dump})

        # Add run block execution metadata
        final_dump.update(
            {
                "task_id": task_id,
                "output": task.extracted_information,
            }
        )

    return final_dump


async def _generate_block_code_from_task(
    block_type: BlockType,
    cache_key: str,
    task_id: str,
    script_step_id: str,
    ai_step_id: str,
    organization_id: str,
    workflow: Workflow,
    workflow_run_id: str,
) -> str:
    block_data = await _get_block_definition_by_label(cache_key, workflow, task_id, organization_id)
    if not block_data:
        return ""
    try:
        # Now regenerate only the specific block that fell back to AI
        task_actions = await app.DATABASE.get_task_actions_hydrated(
            task_id=task_id,
            organization_id=organization_id,
        )

        # Filter actions by step_id and exclude the final action that failed before ai fallback
        actions_to_cache = []
        for index, task_action in enumerate(task_actions):
            # if this action is the last action of the script step, right before ai fallback, we should not include it
            if (
                index < len(task_actions) - 1
                and task_action.step_id == script_step_id
                and task_actions[index + 1].step_id == ai_step_id
            ):
                continue
            action_dump = task_action.model_dump()
            action_dump["xpath"] = task_action.get_xpath()
            is_data_extraction_goal = "data_extraction_goal" in block_data and "data_extraction_goal" in action_dump
            if is_data_extraction_goal:
                # use the raw data extraction goal which is potentially a template
                action_dump["data_extraction_goal"] = block_data["data_extraction_goal"]
            actions_to_cache.append(action_dump)

        if not actions_to_cache:
            LOG.warning("No actions found in successful step for script block regeneration")
            return ""

        # Generate the new block function
        block_fn_def = _build_block_fn(block_data, actions_to_cache)

        # Convert the FunctionDef to code using a temporary module
        temp_module = cst.Module(body=[block_fn_def])
        block_code = temp_module.code

        return block_code

    except Exception as block_gen_error:
        LOG.error("Failed to generate block function", error=str(block_gen_error), exc_info=True)
        # Even if block generation fails, we've created the new script version
        # which can be useful for debugging
        return ""


async def run_task(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    download_suffix: str | None = None,
    totp_identifier: str | None = None,
    totp_url: str | None = None,
    label: str | None = None,
    cache_key: str | None = None,
    engine: RunEngine = RunEngine.skyvern_v1,
    model: dict[str, Any] | None = None,
    error_code_mapping: dict[str, str] | None = None,
) -> dict[str, Any] | list | str | None:
    cache_key = cache_key or label
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)

    context: skyvern_context.SkyvernContext | None = None
    if cache_key and cached_fn:
        # Auto-create workflow block run and task if workflow_run_id is available
        workflow_run_block_id, task_id, step_id = await _create_workflow_block_run_and_task(
            block_type=BlockType.NAVIGATION,
            prompt=prompt,
            url=url,
            label=cache_key,
            model=model,
            created_by="script",
        )
        prompt = _render_template_with_label(prompt, cache_key)
        # set the prompt in the RunContext
        context = skyvern_context.ensure_context()
        context.prompt = prompt
        try:
            await _prepare_cached_block_inputs(cache_key, prompt)
            output = await _run_cached_function(cached_fn)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    output=output,
                    step_id=step_id,
                    label=cache_key,
                )
            return output

        except Exception as e:
            LOG.exception("Failed to run task block. Falling back to AI run.")
            await _fallback_to_ai_run(
                block_type=BlockType.NAVIGATION,
                cache_key=cache_key,
                prompt=prompt,
                url=url,
                max_steps=max_steps,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                error=e,
                workflow_run_block_id=workflow_run_block_id,
                error_code_mapping=error_code_mapping,
            )
            return None
        finally:
            # clear the prompt in the RunContext
            context.prompt = None
            _clear_cached_block_overrides(cache_key)
    else:
        block_validation_output = await _validate_and_get_output_parameter(label)
        task_block = NavigationBlock(
            label=block_validation_output.label,
            output_parameter=block_validation_output.output_parameter,
            url=url,
            navigation_goal=prompt,
            max_steps_per_run=max_steps,
            totp_identifier=totp_identifier,
            totp_verification_url=totp_url,
            include_action_history_in_verification=True,
            engine=RunEngine.skyvern_v1,
            model=model,
        )
        block_output = await task_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )
        return block_output.output_parameter_value


async def download(
    prompt: str,
    url: str | None = None,
    complete_on_download: bool = True,
    download_suffix: str | None = None,
    max_steps: int | None = None,
    totp_identifier: str | None = None,
    totp_url: str | None = None,
    label: str | None = None,
    cache_key: str | None = None,
    model: dict[str, Any] | None = None,
    error_code_mapping: dict[str, str] | None = None,
) -> None:
    cache_key = cache_key or label
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)
    context: skyvern_context.SkyvernContext | None
    if cache_key and cached_fn:
        # Auto-create workflow block run and task if workflow_run_id is available
        workflow_run_block_id, task_id, step_id = await _create_workflow_block_run_and_task(
            block_type=BlockType.FILE_DOWNLOAD,
            prompt=prompt,
            url=url,
            label=cache_key,
            model=model,
            created_by="script",
        )
        prompt = _render_template_with_label(prompt, cache_key)
        # set the prompt in the RunContext
        context = skyvern_context.ensure_context()
        context.prompt = prompt

        try:
            await _prepare_cached_block_inputs(cache_key, prompt)
            await _run_cached_function(cached_fn)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    step_id=step_id,
                    label=cache_key,
                )

        except Exception as e:
            LOG.exception("Failed to run download block. Falling back to AI run.")
            await _fallback_to_ai_run(
                block_type=BlockType.FILE_DOWNLOAD,
                cache_key=cache_key,
                prompt=prompt,
                url=url,
                max_steps=max_steps,
                complete_on_download=complete_on_download,
                error=e,
                workflow_run_block_id=workflow_run_block_id,
                error_code_mapping=error_code_mapping,
            )
        finally:
            context.prompt = None
            _clear_cached_block_overrides(cache_key)
    else:
        block_validation_output = await _validate_and_get_output_parameter(label)
        file_download_block = FileDownloadBlock(
            label=block_validation_output.label,
            output_parameter=block_validation_output.output_parameter,
            url=url,
            complete_on_download=complete_on_download,
            navigation_goal=prompt,
            max_steps_per_run=max_steps,
            totp_identifier=totp_identifier,
            totp_verification_url=totp_url,
            include_action_history_in_verification=True,
            engine=RunEngine.skyvern_v1,
            model=model,
        )
        await file_download_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )


async def action(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    download_suffix: str | None = None,
    totp_identifier: str | None = None,
    totp_url: str | None = None,
    label: str | None = None,
    cache_key: str | None = None,
    model: dict[str, Any] | None = None,
    error_code_mapping: dict[str, str] | None = None,
) -> None:
    context: skyvern_context.SkyvernContext | None
    cache_key = cache_key or label
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)
    if cache_key and cached_fn:
        # Auto-create workflow block run and task if workflow_run_id is available
        workflow_run_block_id, task_id, step_id = await _create_workflow_block_run_and_task(
            block_type=BlockType.ACTION,
            prompt=prompt,
            url=url,
            label=cache_key,
            model=model,
            created_by="script",
        )
        prompt = _render_template_with_label(prompt, cache_key)
        # set the prompt in the RunContext
        context = skyvern_context.ensure_context()
        context.prompt = prompt

        try:
            await _prepare_cached_block_inputs(cache_key, prompt)
            await _run_cached_function(cached_fn)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    step_id=step_id,
                    label=cache_key,
                )

        except Exception as e:
            LOG.exception("Failed to run action block. Falling back to AI run.")
            await _fallback_to_ai_run(
                block_type=BlockType.ACTION,
                cache_key=cache_key,
                prompt=prompt,
                url=url,
                max_steps=max_steps,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                error=e,
                workflow_run_block_id=workflow_run_block_id,
                error_code_mapping=error_code_mapping,
            )
        finally:
            context.prompt = None
            _clear_cached_block_overrides(cache_key)
    else:
        block_validation_output = await _validate_and_get_output_parameter(label)
        action_block = ActionBlock(
            label=block_validation_output.label,
            output_parameter=block_validation_output.output_parameter,
            task_type=TaskType.action,
            url=url,
            navigation_goal=prompt,
            max_steps_per_run=max_steps,
            totp_identifier=totp_identifier,
            totp_verification_url=totp_url,
            model=model,
        )
        await action_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )


async def login(
    prompt: str,
    url: str | None = None,
    max_steps: int | None = None,
    totp_identifier: str | None = None,
    totp_url: str | None = None,
    label: str | None = None,
    cache_key: str | None = None,
    model: dict[str, Any] | None = None,
    error_code_mapping: dict[str, str] | None = None,
) -> None:
    context: skyvern_context.SkyvernContext | None
    cache_key = cache_key or label
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)
    if cache_key and cached_fn:
        # Auto-create workflow block run and task if workflow_run_id is available
        # render template with label
        workflow_run_block_id, task_id, step_id = await _create_workflow_block_run_and_task(
            block_type=BlockType.LOGIN,
            prompt=prompt,
            url=url,
            label=cache_key,
            model=model,
            created_by="script",
        )
        prompt = _render_template_with_label(prompt, cache_key)
        if totp_url:
            totp_url = _render_template_with_label(totp_url, cache_key)
        if totp_identifier:
            totp_identifier = _render_template_with_label(totp_identifier, cache_key)

        # set the prompt in the RunContext
        context = skyvern_context.ensure_context()
        context.prompt = prompt
        try:
            await _prepare_cached_block_inputs(cache_key, prompt)
            await _run_cached_function(cached_fn)

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    step_id=step_id,
                    label=cache_key,
                )

        except Exception as e:
            LOG.exception("Failed to run login block")
            await _fallback_to_ai_run(
                block_type=BlockType.LOGIN,
                cache_key=cache_key,
                prompt=prompt,
                url=url,
                max_steps=max_steps,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                error=e,
                workflow_run_block_id=workflow_run_block_id,
                error_code_mapping=error_code_mapping,
            )
        finally:
            context.prompt = None
            _clear_cached_block_overrides(cache_key)
    else:
        block_validation_output = await _validate_and_get_output_parameter(label)
        login_block = LoginBlock(
            label=block_validation_output.label,
            output_parameter=block_validation_output.output_parameter,
            url=url,
            navigation_goal=prompt,
            max_steps_per_run=max_steps,
            totp_identifier=totp_identifier,
            totp_verification_url=totp_url,
            model=model,
        )
        await login_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )


async def extract(
    prompt: str,
    schema: dict[str, Any] | list | str | None = None,
    url: str | None = None,
    max_steps: int | None = None,
    label: str | None = None,
    cache_key: str | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, Any] | list | str | None:
    output: dict[str, Any] | list | str | None = None

    context: skyvern_context.SkyvernContext | None
    cache_key = cache_key or label
    cached_fn = script_run_context_manager.get_cached_fn(cache_key)
    if cache_key and cached_fn:
        # Auto-create workflow block run and task if workflow_run_id is available
        workflow_run_block_id, task_id, step_id = await _create_workflow_block_run_and_task(
            block_type=BlockType.EXTRACTION,
            prompt=prompt,
            schema=schema,
            url=url,
            label=cache_key,
            model=model,
            created_by="script",
        )
        prompt = _render_template_with_label(prompt, cache_key)
        # set the prompt in the RunContext
        context = skyvern_context.ensure_context()
        context.prompt = prompt
        try:
            output = cast(dict[str, Any] | list | str | None, await _run_cached_function(cached_fn))

            # Update block status to completed if workflow block was created
            if workflow_run_block_id:
                await _update_workflow_block(
                    workflow_run_block_id,
                    BlockStatus.completed,
                    task_id=task_id,
                    step_id=step_id,
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
                    step_id=step_id,
                    step_status=StepStatus.failed,
                    failure_reason=str(e),
                    output=output,
                    label=cache_key,
                )
            raise
        finally:
            context.prompt = None
    else:
        block_validation_output = await _validate_and_get_output_parameter(label)
        extraction_block = ExtractionBlock(
            label=block_validation_output.label,
            url=url,
            data_extraction_goal=prompt,
            max_steps_per_run=max_steps,
            data_schema=schema,
            output_parameter=block_validation_output.output_parameter,
            model=model,
        )
        block_result = await extraction_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )
        return block_result.output_parameter_value


async def validate(
    complete_criterion: str | None = None,
    terminate_criterion: str | None = None,
    error_code_mapping: dict[str, str] | None = None,
    label: str | None = None,
    model: dict[str, Any] | None = None,
) -> None:
    """Validate function that behaves like a ValidationBlock"""
    if not complete_criterion and not terminate_criterion:
        raise Exception("Both complete criterion and terminate criterion are empty")

    result = await execute_validation(complete_criterion, terminate_criterion, error_code_mapping, label, model)
    if result.status == BlockStatus.terminated:
        raise ScriptTerminationException(result.failure_reason)


async def execute_validation(
    complete_criterion: str | None,
    terminate_criterion: str | None,
    error_code_mapping: dict[str, str] | None,
    label: str | None = None,
    model: dict[str, Any] | None = None,
) -> BlockResult:
    block_validation_output = await _validate_and_get_output_parameter(label)
    validation_block = ValidationBlock(
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        task_type=TaskType.validation,
        complete_criterion=complete_criterion,
        terminate_criterion=terminate_criterion,
        error_code_mapping=error_code_mapping,
        max_steps_per_run=2,
        model=model,
    )
    result = await validation_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )
    return result


async def wait(seconds: int, label: str | None = None) -> None:
    # Auto-create workflow block run if workflow_run_id is available (wait block doesn't create tasks)
    workflow_run_block_id, _, _ = await _create_workflow_block_run_and_task(block_type=BlockType.WAIT, label=label)

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
    browser_session_id: str | None = None,
    script_id: str | None = None,
    script_revision_id: str | None = None,
) -> None:
    # register the script run
    context = skyvern_context.current()
    if not context:
        context = skyvern_context.SkyvernContext()
        skyvern_context.set(context)

    context.browser_session_id = browser_session_id
    if organization_id:
        context.organization_id = organization_id
    if script_id:
        context.script_id = script_id
    if script_revision_id:
        context.script_revision_id = script_revision_id

    if workflow_run_id and organization_id:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id, organization_id=organization_id
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
        # update workfow run to indicate that there's a script run
        workflow_run = await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            ai_fallback_triggered=False,
        )
        context.workflow_run_id = workflow_run_id
        context.organization_id = organization_id

    # run the script as subprocess; pass the parameters and run_id to the script
    # Dynamically import the script at the given path
    spec = importlib.util.spec_from_file_location("user_script", path)
    if not spec or not spec.loader:
        raise Exception(f"Failed to import script from {path}")
    user_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(user_script)

    if hasattr(user_script, "run_workflow"):
        # If parameters is None, pass an empty dict
        if parameters:
            await user_script.run_workflow(parameters=parameters)
        else:
            await user_script.run_workflow(parameters={})
    else:
        raise Exception(f"No 'run_workflow' function found in {path}")


def _render_template_with_label(template: str, label: str | None = None) -> str:
    template_data = {}
    context = skyvern_context.current()
    if context and context.workflow_run_id:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(context.workflow_run_id)
        template_data = workflow_run_context.values.copy()
        if label:
            block_reference_data = workflow_run_context.get_block_metadata(label)
            if label in template_data:
                current_value = template_data[label]
                if isinstance(current_value, dict):
                    block_reference_data.update(current_value)
                else:
                    LOG.warning(
                        f"Script service: Parameter {label} has a registered reference value, going to overwrite it by block metadata"
                    )

            template_data[label] = block_reference_data

            # inject the forloop metadata as global variables
            if "current_index" in block_reference_data:
                template_data["current_index"] = block_reference_data["current_index"]
            if "current_item" in block_reference_data:
                template_data["current_item"] = block_reference_data["current_item"]
            if "current_value" in block_reference_data:
                template_data["current_value"] = block_reference_data["current_value"]
    return render_template(template, data=template_data)


def render_template(template: str, data: dict[str, Any] | None = None) -> str:
    """
    Refer to  Block.format_block_parameter_template_from_workflow_run_context

    TODO: complete this function so that block code shares the same template rendering logic
    """
    template_data = data.copy() if data else {}
    jinja_template = jinja_sandbox_env.from_string(template)
    context = skyvern_context.current()
    if context:
        template_data.update(context.script_run_parameters)
        if context.workflow_run_id:
            workflow_run_id = context.workflow_run_id
            workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
            template_data.update(workflow_run_context.values)
            if template in template_data:
                return template_data[template]
    return jinja_template.render(template_data)


def render_list(template: str, data: dict[str, Any] | None = None) -> list[str]:
    rendered_value = render_template(template, data)
    list_value = eval(rendered_value)
    if isinstance(list_value, list):
        return list_value
    else:
        return [list_value]


# Non-task-based blocks
## Non-task-based block helpers
@dataclass
class BlockValidationOutput:
    context: skyvern_context.SkyvernContext
    label: str
    output_parameter: OutputParameter
    input_parameters: list[PARAMETER_TYPE]
    workflow: Workflow
    workflow_id: str
    workflow_run_id: str
    organization_id: str
    browser_session_id: str | None = None


async def _validate_and_get_output_parameter(
    label: str | None = None, parameter_keys: list[str] | None = None
) -> BlockValidationOutput:
    context = skyvern_context.ensure_context()
    workflow_id = context.workflow_id
    workflow_run_id = context.workflow_run_id
    organization_id = context.organization_id
    browser_session_id = context.browser_session_id
    if not workflow_id:
        raise Exception("Workflow ID is required")
    if not workflow_run_id:
        raise Exception("Workflow run ID is required")
    if not organization_id:
        raise Exception("Organization ID is required")
    workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    if not workflow:
        raise Exception("Workflow not found")
    label = label or f"block_{uuid.uuid4()}"
    if context.loop_metadata:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        workflow_run_context.update_block_metadata(label, context.loop_metadata)
    output_parameter = workflow.get_output_parameter(label)
    if not output_parameter:
        # NOT sure if this is legit hack to create output parameter like this
        output_parameter = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"{label}_output",
            workflow_id=workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
        )
    input_parameters = []
    if parameter_keys:
        for parameter_key in parameter_keys:
            parameter = workflow.get_parameter(parameter_key)
            if parameter:
                input_parameters.append(parameter)

    return BlockValidationOutput(
        context=context,
        label=label,
        output_parameter=output_parameter,
        input_parameters=input_parameters,
        workflow=workflow,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        browser_session_id=browser_session_id,
    )


async def run_code(
    code: str,
    label: str | None = None,
    parameters: list[str] | None = None,
) -> dict[str, Any]:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    code_block = CodeBlock(
        code=code,
        label=block_validation_output.label,
        parameters=block_validation_output.input_parameters,
        output_parameter=block_validation_output.output_parameter,
    )
    block_result = await code_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )
    return cast(dict[str, Any], block_result.output_parameter_value)


async def upload_file(
    label: str | None = None,
    parameters: list[str] | None = None,
    storage_type: FileStorageType = FileStorageType.S3,
    s3_bucket: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    region_name: str | None = None,
    azure_storage_account_name: str | None = None,
    azure_storage_account_key: str | None = None,
    azure_blob_container_name: str | None = None,
    path: str | None = None,
) -> None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    if s3_bucket:
        s3_bucket = _render_template_with_label(s3_bucket, label)
    if aws_access_key_id:
        aws_access_key_id = _render_template_with_label(aws_access_key_id, label)
    if aws_secret_access_key:
        aws_secret_access_key = _render_template_with_label(aws_secret_access_key, label)
    if region_name:
        region_name = _render_template_with_label(region_name, label)
    if azure_storage_account_name:
        azure_storage_account_name = _render_template_with_label(azure_storage_account_name, label)
    if azure_storage_account_key:
        azure_storage_account_key = _render_template_with_label(azure_storage_account_key, label)
    if azure_blob_container_name:
        azure_blob_container_name = _render_template_with_label(azure_blob_container_name, label)
    if path:
        path = _render_template_with_label(path, label)
    file_upload_block = FileUploadBlock(
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
        storage_type=FileStorageType(storage_type),
        s3_bucket=s3_bucket,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name,
        azure_storage_account_name=azure_storage_account_name,
        azure_storage_account_key=azure_storage_account_key,
        azure_blob_container_name=azure_blob_container_name,
        path=path,
    )
    await file_upload_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )


async def send_email(
    sender: str,
    recipients: list[str] | str,
    subject: str,
    body: str,
    file_attachments: list[str] = [],
    label: str | None = None,
    parameters: list[str] | None = None,
) -> None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    sender = _render_template_with_label(sender, label)
    if isinstance(recipients, str):
        recipients = render_list(_render_template_with_label(recipients, label))
    subject = _render_template_with_label(subject, label)
    body = _render_template_with_label(body, label)
    workflow = block_validation_output.workflow
    smtp_host_parameter = workflow.get_parameter("smtp_host")
    smtp_port_parameter = workflow.get_parameter("smtp_port")
    smtp_username_parameter = workflow.get_parameter("smtp_username")
    smtp_password_parameter = workflow.get_parameter("smtp_password")
    if not smtp_host_parameter or not smtp_port_parameter or not smtp_username_parameter or not smtp_password_parameter:
        raise Exception("SMTP host, port, username, and password parameters are required")
    send_email_block = SendEmailBlock(
        smtp_host=smtp_host_parameter,
        smtp_port=smtp_port_parameter,
        smtp_username=smtp_username_parameter,
        smtp_password=smtp_password_parameter,
        sender=sender,
        recipients=recipients,
        subject=subject,
        body=body,
        file_attachments=file_attachments,
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
    )
    await send_email_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )


async def parse_pdf(
    file_url: str,
    schema: dict[str, Any] | None = None,
    label: str | None = None,
    parameters: list[str] | None = None,
) -> None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    file_url = _render_template_with_label(file_url, label)
    pdf_parser_block = PDFParserBlock(
        file_url=file_url,
        json_schema=schema,
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
    )
    await pdf_parser_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )


async def parse_file(
    file_url: str,
    file_type: FileType,
    schema: dict[str, Any] | None = None,
    label: str | None = None,
    parameters: list[str] | None = None,
    model: dict[str, Any] | None = None,
) -> None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    file_url = _render_template_with_label(file_url, label)
    file_parser_block = FileParserBlock(
        file_url=file_url,
        file_type=file_type,
        json_schema=schema,
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
        model=model,
    )
    await file_parser_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )


async def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
    follow_redirects: bool = True,
    label: str | None = None,
    parameters: list[str] | None = None,
) -> None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    method = _render_template_with_label(method, label)
    url = _render_template_with_label(url, label)
    http_request_block = HttpRequestBlock(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout=timeout,
        follow_redirects=follow_redirects,
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
    )
    await http_request_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )


async def goto(
    url: str,
    label: str | None = None,
    parameters: list[str] | None = None,
) -> None:
    try:
        block_validation_output = await _validate_and_get_output_parameter(label, parameters)
        url = _render_template_with_label(url, label)
        goto_url_block = UrlBlock(
            url=url,
            label=block_validation_output.label,
            output_parameter=block_validation_output.output_parameter,
            parameters=block_validation_output.input_parameters,
        )
        await goto_url_block.execute_safe(
            workflow_run_id=block_validation_output.workflow_run_id,
            parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
            organization_id=block_validation_output.organization_id,
            browser_session_id=block_validation_output.browser_session_id,
        )
    except Exception:
        run_context = script_run_context_manager.ensure_run_context()
        await run_context.page.goto(url)


async def prompt(
    prompt: str,
    schema: dict[str, Any] | None = None,
    label: str | None = None,
    parameters: list[str] | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, Any] | list | str | None:
    block_validation_output = await _validate_and_get_output_parameter(label, parameters)
    prompt = _render_template_with_label(prompt, label)
    prompt_block = TextPromptBlock(
        prompt=prompt,
        json_schema=schema,
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        parameters=block_validation_output.input_parameters,
        model=model,
    )
    result = await prompt_block.execute_safe(
        workflow_run_id=block_validation_output.workflow_run_id,
        parent_workflow_run_block_id=block_validation_output.context.parent_workflow_run_block_id,
        organization_id=block_validation_output.organization_id,
        browser_session_id=block_validation_output.browser_session_id,
    )
    return result.output_parameter_value


async def loop(
    values: Sequence[Any] | str,
    complete_if_empty: bool = False,
    label: str | None = None,
) -> AsyncGenerator[SkyvernLoopItem, None]:
    workflow_run_block_id, _, _ = await _create_workflow_block_run_and_task(block_type=BlockType.FOR_LOOP, label=label)
    # process values:
    loop_variable_reference = None
    loop_values = None
    if isinstance(values, list):
        loop_values = values
    elif isinstance(values, str):
        loop_variable_reference = values
    else:
        raise ValueError(f"Invalid values type: {type(values)}")

    # step. build the ForLoopBlock instance
    block_validation_output = await _validate_and_get_output_parameter(label)
    loop_block = ForLoopBlock(
        label=block_validation_output.label,
        output_parameter=block_validation_output.output_parameter,
        loop_variable_reference=loop_variable_reference,
        loop_blocks=[],
        complete_if_empty=complete_if_empty,
    )
    workflow_run_id = block_validation_output.workflow_run_id
    organization_id = block_validation_output.organization_id

    if not loop_values:
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        if workflow_run_block_id:
            loop_values = await loop_block.get_values_from_loop_variable_reference(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

    if not loop_values:
        # step 3. if loop_values is empty, record empty output parameter value
        LOG.info(
            "script service: No loop values found, terminating block",
            block_type=BlockType.FOR_LOOP,
            workflow_run_id=workflow_run_id,
            complete_if_empty=complete_if_empty,
        )
        await loop_block.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
        # step 4. build response (success/failure) given the complete_if_empty value
        if complete_if_empty:
            await loop_block.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=[],
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
            return
        else:
            await loop_block.build_block_result(
                success=False,
                failure_reason="No iterable value found for the loop block",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
            raise Exception("No iterable value found for the loop block")

    # register the loop in the global context
    block_validation_output.context.parent_workflow_run_block_id = workflow_run_block_id
    block_validation_output.context.loop_output_values = []

    # step 5. start the loop
    try:
        for index, value in enumerate(loop_values):
            # register current_value, current_item and current_index in workflow run context
            loop_metadata = {
                "current_index": index,
                "current_value": value,
                "current_item": value,
            }
            block_validation_output.context.loop_metadata = loop_metadata
            workflow_run_context.update_block_metadata(block_validation_output.label, loop_metadata)
            # Build the SkyvernLoopItem for this loop
            yield SkyvernLoopItem(index, value)

        # build success output
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.completed,
                output=block_validation_output.context.loop_output_values,
                label=label,
            )
    except Exception as e:
        # build failure output
        if workflow_run_block_id:
            await _update_workflow_block(
                workflow_run_block_id,
                BlockStatus.failed,
                failure_reason=str(e),
                output=block_validation_output.context.loop_output_values,
                label=label,
            )
        raise e
    finally:
        block_validation_output.context.parent_workflow_run_block_id = None
        block_validation_output.context.loop_metadata = None
        block_validation_output.context.loop_output_values = None
