import asyncio
import importlib.util
import json
import os
import textwrap
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
import structlog

import skyvern
from skyvern import analytics
from skyvern.client.types.output_parameter import OutputParameter as BlockOutputParameter
from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import (
    BlockNotFound,
    BrowserSessionNotFound,
    CannotUpdateWorkflowDueToCodeCache,
    FailedToSendWebhook,
    InvalidCredentialId,
    MissingValueForParameter,
    SkyvernException,
    WorkflowNotFound,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_headers
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.trace import TraceManager
from skyvern.forge.sdk.trace.experiment_utils import collect_experiment_metadata_safely
from skyvern.forge.sdk.workflow.exceptions import (
    ContextParameterSourceNotDefined,
    InvalidWaitBlockTime,
    InvalidWorkflowDefinition,
    WorkflowDefinitionHasDuplicateParameterKeys,
    WorkflowDefinitionHasReservedParameterKeys,
    WorkflowParameterMissingRequiredValue,
)
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BlockTypeVar,
    CodeBlock,
    DownloadToS3Block,
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
    TaskV2Block,
    TextPromptBlock,
    UploadToS3Block,
    UrlBlock,
    ValidationBlock,
    WaitBlock,
    get_all_blocks,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    RESERVED_PARAMETER_KEYS,
    AWSSecretParameter,
    AzureVaultCredentialParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    ContextParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import ProxyLocation, RunStatus, RunType, WorkflowRunRequest, WorkflowRunResponse
from skyvern.schemas.scripts import ScriptStatus, WorkflowScript
from skyvern.schemas.workflows import (
    BLOCK_YAML_TYPES,
    BlockResult,
    BlockStatus,
    BlockType,
    ForLoopBlockYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
    WorkflowStatus,
)
from skyvern.services import script_service, workflow_script_service
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()

DEFAULT_FIRST_BLOCK_LABEL = "block_1"
DEFAULT_WORKFLOW_TITLE = "New Workflow"


def _get_workflow_definition_core_data(workflow_definition: WorkflowDefinition) -> dict[str, Any]:
    """
    This function dumps the workflow definition and removes the irrelevant data to the definition, like created_at and modified_at fields inside:
    - list of blocks
    - list of parameters
    And return the dumped workflow definition as a python dictionary.
    """
    # Convert the workflow definition to a dictionary
    workflow_dict = workflow_definition.model_dump()
    fields_to_remove = [
        "created_at",
        "modified_at",
        "deleted_at",
        "output_parameter_id",
        "workflow_id",
        "workflow_parameter_id",
        "aws_secret_parameter_id",
        "bitwarden_login_credential_parameter_id",
        "bitwarden_sensitive_information_parameter_id",
        "bitwarden_credit_card_data_parameter_id",
        "credential_parameter_id",
        "onepassword_credential_parameter_id",
        "azure_vault_credential_parameter_id",
        "disable_cache",
    ]

    # Use BFS to recursively remove fields from all nested objects

    # Queue to store objects to process
    queue = deque([workflow_dict])

    while queue:
        current_obj = queue.popleft()

        if isinstance(current_obj, dict):
            # Remove specified fields from current dictionary
            for field in fields_to_remove:
                if field:  # Skip empty string
                    current_obj.pop(field, None)

            # Add all nested dictionaries and lists to queue for processing
            for value in current_obj.values():
                if isinstance(value, (dict, list)):
                    queue.append(value)

        elif isinstance(current_obj, list):
            # Add all items in the list to queue for processing
            for item in current_obj:
                if isinstance(item, (dict, list)):
                    queue.append(item)

    return workflow_dict


class WorkflowService:
    @staticmethod
    def _collect_extracted_information(value: Any) -> list[Any]:
        """Recursively collect extracted_information values from nested outputs."""
        results: list[Any] = []
        if isinstance(value, dict):
            if "extracted_information" in value and value["extracted_information"] is not None:
                extracted = value["extracted_information"]
                if isinstance(extracted, list):
                    results.extend(extracted)
                else:
                    results.append(extracted)
            else:
                for v in value.values():
                    results.extend(WorkflowService._collect_extracted_information(v))
        elif isinstance(value, list):
            for item in value:
                results.extend(WorkflowService._collect_extracted_information(item))
        return results

    async def _validate_credential_id(self, credential_id: str, organization: Organization) -> None:
        credential = await app.DATABASE.get_credential(credential_id, organization_id=organization.organization_id)
        if credential is None:
            raise InvalidCredentialId(credential_id)

    async def setup_workflow_run(
        self,
        request_id: str | None,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        organization: Organization,
        is_template_workflow: bool = False,
        version: int | None = None,
        max_steps_override: int | None = None,
        parent_workflow_run_id: str | None = None,
        debug_session_id: str | None = None,
        code_gen: bool | None = None,
    ) -> WorkflowRun:
        """
        Create a workflow run and its parameters. Validate the workflow and the organization. If there are missing
        parameters with no default value, mark the workflow run as failed.
        :param request_id: The request id for the workflow run.
        :param workflow_request: The request body for the workflow run, containing the parameters and the config.
        :param workflow_id: The workflow id to run.
        :param organization_id: The organization id for the workflow.
        :param max_steps_override: The max steps override for the workflow run, if any.
        :return: The created workflow run.
        """
        # Validate the workflow and the organization
        workflow = await self.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=None if is_template_workflow else organization.organization_id,
            version=version,
        )
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found", workflow_version=version)
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)
        workflow_id = workflow.workflow_id
        if workflow_request.proxy_location is None and workflow.proxy_location is not None:
            workflow_request.proxy_location = workflow.proxy_location
        if workflow_request.webhook_callback_url is None and workflow.webhook_callback_url is not None:
            workflow_request.webhook_callback_url = workflow.webhook_callback_url
        # Create the workflow run and set skyvern context
        workflow_run = await self.create_workflow_run(
            workflow_request=workflow_request,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization.organization_id,
            parent_workflow_run_id=parent_workflow_run_id,
            sequential_key=workflow.sequential_key,
            debug_session_id=debug_session_id,
            code_gen=code_gen,
        )
        LOG.info(
            f"Created workflow run {workflow_run.workflow_run_id} for workflow {workflow.workflow_id}",
            request_id=request_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            organization_id=workflow.organization_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
            ai_fallback=workflow_request.ai_fallback,
            run_with=workflow_request.run_with,
            code_gen=code_gen,
        )
        context: skyvern_context.SkyvernContext | None = skyvern_context.current()
        current_run_id = context.run_id if context and context.run_id else workflow_run.workflow_run_id
        root_workflow_run_id = (
            context.root_workflow_run_id if context and context.root_workflow_run_id else workflow_run.workflow_run_id
        )
        skyvern_context.set(
            SkyvernContext(
                organization_id=organization.organization_id,
                organization_name=organization.organization_name,
                request_id=request_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                root_workflow_run_id=root_workflow_run_id,
                run_id=current_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                max_steps_override=max_steps_override,
                max_screenshot_scrolls=workflow_request.max_screenshot_scrolls,
            )
        )

        # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
        all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
        try:
            for workflow_parameter in all_workflow_parameters:
                if workflow_request.data and workflow_parameter.key in workflow_request.data:
                    request_body_value = workflow_request.data[workflow_parameter.key]
                    if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                        await self._validate_credential_id(str(request_body_value), organization)
                    await self.create_workflow_run_parameter(
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_parameter=workflow_parameter,
                        value=request_body_value,
                    )
                elif workflow_parameter.default_value is not None:
                    if workflow_parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                        await self._validate_credential_id(str(workflow_parameter.default_value), organization)
                    await self.create_workflow_run_parameter(
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_parameter=workflow_parameter,
                        value=workflow_parameter.default_value,
                    )
                else:
                    raise MissingValueForParameter(
                        parameter_key=workflow_parameter.key,
                        workflow_id=workflow.workflow_permanent_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                    )
        except Exception as e:
            LOG.exception(
                f"Error while setting up workflow run {workflow_run.workflow_run_id}",
                workflow_run_id=workflow_run.workflow_run_id,
            )

            failure_reason = f"Setup workflow failed due to an unexpected exception: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"Setup workflow failed due to an SkyvernException({e.__class__.__name__}): {str(e)}"

            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )
            raise e

        if workflow_request.browser_session_id:
            await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
                browser_session_id=workflow_request.browser_session_id,
                runnable_type="workflow_run",
                runnable_id=workflow_run.workflow_run_id,
                organization_id=organization.organization_id,
            )

        return workflow_run

    @TraceManager.traced_async(ignore_inputs=["organization", "api_key"])
    async def execute_workflow(
        self,
        workflow_run_id: str,
        api_key: str,
        organization: Organization,
        block_labels: list[str] | None = None,
        block_outputs: dict[str, Any] | None = None,
        browser_session_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a workflow."""
        organization_id = organization.organization_id

        # Collect and add experiment metadata to the trace
        experiment_metadata = await collect_experiment_metadata_safely(app.EXPERIMENTATION_PROVIDER)
        if experiment_metadata:
            TraceManager.add_experiment_metadata(experiment_metadata)

        LOG.info(
            "Executing workflow",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            block_labels=block_labels,
            block_outputs=block_outputs,
        )
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id=workflow_run.workflow_permanent_id)
        close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address

        # Set workflow run status to running, create workflow run parameters
        workflow_run = await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id)

        # Get all context parameters from the workflow definition
        context_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(parameter, ContextParameter)
        ]

        secret_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(
                parameter,
                (
                    AWSSecretParameter,
                    BitwardenLoginCredentialParameter,
                    BitwardenCreditCardDataParameter,
                    BitwardenSensitiveInformationParameter,
                    OnePasswordCredentialParameter,
                    AzureVaultCredentialParameter,
                    CredentialParameter,
                ),
            )
        ]

        # Get all <workflow parameter, workflow run parameter> tuples
        wp_wps_tuples = await self.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run_id)
        workflow_output_parameters = await self.get_workflow_output_parameters(workflow_id=workflow.workflow_id)
        try:
            await app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
                organization,
                workflow_run_id,
                workflow.title,
                workflow.workflow_id,
                workflow.workflow_permanent_id,
                wp_wps_tuples,
                workflow_output_parameters,
                context_parameters,
                secret_parameters,
                block_outputs,
            )
        except Exception as e:
            LOG.exception(
                f"Error while initializing workflow run context for workflow run {workflow_run_id}",
                workflow_run_id=workflow_run_id,
            )

            exception_message = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            failure_reason = f"Failed to initialize workflow run context. failure reason: {exception_message}"
            workflow_run = await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id, failure_reason=failure_reason
            )
            await self.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
                api_key=api_key,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion,
            )
            return workflow_run

        # Check if there's a related workflow script that should be used instead
        workflow_script, _ = await workflow_script_service.get_workflow_script(workflow, workflow_run, block_labels)
        current_context = skyvern_context.current()
        if current_context:
            if workflow_script:
                current_context.generate_script = False
            if workflow_run.code_gen:
                current_context.generate_script = True
        is_script_run = self.should_run_script(workflow, workflow_run)
        # Unified execution: execute blocks one by one, using script code when available
        if is_script_run is False:
            workflow_script = None
        workflow_run = await self._execute_workflow_blocks(
            workflow=workflow,
            workflow_run=workflow_run,
            organization=organization,
            browser_session_id=browser_session_id,
            block_labels=block_labels,
            block_outputs=block_outputs,
            workflow_script=workflow_script,
        )

        if refreshed_workflow_run := await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        ):
            workflow_run = refreshed_workflow_run
            if workflow_run.status not in (
                WorkflowRunStatus.canceled,
                WorkflowRunStatus.failed,
                WorkflowRunStatus.terminated,
                WorkflowRunStatus.timed_out,
            ):
                workflow_run = await self.mark_workflow_run_as_completed(
                    workflow_run_id=workflow_run_id,
                )
                await self.generate_script_if_needed(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    block_labels=block_labels,
                )
            else:
                LOG.info(
                    "Workflow run is already timed_out, canceled, failed, or terminated, not marking as completed",
                    workflow_run_id=workflow_run_id,
                    workflow_run_status=workflow_run.status if workflow_run else None,
                )
        await self.clean_up_workflow(
            workflow=workflow,
            workflow_run=workflow_run,
            api_key=api_key,
            browser_session_id=browser_session_id,
            close_browser_on_completion=close_browser_on_completion,
        )

        return workflow_run

    async def _execute_workflow_blocks(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        organization: Organization,
        browser_session_id: str | None = None,
        block_labels: list[str] | None = None,
        block_outputs: dict[str, Any] | None = None,
        workflow_script: WorkflowScript | None = None,
    ) -> WorkflowRun:
        organization_id = organization.organization_id
        workflow_run_id = workflow_run.workflow_run_id
        top_level_blocks = workflow.workflow_definition.blocks
        all_blocks = get_all_blocks(top_level_blocks)

        # Load script blocks if workflow_script is provided
        script_blocks_by_label: dict[str, Any] = {}
        loaded_script_module = None

        if workflow_script:
            LOG.info(
                "Loading script blocks for workflow execution",
                workflow_run_id=workflow_run_id,
                script_id=workflow_script.script_id,
            )
            try:
                # Load script blocks from database
                script = await app.DATABASE.get_script(
                    script_id=workflow_script.script_id,
                    organization_id=organization_id,
                )
                if script:
                    script_files = await app.DATABASE.get_script_files(
                        script_revision_id=script.script_revision_id,
                        organization_id=organization_id,
                    )
                    await script_service.load_scripts(script, script_files)

                    script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
                        script_revision_id=script.script_revision_id,
                        organization_id=organization_id,
                    )

                    # Create mapping from block label to script block
                    for script_block in script_blocks:
                        if script_block.run_signature:
                            script_blocks_by_label[script_block.script_block_label] = script_block

                    script_path = os.path.join(settings.TEMP_PATH, workflow_script.script_id, "main.py")
                    if os.path.exists(script_path):
                        # setup script run
                        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(
                            workflow_run_id=workflow_run.workflow_run_id
                        )
                        script_parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

                        spec = importlib.util.spec_from_file_location("user_script", script_path)
                        if spec and spec.loader:
                            loaded_script_module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(loaded_script_module)
                            await skyvern.setup(
                                script_parameters,
                                generated_parameter_cls=loaded_script_module.GeneratedWorkflowParameters,
                            )
                            LOG.info(
                                "Successfully loaded script module",
                                script_id=workflow_script.script_id,
                                block_count=len(script_blocks_by_label),
                            )
                    else:
                        LOG.warning(
                            "Script file not found at path",
                            script_path=script_path,
                            script_id=workflow_script.script_id,
                        )
            except Exception as e:
                LOG.warning(
                    "Failed to load script blocks, will fallback to normal execution",
                    error=str(e),
                    exc_info=True,
                    workflow_run_id=workflow_run_id,
                    script_id=workflow_script.script_id,
                )
                script_blocks_by_label = {}
                loaded_script_module = None

        # Mark workflow as running with appropriate engine
        run_with = "code" if script_blocks_by_label else "agent"
        await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id, run_with=run_with)

        if block_labels and len(block_labels):
            blocks: list[BlockTypeVar] = []
            all_labels = {block.label: block for block in all_blocks}
            for label in block_labels:
                if label not in all_labels:
                    raise BlockNotFound(block_label=label)

                blocks.append(all_labels[label])

            LOG.info(
                "Executing workflow blocks via whitelist",
                workflow_run_id=workflow_run_id,
                block_cnt=len(blocks),
                block_labels=block_labels,
                block_outputs=block_outputs,
            )

        else:
            blocks = top_level_blocks

        if not blocks:
            raise SkyvernException(f"No blocks found for the given block labels: {block_labels}")

        # Execute workflow blocks
        blocks_cnt = len(blocks)
        block_result = None
        for block_idx, block in enumerate(blocks):
            try:
                if refreshed_workflow_run := await app.DATABASE.get_workflow_run(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                ):
                    workflow_run = refreshed_workflow_run
                    if workflow_run.status == WorkflowRunStatus.canceled:
                        LOG.info(
                            "Workflow run is canceled, stopping execution inside workflow execution loop",
                            workflow_run_id=workflow_run_id,
                            block_idx=block_idx,
                            block_type=block.block_type,
                            block_label=block.label,
                        )
                        break

                    if workflow_run.status == WorkflowRunStatus.timed_out:
                        LOG.info(
                            "Workflow run is timed out, stopping execution inside workflow execution loop",
                            workflow_run_id=workflow_run_id,
                            block_idx=block_idx,
                            block_type=block.block_type,
                            block_label=block.label,
                        )
                        break

                parameters = block.get_all_parameters(workflow_run_id)
                await app.WORKFLOW_CONTEXT_MANAGER.register_block_parameters_for_workflow_run(
                    workflow_run_id, parameters, organization
                )
                LOG.info(
                    f"Executing root block {block.block_type} at index {block_idx}/{blocks_cnt - 1} for workflow run {workflow_run_id}",
                    block_type=block.block_type,
                    workflow_run_id=workflow_run_id,
                    block_idx=block_idx,
                    block_type_var=block.block_type,
                    block_label=block.label,
                    model=block.model,
                )

                # Try executing with script code if available
                block_executed_with_code = False
                valid_to_run_code = block.label and block.label in script_blocks_by_label and not block.disable_cache
                if valid_to_run_code:
                    script_block = script_blocks_by_label[block.label]
                    LOG.info(
                        "Attempting to execute block with script code",
                        block_label=block.label,
                        run_signature=script_block.run_signature,
                    )
                    try:
                        # Execute the run signature and capture the return value
                        vars_dict = vars(loaded_script_module) if loaded_script_module else {}
                        exec_globals = {
                            **vars_dict,
                            "skyvern": skyvern,
                            "__builtins__": __builtins__,
                        }

                        # Use exec to handle multi-line run_signature statements
                        # Create an async function and execute it

                        # Dedent first to normalize indentation, then re-indent for function body
                        assert script_block.run_signature is not None
                        normalized_signature = textwrap.dedent(script_block.run_signature).strip()
                        # Add 8 spaces (2 levels: function + return statement)
                        indented_signature = textwrap.indent(normalized_signature, "        ")

                        # Build the wrapper function
                        wrapper_code = (
                            f"async def __run_signature_wrapper():\n    return (\n{indented_signature}\n    )\n"
                        )

                        LOG.debug("Executing run_signature wrapper", wrapper_code=wrapper_code)

                        exec_code = compile(wrapper_code, "<run_signature>", "exec")
                        exec(exec_code, exec_globals)
                        output_value = await exec_globals["__run_signature_wrapper"]()

                        # Execution succeeded - get the block result from the workflow run blocks
                        # The script execution should have created the workflow run block
                        workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                        )
                        # Find the most recent block with matching label
                        matching_blocks = [b for b in workflow_run_blocks if b.label == block.label]
                        if matching_blocks:
                            latest_block = max(matching_blocks, key=lambda b: b.created_at)

                            # Construct BlockResult from the workflow_run_block
                            block_result = BlockResult(
                                success=latest_block.status == BlockStatus.completed,
                                failure_reason=latest_block.failure_reason,
                                output_parameter=block.output_parameter,
                                output_parameter_value=latest_block.output,
                                status=BlockStatus(latest_block.status) if latest_block.status else BlockStatus.failed,
                                workflow_run_block_id=latest_block.workflow_run_block_id,
                            )
                            block_executed_with_code = True
                            LOG.info(
                                "Successfully executed block with script code",
                                block_label=block.label,
                                block_status=block_result.status,
                                has_output=output_value is not None,
                            )
                        else:
                            LOG.warning(
                                "Block executed with code but no workflow run block found",
                                block_label=block.label,
                            )
                            # Fallback to AI execution
                            block_executed_with_code = False
                    except Exception as e:
                        LOG.warning(
                            "Failed to execute block with script code, falling back to AI",
                            block_label=block.label,
                            error=str(e),
                            exc_info=True,
                        )
                        block_executed_with_code = False

                # Execute with AI if code execution was not attempted or failed
                if not block_executed_with_code:
                    LOG.info(
                        "Executing block with AI",
                        block_label=block.label,
                        block_type=block.block_type,
                    )
                    block_result = await block.execute_safe(
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                        browser_session_id=browser_session_id,
                    )
                if not block_result:
                    workflow_run = await self.mark_workflow_run_as_failed(
                        workflow_run_id=workflow_run_id, failure_reason="Block result is None"
                    )
                    break
                if block_result.status == BlockStatus.canceled:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was canceled for workflow run {workflow_run_id}, cancelling workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )
                    workflow_run = await self.mark_workflow_run_as_canceled(workflow_run_id=workflow_run_id)
                    break
                elif block_result.status == BlockStatus.failed:
                    LOG.error(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed for workflow run {workflow_run_id}",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )
                    if not block.continue_on_failure:
                        failure_reason = (
                            f"{block.block_type} block failed. failure reason: {block_result.failure_reason}"
                        )
                        workflow_run = await self.mark_workflow_run_as_failed(
                            workflow_run_id=workflow_run_id, failure_reason=failure_reason
                        )
                        break

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed but will continue executing the workflow run {workflow_run_id}",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                elif block_result.status == BlockStatus.terminated:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, marking workflow run as terminated",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                    if not block.continue_on_failure:
                        failure_reason = f"{block.block_type} block terminated. Reason: {block_result.failure_reason}"
                        workflow_run = await self.mark_workflow_run_as_terminated(
                            workflow_run_id=workflow_run_id, failure_reason=failure_reason
                        )
                        break

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, but will continue executing the workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                elif block_result.status == BlockStatus.timed_out:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, marking workflow run as failed",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                    if not block.continue_on_failure:
                        failure_reason = f"{block.block_type} block timed out. Reason: {block_result.failure_reason}"
                        workflow_run = await self.mark_workflow_run_as_failed(
                            workflow_run_id=workflow_run_id, failure_reason=failure_reason
                        )
                        break

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, but will continue executing the workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

            except Exception as e:
                LOG.exception(
                    f"Error while executing workflow run {workflow_run_id}",
                    workflow_run_id=workflow_run_id,
                    block_idx=block_idx,
                    block_type=block.block_type,
                    block_label=block.label,
                )

                exception_message = f"Unexpected error: {str(e)}"
                if isinstance(e, SkyvernException):
                    exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

                failure_reason = f"{block.block_type} block failed. failure reason: {exception_message}"
                workflow_run = await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id, failure_reason=failure_reason
                )
                break
        return workflow_run

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: WorkflowDefinition,
        description: str | None = None,
        proxy_location: ProxyLocation | None = None,
        max_screenshot_scrolling_times: int | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        model: dict[str, Any] | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
        extra_http_headers: dict[str, str] | None = None,
        run_with: str | None = None,
        cache_key: str | None = None,
        ai_fallback: bool | None = None,
        run_sequentially: bool = False,
        sequential_key: str | None = None,
    ) -> Workflow:
        return await app.DATABASE.create_workflow(
            title=title,
            workflow_definition=workflow_definition.model_dump(),
            organization_id=organization_id,
            description=description,
            proxy_location=proxy_location,
            webhook_callback_url=webhook_callback_url,
            max_screenshot_scrolling_times=max_screenshot_scrolling_times,
            totp_verification_url=totp_verification_url,
            totp_identifier=totp_identifier,
            persist_browser_session=persist_browser_session,
            model=model,
            workflow_permanent_id=workflow_permanent_id,
            version=version,
            is_saved_task=is_saved_task,
            status=status,
            extra_http_headers=extra_http_headers,
            run_with=run_with,
            cache_key=cache_key,
            ai_fallback=False if ai_fallback is None else ai_fallback,
            run_sequentially=run_sequentially,
            sequential_key=sequential_key,
        )

    async def create_workflow_from_prompt(
        self,
        organization: Organization,
        user_prompt: str,
        totp_identifier: str | None = None,
        totp_verification_url: str | None = None,
        webhook_callback_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        max_iterations: int | None = None,
        max_steps: int | None = None,
        status: WorkflowStatus = WorkflowStatus.auto_generated,
        run_with: str | None = None,
        ai_fallback: bool = True,
        task_version: Literal["v1", "v2"] = "v2",
    ) -> Workflow:
        metadata_prompt = prompt_engine.load_prompt(
            "conversational_ui_goal",
            user_goal=user_prompt,
        )

        metadata_response = await app.LLM_API_HANDLER(
            prompt=metadata_prompt,
            prompt_name="conversational_ui_goal",
            organization_id=organization.organization_id,
        )

        block_label: str = metadata_response.get("block_label", None) or DEFAULT_FIRST_BLOCK_LABEL
        title: str = metadata_response.get("title", None) or DEFAULT_WORKFLOW_TITLE

        if task_version == "v1":
            task_prompt = prompt_engine.load_prompt(
                "generate-task",
                user_prompt=user_prompt,
            )

            task_response = await app.LLM_API_HANDLER(
                prompt=task_prompt,
                prompt_name="generate-task",
                organization_id=organization.organization_id,
            )

            data_extraction_goal: str | None = task_response.get("data_extraction_goal")
            navigation_goal: str = task_response.get("navigation_goal", None) or user_prompt
            url: str = task_response.get("url", None) or ""

            blocks = [
                NavigationBlock(
                    url=url,
                    label=block_label,
                    title=title,
                    navigation_goal=navigation_goal,
                    max_steps_per_run=max_steps or settings.MAX_STEPS_PER_RUN,
                    totp_verification_url=totp_verification_url,
                    totp_identifier=totp_identifier,
                    output_parameter=OutputParameter(
                        output_parameter_id=str(uuid.uuid4()),
                        key=f"{block_label}_output",
                        workflow_id="",
                        created_at=datetime.now(UTC),
                        modified_at=datetime.now(UTC),
                    ),
                ),
            ]

            if data_extraction_goal:
                blocks.append(
                    ExtractionBlock(
                        label="extract_data",
                        title="Extract Data",
                        data_extraction_goal=data_extraction_goal,
                        output_parameter=OutputParameter(
                            output_parameter_id=str(uuid.uuid4()),
                            key="extract_data_output",
                            workflow_id="",
                            created_at=datetime.now(UTC),
                            modified_at=datetime.now(UTC),
                        ),
                        max_steps_per_run=max_steps or settings.MAX_STEPS_PER_RUN,
                        totp_verification_url=totp_verification_url,
                        totp_identifier=totp_identifier,
                    )
                )

        elif task_version == "v2":
            blocks = [
                TaskV2Block(
                    prompt=user_prompt,
                    totp_identifier=totp_identifier,
                    totp_verification_url=totp_verification_url,
                    label=block_label,
                    max_iterations=max_iterations or settings.MAX_ITERATIONS_PER_TASK_V2,
                    max_steps=max_steps or settings.MAX_STEPS_PER_TASK_V2,
                    output_parameter=OutputParameter(
                        output_parameter_id=str(uuid.uuid4()),
                        key=f"{block_label}_output",
                        workflow_id="",
                        created_at=datetime.now(UTC),
                        modified_at=datetime.now(UTC),
                    ),
                )
            ]

        new_workflow = await self.create_workflow(
            title=title,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=blocks),
            organization_id=organization.organization_id,
            proxy_location=proxy_location,
            webhook_callback_url=webhook_callback_url,
            totp_verification_url=totp_verification_url,
            totp_identifier=totp_identifier,
            max_screenshot_scrolling_times=max_screenshot_scrolling_times,
            extra_http_headers=extra_http_headers,
            status=status,
            run_with=run_with,
            ai_fallback=ai_fallback,
        )

        return new_workflow

    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow:
        workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
        if not workflow:
            raise WorkflowNotFound(workflow_id=workflow_id)
        return workflow

    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        exclude_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            exclude_deleted=exclude_deleted,
        )
        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)

        return workflow

    async def get_workflow_versions_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        exclude_deleted: bool = True,
    ) -> list[Workflow]:
        """
        Get all versions of a workflow by its permanent ID.
        Returns an empty list if no workflow is found with that permanent ID.
        """
        workflows = await app.DATABASE.get_workflow_versions_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            exclude_deleted=exclude_deleted,
        )
        return workflows

    async def get_block_outputs_for_debug_session(
        self,
        workflow_permanent_id: str,
        user_id: str,
        organization_id: str,
        exclude_deleted: bool = True,
        version: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            exclude_deleted=exclude_deleted,
        )

        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)

        labels_to_outputs: dict[str, BlockOutputParameter] = {}

        for block in workflow.workflow_definition.blocks:
            label = block.label

            block_run = await app.DATABASE.get_latest_completed_block_run(
                organization_id=organization_id,
                user_id=user_id,
                block_label=label,
                workflow_permanent_id=workflow_permanent_id,
            )

            if not block_run:
                continue

            output_parameter = await app.DATABASE.get_workflow_run_output_parameter_by_id(
                workflow_run_id=block_run.workflow_run_id, output_parameter_id=block_run.output_parameter_id
            )

            if not output_parameter:
                continue

            block_output_parameter = output_parameter.value

            if not isinstance(block_output_parameter, dict):
                continue

            block_output_parameter["created_at"] = output_parameter.created_at
            labels_to_outputs[label] = block_output_parameter

        return labels_to_outputs

    async def get_workflows_by_permanent_ids(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        return await app.DATABASE.get_workflows_by_permanent_ids(
            workflow_permanent_ids,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            title=title,
            statuses=statuses,
        )

    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.
        """
        return await app.DATABASE.get_workflows_by_organization_id(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            only_saved_tasks=only_saved_tasks,
            only_workflows=only_workflows,
            title=title,
            statuses=statuses,
        )

    async def update_workflow_definition(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
    ) -> Workflow:
        updated_workflow = await app.DATABASE.update_workflow(
            workflow_id=workflow_id,
            title=title,
            organization_id=organization_id,
            description=description,
            workflow_definition=(workflow_definition.model_dump() if workflow_definition else None),
        )

        return updated_workflow

    async def maybe_delete_cached_code(
        self,
        workflow: Workflow,
        workflow_definition: WorkflowDefinition,
        organization_id: str,
        delete_script: bool = True,
        delete_code_cache_is_ok: bool = False,
    ) -> None:
        if workflow_definition:
            workflow_definition.validate()

        previous_valid_workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization_id=organization_id,
            exclude_deleted=True,
            ignore_version=workflow.version,
        )

        if previous_valid_workflow:
            current_definition = _get_workflow_definition_core_data(previous_valid_workflow.workflow_definition)
            new_definition = _get_workflow_definition_core_data(workflow_definition)
            has_changes = current_definition != new_definition
        else:
            has_changes = False

        if previous_valid_workflow and has_changes and delete_script:
            candidates = await app.DATABASE.get_workflow_scripts_by_permanent_id(
                organization_id=organization_id,
                workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
            )

            to_delete_published = [script for script in candidates if script.status == ScriptStatus.published]
            to_delete = [script for script in candidates if script.status != ScriptStatus.published]

            if len(to_delete_published) > 0:
                if not delete_code_cache_is_ok:
                    LOG.info(
                        "Workflow definition changed, asking user if deleting published code is ok",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        to_delete_non_published_cnt=len(to_delete),
                        to_delete_published_cnt=len(to_delete_published),
                    )

                    raise CannotUpdateWorkflowDueToCodeCache(
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                    )
                else:
                    LOG.info(
                        "Workflow definition changed, user answered yes to deleting published code",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        to_delete_non_published_cnt=len(to_delete),
                        to_delete_published_cnt=len(to_delete_published),
                    )

                    to_delete.extend(to_delete_published)

            if len(to_delete) > 0:
                try:
                    await app.DATABASE.delete_workflow_scripts_by_permanent_id(
                        organization_id=organization_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        script_ids=[s.script_id for s in to_delete],
                    )
                except Exception as e:
                    LOG.error(
                        "Failed to delete workflow scripts after workflow definition change",
                        workflow_id=workflow.workflow_id,
                        workflow_permanent_id=previous_valid_workflow.workflow_permanent_id,
                        organization_id=organization_id,
                        previous_version=previous_valid_workflow.version,
                        new_version=workflow.version,
                        error=str(e),
                        to_delete_ids=[script.script_id for script in to_delete],
                        to_delete_cnt=len(to_delete),
                    )

    async def delete_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def delete_workflow_by_id(
        self,
        workflow_id: str,
        organization_id: str,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_id(
            workflow_id=workflow_id,
            organization_id=organization_id,
        )

    async def get_workflow_runs(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        ordering: tuple[str, str] | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
            ordering=ordering,
        )

    async def get_workflow_runs_count(
        self,
        organization_id: str,
        status: list[WorkflowRunStatus] | None = None,
    ) -> int:
        return await app.DATABASE.get_workflow_runs_count(
            organization_id=organization_id,
            status=status,
        )

    async def get_workflow_runs_for_workflow_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
        )

    async def create_workflow_run(
        self,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        parent_workflow_run_id: str | None = None,
        sequential_key: str | None = None,
        debug_session_id: str | None = None,
        code_gen: bool | None = None,
    ) -> WorkflowRun:
        # validate the browser session id
        if workflow_request.browser_session_id:
            browser_session = await app.DATABASE.get_persistent_browser_session(
                session_id=workflow_request.browser_session_id,
                organization_id=organization_id,
            )
            if not browser_session:
                raise BrowserSessionNotFound(browser_session_id=workflow_request.browser_session_id)

        return await app.DATABASE.create_workflow_run(
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            browser_session_id=workflow_request.browser_session_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            totp_verification_url=workflow_request.totp_verification_url,
            totp_identifier=workflow_request.totp_identifier,
            parent_workflow_run_id=parent_workflow_run_id,
            max_screenshot_scrolling_times=workflow_request.max_screenshot_scrolls,
            extra_http_headers=workflow_request.extra_http_headers,
            browser_address=workflow_request.browser_address,
            sequential_key=sequential_key,
            run_with=workflow_request.run_with,
            debug_session_id=debug_session_id,
            ai_fallback=workflow_request.ai_fallback,
            code_gen=code_gen,
        )

    async def _update_workflow_run_status(
        self,
        workflow_run_id: str,
        status: WorkflowRunStatus,
        failure_reason: str | None = None,
        run_with: str | None = None,
        ai_fallback: bool | None = None,
    ) -> WorkflowRun:
        workflow_run = await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=status,
            failure_reason=failure_reason,
            run_with=run_with,
            ai_fallback=ai_fallback,
        )
        if status in [WorkflowRunStatus.completed, WorkflowRunStatus.failed, WorkflowRunStatus.terminated]:
            start_time = (
                workflow_run.started_at.replace(tzinfo=UTC)
                if workflow_run.started_at
                else workflow_run.created_at.replace(tzinfo=UTC)
            )
            queued_seconds = (start_time - workflow_run.created_at.replace(tzinfo=UTC)).total_seconds()
            duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
            LOG.info(
                "Workflow run duration metrics",
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_run.workflow_id,
                queued_seconds=queued_seconds,
                duration_seconds=duration_seconds,
                workflow_run_status=workflow_run.status,
                organization_id=workflow_run.organization_id,
                run_with=run_with,
            )
        return workflow_run

    async def mark_workflow_run_as_completed(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as completed",
            workflow_run_id=workflow_run_id,
            workflow_status="completed",
        )

        # Add workflow completion tag to Laminar trace
        TraceManager.add_task_completion_tag(WorkflowRunStatus.completed)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.completed,
            run_with=run_with,
        )

    async def mark_workflow_run_as_failed(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as failed",
            workflow_run_id=workflow_run_id,
            workflow_status="failed",
            failure_reason=failure_reason,
        )

        # Add workflow failure tag to Laminar trace
        TraceManager.add_task_completion_tag(WorkflowRunStatus.failed)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def mark_workflow_run_as_running(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as running",
            workflow_run_id=workflow_run_id,
            workflow_status="running",
            run_with=run_with,
        )
        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.running,
            run_with=run_with,
        )

    async def mark_workflow_run_as_terminated(
        self,
        workflow_run_id: str,
        failure_reason: str | None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as terminated",
            workflow_run_id=workflow_run_id,
            workflow_status="terminated",
            failure_reason=failure_reason,
        )

        # Add workflow terminated tag to Laminar trace
        TraceManager.add_task_completion_tag(WorkflowRunStatus.terminated)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def mark_workflow_run_as_canceled(self, workflow_run_id: str, run_with: str | None = None) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as canceled",
            workflow_run_id=workflow_run_id,
            workflow_status="canceled",
        )

        # Add workflow canceled tag to Laminar trace
        TraceManager.add_task_completion_tag(WorkflowRunStatus.canceled)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.canceled,
            run_with=run_with,
        )

    async def mark_workflow_run_as_timed_out(
        self,
        workflow_run_id: str,
        failure_reason: str | None = None,
        run_with: str | None = None,
    ) -> WorkflowRun:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as timed out",
            workflow_run_id=workflow_run_id,
            workflow_status="timed_out",
        )

        # Add workflow timed out tag to Laminar trace
        TraceManager.add_task_completion_tag(WorkflowRunStatus.timed_out)

        return await self._update_workflow_run_status(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.timed_out,
            failure_reason=failure_reason,
            run_with=run_with,
        )

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id)
        return workflow_run

    async def create_workflow_parameter(
        self,
        workflow_id: str,
        workflow_parameter_type: WorkflowParameterType,
        key: str,
        default_value: bool | int | float | str | dict | list | None = None,
        description: str | None = None,
    ) -> WorkflowParameter:
        return await app.DATABASE.create_workflow_parameter(
            workflow_id=workflow_id,
            workflow_parameter_type=workflow_parameter_type,
            key=key,
            description=description,
            default_value=default_value,
        )

    async def create_aws_secret_parameter(
        self, workflow_id: str, aws_key: str, key: str, description: str | None = None
    ) -> AWSSecretParameter:
        return await app.DATABASE.create_aws_secret_parameter(
            workflow_id=workflow_id, aws_key=aws_key, key=key, description=description
        )

    async def create_bitwarden_login_credential_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        key: str,
        url_parameter_key: str | None = None,
        description: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
    ) -> BitwardenLoginCredentialParameter:
        return await app.DATABASE.create_bitwarden_login_credential_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            key=key,
            url_parameter_key=url_parameter_key,
            description=description,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
        )

    async def create_credential_parameter(
        self,
        workflow_id: str,
        key: str,
        credential_id: str,
        description: str | None = None,
    ) -> CredentialParameter:
        return await app.DATABASE.create_credential_parameter(
            workflow_id=workflow_id,
            key=key,
            credential_id=credential_id,
            description=description,
        )

    async def create_onepassword_credential_parameter(
        self,
        workflow_id: str,
        key: str,
        vault_id: str,
        item_id: str,
        description: str | None = None,
    ) -> OnePasswordCredentialParameter:
        return await app.DATABASE.create_onepassword_credential_parameter(
            workflow_id=workflow_id,
            key=key,
            vault_id=vault_id,
            item_id=item_id,
            description=description,
        )

    async def create_azure_vault_credential_parameter(
        self,
        workflow_id: str,
        key: str,
        vault_name: str,
        username_key: str,
        password_key: str,
        totp_secret_key: str | None = None,
        description: str | None = None,
    ) -> AzureVaultCredentialParameter:
        return await app.DATABASE.create_azure_vault_credential_parameter(
            workflow_id=workflow_id,
            key=key,
            vault_name=vault_name,
            username_key=username_key,
            password_key=password_key,
            totp_secret_key=totp_secret_key,
            description=description,
        )

    async def create_bitwarden_sensitive_information_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_identity_key: str,
        bitwarden_identity_fields: list[str],
        key: str,
        description: str | None = None,
    ) -> BitwardenSensitiveInformationParameter:
        return await app.DATABASE.create_bitwarden_sensitive_information_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_identity_key=bitwarden_identity_key,
            bitwarden_identity_fields=bitwarden_identity_fields,
            key=key,
            description=description,
        )

    async def create_bitwarden_credit_card_data_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_item_id: str,
        key: str,
        description: str | None = None,
    ) -> BitwardenCreditCardDataParameter:
        return await app.DATABASE.create_bitwarden_credit_card_data_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            key=key,
            description=description,
        )

    async def create_output_parameter(
        self, workflow_id: str, key: str, description: str | None = None
    ) -> OutputParameter:
        return await app.DATABASE.create_output_parameter(workflow_id=workflow_id, key=key, description=description)

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        return await app.DATABASE.get_workflow_parameters(workflow_id=workflow_id)

    async def create_workflow_run_parameter(
        self,
        workflow_run_id: str,
        workflow_parameter: WorkflowParameter,
        value: Any,
    ) -> WorkflowRunParameter:
        value = json.dumps(value) if isinstance(value, (dict, list)) else value
        # InvalidWorkflowParameter will be raised if the validation fails
        workflow_parameter.workflow_parameter_type.convert_value(value)

        return await app.DATABASE.create_workflow_run_parameter(
            workflow_run_id=workflow_run_id,
            workflow_parameter=workflow_parameter,
            value=value,
        )

    async def get_workflow_run_parameter_tuples(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        return await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_workflow_output_parameters(workflow_id: str) -> list[OutputParameter]:
        return await app.DATABASE.get_workflow_output_parameters(workflow_id=workflow_id)

    @staticmethod
    async def get_workflow_run_output_parameters(
        workflow_run_id: str,
    ) -> list[WorkflowRunOutputParameter]:
        return await app.DATABASE.get_workflow_run_output_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_output_parameter_workflow_run_output_parameter_tuples(
        workflow_id: str,
        workflow_run_id: str,
    ) -> list[tuple[OutputParameter, WorkflowRunOutputParameter]]:
        workflow_run_output_parameters = await app.DATABASE.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
        output_parameters = await app.DATABASE.get_workflow_output_parameters_by_ids(
            output_parameter_ids=[
                workflow_run_output_parameter.output_parameter_id
                for workflow_run_output_parameter in workflow_run_output_parameters
            ]
        )

        return [
            (output_parameter, workflow_run_output_parameter)
            for workflow_run_output_parameter in workflow_run_output_parameters
            for output_parameter in output_parameters
            if output_parameter.output_parameter_id == workflow_run_output_parameter.output_parameter_id
        ]

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        return await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        return await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

    async def build_workflow_run_status_response_by_workflow_id(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        include_cost: bool = False,
    ) -> WorkflowRunResponseBase:
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        if workflow_run is None:
            LOG.error(f"Workflow run {workflow_run_id} not found")
            raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
        workflow_permanent_id = workflow_run.workflow_permanent_id
        return await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            include_cost=include_cost,
        )

    async def build_workflow_run_status_response(
        self,
        workflow_permanent_id: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        include_cost: bool = False,
    ) -> WorkflowRunResponseBase:
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id)
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found")
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)

        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)

        task_v2 = await app.DATABASE.get_task_v2_by_workflow_run_id(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        screenshot_artifacts = []
        screenshot_urls: list[str] | None = None
        # get the last screenshot for the last 3 tasks of the workflow run
        for task in workflow_run_tasks[::-1]:
            screenshot_artifact = await app.DATABASE.get_latest_artifact(
                task_id=task.task_id,
                artifact_types=[
                    ArtifactType.SCREENSHOT_ACTION,
                    ArtifactType.SCREENSHOT_FINAL,
                ],
                organization_id=organization_id,
            )
            if screenshot_artifact:
                screenshot_artifacts.append(screenshot_artifact)
            if len(screenshot_artifacts) >= 3:
                break
        if screenshot_artifacts:
            screenshot_urls = await app.ARTIFACT_MANAGER.get_share_links(screenshot_artifacts)

        recording_url = None
        recording_artifact = await app.DATABASE.get_artifact_for_run(
            run_id=task_v2.observer_cruise_id if task_v2 else workflow_run_id,
            artifact_type=ArtifactType.RECORDING,
            organization_id=organization_id,
        )
        if recording_artifact:
            recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        downloaded_files: list[FileInfo] = []
        downloaded_file_urls: list[str] | None = None
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                context = skyvern_context.current()
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    run_id=context.run_id if context and context.run_id else workflow_run.workflow_run_id,
                )
                if task_v2:
                    task_v2_downloaded_files = await app.STORAGE.get_downloaded_files(
                        organization_id=workflow_run.organization_id,
                        run_id=task_v2.observer_cruise_id,
                    )
                    if task_v2_downloaded_files:
                        downloaded_files.extend(task_v2_downloaded_files)
                if downloaded_files:
                    downloaded_file_urls = [file_info.url for file_info in downloaded_files]
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to get downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to get downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        workflow_parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters_with_value = {wfp.key: wfrp.value for wfp, wfrp in workflow_parameter_tuples}
        output_parameter_tuples: list[
            tuple[OutputParameter, WorkflowRunOutputParameter]
        ] = await self.get_output_parameter_workflow_run_output_parameter_tuples(
            workflow_id=workflow_run.workflow_id, workflow_run_id=workflow_run_id
        )

        outputs = None
        EXTRACTED_INFORMATION_KEY = "extracted_information"
        if output_parameter_tuples:
            outputs = {output_parameter.key: output.value for output_parameter, output in output_parameter_tuples}
            extracted_information: list[Any] = []
            for _, output in output_parameter_tuples:
                if output.value is not None:
                    extracted_information.extend(WorkflowService._collect_extracted_information(output.value))
            outputs[EXTRACTED_INFORMATION_KEY] = extracted_information

        errors: list[dict[str, Any]] = []
        for task in workflow_run_tasks:
            errors.extend(task.errors)

        total_steps = None
        total_cost = None
        if include_cost:
            workflow_run_steps = await app.DATABASE.get_steps_by_task_ids(
                task_ids=[task.task_id for task in workflow_run_tasks], organization_id=organization_id
            )
            workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
                workflow_run_id=workflow_run_id, organization_id=organization_id
            )
            text_prompt_blocks = [block for block in workflow_run_blocks if block.block_type == BlockType.TEXT_PROMPT]
            total_steps = len(workflow_run_steps)
            # TODO: This is a temporary cost calculation. We need to implement a more accurate cost calculation.
            # successful steps are the ones that have a status of completed and the total count of unique step.order
            successful_steps = [step for step in workflow_run_steps if step.status == StepStatus.completed]
            total_cost = 0.05 * (len(successful_steps) + len(text_prompt_blocks))
        return WorkflowRunResponseBase(
            workflow_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
            failure_reason=workflow_run.failure_reason,
            proxy_location=workflow_run.proxy_location,
            webhook_callback_url=workflow_run.webhook_callback_url,
            webhook_failure_reason=workflow_run.webhook_failure_reason,
            totp_verification_url=workflow_run.totp_verification_url,
            totp_identifier=workflow_run.totp_identifier,
            extra_http_headers=workflow_run.extra_http_headers,
            queued_at=workflow_run.queued_at,
            started_at=workflow_run.started_at,
            finished_at=workflow_run.finished_at,
            created_at=workflow_run.created_at,
            modified_at=workflow_run.modified_at,
            parameters=parameters_with_value,
            screenshot_urls=screenshot_urls,
            recording_url=recording_url,
            downloaded_files=downloaded_files,
            downloaded_file_urls=downloaded_file_urls,
            outputs=outputs,
            total_steps=total_steps,
            total_cost=total_cost,
            workflow_title=workflow.title,
            browser_session_id=workflow_run.browser_session_id,
            max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
            task_v2=task_v2,
            browser_address=workflow_run.browser_address,
            script_run=workflow_run.script_run,
            errors=errors,
        )

    async def clean_up_workflow(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        need_call_webhook: bool = True,
        browser_session_id: str | None = None,
    ) -> None:
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        tasks = await self.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id)
        all_workflow_task_ids = [task.task_id for task in tasks]
        close_browser_on_completion = (
            close_browser_on_completion and browser_session_id is None and not workflow_run.browser_address
        )
        browser_state = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
            workflow_run.workflow_run_id,
            all_workflow_task_ids,
            close_browser_on_completion=close_browser_on_completion,
            browser_session_id=browser_session_id,
            organization_id=workflow_run.organization_id,
        )
        if browser_state:
            await self.persist_video_data(browser_state, workflow, workflow_run)
            if tasks:
                await self.persist_debug_artifacts(browser_state, tasks[-1], workflow, workflow_run)
            if workflow.persist_browser_session and browser_state.browser_artifacts.browser_session_dir:
                await app.STORAGE.store_browser_session(
                    workflow_run.organization_id,
                    workflow.workflow_permanent_id,
                    browser_state.browser_artifacts.browser_session_dir,
                )
                LOG.info("Persisted browser session for workflow run", workflow_run_id=workflow_run.workflow_run_id)

        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(all_workflow_task_ids)

        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                context = skyvern_context.current()
                await app.STORAGE.save_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    run_id=context.run_id if context and context.run_id else workflow_run.workflow_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to save downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        if not need_call_webhook:
            return

        await self.execute_workflow_webhook(workflow_run, api_key)

    async def execute_workflow_webhook(
        self,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
    ) -> None:
        workflow_id = workflow_run.workflow_id
        workflow_run_status_response = await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        )
        LOG.info(
            "Built workflow run status response",
            workflow_run_status_response=workflow_run_status_response,
        )

        if not workflow_run.webhook_callback_url:
            LOG.warning(
                "Workflow has no webhook callback url. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        # build new schema for backward compatible webhook payload
        app_url = (
            f"{settings.SKYVERN_APP_URL.rstrip('/')}/workflows/"
            f"{workflow_run.workflow_permanent_id}/{workflow_run.workflow_run_id}"
        )
        workflow_run_response = WorkflowRunResponse(
            run_id=workflow_run.workflow_run_id,
            run_type=RunType.workflow_run,
            status=RunStatus(workflow_run_status_response.status),
            output=workflow_run_status_response.outputs,
            downloaded_files=workflow_run_status_response.downloaded_files,
            recording_url=workflow_run_status_response.recording_url,
            screenshot_urls=workflow_run_status_response.screenshot_urls,
            failure_reason=workflow_run_status_response.failure_reason,
            app_url=app_url,
            script_run=workflow_run_status_response.script_run,
            created_at=workflow_run_status_response.created_at,
            modified_at=workflow_run_status_response.modified_at,
            run_request=WorkflowRunRequest(
                workflow_id=workflow_run.workflow_permanent_id,
                title=workflow_run_status_response.workflow_title,
                parameters=workflow_run_status_response.parameters,
                proxy_location=workflow_run.proxy_location,
                webhook_url=workflow_run.webhook_callback_url or None,
                totp_url=workflow_run.totp_verification_url or None,
                totp_identifier=workflow_run.totp_identifier,
            ),
            errors=workflow_run_status_response.errors,
        )
        payload_dict = json.loads(workflow_run_status_response.model_dump_json())
        workflow_run_response_dict = json.loads(workflow_run_response.model_dump_json())
        payload_dict.update(workflow_run_response_dict)
        payload = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
        headers = generate_skyvern_webhook_headers(
            payload=payload,
            api_key=api_key,
        )
        LOG.info(
            "Sending webhook run status to webhook callback url",
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            payload=payload,
            headers=headers,
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url=workflow_run.webhook_callback_url, data=payload, headers=headers, timeout=httpx.Timeout(30.0)
                )
            if resp.status_code >= 200 and resp.status_code < 300:
                LOG.info(
                    "Webhook sent successfully",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_workflow_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_failure_reason="",
                )
            else:
                LOG.info(
                    "Webhook failed",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_data=payload,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_workflow_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_failure_reason=f"Webhook failed with status code {resp.status_code}, error message: {resp.text}",
                )
        except Exception as e:
            raise FailedToSendWebhook(
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            ) from e

    async def persist_video_data(
        self, browser_state: BrowserState, workflow: Workflow, workflow_run: WorkflowRun
    ) -> None:
        # Create recording artifact after closing the browser, so we can get an accurate recording
        video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        for video_artifact in video_artifacts:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=video_artifact.video_artifact_id,
                organization_id=workflow_run.organization_id,
                data=video_artifact.video_data,
            )

    async def persist_har_data(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if har_data:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.HAR,
                data=har_data,
            )

    async def persist_browser_console_log(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        browser_log = await app.BROWSER_MANAGER.get_browser_console_log(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if browser_log:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.BROWSER_CONSOLE_LOG,
                data=browser_log,
            )

    async def persist_tracing_data(
        self, browser_state: BrowserState, last_step: Step, workflow_run: WorkflowRun
    ) -> None:
        if browser_state.browser_context is None or browser_state.browser_artifacts.traces_dir is None:
            return

        trace_path = f"{browser_state.browser_artifacts.traces_dir}/{workflow_run.workflow_run_id}.zip"
        await app.ARTIFACT_MANAGER.create_artifact(step=last_step, artifact_type=ArtifactType.TRACE, path=trace_path)

    async def persist_debug_artifacts(
        self,
        browser_state: BrowserState,
        last_task: Task,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        last_step = await app.DATABASE.get_latest_step(
            task_id=last_task.task_id, organization_id=last_task.organization_id
        )
        if not last_step:
            return

        await self.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)
        await self.persist_har_data(browser_state, last_step, workflow, workflow_run)
        await self.persist_tracing_data(browser_state, last_step, workflow_run)

    async def make_workflow_definition(
        self,
        workflow_id: str,
        workflow_definition_yaml: WorkflowDefinitionYAML,
        title: str,
        organization_id: str,
    ) -> WorkflowDefinition:
        # Create parameters from the request
        parameters: dict[str, PARAMETER_TYPE] = {}
        duplicate_parameter_keys = set()

        # Check if user's trying to manually create an output parameter
        if any(parameter.parameter_type == ParameterType.OUTPUT for parameter in workflow_definition_yaml.parameters):
            raise InvalidWorkflowDefinition(message="Cannot manually create output parameters")

        # Check if any parameter keys collide with automatically created output parameter keys
        block_labels = [block.label for block in workflow_definition_yaml.blocks]
        # TODO (kerem): Check if block labels are unique
        output_parameter_keys = [f"{block_label}_output" for block_label in block_labels]
        parameter_keys = [parameter.key for parameter in workflow_definition_yaml.parameters]
        if any(key in output_parameter_keys for key in parameter_keys):
            raise WorkflowDefinitionHasReservedParameterKeys(
                reserved_keys=output_parameter_keys, parameter_keys=parameter_keys
            )

        if any(key in RESERVED_PARAMETER_KEYS for key in parameter_keys):
            raise WorkflowDefinitionHasReservedParameterKeys(
                reserved_keys=RESERVED_PARAMETER_KEYS,
                parameter_keys=parameter_keys,
            )

        # Create output parameters for all blocks
        block_output_parameters = await WorkflowService._create_all_output_parameters_for_workflow(
            workflow_id=workflow_id,
            block_yamls=workflow_definition_yaml.blocks,
        )
        for block_output_parameter in block_output_parameters.values():
            parameters[block_output_parameter.key] = block_output_parameter

        # We're going to process context parameters after other parameters since they depend on the other parameters
        context_parameter_yamls = []

        for parameter in workflow_definition_yaml.parameters:
            if parameter.key in parameters:
                LOG.error(f"Duplicate parameter key {parameter.key}")
                duplicate_parameter_keys.add(parameter.key)
                continue
            if parameter.parameter_type == ParameterType.AWS_SECRET:
                parameters[parameter.key] = await self.create_aws_secret_parameter(
                    workflow_id=workflow_id,
                    aws_key=parameter.aws_key,
                    key=parameter.key,
                    description=parameter.description,
                )
            elif parameter.parameter_type == ParameterType.CREDENTIAL:
                parameters[parameter.key] = await self.create_credential_parameter(
                    workflow_id=workflow_id,
                    key=parameter.key,
                    description=parameter.description,
                    credential_id=parameter.credential_id,
                )
            elif parameter.parameter_type == ParameterType.ONEPASSWORD:
                parameters[parameter.key] = await self.create_onepassword_credential_parameter(
                    workflow_id=workflow_id,
                    key=parameter.key,
                    description=parameter.description,
                    vault_id=parameter.vault_id,
                    item_id=parameter.item_id,
                )
            elif parameter.parameter_type == ParameterType.AZURE_VAULT_CREDENTIAL:
                parameters[parameter.key] = await self.create_azure_vault_credential_parameter(
                    workflow_id=workflow_id,
                    key=parameter.key,
                    description=parameter.description,
                    vault_name=parameter.vault_name,
                    username_key=parameter.username_key,
                    password_key=parameter.password_key,
                    totp_secret_key=parameter.totp_secret_key,
                )
            elif parameter.parameter_type == ParameterType.BITWARDEN_LOGIN_CREDENTIAL:
                if not parameter.bitwarden_collection_id and not parameter.bitwarden_item_id:
                    raise WorkflowParameterMissingRequiredValue(
                        workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                        workflow_parameter_key=parameter.key,
                        required_value="bitwarden_collection_id or bitwarden_item_id",
                    )
                if parameter.bitwarden_collection_id and not parameter.url_parameter_key:
                    raise WorkflowParameterMissingRequiredValue(
                        workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                        workflow_parameter_key=parameter.key,
                        required_value="url_parameter_key",
                    )
                parameters[parameter.key] = await self.create_bitwarden_login_credential_parameter(
                    workflow_id=workflow_id,
                    bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                    bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                    bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                    url_parameter_key=parameter.url_parameter_key,
                    key=parameter.key,
                    description=parameter.description,
                    bitwarden_collection_id=parameter.bitwarden_collection_id,
                    bitwarden_item_id=parameter.bitwarden_item_id,
                )
            elif parameter.parameter_type == ParameterType.BITWARDEN_SENSITIVE_INFORMATION:
                parameters[parameter.key] = await self.create_bitwarden_sensitive_information_parameter(
                    workflow_id=workflow_id,
                    bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                    bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                    bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                    # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                    bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                    bitwarden_identity_key=parameter.bitwarden_identity_key,
                    bitwarden_identity_fields=parameter.bitwarden_identity_fields,
                    key=parameter.key,
                    description=parameter.description,
                )
            elif parameter.parameter_type == ParameterType.BITWARDEN_CREDIT_CARD_DATA:
                parameters[parameter.key] = await self.create_bitwarden_credit_card_data_parameter(
                    workflow_id=workflow_id,
                    bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                    bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                    bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                    # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                    bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                    bitwarden_item_id=parameter.bitwarden_item_id,  # type: ignore
                    key=parameter.key,
                    description=parameter.description,
                )
            elif parameter.parameter_type == ParameterType.WORKFLOW:
                parameters[parameter.key] = await self.create_workflow_parameter(
                    workflow_id=workflow_id,
                    workflow_parameter_type=parameter.workflow_parameter_type,
                    key=parameter.key,
                    default_value=parameter.default_value,
                    description=parameter.description,
                )
            elif parameter.parameter_type == ParameterType.OUTPUT:
                parameters[parameter.key] = await self.create_output_parameter(
                    workflow_id=workflow_id,
                    key=parameter.key,
                    description=parameter.description,
                )
            elif parameter.parameter_type == ParameterType.CONTEXT:
                context_parameter_yamls.append(parameter)
            else:
                LOG.error(f"Invalid parameter type {parameter.parameter_type}")

        # Now we can process the context parameters since all other parameters have been created
        for context_parameter in context_parameter_yamls:
            if context_parameter.source_parameter_key not in parameters:
                raise ContextParameterSourceNotDefined(
                    context_parameter_key=context_parameter.key,
                    source_key=context_parameter.source_parameter_key,
                )

            if context_parameter.key in parameters:
                LOG.error(f"Duplicate parameter key {context_parameter.key}")
                duplicate_parameter_keys.add(context_parameter.key)
                continue

            # We're only adding the context parameter to the parameters dict, we're not creating it in the database
            # It'll only be stored in the `workflow.workflow_definition`
            # todo (kerem): should we have a database table for context parameters?
            parameters[context_parameter.key] = ContextParameter(
                key=context_parameter.key,
                description=context_parameter.description,
                source=parameters[context_parameter.source_parameter_key],
                # Context parameters don't have a default value, the value always depends on the source parameter
                value=None,
            )

        if duplicate_parameter_keys:
            raise WorkflowDefinitionHasDuplicateParameterKeys(duplicate_keys=duplicate_parameter_keys)
        # Create blocks from the request
        block_label_mapping = {}
        blocks: list[BlockTypeVar] = []
        for block_yaml in workflow_definition_yaml.blocks:
            block = await self.block_yaml_to_block(block_yaml, parameters)
            blocks.append(block)
            block_label_mapping[block.label] = block

        # Set the blocks for the workflow definition
        workflow_definition = WorkflowDefinition(parameters=parameters.values(), blocks=blocks)

        LOG.info(
            f"Created workflow from request, title: {title}",
            parameter_keys=[parameter.key for parameter in parameters.values()],
            block_labels=[block.label for block in blocks],
            organization_id=organization_id,
            title=title,
            workflow_id=workflow_id,
        )

        return workflow_definition

    async def create_workflow_from_request(
        self,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str | None = None,
        delete_script: bool = True,
        delete_code_cache_is_ok: bool = True,
    ) -> Workflow:
        organization_id = organization.organization_id
        LOG.info(
            "Creating workflow from request",
            organization_id=organization_id,
            title=request.title,
        )
        new_workflow_id: str | None = None

        try:
            if workflow_permanent_id:
                existing_latest_workflow = await self.get_workflow_by_permanent_id(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    exclude_deleted=False,
                )
                existing_version = existing_latest_workflow.version

                # NOTE: it's only potential, as it may be immediately deleted!
                potential_workflow = await self.create_workflow(
                    title=request.title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    extra_http_headers=request.extra_http_headers,
                    workflow_permanent_id=workflow_permanent_id,
                    version=existing_version + 1,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                    run_with=request.run_with,
                    cache_key=request.cache_key,
                    ai_fallback=request.ai_fallback,
                    run_sequentially=request.run_sequentially,
                    sequential_key=request.sequential_key,
                )
            else:
                # NOTE: it's only potential, as it may be immediately deleted!
                potential_workflow = await self.create_workflow(
                    title=request.title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    model=request.model,
                    max_screenshot_scrolling_times=request.max_screenshot_scrolls,
                    extra_http_headers=request.extra_http_headers,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                    run_with=request.run_with,
                    cache_key=request.cache_key,
                    ai_fallback=request.ai_fallback,
                    run_sequentially=request.run_sequentially,
                    sequential_key=request.sequential_key,
                )
            # Keeping track of the new workflow id to delete it if an error occurs during the creation process
            new_workflow_id = potential_workflow.workflow_id

            workflow_definition = await self.make_workflow_definition(
                potential_workflow.workflow_id,
                request.workflow_definition,
                request.title,
                organization_id,
            )

            updated_workflow = await self.update_workflow_definition(
                workflow_id=potential_workflow.workflow_id,
                organization_id=organization_id,
                title=request.title,
                description=request.description,
                workflow_definition=workflow_definition,
            )

            await self.maybe_delete_cached_code(
                updated_workflow,
                workflow_definition=workflow_definition,
                organization_id=organization_id,
                delete_script=delete_script,
                delete_code_cache_is_ok=delete_code_cache_is_ok,
            )

            return updated_workflow
        except Exception as e:
            if new_workflow_id:
                LOG.error(
                    f"Failed to create workflow from request, deleting workflow {new_workflow_id}",
                    organization_id=organization_id,
                )
                await self.delete_workflow_by_id(workflow_id=new_workflow_id, organization_id=organization_id)
            else:
                LOG.exception(f"Failed to create workflow from request, title: {request.title}")
            raise e

    @staticmethod
    async def create_output_parameter_for_block(workflow_id: str, block_yaml: BLOCK_YAML_TYPES) -> OutputParameter:
        output_parameter_key = f"{block_yaml.label}_output"
        return await app.DATABASE.create_output_parameter(
            workflow_id=workflow_id,
            key=output_parameter_key,
            description=f"Output parameter for block {block_yaml.label}",
        )

    @staticmethod
    async def _create_all_output_parameters_for_workflow(
        workflow_id: str, block_yamls: list[BLOCK_YAML_TYPES]
    ) -> dict[str, OutputParameter]:
        output_parameters = {}
        for block_yaml in block_yamls:
            output_parameter = await WorkflowService.create_output_parameter_for_block(
                workflow_id=workflow_id, block_yaml=block_yaml
            )
            output_parameters[block_yaml.label] = output_parameter
            # Recursively create output parameters for for loop blocks
            if isinstance(block_yaml, ForLoopBlockYAML):
                output_parameters.update(
                    await WorkflowService._create_all_output_parameters_for_workflow(
                        workflow_id=workflow_id, block_yamls=block_yaml.loop_blocks
                    )
                )
        return output_parameters

    @staticmethod
    async def block_yaml_to_block(
        block_yaml: BLOCK_YAML_TYPES,
        parameters: dict[str, PARAMETER_TYPE],
    ) -> BlockTypeVar:
        output_parameter = cast(OutputParameter, parameters[f"{block_yaml.label}_output"])
        if block_yaml.block_type == BlockType.TASK:
            task_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return TaskBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                parameters=task_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                data_extraction_goal=block_yaml.data_extraction_goal,
                data_schema=block_yaml.data_schema,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
                complete_verification=block_yaml.complete_verification,
                include_action_history_in_verification=block_yaml.include_action_history_in_verification,
            )
        elif block_yaml.block_type == BlockType.FOR_LOOP:
            loop_blocks = [
                await WorkflowService.block_yaml_to_block(loop_block, parameters)
                for loop_block in block_yaml.loop_blocks
            ]

            loop_over_parameter: Parameter | None = None
            if block_yaml.loop_over_parameter_key:
                loop_over_parameter = parameters[block_yaml.loop_over_parameter_key]

            if block_yaml.loop_variable_reference:
                # it's backaward compatible with jinja style parameter and context paramter
                # we trim the format like {{ loop_key }} into loop_key to initialize the context parater,
                # otherwise it might break the context parameter initialization chain, blow up the worklofw parameters
                # TODO: consider remove this if we totally give up context parameter
                trimmed_key = block_yaml.loop_variable_reference.strip(" {}")
                if trimmed_key in parameters:
                    loop_over_parameter = parameters[trimmed_key]

            if loop_over_parameter is None and not block_yaml.loop_variable_reference:
                raise Exception("Loop value parameter is required for for loop block")

            return ForLoopBlock(
                label=block_yaml.label,
                loop_over=loop_over_parameter,
                loop_variable_reference=block_yaml.loop_variable_reference,
                loop_blocks=loop_blocks,
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
                complete_if_empty=block_yaml.complete_if_empty,
            )
        elif block_yaml.block_type == BlockType.CODE:
            return CodeBlock(
                label=block_yaml.label,
                code=block_yaml.code,
                parameters=(
                    [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                    if block_yaml.parameter_keys
                    else []
                ),
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.TEXT_PROMPT:
            return TextPromptBlock(
                label=block_yaml.label,
                llm_key=block_yaml.llm_key,
                prompt=block_yaml.prompt,
                parameters=(
                    [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                    if block_yaml.parameter_keys
                    else []
                ),
                json_schema=block_yaml.json_schema,
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
                model=block_yaml.model,
            )
        elif block_yaml.block_type == BlockType.DOWNLOAD_TO_S3:
            return DownloadToS3Block(
                label=block_yaml.label,
                output_parameter=output_parameter,
                url=block_yaml.url,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.UPLOAD_TO_S3:
            return UploadToS3Block(
                label=block_yaml.label,
                output_parameter=output_parameter,
                path=block_yaml.path,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.FILE_UPLOAD:
            return FileUploadBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                storage_type=block_yaml.storage_type,
                s3_bucket=block_yaml.s3_bucket,
                aws_access_key_id=block_yaml.aws_access_key_id,
                aws_secret_access_key=block_yaml.aws_secret_access_key,
                region_name=block_yaml.region_name,
                azure_storage_account_name=block_yaml.azure_storage_account_name,
                azure_storage_account_key=block_yaml.azure_storage_account_key,
                azure_blob_container_name=block_yaml.azure_blob_container_name,
                path=block_yaml.path,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.SEND_EMAIL:
            return SendEmailBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                smtp_host=parameters[block_yaml.smtp_host_secret_parameter_key],
                smtp_port=parameters[block_yaml.smtp_port_secret_parameter_key],
                smtp_username=parameters[block_yaml.smtp_username_secret_parameter_key],
                smtp_password=parameters[block_yaml.smtp_password_secret_parameter_key],
                sender=block_yaml.sender,
                recipients=block_yaml.recipients,
                subject=block_yaml.subject,
                body=block_yaml.body,
                file_attachments=block_yaml.file_attachments or [],
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.FILE_URL_PARSER:
            return FileParserBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                file_url=block_yaml.file_url,
                file_type=block_yaml.file_type,
                json_schema=block_yaml.json_schema,
                continue_on_failure=block_yaml.continue_on_failure,
                model=block_yaml.model,
            )
        elif block_yaml.block_type == BlockType.PDF_PARSER:
            return PDFParserBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                file_url=block_yaml.file_url,
                json_schema=block_yaml.json_schema,
                continue_on_failure=block_yaml.continue_on_failure,
                model=block_yaml.model,
            )
        elif block_yaml.block_type == BlockType.VALIDATION:
            validation_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )

            if not block_yaml.complete_criterion and not block_yaml.terminate_criterion:
                raise Exception("Both complete criterion and terminate criterion are empty")

            return ValidationBlock(
                label=block_yaml.label,
                task_type=TaskType.validation,
                parameters=validation_block_parameters,
                output_parameter=output_parameter,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
                error_code_mapping=block_yaml.error_code_mapping,
                continue_on_failure=block_yaml.continue_on_failure,
                # Should only need one step for validation block, but we allow 2 in case the LLM has an unexpected failure and we need to retry.
                max_steps_per_run=2,
                disable_cache=block_yaml.disable_cache,
                model=block_yaml.model,
            )

        elif block_yaml.block_type == BlockType.ACTION:
            action_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )

            if not block_yaml.navigation_goal:
                raise Exception("empty action instruction")

            return ActionBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                task_type=TaskType.action,
                parameters=action_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                # DO NOT run complete verification for action block
                complete_verification=False,
                max_steps_per_run=1,
            )

        elif block_yaml.block_type == BlockType.NAVIGATION:
            navigation_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return NavigationBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                parameters=navigation_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
                complete_verification=block_yaml.complete_verification,
                include_action_history_in_verification=block_yaml.include_action_history_in_verification,
            )

        elif block_yaml.block_type == BlockType.EXTRACTION:
            extraction_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return ExtractionBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                parameters=extraction_block_parameters,
                output_parameter=output_parameter,
                data_extraction_goal=block_yaml.data_extraction_goal,
                data_schema=block_yaml.data_schema,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                continue_on_failure=block_yaml.continue_on_failure,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                complete_verification=False,
            )

        elif block_yaml.block_type == BlockType.LOGIN:
            login_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return LoginBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                parameters=login_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
                complete_verification=block_yaml.complete_verification,
            )

        elif block_yaml.block_type == BlockType.WAIT:
            if block_yaml.wait_sec <= 0 or block_yaml.wait_sec > settings.WORKFLOW_WAIT_BLOCK_MAX_SEC:
                raise InvalidWaitBlockTime(settings.WORKFLOW_WAIT_BLOCK_MAX_SEC)

            return WaitBlock(
                label=block_yaml.label,
                wait_sec=block_yaml.wait_sec,
                continue_on_failure=block_yaml.continue_on_failure,
                output_parameter=output_parameter,
            )

        elif block_yaml.block_type == BlockType.FILE_DOWNLOAD:
            file_download_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return FileDownloadBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                engine=block_yaml.engine,
                parameters=file_download_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                model=block_yaml.model,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                disable_cache=block_yaml.disable_cache,
                complete_on_download=True,
                complete_verification=True,
                download_timeout=block_yaml.download_timeout,
            )
        elif block_yaml.block_type == BlockType.TaskV2:
            return TaskV2Block(
                label=block_yaml.label,
                prompt=block_yaml.prompt,
                url=block_yaml.url,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                max_iterations=block_yaml.max_iterations,
                max_steps=block_yaml.max_steps,
                disable_cache=block_yaml.disable_cache,
                model=block_yaml.model,
                output_parameter=output_parameter,
            )
        elif block_yaml.block_type == BlockType.HTTP_REQUEST:
            http_request_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return HttpRequestBlock(
                label=block_yaml.label,
                method=block_yaml.method,
                url=block_yaml.url,
                headers=block_yaml.headers,
                body=block_yaml.body,
                timeout=block_yaml.timeout,
                follow_redirects=block_yaml.follow_redirects,
                parameters=http_request_block_parameters,
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.GOTO_URL:
            return UrlBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                output_parameter=output_parameter,
                complete_verification=False,
            )

        raise ValueError(f"Invalid block type {block_yaml.block_type}")

    async def create_empty_workflow(
        self,
        organization: Organization,
        title: str,
        proxy_location: ProxyLocation | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        run_with: str | None = None,
        status: WorkflowStatus = WorkflowStatus.published,
    ) -> Workflow:
        """
        Create a blank workflow with no blocks
        """
        # create a new workflow
        workflow_create_request = WorkflowCreateYAMLRequest(
            title=title,
            workflow_definition=WorkflowDefinitionYAML(
                parameters=[],
                blocks=[],
            ),
            proxy_location=proxy_location,
            status=status,
            max_screenshot_scrolls=max_screenshot_scrolling_times,
            extra_http_headers=extra_http_headers,
            run_with=run_with,
        )
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=organization,
            request=workflow_create_request,
        )

    async def get_workflow_run_timeline(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[WorkflowRunTimeline]:
        """
        build the tree structure of the workflow run timeline
        """
        workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get all the actions for all workflow run blocks
        task_ids = [block.task_id for block in workflow_run_blocks if block.task_id]
        task_id_to_block: dict[str, WorkflowRunBlock] = {
            block.task_id: block for block in workflow_run_blocks if block.task_id
        }
        actions = await app.DATABASE.get_tasks_actions(task_ids=task_ids, organization_id=organization_id)
        for action in actions:
            if not action.task_id:
                continue
            task_block = task_id_to_block[action.task_id]
            task_block.actions.append(action)

        result = []
        block_map: dict[str, WorkflowRunTimeline] = {}
        counter = 0
        while workflow_run_blocks:
            counter += 1
            block = workflow_run_blocks.pop(0)
            workflow_run_timeline = WorkflowRunTimeline(
                type=WorkflowRunTimelineType.block,
                block=block,
                created_at=block.created_at,
                modified_at=block.modified_at,
            )
            if block.parent_workflow_run_block_id:
                if block.parent_workflow_run_block_id in block_map:
                    block_map[block.parent_workflow_run_block_id].children.append(workflow_run_timeline)
                    block_map[block.workflow_run_block_id] = workflow_run_timeline
                else:
                    # put the block back to the queue
                    workflow_run_blocks.append(block)
            else:
                result.append(workflow_run_timeline)
                block_map[block.workflow_run_block_id] = workflow_run_timeline

            if counter > 1000:
                LOG.error("Too many blocks in the workflow run", workflow_run_id=workflow_run_id)
                break

        return result

    async def generate_script_if_needed(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        block_labels: list[str] | None = None,
    ) -> None:
        code_gen = workflow_run.code_gen
        LOG.info(
            "Generate script?",
            block_labels=block_labels,
            code_gen=code_gen,
            workflow_run_id=workflow_run.workflow_run_id,
        )

        if block_labels and not code_gen:
            # Do not generate script if block_labels is provided, and an explicit code_gen
            # request is not made
            return None

        existing_script, rendered_cache_key_value = await workflow_script_service.get_workflow_script(
            workflow,
            workflow_run,
            block_labels,
        )
        if existing_script:
            LOG.info(
                "Found cached script for workflow. Skipping script generation",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                cache_key_value=rendered_cache_key_value,
                script_id=existing_script.script_id,
                script_revision_id=existing_script.script_revision_id,
            )
            return

        created_script = await app.DATABASE.create_script(
            organization_id=workflow.organization_id,
            run_id=workflow_run.workflow_run_id,
        )

        await workflow_script_service.generate_workflow_script(
            workflow_run=workflow_run,
            workflow=workflow,
            script=created_script,
            rendered_cache_key_value=rendered_cache_key_value,
        )

    def should_run_script(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> bool:
        if workflow_run.run_with == "code":
            return True
        if workflow_run.run_with == "agent":
            return False
        if workflow.run_with == "code":
            return True
        return False
