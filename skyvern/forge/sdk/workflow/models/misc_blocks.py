from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import smtplib
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar, Literal, cast
from urllib.parse import urlparse

import aiofiles
import aiohttp
import filetype
import structlog
from email_validator import EmailNotValidError, validate_email
from jinja2 import TemplateSyntaxError

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import DownloadFileMaxSizeExceeded, get_user_facing_exception_message
from skyvern.forge import app
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    download_file,
    get_download_dir,
    get_path_for_workflow_download_directory,
    is_remote_url,
    parse_uri_to_path,
    resolve_run_download_id,
    validate_local_file_path,
)
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat, InvalidLLMResponseType
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    FailedToFormatJinjaStyleParameter,
    InvalidEmailClientConfiguration,
    InvalidWorkflowDefinition,
    NoValidEmailRecipient,
    PayloadTemplateRenderError,
    PayloadTemplateSyntaxError,
)
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models._jinja import (
    _JSON_TYPE_MARKER,
    jinja_json_finalize_strict_env,
    render_templates_in_json_value,
)
from skyvern.forge.sdk.workflow.models.block import (
    SCHEMA_VALIDATION_MAX_ATTEMPTS,
    SCHEMA_VALIDATION_MAX_ERRORS,
    TASKV2_TO_BLOCK_STATUS,
    _build_schema_validation_retry_prompt,
    _default_text_prompt_schema,
    _format_payload_path_segment,
    _is_schema_configuration_failure,
    _llm_response_format_failure_reason,
    _validate_response_against_json_schema,
    sanitize_filename,
)
from skyvern.forge.sdk.workflow.models.block_base import (
    Block,
    capture_block_download_baseline,
    jinja_sandbox_env,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, AWSSecretParameter
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
_BLOCK_MODULE = "skyvern.forge.sdk.workflow.models.block"


class TextPromptBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.TEXT_PROMPT] = BlockType.TEXT_PROMPT  # type: ignore

    llm_key: str | None = None
    prompt: str
    parameters: list[PARAMETER_TYPE] = []
    json_schema: dict[str, Any] | None = None
    schema_validation_max_attempts: ClassVar[int] = SCHEMA_VALIDATION_MAX_ATTEMPTS
    schema_validation_max_errors: ClassVar[int] = SCHEMA_VALIDATION_MAX_ERRORS

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_schema_templates(self, obj: Any, workflow_run_context: WorkflowRunContext) -> Any:
        if isinstance(obj, str):
            try:
                return self.format_block_parameter_template_from_workflow_run_context(obj, workflow_run_context)
            except Exception:
                LOG.warning(
                    "Failed to render Jinja template in json_schema value, using original value",
                    value=obj,
                    block_label=self.label,
                    exc_info=True,
                )
                return obj
        elif isinstance(obj, dict):
            return {k: self._render_schema_templates(v, workflow_run_context) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._render_schema_templates(item, workflow_run_context) for item in obj]
        return obj

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.llm_key:
            self.llm_key = self.format_block_parameter_template_from_workflow_run_context(
                self.llm_key, workflow_run_context
            )
        self.prompt = self.format_block_parameter_template_from_workflow_run_context(self.prompt, workflow_run_context)
        if self.json_schema:
            self.json_schema = self._render_schema_templates(self.json_schema, workflow_run_context)

        self._apply_workflow_system_prompt(workflow_run_context)

    def _validate_response_against_json_schema(self, response: Any) -> str | None:
        return _validate_response_against_json_schema(
            response,
            self.json_schema,
            "Text prompt",
            max_errors=self.schema_validation_max_errors,
        )

    async def send_prompt(
        self,
        prompt: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        workflow_run_block_id: str | None = None,
        schema_validation_failure: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        default_llm_handler = await self._resolve_default_llm_handler(workflow_run_id, organization_id)
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key_for_organization(organization_id) or self.llm_key, default=default_llm_handler
        )
        schema_to_use = json_schema or self.json_schema or _default_text_prompt_schema()

        # `prompt` is already fully rendered by format_potential_template_parameters().
        # Keep send_prompt focused on delivery/retry formatting so parameter values
        # stay literal after that render.
        if schema_validation_failure:
            prompt = _build_schema_validation_retry_prompt(prompt, schema_validation_failure)
        prompt += (
            "\n\n"
            + "Please respond to the prompt above using the following JSON definition:\n\n"
            + "```json\n"
            + json.dumps(schema_to_use, indent=2)
            + "\n```\n\n"
        )

        workflow_run_block = None
        artifacts_to_persist: list[tuple[ArtifactType, bytes]] = []
        if workflow_run_block_id:
            try:
                workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                    workflow_run_block_id, organization_id
                )
                if workflow_run_block:
                    artifacts_to_persist.append((ArtifactType.LLM_PROMPT, prompt.encode("utf-8")))
            except Exception as e:
                LOG.error("Failed to fetch workflow_run_block for TextPromptBlock artifacts", error=e)

        LOG.info(
            "TextPromptBlock Sending prompt to LLM",
            prompt=prompt,
            llm_key=self.llm_key,
        )
        response = await llm_api_handler(
            prompt=prompt,
            prompt_name="text-prompt",
            system_prompt=self.workflow_system_prompt,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            # Schema validation must inspect the raw parsed root; dict coercion can hide wrong-root responses.
            force_dict=False,
        )

        if workflow_run_block:
            artifacts_to_persist.append((ArtifactType.LLM_RESPONSE, json.dumps(response).encode("utf-8")))
            try:
                await app.ARTIFACT_MANAGER.create_workflow_run_block_artifacts(
                    workflow_run_block=workflow_run_block,
                    artifacts=artifacts_to_persist,
                )
            except Exception as e:
                LOG.error("Failed to save TextPromptBlock artifacts", error=e)

        LOG.info("TextPromptBlock Received response from LLM", response=response)
        return response

    async def _resolve_default_llm_handler(self, workflow_run_id: str, organization_id: str | None) -> LLMAPIHandler:
        prompt_config_handler = await get_llm_handler_for_prompt_type("text-prompt", workflow_run_id, organization_id)
        if prompt_config_handler:
            return prompt_config_handler

        secondary_handler = app.SECONDARY_LLM_API_HANDLER
        if secondary_handler:
            return secondary_handler

        LOG.warning(
            "Secondary LLM handler not configured; falling back to primary handler for TextPromptBlock",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return app.LLM_API_HANDLER

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
        await app.DATABASE.observer.update_workflow_run_block(
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
        for parameter in self.parameters:
            if not workflow_run_context.has_value(parameter.key):
                LOG.warning(
                    "TextPromptBlock missing required parameter",
                    block_label=self.label,
                    parameter_key=parameter.key,
                    workflow_run_id=workflow_run_id,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=(
                        f"Parameter '{parameter.key}' is not available in the workflow context. "
                        f"An upstream block that produces this value may have failed or been skipped."
                    ),
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        response: dict[str, Any] | list | str | None = None
        schema_to_use = self.json_schema or _default_text_prompt_schema()
        if not validate_schema(schema_to_use):
            return await self.build_block_result(
                success=False,
                failure_reason="Text prompt JSON schema is invalid.",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        schema_validation_failure_for_retry: str | None = None
        for attempt in range(self.schema_validation_max_attempts):
            try:
                response = await self.send_prompt(
                    self.prompt,
                    workflow_run_id,
                    organization_id,
                    workflow_run_block_id=workflow_run_block_id,
                    schema_validation_failure=schema_validation_failure_for_retry,
                    json_schema=schema_to_use,
                )
            except (InvalidLLMResponseFormat, InvalidLLMResponseType) as e:
                response_format_failure_reason = _llm_response_format_failure_reason(e)
                will_retry = attempt + 1 < self.schema_validation_max_attempts
                LOG.warning(
                    "TextPromptBlock LLM response failed response-format validation",
                    block_label=self.label,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    attempt=attempt + 1,
                    max_attempts=self.schema_validation_max_attempts,
                    will_retry=will_retry,
                    error_type=type(e).__name__,
                    schema_type=schema_to_use.get("type"),
                )
                if not will_retry:
                    return await self.build_block_result(
                        success=False,
                        failure_reason=response_format_failure_reason,
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                schema_validation_failure_for_retry = response_format_failure_reason
                continue
            except Exception as e:
                try:
                    resolved_llm_key = self.override_llm_key_for_organization(organization_id) or self.llm_key
                except Exception:
                    resolved_llm_key = self.llm_key
                LOG.exception(
                    "TextPromptBlock LLM call failed",
                    block_label=self.label,
                    workflow_run_id=workflow_run_id,
                    llm_key=resolved_llm_key,
                )
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"LLM call failed: {e}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            schema_validation_failure = _validate_response_against_json_schema(
                response,
                schema_to_use,
                "Text prompt",
                max_errors=self.schema_validation_max_errors,
            )
            if not schema_validation_failure:
                break

            is_schema_configuration_failure = _is_schema_configuration_failure(schema_validation_failure)
            will_retry = attempt + 1 < self.schema_validation_max_attempts and not is_schema_configuration_failure
            LOG.warning(
                "TextPromptBlock LLM response failed schema validation",
                block_label=self.label,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                attempt=attempt + 1,
                max_attempts=self.schema_validation_max_attempts,
                will_retry=will_retry,
                failure_reason=schema_validation_failure,
                schema_type=schema_to_use.get("type"),
            )
            if not will_retry:
                return await self.build_block_result(
                    success=False,
                    failure_reason=schema_validation_failure,
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            schema_validation_failure_for_retry = schema_validation_failure
            continue

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=response,
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
        context = skyvern_context.current()
        run_id = context.run_id if context and context.run_id else workflow_run_id
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
                path = str(get_path_for_workflow_download_directory(run_id).absolute())
                LOG.info(
                    "SendEmailBlock Using download directory for the workflow run",
                    workflow_run_id=workflow_run_id,
                    file_path=path,
                )

            path = self.format_block_parameter_template_from_workflow_run_context(path, workflow_run_context)
            if not is_remote_url(path):
                path = validate_local_file_path(path, run_id)
            # if the file path is a directory, add all files in the directory, skip directories, limit to 10 files
            if os.path.exists(path):
                if os.path.isdir(path):
                    for file in os.listdir(path):
                        if os.path.isdir(os.path.join(path, file)):
                            LOG.warning("SendEmailBlock Skipping directory", file=file)
                            continue
                        file_path = os.path.join(path, file)
                        file_paths.append(file_path)
                else:
                    # covers the case where the file path is a single file
                    file_paths.append(path)
            elif is_remote_url(path):
                file_paths.append(path)
            else:
                LOG.warning("SendEmailBlock File not found", file_path=path)

        return file_paths

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
                    "SendEmailBlock Invalid email address",
                    recipient=maybe_recipient,
                    reason=str(e),
                )

        if not recipients:
            raise NoValidEmailRecipient(recipients=recipients)

        return recipients

    async def _build_email_message(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        organization_id: str | None = None,
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
            if filename.startswith(("s3://", "gs://", "azure://", "http://", "https://")):
                path = await download_file(filename, organization_id=organization_id)
            else:
                LOG.info("SendEmailBlock Looking for file locally", filename=filename)
                if not os.path.exists(filename):
                    raise FileNotFoundError(f"File not found: {filename}")
                if not os.path.isfile(filename):
                    raise IsADirectoryError(f"Path is a directory: {filename}")

                path = filename
                LOG.info("SendEmailBlock Found file locally", path=path)

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
                "SendEmailBlock Adding attachment",
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
        LOG.info("SendEmailBlock Total files attached", total_files=total_files)
        LOG.info("SendEmailBlock Unique files (based on content) attached", unique_files=unique_files)
        if duplicate_files_list:
            LOG.info(
                "SendEmailBlock Duplicate files (based on content) attached", duplicate_files_list=duplicate_files_list
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
        await app.DATABASE.observer.update_workflow_run_block(
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
            LOG.info("SendEmailBlock Connected to SMTP server")
            smtp_host.starttls()
            smtp_host.login(smtp_username_value, smtp_password_value)
            LOG.info("SendEmailBlock Logged in to SMTP server")
            message = await self._build_email_message(
                workflow_run_context,
                workflow_run_id,
                organization_id=organization_id,
            )
            smtp_host.send_message(message)
            LOG.info("SendEmailBlock Email sent")
        except Exception as e:
            LOG.error("SendEmailBlock Failed to send email", exc_info=True)
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
        await app.DATABASE.observer.update_workflow_run_block(
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

    def _resolve_totp_identifier(self, workflow_run_context: WorkflowRunContext) -> str | None:
        if self.totp_identifier:
            return self.totp_identifier
        if workflow_run_context.credential_totp_identifiers:
            return next(iter(workflow_run_context.credential_totp_identifiers.values()), None)
        return None

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

        # Materialize the workflow-level workflow_system_prompt onto this block so
        # execute() can hand it off to the TaskV2 row verbatim.
        self._apply_workflow_system_prompt(workflow_run_context)

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

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(
                block_context,
                organization_id or "",
                workflow_run_id,
                self.label,
            )

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Simple template resolution - no complex dynamic resolution to prevent recursion
        try:
            self.format_potential_template_parameters(workflow_run_context)

            # Use the resolved values directly
            resolved_prompt = self.prompt
            resolved_url = self.url
            resolved_totp_identifier = self._resolve_totp_identifier(workflow_run_context)
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

        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if not organization:
            raise ValueError(f"Organization not found {organization_id}")
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id)
        if not workflow_run:
            raise ValueError(f"WorkflowRun not found {workflow_run_id} when running TaskV2Block")
        current_context = skyvern_context.current()
        download_lookup_run_id = (
            current_context.run_id if current_context and current_context.run_id else workflow_run_id
        )
        loop_internal_state = copy.deepcopy(current_context.loop_internal_state) if current_context else None
        try:
            # TaskV2Block child runs inherit the parent run's trigger_type so non-UI parents
            # don't silently drop flex-routing eligibility for their TaskV2 children.
            inherited_v2_trigger_type = current_context.trigger_type if current_context else None
            task_v2 = await task_v2_service.initialize_task_v2(
                organization=organization,
                user_prompt=resolved_prompt,
                user_url=resolved_url,
                parent_workflow_run_id=workflow_run_id,
                proxy_location=workflow_run.proxy_location,
                totp_identifier=resolved_totp_identifier,
                totp_verification_url=resolved_totp_verification_url,
                max_screenshot_scrolling_times=workflow_run.max_screenshot_scrolls,
                workflow_system_prompt=self.workflow_system_prompt,
                trigger_type=inherited_v2_trigger_type,
                # Pin the child to the parent's remote browser (CDP address + the
                # headers its handshake authenticates with) so a cloud-browser run
                # doesn't fall back to a fresh local Chrome.
                browser_address=workflow_run.browser_address,
                extra_http_headers=workflow_run.extra_http_headers,
                cdp_connect_headers=workflow_run.cdp_connect_headers,
            )
            await app.DATABASE.observer.update_task_v2(
                task_v2.observer_cruise_id, status=TaskV2Status.queued, organization_id=organization_id
            )
            if task_v2.workflow_run_id:
                await app.DATABASE.workflow_runs.update_workflow_run(
                    workflow_run_id=task_v2.workflow_run_id,
                    status=WorkflowRunStatus.queued,
                )
                await app.DATABASE.observer.update_workflow_run_block(
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    block_workflow_run_id=task_v2.workflow_run_id,
                )
        except Exception as e:
            LOG.exception("Failed to initialize or queue TaskV2", error=e)
            output_reason = f"Failed to initialize or queue TaskV2: {str(e)}"
            await self.record_output_parameter_value(
                workflow_run_context, workflow_run_id, {"failure_reason": output_reason}
            )
            return await self.build_block_result(
                success=False,
                failure_reason=output_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # run_task_v2 uses scoped() internally, so context is always restored
        # even if it raises. Its own exception handlers mark the task as
        # failed/terminated with proper status, so we let exceptions propagate
        # to the status-mapping logic below.
        task_v2 = await task_v2_service.run_task_v2(
            organization=organization,
            task_v2_id=task_v2.observer_cruise_id,
            request_id=None,
            max_steps_override=self.max_steps,
            max_iterations_override=self.max_iterations,
            browser_session_id=browser_session_id,
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
            task_v2_workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                task_v2_workflow_run_id, organization_id
            )
            if task_v2_workflow_run:
                failure_reason = task_v2_workflow_run.failure_reason

        # If continue_on_failure is True, we treat the block as successful even if the task failed
        # This allows the workflow to continue execution despite this block's failure
        task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
            organization_id=organization_id,
            task_v2_id=task_v2.observer_cruise_id,
        )
        workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        # Attempt to get downloaded files for the current iteration
        downloaded_files: list[FileInfo] = []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id or "",
                    run_id=download_lookup_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning("Timeout getting downloaded files", task_v2_id=task_v2.observer_cruise_id)
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            loop_internal_state,
        )

        task_v2_output = {
            "task_id": task_v2.observer_cruise_id,
            "status": task_v2.status,
            "summary": task_v2.summary,
            "extracted_information": result_dict,
            "failure_reason": failure_reason,
            "failure_category": task_v2.failure_category,
            "downloaded_files": [fi.model_dump() for fi in downloaded_files],
            "downloaded_file_urls": [fi.url for fi in downloaded_files],
            "task_screenshot_artifact_ids": [a.artifact_id for a in task_screenshot_artifacts],
            "workflow_screenshot_artifact_ids": [a.artifact_id for a in workflow_screenshot_artifacts],
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, task_v2_output)
        return await self.build_block_result(
            success=success or self.continue_on_failure,
            failure_reason=failure_reason,
            output_parameter_value=task_v2_output,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


def _is_secret_scalar(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _secret_path_suffix(path: str) -> str | None:
    # The last non-numeric segment names the placeholder (placeholder_XXXX_ssn) so the
    # LLM gets the same field-matching signal credential stubs carry (_username, _password).
    for segment in reversed(path.split(".")):
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", segment).strip("_")
        if cleaned and not cleaned.isdigit():
            return cleaned[:32]
    return None


def _register_and_replace_secret_response_path(
    response_body: Any,
    path: str,
    workflow_run_context: WorkflowRunContext,
) -> bool:
    suffix = _secret_path_suffix(path)
    current = response_body
    segments = path.split(".")
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        if isinstance(current, dict):
            if segment not in current:
                return False
            if is_last:
                value = current[segment]
                if not _is_secret_scalar(value):
                    return False
                current[segment] = workflow_run_context.register_secret_value(str(value), suffix=suffix)
                return True
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit():
                return False
            list_index = int(segment)
            if list_index >= len(current):
                return False
            if is_last:
                value = current[list_index]
                if not _is_secret_scalar(value):
                    return False
                current[list_index] = workflow_run_context.register_secret_value(str(value), suffix=suffix)
                return True
            current = current[list_index]
        else:
            return False
    return False


def _apply_secret_response_paths(
    response_body: Any,
    secret_response_paths: list[str],
    workflow_run_context: WorkflowRunContext,
) -> list[str]:
    invalid_paths: list[str] = []
    for path in dict.fromkeys(p.strip() for p in secret_response_paths if p.strip()):
        if not _register_and_replace_secret_response_path(response_body, path, workflow_run_context):
            invalid_paths.append(path)
    return invalid_paths


SECRET_RESPONSE_BODY_REDACTED = "<response body redacted: secret_response_paths did not fully resolve>"


class HttpRequestBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.HTTP_REQUEST] = BlockType.HTTP_REQUEST  # type: ignore

    # Individual HTTP parameters
    method: str = "GET"
    url: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None  # Changed to consistently be dict only
    files: dict[str, str] | None = None  # Dictionary mapping field names to file paths for multipart file uploads
    timeout: int = 30
    follow_redirects: bool = True
    download_filename: str | None = None
    save_response_as_file: bool = False
    secret_response_paths: list[str] | None = None

    # Parameters for templating
    parameters: list[PARAMETER_TYPE] = []

    # Allowed directories for local file access (class variable, not a Pydantic field)
    _allowed_dirs: ClassVar[list[str] | None] = None

    @classmethod
    def get_allowed_dirs(cls) -> list[str]:
        """Get the list of allowed directories for local file access.
        Computed once and cached for performance.
        """
        if cls._allowed_dirs is None:
            allowed_dirs: list[str] = []
            if settings.ARTIFACT_STORAGE_PATH:
                allowed_dirs.append(os.path.abspath(settings.ARTIFACT_STORAGE_PATH))
            if settings.VIDEO_PATH:
                allowed_dirs.append(os.path.abspath(settings.VIDEO_PATH))
            if settings.HAR_PATH:
                allowed_dirs.append(os.path.abspath(settings.HAR_PATH))
            if settings.LOG_PATH:
                allowed_dirs.append(os.path.abspath(settings.LOG_PATH))
            if settings.DOWNLOAD_PATH:
                allowed_dirs.append(os.path.abspath(settings.DOWNLOAD_PATH))
            cls._allowed_dirs = allowed_dirs
        return cls._allowed_dirs or []

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
        template_kwargs = {"force_include_secrets": True}

        def _render_string(value: str) -> str:
            rendered = self.format_block_parameter_template_from_workflow_run_context(
                value, workflow_run_context, **template_kwargs
            )
            # Boundary check so a longer id sharing a registered token's prefix is not partially replaced.
            for token in dict.fromkeys(workflow_run_context.find_embedded_placeholder_tokens(rendered)):
                secret_value = str(workflow_run_context.secrets[token])
                rendered = re.sub(
                    re.escape(token) + r"(?![A-Za-z0-9_])",
                    secret_value.replace("\\", "\\\\"),
                    rendered,
                )
            return rendered

        if self.url:
            self.url = _render_string(self.url)

        if self.body:
            self.body = cast(dict[str, Any], render_templates_in_json_value(self.body, _render_string))

        if self.files:
            self.files = cast(dict[str, str], render_templates_in_json_value(self.files, _render_string))

        if self.headers:
            self.headers = cast(dict[str, str], render_templates_in_json_value(self.headers, _render_string))

        if self.download_filename:
            self.download_filename = _render_string(self.download_filename)

    def validate_url(self, url: str) -> bool:
        """Validate if the URL is properly formatted"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    async def _execute_file_download(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> BlockResult:
        if not self.url:
            return await self.build_block_result(
                success=False,
                failure_reason="URL is required for file download",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            max_size_mb = settings.MAX_HTTP_DOWNLOAD_FILE_SIZE // (1024 * 1024)
            output_dir = get_download_dir(workflow_run_id)
            file_path = await download_file(
                self.url,
                max_size_mb=max_size_mb,
                headers=self.headers,
                output_dir=output_dir,
                filename=self.download_filename,
                organization_id=organization_id,
            )

            response_data = {
                "file_path": file_path,
                "file_name": os.path.basename(file_path),
                "file_size": os.path.getsize(file_path),
            }

            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response_data)

            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=response_data,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        except aiohttp.ClientResponseError as e:
            error_data = {"error": f"HTTP {e.status}", "error_type": "http_error"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"HTTP {e.status}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except DownloadFileMaxSizeExceeded as e:
            max_size_str = f"{e.max_size:.1f}"
            error_data = {"error": f"File exceeds maximum size of {max_size_str}MB", "error_type": "file_too_large"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"File exceeds maximum size of {max_size_str}MB",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
            error_data = {"error": masked_error, "error_type": "unknown"}
            LOG.warning(
                "File download failed",
                error=masked_error,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                workflow_run_id=workflow_run_id,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"File download failed: {masked_error}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

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

        if self.save_response_as_file and self.secret_response_paths:
            return await self.build_block_result(
                success=False,
                failure_reason="secret_response_paths cannot be combined with save_response_as_file",
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
                failure_reason=f"Invalid URL format: {workflow_run_context.mask_secrets_in_data(self.url)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Add default content-type as application/json if not provided (unless files are being uploaded)
        if not self.headers:
            self.headers = {}

        # If files are provided, don't set default Content-Type (aiohttp will set multipart/form-data)
        if not self.files:
            if not self.headers.get("Content-Type") and not self.headers.get("content-type"):
                LOG.info(
                    "Adding default content-type as application/json",
                    headers=workflow_run_context.mask_secrets_in_data(self.headers),
                )
                self.headers["Content-Type"] = "application/json"

        # Download files from HTTP URLs or S3 URIs if needed
        # Also allow local files from allowed directories (ARTIFACT_STORAGE_PATH, VIDEO_PATH, HAR_PATH, LOG_PATH)
        if self.files:
            downloaded_files: dict[str, str] = {}
            for field_name, file_path in self.files.items():
                masked_file_path = str(workflow_run_context.mask_secrets_in_data(file_path))
                # Parse file path (handle file:// URI format)
                actual_file_path: str | None = None
                is_file_uri = file_path.startswith("file://")

                if is_file_uri:
                    try:
                        actual_file_path = parse_uri_to_path(file_path)
                    except ValueError as e:
                        masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
                        return await self.build_block_result(
                            success=False,
                            failure_reason=(f"Invalid file URI format: {masked_file_path}. Error: {masked_error}"),
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                else:
                    actual_file_path = file_path

                # Check if file_path is a URL or managed storage URI
                is_url = (
                    file_path.startswith("http://") or file_path.startswith("https://") or file_path.startswith("www.")
                )
                is_managed_storage_uri = (
                    file_path.startswith("s3://") or file_path.startswith("gs://") or file_path.startswith("azure://")
                )

                # Check if file is in allowed directories
                is_allowed_local_file = False
                if actual_file_path:
                    # Convert to absolute path for comparison (handles both absolute and relative paths)
                    abs_file_path = os.path.abspath(actual_file_path)

                    # Get allowed directory paths (using class method for cached result)
                    allowed_dirs = self.get_allowed_dirs()
                    LOG.debug("HttpRequestBlock Allowed directories", allowed_dirs=allowed_dirs)

                    # Check if file is within any allowed directory
                    for allowed_dir in allowed_dirs:
                        # Use os.path.commonpath to check if file is within allowed directory
                        try:
                            common_path = os.path.commonpath([abs_file_path, allowed_dir])
                            if common_path == allowed_dir:
                                is_allowed_local_file = True
                                break
                        except ValueError:
                            # Paths are on different drives (Windows) or incompatible
                            continue

                # If not URL, managed storage URI, or allowed local file, reject
                if not (is_url or is_managed_storage_uri or is_allowed_local_file):
                    return await self.build_block_result(
                        success=False,
                        failure_reason=(
                            "No permission to access local file: "
                            f"{masked_file_path}. Only HTTP/HTTPS URLs, "
                            "managed storage URIs, or files in allowed directories are allowed."
                        ),
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                # Handle different file sources
                if is_allowed_local_file:
                    # Use local file directly
                    local_file_path_str: str = cast(str, actual_file_path)
                    masked_local_file_path = str(workflow_run_context.mask_secrets_in_data(local_file_path_str))
                    if not os.path.exists(local_file_path_str):
                        return await self.build_block_result(
                            success=False,
                            failure_reason=f"File not found: {masked_local_file_path}",
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )
                    downloaded_files[field_name] = local_file_path_str
                    LOG.info(
                        "HttpRequestBlock Using allowed local file",
                        field_name=field_name,
                        file_path=masked_local_file_path,
                    )
                else:
                    # Download from remote source
                    try:
                        LOG.info(
                            "HttpRequestBlock Downloading file from remote source",
                            field_name=field_name,
                            file_path=masked_file_path,
                            is_url=is_url,
                            is_managed_storage_uri=is_managed_storage_uri,
                        )
                        local_file_path = await download_file(file_path, organization_id=organization_id)
                        downloaded_files[field_name] = local_file_path
                        LOG.info(
                            "HttpRequestBlock File downloaded successfully",
                            field_name=field_name,
                            original_path=masked_file_path,
                            local_path=local_file_path,
                        )
                    except Exception as e:
                        masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
                        return await self.build_block_result(
                            success=False,
                            failure_reason=(f"Failed to download file {masked_file_path}: {masked_error}"),
                            output_parameter_value=None,
                            status=BlockStatus.failed,
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                        )

            # Update self.files with local file paths
            self.files = downloaded_files

        if self.save_response_as_file:
            return await self._execute_file_download(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            LOG.info(
                "Executing HTTP request",
                method=self.method,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                headers=workflow_run_context.mask_secrets_in_data(self.headers),
                workflow_run_id=workflow_run_id,
                body=workflow_run_context.mask_secrets_in_data(self.body),
                files=workflow_run_context.mask_secrets_in_data(self.files),
            )

            status_code, response_headers, response_body = await aiohttp_request(
                method=self.method,
                url=self.url,
                headers=self.headers,
                data=self.body,
                files=self.files,
                timeout=self.timeout,
                follow_redirects=self.follow_redirects,
            )

            success = 200 <= status_code < 300
            failure_reason = None
            invalid_secret_response_paths: list[str] = []
            if self.secret_response_paths:
                # Extract on every status so a secret echoed in an error body never reaches outputs or logs.
                invalid_secret_response_paths = _apply_secret_response_paths(
                    response_body,
                    self.secret_response_paths,
                    workflow_run_context,
                )
                if success and invalid_secret_response_paths:
                    success = False
                    failure_reason = (
                        "secret_response_paths did not resolve to a non-empty string: "
                        f"{', '.join(invalid_secret_response_paths)}"
                    )

            response_data = {
                "status_code": status_code,
                "response_headers": response_headers,
                "response_body": response_body,
                "request_method": self.method,
                "request_url": self.url,
                "request_headers": self.headers,
                "request_body": self.body,
                "headers": response_headers,
                "body": response_body,
                "url": self.url,
            }

            if invalid_secret_response_paths:
                response_data["response_body"] = SECRET_RESPONSE_BODY_REDACTED
                response_data["body"] = SECRET_RESPONSE_BODY_REDACTED

            response_data = workflow_run_context.mask_secrets_in_data(response_data)

            LOG.info(
                "HTTP request completed",
                status_code=status_code,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                method=self.method,
                workflow_run_id=workflow_run_id,
                response_data=response_data,
            )

            if failure_reason is None and not success:
                failure_reason = f"HTTP {status_code}: {response_data.get('response_body', '')}"

            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, response_data)

            return await self.build_block_result(
                success=success,
                failure_reason=failure_reason,
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
            masked_error = str(workflow_run_context.mask_secrets_in_data(str(e)))
            error_data = {"error": masked_error, "error_type": "unknown"}
            LOG.warning(
                "HTTP request failed with unexpected error",
                error=masked_error,
                url=workflow_run_context.mask_secrets_in_data(self.url),
                method=self.method,
                workflow_run_id=workflow_run_id,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"HTTP request failed: {masked_error}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )


class PrintPageBlock(Block):
    block_type: Literal[BlockType.PRINT_PAGE] = BlockType.PRINT_PAGE  # type: ignore

    include_timestamp: bool = True
    custom_filename: str | None = None
    format: str = "A4"
    landscape: bool = False
    print_background: bool = True
    parameters: list[PARAMETER_TYPE] = []

    VALID_FORMATS: ClassVar[set[str]] = {"A4", "Letter", "Legal", "Tabloid"}

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        return sanitize_filename(filename)

    def _build_pdf_options(self) -> dict[str, Any]:
        pdf_format = self.format if self.format in self.VALID_FORMATS else "A4"
        pdf_options: dict[str, Any] = {
            "format": pdf_format,
            "landscape": self.landscape,
            "print_background": self.print_background,
        }

        if self.include_timestamp:
            pdf_options["display_header_footer"] = True
            pdf_options["header_template"] = (
                '<div style="font-size:10px;width:100%;display:flex;justify-content:space-between;padding:0 10px;">'
                '<span class="date"></span><span class="title"></span><span></span></div>'
            )
            pdf_options["footer_template"] = (
                '<div style="font-size:10px;width:100%;display:flex;justify-content:space-between;padding:0 10px;">'
                '<span class="url"></span><span></span><span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>'
            )
            pdf_options["margin"] = {"top": "40px", "bottom": "40px"}

        return pdf_options

    async def _upload_pdf_artifact(
        self,
        *,
        pdf_bytes: bytes,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None,
    ) -> tuple[str | None, str | None]:
        artifact_org_id = organization_id or workflow_run_context.organization_id
        if not artifact_org_id:
            LOG.warning(
                "PrintPageBlock Missing organization_id, skipping artifact upload",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return None, None

        try:
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id,
                organization_id=artifact_org_id,
            )
        except NotFoundError:
            LOG.warning(
                "PrintPageBlock Workflow run block not found, skipping artifact upload",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=artifact_org_id,
            )
            return None, None

        artifact_id, artifact_uri = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact_with_uri(
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.PDF,
            data=pdf_bytes,
        )
        try:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])
        except Exception:
            LOG.warning(
                "PrintPageBlock Failed to upload PDF artifact",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block.workflow_run_block_id,
                exc_info=True,
            )
            return None, None

        # Generate a downloadable URL for the artifact
        artifact_url = None
        try:
            artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id=artifact_org_id)
            if artifact:
                artifact_url = await app.ARTIFACT_MANAGER.get_share_link(artifact)
        except Exception:
            LOG.warning(
                "PrintPageBlock Failed to generate artifact download URL",
                artifact_id=artifact_id,
                exc_info=True,
            )

        return artifact_uri, artifact_url

    async def _register_pdf_as_downloaded_file(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        download_run_id: str | None = None,
    ) -> list[FileInfo]:
        # Workflow finalization eventually runs save_downloaded_files, but the block
        # output snapshot is recorded now and the UI keys off downloaded_file_urls
        # on the block — so we register up front and let finalization re-run safely.
        if not organization_id:
            return []
        storage_run_id = download_run_id or workflow_run_id
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except Exception:
            LOG.warning(
                "PrintPageBlock failed to register PDF as downloaded file; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout getting downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(
                block_context,
                organization_id or "",
                workflow_run_id,
                self.label,
            )

        resolved_download_id = resolve_run_download_id(block_context, fallback_run_id=workflow_run_id)
        browser_state = await self.get_or_create_browser_state(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            download_run_id_override=resolved_download_id,
        )
        if not browser_state:
            return await self.build_block_result(
                success=False,
                failure_reason="No browser state available",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        page = await browser_state.get_working_page()
        if not page:
            return await self.build_block_result(
                success=False,
                failure_reason="No page available",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        pdf_options = self._build_pdf_options()

        try:
            pdf_bytes = await page.pdf(**pdf_options)
        except Exception as e:
            error_msg = str(e)
            if "pdf" in error_msg.lower() and ("not supported" in error_msg.lower() or "chromium" in error_msg.lower()):
                error_msg = "PDF generation requires Chromium browser. Current browser does not support page.pdf()."
            LOG.warning("PrintPageBlock Failed to generate PDF", error=error_msg, workflow_run_id=workflow_run_id)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to generate PDF: {error_msg}",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if self.custom_filename:
            filename = self.format_block_parameter_template_from_workflow_run_context(
                self.custom_filename, workflow_run_context
            )
            filename = self._sanitize_filename(filename)
            if not filename.endswith(".pdf"):
                filename += ".pdf"
        else:
            filename = f"page_{timestamp_str}.pdf"

        # Save PDF to download directory so it appears in runs UI
        download_dir = get_download_dir(resolved_download_id)
        file_path = os.path.join(download_dir, filename)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(pdf_bytes)

        # Upload to artifact storage for downstream block access (e.g., File Extraction Block)
        artifact_uri, artifact_url = await self._upload_pdf_artifact(
            pdf_bytes=pdf_bytes,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            workflow_run_context=workflow_run_context,
            organization_id=organization_id,
        )

        artifact_org_id = organization_id or workflow_run_context.organization_id
        downloaded_files = await self._register_pdf_as_downloaded_file(
            organization_id=artifact_org_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            download_run_id=resolved_download_id,
        )

        current_context = skyvern_context.current()
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            current_context.loop_internal_state if current_context else None,
        )
        output = {
            "filename": filename,
            "file_path": file_path,
            "size_bytes": len(pdf_bytes),
            "artifact_uri": artifact_uri,
            "artifact_url": artifact_url,
            "downloaded_files": [fi.model_dump() for fi in downloaded_files],
            "downloaded_file_urls": [fi.url for fi in downloaded_files],
            "downloaded_file_artifact_ids": [fi.artifact_id for fi in downloaded_files if fi.artifact_id],
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)

        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WorkflowTriggerBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.WORKFLOW_TRIGGER] = BlockType.WORKFLOW_TRIGGER  # type: ignore

    # The permanent ID of the target workflow to trigger
    workflow_permanent_id: str
    # Parameters/payload to pass to the triggered workflow
    payload: dict[str, Any] | None = None
    # Whether to wait for the triggered workflow to complete
    wait_for_completion: bool = True
    # Optional browser session ID for the triggered workflow
    browser_session_id: str | None = None
    # When True, the child workflow inherits the parent's browser session
    use_parent_browser_session: bool = False
    # Parameters for Jinja2 template interpolation
    parameters: list[PARAMETER_TYPE] = []

    MAX_TRIGGER_DEPTH: ClassVar[int] = 10

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    async def _check_trigger_depth(self, workflow_run_id: str) -> int:
        """Check the nesting depth of workflow triggers to prevent infinite recursion.

        Note: This depth guard walks the parent_workflow_run_id chain, which is only
        populated for synchronous triggers. For async (fire-and-forget) dispatch, the
        parent may have already completed before the child runs, so circular async
        chains (A->B->A) are only blocked while A is still running. A full
        visited-workflow guard would require persistent state and is left as a future
        enhancement.
        """
        depth = 0
        current_run_id: str | None = workflow_run_id
        while current_run_id:
            if depth >= self.MAX_TRIGGER_DEPTH:
                raise InvalidWorkflowDefinition(
                    f"Workflow trigger depth exceeds maximum of {self.MAX_TRIGGER_DEPTH}. "
                    "This may indicate a circular workflow trigger chain."
                )
            run = await app.DATABASE.workflow_runs.get_workflow_run(current_run_id)
            if not run or not run.parent_workflow_run_id:
                break
            current_run_id = run.parent_workflow_run_id
            depth += 1
        return depth

    def _render_template_value(
        self,
        value: str,
        workflow_run_context: WorkflowRunContext,
    ) -> Any:
        """Render a single Jinja2 template string, handling the | json filter marker."""
        rendered = self.format_block_parameter_template_from_workflow_run_context(
            value, workflow_run_context, env=jinja_json_finalize_strict_env
        )
        if rendered.startswith(_JSON_TYPE_MARKER) and rendered.endswith(_JSON_TYPE_MARKER):
            json_str = rendered[len(_JSON_TYPE_MARKER) : -len(_JSON_TYPE_MARKER)]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                raise FailedToFormatJinjaStyleParameter(value, f"Raw JSON filter produced invalid JSON: {json_str}")
        elif _JSON_TYPE_MARKER in rendered:
            raise FailedToFormatJinjaStyleParameter(
                value,
                "The '| json' filter can only be used for complete value replacement. "
                "It cannot be combined with other text (e.g., 'prefix-{{ val | json }}'). "
                "Remove the surrounding text or remove the '| json' filter.",
            )
        return rendered

    def _render_scalar_with_path(
        self,
        value: str,
        workflow_run_context: WorkflowRunContext,
        path: str,
    ) -> Any:
        # Wrap render errors with JSON-pointer-style payload path + template so
        # the failure reason surfaces where to look in the workflow.
        try:
            return self._render_template_value(value, workflow_run_context)
        except PayloadTemplateRenderError:
            raise
        except Exception as exc:
            raise PayloadTemplateRenderError(path=path, template=value, original=exc) from exc

    def _render_templates_in_payload(
        self,
        payload: dict[str, Any],
        workflow_run_context: WorkflowRunContext,
        _path: str = "payload",
    ) -> dict[str, Any]:
        """Recursively render Jinja2 templates in payload values."""
        resolved: dict[str, Any] = {}
        for key, value in payload.items():
            current_path = f"{_path}{_format_payload_path_segment(key)}"
            if isinstance(value, str):
                resolved[key] = self._render_scalar_with_path(value, workflow_run_context, current_path)
            elif isinstance(value, dict):
                resolved[key] = self._render_templates_in_payload(value, workflow_run_context, current_path)
            elif isinstance(value, list):
                resolved[key] = self._render_templates_in_list(value, workflow_run_context, current_path)
            else:
                resolved[key] = value
        return resolved

    def _render_templates_in_list(
        self,
        items: list[Any],
        workflow_run_context: WorkflowRunContext,
        _path: str = "payload",
    ) -> list[Any]:
        """Recursively render Jinja2 templates in list items (strings, nested dicts, and nested lists)."""
        result: list[Any] = []
        for idx, item in enumerate(items):
            current_path = f"{_path}[{idx}]"
            if isinstance(item, str):
                result.append(self._render_scalar_with_path(item, workflow_run_context, current_path))
            elif isinstance(item, dict):
                result.append(self._render_templates_in_payload(item, workflow_run_context, current_path))
            elif isinstance(item, list):
                result.append(self._render_templates_in_list(item, workflow_run_context, current_path))
            else:
                result.append(item)
        return result

    def validate_payload_templates(self) -> None:
        """Parse-check every Jinja2 template in self.payload at workflow save time.

        Walks the payload mirroring _render_templates_in_payload so paths match the
        runtime PayloadTemplateRenderError format. On TemplateSyntaxError raises
        PayloadTemplateSyntaxError with block label, JSON-pointer key path, and
        the offending template string.
        """
        if not self.payload:
            return

        def _walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, sub in value.items():
                    _walk(sub, f"{path}{_format_payload_path_segment(key)}")
            elif isinstance(value, list):
                for idx, sub in enumerate(value):
                    _walk(sub, f"{path}[{idx}]")
            elif isinstance(value, str):
                try:
                    jinja_sandbox_env.parse(value)
                except TemplateSyntaxError as exc:
                    raise PayloadTemplateSyntaxError(
                        block_label=self.label, path=path, template=value, original=exc
                    ) from exc

        _walk(self.payload, "payload")

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.workflow_permanent_id = self.format_block_parameter_template_from_workflow_run_context(
            self.workflow_permanent_id, workflow_run_context
        )
        if self.payload:
            self.payload = self._render_templates_in_payload(self.payload, workflow_run_context)
        if self.browser_session_id:
            self.browser_session_id = self.format_block_parameter_template_from_workflow_run_context(
                self.browser_session_id, workflow_run_context
            )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRunStatus  # noqa: PLC0415

        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        # Helper to record output and build a failed block result in one step.
        # This ensures downstream blocks referencing block_X_output see the
        # failure reason instead of "parameter not found".
        async def _fail(failure_reason: str) -> BlockResult:
            error_output = {"failure_reason": failure_reason}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_output)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_output,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # 1. Resolve Jinja2 templates
        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await _fail(f"Failed to resolve templates: {str(e)}")

        resolved_workflow_permanent_id = self.workflow_permanent_id
        resolved_payload = self.payload

        # 2. Check recursion depth
        try:
            await self._check_trigger_depth(workflow_run_id)
        except InvalidWorkflowDefinition as e:
            return await _fail(str(e))

        # 3. Get the organization
        if not organization_id:
            return await _fail("organization_id is required for WorkflowTriggerBlock")
        organization = await app.DATABASE.organizations.get_organization(organization_id)
        if not organization:
            return await _fail(f"Organization {organization_id} not found")

        # 4. Resolve browser session
        # Browser session priority:
        # 1. Explicit browser_session_id configured on the block
        # 2. use_parent_browser_session → inherit parent's session (persistent
        #    or in-memory via self.pages[parent_workflow_run_id] lookup)
        # 3. Neither → for sync (wait_for_completion), create a fresh persistent
        #    session; for async (fire-and-forget), let the child's Temporal worker
        #    handle its own browser.
        created_fresh_session = False
        if self.browser_session_id:
            resolved_browser_session_id = self.browser_session_id
        elif self.use_parent_browser_session and browser_session_id:
            resolved_browser_session_id = browser_session_id
        elif self.use_parent_browser_session:
            # Parent uses an in-memory browser (no persistent session).
            # Pass None so the child inherits via the parent_workflow_run_id
            # lookup in get_or_create_for_workflow_run.
            resolved_browser_session_id = None
        elif self.wait_for_completion:
            # Sync mode: child runs inline in the same process, so it needs
            # its own persistent session to avoid sharing the parent's browser.
            parent_workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
            proxy_location = parent_workflow_run.proxy_location if parent_workflow_run else None
            try:
                child_browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                    organization_id=organization_id,
                    proxy_location=proxy_location,
                    timeout_minutes=30,
                )
                resolved_browser_session_id = child_browser_session.persistent_browser_session_id
                created_fresh_session = True
                LOG.info(
                    "Created fresh browser session for triggered workflow",
                    parent_workflow_run_id=workflow_run_id,
                    child_browser_session_id=resolved_browser_session_id,
                )
            except Exception as e:
                return await _fail(f"Failed to create browser session for triggered workflow: {str(e)}")
        else:
            # Async (fire-and-forget): the child runs in its own Temporal worker
            # and will create its own browser. No pre-creation needed.
            resolved_browser_session_id = None

        # 5. Execute based on wait mode
        output_data: dict[str, Any] = {}
        success = False
        if self.wait_for_completion:
            # Synchronous: setup + execute inline in the same process.
            workflow_request = WorkflowRequestBody(
                data=resolved_payload,
                browser_session_id=resolved_browser_session_id,
            )

            # Isolate the synchronous child workflow in a placeholder scope so
            # setup_workflow_run() can replace the current context without
            # flushing the parent's pending workflow_feature_flags summary.
            parent_context = skyvern_context.current()
            inherited_trigger_type = parent_context.trigger_type if parent_context else None
            with skyvern_context.scoped(
                skyvern_context.SkyvernContext(
                    run_id=parent_context.run_id if parent_context else None,
                    root_workflow_run_id=parent_context.root_workflow_run_id if parent_context else None,
                    copilot_session_id=parent_context.copilot_session_id if parent_context else None,
                    trigger_type=inherited_trigger_type,
                )
            ):
                try:
                    triggered_workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
                        request_id=None,
                        workflow_request=workflow_request,
                        workflow_permanent_id=resolved_workflow_permanent_id,
                        organization=organization,
                        parent_workflow_run_id=workflow_run_id,
                        ignore_inherited_workflow_system_prompt=self.ignore_workflow_system_prompt,
                        trigger_type=inherited_trigger_type,
                    )
                except Exception as e:
                    error_msg = get_user_facing_exception_message(e)
                    return await _fail(f"Failed to setup triggered workflow run: {error_msg}")

                triggered_run_id = triggered_workflow_run.workflow_run_id

                LOG.info(
                    "Triggered workflow run (sync)",
                    parent_workflow_run_id=workflow_run_id,
                    triggered_workflow_run_id=triggered_run_id,
                    triggered_workflow_permanent_id=resolved_workflow_permanent_id,
                )

                try:
                    # The opt-out flag is persisted on the child's workflow_run row at
                    # spawn time (setup_workflow_run above), so execute_workflow reads
                    # it from the DB. This works identically for sync and async triggers.
                    final_run = await app.WORKFLOW_SERVICE.execute_workflow(
                        workflow_run_id=triggered_run_id,
                        api_key=None,
                        organization=organization,
                        browser_session_id=resolved_browser_session_id,
                    )
                    success = final_run.status == WorkflowRunStatus.completed
                    output_data = {
                        "workflow_run_id": triggered_run_id,
                        "workflow_permanent_id": resolved_workflow_permanent_id,
                        "status": str(final_run.status),
                        "failure_reason": final_run.failure_reason,
                    }
                    # Include the child workflow's output parameters so downstream
                    # blocks can reference them (e.g. block_3_output.outputs.block_2_output)
                    try:
                        child_output_params = (
                            await app.WORKFLOW_SERVICE.get_output_parameter_workflow_run_output_parameter_tuples(
                                workflow_id=final_run.workflow_id,
                                workflow_run_id=triggered_run_id,
                            )
                        )
                        child_outputs: dict[str, Any] = {}
                        for output_param, run_output_param in child_output_params:
                            child_outputs[output_param.key] = run_output_param.value
                        output_data["outputs"] = child_outputs
                    except Exception:
                        LOG.warning(
                            "Failed to fetch child workflow outputs",
                            triggered_workflow_run_id=triggered_run_id,
                            exc_info=True,
                        )
                except Exception as e:
                    error_msg = get_user_facing_exception_message(e)
                    output_data = {
                        "workflow_run_id": triggered_run_id,
                        "workflow_permanent_id": resolved_workflow_permanent_id,
                        "status": "failed",
                        "failure_reason": f"Triggered workflow execution failed: {error_msg}",
                    }
                    success = False
                if created_fresh_session and resolved_browser_session_id:
                    try:
                        await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                            organization_id, resolved_browser_session_id
                        )
                    except Exception:
                        LOG.warning(
                            "Failed to close child browser session",
                            child_browser_session_id=resolved_browser_session_id,
                            triggered_workflow_run_id=triggered_run_id,
                            exc_info=True,
                        )
        else:
            # Fire and forget: dispatch the child workflow via Temporal so it
            # gets its own independent worker process. This ensures the child
            # survives even if the parent workflow finishes first.
            # NOTE: This path requires Temporal (cloud). On self-hosted
            # (BackgroundTaskExecutor), the workflow run record is created but
            # execution is silently skipped because background_tasks=None.
            from skyvern.services.workflow_service import run_workflow  # noqa: PLC0415

            workflow_request = WorkflowRequestBody(
                data=resolved_payload,
                browser_session_id=resolved_browser_session_id,
            )
            try:
                # ``run_workflow`` persists this flag to the child's
                # workflow_run row via its internal setup_workflow_run call,
                # then dispatches to Temporal without passing the flag
                # separately; the worker reads it back from the DB inside
                # ``execute_workflow``. Symmetric with the sync branch above
                # — the flag is written once, at spawn time, for both paths.
                async_parent_context = skyvern_context.current()
                async_inherited_trigger_type = async_parent_context.trigger_type if async_parent_context else None
                triggered_workflow_run = await run_workflow(
                    workflow_id=resolved_workflow_permanent_id,
                    organization=organization,
                    workflow_request=workflow_request,
                    request=None,
                    background_tasks=None,
                    parent_workflow_run_id=workflow_run_id,
                    ignore_inherited_workflow_system_prompt=self.ignore_workflow_system_prompt,
                    trigger_type=async_inherited_trigger_type,
                )
            except Exception as e:
                error_msg = get_user_facing_exception_message(e)
                return await _fail(f"Failed to dispatch triggered workflow: {error_msg}")

            triggered_run_id = triggered_workflow_run.workflow_run_id

            LOG.info(
                "Async workflow dispatch succeeded (via Temporal)",
                parent_workflow_run_id=workflow_run_id,
                triggered_workflow_run_id=triggered_run_id,
                triggered_workflow_permanent_id=resolved_workflow_permanent_id,
            )
            output_data = {
                "workflow_run_id": triggered_run_id,
                "workflow_permanent_id": resolved_workflow_permanent_id,
                "status": "queued",
            }
            success = True

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)

        return await self.build_block_result(
            success=success,
            failure_reason=output_data.get("failure_reason") if not success else None,
            output_parameter_value=output_data,
            status=BlockStatus.completed if success else BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
