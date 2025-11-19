from __future__ import annotations

import abc
import ast
import asyncio
import csv
import json
import os
import re
import smtplib
import textwrap
import uuid
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Awaitable, Callable, Literal, Union
from urllib.parse import quote, urlparse

import filetype
import pandas as pd
import pyotp
import structlog
from email_validator import EmailNotValidError, validate_email
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from playwright.async_api import Page
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from skyvern.config import settings
from skyvern.constants import (
    AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT,
    GET_DOWNLOADED_FILES_TIMEOUT,
    MAX_UPLOAD_FILE_COUNT,
)
from skyvern.exceptions import (
    AzureConfigurationError,
    ContextParameterValueNotFound,
    MissingBrowserState,
    MissingBrowserStatePage,
    SkyvernException,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api import email
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.azure import AsyncAzureStorageClient
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    create_named_temporary_file,
    download_file,
    download_from_s3,
    get_path_for_workflow_download_directory,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import AzureVaultConstants, OnePasswordConstants
from skyvern.forge.sdk.trace import TraceManager
from skyvern.forge.sdk.workflow.context_manager import BlockMetadata, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    CustomizedCodeException,
    FailedToFormatJinjaStyleParameter,
    InsecureCodeDetected,
    InvalidEmailClientConfiguration,
    InvalidFileType,
    MissingJinjaVariables,
    NoIterableValueFound,
    NoValidEmailRecipient,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    ContextParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
)
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, FileStorageType, FileType
from skyvern.utils.strings import generate_random_string
from skyvern.utils.templating import get_missing_variables
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
    jinja_sandbox_env = SandboxedEnvironment(undefined=StrictUndefined)
else:
    jinja_sandbox_env = SandboxedEnvironment()


# Mapping from TaskV2Status to the corresponding BlockStatus. Declared once at
# import time so it is not recreated on each block execution.
TASKV2_TO_BLOCK_STATUS: dict[TaskV2Status, BlockStatus] = {
    TaskV2Status.completed: BlockStatus.completed,
    TaskV2Status.terminated: BlockStatus.terminated,
    TaskV2Status.failed: BlockStatus.failed,
    TaskV2Status.canceled: BlockStatus.canceled,
    TaskV2Status.timed_out: BlockStatus.timed_out,
}

# ForLoop constants
DEFAULT_MAX_LOOP_ITERATIONS = 100
DEFAULT_MAX_STEPS_PER_ITERATION = 50


class Block(BaseModel, abc.ABC):
    # Must be unique within workflow definition
    label: str
    block_type: BlockType
    output_parameter: OutputParameter
    continue_on_failure: bool = False
    model: dict[str, Any] | None = None
    disable_cache: bool = False

    @property
    def override_llm_key(self) -> str | None:
        """
        If the `Block` has a `model` defined, then return the mapped llm_key for it.

        Otherwise return `None`.
        """
        if self.model:
            model_name = self.model.get("model_name")
            if model_name:
                mapping = settings.get_model_name_to_llm_key()
                return mapping.get(model_name, {}).get("llm_key")

        return None

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

        template = jinja_sandbox_env.from_string(potential_template)

        block_reference_data: dict[str, Any] = workflow_run_context.get_block_metadata(self.label)
        template_data = workflow_run_context.values.copy()
        if workflow_run_context.include_secrets_in_templates:
            template_data.update(workflow_run_context.secrets)

            # Create easier-to-access entries for credentials
            # Look for credential parameters and create real_username/real_password entries
            # First collect all credential parameters to avoid modifying dict during iteration
            credential_params = []
            for key, value in template_data.items():
                if isinstance(value, dict) and "context" in value and "username" in value and "password" in value:
                    credential_params.append((key, value))

            # Now add the real_username/real_password entries
            for key, value in credential_params:
                username_secret_id = value.get("username", "")
                password_secret_id = value.get("password", "")

                # Get the actual values from the secrets
                real_username = template_data.get(username_secret_id, "")
                real_password = template_data.get(password_secret_id, "")

                # Add easier-to-access entries
                template_data[f"{key}_real_username"] = real_username
                template_data[f"{key}_real_password"] = real_password

        if self.label in template_data:
            current_value = template_data[self.label]
            if isinstance(current_value, dict):
                block_reference_data.update(current_value)
            else:
                LOG.warning(
                    f"Parameter {self.label} has a registered reference value, going to overwrite it by block metadata"
                )

        template_data[self.label] = block_reference_data

        # TODO (suchintan): This is pretty hacky - we should have a standard way to initialize the workflow run context
        # inject the forloop metadata as global variables
        if "current_index" in block_reference_data:
            template_data["current_index"] = block_reference_data["current_index"]
        if "current_item" in block_reference_data:
            template_data["current_item"] = block_reference_data["current_item"]
        if "current_value" in block_reference_data:
            template_data["current_value"] = block_reference_data["current_value"]

        # Initialize workflow-level parameters
        if "workflow_title" not in template_data:
            template_data["workflow_title"] = workflow_run_context.workflow_title
        if "workflow_id" not in template_data:
            template_data["workflow_id"] = workflow_run_context.workflow_id
        if "workflow_permanent_id" not in template_data:
            template_data["workflow_permanent_id"] = workflow_run_context.workflow_permanent_id
        if "workflow_run_id" not in template_data:
            template_data["workflow_run_id"] = workflow_run_context.workflow_run_id

        if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
            if missing_variables := get_missing_variables(potential_template, template_data):
                raise MissingJinjaVariables(
                    template=potential_template,
                    variables=missing_variables,
                )

        return template.render(template_data)

    @classmethod
    def get_subclasses(cls) -> tuple[type[Block], ...]:
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

    async def _generate_workflow_run_block_description(
        self, workflow_run_block_id: str, organization_id: str | None = None
    ) -> None:
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
                workflow_run_block_id=workflow_run_block_id,
            )
        except Exception as e:
            LOG.exception("Failed to generate description for the workflow run block", error=e)

        if description:
            await app.DATABASE.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                description=description,
                organization_id=organization_id,
            )

    @TraceManager.traced_async(ignore_inputs=["kwargs"])
    async def execute_safe(
        self,
        workflow_run_id: str,
        parent_workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_block_id = None
        engine: RunEngine | None = None
        try:
            if isinstance(self, BaseTaskBlock):
                engine = self.engine

            workflow_run_block = await app.DATABASE.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                parent_workflow_run_block_id=parent_workflow_run_block_id,
                label=self.label,
                block_type=self.block_type,
                continue_on_failure=self.continue_on_failure,
                engine=engine,
            )
            workflow_run_block_id = workflow_run_block.workflow_run_block_id

            # generate the description for the workflow run block asynchronously
            asyncio.create_task(self._generate_workflow_run_block_description(workflow_run_block_id, organization_id))

            # create a screenshot
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if not browser_state:
                LOG.warning(
                    "No browser state found when creating workflow_run_block",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    browser_session_id=browser_session_id,
                    block_label=self.label,
                )
            else:
                try:
                    screenshot = await browser_state.take_fullpage_screenshot(
                        use_playwright_fullpage=await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                            "ENABLE_PLAYWRIGHT_FULLPAGE",
                            workflow_run_id,
                            properties={"organization_id": str(organization_id)},
                        )
                    )
                except Exception:
                    LOG.warning(
                        "Failed to take screenshot before executing the block, ignoring the exception",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                    )
                    screenshot = None
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
    engine: RunEngine = RunEngine.skyvern_v1
    complete_criterion: str | None = None
    terminate_criterion: str | None = None
    navigation_goal: str | None = None
    data_extraction_goal: str | None = None
    data_schema: dict[str, Any] | list | str | None = None
    # error code to error description for the LLM
    error_code_mapping: dict[str, str] | None = None
    max_retries: int = 0
    max_steps_per_run: int | None = None
    parameters: list[PARAMETER_TYPE] = []
    complete_on_download: bool = False
    download_suffix: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    complete_verification: bool = True
    include_action_history_in_verification: bool = False
    download_timeout: float | None = None  # minutes

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

        if isinstance(self.data_schema, str):
            self.data_schema = self.format_block_parameter_template_from_workflow_run_context(
                self.data_schema, workflow_run_context
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
                        workflow_run=workflow_run,
                        url=self.url,
                        browser_session_id=browser_session_id,
                        browser_profile_id=workflow_run.browser_profile_id,
                    )
                    working_page = await browser_state.get_working_page()
                    if not working_page:
                        LOG.error(
                            "BrowserState has no page",
                            workflow_run_id=workflow_run.workflow_run_id,
                        )
                        raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)
                    if working_page.url == "about:blank" and self.url:
                        await browser_state.navigate_to_url(page=working_page, url=self.url)

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

                try:
                    # add screenshot artifact for the first task
                    screenshot = await browser_state.take_fullpage_screenshot(
                        use_playwright_fullpage=await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                            "ENABLE_PLAYWRIGHT_FULLPAGE",
                            workflow_run_id,
                            properties={"organization_id": str(organization_id)},
                        )
                    )
                    if screenshot:
                        await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                            workflow_run_block=workflow_run_block,
                            artifact_type=ArtifactType.SCREENSHOT_LLM,
                            data=screenshot,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to take screenshot for first task",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )
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
                current_context = skyvern_context.ensure_context()
                current_context.task_id = task.task_id
                close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address
                await app.agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    task_block=self,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=close_browser_on_completion,
                    complete_verification=self.complete_verification,
                    engine=self.engine,
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
            finally:
                current_context.task_id = None

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
                            run_id=current_context.run_id
                            if current_context and current_context.run_id
                            else workflow_run_id or updated_task.task_id,
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
                            run_id=current_context.run_id
                            if current_context and current_context.run_id
                            else workflow_run_id or updated_task.task_id,
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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TASK] = BlockType.TASK  # type: ignore


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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore

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

    async def get_values_from_loop_variable_reference(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        parameter_value = None
        if self.loop_variable_reference:
            LOG.debug("Processing loop variable reference", loop_variable_reference=self.loop_variable_reference)

            # Check if this looks like a parameter path (contains dots and/or _output)
            is_likely_parameter_path = "extracted_information." in self.loop_variable_reference

            # Try parsing as Jinja template
            parameter_value = self.try_parse_jinja_template(workflow_run_context)

            if parameter_value is None and not is_likely_parameter_path:
                try:
                    # Create and execute extraction block using the current block's workflow_id
                    extraction_block = self._create_initial_extraction_block(self.loop_variable_reference)

                    LOG.info(
                        "Processing natural language loop input",
                        prompt=self.loop_variable_reference,
                        extraction_goal=extraction_block.data_extraction_goal,
                    )

                    extraction_result = await extraction_block.execute(
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                    if not extraction_result.success:
                        LOG.error("Extraction block failed", failure_reason=extraction_result.failure_reason)
                        raise ValueError(f"Extraction block failed: {extraction_result.failure_reason}")

                    LOG.debug("Extraction block succeeded", output=extraction_result.output_parameter_value)

                    # Store the extraction result in the workflow context
                    await extraction_block.record_output_parameter_value(
                        workflow_run_context=workflow_run_context,
                        workflow_run_id=workflow_run_id,
                        value=extraction_result.output_parameter_value,
                    )

                    # Get the extracted information
                    if not isinstance(extraction_result.output_parameter_value, dict):
                        LOG.error(
                            "Extraction result output_parameter_value is not a dict",
                            output_parameter_value=extraction_result.output_parameter_value,
                        )
                        raise ValueError("Extraction result output_parameter_value is not a dictionary")

                    if "extracted_information" not in extraction_result.output_parameter_value:
                        LOG.error(
                            "Extraction result missing extracted_information key",
                            output_parameter_value=extraction_result.output_parameter_value,
                        )
                        raise ValueError("Extraction result missing extracted_information key")

                    extracted_info = extraction_result.output_parameter_value["extracted_information"]

                    # Handle different possible structures of extracted_info
                    if isinstance(extracted_info, list):
                        # If it's a list, take the first element
                        if len(extracted_info) > 0:
                            extracted_info = extracted_info[0]
                        else:
                            LOG.error("Extracted information list is empty")
                            raise ValueError("Extracted information list is empty")

                    # At this point, extracted_info should be a dict
                    if not isinstance(extracted_info, dict):
                        LOG.error("Invalid extraction result structure - not a dict", extracted_info=extracted_info)
                        raise ValueError("Extraction result is not a dictionary")

                    # Extract the loop values
                    loop_values = extracted_info.get("loop_values", [])

                    if not loop_values:
                        LOG.error("No loop values found in extraction result")
                        raise ValueError("No loop values found in extraction result")

                    LOG.info("Extracted loop values", count=len(loop_values), values=loop_values)

                    # Update the loop variable reference to point to the extracted loop values
                    # We'll use a temporary key that we can reference
                    temp_key = f"extracted_loop_values_{generate_random_string()}"
                    workflow_run_context.set_value(temp_key, loop_values)
                    self.loop_variable_reference = temp_key

                    # Now try parsing again with the updated reference
                    parameter_value = self.try_parse_jinja_template(workflow_run_context)

                except Exception as e:
                    LOG.error("Failed to process natural language loop input", error=str(e))
                    raise FailedToFormatJinjaStyleParameter(self.loop_variable_reference, str(e))

            if parameter_value is None:
                # Fall back to the original Jinja template approach
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                try:
                    value_json = self.format_block_parameter_template_from_workflow_run_context(
                        value_template, workflow_run_context
                    )
                except Exception as e:
                    raise FailedToFormatJinjaStyleParameter(value_template, str(e))
                parameter_value = json.loads(value_json)

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            return [parameter_value]

    async def get_loop_over_parameter_values(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        # parse the value from self.loop_variable_reference and then from self.loop_over
        if self.loop_variable_reference:
            return await self.get_values_from_loop_variable_reference(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
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

    def try_parse_jinja_template(self, workflow_run_context: WorkflowRunContext) -> Any | None:
        """Try to parse the loop variable reference as a Jinja template."""
        try:
            # Try the exact reference first
            try:
                if self.loop_variable_reference is None:
                    return None
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                value_json = self.format_block_parameter_template_from_workflow_run_context(
                    value_template, workflow_run_context
                )
                parameter_value = json.loads(value_json)
                if parameter_value is not None:
                    return parameter_value
            except Exception:
                pass

            # If that fails, try common access patterns for extraction results
            if self.loop_variable_reference is None:
                return None
            access_patterns = [
                f"{self.loop_variable_reference}.extracted_information",
                f"{self.loop_variable_reference}.extracted_information.results",
                f"{self.loop_variable_reference}.results",
            ]

            for pattern in access_patterns:
                try:
                    value_template = f"{{{{ {pattern.strip(' {}')} | tojson }}}}"
                    value_json = self.format_block_parameter_template_from_workflow_run_context(
                        value_template, workflow_run_context
                    )
                    parameter_value = json.loads(value_json)
                    if parameter_value is not None:
                        return parameter_value
                except Exception:
                    continue

            return None
        except Exception:
            return None

    def _create_initial_extraction_block(self, natural_language_prompt: str) -> ExtractionBlock:
        """Create an extraction block to process natural language input."""

        # Create a schema that only extracts loop values
        data_schema = {
            "type": "object",
            "properties": {
                "loop_values": {
                    "type": "array",
                    "description": "Array of values to iterate over. Each value should be the primary data needed for the loop blocks.",
                    "items": {
                        "type": "string",
                        "description": "The primary value to be used in the loop iteration (e.g., URL, text, identifier, etc.)",
                    },
                }
            },
        }

        # Create extraction goal that includes the natural language prompt
        extraction_goal = prompt_engine.load_prompt(
            "extraction_prompt_for_nat_language_loops", natural_language_prompt=natural_language_prompt
        )

        # Create a temporary output parameter using the current block's workflow_id

        output_param = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"natural_lang_extraction_{generate_random_string()}",
            workflow_id=self.output_parameter.workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
            description="Natural language extraction result",
        )

        return ExtractionBlock(
            label=f"natural_lang_extraction_{generate_random_string()}",
            data_extraction_goal=extraction_goal,
            data_schema=data_schema,
            output_parameter=output_param,
        )

    async def execute_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        loop_over_values: list[Any],
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        for loop_idx, loop_over_value in enumerate(loop_over_values):
            # Check max_iterations limit
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    f"ForLoopBlock: Reached max_iterations limit ({DEFAULT_MAX_LOOP_ITERATIONS}), stopping loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    max_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Reached max_loop_iterations limit of {DEFAULT_MAX_LOOP_ITERATIONS}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                block_outputs.append(failure_block_result)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )
            LOG.info("Starting loop iteration", loop_idx=loop_idx, loop_over_value=loop_over_value)
            # context parameter has been deprecated. However, it's still used by task v2 - we should migrate away from it.
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)

            each_loop_output_values: list[dict[str, Any]] = []

            # Track steps for current iteration
            iteration_step_count = 0
            LOG.info(
                f"ForLoopBlock: Starting iteration {loop_idx} with max_steps_per_iteration={DEFAULT_MAX_STEPS_PER_ITERATION}",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            for block_idx, loop_block in enumerate(self.loop_blocks):
                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": loop_over_value,
                    "current_item": loop_over_value,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.copy()
                current_block = loop_block

                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                # Log the output value for debugging
                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_value=output_value)

                # Log URL information for goto_url blocks
                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", url=loop_block.url, loop_idx=loop_idx)
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

                # Check max_steps_per_iteration limit after each block execution
                iteration_step_count += 1  # Count each block execution as a step
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        f"ForLoopBlock: Reached max_steps_per_iteration limit ({DEFAULT_MAX_STEPS_PER_ITERATION}) in iteration {loop_idx}, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    # Create a failure block result for this iteration
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )
                    block_outputs.append(failure_block_result)
                    # If continue_on_failure is False, stop the entire loop
                    if not self.continue_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    # If continue_on_failure is True, break out of the block loop for this iteration
                    break

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
            loop_over_values = await self.get_loop_over_parameter_values(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
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
            browser_session_id=browser_session_id,
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


class Credential(SimpleNamespace):
    pass


class CodeBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CODE] = BlockType.CODE  # type: ignore

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
            "re": re,
            "json": json,
            "Exception": Exception,
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
        await app.AGENT_FUNCTION.validate_code_block(organization_id=organization_id)

        # TODO: only support to use code block to manupilate the browser page
        # support browser context in the future
        browser_state: BrowserState | None = None
        if browser_session_id and organization_id:
            LOG.info(
                "Getting browser state for workflow run from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id, organization_id)
            if browser_state:
                LOG.info("Was occupying session here, but no longer.", browser_session_id=browser_session_id)
        else:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)

        # If no browser state exists, create one (e.g., when code block is the first block)
        if not browser_state:
            LOG.info(
                "No browser state found, creating one for code block execution",
                workflow_run_id=workflow_run_id,
                browser_session_id=browser_session_id,
            )
            workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            try:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url=None,  # Code block doesn't need to navigate to a URL initially
                    browser_session_id=browser_session_id,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
                # Ensure the browser state has a working page
                await browser_state.check_and_fix_state(
                    url=None,  # Don't navigate to any URL, just ensure a page exists
                    proxy_location=workflow_run.proxy_location,
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                    extra_http_headers=workflow_run.extra_http_headers,
                    browser_address=workflow_run.browser_address,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
            except Exception as e:
                LOG.exception(
                    "Failed to create browser state for code block",
                    workflow_run_id=workflow_run_id,
                    error=str(e),
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"Failed to create browser for code block: {str(e)}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

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
            if not parameter.parameter_type.is_secret_or_credential() and not (
                # NOTE: skyvern credential is a 'credential_id' workflow parameter type
                parameter.parameter_type == ParameterType.WORKFLOW
                and parameter.workflow_parameter_type is not None
                and parameter.workflow_parameter_type.is_credential_type()
            ):
                parameter_values[parameter.key] = value
                continue
            if isinstance(value, dict):
                real_secret_values = {}
                for credential_field, credential_place_holder in value.items():
                    # "context" is a skyvern-defined field to reduce LLM hallucination
                    if credential_field == "context":
                        continue
                    secret_value = workflow_run_context.get_original_secret_value_or_none(credential_place_holder)
                    if (
                        secret_value == BitwardenConstants.TOTP
                        or secret_value == OnePasswordConstants.TOTP
                        or secret_value == AzureVaultConstants.TOTP
                    ):
                        totp_secret_key = workflow_run_context.totp_secret_value_key(credential_place_holder)
                        totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
                        if totp_secret:
                            secret_value = pyotp.TOTP(totp_secret).now()
                        else:
                            LOG.warning(
                                "No TOTP secret found, returning the parameter value as is",
                                parameter=credential_place_holder,
                            )

                    real_secret_value = secret_value if secret_value is not None else credential_place_holder
                    parameter_values[credential_field] = real_secret_value
                    real_secret_values[credential_field] = real_secret_value
                parameter_values[parameter.key] = Credential(**real_secret_values)
            else:
                secret_value = workflow_run_context.get_original_secret_value_or_none(value)
                parameter_values[parameter.key] = secret_value if secret_value is not None else value

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


class TextPromptBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT  # type: ignore

    llm_key: str | None = None
    prompt: str
    parameters: list[PARAMETER_TYPE] = []
    json_schema: dict[str, Any] | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.llm_key:
            self.llm_key = self.format_block_parameter_template_from_workflow_run_context(
                self.llm_key, workflow_run_context
            )
        self.prompt = self.format_block_parameter_template_from_workflow_run_context(self.prompt, workflow_run_context)

    async def send_prompt(self, prompt: str, parameter_values: dict[str, Any]) -> dict[str, Any]:
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key or self.llm_key, default=app.LLM_API_HANDLER
        )
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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.DOWNLOAD_TO_S3] = BlockType.DOWNLOAD_TO_S3  # type: ignore

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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.UPLOAD_TO_S3] = BlockType.UPLOAD_TO_S3  # type: ignore

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
            context = skyvern_context.current()
            self.path = str(
                get_path_for_workflow_download_directory(
                    context.run_id if context and context.run_id else workflow_run_id
                ).absolute()
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


class FileUploadBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_UPLOAD] = BlockType.FILE_UPLOAD  # type: ignore

    storage_type: FileStorageType = FileStorageType.S3
    s3_bucket: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    region_name: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_blob_container_name: str | None = None
    path: str | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        parameters = []

        if self.path and workflow_run_context.has_parameter(self.path):
            parameters.append(workflow_run_context.get_parameter(self.path))

        if self.s3_bucket and workflow_run_context.has_parameter(self.s3_bucket):
            parameters.append(workflow_run_context.get_parameter(self.s3_bucket))

        if self.aws_access_key_id and workflow_run_context.has_parameter(self.aws_access_key_id):
            parameters.append(workflow_run_context.get_parameter(self.aws_access_key_id))

        if self.aws_secret_access_key and workflow_run_context.has_parameter(self.aws_secret_access_key):
            parameters.append(workflow_run_context.get_parameter(self.aws_secret_access_key))

        if self.azure_storage_account_name and workflow_run_context.has_parameter(self.azure_storage_account_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_name))

        if self.azure_storage_account_key and workflow_run_context.has_parameter(self.azure_storage_account_key):
            parameters.append(workflow_run_context.get_parameter(self.azure_storage_account_key))

        if self.azure_blob_container_name and workflow_run_context.has_parameter(self.azure_blob_container_name):
            parameters.append(workflow_run_context.get_parameter(self.azure_blob_container_name))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.path:
            self.path = self.format_block_parameter_template_from_workflow_run_context(self.path, workflow_run_context)

        if self.s3_bucket:
            self.s3_bucket = self.format_block_parameter_template_from_workflow_run_context(
                self.s3_bucket, workflow_run_context
            )
        if self.aws_access_key_id:
            self.aws_access_key_id = self.format_block_parameter_template_from_workflow_run_context(
                self.aws_access_key_id, workflow_run_context
            )
        if self.aws_secret_access_key:
            self.aws_secret_access_key = self.format_block_parameter_template_from_workflow_run_context(
                self.aws_secret_access_key, workflow_run_context
            )
        if self.azure_storage_account_name:
            self.azure_storage_account_name = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_storage_account_name, workflow_run_context
            )
        if self.azure_storage_account_key:
            self.azure_storage_account_key = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_storage_account_key, workflow_run_context
            )
        if self.azure_blob_container_name:
            self.azure_blob_container_name = self.format_block_parameter_template_from_workflow_run_context(
                self.azure_blob_container_name, workflow_run_context
            )

    def _get_s3_uri(self, workflow_run_id: str, path: str) -> str:
        folder_path = self.path or f"{workflow_run_id}"
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        s3_suffix = f"{uuid.uuid4()}_{Path(path).name}"
        return f"s3://{self.s3_bucket}/{folder_path}/{s3_suffix}"

    def _get_azure_blob_name(self, workflow_run_id: str, file_path: str) -> str:
        blob_name = f"{uuid.uuid4()}_{Path(file_path).name}"
        folder_path = self.path or workflow_run_id
        # Remove trailing slash from folder_path to avoid double slashes
        folder_path = folder_path.rstrip("/")
        # Remove any empty path segments to avoid double slashes
        folder_path = "/".join(segment for segment in folder_path.split("/") if segment)
        return folder_path + "/" + blob_name

    def _get_azure_blob_uri(self, workflow_run_id: str, blob_name: str) -> str:
        return f"https://{self.azure_storage_account_name}.blob.core.windows.net/{self.azure_blob_container_name}/{blob_name}"

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
        # data validate before uploading
        missing_parameters = []
        if self.storage_type == FileStorageType.S3:
            if not self.s3_bucket:
                missing_parameters.append("s3_bucket")
            if not self.aws_access_key_id:
                missing_parameters.append("aws_access_key_id")
            if not self.aws_secret_access_key:
                missing_parameters.append("aws_secret_access_key")
        elif self.storage_type == FileStorageType.AZURE:
            if not self.azure_storage_account_name or self.azure_storage_account_name == "":
                missing_parameters.append("azure_storage_account_name")
            if not self.azure_storage_account_key or self.azure_storage_account_key == "":
                missing_parameters.append("azure_storage_account_key")
            if not self.azure_blob_container_name or self.azure_blob_container_name == "":
                missing_parameters.append("azure_blob_container_name")
        else:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Unsupported storage type: {self.storage_type}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if missing_parameters:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Required block values are missing in the FileUploadBlock (label: {self.label}): {', '.join(missing_parameters)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
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

        context = skyvern_context.current()
        download_files_path = str(
            get_path_for_workflow_download_directory(
                context.run_id if context and context.run_id else workflow_run_id
            ).absolute()
        )

        uploaded_uris = []
        try:
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            files_to_upload = []
            if os.path.isdir(download_files_path):
                files = os.listdir(download_files_path)
                max_file_count = (
                    MAX_UPLOAD_FILE_COUNT
                    if self.storage_type == FileStorageType.S3
                    else AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT
                )
                if len(files) > max_file_count:
                    raise ValueError(f"Too many files in the directory, not uploading. Max: {max_file_count}")
                for file in files:
                    if os.path.isdir(os.path.join(download_files_path, file)):
                        LOG.warning("FileUploadBlock: Skipping directory", file=file)
                        continue
                    files_to_upload.append(os.path.join(download_files_path, file))
            else:
                files_to_upload.append(download_files_path)

            if self.storage_type == FileStorageType.S3:
                actual_aws_access_key_id = (
                    workflow_run_context.get_original_secret_value_or_none(self.aws_access_key_id)
                    or self.aws_access_key_id
                )
                actual_aws_secret_access_key = (
                    workflow_run_context.get_original_secret_value_or_none(self.aws_secret_access_key)
                    or self.aws_secret_access_key
                )
                aws_client = AsyncAWSClient(
                    aws_access_key_id=actual_aws_access_key_id,
                    aws_secret_access_key=actual_aws_secret_access_key,
                    region_name=self.region_name,
                )
                for file_path in files_to_upload:
                    s3_uri = self._get_s3_uri(workflow_run_id, file_path)
                    uploaded_uris.append(s3_uri)
                    await aws_client.upload_file_from_path(uri=s3_uri, file_path=file_path, raise_exception=True)
                LOG.info("FileUploadBlock: File(s) uploaded to S3", file_path=self.path)
            elif self.storage_type == FileStorageType.AZURE:
                actual_azure_storage_account_name = (
                    workflow_run_context.get_original_secret_value_or_none(self.azure_storage_account_name)
                    or self.azure_storage_account_name
                )
                actual_azure_storage_account_key = (
                    workflow_run_context.get_original_secret_value_or_none(self.azure_storage_account_key)
                    or self.azure_storage_account_key
                )
                if actual_azure_storage_account_name is None or actual_azure_storage_account_key is None:
                    raise AzureConfigurationError("Azure Storage is not configured")

                azure_client = AsyncAzureStorageClient(
                    storage_account_name=actual_azure_storage_account_name,
                    storage_account_key=actual_azure_storage_account_key,
                )
                for file_path in files_to_upload:
                    LOG.info("FileUploadBlock: Uploading file to Azure Blob Storage", file_path=file_path)
                    blob_name = self._get_azure_blob_name(workflow_run_id, file_path)
                    azure_uri = self._get_azure_blob_uri(workflow_run_id, blob_name)
                    uploaded_uris.append(azure_uri)
                    await azure_client.upload_file_from_path(
                        container_name=self.azure_blob_container_name or "", blob_name=blob_name, file_path=file_path
                    )
                LOG.info("FileUploadBlock: File(s) uploaded to Azure Blob Storage", file_path=self.path)
            else:
                # This case should ideally be caught by the initial validation
                raise ValueError(f"Unsupported storage type: {self.storage_type}")

        except Exception as e:
            LOG.exception("FileUploadBlock: Failed to upload file", file_path=self.path, storage_type=self.storage_type)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to upload file to {self.storage_type}: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, uploaded_uris)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=uploaded_uris,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class SendEmailBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.SEND_EMAIL] = BlockType.SEND_EMAIL  # type: ignore

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

        # Format recipients
        formatted_recipients = []
        for recipient in self.recipients:
            formatted_recipient = self.format_block_parameter_template_from_workflow_run_context(
                recipient, workflow_run_context
            )
            formatted_recipients.append(formatted_recipient)
        self.recipients = formatted_recipients

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
                context = skyvern_context.current()
                path = str(
                    get_path_for_workflow_download_directory(
                        context.run_id if context and context.run_id else workflow_run_id
                    ).absolute()
                )
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
            # Check if the recipient is a parameter and get its value
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


class FileParserBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_URL_PARSER] = BlockType.FILE_URL_PARSER  # type: ignore

    file_url: str
    file_type: FileType
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

    def _detect_file_type_from_url(self, file_url: str) -> FileType:
        """Detect file type based on file extension in the URL."""
        url_parsed = urlparse(file_url)
        # TODO: use filetype.guess(file_path) to make the detection more robust
        suffix = Path(url_parsed.path).suffix.lower()
        if suffix in (".xlsx", ".xls", ".xlsm"):
            return FileType.EXCEL
        elif suffix == ".pdf":
            return FileType.PDF
        elif suffix == ".tsv":
            return FileType.CSV  # TSV files are handled by the CSV parser
        else:
            return FileType.CSV  # Default to CSV for .csv and any other extensions

    def validate_file_type(self, file_url_used: str, file_path: str) -> None:
        if self.file_type == FileType.CSV:
            try:
                with open(file_path) as file:
                    csv.Sniffer().sniff(file.read(1024))
            except csv.Error as e:
                raise InvalidFileType(file_url=file_url_used, file_type=self.file_type, error=str(e))
        elif self.file_type == FileType.EXCEL:
            try:
                # Try to read the file with pandas to validate it's a valid Excel file
                pd.read_excel(file_path, nrows=1, engine="openpyxl")
            except Exception as e:
                raise InvalidFileType(
                    file_url=file_url_used, file_type=self.file_type, error=f"Invalid Excel file format: {str(e)}"
                )
        elif self.file_type == FileType.PDF:
            try:
                # Try to read the file with PyPDF to validate it's a valid PDF file
                reader = PdfReader(file_path)
                # Just check if we can access pages, don't read content yet
                _ = len(reader.pages)
            except Exception as e:
                raise InvalidFileType(file_url=file_url_used, file_type=self.file_type, error=str(e))

    async def _parse_csv_file(self, file_path: str) -> list[dict[str, Any]]:
        """Parse CSV/TSV file and return list of dictionaries."""
        parsed_data = []
        with open(file_path) as file:
            # Try to detect the delimiter (comma for CSV, tab for TSV)
            sample = file.read(1024)
            file.seek(0)  # Reset file pointer

            # Use csv.Sniffer to detect the delimiter
            try:
                dialect = csv.Sniffer().sniff(sample)
                delimiter = dialect.delimiter
            except csv.Error:
                # Default to comma if detection fails
                delimiter = ","

            reader = csv.DictReader(file, delimiter=delimiter)
            for row in reader:
                parsed_data.append(row)
        return parsed_data

    def _clean_dataframe_for_json(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        """Clean DataFrame to ensure it can be serialized to JSON."""
        # Replace NaN and NaT values with "nan" string
        df_cleaned = df.replace({pd.NA: "nan", pd.NaT: "nan"})
        df_cleaned = df_cleaned.where(pd.notna(df_cleaned), "nan")

        # Convert to list of dictionaries
        records = df_cleaned.to_dict("records")

        # Additional cleaning for any remaining problematic values
        for record in records:
            for key, value in record.items():
                if pd.isna(value) or value == "NaN" or value == "NaT":
                    record[key] = "nan"
                elif isinstance(value, (pd.Timestamp, pd.DatetimeTZDtype)):
                    # Convert pandas timestamps to ISO format strings
                    record[key] = value.isoformat() if pd.notna(value) else "nan"

        return records

    async def _parse_excel_file(self, file_path: str) -> list[dict[str, Any]]:
        """Parse Excel file and return list of dictionaries."""
        try:
            # Read Excel file with pandas, specifying engine explicitly
            df = pd.read_excel(file_path, engine="openpyxl")
            # Clean and convert DataFrame to list of dictionaries
            return self._clean_dataframe_for_json(df)
        except ImportError as e:
            raise InvalidFileType(
                file_url=self.file_url,
                file_type=self.file_type,
                error=f"Missing required dependency for Excel parsing: {str(e)}. Please install openpyxl: pip install openpyxl",
            )
        except Exception as e:
            raise InvalidFileType(
                file_url=self.file_url, file_type=self.file_type, error=f"Failed to parse Excel file: {str(e)}"
            )

    async def _parse_pdf_file(self, file_path: str) -> str:
        """Parse PDF file and return extracted text."""
        try:
            reader = PdfReader(file_path)
            extracted_text = ""
            page_count = len(reader.pages)
            for i in range(page_count):
                extracted_text += reader.pages[i].extract_text() + "\n"
            return extracted_text
        except PdfReadError as e:
            raise InvalidFileType(file_url=self.file_url, file_type=self.file_type, error=str(e))

    async def _extract_with_ai(
        self, content: str | list[dict[str, Any]], workflow_run_context: WorkflowRunContext
    ) -> dict[str, Any]:
        """Extract structured data using AI based on json_schema."""
        # Use local variable to avoid mutating the instance
        schema_to_use = self.json_schema or {
            "type": "object",
            "properties": {
                "output": {
                    "type": "object",
                    "description": "Information extracted from the file",
                }
            },
        }

        # Convert content to string for AI processing
        if isinstance(content, list):
            # For CSV/Excel data, convert to a readable format
            content_str = json.dumps(content, indent=2)
        else:
            content_str = content

        llm_prompt = prompt_engine.load_prompt(
            "extract-information-from-file-text", extracted_text_content=content_str, json_schema=schema_to_use
        )

        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key, default=app.LLM_API_HANDLER
        )

        llm_response = await llm_api_handler(prompt=llm_prompt, prompt_name="extract-information-from-file-text")
        return llm_response

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

        # Auto-detect file type based on file extension
        detected_file_type = self._detect_file_type_from_url(self.file_url)
        self.file_type = detected_file_type

        # Validate the file type
        self.validate_file_type(self.file_url, file_path)

        LOG.debug(
            "FileParserBlock: After file type validation",
            file_type=self.file_type,
            json_schema_present=self.json_schema is not None,
            json_schema_type=type(self.json_schema),
        )

        # Parse the file based on type
        parsed_data: str | list[dict[str, Any]]
        if self.file_type == FileType.CSV:
            parsed_data = await self._parse_csv_file(file_path)
        elif self.file_type == FileType.EXCEL:
            parsed_data = await self._parse_excel_file(file_path)
        elif self.file_type == FileType.PDF:
            parsed_data = await self._parse_pdf_file(file_path)
        else:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Unsupported file type: {self.file_type}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # If json_schema is provided, use AI to extract structured data
        final_data: str | list[dict[str, Any]] | dict[str, Any]
        LOG.debug(
            "FileParserBlock: JSON schema check",
            has_json_schema=self.json_schema is not None,
            json_schema_type=type(self.json_schema),
            json_schema=self.json_schema,
        )

        if self.json_schema:
            try:
                ai_extracted_data = await self._extract_with_ai(parsed_data, workflow_run_context)
                final_data = ai_extracted_data
            except Exception as e:
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"Failed to extract data with AI: {str(e)}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
        else:
            # Return raw parsed data
            final_data = parsed_data

        # Record the parsed data
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, final_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=final_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class PDFParserBlock(Block):
    """
    DEPRECATED: Use FileParserBlock with file_type=FileType.PDF instead.
    This block will be removed in a future version.
    """

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.PDF_PARSER] = BlockType.PDF_PARSER  # type: ignore

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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.WAIT] = BlockType.WAIT  # type: ignore

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


class HumanInteractionBlock(BaseTaskBlock):
    """
    A block for human/agent interaction.

    For the first pass at this, the implicit behaviour is that the user is given a single binary
    choice (a go//no-go).

    If the human:
      - chooses positively, the workflow continues
      - chooses negatively, the workflow is terminated
      - does not respond within the timeout period, the workflow terminates
    """

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.HUMAN_INTERACTION] = BlockType.HUMAN_INTERACTION  # type: ignore

    instructions: str = "Please review and approve or reject to continue the workflow."
    positive_descriptor: str = "Approve"
    negative_descriptor: str = "Reject"
    timeout_seconds: int = 60 * 60 * 2  # two hours

    # email options
    sender: str = "hello@skyvern.com"
    recipients: list[str] = []
    subject: str = "Human interaction required for workflow run"
    body: str = "Your interaction is required for a workflow run!"

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        super().format_potential_template_parameters(workflow_run_context)

        self.instructions = self.format_block_parameter_template_from_workflow_run_context(
            self.instructions, workflow_run_context
        )

        self.body = self.format_block_parameter_template_from_workflow_run_context(self.body, workflow_run_context)

        self.subject = self.format_block_parameter_template_from_workflow_run_context(
            self.subject, workflow_run_context
        )

        formatted: list[str] = []
        for recipient in self.recipients:
            formatted.append(
                self.format_block_parameter_template_from_workflow_run_context(recipient, workflow_run_context)
            )

        self.recipients = formatted

        self.negative_descriptor = self.format_block_parameter_template_from_workflow_run_context(
            self.negative_descriptor, workflow_run_context
        )

        self.positive_descriptor = self.format_block_parameter_template_from_workflow_run_context(
            self.positive_descriptor, workflow_run_context
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # avoid circular import
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus  # noqa: PLC0415

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

        await app.DATABASE.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            recipients=self.recipients,
            subject=self.subject,
            body=self.body,
            instructions=self.instructions,
            positive_descriptor=self.positive_descriptor,
            negative_descriptor=self.negative_descriptor,
        )

        LOG.info(
            "Pausing workflow for human interaction",
            workflow_run_id=workflow_run_id,
            recipients=self.recipients,
            timeout=self.timeout_seconds,
            browser_session_id=browser_session_id,
        )

        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.paused,
        )

        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        if not workflow_run:
            return await self.build_block_result(
                success=False,
                failure_reason="Workflow run not found",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        app_url = f"{settings.SKYVERN_APP_URL}/runs/{workflow_run_id}/overview"
        body = f"{self.body}\n\nKindly visit {app_url}\n\n{self.instructions}\n\n"
        subject = f"{self.subject} - Workflow Run ID: {workflow_run_id}"

        try:
            await email.send(
                body=body,
                sender=self.sender,
                subject=subject,
                recipients=self.recipients,
            )

            email_success = True
            email_failure_reason = None
        except Exception as ex:
            LOG.error(
                "Failed to send human interaction email",
                workflow_run_id=workflow_run_id,
                error=str(ex),
                browser_session_id=browser_session_id,
            )
            email_success = False
            email_failure_reason = str(ex)

        if not email_success:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to send human interaction email: {email_failure_reason or 'email failed'}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Wait for the timeout_seconds or until the workflow run status changes from paused
        start_time = asyncio.get_event_loop().time()
        check_interval = 5  # Check every 5 seconds
        log_that_we_are_waiting = True
        log_wait = 0

        while True:
            if not log_that_we_are_waiting:
                log_wait += check_interval
                if log_wait >= 60:  # Log every 1 minute
                    log_that_we_are_waiting = True
                    log_wait = 0

            elapsed_time_seconds = asyncio.get_event_loop().time() - start_time

            if log_that_we_are_waiting:
                LOG.info(
                    "Waiting for human interaction...",
                    workflow_run_id=workflow_run_id,
                    elapsed_time_seconds=elapsed_time_seconds,
                    timeout_seconds=self.timeout_seconds,
                    browser_session_id=browser_session_id,
                )
                log_that_we_are_waiting = False

            # Check if timeout_seconds has elapsed
            if elapsed_time_seconds >= self.timeout_seconds:
                LOG.info(
                    "Human Interaction block timeout_seconds reached",
                    workflow_run_id=workflow_run_id,
                    elapsed_time_seconds=elapsed_time_seconds,
                    browser_session_id=browser_session_id,
                )

                workflow_run_context = self.get_workflow_run_context(workflow_run_id)
                success = False
                reason = "Timeout elapsed with no human interaction"
                result_dict = {"success": success, "reason": reason}

                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)

                return await self.build_block_result(
                    success=success,
                    failure_reason=reason,
                    output_parameter_value=result_dict,
                    status=BlockStatus.timed_out,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )

            if workflow_run and workflow_run.status != WorkflowRunStatus.paused:
                LOG.info(
                    "Workflow run status changed from paused",
                    workflow_run_id=workflow_run_id,
                    new_status=workflow_run.status,
                    browser_session_id=browser_session_id,
                )

                workflow_run_context = self.get_workflow_run_context(workflow_run_id)
                result_dict = {"success": True, "reason": f"status_changed:{workflow_run.status}"}

                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result_dict)

                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=result_dict,
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            await asyncio.sleep(min(check_interval, self.timeout_seconds - elapsed_time_seconds))


class ValidationBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.VALIDATION] = BlockType.VALIDATION  # type: ignore

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
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.ACTION] = BlockType.ACTION  # type: ignore


class NavigationBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.NAVIGATION] = BlockType.NAVIGATION  # type: ignore

    navigation_goal: str


class ExtractionBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.EXTRACTION] = BlockType.EXTRACTION  # type: ignore

    data_extraction_goal: str


class LoginBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN  # type: ignore


class FileDownloadBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD  # type: ignore


class UrlBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.GOTO_URL] = BlockType.GOTO_URL  # type: ignore
    url: str


class TaskV2Block(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TaskV2] = BlockType.TaskV2  # type: ignore
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

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.prompt = self.format_block_parameter_template_from_workflow_run_context(self.prompt, workflow_run_context)
        if self.url:
            self.url = self.format_block_parameter_template_from_workflow_run_context(self.url, workflow_run_context)

        if self.totp_identifier:
            self.totp_identifier = self.format_block_parameter_template_from_workflow_run_context(
                self.totp_identifier, workflow_run_context
            )

        if self.totp_verification_url:
            self.totp_verification_url = self.format_block_parameter_template_from_workflow_run_context(
                self.totp_verification_url, workflow_run_context
            )
            self.totp_verification_url = prepend_scheme_and_validate_url(self.totp_verification_url)

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus  # noqa: PLC0415
        from skyvern.services import task_v2_service  # noqa: PLC0415

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Simple template resolution - no complex dynamic resolution to prevent recursion
        try:
            self.format_potential_template_parameters(workflow_run_context)

            # Use the resolved values directly
            resolved_prompt = self.prompt
            resolved_url = self.url
            resolved_totp_identifier = self.totp_identifier
            resolved_totp_verification_url = self.totp_verification_url

        except Exception as e:
            output_reason = f"Failed to format jinja template: {str(e)}"
            await self.record_output_parameter_value(
                workflow_run_context, workflow_run_id, {"failure_reason": output_reason}
            )
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not resolved_url:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if browser_state:
                page = await browser_state.get_working_page()
                if page:
                    current_url = await SkyvernFrame.get_url(frame=page)
                    if current_url != "about:blank":
                        resolved_url = current_url

        if not organization_id:
            raise ValueError("Running TaskV2Block requires organization_id")

        organization = await app.DATABASE.get_organization(organization_id)
        if not organization:
            raise ValueError(f"Organization not found {organization_id}")
        workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id, organization_id)
        if not workflow_run:
            raise ValueError(f"WorkflowRun not found {workflow_run_id} when running TaskV2Block")
        try:
            task_v2 = await task_v2_service.initialize_task_v2(
                organization=organization,
                user_prompt=resolved_prompt,
                user_url=resolved_url,
                parent_workflow_run_id=workflow_run_id,
                proxy_location=workflow_run.proxy_location,
                totp_identifier=resolved_totp_identifier,
                totp_verification_url=resolved_totp_verification_url,
                max_screenshot_scrolling_times=workflow_run.max_screenshot_scrolls,
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
        finally:
            context: skyvern_context.SkyvernContext | None = skyvern_context.current()
            current_run_id = context.run_id if context and context.run_id else workflow_run_id
            root_workflow_run_id = (
                context.root_workflow_run_id if context and context.root_workflow_run_id else workflow_run_id
            )
            skyvern_context.set(
                skyvern_context.SkyvernContext(
                    organization_id=organization_id,
                    organization_name=organization.organization_name,
                    workflow_id=workflow_run.workflow_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    workflow_run_id=workflow_run_id,
                    root_workflow_run_id=root_workflow_run_id,
                    run_id=current_run_id,
                    browser_session_id=browser_session_id,
                    max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
                )
            )
        result_dict = None
        if task_v2:
            result_dict = task_v2.output

        # Determine block status from task status using module-level mapping
        block_status = TASKV2_TO_BLOCK_STATUS.get(task_v2.status, BlockStatus.failed)
        success = task_v2.status == TaskV2Status.completed
        failure_reason: str | None = None
        task_v2_workflow_run_id = task_v2.workflow_run_id
        if task_v2_workflow_run_id:
            task_v2_workflow_run = await app.DATABASE.get_workflow_run(task_v2_workflow_run_id, organization_id)
            if task_v2_workflow_run:
                failure_reason = task_v2_workflow_run.failure_reason

        # If continue_on_failure is True, we treat the block as successful even if the task failed
        # This allows the workflow to continue execution despite this block's failure
        task_v2_output = {
            "task_id": task_v2.observer_cruise_id,
            "status": task_v2.status,
            "summary": task_v2.summary,
            "extracted_information": result_dict,
            "failure_reason": failure_reason,
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, task_v2_output)
        return await self.build_block_result(
            success=success or self.continue_on_failure,
            failure_reason=failure_reason,
            output_parameter_value=result_dict,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class HttpRequestBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.HTTP_REQUEST] = BlockType.HTTP_REQUEST  # type: ignore

    # Individual HTTP parameters
    method: str = "GET"
    url: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None  # Changed to consistently be dict only
    timeout: int = 30
    follow_redirects: bool = True

    # Parameters for templating
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = self.parameters
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Check if url is a parameter
        if self.url and workflow_run_context.has_parameter(self.url):
            if self.url not in [parameter.key for parameter in parameters]:
                parameters.append(workflow_run_context.get_parameter(self.url))

        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        """Format template parameters in the block fields"""
        if self.url:
            self.url = self.format_block_parameter_template_from_workflow_run_context(self.url, workflow_run_context)

        if self.body:
            # If body is provided as a template string, try to parse it as JSON
            for key, value in self.body.items():
                if isinstance(value, str):
                    self.body[key] = self.format_block_parameter_template_from_workflow_run_context(
                        value, workflow_run_context
                    )

        if self.headers:
            for key, value in self.headers.items():
                self.headers[key] = self.format_block_parameter_template_from_workflow_run_context(
                    value, workflow_run_context
                )

    def validate_url(self, url: str) -> bool:
        """Validate if the URL is properly formatted"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        """Execute the HTTP request and return the response"""

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

        # Validate URL
        if not self.url:
            return await self.build_block_result(
                success=False,
                failure_reason="URL is required for HTTP request",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if not self.validate_url(self.url):
            return await self.build_block_result(
                success=False,
                failure_reason=f"Invalid URL format: {self.url}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Execute HTTP request using the generic aiohttp_request function
        try:
            LOG.info(
                "Executing HTTP request",
                method=self.method,
                url=self.url,
                headers=self.headers,
                workflow_run_id=workflow_run_id,
                body=self.body,
            )

            # Use the generic aiohttp_request function
            status_code, response_headers, response_body = await aiohttp_request(
                method=self.method,
                url=self.url,
                headers=self.headers,
                json_data=self.body,
                timeout=self.timeout,
                follow_redirects=self.follow_redirects,
            )

            response_data = {
                # Response information
                "status_code": status_code,
                "response_headers": response_headers,
                "response_body": response_body,
                # Request information (what was sent)
                "request_method": self.method,
                "request_url": self.url,
                "request_headers": self.headers,
                "request_body": self.body,
                # Backwards compatibility
                "headers": response_headers,
                "body": response_body,
                "url": self.url,
            }

            LOG.info(
                "HTTP request completed",
                status_code=status_code,
                url=self.url,
                method=self.method,
                workflow_run_id=workflow_run_id,
            )

            # Determine success based on status code
            success = 200 <= status_code < 300

            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response_data)

            return await self.build_block_result(
                success=success,
                failure_reason=None if success else f"HTTP {status_code}: {response_body}",
                output_parameter_value=response_data,
                status=BlockStatus.completed if success else BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        except asyncio.TimeoutError:
            error_data = {"error": "Request timed out", "error_type": "timeout"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Request timed out after {self.timeout} seconds",
                output_parameter_value=error_data,
                status=BlockStatus.timed_out,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            error_data = {"error": str(e), "error_type": "unknown"}
            LOG.warning(  # Changed from LOG.exception to LOG.warning as requested
                "HTTP request failed with unexpected error",
                error=str(e),
                url=self.url,
                method=self.method,
                workflow_run_id=workflow_run_id,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"HTTP request failed: {str(e)}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )


def get_all_blocks(blocks: list[BlockTypeVar]) -> list[BlockTypeVar]:
    """
    Recursively get "all blocks" in a workflow definition.

    At time of writing, blocks can be nested via the ForLoop block. This function
    returns all blocks, flattened.
    """

    all_blocks: list[BlockTypeVar] = []

    for block in blocks:
        all_blocks.append(block)

        if block.block_type == BlockType.FOR_LOOP:
            nested_blocks = get_all_blocks(block.loop_blocks)
            all_blocks.extend(nested_blocks)

    return all_blocks


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
    HumanInteractionBlock,
    FileDownloadBlock,
    UrlBlock,
    TaskV2Block,
    FileUploadBlock,
    HttpRequestBlock,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]
