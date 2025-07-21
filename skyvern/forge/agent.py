import asyncio
import base64
import json
import os
import random
import string
from asyncio.exceptions import CancelledError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Tuple, cast

import httpx
import structlog
from openai.types.responses.response import Response as OpenAIResponse
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Page

from skyvern import analytics
from skyvern.config import settings
from skyvern.constants import (
    BROWSER_DOWNLOADING_SUFFIX,
    DEFAULT_MAX_SCREENSHOT_SCROLLS,
    GET_DOWNLOADED_FILES_TIMEOUT,
    SAVE_DOWNLOADED_FILES_TIMEOUT,
    SCRAPE_TYPE_ORDER,
    SPECIAL_FIELD_VERIFICATION_CODE,
    ScrapeType,
)
from skyvern.exceptions import (
    BrowserSessionNotFound,
    BrowserStateMissingPage,
    DownloadFileMaxWaitingTime,
    EmptyScrapePage,
    FailedToNavigateToUrl,
    FailedToParseActionInstruction,
    FailedToSendWebhook,
    FailedToTakeScreenshot,
    InvalidTaskStatusTransition,
    InvalidWorkflowTaskURLState,
    MissingBrowserState,
    MissingBrowserStatePage,
    NoTOTPVerificationCodeFound,
    ScrapingFailed,
    SkyvernException,
    StepTerminationError,
    StepUnableToExecuteError,
    TaskAlreadyCanceled,
    TaskAlreadyTimeout,
    TaskNotFound,
    UnsupportedActionType,
    UnsupportedTaskType,
)
from skyvern.forge import app
from skyvern.forge.async_operations import AgentPhase, AsyncOperationPool
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    get_path_for_workflow_download_directory,
    list_downloading_files_in_directory,
    list_files_in_directory,
    rename_file,
    wait_for_download_finished,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory, LLMCaller, LLMCallerManager
from skyvern.forge.sdk.api.llm.ui_tars_llm_caller import UITarsLLMCaller
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_headers
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.log_artifacts import save_step_logs, save_task_logs
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.trace import TraceManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import ActionBlock, BaseTaskBlock, ValidationBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from skyvern.schemas.runs import CUA_ENGINES, RunEngine
from skyvern.services import run_service
from skyvern.services.task_v1_service import is_cua_task
from skyvern.utils.image_resizer import Resolution
from skyvern.utils.prompt_engine import load_prompt_with_elements
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    CompleteAction,
    CompleteVerifyResult,
    DecisiveAction,
    ExtractAction,
    ReloadPageAction,
    TerminateAction,
    UserDefinedError,
    WebAction,
)
from skyvern.webeye.actions.caching import retrieve_action_plan
from skyvern.webeye.actions.handler import ActionHandler, poll_verification_code
from skyvern.webeye.actions.models import AgentStepOutput, DetailedAgentStepOutput
from skyvern.webeye.actions.parse_actions import (
    parse_actions,
    parse_anthropic_actions,
    parse_cua_actions,
    parse_ui_tars_actions,
)
from skyvern.webeye.actions.responses import ActionResult, ActionSuccess
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ElementTreeFormat, ScrapedPage, scrape_website
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()


class ActionLinkedNode:
    def __init__(self, action: Action) -> None:
        self.action = action
        self.next: ActionLinkedNode | None = None


class ForgeAgent:
    def __init__(self) -> None:
        if settings.ADDITIONAL_MODULES:
            for module in settings.ADDITIONAL_MODULES:
                LOG.debug("Loading additional module", module=module)
                __import__(module)
            LOG.debug(
                "Additional modules loaded",
                modules=settings.ADDITIONAL_MODULES,
            )
        self.async_operation_pool = AsyncOperationPool()

    async def create_task_and_step_from_block(
        self,
        task_block: BaseTaskBlock,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        workflow_run_context: WorkflowRunContext,
        task_order: int,
        task_retry: int,
    ) -> tuple[Task, Step]:
        task_block_parameters = task_block.parameters
        navigation_payload = {}
        for parameter in task_block_parameters:
            navigation_payload[parameter.key] = workflow_run_context.get_value(parameter.key)

        task_url = task_block.url
        if task_url is None:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id, parent_workflow_run_id=workflow_run.parent_workflow_run_id
            )
            if browser_state is None:
                raise MissingBrowserState(workflow_run_id=workflow_run.workflow_run_id)

            working_page = await browser_state.get_working_page()
            if not working_page:
                LOG.error(
                    "BrowserState has no page",
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)

            if working_page.url == "about:blank":
                raise InvalidWorkflowTaskURLState(workflow_run.workflow_run_id)

            task_url = working_page.url

        task = await app.DATABASE.create_task(
            url=task_url,
            task_type=task_block.task_type,
            complete_criterion=task_block.complete_criterion,
            terminate_criterion=task_block.terminate_criterion,
            title=task_block.title or task_block.label,
            webhook_callback_url=None,
            totp_verification_url=task_block.totp_verification_url,
            totp_identifier=task_block.totp_identifier,
            navigation_goal=task_block.navigation_goal,
            data_extraction_goal=task_block.data_extraction_goal,
            navigation_payload=navigation_payload,
            organization_id=workflow_run.organization_id,
            proxy_location=workflow_run.proxy_location,
            extracted_information_schema=task_block.data_schema,
            workflow_run_id=workflow_run.workflow_run_id,
            order=task_order,
            retry=task_retry,
            max_steps_per_run=task_block.max_steps_per_run,
            error_code_mapping=task_block.error_code_mapping,
            include_action_history_in_verification=task_block.include_action_history_in_verification,
            model=task_block.model,
            max_screenshot_scrolling_times=workflow_run.max_screenshot_scrolls,
            extra_http_headers=workflow_run.extra_http_headers,
        )
        LOG.info(
            "Created a new task for workflow run",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            task_id=task.task_id,
            url=task.url,
            title=task.title,
            proxy_location=task.proxy_location,
            task_order=task_order,
            task_retry=task_retry,
        )
        # Update task status to running
        task = await app.DATABASE.update_task(
            task_id=task.task_id,
            organization_id=task.organization_id,
            status=TaskStatus.running,
        )
        step = await app.DATABASE.create_step(
            task.task_id,
            order=0,
            retry_index=0,
            organization_id=task.organization_id,
        )
        LOG.info(
            "Created new step for workflow run",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            step_id=step.step_id,
            task_id=task.task_id,
            order=step.order,
            retry_index=step.retry_index,
        )
        return task, step

    async def create_task(self, task_request: TaskRequest, organization_id: str) -> Task:
        webhook_callback_url = str(task_request.webhook_callback_url) if task_request.webhook_callback_url else None
        totp_verification_url = str(task_request.totp_verification_url) if task_request.totp_verification_url else None
        # validate browser session id
        if task_request.browser_session_id:
            browser_session = await app.DATABASE.get_persistent_browser_session(
                session_id=task_request.browser_session_id,
                organization_id=organization_id,
            )
            if not browser_session:
                raise BrowserSessionNotFound(browser_session_id=task_request.browser_session_id)

        task = await app.DATABASE.create_task(
            url=str(task_request.url),
            title=task_request.title,
            webhook_callback_url=webhook_callback_url,
            totp_verification_url=totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            navigation_goal=task_request.navigation_goal,
            complete_criterion=task_request.complete_criterion,
            terminate_criterion=task_request.terminate_criterion,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_payload,
            organization_id=organization_id,
            proxy_location=task_request.proxy_location,
            extracted_information_schema=task_request.extracted_information_schema,
            error_code_mapping=task_request.error_code_mapping,
            application=task_request.application,
            include_action_history_in_verification=task_request.include_action_history_in_verification,
            model=task_request.model,
            max_screenshot_scrolling_times=task_request.max_screenshot_scrolls,
            extra_http_headers=task_request.extra_http_headers,
            browser_session_id=task_request.browser_session_id,
        )
        LOG.info(
            "Created new task",
            task_id=task.task_id,
            url=task.url,
            proxy_location=task.proxy_location,
            organization_id=organization_id,
        )
        return task

    async def register_async_operations(self, organization: Organization, task: Task, page: Page) -> None:
        operations = await app.AGENT_FUNCTION.generate_async_operations(organization, task, page)
        self.async_operation_pool.add_operations(task.task_id, operations)

    @TraceManager.traced_async(
        ignore_inputs=["api_key", "close_browser_on_completion", "task_block", "cua_response", "llm_caller"]
    )
    async def execute_step(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        task_block: BaseTaskBlock | None = None,
        browser_session_id: str | None = None,
        complete_verification: bool = True,
        engine: RunEngine = RunEngine.skyvern_v1,
        cua_response: OpenAIResponse | None = None,
        llm_caller: LLMCaller | None = None,
    ) -> Tuple[Step, DetailedAgentStepOutput | None, Step | None]:
        # do not need to do complete verification when it's a CUA task
        # 1. CUA executes only one action step by step -- it's pretty less likely to have a hallucination for completion or forget to return a complete
        # 2. It will significantly slow down CUA tasks
        if engine in CUA_ENGINES:
            complete_verification = False

        workflow_run: WorkflowRun | None = None
        if task.workflow_run_id:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=task.workflow_run_id,
                organization_id=organization.organization_id,
            )
            if workflow_run and workflow_run.status == WorkflowRunStatus.canceled:
                LOG.info(
                    "Workflow run is canceled, stopping execution inside task",
                    workflow_run_id=workflow_run.workflow_run_id,
                    step_id=step.step_id,
                )
                step = await self.update_step(
                    step,
                    status=StepStatus.canceled,
                    is_last=True,
                )
                task = await self.update_task(
                    task,
                    status=TaskStatus.canceled,
                )
                return step, None, None

            if workflow_run and workflow_run.status == WorkflowRunStatus.timed_out:
                LOG.info(
                    "Workflow run is timed out, stopping execution inside task",
                    workflow_run_id=workflow_run.workflow_run_id,
                    step_id=step.step_id,
                )
                step = await self.update_step(
                    step,
                    status=StepStatus.canceled,
                    is_last=True,
                )
                task = await self.update_task(
                    task,
                    status=TaskStatus.timed_out,
                )
                return step, None, None

        refreshed_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=organization.organization_id)
        if refreshed_task:
            task = refreshed_task

        if task.status == TaskStatus.canceled:
            LOG.info(
                "Task is canceled, stopping execution",
                task_id=task.task_id,
            )
            step = await self.update_step(
                step,
                status=StepStatus.canceled,
                is_last=True,
            )
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                need_call_webhook=True,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
            )
            return step, None, None

        context = skyvern_context.current()
        override_max_steps_per_run = context.max_steps_override if context else None
        max_steps_per_run = (
            override_max_steps_per_run
            or task.max_steps_per_run
            or organization.max_steps_per_run
            or settings.MAX_STEPS_PER_RUN
        )
        if max_steps_per_run and task.max_steps_per_run != max_steps_per_run:
            await app.DATABASE.update_task(
                task_id=task.task_id,
                organization_id=organization.organization_id,
                max_steps_per_run=max_steps_per_run,
            )
        next_step: Step | None = None
        detailed_output: DetailedAgentStepOutput | None = None
        list_files_before: list[str] = []
        try:
            if task.workflow_run_id:
                list_files_before = list_files_in_directory(
                    get_path_for_workflow_download_directory(task.workflow_run_id)
                )
            # Check some conditions before executing the step, throw an exception if the step can't be executed
            await app.AGENT_FUNCTION.validate_step_execution(task, step)

            (
                step,
                browser_state,
                detailed_output,
            ) = await self.initialize_execution_state(task, step, workflow_run, browser_session_id)

            # mark step as completed and mark task as completed
            if (
                not task.navigation_goal
                and not task.data_extraction_goal
                and not task.complete_criterion
                and not task.terminate_criterion
            ):
                # most likely a GOTO_URL task block
                page = await browser_state.must_get_working_page()
                current_url = page.url
                if current_url.rstrip("/") != task.url.rstrip("/"):
                    await page.goto(task.url)
                step = await self.update_step(
                    step, status=StepStatus.completed, is_last=True, output=AgentStepOutput(action_results=[])
                )
                task = await self.update_task(task, status=TaskStatus.completed)
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    need_call_webhook=True,
                    close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                    browser_session_id=browser_session_id,
                )
                return step, detailed_output, None

            if page := await browser_state.get_working_page():
                await self.register_async_operations(organization, task, page)

            if engine == RunEngine.anthropic_cua and not llm_caller:
                # see if the llm_caller is already set in memory
                llm_caller = LLMCallerManager.get_llm_caller(task.task_id)
                if not llm_caller:
                    # if not, create a new llm_caller
                    llm_key = task.llm_key
                    llm_caller = LLMCaller(
                        llm_key=llm_key or settings.ANTHROPIC_CUA_LLM_KEY, screenshot_scaling_enabled=True
                    )

            if engine == RunEngine.ui_tars and not llm_caller:
                # see if the llm_caller is already set in memory
                llm_caller = LLMCallerManager.get_llm_caller(task.task_id)
                if not llm_caller:
                    # create a new UI-TARS llm_caller
                    llm_key = task.llm_key or settings.VOLCENGINE_CUA_LLM_KEY
                    ui_tars_llm_caller = UITarsLLMCaller(llm_key=llm_key, screenshot_scaling_enabled=True)
                    ui_tars_llm_caller.initialize_conversation(task)
                    llm_caller = ui_tars_llm_caller

            # TODO: remove the code after migrating everything to llm callers
            # currently, only anthropic cua and ui_tars tasks use llm_caller
            if engine in [RunEngine.anthropic_cua, RunEngine.ui_tars] and llm_caller:
                LLMCallerManager.set_llm_caller(task.task_id, llm_caller)

            step, detailed_output = await self.agent_step(
                task,
                step,
                browser_state,
                organization=organization,
                task_block=task_block,
                complete_verification=complete_verification,
                engine=engine,
                cua_response=cua_response,
                llm_caller=llm_caller,
            )
            await app.AGENT_FUNCTION.post_step_execution(task, step)
            task = await self.update_task_errors_from_detailed_output(task, detailed_output)
            retry = False

            if task_block and task_block.complete_on_download and task.workflow_run_id:
                workflow_download_directory = get_path_for_workflow_download_directory(task.workflow_run_id)

                downloading_files: list[Path] = list_downloading_files_in_directory(workflow_download_directory)
                if len(downloading_files) > 0:
                    LOG.info(
                        "Detecting files are still downloading, waiting for files to be completely downloaded.",
                        downloading_files=downloading_files,
                        step_id=step.step_id,
                    )
                    try:
                        await wait_for_download_finished(downloading_files=downloading_files)
                    except DownloadFileMaxWaitingTime as e:
                        LOG.warning(
                            "There're several long-time downloading files, these files might be broken",
                            downloading_files=e.downloading_files,
                            task_id=task.task_id,
                            step_id=step.step_id,
                            workflow_run_id=task.workflow_run_id,
                        )

                list_files_after = list_files_in_directory(workflow_download_directory)
                if len(list_files_after) > len(list_files_before):
                    files_to_rename = list(set(list_files_after) - set(list_files_before))
                    for file in files_to_rename:
                        file_extension = Path(file).suffix
                        if file_extension == BROWSER_DOWNLOADING_SUFFIX:
                            LOG.warning(
                                "Detecting incompleted download file, skip the rename",
                                file=file,
                                task_id=task.task_id,
                                workflow_run_id=task.workflow_run_id,
                            )
                            continue

                        random_file_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                        random_file_name = f"download-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{random_file_id}"
                        if task_block.download_suffix:
                            random_file_name = f"{random_file_name}-{task_block.download_suffix}"
                        rename_file(os.path.join(workflow_download_directory, file), random_file_name + file_extension)

                    LOG.info(
                        "Task marked as completed due to download",
                        task_id=task.task_id,
                        num_files_before=len(list_files_before),
                        num_files_after=len(list_files_after),
                        new_files=files_to_rename,
                    )
                    last_step = await self.update_step(step, is_last=True)
                    completed_task = await self.update_task(
                        task,
                        status=TaskStatus.completed,
                    )
                    await self.clean_up_task(
                        task=completed_task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return last_step, detailed_output, None

            # If the step failed, mark the step as failed and retry
            if step.status == StepStatus.failed:
                maybe_next_step = await self.handle_failed_step(organization, task, step)
                # If there is no next step, it means that the task has failed
                if maybe_next_step:
                    next_step = maybe_next_step
                    retry = True
                else:
                    await self.clean_up_task(
                        task=task,
                        last_step=step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return step, detailed_output, None
            elif step.status == StepStatus.completed:
                # TODO (kerem): keep the task object uptodate at all times so that clean_up_task can just use it
                (
                    is_task_completed,
                    maybe_last_step,
                    maybe_next_step,
                ) = await self.handle_completed_step(
                    organization=organization,
                    task=task,
                    step=step,
                    page=await browser_state.get_working_page(),
                    task_block=task_block,
                )
                if is_task_completed is not None and maybe_last_step:
                    last_step = maybe_last_step
                    await self.clean_up_task(
                        task=task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return last_step, detailed_output, None
                elif maybe_next_step:
                    next_step = maybe_next_step
                    retry = False
                else:
                    LOG.error(
                        "Step completed but task is not completed and next step is not created.",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        is_task_completed=is_task_completed,
                        maybe_last_step=maybe_last_step,
                        maybe_next_step=maybe_next_step,
                    )
            else:
                LOG.error(
                    "Unexpected step status after agent_step",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    step_status=step.status,
                )

            cua_response_param = detailed_output.cua_response if detailed_output else None
            if not cua_response_param and cua_response:
                cua_response_param = cua_response

            if retry and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    browser_session_id=browser_session_id,
                    task_block=task_block,
                    complete_verification=complete_verification,
                    engine=engine,
                    cua_response=cua_response_param,
                    llm_caller=llm_caller,
                )
            elif settings.execute_all_steps() and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    browser_session_id=browser_session_id,
                    task_block=task_block,
                    complete_verification=complete_verification,
                    engine=engine,
                    cua_response=cua_response_param,
                    llm_caller=llm_caller,
                )
            else:
                LOG.info(
                    "Step executed but continuous execution is disabled.",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    is_cloud_env=settings.is_cloud_environment(),
                    execute_all_steps=settings.execute_all_steps(),
                    next_step_id=next_step.step_id if next_step else None,
                )

            return step, detailed_output, next_step
        # TODO (kerem): Let's add other exceptions that we know about here as custom exceptions as well
        except StepUnableToExecuteError:
            LOG.error(
                "Step cannot be executed. Task execution stopped",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            raise
        except TaskAlreadyTimeout:
            LOG.warning(
                "Task is timed out, stopping execution",
                task_id=task.task_id,
                step=step.step_id,
            )
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                close_browser_on_completion=browser_session_id is None,
                browser_session_id=browser_session_id,
            )
            return step, detailed_output, None
        except StepTerminationError as e:
            LOG.warning(
                "Step cannot be executed, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )
            is_task_marked_as_failed = await self.fail_task(task, step, e.message)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning(
                    "Task isn't marked as failed, after step termination. NOT clean up the task",
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
            return step, detailed_output, None
        except FailedToSendWebhook:
            LOG.exception(
                "Failed to send webhook",
                task_id=task.task_id,
                step_id=step.step_id,
                task=task,
                step=step,
            )
            return step, detailed_output, next_step
        except FailedToNavigateToUrl as e:
            # Fail the task if we can't navigate to the URL and send the response
            LOG.error(
                "Failed to navigate to URL, marking task as failed, and sending webhook response",
                task_id=task.task_id,
                step_id=step.step_id,
                url=e.url,
                error_message=e.error_message,
            )
            failure_reason = f"Failed to navigate to URL. URL:{e.url}, Error:{e.error_message}"
            is_task_marked_as_failed = await self.fail_task(task, step, failure_reason)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                    need_final_screenshot=False,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning(
                    "Task isn't marked as failed, after navigation failure. NOT clean up the task",
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
            return step, detailed_output, next_step
        except TaskAlreadyCanceled:
            LOG.info(
                "Task is already canceled, stopping execution",
                task_id=task.task_id,
            )
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                need_call_webhook=False,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
            )
            return step, detailed_output, None
        except InvalidTaskStatusTransition:
            LOG.warning(
                "Invalid task status transition",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            # TODO: shall we send task response here?
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                need_call_webhook=False,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
            )
            return step, detailed_output, None
        except (UnsupportedActionType, UnsupportedTaskType, FailedToParseActionInstruction) as e:
            LOG.warning(
                "unsupported task type or action type, marking the task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            await self.fail_task(task, step, e.message)
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                need_call_webhook=False,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
            )
            return step, detailed_output, None
        except ScrapingFailed as sfe:
            LOG.warning(
                "Scraping failed, marking the task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

            await self.fail_task(
                task,
                step,
                sfe.reason
                or "Skyvern failed to load the website. This usually happens when the website is not properly designed, and crashes the browser as a result.",
            )
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                browser_session_id=browser_session_id,
            )
            return step, detailed_output, None
        except Exception as e:
            LOG.exception(
                "Got an unexpected exception in step, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
            )

            failure_reason = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            is_task_marked_as_failed = await self.fail_task(task, step, failure_reason)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning(
                    "Task isn't marked as failed, after unexpected exception. NOT clean up the task",
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
            return step, detailed_output, None

    async def fail_task(self, task: Task, step: Step | None, reason: str | None) -> bool:
        try:
            if step is not None:
                await self.update_step(
                    step=step,
                    status=StepStatus.failed,
                )

            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=reason,
            )
            return True
        except TaskAlreadyCanceled:
            LOG.info(
                "Task is already canceled. Can't fail the task.",
                task_id=task.task_id,
                step_id=step.step_id if step else "",
            )
            return False
        except InvalidTaskStatusTransition:
            LOG.warning(
                "Invalid task status transition while failing a task",
                task_id=task.task_id,
                step_id=step.step_id if step else "",
            )
            return False
        except Exception:
            LOG.exception(
                "Failed to update status and failure reason in database. Task might going to be time_out",
                task_id=task.task_id,
                step_id=step.step_id if step else "",
                reason=reason,
            )
            return True

    @TraceManager.traced_async(
        ignore_inputs=["browser_state", "organization", "task_block", "cua_response", "llm_caller"]
    )
    async def agent_step(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        engine: RunEngine = RunEngine.skyvern_v1,
        organization: Organization | None = None,
        task_block: BaseTaskBlock | None = None,
        complete_verification: bool = True,
        cua_response: OpenAIResponse | None = None,
        llm_caller: LLMCaller | None = None,
    ) -> tuple[Step, DetailedAgentStepOutput]:
        detailed_agent_step_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )
        try:
            LOG.info(
                "Starting agent step",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            step = await self.update_step(step=step, status=StepStatus.running)
            await app.AGENT_FUNCTION.prepare_step_execution(
                organization=organization, task=task, step=step, browser_state=browser_state
            )

            (
                scraped_page,
                extract_action_prompt,
            ) = await self.build_and_record_step_prompt(
                task,
                step,
                browser_state,
                engine,
            )
            detailed_agent_step_output.scraped_page = scraped_page
            detailed_agent_step_output.extract_action_prompt = extract_action_prompt
            json_response = None
            actions: list[Action]

            if engine == RunEngine.openai_cua:
                actions, new_cua_response = await self._generate_cua_actions(
                    task=task,
                    step=step,
                    scraped_page=scraped_page,
                    previous_response=cua_response,
                    engine=engine,
                )
                detailed_agent_step_output.cua_response = new_cua_response
            elif engine == RunEngine.anthropic_cua:
                assert llm_caller is not None
                actions = await self._generate_anthropic_actions(
                    task=task,
                    step=step,
                    scraped_page=scraped_page,
                    llm_caller=llm_caller,
                )
            elif engine == RunEngine.ui_tars and not app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "DISABLE_UI_TARS_CUA",
                task.workflow_run_id or task.task_id,
                properties={"organization_id": task.organization_id},
            ):
                assert llm_caller is not None
                actions = await self._generate_ui_tars_actions(
                    task=task,
                    step=step,
                    scraped_page=scraped_page,
                    llm_caller=llm_caller,
                )

            else:
                using_cached_action_plan = False
                if not task.navigation_goal and not isinstance(task_block, ValidationBlock):
                    actions = [await self.create_extract_action(task, step, scraped_page)]
                elif (
                    task_block
                    and task_block.cache_actions
                    and (actions := await retrieve_action_plan(task, step, scraped_page))
                ):
                    using_cached_action_plan = True
                else:
                    llm_key_override = task.llm_key
                    # FIXME: Redundant engine check?
                    if engine in CUA_ENGINES:
                        self.async_operation_pool.run_operation(task.task_id, AgentPhase.llm)
                        llm_key_override = None

                    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                        llm_key_override, default=app.LLM_API_HANDLER
                    )
                    json_response = await llm_api_handler(
                        prompt=extract_action_prompt,
                        prompt_name="extract-actions",
                        step=step,
                        screenshots=scraped_page.screenshots,
                    )
                    try:
                        json_response = await self.handle_potential_verification_code(
                            task,
                            step,
                            scraped_page,
                            browser_state,
                            json_response,
                        )
                        detailed_agent_step_output.llm_response = json_response
                        actions = parse_actions(task, step.step_id, step.order, scraped_page, json_response["actions"])
                    except NoTOTPVerificationCodeFound:
                        actions = [
                            TerminateAction(
                                organization_id=task.organization_id,
                                workflow_run_id=task.workflow_run_id,
                                task_id=task.task_id,
                                step_id=step.step_id,
                                step_order=step.order,
                                action_order=0,
                                reasoning="No TOTP verification code found. Going to terminate.",
                                intention="No TOTP verification code found. Going to terminate.",
                            )
                        ]

            detailed_agent_step_output.actions = actions
            if len(actions) == 0:
                LOG.info(
                    "No actions to execute, marking step as failed",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    step_order=step.order,
                    step_retry=step.retry_index,
                )
                step = await self.update_step(
                    step=step,
                    status=StepStatus.failed,
                    output=detailed_agent_step_output.to_agent_step_output(),
                )
                return step, detailed_agent_step_output

            # Execute the actions
            LOG.info(
                "Executing actions",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                actions=actions,
            )
            action_results: list[ActionResult] = []
            detailed_agent_step_output.action_results = action_results
            # filter out wait action if there are other actions in the list
            # we do this because WAIT action is considered as a failure
            # which will block following actions if we don't remove it from the list
            # if the list only contains WAIT action, we will execute WAIT action(s)
            if len(actions) > 1:
                wait_actions_to_skip = [action for action in actions if action.action_type == ActionType.WAIT]
                wait_actions_len = len(wait_actions_to_skip)
                # if there are wait actions and there are other actions in the list, skip wait actions
                # if we are using cached action plan, we don't skip wait actions
                if wait_actions_len > 0 and wait_actions_len < len(actions) and not using_cached_action_plan:
                    actions = [action for action in actions if action.action_type != ActionType.WAIT]
                    LOG.info(
                        "Skipping wait actions",
                        wait_actions_to_skip=wait_actions_to_skip,
                        actions=actions,
                    )

            # initialize list of tuples and set actions as the first element of each tuple so that in the case
            # of an exception, we can still see all the actions
            detailed_agent_step_output.actions_and_results = [(action, []) for action in actions]

            # build a linked action chain by the action_idx
            action_linked_list: list[ActionLinkedNode] = []
            element_id_to_action_index: dict[str, int] = dict()
            for action_idx, action in enumerate(actions):
                node = ActionLinkedNode(action=action)
                action_linked_list.append(node)

                if not isinstance(action, WebAction):
                    continue

                previous_action_idx = element_id_to_action_index.get(action.element_id)
                if previous_action_idx is not None:
                    previous_node = action_linked_list[previous_action_idx]
                    previous_node.next = node

                element_id_to_action_index[action.element_id] = action_idx

            element_id_to_last_action: dict[str, int] = dict()
            for action_idx, action_node in enumerate(action_linked_list):
                context = skyvern_context.ensure_context()
                if context.refresh_working_page:
                    LOG.warning(
                        "Detected the signal to reload the page, going to reload and skip the rest of the actions",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                    )
                    await browser_state.reload_page()
                    context.refresh_working_page = False
                    action_result = ActionSuccess()
                    action_result.step_order = step.order
                    action_result.step_retry_number = step.retry_index
                    action = ReloadPageAction(
                        reasoning="Something wrong with the current page, reload to continue",
                        status=ActionStatus.completed,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=action_idx,
                    )
                    detailed_agent_step_output.actions_and_results[action_idx] = (action, [action_result])
                    await app.DATABASE.create_action(action=action)
                    await self.record_artifacts_after_action(task, step, browser_state, engine)
                    break

                action = action_node.action
                if isinstance(action, WebAction):
                    previous_action_idx = element_id_to_last_action.get(action.element_id)
                    if previous_action_idx is not None:
                        LOG.warning(
                            "Duplicate action element id.",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            step_order=step.order,
                            action=action,
                        )

                        previous_action, previous_result = detailed_agent_step_output.actions_and_results[
                            previous_action_idx
                        ]
                        if len(previous_result) > 0 and previous_result[-1].success:
                            LOG.info(
                                "Previous action succeeded, but we'll still continue.",
                                task_id=task.task_id,
                                step_id=step.step_id,
                                step_order=step.order,
                                previous_action=previous_action,
                                previous_result=previous_result,
                            )
                        else:
                            LOG.warning(
                                "Previous action failed, so handle the next action.",
                                task_id=task.task_id,
                                step_id=step.step_id,
                                step_order=step.order,
                                previous_action=previous_action,
                                previous_result=previous_result,
                            )

                    element_id_to_last_action[action.element_id] = action_idx

                if engine != RunEngine.openai_cua:
                    self.async_operation_pool.run_operation(task.task_id, AgentPhase.action)
                current_page = await browser_state.must_get_working_page()
                if isinstance(action, CompleteAction) and not complete_verification:
                    # Do not verify the complete action when complete_verification is False
                    # set verified to True will skip the completion verification
                    action.verified = True
                results = await ActionHandler.handle_action(scraped_page, task, step, current_page, action)
                detailed_agent_step_output.actions_and_results[action_idx] = (
                    action,
                    results,
                )
                # wait random time between actions to avoid detection
                await asyncio.sleep(random.uniform(0.5, 1.0))
                await self.record_artifacts_after_action(task, step, browser_state, engine)
                for result in results:
                    result.step_retry_number = step.retry_index
                    result.step_order = step.order
                step.output = detailed_agent_step_output.to_agent_step_output()
                action_results.extend(results)
                # Check the last result for this action. If that succeeded, assume the entire action is successful
                if results and results[-1].success:
                    LOG.info(
                        "Action succeeded",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                    if results[-1].skip_remaining_actions:
                        LOG.warning(
                            "Going to stop executing the remaining actions",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            step_order=step.order,
                            step_retry=step.retry_index,
                            action_idx=action_idx,
                            action=action,
                            action_result=results,
                        )
                        break

                elif results and isinstance(action, DecisiveAction):
                    LOG.warning(
                        "DecisiveAction failed, but not stopping execution and not retrying the step",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                elif results and not results[-1].success and not results[-1].stop_execution_on_failure:
                    LOG.warning(
                        "Action failed, but not stopping execution",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                else:
                    if action_node.next is not None:
                        LOG.warning(
                            "Action failed, but have duplicated element id in the action list. Continue excuting.",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            step_order=step.order,
                            step_retry=step.retry_index,
                            action_idx=action_idx,
                            action=action,
                            next_action=action_node.next.action,
                            action_result=results,
                        )
                        continue

                    LOG.warning(
                        "Action failed, marking step as failed",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                        actions_and_results=detailed_agent_step_output.actions_and_results,
                    )
                    # if the action failed, don't execute the rest of the actions, mark the step as failed, and retry
                    failed_step = await self.update_step(
                        step=step,
                        status=StepStatus.failed,
                        output=detailed_agent_step_output.to_agent_step_output(),
                    )
                    return failed_step, detailed_agent_step_output.get_clean_detailed_output()

            LOG.info(
                "Actions executed successfully, marking step as completed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                action_results=action_results,
            )

            # Check if Skyvern already returned a complete action, if so, don't run user goal check
            has_decisive_action = False
            if detailed_agent_step_output and detailed_agent_step_output.actions_and_results:
                for action, results in detailed_agent_step_output.actions_and_results:
                    if isinstance(action, DecisiveAction):
                        has_decisive_action = True
                        break

            task_completes_on_download = task_block and task_block.complete_on_download and task.workflow_run_id
            if (
                not has_decisive_action
                and not task_completes_on_download
                and not isinstance(task_block, ActionBlock)
                and complete_verification
                and (task.navigation_goal or task.complete_criterion)
            ):
                disable_user_goal_check = app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                    "DISABLE_USER_GOAL_CHECK",
                    task.task_id,
                    properties={"task_url": task.url, "organization_id": task.organization_id},
                )
                if not disable_user_goal_check:
                    working_page = await browser_state.must_get_working_page()
                    complete_action = await self.check_user_goal_complete(
                        page=working_page,
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                    )
                    if complete_action is not None:
                        LOG.info("User goal achieved, executing complete action")
                        complete_action.organization_id = task.organization_id
                        complete_action.workflow_run_id = task.workflow_run_id
                        complete_action.task_id = task.task_id
                        complete_action.step_id = step.step_id
                        complete_action.step_order = step.order
                        complete_action.action_order = len(detailed_agent_step_output.actions_and_results)
                        complete_results = await ActionHandler.handle_action(
                            scraped_page, task, step, working_page, complete_action
                        )
                        detailed_agent_step_output.actions_and_results.append((complete_action, complete_results))
                        await self.record_artifacts_after_action(task, step, browser_state, engine)

            # if the last action is complete and is successful, check if there's a data extraction goal
            # if task has navigation goal and extraction goal at the same time, handle ExtractAction before marking step as completed
            if (
                task.navigation_goal
                and task.data_extraction_goal
                and self.step_has_completed_goal(detailed_agent_step_output)
            ):
                working_page = await browser_state.must_get_working_page()
                # refresh task in case the extracted information is updated previously
                refreshed_task = await app.DATABASE.get_task(task.task_id, task.organization_id)
                assert refreshed_task is not None
                task = refreshed_task
                extract_action = await self.create_extract_action(task, step, scraped_page)
                extract_results = await ActionHandler.handle_action(
                    scraped_page, task, step, working_page, extract_action
                )
                detailed_agent_step_output.actions_and_results.append((extract_action, extract_results))

            # If no action errors return the agent state and output
            completed_step = await self.update_step(
                step=step,
                status=StepStatus.completed,
                output=detailed_agent_step_output.to_agent_step_output(),
            )
            return completed_step, detailed_agent_step_output.get_clean_detailed_output()
        except CancelledError:
            LOG.exception(
                "CancelledError in agent_step, marking step as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            detailed_agent_step_output.step_exception = "CancelledError"
            failed_step = await self.update_step(
                step=step,
                status=StepStatus.failed,
                output=detailed_agent_step_output.to_agent_step_output(),
            )
            return failed_step, detailed_agent_step_output.get_clean_detailed_output()
        except (
            UnsupportedActionType,
            UnsupportedTaskType,
            FailedToParseActionInstruction,
            ScrapingFailed,
        ):
            raise

        except Exception as e:
            LOG.exception(
                "Unexpected exception in agent_step, marking step as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            detailed_agent_step_output.step_exception = e.__class__.__name__
            failed_step = await self.update_step(
                step=step,
                status=StepStatus.failed,
                output=detailed_agent_step_output.to_agent_step_output(),
            )
            return failed_step, detailed_agent_step_output.get_clean_detailed_output()

    async def _generate_cua_actions(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        previous_response: OpenAIResponse | None = None,
        engine: RunEngine = RunEngine.openai_cua,
    ) -> tuple[list[Action], OpenAIResponse | None]:
        if not previous_response:
            # this is the first step
            first_response: OpenAIResponse = await app.OPENAI_CLIENT.responses.create(
                model="computer-use-preview",
                tools=[
                    {
                        "type": "computer_use_preview",
                        "display_width": settings.BROWSER_WIDTH,
                        "display_height": settings.BROWSER_HEIGHT,
                        "environment": "browser",
                    }
                ],
                input=[
                    {
                        "role": "user",
                        "content": task.navigation_goal,
                    }
                ],
                reasoning={
                    "generate_summary": "concise",
                },
                truncation="auto",
                temperature=0,
            )
            previous_response = first_response
            input_tokens = first_response.usage.input_tokens or 0
            output_tokens = first_response.usage.output_tokens or 0
            first_response.usage.total_tokens or 0
            cached_tokens = first_response.usage.input_tokens_details.cached_tokens or 0
            reasoning_tokens = first_response.usage.output_tokens_details.reasoning_tokens or 0
            llm_cost = (3.0 / 1000000) * input_tokens + (12.0 / 1000000) * output_tokens
            await app.DATABASE.update_step(
                task_id=task.task_id,
                step_id=step.step_id,
                organization_id=task.organization_id,
                incremental_cost=llm_cost,
                incremental_input_tokens=input_tokens if input_tokens > 0 else None,
                incremental_output_tokens=output_tokens if output_tokens > 0 else None,
                incremental_reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
                incremental_cached_tokens=cached_tokens if cached_tokens > 0 else None,
            )
        if not scraped_page.screenshots:
            return [], previous_response

        computer_calls = [item for item in previous_response.output if item.type == "computer_call"]
        reasonings = [item for item in previous_response.output if item.type == "reasoning"]
        assistant_messages = [
            item for item in previous_response.output if item.type == "message" and item.role == "assistant"
        ]
        last_call_id = None
        if computer_calls:
            last_call_id = computer_calls[-1].call_id

        screenshot_base64 = base64.b64encode(scraped_page.screenshots[0]).decode("utf-8")
        if last_call_id is None:
            current_context = skyvern_context.ensure_context()
            resp_content = None
            if task.task_id in current_context.totp_codes:
                verification_code = current_context.totp_codes[task.task_id]
                current_context.totp_codes.pop(task.task_id)
                LOG.info(
                    "Using verification code from context",
                    task_id=task.task_id,
                    verification_code=verification_code,
                )
                resp_content = f"Here is the verification code: {verification_code}"
            else:
                # try address the conversation with the context we have
                reasoning = reasonings[0].summary[0].text if reasonings and reasonings[0].summary else None
                assistant_message = assistant_messages[0].content[0].text if assistant_messages else None
                skyvern_repsonse_prompt = load_prompt_with_elements(
                    element_tree_builder=scraped_page,
                    prompt_engine=prompt_engine,
                    template_name="cua-answer-question",
                    navigation_goal=task.navigation_goal,
                    assistant_reasoning=reasoning,
                    assistant_message=assistant_message,
                )
                skyvern_response = await app.LLM_API_HANDLER(
                    prompt=skyvern_repsonse_prompt,
                    prompt_name="cua-answer-question",
                    step=step,
                    screenshots=scraped_page.screenshots,
                )
                LOG.info("Skyvern response to CUA question", skyvern_response=skyvern_response)
                resp_content = skyvern_response.get("answer")
                if not resp_content:
                    resp_content = "I don't know. Can you help me make the best decision to achieve the goal?"
            current_response = await app.OPENAI_CLIENT.responses.create(
                model="computer-use-preview",
                previous_response_id=previous_response.id,
                tools=[
                    {
                        "type": "computer_use_preview",
                        "display_width": settings.BROWSER_WIDTH,
                        "display_height": settings.BROWSER_HEIGHT,
                        "environment": "browser",
                    }
                ],
                input=[
                    {"role": "user", "content": resp_content},
                ],
                reasoning={"generate_summary": "concise"},
                truncation="auto",
                temperature=0,
            )
        else:
            last_computer_call = computer_calls[-1]
            computer_call_input = {
                "call_id": last_call_id,
                "type": "computer_call_output",
                "output": {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{screenshot_base64}",
                },
            }
            if last_computer_call.pending_safety_checks:
                pending_checks = [check.model_dump() for check in last_computer_call.pending_safety_checks]
                computer_call_input["acknowledged_safety_checks"] = pending_checks

            current_response = await app.OPENAI_CLIENT.responses.create(
                model="computer-use-preview",
                previous_response_id=previous_response.id,
                tools=[
                    {
                        "type": "computer_use_preview",
                        "display_width": settings.BROWSER_WIDTH,
                        "display_height": settings.BROWSER_HEIGHT,
                        "environment": "browser",
                    }
                ],
                input=[computer_call_input],
                reasoning={
                    "generate_summary": "concise",
                },
                truncation="auto",
                temperature=0,
            )
        input_tokens = current_response.usage.input_tokens or 0
        output_tokens = current_response.usage.output_tokens or 0
        current_response.usage.total_tokens or 0
        cached_tokens = current_response.usage.input_tokens_details.cached_tokens or 0
        reasoning_tokens = current_response.usage.output_tokens_details.reasoning_tokens or 0
        llm_cost = (3.0 / 1000000) * input_tokens + (12.0 / 1000000) * output_tokens
        await app.DATABASE.update_step(
            task_id=task.task_id,
            step_id=step.step_id,
            organization_id=task.organization_id,
            incremental_cost=llm_cost,
            incremental_input_tokens=input_tokens if input_tokens > 0 else None,
            incremental_output_tokens=output_tokens if output_tokens > 0 else None,
            incremental_reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
            incremental_cached_tokens=cached_tokens if cached_tokens > 0 else None,
        )

        return await parse_cua_actions(task, step, current_response), current_response

    async def _generate_anthropic_actions(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        llm_caller: LLMCaller,
    ) -> list[Action]:
        LOG.info(
            "Anthropic CU call starts",
            tool_results=llm_caller.current_tool_results,
            message_length=len(llm_caller.message_history),
        )
        if llm_caller.current_tool_results:
            llm_caller.message_history.append({"role": "user", "content": llm_caller.current_tool_results})
            llm_caller.clear_tool_results()
            LOG.info(
                "Anthropic CU call - appended tool result message to message history and cleared cached tool results",
                message=llm_caller.current_tool_results,
                message_length=len(llm_caller.message_history),
            )
        tools = [
            {
                "type": "computer_20250124",
                "name": "computer",
                "display_height_px": settings.BROWSER_HEIGHT,
                "display_width_px": settings.BROWSER_WIDTH,
            }
        ]
        thinking = {"type": "enabled", "budget_tokens": 1024}
        betas = ["computer-use-2025-01-24"]
        window_dimension = cast(Resolution, scraped_page.window_dimension) if scraped_page.window_dimension else None
        if not llm_caller.message_history:
            llm_response = await llm_caller.call(
                prompt=task.navigation_goal,
                step=step,
                screenshots=scraped_page.screenshots,
                use_message_history=True,
                tools=tools,
                raw_response=True,
                betas=betas,
                thinking=thinking,
                window_dimension=window_dimension,
            )
        else:
            current_context = skyvern_context.ensure_context()
            resp_content = None
            if task.task_id in current_context.totp_codes:
                verification_code = current_context.totp_codes[task.task_id]
                current_context.totp_codes.pop(task.task_id)
                LOG.info(
                    "Using verification code from context for anthropic CU call",
                    task_id=task.task_id,
                    verification_code=verification_code,
                )
                resp_content = f"Here is the verification code: {verification_code}"

            llm_response = await llm_caller.call(
                prompt=resp_content,
                step=step,
                screenshots=scraped_page.screenshots,
                use_message_history=True,
                tools=tools,
                raw_response=True,
                betas=betas,
                thinking=thinking,
                window_dimension=window_dimension,
            )
        assistant_content = llm_response["content"]
        llm_caller.message_history.append({"role": "assistant", "content": assistant_content})

        actions = await parse_anthropic_actions(
            task,
            step,
            assistant_content,
            window_dimension or llm_caller.browser_window_dimension,
            llm_caller.get_screenshot_resize_target_dimension(window_dimension),
        )
        return actions

    async def _generate_ui_tars_actions(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        llm_caller: LLMCaller,
    ) -> list[Action]:
        """Generate actions using UI-TARS (Seed1.5-VL) model through the LLMCaller pattern."""

        LOG.info(
            "UI-TARS action generation starts",
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
        )

        # Ensure we have a UITarsLLMCaller instance
        if not isinstance(llm_caller, UITarsLLMCaller):
            raise ValueError(f"Expected UITarsLLMCaller, got {type(llm_caller)}")

        # Add the current screenshot to conversation
        if scraped_page.screenshots:
            llm_caller.add_screenshot(scraped_page.screenshots[0])
        else:
            LOG.error("No screenshots found, skipping UI-TARS action generation")
            raise ValueError("No screenshots found, skipping UI-TARS action generation")

        # Generate response using the LLMCaller
        response_content = await llm_caller.generate_ui_tars_response(step)

        LOG.info(f"UI-TARS raw response: {response_content}")

        window_dimension = (
            cast(Resolution, scraped_page.window_dimension)
            if scraped_page.window_dimension
            else Resolution(width=1920, height=1080)
        )
        LOG.info(f"UI-TARS browser window dimension: {window_dimension}")

        actions = await parse_ui_tars_actions(task, step, response_content, window_dimension)

        LOG.info(
            "UI-TARS action generation completed",
            task_id=task.task_id,
            step_id=step.step_id,
            actions_count=len(actions),
        )

        return actions

    async def complete_verify(
        self, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
    ) -> CompleteVerifyResult:
        LOG.info(
            "Checking if user goal is achieved after re-scraping the page",
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        scroll = True
        llm_key_override = task.llm_key
        if await is_cua_task(task=task):
            scroll = False
            llm_key_override = None

        scraped_page_refreshed = await scraped_page.refresh(draw_boxes=False, scroll=scroll)

        actions_and_results_str = ""
        if task.include_action_history_in_verification:
            actions_and_results_str = await self._get_action_results(task, current_step=step)

        verification_prompt = load_prompt_with_elements(
            element_tree_builder=scraped_page_refreshed,
            prompt_engine=prompt_engine,
            template_name="check-user-goal",
            navigation_goal=task.navigation_goal,
            navigation_payload=task.navigation_payload,
            complete_criterion=task.complete_criterion,
            action_history=actions_and_results_str,
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        )

        # this prompt is critical to our agent so let's use the primary LLM API handler

        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            llm_key_override, default=app.LLM_API_HANDLER
        )
        verification_result = await llm_api_handler(
            prompt=verification_prompt,
            step=step,
            screenshots=scraped_page_refreshed.screenshots,
            prompt_name="check-user-goal",
        )
        return CompleteVerifyResult.model_validate(verification_result)

    async def check_user_goal_complete(
        self, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
    ) -> CompleteAction | None:
        try:
            verification_result = await self.complete_verify(
                page=page,
                scraped_page=scraped_page,
                task=task,
                step=step,
            )

            # We don't want to return a complete action if the user goal is not achieved since we're checking at every step
            if not verification_result.user_goal_achieved:
                return None

            return CompleteAction(
                reasoning=verification_result.thoughts,
                data_extraction_goal=task.data_extraction_goal,
                verified=True,
            )

        except Exception:
            LOG.exception(
                "Failed to check user goal complete, skipping",
                task_id=task.task_id,
                step_id=step.step_id,
                workflow_run_id=task.workflow_run_id,
            )
            return None

    async def record_artifacts_after_action(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        engine: RunEngine,
    ) -> None:
        working_page = await browser_state.get_working_page()
        if not working_page:
            raise BrowserStateMissingPage()

        context = skyvern_context.ensure_context()
        scrolling_number = context.max_screenshot_scrolls
        if scrolling_number is None:
            scrolling_number = DEFAULT_MAX_SCREENSHOT_SCROLLS

        if engine in CUA_ENGINES:
            scrolling_number = 0

        try:
            screenshot = await browser_state.take_post_action_screenshot(
                scrolling_number=scrolling_number,
                use_playwright_fullpage=app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                    "ENABLE_PLAYWRIGHT_FULLPAGE",
                    task.workflow_run_id or task.task_id,
                    properties={"organization_id": task.organization_id},
                ),
            )
            await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                data=screenshot,
            )
        except Exception:
            LOG.error(
                "Failed to record screenshot after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

        try:
            skyvern_frame = await SkyvernFrame.create_instance(frame=working_page)
            html = await skyvern_frame.get_content()
            await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.HTML_ACTION,
                data=html.encode(),
            )
        except Exception:
            LOG.error(
                "Failed to record html after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

        try:
            video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
                task_id=task.task_id, browser_state=browser_state
            )
            for video_artifact in video_artifacts:
                await app.ARTIFACT_MANAGER.update_artifact_data(
                    artifact_id=video_artifact.video_artifact_id,
                    organization_id=task.organization_id,
                    data=video_artifact.video_data,
                )
        except Exception:
            LOG.error(
                "Failed to record video after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

    async def initialize_execution_state(
        self,
        task: Task,
        step: Step,
        workflow_run: WorkflowRun | None = None,
        browser_session_id: str | None = None,
    ) -> tuple[Step, BrowserState, DetailedAgentStepOutput]:
        if workflow_run:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url=task.url,
                browser_session_id=browser_session_id,
            )
        else:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_task(
                task=task,
                browser_session_id=browser_session_id,
            )
        # Initialize video artifact for the task here, afterwards it'll only get updated
        if browser_state and browser_state.browser_artifacts:
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

        detailed_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            step_exception=None,
        )
        return step, browser_state, detailed_output

    async def _scrape_with_type(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        scrape_type: ScrapeType,
        engine: RunEngine,
    ) -> ScrapedPage:
        if scrape_type == ScrapeType.NORMAL:
            pass

        elif scrape_type == ScrapeType.STOPLOADING:
            LOG.info(
                "Try to stop loading the page before scraping",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await browser_state.stop_page_loading()
        elif scrape_type == ScrapeType.RELOAD:
            LOG.info(
                "Try to reload the page before scraping",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await browser_state.reload_page()

        max_screenshot_number = settings.MAX_NUM_SCREENSHOTS
        draw_boxes = True
        scroll = True
        if engine in CUA_ENGINES:
            max_screenshot_number = 1
            draw_boxes = False
            scroll = False
        return await scrape_website(
            browser_state,
            task.url,
            app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step),
            scrape_exclude=app.scrape_exclude,
            max_screenshot_number=max_screenshot_number,
            draw_boxes=draw_boxes,
            scroll=scroll,
        )

    async def build_and_record_step_prompt(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        engine: RunEngine,
    ) -> tuple[ScrapedPage, str]:
        # start the async tasks while running scrape_website
        if engine not in CUA_ENGINES:
            self.async_operation_pool.run_operation(task.task_id, AgentPhase.scrape)

        # Scrape the web page and get the screenshot and the elements
        # HACK: try scrape_website three time to handle screenshot timeout
        # first time: normal scrape to take screenshot
        # second time: try again the normal scrape, (stopping window loading before scraping barely helps, but causing problem)
        # third time: reload the page before scraping
        scraped_page: ScrapedPage | None = None
        for idx, scrape_type in enumerate(SCRAPE_TYPE_ORDER):
            try:
                scraped_page = await self._scrape_with_type(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    scrape_type=scrape_type,
                    engine=engine,
                )
                break
            except (FailedToTakeScreenshot, ScrapingFailed) as e:
                if idx < len(SCRAPE_TYPE_ORDER) - 1:
                    continue
                LOG.error(
                    f"{e.__class__.__name__} happened in two normal attempts and reload-page retry",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    exc_info=True,
                )
                raise ScrapingFailed()

        if scraped_page is None:
            raise EmptyScrapePage()

        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.HTML_SCRAPE,
            data=scraped_page.html.encode(),
        )
        LOG.info(
            "Scraped website",
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            step_retry=step.retry_index,
            num_elements=len(scraped_page.elements),
            url=task.url,
        )
        # TODO: we only use HTML element for now, introduce a way to switch in the future
        element_tree_format = ElementTreeFormat.HTML
        element_tree_in_prompt: str = scraped_page.build_element_tree(element_tree_format)
        extract_action_prompt = ""
        if engine not in CUA_ENGINES:
            extract_action_prompt = await self._build_extract_action_prompt(
                task,
                step,
                browser_state,
                scraped_page,
                verification_code_check=bool(task.totp_verification_url or task.totp_identifier),
                expire_verification_code=True,
            )

        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP,
            data=json.dumps(scraped_page.id_to_css_dict, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP,
            data=json.dumps(scraped_page.id_to_frame_dict, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE,
            data=json.dumps(scraped_page.element_tree, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED,
            data=json.dumps(scraped_page.element_tree_trimmed, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT,
            data=element_tree_in_prompt.encode(),
        )

        return scraped_page, extract_action_prompt

    async def _build_extract_action_prompt(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        scraped_page: ScrapedPage,
        verification_code_check: bool = False,
        expire_verification_code: bool = False,
    ) -> str:
        actions_and_results_str = await self._get_action_results(task)

        # Generate the extract action prompt
        navigation_goal = task.navigation_goal
        starting_url = task.url
        page = await browser_state.get_working_page()
        current_url = (
            await SkyvernFrame.evaluate(frame=page, expression="() => document.location.href") if page else starting_url
        )
        final_navigation_payload = self._build_navigation_payload(
            task, expire_verification_code=expire_verification_code
        )

        task_type = task.task_type if task.task_type else TaskType.general
        template = ""
        if task_type == TaskType.general:
            template = "extract-action"
        elif task_type == TaskType.validation:
            template = "decisive-criterion-validate"
        elif task_type == TaskType.action:
            prompt = prompt_engine.load_prompt(
                "infer-action-type", navigation_goal=navigation_goal, prompt_name="infer-action-type"
            )
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                task.llm_key, default=app.LLM_API_HANDLER
            )
            json_response = await llm_api_handler(prompt=prompt, step=step, prompt_name="infer-action-type")
            if json_response.get("error"):
                raise FailedToParseActionInstruction(
                    reason=json_response.get("thought"), error_type=json_response.get("error")
                )

            inferred_actions: list[dict[str, Any]] = json_response.get("inferred_actions", [])
            if not inferred_actions:
                raise FailedToParseActionInstruction(reason=json_response.get("thought"), error_type="EMPTY_ACTION")

            action_type: str = inferred_actions[0].get("action_type") or ""
            action_type = ActionType[action_type.upper()]

            if action_type == ActionType.CLICK:
                template = "single-click-action"
            elif action_type == ActionType.INPUT_TEXT:
                template = "single-input-action"
            elif action_type == ActionType.UPLOAD_FILE:
                template = "single-upload-action"
            elif action_type == ActionType.SELECT_OPTION:
                template = "single-select-action"
            else:
                raise UnsupportedActionType(action_type=action_type)

        if not template:
            raise UnsupportedTaskType(task_type=task_type)

        context = skyvern_context.ensure_context()
        return load_prompt_with_elements(
            element_tree_builder=scraped_page,
            prompt_engine=prompt_engine,
            template_name=template,
            navigation_goal=navigation_goal,
            navigation_payload_str=json.dumps(final_navigation_payload),
            starting_url=starting_url,
            current_url=current_url,
            data_extraction_goal=task.data_extraction_goal,
            action_history=actions_and_results_str,
            error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
            local_datetime=datetime.now(context.tz_info).isoformat(),
            verification_code_check=verification_code_check,
            complete_criterion=task.complete_criterion.strip() if task.complete_criterion else None,
            terminate_criterion=task.terminate_criterion.strip() if task.terminate_criterion else None,
        )

    def _build_navigation_payload(
        self,
        task: Task,
        expire_verification_code: bool = False,
    ) -> dict[str, Any] | list | str | None:
        final_navigation_payload = task.navigation_payload
        current_context = skyvern_context.ensure_context()
        verification_code = current_context.totp_codes.get(task.task_id)
        if (task.totp_verification_url or task.totp_identifier) and verification_code:
            if (
                isinstance(final_navigation_payload, dict)
                and SPECIAL_FIELD_VERIFICATION_CODE not in final_navigation_payload
            ):
                final_navigation_payload[SPECIAL_FIELD_VERIFICATION_CODE] = verification_code
            elif (
                isinstance(final_navigation_payload, str)
                and SPECIAL_FIELD_VERIFICATION_CODE not in final_navigation_payload
            ):
                final_navigation_payload = (
                    final_navigation_payload + "\n" + str({SPECIAL_FIELD_VERIFICATION_CODE: verification_code})
                )
            if expire_verification_code:
                current_context.totp_codes.pop(task.task_id)
        return final_navigation_payload

    async def _get_action_results(self, task: Task, current_step: Step | None = None) -> str:
        """
        Get the action results from the last app.SETTINGS.PROMPT_ACTION_HISTORY_WINDOW steps.
        If current_step is provided, the current executing step will be included in the action history.
        Default is excluding the current executing step from the action history.
        """

        # Get action results from the last app.SETTINGS.PROMPT_ACTION_HISTORY_WINDOW steps
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        # the last step is always the newly created one and it should be excluded from the history window
        window_steps = steps[-1 - settings.PROMPT_ACTION_HISTORY_WINDOW : -1]
        if current_step:
            window_steps.append(current_step)

        actions_and_results: list[tuple[Action, list[ActionResult]]] = []
        for window_step in window_steps:
            if window_step.output and window_step.output.actions_and_results:
                actions_and_results.extend(window_step.output.actions_and_results)

        # exclude successful action from history
        action_history = [
            {
                "action": action.model_dump(
                    exclude_none=True,
                    include={"action_type", "element_id", "status", "reasoning", "option", "download"},
                ),
                "results": [
                    result.model_dump(
                        exclude_none=True,
                        include={
                            "success",
                            "exception_type",
                            "exception_message",
                        },
                    )
                    for result in results
                ],
            }
            for action, results in actions_and_results
            if len(results) > 0
        ]
        return json.dumps(action_history)

    async def get_extracted_information_for_task(self, task: Task) -> dict[str, Any] | list | str | None:
        """
        Find the last successful ScrapeAction for the task and return the extracted information.
        """
        # TODO: make sure we can get extracted information with the ExtractAction change
        steps = await app.DATABASE.get_task_steps(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        for step in reversed(steps):
            if step.status != StepStatus.completed:
                continue
            if not step.output or not step.output.actions_and_results:
                continue
            for action, action_results in step.output.actions_and_results:
                if action.action_type != ActionType.EXTRACT:
                    continue

                for action_result in action_results:
                    if action_result.success:
                        LOG.info(
                            "Extracted information for task",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            extracted_information=action_result.data,
                        )
                        return action_result.data

        if task.data_extraction_goal:
            LOG.warning(
                "Failed to find extracted information for task",
                task_id=task.task_id,
            )
        return None

    async def get_failure_reason_for_task(self, task: Task) -> str | None:
        """
        Find the TerminateAction for the task and return the reasoning.
        # TODO (kerem): Also return meaningful exceptions when we add them [WYV-311]
        """
        steps = await app.DATABASE.get_task_steps(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        for step in reversed(steps):
            if step.status != StepStatus.completed:
                continue
            if not step.output:
                continue

            if step.output.actions_and_results:
                for action, action_results in step.output.actions_and_results:
                    if action.action_type == ActionType.TERMINATE:
                        return action.reasoning

        LOG.error(
            "Failed to find failure reasoning for task",
            task_id=task.task_id,
        )
        return None

    async def clean_up_task(
        self,
        task: Task,
        last_step: Step,
        api_key: str | None = None,
        need_call_webhook: bool = True,
        close_browser_on_completion: bool = True,
        need_final_screenshot: bool = True,
        browser_session_id: str | None = None,
    ) -> None:
        """
        send the task response to the webhook callback url
        """
        # refresh the task from the db to get the latest status
        try:
            refreshed_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
            if not refreshed_task:
                LOG.error("Failed to get task from db when clean up task", task_id=task.task_id)
                raise TaskNotFound(task_id=task.task_id)
        except Exception as e:
            LOG.exception(
                "Failed to get task from db when clean up task",
                task_id=task.task_id,
            )
            raise TaskNotFound(task_id=task.task_id) from e
        task = refreshed_task

        # log the task status as an event
        analytics.capture("skyvern-oss-agent-task-status", {"status": task.status})
        if need_final_screenshot:
            # Take one last screenshot and create an artifact before closing the browser to see the final state
            # We don't need the artifacts and send the webhook response directly only when there is an issue with the browser
            # initialization. In this case, we don't have any artifacts to send and we can't take final screenshots etc.
            # since the browser is not initialized properly or the proxy is not working.

            browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id)
            if browser_state is not None and await browser_state.get_working_page() is not None:
                try:
                    screenshot = await browser_state.take_fullpage_screenshot(
                        use_playwright_fullpage=app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                            "ENABLE_PLAYWRIGHT_FULLPAGE",
                            task.workflow_run_id or task.task_id,
                            properties={"organization_id": task.organization_id},
                        )
                    )
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=last_step,
                        artifact_type=ArtifactType.SCREENSHOT_FINAL,
                        data=screenshot,
                    )
                except TargetClosedError:
                    LOG.warning(
                        "Failed to take screenshot before sending task response, page is closed",
                        task_id=task.task_id,
                        step_id=last_step.step_id,
                    )
                except Exception:
                    LOG.exception(
                        "Failed to take screenshot before sending task response",
                        task_id=task.task_id,
                        step_id=last_step.step_id,
                    )

        if task.organization_id:
            try:
                async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                    await app.STORAGE.save_downloaded_files(
                        task.organization_id, task_id=task.task_id, workflow_run_id=task.workflow_run_id
                    )
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout to save downloaded files",
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to save downloaded files",
                    exc_info=True,
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                )

        # if it's a task block from workflow run,
        # we don't need to close the browser, save browser artifacts, or call webhook
        if task.workflow_run_id:
            LOG.info(
                "Task is part of a workflow run, not sending a webhook response",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
            )
            return

        await self.async_operation_pool.remove_task(task.task_id)

        await self.cleanup_browser_and_create_artifacts(
            close_browser_on_completion, last_step, task, browser_session_id=browser_session_id
        )

        # Wait for all tasks to complete before generating the links for the artifacts
        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([task.task_id])

        if need_call_webhook:
            await self.execute_task_webhook(task=task, last_step=last_step, api_key=api_key)

    async def execute_task_webhook(
        self,
        task: Task,
        last_step: Step | None,
        api_key: str | None,
    ) -> None:
        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending task response",
                task_id=task.task_id,
            )
            return

        if not task.webhook_callback_url:
            LOG.warning(
                "Task has no webhook callback url. Not sending task response",
                task_id=task.task_id,
            )
            return

        task_response = await self.build_task_response(task=task, last_step=last_step)
        # try to build the new TaskRunResponse for backward compatibility
        task_run_response_json: str | None = None
        try:
            run_response = await run_service.get_run_response(
                run_id=task.task_id,
                organization_id=task.organization_id,
            )
            if run_response is not None:
                task_run_response_json = run_response.model_dump_json(exclude={"run_request"})

            # send task_response to the webhook callback url
            payload_json = task_response.model_dump_json(exclude={"request"})
            payload_dict = json.loads(payload_json)
            if task_run_response_json:
                payload_dict.update(json.loads(task_run_response_json))
            payload = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
            headers = generate_skyvern_webhook_headers(payload=payload, api_key=api_key)
            LOG.info(
                "Sending task response to webhook callback url",
                task_id=task.task_id,
                webhook_callback_url=task.webhook_callback_url,
                payload=payload,
                headers=headers,
            )

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    task.webhook_callback_url, data=payload, headers=headers, timeout=httpx.Timeout(30.0)
                )
            if resp.status_code >= 200 and resp.status_code < 300:
                LOG.info(
                    "Webhook sent successfully",
                    task_id=task.task_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
            else:
                LOG.info(
                    "Webhook failed",
                    task_id=task.task_id,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
        except Exception as e:
            raise FailedToSendWebhook(task_id=task.task_id) from e

    async def build_task_response(
        self,
        task: Task,
        last_step: Step | None = None,
        failure_reason: str | None = None,
        need_browser_log: bool = False,
    ) -> TaskResponse:
        # no last step means the task didn't start, so we don't have any other artifacts
        if last_step is None:
            return task.to_task_response(
                failure_reason=failure_reason,
            )

        screenshot_url = None
        recording_url = None
        browser_console_log_url: str | None = None
        latest_action_screenshot_urls: list[str] | None = None
        downloaded_files: list[FileInfo] | None = None

        # get the artifact of the screenshot and get the screenshot_url
        screenshot_artifact = await app.DATABASE.get_artifact(
            task_id=task.task_id,
            step_id=last_step.step_id,
            artifact_type=ArtifactType.SCREENSHOT_FINAL,
            organization_id=task.organization_id,
        )
        if screenshot_artifact:
            screenshot_url = await app.ARTIFACT_MANAGER.get_share_link(screenshot_artifact)

        first_step = await app.DATABASE.get_first_step(task_id=task.task_id, organization_id=task.organization_id)
        if first_step:
            recording_artifact = await app.DATABASE.get_artifact(
                task_id=task.task_id,
                step_id=first_step.step_id,
                artifact_type=ArtifactType.RECORDING,
                organization_id=task.organization_id,
            )
            if recording_artifact:
                recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        # get the artifact of the last TASK_RESPONSE_ACTION_SCREENSHOT_COUNT screenshots and get the screenshot_url
        latest_action_screenshot_artifacts = await app.DATABASE.get_latest_n_artifacts(
            task_id=task.task_id,
            organization_id=task.organization_id,
            artifact_types=[ArtifactType.SCREENSHOT_ACTION],
            n=settings.TASK_RESPONSE_ACTION_SCREENSHOT_COUNT,
        )
        if latest_action_screenshot_artifacts:
            latest_action_screenshot_urls = await app.ARTIFACT_MANAGER.get_share_links(
                latest_action_screenshot_artifacts
            )

        if task.organization_id:
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    downloaded_files = await app.STORAGE.get_downloaded_files(
                        organization_id=task.organization_id, task_id=task.task_id, workflow_run_id=task.workflow_run_id
                    )
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout to get downloaded files",
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to get downloaded files",
                    exc_info=True,
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                )

        if need_browser_log:
            browser_console_log = await app.DATABASE.get_latest_artifact(
                task_id=task.task_id,
                artifact_types=[ArtifactType.BROWSER_CONSOLE_LOG],
                organization_id=task.organization_id,
            )
            if browser_console_log:
                browser_console_log_url = await app.ARTIFACT_MANAGER.get_share_link(browser_console_log)

        # get the latest task from the db to get the latest status, extracted_information, and failure_reason
        task_from_db = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
        if not task_from_db:
            LOG.error("Failed to get task from db when sending task response")
            raise TaskNotFound(task_id=task.task_id)

        task = task_from_db
        return task.to_task_response(
            action_screenshot_urls=latest_action_screenshot_urls,
            screenshot_url=screenshot_url,
            recording_url=recording_url,
            browser_console_log_url=browser_console_log_url,
            downloaded_files=downloaded_files,
            failure_reason=failure_reason,
        )

    async def cleanup_browser_and_create_artifacts(
        self,
        close_browser_on_completion: bool,
        last_step: Step,
        task: Task,
        browser_session_id: str | None = None,
    ) -> None:
        """
        Developer notes: we should not expect any exception to be raised here.
        This function should handle exceptions gracefully.
        If errors are raised and not caught inside this function, please catch and handle them.
        """
        # We need to close the browser even if there is no webhook callback url or api key
        browser_state = await app.BROWSER_MANAGER.cleanup_for_task(
            task.task_id,
            close_browser_on_completion,
            browser_session_id,
            task.organization_id,
        )
        if browser_state:
            # Update recording artifact after closing the browser, so we can get an accurate recording
            video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
                task_id=task.task_id, browser_state=browser_state
            )
            for video_artifact in video_artifacts:
                await app.ARTIFACT_MANAGER.update_artifact_data(
                    artifact_id=video_artifact.video_artifact_id,
                    organization_id=task.organization_id,
                    data=video_artifact.video_data,
                )

            har_data = await app.BROWSER_MANAGER.get_har_data(task_id=task.task_id, browser_state=browser_state)
            if har_data:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.HAR,
                    data=har_data,
                )

            browser_log = await app.BROWSER_MANAGER.get_browser_console_log(
                task_id=task.task_id, browser_state=browser_state
            )
            if browser_log:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.BROWSER_CONSOLE_LOG,
                    data=browser_log,
                )

            if browser_state.browser_context and browser_state.browser_artifacts.traces_dir:
                trace_path = f"{browser_state.browser_artifacts.traces_dir}/{task.task_id}.zip"
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.TRACE,
                    path=trace_path,
                )
        else:
            LOG.warning(
                "BrowserState is missing before sending response to webhook_callback_url",
                web_hook_url=task.webhook_callback_url,
            )

    async def update_step(
        self,
        step: Step,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        is_last: bool | None = None,
        retry_index: int | None = None,
    ) -> Step:
        step.validate_update(status, output, is_last)
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if output is not None:
            updates["output"] = output
        if is_last is not None:
            updates["is_last"] = is_last
        if retry_index is not None:
            updates["retry_index"] = retry_index
        update_comparison = {
            key: {"old": getattr(step, key), "new": value}
            for key, value in updates.items()
            if getattr(step, key) != value and key != "output"
        }
        LOG.debug(
            "Updating step in db",
            task_id=step.task_id,
            step_id=step.step_id,
            diff=update_comparison,
        )

        # Track step duration when step is completed or failed
        if status in [StepStatus.completed, StepStatus.failed]:
            duration_seconds = (datetime.now(UTC) - step.created_at.replace(tzinfo=UTC)).total_seconds()
            LOG.info(
                "Step duration metrics",
                task_id=step.task_id,
                step_id=step.step_id,
                duration_seconds=duration_seconds,
                step_status=status,
                organization_id=step.organization_id,
            )

        await save_step_logs(step.step_id)

        return await app.DATABASE.update_step(
            task_id=step.task_id,
            step_id=step.step_id,
            organization_id=step.organization_id,
            **updates,
        )

    async def update_task(
        self,
        task: Task,
        status: TaskStatus,
        extracted_information: dict[str, Any] | list | str | None = None,
        failure_reason: str | None = None,
    ) -> Task:
        # refresh task from db to get the latest status
        task_from_db = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
        if task_from_db:
            task = task_from_db

        task.validate_update(status, extracted_information, failure_reason)
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if extracted_information is not None:
            updates["extracted_information"] = extracted_information
        if failure_reason is not None:
            updates["failure_reason"] = failure_reason
        update_comparison = {
            key: {"old": getattr(task, key), "new": value}
            for key, value in updates.items()
            if getattr(task, key) != value
        }

        # Track task duration when task is completed, failed, or terminated
        if status in [TaskStatus.completed, TaskStatus.failed, TaskStatus.terminated]:
            duration_seconds = (datetime.now(UTC) - task.created_at.replace(tzinfo=UTC)).total_seconds()
            LOG.info(
                "Task duration metrics",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                duration_seconds=duration_seconds,
                task_status=status,
                organization_id=task.organization_id,
            )

        await save_task_logs(task.task_id)
        LOG.info("Updating task in db", task_id=task.task_id, diff=update_comparison)
        return await app.DATABASE.update_task(
            task.task_id,
            organization_id=task.organization_id,
            **updates,
        )

    async def handle_failed_step(self, organization: Organization, task: Task, step: Step) -> Step | None:
        max_retries_per_step = (
            organization.max_retries_per_step
            # we need to check by None because 0 is a valid value for max_retries_per_step
            if organization.max_retries_per_step is not None
            else settings.MAX_RETRIES_PER_STEP
        )
        if step.retry_index >= max_retries_per_step:
            LOG.warning(
                "Step failed after max retries, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                max_retries=settings.MAX_RETRIES_PER_STEP,
            )
            browser_state = app.BROWSER_MANAGER.get_for_task(task_id=task.task_id, workflow_run_id=task.workflow_run_id)
            page = None
            if browser_state is not None:
                page = await browser_state.get_working_page()

            failure_reason = await self.summary_failure_reason_for_max_retries(
                organization=organization,
                task=task,
                step=step,
                page=page,
                max_retries=max_retries_per_step,
            )

            await self.update_task(
                task,
                TaskStatus.failed,
                failure_reason=(
                    f"Max retries per step ({max_retries_per_step}) exceeded. Possible failure reasons: {failure_reason}"
                ),
            )
            return None
        else:
            LOG.warning(
                "Step failed, retrying",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            next_step = await app.DATABASE.create_step(
                task_id=task.task_id,
                organization_id=task.organization_id,
                order=step.order,
                retry_index=step.retry_index + 1,
            )
            return next_step

    async def summary_failure_reason_for_max_steps(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        page: Page | None,
    ) -> str:
        steps_results = []
        try:
            steps = await app.DATABASE.get_task_steps(
                task_id=task.task_id, organization_id=organization.organization_id
            )
            for step_cnt, step in enumerate(steps):
                if step.output is None:
                    continue

                if len(step.output.errors) > 0:
                    return ";".join([repr(err) for err in step.output.errors])

                if step.output.actions_and_results is None:
                    continue

                action_result_summary: list[str] = []
                step_result: dict[str, Any] = {
                    "order": step_cnt,
                }
                for action, action_results in step.output.actions_and_results:
                    if len(action_results) == 0:
                        continue
                    action_result_summary.append(
                        f"{action.reasoning}(action_type={action.action_type}, result={'success' if action_results[-1].success else 'failed'})"
                    )
                step_result["actions_result"] = action_result_summary
                steps_results.append(step_result)

            scroll = True
            if await is_cua_task(task=task):
                scroll = False

            screenshots: list[bytes] = []
            if page is not None:
                screenshots = await SkyvernFrame.take_split_screenshots(page=page, url=page.url, scroll=scroll)

            prompt = prompt_engine.load_prompt(
                "summarize-max-steps-reason",
                step_count=len(steps),
                navigation_goal=task.navigation_goal,
                navigation_payload=task.navigation_payload,
                steps=steps_results,
                local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            )
            json_response = await app.LLM_API_HANDLER(
                prompt=prompt, screenshots=screenshots, step=step, prompt_name="summarize-max-steps-reason"
            )
            return json_response.get("reasoning", "")
        except Exception:
            LOG.warning("Failed to summary the failure reason", task_id=task.task_id, step_id=step.step_id)
            if steps_results:
                last_step_result = steps_results[-1]
                return f"Step {last_step_result['order']}: {last_step_result['actions_result']}"
            return ""

    async def summary_failure_reason_for_max_retries(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        page: Page | None,
        max_retries: int,
    ) -> str:
        html = ""
        screenshots: list[bytes] = []
        steps_results = []
        try:
            steps = await app.DATABASE.get_task_steps(
                task_id=task.task_id, organization_id=organization.organization_id
            )
            for step_cnt, cur_step in enumerate(steps[-max_retries:]):
                if cur_step.output and cur_step.output.actions_and_results:
                    action_result_summary: list[str] = []
                    step_result: dict[str, Any] = {
                        "order": step_cnt,
                    }
                    for action, action_results in cur_step.output.actions_and_results:
                        if len(action_results) == 0:
                            continue
                        last_result = action_results[-1]
                        if last_result.success:
                            continue
                        reason = last_result.exception_message or ""
                        action_result_summary.append(
                            f"{action.reasoning}(action_type={action.action_type}, result=failed, reason={reason})"
                        )
                    step_result["actions_result"] = action_result_summary
                    steps_results.append(step_result)

            if page is not None:
                skyvern_frame = await SkyvernFrame.create_instance(frame=page)
                html = await skyvern_frame.get_content()
                screenshots = await SkyvernFrame.take_split_screenshots(page=page, url=page.url)

            prompt = prompt_engine.load_prompt(
                "summarize-max-retries-reason",
                navigation_goal=task.navigation_goal,
                navigation_payload=task.navigation_payload,
                steps=steps_results,
                page_html=html,
                max_retries=max_retries,
                local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            )
            json_response = await app.LLM_API_HANDLER(
                prompt=prompt,
                screenshots=screenshots,
                step=step,
                prompt_name="summarize-max-retries-reason",
            )
            return json_response.get("reasoning", "")
        except Exception:
            LOG.warning(
                "Failed to summarize the failure reason for max retries",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            if steps_results:
                last_step_result = steps_results[-1]
                return f"Retry Step {last_step_result['order']}: {last_step_result['actions_result']}"
            return ""

    async def handle_completed_step(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        page: Page | None,
        task_block: BaseTaskBlock | None = None,
    ) -> tuple[bool | None, Step | None, Step | None]:
        if step.is_goal_achieved():
            LOG.info(
                "Step completed and goal achieved, marking task as completed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            last_step = await self.update_step(step, is_last=True)
            extracted_information = await self.get_extracted_information_for_task(task)
            await self.update_task(
                task,
                status=TaskStatus.completed,
                extracted_information=extracted_information,
            )
            return True, last_step, None
        if step.is_terminated():
            LOG.info(
                "Step completed and terminated by the agent, marking task as terminated",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            last_step = await self.update_step(step, is_last=True)
            failure_reason = await self.get_failure_reason_for_task(task)
            await self.update_task(task, status=TaskStatus.terminated, failure_reason=failure_reason)
            return False, last_step, None
        # If the max steps are exceeded, mark the current step as the last step and conclude the task
        context = skyvern_context.current()
        override_max_steps_per_run = context.max_steps_override if context else None
        max_steps_per_run = (
            override_max_steps_per_run
            or task.max_steps_per_run
            or organization.max_steps_per_run
            or settings.MAX_STEPS_PER_RUN
        )

        # HACK: action block only have one step to execute without complete action, so we consider the task is completed as long as the step is completed
        if isinstance(task_block, ActionBlock) and step.is_success():
            LOG.info(
                "Step completed for the action block, marking task as completed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            last_step = await self.update_step(step, is_last=True)
            await self.update_task(
                task,
                status=TaskStatus.completed,
            )
            return True, last_step, None

        if step.order + 1 >= max_steps_per_run:
            LOG.info(
                "Step completed but max steps reached, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                max_steps=max_steps_per_run,
            )
            last_step = await self.update_step(step, is_last=True)

            failure_reason = await self.summary_failure_reason_for_max_steps(
                organization=organization,
                task=task,
                step=step,
                page=page,
            )
            failure_reason = (
                f"Reached the maximum steps ({max_steps_per_run}). Possible failure reasons: {failure_reason}"
            )

            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=failure_reason,
            )
            return False, last_step, None
        else:
            LOG.info(
                "Step completed, creating next step",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            next_step = await app.DATABASE.create_step(
                task_id=task.task_id,
                order=step.order + 1,
                retry_index=0,
                organization_id=task.organization_id,
            )

            if step.order == int(max_steps_per_run * settings.LONG_RUNNING_TASK_WARNING_RATIO - 1):
                LOG.info(
                    "Long running task warning",
                    order=step.order,
                    max_steps=max_steps_per_run,
                    warning_ratio=settings.LONG_RUNNING_TASK_WARNING_RATIO,
                )
            return None, None, next_step

    async def handle_potential_verification_code(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        browser_state: BrowserState,
        json_response: dict[str, Any],
    ) -> dict[str, Any]:
        place_to_enter_verification_code = json_response.get("place_to_enter_verification_code")
        should_enter_verification_code = json_response.get("should_enter_verification_code")
        if (
            place_to_enter_verification_code
            and should_enter_verification_code
            and (task.totp_verification_url or task.totp_identifier)
            and task.organization_id
        ):
            LOG.info("Need verification code", step_id=step.step_id)
            workflow_id = workflow_permanent_id = None
            if task.workflow_run_id:
                workflow_run = await app.DATABASE.get_workflow_run(task.workflow_run_id)
                if workflow_run:
                    workflow_id = workflow_run.workflow_id
                    workflow_permanent_id = workflow_run.workflow_permanent_id
            verification_code = await poll_verification_code(
                task.task_id,
                task.organization_id,
                workflow_id=workflow_id,
                workflow_run_id=task.workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                totp_verification_url=task.totp_verification_url,
                totp_identifier=task.totp_identifier,
            )
            current_context = skyvern_context.ensure_context()
            current_context.totp_codes[task.task_id] = verification_code

            extract_action_prompt = await self._build_extract_action_prompt(
                task,
                step,
                browser_state,
                scraped_page,
                verification_code_check=False,
                expire_verification_code=True,
            )
            llm_key_override = task.llm_key
            if await is_cua_task(task=task):
                llm_key_override = None
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                llm_key_override, default=app.LLM_API_HANDLER
            )
            return await llm_api_handler(
                prompt=extract_action_prompt,
                step=step,
                screenshots=scraped_page.screenshots,
                prompt_name="extract-actions",
            )
        return json_response

    @staticmethod
    async def get_task_errors(task: Task) -> list[UserDefinedError]:
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        errors = []
        for step in steps:
            if step.output and step.output.errors:
                errors.extend(step.output.errors)

        return errors

    @staticmethod
    async def update_task_errors_from_detailed_output(
        task: Task, detailed_step_output: DetailedAgentStepOutput
    ) -> Task:
        task_errors = task.errors
        step_errors = detailed_step_output.extract_errors() or []
        task_errors.extend([error.model_dump() for error in step_errors])

        return await app.DATABASE.update_task(
            task_id=task.task_id,
            organization_id=task.organization_id,
            errors=task_errors,
        )

    @staticmethod
    async def create_extract_action(task: Task, step: Step, scraped_page: ScrapedPage) -> ExtractAction:
        context = skyvern_context.ensure_context()
        # generate reasoning by prompt llm to think briefly what data to extract
        prompt = prompt_engine.load_prompt(
            "data-extraction-summary",
            data_extraction_goal=task.data_extraction_goal,
            data_extraction_schema=task.extracted_information_schema,
            current_url=scraped_page.url,
            local_datetime=datetime.now(context.tz_info).isoformat(),
        )

        data_extraction_summary_resp = await app.SECONDARY_LLM_API_HANDLER(
            prompt=prompt, step=step, prompt_name="data-extraction-summary"
        )
        return ExtractAction(
            reasoning=data_extraction_summary_resp.get("summary", "Extracting information from the page"),
            data_extraction_goal=task.data_extraction_goal,
            organization_id=task.organization_id,
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
            confidence_float=1.0,
        )

    @staticmethod
    def step_has_completed_goal(detailed_agent_step_output: DetailedAgentStepOutput) -> bool:
        if not detailed_agent_step_output.actions_and_results:
            return False

        last_action, last_action_results = detailed_agent_step_output.actions_and_results[-1]
        if last_action.action_type not in [ActionType.COMPLETE, ActionType.EXTRACT]:
            return False

        return any(action_result.success for action_result in last_action_results)
