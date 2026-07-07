from __future__ import annotations

import ast
import asyncio
import json
import re
import uuid
from collections import deque
from datetime import datetime
from typing import Annotated, Any, Literal, Union
from urllib.parse import quote

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, Field, model_validator

from skyvern.config import settings
from skyvern.constants import (
    GET_DOWNLOADED_FILES_TIMEOUT,
)
from skyvern.exceptions import (
    ContextParameterValueNotFound,
    MissingBrowserState,
    MissingBrowserStatePage,
    MissingStarterUrl,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api import email
from skyvern.forge.sdk.api.files import (
    resolve_run_download_id,
)
from skyvern.forge.sdk.api.llm.schema_validator import validate_schema
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.workflow.constants import OUTPUT_PARAMETER_MAX_VALUE_BYTES
from skyvern.forge.sdk.workflow.context_manager import BlockMetadata, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    FailedToFormatJinjaStyleParameter,
    InvalidWorkflowDefinition,
    MissingJinjaVariables,
    NoIterableValueFound,
)
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    filter_downloaded_files_for_current_iteration,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models.block_base import (  # noqa: F401  (re-exported for tests/back-compat)
    CURRENT_DATE_FORMAT,
    MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
    Block,
    capture_block_download_baseline,
    jinja_sandbox_env,
    warn_if_file_download_max_steps_low,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    ContextParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
)
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import (  # noqa: F401  # FileType re-exported for callers importing it from this module
    AIFallbackMode,
    BlockResult,
    BlockStatus,
    BlockType,
    FileType,
)
from skyvern.services.error_detection_service import detect_user_defined_errors_for_task
from skyvern.utils.strings import generate_random_string
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


# Mapping from TaskV2Status to the corresponding BlockStatus. Declared once at
# import time so it is not recreated on each block execution.
TASKV2_TO_BLOCK_STATUS: dict[TaskV2Status, BlockStatus] = {
    TaskV2Status.completed: BlockStatus.completed,
    TaskV2Status.terminated: BlockStatus.terminated,
    TaskV2Status.failed: BlockStatus.failed,
    TaskV2Status.canceled: BlockStatus.canceled,
    TaskV2Status.timed_out: BlockStatus.timed_out,
}

TASK_TO_BLOCK_STATUS: dict[TaskStatus, BlockStatus] = {
    TaskStatus.completed: BlockStatus.completed,
    TaskStatus.terminated: BlockStatus.terminated,
    TaskStatus.failed: BlockStatus.failed,
    TaskStatus.canceled: BlockStatus.canceled,
    TaskStatus.timed_out: BlockStatus.timed_out,
}


def extract_file_url_from_block_output(value: Any) -> str | None:
    """Extract a file URL from block output values that wrap downloaded files."""
    if isinstance(value, dict):
        downloaded_files = value.get("downloaded_files")
        if isinstance(downloaded_files, list) and downloaded_files:
            first_file = downloaded_files[0]
            if isinstance(first_file, dict):
                return first_file.get("url") or first_file.get("file_path") or None

        for key in ("artifact_url", "file_url", "file_path"):
            extracted = value.get(key)
            if isinstance(extracted, str) and extracted:
                return extracted
        return None

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return extract_file_url_from_block_output(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, dict):
                return extract_file_url_from_block_output(parsed)
        except (ValueError, SyntaxError):
            pass
    return None


def sanitize_filename(filename: str, default: str = "document") -> str:
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename).strip(". ")
    return sanitized[:200] if sanitized else default


def _format_payload_path_segment(key: str) -> str:
    """Plain identifiers render as `.key`; anything else (dots, brackets, spaces,
    quotes) renders as a bracketed JSON-escaped string so paths stay unambiguous
    against keys that contain `.` or `[`."""
    if key.isidentifier():
        return f".{key}"
    return f"[{json.dumps(key)}]"


# ForLoop constants
DEFAULT_MAX_LOOP_ITERATIONS = 500
MAX_LOOP_OVER_VALUE_LOG_CHARS = 2000
# Persist accumulated loop output to DB every N iterations to survive timeouts.
# Trades up to N-1 iterations of data loss for O(N/K) writes instead of O(N).
PERSIST_LOOP_OUTPUT_INTERVAL = 10
DEFAULT_MAX_STEPS_PER_ITERATION = 50


def _maybe_truncate_loop_outputs(
    outputs_with_loop_values: list[list[dict[str, Any]]],
    *,
    workflow_run_id: str,
    output_parameter_id: str | None,
) -> None:
    """Fail-open in-memory cap for loop accumulators; preserves per-entry schema (SKY-9779)."""
    try:
        size_bytes = len(json.dumps(outputs_with_loop_values, default=str).encode("utf-8"))
    except Exception:
        LOG.warning(
            "Failed to measure loop output size; skipping truncation",
            workflow_run_id=workflow_run_id,
            output_parameter_id=output_parameter_id,
            exc_info=True,
        )
        return

    if size_bytes <= OUTPUT_PARAMETER_MAX_VALUE_BYTES:
        return

    summarized_through = len(outputs_with_loop_values) - 1
    summary_entry = [
        {
            "loop_value": None,
            "output_parameter": None,
            "output_value": {
                "truncated": True,
                "reason": "loop_output_size_exceeded",
                "iterations_summarized_through": summarized_through,
            },
        }
    ]
    LOG.warning(
        "Truncating loop output accumulator",
        workflow_run_id=workflow_run_id,
        output_parameter_id=output_parameter_id,
        size_bytes=size_bytes,
        limit_bytes=OUTPUT_PARAMETER_MAX_VALUE_BYTES,
        iterations_summarized_through=summarized_through,
    )
    last = outputs_with_loop_values[-1]
    outputs_with_loop_values.clear()
    outputs_with_loop_values.append(summary_entry)
    outputs_with_loop_values.append(last)


def _should_skip_retry_on_anti_bot_detection(task: Task) -> bool:
    categories = task.failure_category
    if categories:
        return any(c.get("category") == "ANTI_BOT_DETECTION" for c in categories)

    if task.failure_reason:
        result = classify_from_failure_reason(task.failure_reason)
        if result and any(c.get("category") == "ANTI_BOT_DETECTION" for c in result):
            return True

    return False


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
    include_extracted_text: bool = True

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

        # Inherit workflow-level error_code_mapping; block-level entries override on key conflicts.
        workflow = getattr(workflow_run_context, "workflow", None)
        workflow_error_code_mapping: dict[str, str] | None = None
        if workflow is not None and workflow.workflow_definition is not None:
            workflow_error_code_mapping = workflow.workflow_definition.error_code_mapping

        if workflow_error_code_mapping or self.error_code_mapping:
            merged_mapping = dict(workflow_error_code_mapping or {})
            merged_mapping.update(self.error_code_mapping or {})
            self.error_code_mapping = {
                self.format_block_parameter_template_from_workflow_run_context(error_code, workflow_run_context): (
                    self.format_block_parameter_template_from_workflow_run_context(
                        error_description, workflow_run_context
                    )
                )
                for error_code, error_description in merged_mapping.items()
            }

        # Materialize the workflow-level workflow_system_prompt onto this block so
        # ForgeAgent.create_task can hand it off to the Task row verbatim.
        self._apply_workflow_system_prompt(workflow_run_context)

    @staticmethod
    async def get_task_order(workflow_run_id: str, current_retry: int) -> tuple[int, int]:
        """
        Returns the order and retry for the next task in the workflow run as a tuple.
        """
        last_task_for_workflow_run = await app.DATABASE.tasks.get_last_task_for_workflow_run(
            workflow_run_id=workflow_run_id
        )
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

    async def _handle_task_failure_with_error_detection(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState | None,
        failure_reason: str,
        organization_id: str,
    ) -> None:
        """
        Handle task failure by updating the task status and detecting user-defined errors.

        This helper method consolidates the error detection logic that was previously
        duplicated across multiple exception handlers in the execute method.
        """
        await app.DATABASE.tasks.update_task(
            task.task_id,
            status=TaskStatus.failed,
            organization_id=organization_id,
            failure_reason=failure_reason,
        )
        # Detect user-defined errors if error_code_mapping is provided
        if self.error_code_mapping:
            try:
                detected_errors = await detect_user_defined_errors_for_task(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    failure_reason=failure_reason,
                )
                if detected_errors:
                    # Only pass new errors — update_task() appends to existing errors
                    new_errors = [error.model_dump() for error in detected_errors]
                    await app.DATABASE.tasks.update_task(
                        task_id=task.task_id,
                        organization_id=organization_id,
                        errors=new_errors,
                    )
            except Exception:
                LOG.exception(
                    "Failed to detect or store user-defined errors during task failure",
                    task_id=task.task_id,
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
        current_retry = 0
        # initial value for will_retry is True, so that the loop runs at least once
        will_retry = True
        current_running_task: Task | None = None
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        # Scope downloaded files to this block only.
        block_context = skyvern_context.current()
        if block_context:
            await capture_block_download_baseline(block_context, organization_id or "", workflow_run_id, self.label)

        # Get workflow from context if available, otherwise query database
        workflow = workflow_run_context.workflow
        if workflow is None:
            workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_run.workflow_permanent_id,
            )
            # Cache the workflow back to context for future block executions
            workflow_run_context.set_workflow(workflow)
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

        if self.totp_identifier:
            if workflow_run_context.has_parameter(self.totp_identifier) and workflow_run_context.has_value(
                self.totp_identifier
            ):
                totp_identifier_parameter_value = workflow_run_context.get_value(self.totp_identifier)
                if totp_identifier_parameter_value:
                    self.totp_identifier = totp_identifier_parameter_value
        else:
            for parameter in self.get_all_parameters(workflow_run_id):
                parameter_key = getattr(parameter, "key", None)
                if not parameter_key:
                    continue
                credential_totp_identifier = workflow_run_context.get_credential_totp_identifier(parameter_key)
                if credential_totp_identifier:
                    self.totp_identifier = credential_totp_identifier
                    break

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

        # SKY-8818: observability + wait_until override. Computed ONCE per block
        # execution — hoisted outside the retry loop so the Datadog signal counts
        # block runs, not retries, and `_navigate_wait_until` is a pure function of
        # self.block_type (which does not change between retries).
        warn_if_file_download_max_steps_low(self, workflow_run_id=workflow_run_id)
        _is_file_download = self.block_type == BlockType.FILE_DOWNLOAD
        _navigate_wait_until: Literal["load", "domcontentloaded", "commit"] = (
            "domcontentloaded" if _is_file_download else "load"
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
            workflow_run_block = await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                task_id=task.task_id,
                organization_id=organization_id,
            )
            current_running_task = task
            organization = await app.DATABASE.organizations.get_organization(
                organization_id=workflow_run.organization_id
            )
            if not organization:
                raise Exception(f"Organization is missing organization_id={workflow_run.organization_id}")

            browser_state: BrowserState | None = None
            if is_first_task:
                # the first task block will create the browser state and do the navigation
                try:
                    # SKY-8818: for file_download blocks, skip the browser factory's built-in
                    # goto (which uses wait_until='load' and stalls on slow subresources) and
                    # let the about:blank fallback below handle navigation with our override.
                    _bm_url = None if _is_file_download else self.url
                    browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                        workflow_run=workflow_run,
                        url=_bm_url,
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
                    # SKY-8818: for file_download we passed url=None above so the factory
                    # skipped its built-in goto. We must therefore navigate explicitly to
                    # self.url — not just when the page is about:blank, but whenever the
                    # working page is not already on the target URL (e.g. persistent
                    # browser sessions that carry state from a prior block).
                    if self.url:
                        _needs_navigation = working_page.url == "about:blank" or (
                            _is_file_download and working_page.url.rstrip("/") != self.url.rstrip("/")
                        )
                        if _needs_navigation:
                            await browser_state.navigate_to_url(
                                page=working_page,
                                url=self.url,
                                wait_until=_navigate_wait_until,
                            )

                    # When a browser profile is loaded, wait for the page to fully settle
                    # so that cookie-based authentication can redirect or restore the session
                    # BEFORE the agent starts interacting with the page.
                    if workflow_run.browser_profile_id:
                        LOG.info(
                            "Browser profile loaded — waiting for page to settle before agent acts",
                            browser_profile_id=workflow_run.browser_profile_id,
                            workflow_run_id=workflow_run.workflow_run_id,
                        )
                        try:
                            await working_page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            LOG.debug(
                                "networkidle timeout after browser profile load (non-fatal)",
                                workflow_run_id=workflow_run.workflow_run_id,
                            )

                except Exception as e:
                    LOG.exception(
                        "Failed to get browser state for first task",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                    )
                    await self._handle_task_failure_with_error_detection(
                        task=task,
                        step=step,
                        browser_state=browser_state,
                        failure_reason=str(e),
                        organization_id=workflow_run.organization_id,
                    )
                    raise e

                # Validate starter URL before downstream scraping on a blank page
                if not (self.url and self.url.strip()) and working_page.url in ("about:blank", "", ":"):
                    missing_url_exc = MissingStarterUrl(block_label=self.label)
                    LOG.warning(
                        "First browser block has no starter URL",
                        task_id=task.task_id,
                        workflow_run_id=workflow_run_id,
                        block_label=self.label,
                    )
                    await self._handle_task_failure_with_error_detection(
                        task=task,
                        step=step,
                        browser_state=browser_state,
                        failure_reason=str(missing_url_exc),
                        organization_id=workflow_run.organization_id,
                    )
                    raise missing_url_exc

                try:
                    # add screenshot artifact for the first task
                    screenshot = await browser_state.take_fullpage_screenshot()
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
                        # SKY-8818: use the hoisted wait_until override so file_download
                        # pages with slow subresources can still resolve via domcontentloaded.
                        await browser_state.navigate_to_url(
                            page=working_page,
                            url=self.url,
                            wait_until=_navigate_wait_until,
                        )
                    except Exception as e:
                        await self._handle_task_failure_with_error_detection(
                            task=task,
                            step=step,
                            browser_state=browser_state,
                            failure_reason=str(e),
                            organization_id=workflow_run.organization_id,
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
                await self._handle_task_failure_with_error_detection(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    failure_reason=str(e),
                    organization_id=workflow_run.organization_id,
                )
                raise e
            finally:
                current_context.task_id = None

            # Check task status
            updated_task = await app.DATABASE.tasks.get_task(
                task_id=task.task_id, organization_id=workflow_run.organization_id
            )
            if not updated_task:
                raise TaskNotFound(task.task_id)
            if not updated_task.status.is_final():
                raise UnexpectedTaskStatus(task_id=updated_task.task_id, status=updated_task.status)
            current_running_task = updated_task

            block_status_mapping = TASK_TO_BLOCK_STATUS
            if updated_task.status == TaskStatus.completed or updated_task.status == TaskStatus.terminated:
                LOG.info(
                    "Task completed",
                    sampling=True,
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

                # SKY-7005: scope downloaded files to the current loop iteration
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state if current_context else None,
                )

                task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
                    organization_id=workflow_run.organization_id,
                    task_id=updated_task.task_id,
                )
                workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )

                task_output = TaskOutput.from_task(
                    updated_task,
                    downloaded_files,
                    task_screenshot_artifact_ids=[a.artifact_id for a in task_screenshot_artifacts],
                    workflow_screenshot_artifact_ids=[a.artifact_id for a in workflow_screenshot_artifacts],
                )
                output_parameter_value = task_output.model_dump()
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
                return await self.build_block_result(
                    success=success,
                    failure_reason=(
                        updated_task.failure_reason
                        if success
                        else (
                            updated_task.failure_reason
                            or f"Task {updated_task.task_id} finished with status {updated_task.status}"
                        )
                    ),
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
                    failure_reason=updated_task.failure_reason or f"Task {updated_task.task_id} was canceled",
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
                    failure_reason=updated_task.failure_reason or f"Task {updated_task.task_id} timed out",
                    output_parameter_value=None,
                    status=block_status_mapping[updated_task.status],
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                current_retry += 1
                will_retry = current_retry <= self.max_retries
                if will_retry and _should_skip_retry_on_anti_bot_detection(updated_task):
                    LOG.warning(
                        "Skipping retry - task failed due to anti-bot detection",
                        task_id=updated_task.task_id,
                        workflow_run_id=workflow_run_id,
                        workflow_id=workflow.workflow_id,
                        organization_id=workflow_run.organization_id,
                        current_retry=current_retry,
                        max_retries=self.max_retries,
                        failure_reason=updated_task.failure_reason,
                        failure_category=updated_task.failure_category,
                    )
                    will_retry = False
                retry_message = f", retrying task {current_retry}/{self.max_retries}" if will_retry else ""
                downloaded_files = []
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

                # SKY-7005: scope downloaded files to the current loop iteration
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state if current_context else None,
                )

                task_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts(
                    organization_id=workflow_run.organization_id,
                    task_id=updated_task.task_id,
                )
                workflow_screenshot_artifacts = await app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts(
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )

                task_output = TaskOutput.from_task(
                    updated_task,
                    downloaded_files,
                    task_screenshot_artifact_ids=[a.artifact_id for a in task_screenshot_artifacts],
                    workflow_screenshot_artifact_ids=[a.artifact_id for a in workflow_screenshot_artifacts],
                )
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
                        failure_reason=(
                            updated_task.failure_reason
                            or f"Task {updated_task.task_id} failed with status {updated_task.status}"
                        ),
                        output_parameter_value=output_parameter_value,
                        status=block_status_mapping[updated_task.status],
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id)
        return await self.build_block_result(
            success=False,
            status=BlockStatus.failed,
            failure_reason=(
                (current_running_task.failure_reason or f"Task {current_running_task.task_id} failed")
                if current_running_task
                else "Task failed (no task reference available)"
            ),
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
    # True only when the loop exhausted all iterations naturally (for-loop) or the
    # condition turned false (while-loop). False on every early-return path
    # (cancel, structural error, max iterations, body failure with no swallow flag).
    natural_completion: bool = False

    def is_canceled(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.canceled

    def is_synthetic_loop_failure(self) -> bool:
        """Last appended result is a loop-structural / safety-limit failure, not a child."""
        return bool(self.block_outputs) and self.block_outputs[-1].is_synthetic_loop_failure

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

        # Swallow flags apply only on natural-completion paths whose last result
        # is a real child failure; structural/safety synthetics must propagate.
        if not self.natural_completion or self.is_synthetic_loop_failure():
            return False

        if self.last_block.continue_on_failure:
            return True

        if self.last_block.next_loop_on_failure:
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

    def resolve_status(self, parent_next_loop_on_failure: bool) -> tuple[BlockStatus, bool, str | None]:
        """Decide the loop block's overall status, success flag, and failure_reason.

        ``parent_next_loop_on_failure`` is the parent loop's swallow flag; when
        set, body failures swallowed mid-loop must not re-surface as the loop's
        overall status. Synthetic safety/structural failures still propagate.
        """
        parent_swallow = (
            parent_next_loop_on_failure
            and self.natural_completion
            and not self.is_canceled()
            and not self.is_synthetic_loop_failure()
        )

        if self.is_canceled():
            block_status = BlockStatus.canceled
            success = False
        elif self.is_completed() or parent_swallow:
            block_status = BlockStatus.completed
            success = True
        elif self.is_terminated():
            block_status = BlockStatus.terminated
            success = False
        else:
            block_status = BlockStatus.failed
            success = False

        failure_reason = None if success else self.get_failure_reason()
        return block_status, success, failure_reason


def compute_conditional_scopes(
    label_to_block: dict[str, Any],
    default_next_map: dict[str, str | None],
) -> dict[str, str]:
    """Map each block label to the conditional block label whose scope it belongs to.

    For each conditional block, trace each branch's chain of blocks via
    ``default_next_map``.  Labels that appear in **all** branch chains are
    considered merge-point blocks (i.e. they come *after* the conditional
    reconverges) and are **not** scoped.  Labels that appear in fewer chains
    than the total number of branches **are** inside the conditional.

    Inner conditionals are themselves scoped to an outer conditional, but
    their *own* branch targets are handled by a recursive application of
    the same logic (inner wins via the ``if lbl not in scopes`` guard).
    """
    scopes: dict[str, str] = {}

    conditional_labels = [lbl for lbl, blk in label_to_block.items() if blk.block_type == BlockType.CONDITIONAL]

    for cond_label in conditional_labels:
        cond_block = label_to_block[cond_label]
        branch_targets: list[str | None] = [branch.next_block_label for branch in cond_block.ordered_branches]
        # Deduplicate while preserving order – two branches may point to the same target
        seen_targets: set[str | None] = set()
        unique_targets: list[str | None] = []
        for t in branch_targets:
            if t not in seen_targets:
                seen_targets.add(t)
                unique_targets.append(t)

        num_branches = len(unique_targets)
        if num_branches == 0:
            continue

        # For each unique branch target, trace the chain via default_next_map.
        # Stop at other conditional blocks (they handle their own branches).
        chain_sets: list[list[str]] = []
        for target in unique_targets:
            chain: list[str] = []
            cur = target
            while cur and cur in label_to_block:
                chain.append(cur)
                # Stop tracing when we hit another conditional – it owns its own sub-tree
                if label_to_block[cur].block_type == BlockType.CONDITIONAL:
                    break
                cur = default_next_map.get(cur)
            chain_sets.append(chain)

        # Count how many branch chains each label appears in
        label_count: dict[str, int] = {}
        for chain in chain_sets:
            for lbl in chain:
                label_count[lbl] = label_count.get(lbl, 0) + 1

        # Labels appearing in ALL branches are merge points (after the conditional).
        # Labels appearing in fewer branches are inside the conditional.
        for chain in chain_sets:
            for lbl in chain:
                if label_count[lbl] >= num_branches:
                    # This is a merge point – stop scoping further along this chain
                    break
                if lbl not in scopes:
                    scopes[lbl] = cond_label

    return scopes


class ForLoopBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore

    loop_blocks: list[BlockTypeVar]
    loop_over: PARAMETER_TYPE | None = None
    loop_variable_reference: str | None = None
    complete_if_empty: bool = False
    # Note: intentionally excludes `list` (unlike BaseTaskBlock.data_schema) because a list schema
    # does not describe the shape of individual loop items -- only dict schemas are meaningful here.
    data_schema: dict[str, Any] | str | None = None

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
                    extraction_block = self._create_initial_extraction_block(
                        self.loop_variable_reference, workflow_run_context=workflow_run_context
                    )

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
                        raise ValueError(
                            f"Extraction block failed: "
                            f"{extraction_result.failure_reason or 'Unknown error (no failure reason provided)'}"
                        )

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

    def _create_initial_extraction_block(
        self,
        natural_language_prompt: str,
        workflow_run_context: WorkflowRunContext | None = None,
    ) -> ExtractionBlock:
        """Create an extraction block to process natural language input."""

        # Determine the items schema for loop_values
        items_schema: dict[str, Any] | None = None
        if self.data_schema is not None:
            if isinstance(self.data_schema, dict):
                items_schema = self.data_schema
            elif isinstance(self.data_schema, str):
                # Interpolate Jinja templates before parsing, matching how BaseTaskBlock.setup_block_v2
                # handles data_schema strings (see line 652-654)
                schema_str = self.data_schema
                if workflow_run_context is not None:
                    schema_str = self.format_block_parameter_template_from_workflow_run_context(
                        schema_str, workflow_run_context
                    )
                try:
                    parsed = json.loads(schema_str)
                    if isinstance(parsed, dict):
                        items_schema = parsed
                    else:
                        LOG.warning(
                            "Parsed data_schema is not a dict, falling back to default string schema",
                            block_label=self.label,
                            data_schema=self.data_schema,
                        )
                except (json.JSONDecodeError, TypeError):
                    LOG.warning(
                        "Failed to parse data_schema string, falling back to default string schema",
                        block_label=self.label,
                        data_schema=self.data_schema,
                    )

        if items_schema is not None:
            # User provided a custom schema — each loop iteration will produce a structured object
            data_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "loop_values": {
                        "type": "array",
                        "description": "Array of structured values to iterate over, matching the provided schema.",
                        "items": items_schema,
                    }
                },
            }
        else:
            # Default: extract simple string array
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

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            # Allow multiple branches of a conditional to point to the same target
            # without double-counting the incoming edge.
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

        queue: deque[str] = deque([roots[0]])
        visited_count = 0
        in_degree = dict(incoming)
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(label_to_block):
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph for cycles, orphans, and dangling references.

        Skips sequential defaulting so that disconnected subgraphs are detected.
        Also recursively validates any nested loop block children.
        Raises InvalidWorkflowDefinition (422) on validation failure.
        """
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial for-loop output to DB so data survives Temporal
        activity timeouts. The timeout handler runs on a different node and
        reads from DB — without this, accumulated iteration data is lost when
        the loop is killed mid-execution.

        Uses the DB UPSERT directly instead of record_output_parameter_value
        to avoid re-registering context parameters and emitting spurious
        'already has a registered value' warnings on every call.

        On the normal iteration path, this is called every
        PERSIST_LOOP_OUTPUT_INTERVAL iterations and on the final iteration
        to balance durability vs DB load. Early-return paths (failure,
        cancellation) always persist since they are terminal."""
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist for-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
                exc_info=True,
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

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        for loop_idx, loop_over_value in enumerate(loop_over_values):
            # Check max_iterations limit
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    f"ForLoopBlock Reached max_iterations limit ({DEFAULT_MAX_LOOP_ITERATIONS}), stopping loop",
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
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )
            loop_over_value_repr = repr(loop_over_value)
            if len(loop_over_value_repr) > MAX_LOOP_OVER_VALUE_LOG_CHARS:
                loop_over_value_repr = (
                    loop_over_value_repr[:MAX_LOOP_OVER_VALUE_LOG_CHARS]
                    + f"...[truncated, original size: {len(loop_over_value_repr)}]"
                )
            LOG.info("Starting loop iteration", loop_idx=loop_idx, loop_over_value=loop_over_value_repr)

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            # context parameter has been deprecated. However, it's still used by task v2 - we should migrate away from it.
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "ForLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": loop_over_value,
                    "current_item": loop_over_value,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                # Determine the parent for timeline nesting: if this block is
                # inside a conditional's scope, parent it to that conditional's
                # workflow_run_block rather than the loop's.
                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=str(loop_over_value),
                    current_index=loop_idx,
                )

                # Track conditional workflow_run_block_ids so branch targets
                # can be parented to them.
                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

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
                        await app.DATABASE.observer.update_workflow_run_block(
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
                        f"ForLoopBlock Reached max_steps_per_iteration limit ({DEFAULT_MAX_STEPS_PER_ITERATION}) in iteration {loop_idx}, stopping iteration",
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
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    # If next_loop_on_failure is False, stop the entire loop
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    # If next_loop_on_failure is True, break out of the block loop for this iteration
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        f"ForLoopBlock Block with type {loop_block.block_type} at index {block_idx} during loop {loop_idx} was canceled for workflow run {workflow_run_id}, canceling for loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_outputs,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        f"ForLoopBlock Encountered a failure processing block {block_idx} during loop {loop_idx}, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        failure_reason=block_output.failure_reason,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        f"ForLoopBlock Block {block_idx} during loop {loop_idx} failed but will continue to next iteration",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            is_last_iteration = loop_idx == len(loop_over_values) - 1
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0 or is_last_iteration:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this
        # loop finishes. Supports nested loops (parent's state is preserved) and
        # ensures stale per-iteration baselines don't leak into subsequent blocks.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
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

        await app.DATABASE.observer.update_workflow_run_block(
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

        try:
            loop_executed_result = await self.execute_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                loop_over_values=loop_over_values,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition as exc:
            LOG.error(
                "Loop graph validation failed",
                error=str(exc),
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason=str(exc),
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WhileLoopBlock(Block):
    """Loop block driven by a runtime condition. Iterates while ``condition`` evaluates truthy.

    Top-of-loop semantics: the condition is evaluated *before* each iteration (including the
    first). If the condition is false on the first check, the body never runs and the block
    returns success with an empty output list.

    Safety: the loop is capped at ``DEFAULT_MAX_LOOP_ITERATIONS`` (500). Reaching the cap is
    treated as a failure so that a misbehaving condition can never spin forever.
    """

    block_type: Literal[BlockType.WHILE_LOOP] = BlockType.WHILE_LOOP  # type: ignore

    loop_blocks: list[BlockTypeVar]
    # The discriminated union on ``criteria_type`` handles dict→typed coercion. Pydantic
    # rejects a dict missing ``criteria_type`` with ``union_tag_not_found`` before any
    # model_validator runs, so no extra coercion validator is needed here.
    condition: BranchCriteriaTypeVar

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters: set[PARAMETER_TYPE] = set()
        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        # Duplicated from ForLoopBlock._build_loop_graph for PR 1; promotion to a shared
        # helper is tracked in PR 7 (refactor).
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

        queue: deque[str] = deque([roots[0]])
        visited_count = 0
        in_degree = dict(incoming)
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(label_to_block):
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph and recurse into nested loop blocks."""
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial while-loop output to DB so accumulated iteration data survives
        Temporal activity timeouts. Mirrors ``ForLoopBlock._persist_partial_loop_output``.
        """
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist while-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
                exc_info=True,
            )

    async def _evaluate_condition(
        self,
        workflow_run_context: WorkflowRunContext,
        *,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> bool:
        """Evaluate the loop condition. Raises on rendering errors so the caller can convert
        the failure into a block result with a clear message.

        ``current_index`` (the 0-indexed iteration counter) is read from this block's own
        metadata via the existing for_loop injection in
        :meth:`format_block_parameter_template_from_workflow_run_context`. ``current_value``
        holds the same integer so ``{{ current_value }}`` caps work like For Each loops.
        The caller writes both onto ``self.label`` before invoking this method, so
        condition authors can bootstrap iteration 1 with
        ``{{ current_index == 0 or <body_output_ref> }}``.
        """
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                potential_template,
                workflow_run_context,
            ),
        )
        if isinstance(self.condition, PromptBranchCriteria):
            synthetic_branch = BranchCondition(
                id=str(uuid.uuid4()),
                criteria=self.condition,
                next_block_label=None,
                is_default=False,
            )
            results, _, _, _ = await _evaluate_prompt_branch_conditions_batch(
                log_label=self.label,
                branches=[synthetic_branch],
                evaluation_context=evaluation_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                workflow_id=self.output_parameter.workflow_id,
                extraction_description_suffix="while_loop condition",
            )
            return results[0]

        return await self.condition.evaluate(evaluation_context)

    async def _execute_while_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        loop_idx = 0
        while True:
            # Evaluate the condition at the top of every iteration (including the first).
            # The cap check fires *after* the condition check so that a loop which would
            # naturally exit on the (cap+1)-th check returns success rather than tripping
            # the cap one iteration early.
            #
            # Condition rendering errors always terminate the loop, regardless of
            # ``next_loop_on_failure``. The flag governs *body* failures (which can vary
            # iteration to iteration), but a Jinja render error means the condition itself
            # is malformed and will fail identically on the next iteration — there is no
            # forward progress to be made by retrying.
            # Expose ``current_index`` to the condition's template scope before evaluation
            # so authors can bootstrap iteration 0 or cap iterations. ``current_value`` and
            # ``current_item`` stay None so Jinja matches persisted timeline rows
            # (``execute_safe(..., current_value=None)``) and outer for-loop rows cannot leak.
            condition_metadata: BlockMetadata = {
                "current_index": loop_idx,
                "current_value": None,
                "current_item": None,
            }
            workflow_run_context.update_block_metadata(self.label, condition_metadata)

            try:
                should_continue = await self._evaluate_condition(
                    workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
            except (FailedToFormatJinjaStyleParameter, MissingJinjaVariables, ValueError) as exc:
                LOG.error(
                    "WhileLoopBlock condition evaluation failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    error=str(exc),
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Failed to evaluate while-loop condition: {str(exc)}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            if not should_continue:
                LOG.info(
                    "WhileLoopBlock condition is false, exiting loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                )
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                break

            # Check max_iterations limit: only fires when the condition is still true at
            # iteration index ``cap``, i.e. the loop would have run a (cap+1)-th body.
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    "WhileLoopBlock reached max_iterations limit, stopping loop",
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
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "WhileLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                # ``current_index`` is the iteration counter. ``current_value`` stays None so
                # runtime matches ``execute_safe`` / timeline rows; use ``{{ current_index }}``
                # in Jinja. ``current_item`` stays None.
                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": None,
                    "current_item": None,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                # ``current_value`` is None on persisted timeline rows and in block metadata;
                # iteration is available only as ``current_index``.
                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=None,
                    current_index=loop_idx,
                )

                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_value=output_value)

                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", url=loop_block.url, loop_idx=loop_idx)

                each_loop_output_values.append(
                    {
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )

                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.observer.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=None,
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)

                iteration_step_count += 1
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        "WhileLoopBlock reached max_steps_per_iteration limit, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        "WhileLoopBlock child block canceled, canceling while loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        loop_idx=loop_idx,
                        block_result=block_outputs,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        "WhileLoopBlock encountered a failure processing block, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        failure_reason=block_output.failure_reason,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        "WhileLoopBlock child block failed but will continue to next iteration",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            # We don't know "is_last_iteration" for a while-loop ahead of time, so persist
            # every PERSIST_LOOP_OUTPUT_INTERVAL iterations and once again at the top of the
            # next iteration when the condition is false (handled at the break above).
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

            loop_idx += 1

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this loop
        # finishes. Mirrors ForLoopBlock.execute.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if not self.loop_blocks:
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

        try:
            loop_executed_result = await self._execute_while_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition as exc:
            LOG.error(
                "While-loop graph validation failed",
                error=str(exc),
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason=str(exc),
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        # Special case: condition false on the very first check. The body never ran, so
        # there are no block_outputs. Return success with an empty output list — this is
        # the normal/expected "nothing to do" path for a while-loop.
        if not loop_executed_result.block_outputs:
            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=loop_executed_result.outputs_with_loop_values,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


SCHEMA_VALIDATION_MAX_ATTEMPTS = 2
SCHEMA_VALIDATION_MAX_ERRORS = 5


def _default_structured_output_schema(description: str) -> dict[str, Any]:
    # The output field is optional to preserve the legacy permissive default schema.
    return {
        "type": "object",
        "properties": {
            "output": {
                "type": "object",
                "description": description,
            }
        },
    }


def _default_text_prompt_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "llm_response": {
                "type": "string",
                "description": "Your response to the prompt",
            }
        },
    }


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _schema_type_description(schema_type: Any) -> str:
    if isinstance(schema_type, list):
        return " or ".join(str(t) for t in schema_type)
    return str(schema_type)


def _schema_path(error: ValidationError) -> str:
    schema_path = list(error.absolute_schema_path)
    path_parts: list[str] = []
    index = 0
    while index < len(schema_path):
        part = schema_path[index]
        if part == "properties" and index + 1 < len(schema_path):
            path_parts.append(str(schema_path[index + 1]))
            index += 2
            continue
        if part == "items":
            path_parts.append("[]")
            index += 1
            continue
        if part == "additionalProperties":
            path_parts.append("<map value>")
            index += 1
            continue
        if part == "patternProperties":
            path_parts.append("<map value>")
            index += 2 if index + 1 < len(schema_path) else 1
            continue
        index += 1

    return "root" + "".join(f"{part}" if part == "[]" else f".{part}" for part in path_parts)


def _format_schema_validation_error(error: ValidationError) -> str:
    path = _schema_path(error)
    actual_type = _json_type_name(error.instance)

    if error.validator == "type":
        expected_type = _schema_type_description(error.validator_value)
        return f"{path}: expected type {expected_type}, got {actual_type}"

    if error.validator == "required":
        match = re.match(r"'([^']+)' is a required property", error.message)
        if match:
            return f"{path}: missing required property {match.group(1)}"
        return f"{path}: missing required property"

    if error.validator == "additionalProperties":
        unexpected_count: int | None = None
        schema_properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        if isinstance(error.instance, dict) and isinstance(schema_properties, dict):
            unexpected_count = sum(1 for field in error.instance if field not in schema_properties)
        if unexpected_count is not None:
            return f"{path}: has {unexpected_count} unexpected properties"
        return f"{path}: has unexpected properties"

    if error.validator in {"minItems", "maxItems"} and isinstance(error.instance, list):
        return f"{path}: violates {error.validator}={error.validator_value}; item count={len(error.instance)}"

    if error.validator in {"minLength", "maxLength"} and isinstance(error.instance, str):
        return f"{path}: violates {error.validator}={error.validator_value}; string length={len(error.instance)}"

    if error.validator == "enum":
        allowed_count = len(error.validator_value) if isinstance(error.validator_value, list) else "configured"
        return f"{path}: value is not one of {allowed_count} allowed values; got {actual_type}"

    return f"{path}: violates {error.validator} constraint; got {actual_type}"


def _validate_response_against_json_schema(
    response: Any,
    json_schema: dict[str, Any] | None,
    schema_label: str,
    max_errors: int = SCHEMA_VALIDATION_MAX_ERRORS,
) -> str | None:
    if not json_schema:
        return None

    if not validate_schema(json_schema):
        return f"{schema_label} JSON schema is invalid."

    try:
        validator = Draft202012Validator(json_schema)
        validation_errors = [_format_schema_validation_error(error) for error in validator.iter_errors(response)]
    except Exception as e:
        LOG.warning(
            "Failed to validate LLM response against JSON schema",
            schema_label=schema_label,
            error_type=type(e).__name__,
            exc_info=True,
        )
        return f"{schema_label} JSON schema validation failed ({type(e).__name__})."

    validation_errors = list(dict.fromkeys(validation_errors))
    if not validation_errors:
        return None

    return f"LLM response does not match {schema_label.lower()} JSON schema: " + "; ".join(
        validation_errors[:max_errors]
    )


def _is_schema_configuration_failure(failure_reason: str) -> bool:
    return "JSON schema is invalid" in failure_reason or "JSON schema validation failed" in failure_reason


def _llm_response_format_failure_reason(error: Exception) -> str:
    return f"LLM response could not be parsed or coerced into the required JSON shape ({type(error).__name__})."


def _build_schema_validation_retry_prompt(prompt: str, failure_reason: str) -> str:
    return (
        f"{prompt}\n\n"
        "Your previous response failed JSON schema validation.\n"
        f"Validation error: {failure_reason}\n\n"
        "Retry the task. Return only valid JSON that exactly matches the schema. "
        "Do not include markdown, code fences, explanatory text, or extra fields."
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

        await app.DATABASE.observer.update_workflow_run_block(
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

        await app.DATABASE.workflow_runs.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.paused,
        )

        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
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
        if browser_session_id:
            browser_session_url = f"{settings.SKYVERN_APP_URL}/browser-session/{browser_session_id}"
            body += f"To interact with the browser session directly, visit {browser_session_url}\n\n"
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

            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
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

    selector: str | None = None
    ai_fallback: AIFallbackMode = AIFallbackMode.FALLBACK


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
    include_extracted_text: bool = False


class LoginBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.LOGIN] = BlockType.LOGIN  # type: ignore

    # Opt out of reusing the credential's saved browser profile so the run logs in fresh and
    # the captured session persists via the normal path (a reused profile is loaded read-only).
    skip_saved_profile: bool = False


class FileDownloadBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FILE_DOWNLOAD] = BlockType.FILE_DOWNLOAD  # type: ignore


class UrlBlock(BaseTaskBlock):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.GOTO_URL] = BlockType.GOTO_URL  # type: ignore
    url: str


def get_all_blocks(blocks: list[BlockTypeVar]) -> list[BlockTypeVar]:
    """
    Recursively get "all blocks" in a workflow definition.

    Blocks can be nested via ForLoop and WhileLoop blocks. This function returns
    all blocks, flattened.
    """

    all_blocks: list[BlockTypeVar] = []

    for block in blocks:
        all_blocks.append(block)

        if block.block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
            nested_blocks = get_all_blocks(block.loop_blocks)
            all_blocks.extend(nested_blocks)

    return all_blocks


# isort: off
# branching.py is a leaf module (it only imports Block-module names lazily at call time),
# so this import is cycle-safe in any import order. It lives down here with the other
# submodule imports to keep the facade re-export section in one place.
# ``_evaluate_prompt_branch_conditions_batch`` is re-exported because WhileLoopBlock (which
# stays here) drives while-loop conditions through it. ``ConditionalBlock`` is defined below
# rather than in branching.py because it subclasses ``Block``; a module-level Block import in
# branching.py would make it un-importable on its own (see PR #6979 review).
from skyvern.forge.sdk.workflow.models.branching import (  # noqa: E402
    DECISION_BLOCK_FIELD_MAX_BYTES,  # noqa: F401 - re-exported for facade compatibility
    BranchCondition,
    BranchCriteria,  # noqa: F401 - re-exported for facade compatibility
    BranchCriteriaSubclasses,  # noqa: F401 - re-exported for facade compatibility
    BranchCriteriaTypeVar,
    BranchEvaluationContext,
    JinjaBranchCriteria,  # noqa: F401 - re-exported for facade compatibility
    PromptBranchCriteria,
    _build_branch_evaluation_schema,  # noqa: F401 - re-exported for facade compatibility
    _cap_debug_field,  # noqa: F401 - re-exported for facade compatibility
    _coerce_condition_index,  # noqa: F401 - re-exported for facade compatibility
    _evaluate_prompt_branch_conditions_batch,
    _make_empty_params_explicit,  # noqa: F401 - re-exported for facade compatibility
    _render_jinja_expression_for_display,
    _trim_branch_evaluations,  # noqa: F401 - re-exported for facade compatibility
)

# Late import: these sibling modules import Block from block_base, so this re-export lives at the
# bottom; every name below is re-exported for zero call-site changes.
from skyvern.forge.sdk.workflow.models.code_block import (  # noqa: E402, F401
    CodeBlock,
    CodeBlockOTPError,
    CodeBlockStep,
    Credential,
    _bind_code_block_otp,
    _code_block_otp_builtin,
    _register_code_block_secret,
    _resolve_code_block_otp,
)
from skyvern.forge.sdk.workflow.models.parser_blocks import (  # noqa: E402
    FileParserBlock,
    PDFParserBlock,
)
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import (  # noqa: E402
    GoogleSheetsReadBlock,
    GoogleSheetsWriteBlock,
)
from skyvern.forge.sdk.workflow.models.pdf_fill_block import PdfFillBlock  # noqa: E402
from skyvern.forge.sdk.workflow.models.storage_blocks import (  # noqa: E402
    DownloadToS3Block,
    FileUploadBlock,
    UploadToS3Block,
)
from skyvern.forge.sdk.workflow.models.misc_blocks import (  # noqa: E402, F401
    SECRET_RESPONSE_BODY_REDACTED,
    HttpRequestBlock,
    PrintPageBlock,
    SendEmailBlock,
    TaskV2Block,
    TextPromptBlock,
    WaitBlock,
    WorkflowTriggerBlock,
    _apply_secret_response_paths,
    _secret_path_suffix,
)
# isort: on


class ConditionalBlock(Block):
    """Branching block that selects the next block label based on list-ordered conditions."""

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CONDITIONAL] = BlockType.CONDITIONAL  # type: ignore

    branch_conditions: list[BranchCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_branches(self) -> ConditionalBlock:
        if not self.branch_conditions:
            raise ValueError("Conditional blocks require at least one branch.")

        default_branches = [branch for branch in self.branch_conditions if branch.is_default]
        if len(default_branches) > 1:
            raise ValueError("Only one default branch is permitted per conditional block.")

        return self

    def get_all_parameters(
        self,
        workflow_run_id: str,  # noqa: ARG002 - preserved for interface compatibility
    ) -> list[PARAMETER_TYPE]:
        # BranchCriteria subclasses will surface their parameter dependencies once implemented.
        return []

    async def _evaluate_prompt_branches(
        self,
        *,
        branches: list[BranchCondition],
        evaluation_context: BranchEvaluationContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> tuple[list[bool], list[str], str | None, dict | None]:
        """
        Evaluate natural language branch conditions in batch.

        All prompt-based conditions are batched into ONE LLM call for performance.
        Jinja parts ({{ }}) are pre-rendered before sending to LLM.

        Evaluation strategy:
        - If any condition is pure natural language, use ExtractionBlock for browser/page context.
        - If all conditions contain Jinja and are pre-rendered, use direct LLM call (no browser context).

        Returns:
            A tuple of (results, rendered_expressions, extraction_goal, llm_response):
            - results: List of boolean results for each branch
            - rendered_expressions: List of expressions after Jinja pre-rendering
            - extraction_goal: The prompt sent to the LLM (for UI display)
            - llm_response: The raw LLM response for debugging
        """
        return await _evaluate_prompt_branch_conditions_batch(
            log_label=self.label,
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            workflow_id=self.output_parameter.workflow_id,
            extraction_description_suffix=f"{len(branches)} conditions",
        )

    async def execute(  # noqa: D401
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        """
        Evaluate conditional branches and determine next block to execute.

        Returns a BlockResult with branch metadata in the output_parameter_value.
        """
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=(
                lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                    potential_template,
                    workflow_run_context,
                )
            )
            if workflow_run_context
            else None,
        )

        matched_branch = None
        failure_reason: str | None = None

        # Track all branch evaluations for UI display
        branch_evaluations_list: list[dict] = []
        prompt_rendered_by_id: dict[str, str] = {}

        natural_language_branches = [
            branch for branch in self.ordered_branches if isinstance(branch.criteria, PromptBranchCriteria)
        ]
        prompt_results_by_id: dict[str, bool] = {}
        prompt_llm_response: dict | None = None
        prompt_extraction_goal: str | None = None
        if natural_language_branches:
            try:
                (
                    prompt_results,
                    prompt_rendered_expressions,
                    prompt_extraction_goal,
                    prompt_llm_response,
                ) = await self._evaluate_prompt_branches(
                    branches=natural_language_branches,
                    evaluation_context=evaluation_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                prompt_results_by_id = {
                    branch.id: result for branch, result in zip(natural_language_branches, prompt_results, strict=False)
                }
                prompt_rendered_by_id = {
                    branch.id: rendered
                    for branch, rendered in zip(natural_language_branches, prompt_rendered_expressions, strict=False)
                }
            except Exception as exc:
                failure_reason = f"Failed to evaluate natural language branches: {str(exc)}"
                LOG.error(
                    "Failed to evaluate natural language branches",
                    block_label=self.label,
                    error=str(exc),
                    exc_info=True,
                )

        for idx, branch in enumerate(self.ordered_branches):
            branch_eval: dict = {
                "branch_id": branch.id,
                "branch_index": idx,
                "criteria_type": branch.criteria.criteria_type if branch.criteria else None,
                "original_expression": branch.criteria.expression if branch.criteria else None,
                "rendered_expression": None,
                "result": None,
                "is_matched": False,
                "is_default": branch.is_default,
                "next_block_label": branch.next_block_label,
                "error": None,
            }

            # Handle default branch (no criteria to evaluate)
            if branch.criteria is None:
                # Default branch - only matched if no other branch matches
                branch_evaluations_list.append(branch_eval)
                continue

            if branch.criteria.criteria_type == "prompt":
                if failure_reason:
                    branch_eval["error"] = failure_reason
                    branch_evaluations_list.append(branch_eval)
                    break
                prompt_result = prompt_results_by_id.get(branch.id)
                rendered_expr = prompt_rendered_by_id.get(branch.id)
                branch_eval["rendered_expression"] = rendered_expr
                if prompt_result is None:
                    failure_reason = "Missing result for natural language branch evaluation"
                    branch_eval["error"] = failure_reason
                    LOG.error(
                        "Missing prompt evaluation result",
                        block_label=self.label,
                        branch_index=idx,
                        branch_id=branch.id,
                    )
                    branch_evaluations_list.append(branch_eval)
                    break
                branch_eval["result"] = prompt_result
                branch_evaluations_list.append(branch_eval)
                if prompt_result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional natural language branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
                continue

            # Jinja template branch
            try:
                # Render the expression for UI display - substitute variables without evaluating
                rendered_expression = _render_jinja_expression_for_display(
                    expression=branch.criteria.expression,
                    context_values=evaluation_context.workflow_run_context.values
                    if evaluation_context.workflow_run_context
                    else {},
                    block_label=self.label,
                )
                branch_eval["rendered_expression"] = rendered_expression

                result = await branch.criteria.evaluate(evaluation_context)
                branch_eval["result"] = result
                branch_evaluations_list.append(branch_eval)

                if result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
            except Exception as exc:
                failure_reason = f"Failed to evaluate branch {idx} for {self.label}: {str(exc)}"
                branch_eval["error"] = str(exc)
                branch_eval["result"] = None
                branch_evaluations_list.append(branch_eval)
                LOG.error(
                    "Failed to evaluate conditional branch",
                    block_label=self.label,
                    branch_index=idx,
                    error=str(exc),
                    exc_info=True,
                )
                break

        if matched_branch is None and failure_reason is None:
            matched_branch = self.get_default_branch()
            # Update is_matched for default branch in evaluations
            if matched_branch:
                for eval_entry in branch_evaluations_list:
                    if eval_entry["branch_id"] == matched_branch.id:
                        eval_entry["is_matched"] = True
                        break

        matched_index = self.ordered_branches.index(matched_branch) if matched_branch in self.ordered_branches else None
        next_block_label = matched_branch.next_block_label if matched_branch else None
        executed_branch_id = matched_branch.id if matched_branch else None

        # Extract execution details for frontend display
        executed_branch_expression: str | None = None
        executed_branch_result: bool | None = None
        executed_branch_next_block: str | None = None

        if matched_branch:
            executed_branch_next_block = matched_branch.next_block_label
            if matched_branch.is_default:
                # Default/else branch - no expression to evaluate
                executed_branch_expression = None
                executed_branch_result = None
            elif matched_branch.criteria:
                # Regular condition branch - it matched
                executed_branch_expression = matched_branch.criteria.expression
                executed_branch_result = True

        branch_metadata: BlockMetadata = {
            "branch_taken": next_block_label,
            "branch_index": matched_index,
            "branch_id": executed_branch_id,
            "branch_description": matched_branch.description if matched_branch else None,
            "criteria_type": matched_branch.criteria.criteria_type
            if matched_branch and matched_branch.criteria
            else None,
            "criteria_expression": matched_branch.criteria.expression
            if matched_branch and matched_branch.criteria
            else None,
            "next_block_label": next_block_label,
            # Detailed evaluation info for all branches (rendered_expression trimmed/capped — SKY-9779)
            "evaluations": _trim_branch_evaluations(branch_evaluations_list) if branch_evaluations_list else None,
            # Raw LLM response for debugging prompt-based evaluations (masked for secrets, capped)
            "llm_response": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_llm_response)
                if workflow_run_context and prompt_llm_response
                else prompt_llm_response
            ),
            # The exact prompt sent to LLM for debugging (masked for secrets, capped)
            "llm_prompt": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_extraction_goal)
                if workflow_run_context and prompt_extraction_goal
                else prompt_extraction_goal
            ),
        }

        status = BlockStatus.completed
        success = True

        if failure_reason:
            status = BlockStatus.failed
            success = False
        elif matched_branch is None:
            failure_reason = "No conditional branch matched and no default branch configured"
            status = BlockStatus.failed
            success = False

        if workflow_run_context:
            workflow_run_context.update_block_metadata(self.label, branch_metadata)
            try:
                await self.record_output_parameter_value(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    value=branch_metadata,
                )
            except Exception as exc:
                LOG.warning(
                    "Failed to record branch metadata as output parameter",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    error=str(exc),
                )

        block_result = await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=branch_metadata,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            executed_branch_id=executed_branch_id,
            executed_branch_expression=executed_branch_expression,
            executed_branch_result=executed_branch_result,
            executed_branch_next_block=executed_branch_next_block,
        )
        return block_result

    @property
    def ordered_branches(self) -> list[BranchCondition]:
        """Convenience accessor that returns branches in author-specified list order."""
        return list(self.branch_conditions)

    def get_default_branch(self) -> BranchCondition | None:
        """Return the default/else branch when configured."""
        return next((branch for branch in self.branch_conditions if branch.is_default), None)


BlockSubclasses = Union[
    ConditionalBlock,
    ForLoopBlock,
    WhileLoopBlock,
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
    PrintPageBlock,
    WorkflowTriggerBlock,
    GoogleSheetsReadBlock,
    GoogleSheetsWriteBlock,
    PdfFillBlock,
]
BlockTypeVar = Annotated[BlockSubclasses, Field(discriminator="block_type")]


def resolve_conditional_merge_edges(
    blocks: list[BlockTypeVar],
    label_to_block: dict[str, BlockTypeVar],
    default_next_map: dict[str, str | None],
) -> None:
    """Point each conditional branch chain's terminal block at the conditional's successor (merge point).

    SKY-8571: iterates to convergence so an outer conditional patched on one pass can let an inner
    conditional's branch terminals be patched on the next. Mutates default_next_map in place.
    """
    changed = True
    while changed:
        changed = False
        for block in blocks:
            if not isinstance(block, ConditionalBlock):
                continue
            successor = default_next_map.get(block.label)
            if not successor:
                continue
            for branch in block.ordered_branches:
                target = branch.next_block_label
                if not target or target == successor:
                    continue
                cur: str | None = target
                visited: set[str] = set()
                while cur and cur in label_to_block and cur not in visited:
                    if cur == successor:
                        break
                    visited.add(cur)
                    nxt = default_next_map.get(cur)
                    if nxt is None:
                        default_next_map[cur] = successor
                        changed = True
                        break
                    cur = nxt
