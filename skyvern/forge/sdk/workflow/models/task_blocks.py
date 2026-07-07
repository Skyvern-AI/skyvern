"""Task-block family: the shared task execution engine and its block subclasses.

Extracted from ``block.py`` (see SKY-11658). ``block.py`` re-exports every public
name defined here, so existing ``from ...models.block import TaskBlock`` call sites
keep working unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from urllib.parse import quote

import structlog

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import (
    MissingBrowserState,
    MissingBrowserStatePage,
    MissingStarterUrl,
    TaskNotFound,
    UnexpectedTaskStatus,
)
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.api import email
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models.block_base import (
    Block,
    capture_block_download_baseline,
    warn_if_file_download_max_steps_low,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import AIFallbackMode, BlockResult, BlockStatus, BlockType
from skyvern.services.error_detection_service import detect_user_defined_errors_for_task
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


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

    def get_engine(self) -> RunEngine | None:
        return self.engine

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
