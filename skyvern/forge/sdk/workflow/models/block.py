import abc
import json
import os
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal, Union

import filetype
import structlog
from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.exceptions import (
    ContextParameterValueNotFound,
    MissingBrowserStatePage,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.files import download_file, get_path_for_workflow_download_directory
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.schemas.tasks import TaskOutput, TaskStatus
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import InvalidEmailClientConfiguration, NoValidEmailRecipient
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    ContextParameter,
    OutputParameter,
    WorkflowParameter,
)

LOG = structlog.get_logger()


class BlockType(StrEnum):
    TASK = "task"
    FOR_LOOP = "for_loop"
    CODE = "code"
    TEXT_PROMPT = "text_prompt"
    DOWNLOAD_TO_S3 = "download_to_s3"
    UPLOAD_TO_S3 = "upload_to_s3"
    SEND_EMAIL = "send_email"


@dataclass(frozen=True)
class BlockResult:
    success: bool
    output_parameter: OutputParameter
    output_parameter_value: dict[str, Any] | list | str | None = None


class Block(BaseModel, abc.ABC):
    # Must be unique within workflow definition
    label: str
    block_type: BlockType
    output_parameter: OutputParameter
    continue_on_failure: bool = False

    async def record_output_parameter_value(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        value: dict[str, Any] | list | str | None = None,
    ) -> None:
        if workflow_run_context.has_value(self.output_parameter.key):
            LOG.warning(
                "Output parameter value already recorded",
                output_parameter_id=self.output_parameter.output_parameter_id,
                workflow_run_id=workflow_run_id,
            )
            return

        await workflow_run_context.register_output_parameter_value_post_execution(
            parameter=self.output_parameter,
            value=value,
        )
        await app.DATABASE.create_workflow_run_output_parameter(
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
            value=value,
        )
        LOG.info(
            "Registered output parameter value",
            output_parameter_id=self.output_parameter.output_parameter_id,
            workflow_run_id=workflow_run_id,
        )

    def build_block_result(
        self,
        success: bool,
        output_parameter_value: dict[str, Any] | list | str | None = None,
    ) -> BlockResult:
        return BlockResult(
            success=success,
            output_parameter=self.output_parameter,
            output_parameter_value=output_parameter_value,
        )

    @classmethod
    def get_subclasses(cls) -> tuple[type["Block"], ...]:
        return tuple(cls.__subclasses__())

    @staticmethod
    def get_workflow_run_context(workflow_run_id: str) -> WorkflowRunContext:
        return app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)

    @staticmethod
    def get_async_aws_client() -> AsyncAWSClient:
        return app.WORKFLOW_CONTEXT_MANAGER.aws_client

    @abc.abstractmethod
    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        pass

    async def execute_safe(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        try:
            return await self.execute(workflow_run_id, **kwargs)
        except Exception:
            LOG.exception(
                "Block execution failed",
                workflow_run_id=workflow_run_id,
                block_label=self.label,
                block_type=self.block_type,
            )
            # Record output parameter value if it hasn't been recorded yet
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            if not workflow_run_context.has_value(self.output_parameter.key):
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id)
            return self.build_block_result(success=False)

    @abc.abstractmethod
    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        pass


class TaskBlock(Block):
    block_type: Literal[BlockType.TASK] = BlockType.TASK

    url: str | None = None
    title: str = "Untitled Task"
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | None = None
    # error code to error description for the LLM
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameters: list[PARAMETER_TYPE] = []
    complete_on_download: bool = False

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = self.parameters
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            if workflow_run_context.has_value(self.url):
                LOG.info(
                    "Task URL is parameterized, using parameter value",
                    task_url_parameter_value=workflow_run_context.get_value(self.url),
                    task_url_parameter_key=self.url,
                )
                self.url = workflow_run_context.get_value(self.url)
            else:
                # if the parameter is not resolved yet, we'll add it to the list of parameters to resolve
                # parameterization of the url would happen when the task is executed
                parameters.append(workflow_run_context.get_parameter(self.url))

        return parameters

    @staticmethod
    async def get_task_order(workflow_run_id: str, current_retry: int) -> tuple[int, int]:
        """
        Returns the order and retry for the next task in the workflow run as a tuple.
        """
        last_task_for_workflow_run = await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)
        # If there is no previous task, the order will be 0 and the retry will be 0.
        if last_task_for_workflow_run is None:
            return 0, 0
        # If there is a previous task but the current retry is 0, the order will be the order of the last task + 1
        # and the retry will be 0.
        order = last_task_for_workflow_run.order or 0
        if current_retry == 0:
            return order + 1, 0
        # If there is a previous task and the current retry is not 0, the order will be the order of the last task
        # and the retry will be the retry of the last task + 1. (There is a validation that makes sure the retry
        # of the last task is equal to current_retry - 1) if it is not, we use last task retry + 1.
        retry = last_task_for_workflow_run.retry or 0
        if retry + 1 != current_retry:
            LOG.error(
                f"Last task for workflow run is retry number {last_task_for_workflow_run.retry}, "
                f"but current retry is {current_retry}. Could be race condition. Using last task retry + 1",
                workflow_run_id=workflow_run_id,
                last_task_id=last_task_for_workflow_run.task_id,
                last_task_retry=last_task_for_workflow_run.retry,
                current_retry=current_retry,
            )

        return order, retry + 1

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        current_retry = 0
        # initial value for will_retry is True, so that the loop runs at least once
        will_retry = True
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id=workflow_run_id)
        workflow = await app.WORKFLOW_SERVICE.get_workflow(workflow_id=workflow_run.workflow_id)
        # if the task url is parameterized, we need to get the value from the workflow run context
        if self.url and workflow_run_context.has_parameter(self.url) and workflow_run_context.has_value(self.url):
            task_url_parameter_value = workflow_run_context.get_value(self.url)
            if task_url_parameter_value:
                LOG.info(
                    "Task URL is parameterized, using parameter value",
                    task_url_parameter_value=task_url_parameter_value,
                    task_url_parameter_key=self.url,
                )
                self.url = task_url_parameter_value

        # TODO (kerem) we should always retry on terminated. We should make a distinction between retriable and
        # non-retryable terminations
        while will_retry:
            task_order, task_retry = await self.get_task_order(workflow_run_id, current_retry)
            task, step = await app.agent.create_task_and_step_from_block(
                task_block=self,
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_context=workflow_run_context,
                task_order=task_order,
                task_retry=task_retry,
            )
            organization = await app.DATABASE.get_organization(organization_id=workflow.organization_id)
            if not organization:
                raise Exception(f"Organization is missing organization_id={workflow.organization_id}")
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                workflow_run=workflow_run, url=self.url
            )
            if not browser_state.page:
                LOG.error(
                    "BrowserState has no page",
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)

            LOG.info(
                "Navigating to page",
                url=self.url,
                workflow_run_id=workflow_run_id,
                task_id=task.task_id,
                workflow_id=workflow.workflow_id,
                organization_id=workflow.organization_id,
                step_id=step.step_id,
            )

            if self.url:
                await browser_state.page.goto(self.url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)

            try:
                await app.agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    workflow_run=workflow_run,
                    complete_on_download=self.complete_on_download,
                )
            except Exception as e:
                # Make sure the task is marked as failed in the database before raising the exception
                await app.DATABASE.update_task(
                    task.task_id,
                    status=TaskStatus.failed,
                    organization_id=workflow.organization_id,
                    failure_reason=str(e),
                )
                raise e

            # Check task status
            updated_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=workflow.organization_id)
            if not updated_task:
                raise TaskNotFound(task.task_id)
            if not updated_task.status.is_final():
                raise UnexpectedTaskStatus(task_id=updated_task.task_id, status=updated_task.status)

            if updated_task.status == TaskStatus.completed or updated_task.status == TaskStatus.terminated:
                LOG.info(
                    "Task completed",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow.organization_id,
                )
                success = updated_task.status == TaskStatus.completed
                task_output = TaskOutput.from_task(updated_task)
                output_parameter_value = task_output.model_dump()
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
                return self.build_block_result(success=success, output_parameter_value=output_parameter_value)
            else:
                current_retry += 1
                will_retry = current_retry <= self.max_retries
                retry_message = f", retrying task {current_retry}/{self.max_retries}" if will_retry else ""
                task_output = TaskOutput.from_task(updated_task)
                LOG.warning(
                    f"Task failed with status {updated_task.status}{retry_message}",
                    task_id=updated_task.task_id,
                    status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow.organization_id,
                    current_retry=current_retry,
                    max_retries=self.max_retries,
                    task_output=task_output.model_dump_json(),
                )
                if not will_retry:
                    output_parameter_value = task_output.model_dump()
                    await self.record_output_parameter_value(
                        workflow_run_context, workflow_run_id, output_parameter_value
                    )
                    return self.build_block_result(success=False, output_parameter_value=output_parameter_value)

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id)
        return self.build_block_result(success=False)


class ForLoopBlock(Block):
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP

    loop_over: PARAMETER_TYPE
    loop_blocks: list["BlockTypeVar"]

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = {self.loop_over}

        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def get_loop_block_context_parameters(self, workflow_run_id: str, loop_data: Any) -> list[ContextParameter]:
        if not isinstance(loop_data, dict):
            # TODO (kerem): Should we add support for other types?
            raise ValueError("loop_data should be a dict")

        context_parameters = []
        for loop_block in self.loop_blocks:
            # todo: handle the case where the loop_block is a ForLoopBlock

            all_parameters = loop_block.get_all_parameters(workflow_run_id)
            for parameter in all_parameters:
                if isinstance(parameter, ContextParameter):
                    context_parameters.append(parameter)

        for context_parameter in context_parameters:
            if context_parameter.source.key != self.loop_over.key:
                continue
            if context_parameter.key not in loop_data:
                raise ContextParameterValueNotFound(
                    parameter_key=context_parameter.key,
                    existing_keys=list(loop_data.keys()),
                    workflow_run_id=workflow_run_id,
                )
            context_parameter.value = loop_data[context_parameter.key]

        return context_parameters

    def get_loop_over_parameter_values(self, workflow_run_context: WorkflowRunContext) -> list[Any]:
        if isinstance(self.loop_over, WorkflowParameter):
            parameter_value = workflow_run_context.get_value(self.loop_over.key)
        elif isinstance(self.loop_over, OutputParameter):
            # If the output parameter is for a TaskBlock, it will be a TaskOutput object. We need to extract the
            # value from the TaskOutput object's extracted_information field.
            output_parameter_value = workflow_run_context.get_value(self.loop_over.key)
            if isinstance(output_parameter_value, dict) and "extracted_information" in output_parameter_value:
                parameter_value = output_parameter_value["extracted_information"]
            else:
                parameter_value = output_parameter_value
        elif isinstance(self.loop_over, ContextParameter):
            parameter_value = self.loop_over.value
            if not parameter_value:
                source_parameter_value = workflow_run_context.get_value(self.loop_over.source.key)
                if isinstance(source_parameter_value, dict):
                    if "extracted_information" in source_parameter_value:
                        parameter_value = source_parameter_value["extracted_information"].get(self.loop_over.key)
                    else:
                        parameter_value = source_parameter_value.get(self.loop_over.key)
                else:
                    raise ValueError("ContextParameter source value should be a dict")
        else:
            raise NotImplementedError

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            # TODO (kerem): Should we raise an error here?
            return [parameter_value]

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        outputs_with_loop_values = []
        success = False
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        loop_over_values = self.get_loop_over_parameter_values(workflow_run_context)
        LOG.info(
            f"Number of loop_over values: {len(loop_over_values)}",
            block_type=self.block_type,
            workflow_run_id=workflow_run_id,
            num_loop_over_values=len(loop_over_values),
        )
        if not loop_over_values or len(loop_over_values) == 0:
            LOG.info(
                "No loop_over values found",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_over_values=len(loop_over_values),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return self.build_block_result(success=False)
        for loop_idx, loop_over_value in enumerate(loop_over_values):
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)
            block_outputs = []
            for block_idx, loop_block in enumerate(self.loop_blocks):
                block_output = await loop_block.execute_safe(workflow_run_id=workflow_run_id)
                block_outputs.append(block_output)
                if not block_output.success and not loop_block.continue_on_failure:
                    LOG.info(
                        f"ForLoopBlock: Encountered an failure processing block {block_idx} during loop {loop_idx}, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                    )
                    break

            outputs_with_loop_values.append(
                [
                    {
                        "loop_value": loop_over_value,
                        "output_parameter": block_output.output_parameter,
                        "output_value": workflow_run_context.get_value(block_output.output_parameter.key),
                    }
                    for block_output in block_outputs
                    if block_output.output_parameter
                ]
            )

            # If all block outputs are successful, the loop is successful. If self.continue_on_failure is True, we will
            # continue to the next loop iteration even if there are failures.
            success = all([block_output.success for block_output in block_outputs])
            if not success and not self.continue_on_failure:
                LOG.info(
                    f"ForLoopBlock: Encountered an failure processing loop {loop_idx}, won't continue to the next loop. Total number of loops: {len(loop_over_values)}",
                    for_loop_continue_on_failure=self.continue_on_failure,
                )
                break

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, outputs_with_loop_values)
        return self.build_block_result(success=success, output_parameter_value=outputs_with_loop_values)


class CodeBlock(Block):
    block_type: Literal[BlockType.CODE] = BlockType.CODE

    code: str
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        parameter_values = {}
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            secret_value = workflow_run_context.get_original_secret_value_or_none(value)
            if secret_value is not None:
                parameter_values[parameter.key] = secret_value
            else:
                parameter_values[parameter.key] = value

        local_variables: dict[str, Any] = {}
        exec(self.code, parameter_values, local_variables)
        result = {"result": local_variables.get("result")}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result)
        return self.build_block_result(success=True, output_parameter_value=result)


class TextPromptBlock(Block):
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT

    llm_key: str
    prompt: str
    parameters: list[PARAMETER_TYPE] = []
    json_schema: dict[str, Any] | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def send_prompt(self, prompt: str, parameter_values: dict[str, Any]) -> dict[str, Any]:
        llm_api_handler = LLMAPIHandlerFactory.get_llm_api_handler(self.llm_key)
        if not self.json_schema:
            self.json_schema = {
                "type": "object",
                "properties": {
                    "llm_response": {
                        "type": "string",
                        "description": "Your response to the prompt",
                    }
                },
            }

        prompt = prompt_engine.load_prompt_from_string(prompt, **parameter_values)
        prompt += (
            "\n\n"
            + "Please respond to the prompt above using the following JSON definition:\n\n"
            + "```json\n"
            + json.dumps(self.json_schema, indent=2)
            + "\n```\n\n"
        )
        LOG.info(
            "TextPromptBlock: Sending prompt to LLM",
            prompt=prompt,
            llm_key=self.llm_key,
        )
        response = await llm_api_handler(prompt=prompt)
        LOG.info("TextPromptBlock: Received response from LLM", response=response)
        return response

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        parameter_values = {}
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            secret_value = workflow_run_context.get_original_secret_value_or_none(value)
            if secret_value is not None:
                parameter_values[parameter.key] = secret_value
            else:
                parameter_values[parameter.key] = value

        response = await self.send_prompt(self.prompt, parameter_values)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response)
        return self.build_block_result(success=True, output_parameter_value=response)


class DownloadToS3Block(Block):
    block_type: Literal[BlockType.DOWNLOAD_TO_S3] = BlockType.DOWNLOAD_TO_S3

    url: str

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            return [workflow_run_context.get_parameter(self.url)]

        return []

    async def _upload_file_to_s3(self, uri: str, file_path: str) -> None:
        try:
            client = self.get_async_aws_client()
            await client.upload_file_from_path(uri=uri, file_path=file_path)
        finally:
            # Clean up the temporary file since it's created with delete=False
            os.unlink(file_path)

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.url and workflow_run_context.has_parameter(self.url) and workflow_run_context.has_value(self.url):
            task_url_parameter_value = workflow_run_context.get_value(self.url)
            if task_url_parameter_value:
                LOG.info(
                    "DownloadToS3Block: Task URL is parameterized, using parameter value",
                    task_url_parameter_value=task_url_parameter_value,
                    task_url_parameter_key=self.url,
                )
                self.url = task_url_parameter_value

        try:
            file_path = await download_file(self.url, max_size_mb=10)
        except Exception as e:
            LOG.error("DownloadToS3Block: Failed to download file", url=self.url, error=str(e))
            raise e

        uri = None
        try:
            uri = f"s3://{SettingsManager.get_settings().AWS_S3_BUCKET_UPLOADS}/{SettingsManager.get_settings().ENV}/{workflow_run_id}/{uuid.uuid4()}"
            await self._upload_file_to_s3(uri, file_path)
        except Exception as e:
            LOG.error("DownloadToS3Block: Failed to upload file to S3", uri=uri, error=str(e))
            raise e

        LOG.info("DownloadToS3Block: File downloaded and uploaded to S3", uri=uri)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uri)
        return self.build_block_result(success=True, output_parameter_value=uri)


class UploadToS3Block(Block):
    block_type: Literal[BlockType.UPLOAD_TO_S3] = BlockType.UPLOAD_TO_S3

    # TODO (kerem): A directory upload is supported but we should also support a list of files
    path: str | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.path and workflow_run_context.has_parameter(self.path):
            return [workflow_run_context.get_parameter(self.path)]

        return []

    @staticmethod
    def _get_s3_uri(workflow_run_id: str, path: str) -> str:
        s3_bucket = SettingsManager.get_settings().AWS_S3_BUCKET_UPLOADS
        s3_key = f"{SettingsManager.get_settings().ENV}/{workflow_run_id}/{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{s3_bucket}/{s3_key}"

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        # get all parameters into a dictionary
        if self.path and workflow_run_context.has_parameter(self.path) and workflow_run_context.has_value(self.path):
            file_path_parameter_value = workflow_run_context.get_value(self.path)
            if file_path_parameter_value:
                LOG.info(
                    "UploadToS3Block: File path is parameterized, using parameter value",
                    file_path_parameter_value=file_path_parameter_value,
                    file_path_parameter_key=self.path,
                )
                self.path = file_path_parameter_value
        # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use the download directory for the workflow run
        elif self.path == SettingsManager.get_settings().WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
            self.path = str(get_path_for_workflow_download_directory(workflow_run_id).absolute())

        if not self.path or not os.path.exists(self.path):
            raise FileNotFoundError(f"UploadToS3Block: File not found at path: {self.path}")

        s3_uris = []
        try:
            client = self.get_async_aws_client()
            # is the file path a file or a directory?
            if os.path.isdir(self.path):
                # get all files in the directory, if there are more than 25 files, we will not upload them
                files = os.listdir(self.path)
                if len(files) > 25:
                    raise ValueError("Too many files in the directory, not uploading")
                for file in files:
                    # if the file is a directory, we will not upload it
                    if os.path.isdir(os.path.join(self.path, file)):
                        LOG.warning("UploadToS3Block: Skipping directory", file=file)
                        continue
                    file_path = os.path.join(self.path, file)
                    s3_uri = self._get_s3_uri(workflow_run_id, file_path)
                    s3_uris.append(s3_uri)
                    await client.upload_file_from_path(uri=s3_uri, file_path=file_path)
            else:
                s3_uri = self._get_s3_uri(workflow_run_id, self.path)
                s3_uris.append(s3_uri)
                await client.upload_file_from_path(uri=s3_uri, file_path=self.path)
        except Exception as e:
            LOG.exception("UploadToS3Block: Failed to upload file to S3", file_path=self.path)
            raise e

        LOG.info("UploadToS3Block: File(s) uploaded to S3", file_path=self.path)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, s3_uris)
        return self.build_block_result(success=True, output_parameter_value=s3_uris)


class SendEmailBlock(Block):
    block_type: Literal[BlockType.SEND_EMAIL] = BlockType.SEND_EMAIL

    smtp_host: AWSSecretParameter
    smtp_port: AWSSecretParameter
    smtp_username: AWSSecretParameter
    # if you're using a Gmail account, you need to pass in an app password instead of your regular password
    smtp_password: AWSSecretParameter
    sender: str
    recipients: list[str]
    subject: str
    body: str
    file_attachments: list[str] = []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        parameters = [
            self.smtp_host,
            self.smtp_port,
            self.smtp_username,
            self.smtp_password,
        ]

        if self.file_attachments:
            for file_path in self.file_attachments:
                if workflow_run_context.has_parameter(file_path):
                    parameters.append(workflow_run_context.get_parameter(file_path))

        if self.subject and workflow_run_context.has_parameter(self.subject):
            parameters.append(workflow_run_context.get_parameter(self.subject))

        if self.body and workflow_run_context.has_parameter(self.body):
            parameters.append(workflow_run_context.get_parameter(self.body))

        return parameters

    def _decrypt_smtp_parameters(self, workflow_run_context: WorkflowRunContext) -> tuple[str, int, str, str]:
        obfuscated_smtp_host_value = workflow_run_context.get_value(self.smtp_host.key)
        obfuscated_smtp_port_value = workflow_run_context.get_value(self.smtp_port.key)
        obfuscated_smtp_username_value = workflow_run_context.get_value(self.smtp_username.key)
        obfuscated_smtp_password_value = workflow_run_context.get_value(self.smtp_password.key)
        smtp_host_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_host_value)
        smtp_port_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_port_value)
        smtp_username_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_username_value)
        smtp_password_value = workflow_run_context.get_original_secret_value_or_none(obfuscated_smtp_password_value)

        email_config_problems = []
        if smtp_host_value is None:
            email_config_problems.append("Missing SMTP server")
        if smtp_port_value is None:
            email_config_problems.append("Missing SMTP port")
        elif not smtp_port_value.isdigit():
            email_config_problems.append("SMTP port should be a number")
        if smtp_username_value is None:
            email_config_problems.append("Missing SMTP username")
        if smtp_password_value is None:
            email_config_problems.append("Missing SMTP password")

        if email_config_problems:
            raise InvalidEmailClientConfiguration(email_config_problems)

        return (
            smtp_host_value,
            smtp_port_value,
            smtp_username_value,
            smtp_password_value,
        )

    def _get_file_paths(self, workflow_run_context: WorkflowRunContext, workflow_run_id: str) -> list[str]:
        file_paths = []
        for path in self.file_attachments:
            # if the file path is a parameter, get the value from the workflow run context first
            if workflow_run_context.has_parameter(path):
                file_path_parameter_value = workflow_run_context.get_value(path)
                # if the file path is a secret, get the original secret value from the workflow run context
                file_path_parameter_secret_value = workflow_run_context.get_original_secret_value_or_none(
                    file_path_parameter_value
                )
                if file_path_parameter_secret_value:
                    path = file_path_parameter_secret_value
                else:
                    path = file_path_parameter_value

            if path == SettingsManager.get_settings().WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
                # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use download directory for the workflow run
                path = str(get_path_for_workflow_download_directory(workflow_run_id).absolute())
                LOG.info(
                    "SendEmailBlock: Using download directory for the workflow run",
                    workflow_run_id=workflow_run_id,
                    file_path=path,
                )

            # if the file path is a directory, add all files in the directory, skip directories, limit to 10 files
            if os.path.exists(path) and os.path.isdir(path):
                if len(os.listdir(path)) > 10:
                    LOG.warning("SendEmailBlock: Too many files in the directory, not attaching to email")
                    continue
                for file in os.listdir(path):
                    if os.path.isdir(os.path.join(path, file)):
                        LOG.warning("SendEmailBlock: Skipping directory", file=file)
                        continue
                    file_path = os.path.join(path, file)
                    file_paths.append(file_path)
            else:
                # covers the case where the file path is a single file, a url, or an S3 uri
                file_paths.append(path)

        return file_paths

    async def _download_from_s3(self, s3_uri: str) -> str:
        client = self.get_async_aws_client()
        downloaded_bytes = await client.download_file(uri=s3_uri)
        file_path = NamedTemporaryFile(delete=False)
        file_path.write(downloaded_bytes)
        return file_path.name

    def get_real_email_recipients(self, workflow_run_context: WorkflowRunContext) -> list[str]:
        recipients = []
        for recipient in self.recipients:
            if workflow_run_context.has_parameter(recipient):
                maybe_recipient = workflow_run_context.get_value(recipient)
            else:
                maybe_recipient = recipient

            # check if maybe_recipient is a valid email address
            try:
                validate_email(maybe_recipient)
                recipients.append(maybe_recipient)
            except EmailNotValidError as e:
                LOG.warning(
                    "SendEmailBlock: Invalid email address",
                    recipient=maybe_recipient,
                    reason=str(e),
                )

        if not recipients:
            raise NoValidEmailRecipient(recipients=recipients)

        return recipients

    async def _build_email_message(
        self, workflow_run_context: WorkflowRunContext, workflow_run_id: str
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = self.subject + f" - Workflow Run ID: {workflow_run_id}"
        msg["To"] = ", ".join(self.get_real_email_recipients(workflow_run_context))
        msg["BCC"] = self.sender  # BCC the sender so there is a record of the email being sent
        msg["From"] = self.sender
        if self.body and workflow_run_context.has_parameter(self.body) and workflow_run_context.has_value(self.body):
            # We're purposely not decrypting the body parameter value here because we don't want to expose secrets
            body_parameter_value = workflow_run_context.get_value(self.body)
            msg.set_content(body_parameter_value)
        else:
            msg.set_content(self.body)

        for filename in self._get_file_paths(workflow_run_context, workflow_run_id):
            path = None
            try:
                if filename.startswith("s3://"):
                    path = await self._download_from_s3(filename)
                elif filename.startswith("http://") or filename.startswith("https://"):
                    path = await download_file(filename)
                else:
                    LOG.info("SendEmailBlock: Looking for file locally", filename=filename)
                    if not os.path.exists(filename):
                        raise FileNotFoundError(f"File not found: {filename}")
                    if not os.path.isfile(filename):
                        raise IsADirectoryError(f"Path is a directory: {filename}")

                    LOG.info("SendEmailBlock: Found file locally", path=path)
                    path = filename

                if not path:
                    raise FileNotFoundError(f"File not found: {filename}")

                # Guess the content type based on the file's extension.  Encoding
                # will be ignored, although we should check for simple things like
                # gzip'd or compressed files.
                kind = filetype.guess(path)
                if kind:
                    ctype = kind.mime
                    extension = kind.extension
                else:
                    # No guess could be made, or the file is encoded (compressed), so
                    # use a generic bag-of-bits type.
                    ctype = "application/octet-stream"
                    extension = None

                maintype, subtype = ctype.split("/", 1)
                attachment_path = Path(path)
                attachment_filename = attachment_path.name

                # Check if the filename has an extension
                if not attachment_path.suffix:
                    # If no extension, guess it based on the MIME type
                    if extension:
                        attachment_filename += f".{extension}"

                LOG.info(
                    "SendEmailBlock: Adding attachment",
                    filename=attachment_filename,
                    maintype=maintype,
                    subtype=subtype,
                )
                with open(path, "rb") as fp:
                    msg.add_attachment(
                        fp.read(),
                        maintype=maintype,
                        subtype=subtype,
                        filename=attachment_filename,
                    )
            finally:
                if path:
                    os.unlink(path)

        return msg

    async def execute(self, workflow_run_id: str, **kwargs: dict) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        smtp_host_value, smtp_port_value, smtp_username_value, smtp_password_value = self._decrypt_smtp_parameters(
            workflow_run_context
        )

        smtp_host = None
        try:
            smtp_host = smtplib.SMTP(smtp_host_value, smtp_port_value)
            LOG.info("SendEmailBlock: Connected to SMTP server")
            smtp_host.starttls()
            smtp_host.login(smtp_username_value, smtp_password_value)
            LOG.info("SendEmailBlock: Logged in to SMTP server")
            message = await self._build_email_message(workflow_run_context, workflow_run_id)
            smtp_host.send_message(message)
            LOG.info("SendEmailBlock: Email sent")
        except Exception as e:
            LOG.error("SendEmailBlock: Failed to send email", exc_info=True)
            result_dict = {"success": False, "error": str(e)}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
            return self.build_block_result(success=False, output_parameter_value=result_dict)
        finally:
            if smtp_host:
                smtp_host.quit()

        result_dict = {"success": True}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
        return self.build_block_result(success=True, output_parameter_value=result_dict)


BlockSubclasses = Union[
    ForLoopBlock,
    TaskBlock,
    CodeBlock,
    TextPromptBlock,
    DownloadToS3Block,
    UploadToS3Block,
    SendEmailBlock,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]
