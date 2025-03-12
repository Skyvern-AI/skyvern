from __future__ import annotations

import abc
import ast
import asyncio
import csv
import json
import os
import smtplib
import textwrap
import uuid
from collections import defaultdict
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Union
from urllib.parse import quote

import filetype
import structlog
from email_validator import EmailNotValidError, validate_email
from jinja2 import Template
from playwright.async_api import Page
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, MAX_UPLOAD_FILE_COUNT
from skyvern.exceptions import (
    ContextParameterValueNotFound,
    DisabledBlockExecutionError,
    MissingBrowserState,
    MissingBrowserStatePage,
    SkyvernException,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    create_named_temporary_file,
    download_file,
    download_from_s3,
    get_path_for_workflow_download_directory,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core.validators import prepend_scheme_and_validate_url
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.workflow.context_manager import BlockMetadata, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    CustomizedCodeException,
    FailedToFormatJinjaStyleParameter,
    InsecureCodeDetected,
    InvalidEmailClientConfiguration,
    InvalidFileType,
    NoIterableValueFound,
    NoValidEmailRecipient,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    ContextParameter,
    OutputParameter,
    WorkflowParameter,
)
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()


class BlockType(StrEnum):
    TASK = "task"
    TaskV2 = "task_v2"
    FOR_LOOP = "for_loop"
    CODE = "code"
    TEXT_PROMPT = "text_prompt"
    DOWNLOAD_TO_S3 = "download_to_s3"
    UPLOAD_TO_S3 = "upload_to_s3"
    SEND_EMAIL = "send_email"
    FILE_URL_PARSER = "file_url_parser"
    VALIDATION = "validation"
    ACTION = "action"
    NAVIGATION = "navigation"
    EXTRACTION = "extraction"
    LOGIN = "login"
    WAIT = "wait"
    FILE_DOWNLOAD = "file_download"
    GOTO_URL = "goto_url"
    PDF_PARSER = "pdf_parser"


class BlockStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"


@dataclass(frozen=True)
class BlockResult:
    success: bool
    output_parameter: OutputParameter
    output_parameter_value: dict[str, Any] | list | str | None = None
    status: BlockStatus | None = None
    failure_reason: str | None = None
    workflow_run_block_id: str | None = None


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
        await workflow_run_context.register_output_parameter_value_post_execution(
            parameter=self.output_parameter,
            value=value,
        )
        await app.DATABASE.create_or_update_workflow_run_output_parameter(
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
            value=value,
        )
        LOG.info(
            "Registered output parameter value",
            output_parameter_id=self.output_parameter.output_parameter_id,
            workflow_run_id=workflow_run_id,
        )

    async def build_block_result(
        self,
        success: bool,
        failure_reason: str | None,
        output_parameter_value: dict[str, Any] | list | str | None = None,
        status: BlockStatus | None = None,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
    ) -> BlockResult:
        # TODO: update workflow run block status and failure reason
        if isinstance(output_parameter_value, str):
            output_parameter_value = {"value": output_parameter_value}

        if workflow_run_block_id:
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                output=output_parameter_value,
                status=status,
                failure_reason=failure_reason,
                organization_id=organization_id,
            )
        return BlockResult(
            success=success,
            failure_reason=failure_reason,
            output_parameter=self.output_parameter,
            output_parameter_value=output_parameter_value,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
        )

    def format_block_parameter_template_from_workflow_run_context(
        self, potential_template: str, workflow_run_context: WorkflowRunContext
    ) -> str:
        if not potential_template:
            return potential_template
        template = Template(potential_template)

        template_data = workflow_run_context.values.copy()
        template_data[self.label] = workflow_run_context.get_block_metadata(self.label)
        return template.render(template_data)

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
    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        pass

    async def execute_safe(
        self,
        workflow_run_id: str,
        parent_workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_block_id = None
        try:
            workflow_run_block = await app.DATABASE.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                parent_workflow_run_block_id=parent_workflow_run_block_id,
                label=self.label,
                block_type=self.block_type,
                continue_on_failure=self.continue_on_failure,
            )
            workflow_run_block_id = workflow_run_block.workflow_run_block_id

            description = None
            try:
                block_data = self.model_dump(
                    exclude={
                        "workflow_run_block_id",
                        "organization_id",
                        "task_id",
                        "workflow_run_id",
                        "parent_workflow_run_block_id",
                        "label",
                        "status",
                        "output",
                        "continue_on_failure",
                        "failure_reason",
                        "actions",
                        "created_at",
                        "modified_at",
                    },
                    exclude_none=True,
                )
                description_generation_prompt = prompt_engine.load_prompt(
                    "generate_workflow_run_block_description",
                    block=block_data,
                )
                json_response = await app.SECONDARY_LLM_API_HANDLER(
                    prompt=description_generation_prompt, prompt_name="generate-workflow-run-block-description"
                )
                description = json_response.get("summary")
                LOG.info(
                    "Generated description for the workflow run block",
                    description=description,
                    workflow_run_block_id=workflow_run_block.workflow_run_block_id,
                )
            except Exception as e:
                LOG.exception("Failed to generate description for the workflow run block", error=e)

            if description:
                workflow_run_block = await app.DATABASE.update_workflow_run_block(
                    workflow_run_block_id=workflow_run_block.workflow_run_block_id,
                    description=description,
                    organization_id=organization_id,
                )

            # create a screenshot
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if not browser_state:
                LOG.warning("No browser state found when creating workflow_run_block", workflow_run_id=workflow_run_id)
            else:
                screenshot = await browser_state.take_screenshot(full_page=True)
                if screenshot:
                    await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                        workflow_run_block=workflow_run_block,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            LOG.info(
                "Executing block", workflow_run_id=workflow_run_id, block_label=self.label, block_type=self.block_type
            )
            return await self.execute(
                workflow_run_id,
                workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        except Exception as e:
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

            failure_reason = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

    @abc.abstractmethod
    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        pass


class BaseTaskBlock(Block):
    task_type: str = TaskType.general
    url: str | None = None
    title: str = ""
    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | None = None
    # error code to error description for the LLM
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameters: list[PARAMETER_TYPE] = []
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    cache_actions: bool = False

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = self.parameters
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if self.url and workflow_run_context.has_parameter(self.url):
            if self.url not in [parameter.key for parameter in parameters]:
                parameters.append(workflow_run_context.get_parameter(self.url))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.title = self.format_block_parameter_template_from_workflow_run_context(self.title, workflow_run_context)

        if self.url:
            self.url = self.format_block_parameter_template_from_workflow_run_context(self.url, workflow_run_context)
            self.url = prepend_scheme_and_validate_url(self.url)

        if self.totp_identifier:
            self.totp_identifier = self.format_block_parameter_template_from_workflow_run_context(
                self.totp_identifier, workflow_run_context
            )

        if self.totp_verification_url:
            self.totp_verification_url = self.format_block_parameter_template_from_workflow_run_context(
                self.totp_verification_url, workflow_run_context
            )
            self.totp_verification_url = prepend_scheme_and_validate_url(self.totp_verification_url)

        if self.download_suffix:
            self.download_suffix = self.format_block_parameter_template_from_workflow_run_context(
                self.download_suffix, workflow_run_context
            )
            # encode the suffix to prevent invalid path style
            self.download_suffix = quote(string=self.download_suffix, safe="")

        if self.navigation_goal:
            self.navigation_goal = self.format_block_parameter_template_from_workflow_run_context(
                self.navigation_goal, workflow_run_context
            )

        if self.data_extraction_goal:
            self.data_extraction_goal = self.format_block_parameter_template_from_workflow_run_context(
                self.data_extraction_goal, workflow_run_context
            )

        if self.complete_criterion:
            self.complete_criterion = self.format_block_parameter_template_from_workflow_run_context(
                self.complete_criterion, workflow_run_context
            )

        if self.terminate_criterion:
            self.terminate_criterion = self.format_block_parameter_template_from_workflow_run_context(
                self.terminate_criterion, workflow_run_context
            )

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

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        current_retry = 0
        # initial value for will_retry is True, so that the loop runs at least once
        will_retry = True
        current_running_task: Task | None = None
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_run.workflow_permanent_id,
        )
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

        if (
            self.totp_identifier
            and workflow_run_context.has_parameter(self.totp_identifier)
            and workflow_run_context.has_value(self.totp_identifier)
        ):
            totp_identifier_parameter_value = workflow_run_context.get_value(self.totp_identifier)
            if totp_identifier_parameter_value:
                LOG.info(
                    "TOTP identifier is parameterized, using parameter value",
                    totp_identifier_parameter_value=totp_identifier_parameter_value,
                    totp_identifier_parameter_key=self.totp_identifier,
                )
                self.totp_identifier = totp_identifier_parameter_value

        if self.download_suffix and workflow_run_context.has_parameter(self.download_suffix):
            download_suffix_parameter_value = workflow_run_context.get_value(self.download_suffix)
            if download_suffix_parameter_value:
                LOG.info(
                    "Download prefix is parameterized, using parameter value",
                    download_suffix_parameter_value=download_suffix_parameter_value,
                    download_suffix_parameter_key=self.download_suffix,
                )
                self.download_suffix = download_suffix_parameter_value

        try:
            self.format_potential_template_parameters(workflow_run_context=workflow_run_context)
        except Exception as e:
            failure_reason = f"Failed to format jinja template: {str(e)}"
            await self.record_output_parameter_value(
                workflow_run_context, workflow_run_id, {"failure_reason": failure_reason}
            )
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # TODO (kerem) we should always retry on terminated. We should make a distinction between retriable and
        # non-retryable terminations
        while will_retry:
            task_order, task_retry = await self.get_task_order(workflow_run_id, current_retry)
            is_first_task = task_order == 0
            task, step = await app.agent.create_task_and_step_from_block(
                task_block=self,
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_context=workflow_run_context,
                task_order=task_order,
                task_retry=task_retry,
            )
            workflow_run_block = await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                task_id=task.task_id,
                organization_id=organization_id,
            )
            current_running_task = task
            organization = await app.DATABASE.get_organization(organization_id=workflow_run.organization_id)
            if not organization:
                raise Exception(f"Organization is missing organization_id={workflow_run.organization_id}")

            browser_state: BrowserState | None = None
            if is_first_task:
                # the first task block will create the browser state and do the navigation
                try:
                    browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                        workflow_run=workflow_run, url=self.url, browser_session_id=browser_session_id
                    )
                    # add screenshot artifact for the first task
                    screenshot = await browser_state.take_screenshot(full_page=True)
                    if screenshot:
                        await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                            workflow_run_block=workflow_run_block,
                            artifact_type=ArtifactType.SCREENSHOT_LLM,
                            data=screenshot,
                        )
                except Exception as e:
                    LOG.exception(
                        "Failed to get browser state for first task",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                    )
                    # Make sure the task is marked as failed in the database before raising the exception
                    await app.DATABASE.update_task(
                        task.task_id,
                        status=TaskStatus.failed,
                        organization_id=workflow_run.organization_id,
                        failure_reason=str(e),
                    )
                    raise e
            else:
                # if not the first task block, need to navigate manually
                browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=workflow_run_id)
                if browser_state is None:
                    raise MissingBrowserState(task_id=task.task_id, workflow_run_id=workflow_run_id)

                working_page = await browser_state.get_working_page()
                if not working_page:
                    LOG.error(
                        "BrowserState has no page",
                        workflow_run_id=workflow_run.workflow_run_id,
                    )
                    raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)

                if self.url:
                    LOG.info(
                        "Navigating to page",
                        url=self.url,
                        workflow_run_id=workflow_run_id,
                        task_id=task.task_id,
                        workflow_id=workflow.workflow_id,
                        organization_id=workflow_run.organization_id,
                        step_id=step.step_id,
                    )
                    try:
                        await browser_state.navigate_to_url(page=working_page, url=self.url)
                    except Exception as e:
                        await app.DATABASE.update_task(
                            task.task_id,
                            status=TaskStatus.failed,
                            organization_id=workflow_run.organization_id,
                            failure_reason=str(e),
                        )
                        raise e

            try:
                await app.agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    task_block=self,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=browser_session_id is None,
                )
            except Exception as e:
                # Make sure the task is marked as failed in the database before raising the exception
                await app.DATABASE.update_task(
                    task.task_id,
                    status=TaskStatus.failed,
                    organization_id=workflow_run.organization_id,
                    failure_reason=str(e),
                )
                raise e

            # Check task status
            updated_task = await app.DATABASE.get_task(
                task_id=task.task_id, organization_id=workflow_run.organization_id
            )
            if not updated_task:
                raise TaskNotFound(task.task_id)
            if not updated_task.status.is_final():
                raise UnexpectedTaskStatus(task_id=updated_task.task_id, status=updated_task.status)
            current_running_task = updated_task

            block_status_mapping = {
                TaskStatus.completed: BlockStatus.completed,
                TaskStatus.terminated: BlockStatus.terminated,
                TaskStatus.failed: BlockStatus.failed,
                TaskStatus.canceled: BlockStatus.canceled,
                TaskStatus.timed_out: BlockStatus.timed_out,
            }
            if updated_task.status == TaskStatus.completed or updated_task.status == TaskStatus.terminated:
                LOG.info(
                    "Task completed",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                success = updated_task.status == TaskStatus.completed

                downloaded_files: list[FileInfo] = []
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=workflow_run.organization_id,
                            task_id=updated_task.task_id,
                            workflow_run_id=workflow_run_id,
                        )
                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)

                task_output = TaskOutput.from_task(updated_task, downloaded_files)
                output_parameter_value = task_output.model_dump()
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
                return await self.build_block_result(
                    success=success,
                    failure_reason=updated_task.failure_reason,
                    output_parameter_value=output_parameter_value,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            elif updated_task.status == TaskStatus.canceled:
                LOG.info(
                    "Task canceled, cancelling block",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=updated_task.failure_reason,
                    output_parameter_value=None,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            elif updated_task.status == TaskStatus.timed_out:
                LOG.info(
                    "Task timed out, making the block time out",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=updated_task.failure_reason,
                    output_parameter_value=None,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                current_retry += 1
                will_retry = current_retry <= self.max_retries
                retry_message = f", retrying task {current_retry}/{self.max_retries}" if will_retry else ""
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=workflow_run.organization_id,
                            task_id=updated_task.task_id,
                            workflow_run_id=workflow_run_id,
                        )

                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)

                task_output = TaskOutput.from_task(updated_task, downloaded_files)
                LOG.warning(
                    f"Task failed with status {updated_task.status}{retry_message}",
                    task_id=updated_task.task_id,
                    task_status=updated_task.status,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow.workflow_id,
                    organization_id=workflow_run.organization_id,
                    current_retry=current_retry,
                    max_retries=self.max_retries,
                    task_output=task_output.model_dump_json(),
                )
                if not will_retry:
                    output_parameter_value = task_output.model_dump()
                    await self.record_output_parameter_value(
                        workflow_run_context, workflow_run_id, output_parameter_value
                    )
                    return await self.build_block_result(
                        success=False,
                        failure_reason=updated_task.failure_reason,
                        output_parameter_value=output_parameter_value,
                        status=block_status_mapping[updated_task.status],
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id)
        return await self.build_block_result(
            success=False,
            status=BlockStatus.failed,
            failure_reason=current_running_task.failure_reason if current_running_task else None,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class TaskBlock(BaseTaskBlock):
    block_type: Literal[BlockType.TASK] = BlockType.TASK


class LoopBlockExecutedResult(BaseModel):
    outputs_with_loop_values: list[list[dict[str, Any]]]
    block_outputs: list[BlockResult]
    last_block: BlockTypeVar | None

    def is_canceled(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.canceled

    def is_completed(self) -> bool:
        if len(self.block_outputs) == 0:
            return False

        if self.last_block is None:
            return False

        if self.is_canceled():
            return False

        last_ouput = self.block_outputs[-1]
        if last_ouput.success:
            return True

        if self.last_block.continue_on_failure:
            return True

        return False

    def is_terminated(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.terminated

    def get_failure_reason(self) -> str | None:
        if self.is_completed():
            return None

        if self.is_canceled():
            return f"Block({self.last_block.label if self.last_block else ''}) with type {self.last_block.block_type if self.last_block else ''} was canceled, canceling for loop"

        return self.block_outputs[-1].failure_reason if len(self.block_outputs) > 0 else "No block has been executed"


class ForLoopBlock(Block):
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP

    loop_blocks: list[BlockTypeVar]
    loop_over: PARAMETER_TYPE | None = None
    loop_variable_reference: str | None = None
    complete_if_empty: bool = False

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = set()
        if self.loop_over is not None:
            parameters.add(self.loop_over)

        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def get_loop_block_context_parameters(self, workflow_run_id: str, loop_data: Any) -> list[ContextParameter]:
        context_parameters = []
        for loop_block in self.loop_blocks:
            # todo: handle the case where the loop_block is a ForLoopBlock

            all_parameters = loop_block.get_all_parameters(workflow_run_id)
            for parameter in all_parameters:
                if isinstance(parameter, ContextParameter):
                    context_parameters.append(parameter)

        if self.loop_over is None:
            return context_parameters

        for context_parameter in context_parameters:
            if context_parameter.source.key != self.loop_over.key:
                continue
            # If the loop_data is a dict, we need to check if the key exists in the loop_data
            if isinstance(loop_data, dict):
                if context_parameter.key in loop_data:
                    context_parameter.value = loop_data[context_parameter.key]
                else:
                    raise ContextParameterValueNotFound(
                        parameter_key=context_parameter.key,
                        existing_keys=list(loop_data.keys()),
                        workflow_run_id=workflow_run_id,
                    )
            else:
                # If the loop_data is a list, we can directly assign the loop_data to the context_parameter value
                context_parameter.value = loop_data

        return context_parameters

    def get_loop_over_parameter_values(self, workflow_run_context: WorkflowRunContext) -> list[Any]:
        # parse the value from self.loop_variable_reference and then from self.loop_over
        if self.loop_variable_reference:
            value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
            try:
                value_json = self.format_block_parameter_template_from_workflow_run_context(
                    value_template, workflow_run_context
                )
            except Exception as e:
                raise FailedToFormatJinjaStyleParameter(value_template, str(e))
            parameter_value = json.loads(value_json)

        elif self.loop_over is not None:
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
                raise NotImplementedError()

        else:
            if self.complete_if_empty:
                return []
            else:
                raise NoIterableValueFound()

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            # TODO (kerem): Should we raise an error here?
            return [parameter_value]

    async def execute_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        loop_over_values: list[Any],
        organization_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        for loop_idx, loop_over_value in enumerate(loop_over_values):
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)

            each_loop_output_values: list[dict[str, Any]] = []
            for block_idx, loop_block in enumerate(self.loop_blocks):
                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": loop_over_value,
                }
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.copy()
                current_block = loop_block

                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )
                each_loop_output_values.append(
                    {
                        "loop_value": loop_over_value,
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )
                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=str(loop_over_value),
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_over_value=loop_over_value,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)
                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        f"ForLoopBlock: Block with type {loop_block.block_type} at index {block_idx} during loop {loop_idx} was canceled for workflow run {workflow_run_id}, canceling for loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_outputs,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if not block_output.success and not loop_block.continue_on_failure:
                    LOG.info(
                        f"ForLoopBlock: Encountered a failure processing block {block_idx} during loop {loop_idx}, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        failure_reason=block_output.failure_reason,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

            outputs_with_loop_values.append(each_loop_output_values)

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            loop_over_values = self.get_loop_over_parameter_values(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"failed to get loop values: {str(e)}",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            loop_values=loop_over_values,
        )

        LOG.info(
            f"Number of loop_over values: {len(loop_over_values)}",
            block_type=self.block_type,
            workflow_run_id=workflow_run_id,
            num_loop_over_values=len(loop_over_values),
        )
        if not loop_over_values or len(loop_over_values) == 0:
            LOG.info(
                "No loop_over values found, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_over_values=len(loop_over_values),
                complete_if_empty=self.complete_if_empty,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            if self.complete_if_empty:
                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=[],
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                return await self.build_block_result(
                    success=False,
                    failure_reason="No iterable value found for the loop block",
                    status=BlockStatus.terminated,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        if not self.loop_blocks or len(self.loop_blocks) == 0:
            LOG.info(
                "No defined blocks to loop, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_blocks=len(self.loop_blocks),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return await self.build_block_result(
                success=False,
                failure_reason="No defined blocks to loop",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        loop_executed_result = await self.execute_loop_helper(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_context=workflow_run_context,
            loop_over_values=loop_over_values,
            organization_id=organization_id,
        )
        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )
        block_status = BlockStatus.failed
        success = False

        if loop_executed_result.is_canceled():
            block_status = BlockStatus.canceled
        elif loop_executed_result.is_completed():
            block_status = BlockStatus.completed
            success = True
        elif loop_executed_result.is_terminated():
            block_status = BlockStatus.terminated
        else:
            block_status = BlockStatus.failed

        return await self.build_block_result(
            success=success,
            failure_reason=loop_executed_result.get_failure_reason(),
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class CodeBlock(Block):
    block_type: Literal[BlockType.CODE] = BlockType.CODE

    code: str
    parameters: list[PARAMETER_TYPE] = []

    @staticmethod
    def is_safe_code(code: str) -> None:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if hasattr(node, "attr") and str(node.attr).startswith("__"):
                raise InsecureCodeDetected("Not allowed to access private methods or attributes")
            if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
                raise InsecureCodeDetected("Not allowed to import modules")

    @staticmethod
    def build_safe_vars() -> dict[str, Any]:
        return {
            "__builtins__": {},  # only allow several builtins due to security concerns
            "locals": locals,
            "print": print,
            "len": len,
            "range": range,
            "str": str,
            "int": int,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "bool": bool,
            "asyncio": asyncio,
        }

    def generate_async_user_function(
        self, code: str, page: Page, parameters: dict[str, Any] | None = None
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        code = textwrap.indent(code, "    ")
        full_code = f"""
async def wrapper():
{code}
    return locals()
"""
        runtime_variables: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}
        safe_vars = self.build_safe_vars()
        if parameters:
            safe_vars.update(parameters)
        safe_vars["page"] = page
        exec(full_code, safe_vars, runtime_variables)
        return runtime_variables["wrapper"]

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.code = self.format_block_parameter_template_from_workflow_run_context(self.code, workflow_run_context)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        if not settings.ENABLE_CODE_BLOCK:
            raise DisabledBlockExecutionError("CodeBlock is disabled")

        # TODO: only support to use code block to manupilate the browser page
        # support browser context in the future
        browser_state: BrowserState | None = None
        if browser_session_id and organization_id:
            LOG.info(
                "Getting browser state for workflow run from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id)
            if browser_state:
                await app.PERSISTENT_SESSIONS_MANAGER.occupy_browser_session(
                    browser_session_id,
                    runnable_type="workflow_run",
                    runnable_id=workflow_run_id,
                    organization_id=organization_id,
                )
        else:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)

        if not browser_state:
            return await self.build_block_result(
                success=False,
                failure_reason="No browser found to run the code block",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        page = await browser_state.get_working_page()
        if not page:
            return await self.build_block_result(
                success=False,
                failure_reason="No page found to run the code block",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # get all parameters into a dictionary
        parameter_values = {}
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            secret_value = workflow_run_context.get_original_secret_value_or_none(value)
            if secret_value is not None:
                parameter_values[parameter.key] = secret_value
            else:
                parameter_values[parameter.key] = value

        try:
            self.is_safe_code(self.code)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        user_function = self.generate_async_user_function(self.code, page, parameter_values)
        try:
            result = await user_function()
        except Exception as e:
            exc = CustomizedCodeException(e)
            return await self.build_block_result(
                success=False,
                failure_reason=exc.message,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        result = json.loads(
            json.dumps(result, default=lambda value: f"Object '{type(value)}' is not JSON serializable")
        )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


DEFAULT_TEXT_PROMPT_LLM_KEY = settings.PROMPT_BLOCK_LLM_KEY or settings.LLM_KEY


class TextPromptBlock(Block):
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT

    llm_key: str = DEFAULT_TEXT_PROMPT_LLM_KEY
    prompt: str
    parameters: list[PARAMETER_TYPE] = []
    json_schema: dict[str, Any] | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.llm_key = self.format_block_parameter_template_from_workflow_run_context(
            self.llm_key, workflow_run_context
        )
        self.prompt = self.format_block_parameter_template_from_workflow_run_context(self.prompt, workflow_run_context)

    async def send_prompt(self, prompt: str, parameter_values: dict[str, Any]) -> dict[str, Any]:
        llm_key = self.llm_key or DEFAULT_TEXT_PROMPT_LLM_KEY
        llm_api_handler = LLMAPIHandlerFactory.get_llm_api_handler(llm_key)
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
        response = await llm_api_handler(prompt=prompt, prompt_name="text-prompt")
        LOG.info("TextPromptBlock: Received response from LLM", response=response)
        return response

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Validate block execution
        await app.AGENT_FUNCTION.validate_block_execution(
            block=self,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get workflow run context
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            prompt=self.prompt,
        )
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        # get all parameters into a dictionary
        parameter_values = {}
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            secret_value = workflow_run_context.get_original_secret_value_or_none(value)
            if secret_value:
                continue
            else:
                parameter_values[parameter.key] = value

        response = await self.send_prompt(self.prompt, parameter_values)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=response,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


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

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.url = self.format_block_parameter_template_from_workflow_run_context(self.url, workflow_run_context)

    async def _upload_file_to_s3(self, uri: str, file_path: str) -> None:
        try:
            client = self.get_async_aws_client()
            await client.upload_file_from_path(uri=uri, file_path=file_path)
        finally:
            # Clean up the temporary file since it's created with delete=False
            os.unlink(file_path)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
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
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            file_path = await download_file(self.url, max_size_mb=10)
        except Exception as e:
            LOG.error("DownloadToS3Block: Failed to download file", url=self.url, error=str(e))
            raise e

        uri = None
        try:
            uri = f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}"
            await self._upload_file_to_s3(uri, file_path)
        except Exception as e:
            LOG.error("DownloadToS3Block: Failed to upload file to S3", uri=uri, error=str(e))
            raise e

        LOG.info("DownloadToS3Block: File downloaded and uploaded to S3", uri=uri)
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uri)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uri,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


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

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.format_block_parameter_template_from_workflow_run_context(self.path, workflow_run_context)

    @staticmethod
    def _get_s3_uri(workflow_run_id: str, path: str) -> str:
        s3_bucket = settings.AWS_S3_BUCKET_UPLOADS
        s3_key = f"{settings.ENV}/{workflow_run_id}/{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{s3_bucket}/{s3_key}"

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
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
        elif self.path == settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
            self.path = str(get_path_for_workflow_download_directory(workflow_run_id).absolute())

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.path or not os.path.exists(self.path):
            raise FileNotFoundError(f"UploadToS3Block: File not found at path: {self.path}")

        s3_uris = []
        try:
            client = self.get_async_aws_client()
            # is the file path a file or a directory?
            if os.path.isdir(self.path):
                # get all files in the directory, if there are more than 25 files, we will not upload them
                files = os.listdir(self.path)
                if len(files) > MAX_UPLOAD_FILE_COUNT:
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
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=s3_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


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

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.sender = self.format_block_parameter_template_from_workflow_run_context(self.sender, workflow_run_context)
        self.subject = self.format_block_parameter_template_from_workflow_run_context(
            self.subject, workflow_run_context
        )
        self.body = self.format_block_parameter_template_from_workflow_run_context(self.body, workflow_run_context)
        # file_attachments are formatted in _get_file_paths()
        # recipients are formatted in get_real_email_recipients()

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

            if path == settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY:
                # if the path is WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, use download directory for the workflow run
                path = str(get_path_for_workflow_download_directory(workflow_run_id).absolute())
                LOG.info(
                    "SendEmailBlock: Using download directory for the workflow run",
                    workflow_run_id=workflow_run_id,
                    file_path=path,
                )

            path = self.format_block_parameter_template_from_workflow_run_context(path, workflow_run_context)
            # if the file path is a directory, add all files in the directory, skip directories, limit to 10 files
            if os.path.exists(path):
                if os.path.isdir(path):
                    for file in os.listdir(path):
                        if os.path.isdir(os.path.join(path, file)):
                            LOG.warning("SendEmailBlock: Skipping directory", file=file)
                            continue
                        file_path = os.path.join(path, file)
                        file_paths.append(file_path)
                else:
                    # covers the case where the file path is a single file
                    file_paths.append(path)
            # check if path is a url, or an S3 uri
            elif (
                path.startswith("http://")
                or path.startswith("https://")
                or path.startswith("s3://")
                or path.startswith("www.")
            ):
                file_paths.append(path)
            else:
                LOG.warning("SendEmailBlock: File not found", file_path=path)

        return file_paths

    async def _download_from_s3(self, s3_uri: str) -> str:
        client = self.get_async_aws_client()
        downloaded_bytes = await client.download_file(uri=s3_uri)
        file_path = create_named_temporary_file(delete=False)
        file_path.write(downloaded_bytes)
        return file_path.name

    def get_real_email_recipients(self, workflow_run_context: WorkflowRunContext) -> list[str]:
        recipients = []
        for recipient in self.recipients:
            if workflow_run_context.has_parameter(recipient):
                maybe_recipient = workflow_run_context.get_value(recipient)
            else:
                maybe_recipient = recipient

            recipient = self.format_block_parameter_template_from_workflow_run_context(recipient, workflow_run_context)
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
        msg["Subject"] = (
            self.subject.strip().replace("\n", "").replace("\r", "") + f" - Workflow Run ID: {workflow_run_id}"
        )
        msg["To"] = ", ".join(self.get_real_email_recipients(workflow_run_context))
        msg["BCC"] = self.sender  # BCC the sender so there is a record of the email being sent
        msg["From"] = self.sender
        if self.body and workflow_run_context.has_parameter(self.body) and workflow_run_context.has_value(self.body):
            # We're purposely not decrypting the body parameter value here because we don't want to expose secrets
            body_parameter_value = workflow_run_context.get_value(self.body)
            msg.set_content(str(body_parameter_value))
        else:
            msg.set_content(self.body)

        file_names_by_hash: dict[str, list[str]] = defaultdict(list)

        for filename in self._get_file_paths(workflow_run_context, workflow_run_id):
            if filename.startswith("s3://"):
                path = await download_from_s3(self.get_async_aws_client(), filename)
            elif filename.startswith("http://") or filename.startswith("https://"):
                path = await download_file(filename)
            else:
                LOG.info("SendEmailBlock: Looking for file locally", filename=filename)
                if not os.path.exists(filename):
                    raise FileNotFoundError(f"File not found: {filename}")
                if not os.path.isfile(filename):
                    raise IsADirectoryError(f"Path is a directory: {filename}")

                path = filename
                LOG.info("SendEmailBlock: Found file locally", path=path)

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
                file_hash = calculate_sha256_for_file(path)
                file_names_by_hash[file_hash].append(path)

        # Calculate file stats based on content hashes
        total_files = sum(len(files) for files in file_names_by_hash.values())
        unique_files = len(file_names_by_hash)
        duplicate_files_list = [files for files in file_names_by_hash.values() if len(files) > 1]

        # Log file statistics
        LOG.info("SendEmailBlock: Total files attached", total_files=total_files)
        LOG.info("SendEmailBlock: Unique files (based on content) attached", unique_files=unique_files)
        if duplicate_files_list:
            LOG.info(
                "SendEmailBlock: Duplicate files (based on content) attached", duplicate_files_list=duplicate_files_list
            )

        return msg

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            recipients=self.recipients,
            attachments=self.file_attachments,
            subject=self.subject,
            body=self.body,
        )
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
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
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=result_dict,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        finally:
            if smtp_host:
                smtp_host.quit()

        result_dict = {"success": True}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result_dict,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class FileType(StrEnum):
    CSV = "csv"


class FileParserBlock(Block):
    block_type: Literal[BlockType.FILE_URL_PARSER] = BlockType.FILE_URL_PARSER

    file_url: str
    file_type: FileType

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if self.file_url and workflow_run_context.has_parameter(self.file_url):
            return [workflow_run_context.get_parameter(self.file_url)]
        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.file_url = self.format_block_parameter_template_from_workflow_run_context(
            self.file_url, workflow_run_context
        )

    def validate_file_type(self, file_url_used: str, file_path: str) -> None:
        if self.file_type == FileType.CSV:
            try:
                with open(file_path, "r") as file:
                    csv.Sniffer().sniff(file.read(1024))
            except csv.Error as e:
                raise InvalidFileType(file_url=file_url_used, file_type=self.file_type, error=str(e))

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if (
            self.file_url
            and workflow_run_context.has_parameter(self.file_url)
            and workflow_run_context.has_value(self.file_url)
        ):
            file_url_parameter_value = workflow_run_context.get_value(self.file_url)
            if file_url_parameter_value:
                LOG.info(
                    "FileParserBlock: File URL is parameterized, using parameter value",
                    file_url_parameter_value=file_url_parameter_value,
                    file_url_parameter_key=self.file_url,
                )
                self.file_url = file_url_parameter_value

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Download the file
        if self.file_url.startswith("s3://"):
            file_path = await download_from_s3(self.get_async_aws_client(), self.file_url)
        else:
            file_path = await download_file(self.file_url)
        # Validate the file type
        self.validate_file_type(self.file_url, file_path)
        # Parse the file into a list of dictionaries where each dictionary represents a row in the file
        parsed_data = []
        with open(file_path, "r") as file:
            if self.file_type == FileType.CSV:
                reader = csv.DictReader(file)
                for row in reader:
                    parsed_data.append(row)
        # Record the parsed data
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, parsed_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=parsed_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class PDFParserBlock(Block):
    block_type: Literal[BlockType.PDF_PARSER] = BlockType.PDF_PARSER

    file_url: str
    json_schema: dict[str, Any] | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if self.file_url and workflow_run_context.has_parameter(self.file_url):
            return [workflow_run_context.get_parameter(self.file_url)]
        return []

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.file_url = self.format_block_parameter_template_from_workflow_run_context(
            self.file_url, workflow_run_context
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if (
            self.file_url
            and workflow_run_context.has_parameter(self.file_url)
            and workflow_run_context.has_value(self.file_url)
        ):
            file_url_parameter_value = workflow_run_context.get_value(self.file_url)
            if file_url_parameter_value:
                LOG.info(
                    "PDFParserBlock: File URL is parameterized, using parameter value",
                    file_url_parameter_value=file_url_parameter_value,
                    file_url_parameter_key=self.file_url,
                )
                self.file_url = file_url_parameter_value

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Download the file
        file_path = None
        if self.file_url.startswith("s3://"):
            file_path = await download_from_s3(self.get_async_aws_client(), self.file_url)
        else:
            file_path = await download_file(self.file_url)

        extracted_text = ""
        try:
            reader = PdfReader(file_path)
            page_count = len(reader.pages)
            for i in range(page_count):
                extracted_text += reader.pages[i].extract_text() + "\n"

        except PdfReadError:
            return await self.build_block_result(
                success=False,
                failure_reason="Failed to parse PDF file",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.json_schema:
            self.json_schema = {
                "type": "object",
                "properties": {
                    "output": {
                        "type": "object",
                        "description": "Information extracted from the text",
                    }
                },
            }

        llm_prompt = prompt_engine.load_prompt(
            "extract-information-from-file-text", extracted_text_content=extracted_text, json_schema=self.json_schema
        )
        llm_response = await app.LLM_API_HANDLER(prompt=llm_prompt, prompt_name="extract-information-from-file-text")
        # Record the parsed data
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, llm_response)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=llm_response,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WaitBlock(Block):
    block_type: Literal[BlockType.WAIT] = BlockType.WAIT

    wait_sec: int
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # TODO: we need to support to interrupt the sleep when the workflow run failed/cancelled/terminated
        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            wait_sec=self.wait_sec,
        )
        LOG.info(
            "Going to pause the workflow for a while",
            second=self.wait_sec,
            workflow_run_id=workflow_run_id,
        )
        await asyncio.sleep(self.wait_sec)
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        result_dict = {"success": True}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result_dict,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class ValidationBlock(BaseTaskBlock):
    block_type: Literal[BlockType.VALIDATION] = BlockType.VALIDATION

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        task_order, _ = await self.get_task_order(workflow_run_id, 0)
        is_first_task = task_order == 0
        if is_first_task:
            return await self.build_block_result(
                success=False,
                failure_reason="Validation block should not be the first block",
                output_parameter_value=None,
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        return await super().execute(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            kwargs=kwargs,
        )


class ActionBlock(BaseTaskBlock):
    block_type: Literal[BlockType.ACTION] = BlockType.ACTION


class NavigationBlock(BaseTaskBlock):
    block_type: Literal[BlockType.NAVIGATION] = BlockType.NAVIGATION

    navigation_goal: str


class ExtractionBlock(BaseTaskBlock):
    block_type: Literal[BlockType.EXTRACTION] = BlockType.EXTRACTION

    data_extraction_goal: str


class LoginBlock(BaseTaskBlock):
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN


class FileDownloadBlock(BaseTaskBlock):
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD


class UrlBlock(BaseTaskBlock):
    block_type: Literal[BlockType.GOTO_URL] = BlockType.GOTO_URL
    url: str


class TaskV2Block(Block):
    block_type: Literal[BlockType.TaskV2] = BlockType.TaskV2
    prompt: str
    url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    max_iterations: int = settings.MAX_ITERATIONS_PER_TASK_V2
    max_steps: int = settings.MAX_STEPS_PER_TASK_V2

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return []

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        from skyvern.forge.sdk.services import task_v2_service
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

        if not self.url:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if browser_state:
                page = await browser_state.get_working_page()
                if page:
                    current_url = await SkyvernFrame.get_url(frame=page)
                    if current_url != "about:blank":
                        self.url = current_url

        if not organization_id:
            raise ValueError("Running TaskV2Block requires organization_id")

        organization = await app.DATABASE.get_organization(organization_id)
        if not organization:
            raise ValueError(f"Organization not found {organization_id}")
        workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id, organization_id)
        if not workflow_run:
            raise ValueError(f"WorkflowRun not found {workflow_run_id} when running TaskV2Block")
        task_v2 = await task_v2_service.initialize_task_v2(
            organization,
            user_prompt=self.prompt,
            user_url=self.url,
            parent_workflow_run_id=workflow_run_id,
            proxy_location=workflow_run.proxy_location,
        )
        await app.DATABASE.update_task_v2(
            task_v2.observer_cruise_id, status=TaskV2Status.queued, organization_id=organization_id
        )
        if task_v2.workflow_run_id:
            await app.DATABASE.update_workflow_run(
                workflow_run_id=task_v2.workflow_run_id,
                status=WorkflowRunStatus.queued,
            )
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                block_workflow_run_id=task_v2.workflow_run_id,
            )

        task_v2 = await task_v2_service.run_task_v2(
            organization=organization,
            task_v2_id=task_v2.observer_cruise_id,
            request_id=None,
            max_steps_override=self.max_steps,
            browser_session_id=browser_session_id,
        )
        result_dict = None
        if task_v2:
            result_dict = task_v2.output

        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result_dict,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


BlockSubclasses = Union[
    ForLoopBlock,
    TaskBlock,
    CodeBlock,
    TextPromptBlock,
    DownloadToS3Block,
    UploadToS3Block,
    SendEmailBlock,
    FileParserBlock,
    PDFParserBlock,
    ValidationBlock,
    ActionBlock,
    NavigationBlock,
    ExtractionBlock,
    LoginBlock,
    WaitBlock,
    FileDownloadBlock,
    UrlBlock,
    TaskV2Block,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]
