import asyncio
import base64
import hashlib
import json
import os
import random
import re
import string
import uuid
from asyncio.exceptions import CancelledError
from dataclasses import dataclass
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
    BROWSER_DOWNLOAD_TIMEOUT,
    BROWSER_DOWNLOADING_SUFFIX,
    DEFAULT_MAX_SCREENSHOT_SCROLLS,
    GET_DOWNLOADED_FILES_TIMEOUT,
    SAVE_DOWNLOADED_FILES_TIMEOUT,
    SCRAPE_TYPE_ORDER,
    SPECIAL_FIELD_VERIFICATION_CODE,
    ScrapeType,
)
from skyvern.errors.errors import (
    GetTOTPVerificationCodeError,
    ReachMaxRetriesError,
    ReachMaxStepsError,
    TimeoutGetTOTPVerificationCodeError,
    UserDefinedError,
)
from skyvern.exceptions import (
    BrowserSessionNotFound,
    DownloadFileMaxWaitingTime,
    EmptyScrapePage,
    FailedToGetTOTPVerificationCode,
    FailedToNavigateToUrl,
    FailedToParseActionInstruction,
    FailedToSendWebhook,
    FailedToTakeScreenshot,
    InvalidTaskStatusTransition,
    InvalidWorkflowTaskURLState,
    MissingBrowserStatePage,
    MissingExtractActionsResponse,
    NoTOTPVerificationCodeFound,
    PDFEmbedBase64DecodeError,
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
from skyvern.forge.sdk.api.aws import aws_client
from skyvern.forge.sdk.api.files import (
    get_path_for_workflow_download_directory,
    list_downloading_files_in_directory,
    list_files_in_directory,
    rename_file,
    wait_for_download_finished,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory, LLMCaller, LLMCallerManager
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import LLM_PROVIDER_ERROR_RETRYABLE_TASK_TYPE, LLM_PROVIDER_ERROR_TYPE
from skyvern.forge.sdk.api.llm.ui_tars_llm_caller import UITarsLLMCaller
from skyvern.forge.sdk.api.llm.vertex_cache_manager import get_cache_manager
from skyvern.forge.sdk.artifact.manager import BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.log_artifacts import save_step_logs, save_task_logs
from skyvern.forge.sdk.models import SpeculativeLLMMetadata, Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.trace import TraceManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BaseTaskBlock,
    ValidationBlock,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from skyvern.schemas.runs import CUA_ENGINES, RunEngine
from skyvern.schemas.steps import AgentStepOutput
from skyvern.services import run_service, service_utils
from skyvern.services.action_service import get_action_history
from skyvern.services.otp_service import poll_otp_value
from skyvern.utils.image_resizer import Resolution
from skyvern.utils.prompt_engine import MaxStepsReasonResponse, load_prompt_with_elements
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    CompleteAction,
    CompleteVerifyResult,
    DecisiveAction,
    DownloadFileAction,
    ExtractAction,
    GotoUrlAction,
    ReloadPageAction,
    TerminateAction,
    WebAction,
)
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.parse_actions import (
    parse_actions,
    parse_anthropic_actions,
    parse_cua_actions,
    parse_ui_tars_actions,
)
from skyvern.webeye.actions.responses import ActionResult, ActionSuccess
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper.scraped_page import ElementTreeFormat, ScrapedPage
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

EXTRACT_ACTION_TEMPLATE = "extract-action"
EXTRACT_ACTION_PROMPT_NAME = "extract-actions"
EXTRACT_ACTION_CACHE_KEY_PREFIX = f"{EXTRACT_ACTION_TEMPLATE}-static"


@dataclass
class SpeculativePlan:
    scraped_page: ScrapedPage
    extract_action_prompt: str
    use_caching: bool
    llm_json_response: dict[str, Any] | None
    llm_metadata: SpeculativeLLMMetadata | None = None
    prompt_name: str = "extract-actions"


class ActionLinkedNode:
    def __init__(self, action: Action) -> None:
        self.action = action
        self.next: ActionLinkedNode | None = None


class ForgeAgent:
    def __init__(self) -> None:
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
            if browser_state is not None:
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
            else:
                LOG.info("No browser state found for workflow run, setting task url to empty string")
                task_url = ""

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
            browser_address=workflow_run.browser_address,
            browser_session_id=workflow_run.browser_session_id,
            download_timeout=task_block.download_timeout,
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
            browser_address=task_request.browser_address,
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
        # set the step_id and task_id in the context
        context = skyvern_context.ensure_context()
        context.step_id = step.step_id
        context.task_id = task.task_id

        # do not need to do complete verification when it's a CUA task
        # 1. CUA executes only one action step by step -- it's pretty less likely to have a hallucination for completion or forget to return a complete
        # 2. It will significantly slow down CUA tasks
        if engine in CUA_ENGINES:
            complete_verification = False

        close_browser_on_completion = (
            close_browser_on_completion and browser_session_id is None and not task.browser_address
        )

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
                close_browser_on_completion=close_browser_on_completion,
            )
            return step, None, None

        override_max_steps_per_run = context.max_steps_override or None
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
                    get_path_for_workflow_download_directory(
                        context.run_id if context and context.run_id else task.workflow_run_id
                    )
                )
            if task.browser_session_id:
                browser_session_downloaded_files = await app.STORAGE.list_downloaded_files_in_browser_session(
                    organization_id=organization.organization_id,
                    browser_session_id=task.browser_session_id,
                )
                list_files_before = list_files_before + browser_session_downloaded_files
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
                    await page.goto(task.url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                step = await self.update_step(
                    step, status=StepStatus.completed, is_last=True, output=AgentStepOutput(action_results=[])
                )
                task = await self.update_task(task, status=TaskStatus.completed)
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    need_call_webhook=True,
                    close_browser_on_completion=close_browser_on_completion,
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
            task = await self.update_task_errors_from_detailed_output(task, detailed_output)  # type: ignore
            retry = False

            if task_block and task_block.complete_on_download and task.workflow_run_id:
                workflow_download_directory = get_path_for_workflow_download_directory(
                    context.run_id if context and context.run_id else task.workflow_run_id
                )

                downloading_files = list_downloading_files_in_directory(workflow_download_directory)
                if task.browser_session_id:
                    browser_session_downloading_files = await app.STORAGE.list_downloading_files_in_browser_session(
                        organization_id=organization.organization_id,
                        browser_session_id=task.browser_session_id,
                    )
                    downloading_files = downloading_files + browser_session_downloading_files
                if len(downloading_files) > 0:
                    LOG.info(
                        "Detecting files are still downloading, waiting for files to be completely downloaded.",
                        downloading_files=downloading_files,
                    )
                    try:
                        await wait_for_download_finished(
                            downloading_files=downloading_files,
                            timeout=task_block.download_timeout or BROWSER_DOWNLOAD_TIMEOUT,
                        )
                    except DownloadFileMaxWaitingTime as e:
                        LOG.warning(
                            "There're several long-time downloading files, these files might be broken",
                            downloading_files=e.downloading_files,
                            workflow_run_id=task.workflow_run_id,
                        )

                list_files_after = list_files_in_directory(workflow_download_directory)
                if task.browser_session_id:
                    browser_session_downloaded_files_after = await app.STORAGE.list_downloaded_files_in_browser_session(
                        organization_id=organization.organization_id,
                        browser_session_id=task.browser_session_id,
                    )
                    list_files_after = list_files_after + browser_session_downloaded_files_after
                if len(list_files_after) > len(list_files_before):
                    files_to_rename = list(set(list_files_after) - set(list_files_before))
                    for file in files_to_rename:
                        if file.startswith("s3://"):
                            file_data = await aws_client.download_file(file, log_exception=False)
                            if not file_data:
                                continue
                            file = file.split("/")[-1]  # Extract filename from the end of S3 URI
                            with open(os.path.join(workflow_download_directory, file), "wb") as f:
                                f.write(file_data)

                        file_extension = Path(file).suffix
                        if file_extension == BROWSER_DOWNLOADING_SUFFIX:
                            LOG.warning(
                                "Detecting incompleted download file, skip the rename",
                                file=file,
                                task_id=task.task_id,
                                workflow_run_id=task.workflow_run_id,
                            )
                            continue

                        if task_block.download_suffix:
                            # Use download_suffix as the complete filename (without extension)
                            final_file_name = task_block.download_suffix
                        else:
                            # Fallback to random filename if no download_suffix provided
                            random_file_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                            final_file_name = f"download-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{random_file_id}"

                        # Check if file with this name already exists
                        final_file_name = final_file_name
                        target_path = os.path.join(workflow_download_directory, final_file_name + file_extension)
                        counter = 1
                        while os.path.exists(target_path):
                            # If file exists, append counter to filename
                            final_file_name = f"{final_file_name}_{counter}"
                            target_path = os.path.join(workflow_download_directory, final_file_name + file_extension)
                            counter += 1

                        rename_file(os.path.join(workflow_download_directory, file), final_file_name + file_extension)

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
                        close_browser_on_completion=close_browser_on_completion,
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
                        close_browser_on_completion=close_browser_on_completion,
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
                    browser_state=browser_state,
                    scraped_page=detailed_output.scraped_page if detailed_output else None,
                    engine=engine,
                    complete_verification=complete_verification,
                )
                if is_task_completed is not None and maybe_last_step:
                    last_step = maybe_last_step
                    await self.clean_up_task(
                        task=task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
                        browser_session_id=browser_session_id,
                    )
                    return last_step, detailed_output, None
                elif maybe_next_step:
                    next_step = maybe_next_step
                    retry = False
                else:
                    LOG.error(
                        "Step completed but task is not completed and next step is not created.",
                        is_task_completed=is_task_completed,
                        maybe_last_step=maybe_last_step,
                        maybe_next_step=maybe_next_step,
                    )
            else:
                LOG.error(
                    "Unexpected step status after agent_step",
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
                    is_cloud_env=settings.is_cloud_environment(),
                    execute_all_steps=settings.execute_all_steps(),
                    next_step_id=next_step.step_id if next_step else None,
                )

            return step, detailed_output, next_step
        # TODO (kerem): Let's add other exceptions that we know about here as custom exceptions as well
        except StepUnableToExecuteError:
            LOG.exception("Step cannot be executed. Task execution stopped")
            raise
        except TaskAlreadyTimeout:
            LOG.warning("Task is timed out, stopping execution")
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
            )
            return step, detailed_output, None
        except StepTerminationError as e:
            LOG.warning(
                "Step cannot be executed, marking task as failed",
                exc_info=True,
            )
            is_task_marked_as_failed = await self.fail_task(task, step, e.message)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning("Task isn't marked as failed, after step termination. NOT clean up the task")
            return step, detailed_output, None
        except FailedToSendWebhook:
            LOG.exception(
                "Failed to send webhook",
                task=task,
                step=step,
            )
            return step, detailed_output, next_step
        except FailedToNavigateToUrl as e:
            # Fail the task if we can't navigate to the URL and send the response
            LOG.exception(
                "Failed to navigate to URL, marking task as failed, and sending webhook response",
                url=e.url,
            )
            failure_reason = f"Failed to navigate to URL. URL:{e.url}, Error:{e.error_message}"
            is_task_marked_as_failed = await self.fail_task(task, step, failure_reason)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    need_final_screenshot=False,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning("Task isn't marked as failed, after navigation failure. NOT clean up the task")
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
                close_browser_on_completion=close_browser_on_completion,
            )
            return step, detailed_output, None
        except InvalidTaskStatusTransition:
            LOG.warning("Invalid task status transition")
            # TODO: shall we send task response here?
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                need_call_webhook=False,
                browser_session_id=browser_session_id,
                close_browser_on_completion=close_browser_on_completion,
            )
            return step, detailed_output, None
        except (UnsupportedActionType, UnsupportedTaskType, FailedToParseActionInstruction) as e:
            LOG.warning(
                "unsupported task type or action type, marking the task as failed",
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
                close_browser_on_completion=close_browser_on_completion,
            )
            return step, detailed_output, None
        except ScrapingFailed as sfe:
            LOG.warning(
                "Scraping failed, marking the task as failed",
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
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
            )
            return step, detailed_output, None
        except MissingBrowserStatePage:
            LOG.warning("Missing browser state page, marking the task as failed")
            await self.fail_task(
                task,
                step,
                "The browser does not have a valid page for skyvern to operate. This may be due to the website being empty or the browser crashing.",
            )
            await self.clean_up_task(
                task=task,
                last_step=step,
                api_key=api_key,
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
            )
            return step, detailed_output, None
        except Exception as e:
            LOG.exception("Got an unexpected exception in step, marking task as failed")

            failure_reason = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            is_task_marked_as_failed = await self.fail_task(task, step, failure_reason)
            if is_task_marked_as_failed:
                await self.clean_up_task(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    browser_session_id=browser_session_id,
                )
            else:
                LOG.warning("Task isn't marked as failed, after unexpected exception. NOT clean up the task")
            return step, detailed_output, None
        finally:
            # remove the step_id from the context
            context = skyvern_context.ensure_context()
            context.step_id = None
            context.task_id = None

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
            )
            return False
        except InvalidTaskStatusTransition:
            LOG.warning(
                "Invalid task status transition while failing a task",
            )
            return False
        except Exception:
            LOG.exception(
                "Failed to update status and failure reason in database. Task might going to be time_out",
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
                step_order=step.order,
                step_retry=step.retry_index,
            )

            # Update context with step_id for auto action/screenshot creation
            context = skyvern_context.current()
            if context:
                context.step_id = step.step_id

            step = await self.update_step(step=step, status=StepStatus.running)
            await app.AGENT_FUNCTION.prepare_step_execution(
                organization=organization, task=task, step=step, browser_state=browser_state
            )

            speculative_plan: SpeculativePlan | None = None
            reuse_speculative_llm_response = False
            speculative_llm_metadata: SpeculativeLLMMetadata | None = None
            if context:
                speculative_plan = context.speculative_plans.pop(step.step_id, None)

            if speculative_plan:
                step.is_speculative = False
                scraped_page = speculative_plan.scraped_page
                extract_action_prompt = speculative_plan.extract_action_prompt
                use_caching = speculative_plan.use_caching
                json_response = speculative_plan.llm_json_response
                reuse_speculative_llm_response = json_response is not None
                speculative_llm_metadata = speculative_plan.llm_metadata
                prompt_name = speculative_plan.prompt_name
                await self._persist_scrape_artifacts(
                    task=task,
                    step=step,
                    scraped_page=scraped_page,
                    context=context,
                )
            else:
                (
                    scraped_page,
                    extract_action_prompt,
                    use_caching,
                    prompt_name,
                ) = await self.build_and_record_step_prompt(
                    task,
                    step,
                    browser_state,
                    engine,
                )
                json_response = None

            detailed_agent_step_output.scraped_page = scraped_page
            detailed_agent_step_output.extract_action_prompt = extract_action_prompt
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
            elif engine == RunEngine.ui_tars and not await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
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
                if not task.navigation_goal and not isinstance(task_block, ValidationBlock):
                    actions = [await self.create_extract_action(task, step, scraped_page)]
                else:
                    llm_key_override = task.llm_key
                    # FIXME: Redundant engine check?
                    if engine in CUA_ENGINES:
                        self.async_operation_pool.run_operation(task.task_id, AgentPhase.llm)
                        llm_key_override = None

                    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                        llm_key_override, default=app.LLM_API_HANDLER
                    )
                    # Add caching flag to context for monitoring
                    if use_caching:
                        context = skyvern_context.current()
                        if context:
                            context.use_prompt_caching = True

                    if not reuse_speculative_llm_response:
                        json_response = await llm_api_handler(
                            prompt=extract_action_prompt,
                            prompt_name=prompt_name,
                            step=step,
                            screenshots=scraped_page.screenshots,
                        )
                    else:
                        LOG.debug(
                            "Using speculative extract-actions response",
                            step_id=step.step_id,
                        )
                    if json_response is None:
                        raise MissingExtractActionsResponse()
                    try:
                        if pdf_embed_src := scraped_page.check_pdf_viewer_embed():
                            LOG.info("Generate DownloadFileAction for PDF viewer page", step_id=step.step_id)
                            pdf_bytes: bytes | None = None
                            download_url: str | None = None

                            # Check if the embed src is a data URI with base64 encoded PDF
                            # Format: data:application/pdf[;charset=...];base64,<base64_data>
                            if pdf_embed_src.startswith("data:application/pdf"):
                                # Use more precise regex to extract base64 data after the base64, prefix
                                # This pattern matches: data:application/pdf[;optional_params];base64,<data>
                                m = re.search(r"data:application/pdf[^;]*;base64,(.+)", pdf_embed_src, re.S)
                                if not m:
                                    raise PDFEmbedBase64DecodeError(
                                        pdf_embed_src=pdf_embed_src,
                                        reason="Failed to extract base64 data from PDF embed src. Expected format: data:application/pdf[;charset=...];base64,<data>",
                                    )

                                base64_data = m.group(1)
                                LOG.info(
                                    "Found base64 data in PDF embed src",
                                    step_id=step.step_id,
                                    base64_data_length=len(base64_data),
                                )

                                # Decode base64 data with error handling
                                try:
                                    pdf_bytes = base64.b64decode(base64_data, validate=True)
                                except Exception as e:
                                    raise PDFEmbedBase64DecodeError(
                                        pdf_embed_src=pdf_embed_src,
                                        reason=f"Failed to decode base64 data: {str(e)}",
                                    ) from e
                            else:
                                # If not a data URI, treat it as a URL
                                LOG.info(
                                    "Found PDF embed src as URL (not base64 data)",
                                    step_id=step.step_id,
                                    download_url=pdf_embed_src,
                                )
                                download_url = pdf_embed_src

                            actions = [
                                DownloadFileAction(
                                    reasoning="Downloading the file from the PDF viewer.",
                                    organization_id=task.organization_id,
                                    workflow_run_id=task.workflow_run_id,
                                    task_id=task.task_id,
                                    step_id=step.step_id,
                                    step_order=step.order,
                                    action_order=0,
                                    file_name=f"{uuid.uuid4()}.pdf",
                                    byte=pdf_bytes,
                                    download_url=download_url,
                                    download=True,
                                )
                            ]
                        else:
                            otp_json_response, otp_actions = await self.handle_potential_OTP_actions(
                                task, step, scraped_page, browser_state, json_response
                            )
                            if otp_actions:
                                detailed_agent_step_output.llm_response = otp_json_response
                                actions = otp_actions
                            else:
                                actions = parse_actions(
                                    task, step.step_id, step.order, scraped_page, json_response["actions"]
                                )

                        if context:
                            context.pop_totp_code(task.task_id)
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
                                errors=[TimeoutGetTOTPVerificationCodeError().to_user_defined_error()],
                            )
                        ]
                    except FailedToGetTOTPVerificationCode as e:
                        actions = [
                            TerminateAction(
                                reasoning=f"Failed to get TOTP verification code. Going to terminate. Reason: {e.reason}",
                                intention=f"Failed to get TOTP verification code. Going to terminate. Reason: {e.reason}",
                                organization_id=task.organization_id,
                                workflow_run_id=task.workflow_run_id,
                                task_id=task.task_id,
                                step_id=step.step_id,
                                step_order=step.order,
                                action_order=0,
                                errors=[GetTOTPVerificationCodeError(reason=e.reason).to_user_defined_error()],
                            )
                        ]

                    if reuse_speculative_llm_response and speculative_llm_metadata:
                        await self._persist_speculative_llm_metadata(
                            step,
                            speculative_llm_metadata,
                            screenshots=scraped_page.screenshots,
                        )
                        speculative_llm_metadata = None

            detailed_agent_step_output.actions = actions
            if len(actions) == 0:
                LOG.info(
                    "No actions to execute, marking step as failed",
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
                if wait_actions_len > 0 and wait_actions_len < len(actions):
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
                            step_order=step.order,
                            action=action,
                        )

                        previous_action, previous_result = detailed_agent_step_output.actions_and_results[
                            previous_action_idx
                        ]
                        if len(previous_result) > 0 and previous_result[-1].success:
                            LOG.info(
                                "Previous action succeeded, but we'll still continue.",
                                step_order=step.order,
                                previous_action=previous_action,
                                previous_result=previous_result,
                            )
                        else:
                            LOG.warning(
                                "Previous action failed, so handle the next action.",
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

                # Pass TOTP secret to handler for multi-field TOTP sequences
                # Handler will generate TOTP at execution time
                if (
                    action.action_type == ActionType.INPUT_TEXT
                    and self._is_multi_field_totp_sequence(actions)
                    and (totp_secret := skyvern_context.ensure_context().totp_codes.get(f"{task.task_id}_secret"))
                ):
                    # Pass TOTP secret to handler for execution-time generation
                    action.totp_timing_info = {
                        "is_totp_sequence": True,
                        "action_index": action_idx,
                        "totp_secret": totp_secret,
                        "is_retry": step.retry_index > 0,
                    }

                results = await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=current_page,
                    action=action,
                )
                await app.AGENT_FUNCTION.post_action_execution(action)
                detailed_agent_step_output.actions_and_results[action_idx] = (
                    action,
                    results,
                )

                # Determine wait time between actions
                wait_time = random.uniform(0.5, 1.0)

                # For multi-field TOTP sequences, use zero delay between all digits for fast execution
                if action.action_type == ActionType.INPUT_TEXT and self._is_multi_field_totp_sequence(actions):
                    current_text = action.text if hasattr(action, "text") else None

                    if current_text and len(current_text) == 1 and current_text.isdigit():
                        # Zero delay between all TOTP digits for fast execution
                        wait_time = 0.0
                        LOG.debug(
                            "TOTP: zero delay for digit",
                            task_id=task.task_id,
                            action_idx=action_idx,
                            digit=current_text,
                        )

                await asyncio.sleep(wait_time)
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
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                    if results[-1].skip_remaining_actions:
                        LOG.warning(
                            "Going to stop executing the remaining actions",
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
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                elif results and not results[-1].success and not results[-1].stop_execution_on_failure:
                    LOG.warning(
                        "Action failed, but not stopping execution",
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
                step_order=step.order,
                step_retry=step.retry_index,
                action_results=action_results,
            )

            # Clean up TOTP cache after multi-field TOTP sequence completion
            if self._is_multi_field_totp_sequence(actions):
                context = skyvern_context.ensure_context()
                cache_key = f"{task.task_id}_totp_cache"
                if cache_key in context.totp_codes:
                    context.totp_codes.pop(cache_key)
                    LOG.debug(
                        "Cleaned up TOTP cache after multi-field sequence completion",
                        task_id=task.task_id,
                    )

                secret_key = f"{task.task_id}_secret"
                if secret_key in context.totp_codes:
                    context.totp_codes.pop(secret_key)

            # Check if Skyvern already returned a complete action, if so, don't run user goal check
            has_decisive_action = False
            if detailed_agent_step_output and detailed_agent_step_output.actions_and_results:
                for action, results in detailed_agent_step_output.actions_and_results:
                    if isinstance(action, DecisiveAction):
                        has_decisive_action = True
                        break

            task_completes_on_download = task_block and task_block.complete_on_download and task.workflow_run_id
            enable_parallel_verification = False
            if (
                not has_decisive_action
                and not task_completes_on_download
                and not isinstance(task_block, ActionBlock)
                and complete_verification
                and (task.navigation_goal or task.complete_criterion)
            ):
                disable_user_goal_check = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                    "DISABLE_USER_GOAL_CHECK",
                    task.task_id,
                    properties={"task_url": task.url, "organization_id": task.organization_id},
                )

                # Parallel verification is always enabled (user goal check deferred to handle_completed_step)
                enable_parallel_verification = not disable_user_goal_check

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
                await app.AGENT_FUNCTION.post_action_execution(extract_action)
                detailed_agent_step_output.actions_and_results.append((extract_action, extract_results))

            # If no action errors return the agent state and output
            completed_step = await self.update_step(
                step=step,
                status=StepStatus.completed,
                output=detailed_agent_step_output.to_agent_step_output(),
            )
            if enable_parallel_verification:
                completed_step.speculative_original_status = StepStatus.completed
            return completed_step, detailed_agent_step_output.get_clean_detailed_output()
        except CancelledError:
            LOG.exception(
                "CancelledError in agent_step, marking step as failed",
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
            MissingBrowserStatePage,
        ):
            raise

        except Exception as e:
            LOG.exception(
                "Unexpected exception in agent_step, marking step as failed",
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
            actions_count=len(actions),
        )

        return actions

    async def _speculate_next_step_plan(
        self,
        task: Task,
        current_step: Step,
        next_step: Step,
        browser_state: BrowserState,
        engine: RunEngine,
    ) -> SpeculativePlan | None:
        if engine in CUA_ENGINES:
            LOG.info(
                "Skipping speculative extract-actions for CUA engine",
                step_id=current_step.step_id,
                task_id=task.task_id,
            )
            return None

        try:
            next_step.is_speculative = True

            scraped_page, extract_action_prompt, use_caching, prompt_name = await self.build_and_record_step_prompt(
                task,
                next_step,
                browser_state,
                engine,
                persist_artifacts=False,
            )

            if scraped_page.check_pdf_viewer_embed():
                next_step.is_speculative = False
                LOG.info("Skipping speculative extract-actions for PDF viewer page", step_id=current_step.step_id)
                return None

            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                task.llm_key,
                default=app.LLM_API_HANDLER,
            )

            llm_json_response = await llm_api_handler(
                prompt=extract_action_prompt,
                prompt_name=prompt_name,
                step=next_step,
                screenshots=scraped_page.screenshots,
            )

            LOG.info(
                "Speculative extract-actions completed",
                current_step_id=current_step.step_id,
                synthetic_step_id=next_step.step_id,
            )

            metadata_copy = None
            if next_step.speculative_llm_metadata is not None:
                metadata_copy = next_step.speculative_llm_metadata.model_copy()
                next_step.speculative_llm_metadata = None
            next_step.is_speculative = False

            return SpeculativePlan(
                scraped_page=scraped_page,
                extract_action_prompt=extract_action_prompt,
                use_caching=use_caching,
                llm_json_response=llm_json_response,
                llm_metadata=metadata_copy,
                prompt_name=prompt_name,
            )
        except Exception:
            LOG.warning(
                "Failed to run speculative extract-actions",
                step_id=current_step.step_id,
                exc_info=True,
            )
            next_step.is_speculative = False
            return None

    async def _persist_speculative_llm_metadata(
        self,
        step: Step,
        metadata: SpeculativeLLMMetadata,
        *,
        screenshots: list[bytes] | None = None,
    ) -> None:
        if not metadata:
            return

        LOG.debug("Persisting speculative LLM metadata")

        artifacts = []
        if metadata.prompt:
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=metadata.prompt.encode("utf-8"),
                    artifact_type=ArtifactType.LLM_PROMPT,
                    screenshots=screenshots,
                    step=step,
                )
            )

        if metadata.llm_request_json:
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=metadata.llm_request_json.encode("utf-8"),
                    artifact_type=ArtifactType.LLM_REQUEST,
                    step=step,
                )
            )

        if metadata.llm_response_json:
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=metadata.llm_response_json.encode("utf-8"),
                    artifact_type=ArtifactType.LLM_RESPONSE,
                    step=step,
                )
            )

        if metadata.parsed_response_json:
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=metadata.parsed_response_json.encode("utf-8"),
                    artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                    step=step,
                )
            )

        if metadata.rendered_response_json:
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=metadata.rendered_response_json.encode("utf-8"),
                    artifact_type=ArtifactType.LLM_RESPONSE_RENDERED,
                    step=step,
                )
            )

        if artifacts:
            await app.ARTIFACT_MANAGER.bulk_create_artifacts(artifacts)

        incremental_cost = metadata.llm_cost if metadata.llm_cost and metadata.llm_cost > 0 else None
        incremental_input_tokens = (
            metadata.input_tokens if metadata.input_tokens and metadata.input_tokens > 0 else None
        )
        incremental_output_tokens = (
            metadata.output_tokens if metadata.output_tokens and metadata.output_tokens > 0 else None
        )
        incremental_reasoning_tokens = (
            metadata.reasoning_tokens if metadata.reasoning_tokens and metadata.reasoning_tokens > 0 else None
        )
        incremental_cached_tokens = (
            metadata.cached_tokens if metadata.cached_tokens and metadata.cached_tokens > 0 else None
        )

        if (
            incremental_cost is not None
            or incremental_input_tokens is not None
            or incremental_output_tokens is not None
            or incremental_reasoning_tokens is not None
            or incremental_cached_tokens is not None
        ):
            await app.DATABASE.update_step(
                task_id=step.task_id,
                step_id=step.step_id,
                organization_id=step.organization_id,
                incremental_cost=incremental_cost,
                incremental_input_tokens=incremental_input_tokens,
                incremental_output_tokens=incremental_output_tokens,
                incremental_reasoning_tokens=incremental_reasoning_tokens,
                incremental_cached_tokens=incremental_cached_tokens,
            )

            if incremental_input_tokens:
                step.input_token_count += incremental_input_tokens
            if incremental_output_tokens:
                step.output_token_count += incremental_output_tokens
            if incremental_reasoning_tokens:
                step.reasoning_token_count = (step.reasoning_token_count or 0) + incremental_reasoning_tokens
            if incremental_cached_tokens:
                step.cached_token_count = (step.cached_token_count or 0) + incremental_cached_tokens
            if incremental_cost:
                step.step_cost += incremental_cost

        step.speculative_llm_metadata = None

    async def _persist_speculative_metadata_for_discarded_plan(
        self,
        step: Step,
        speculative_task: asyncio.Future[SpeculativePlan | None],
        *,
        cancel_step: bool = False,
    ) -> None:
        try:
            plan = await asyncio.shield(speculative_task)
        except CancelledError:
            LOG.debug(
                "Speculative extract-actions cancelled before metadata persistence",
                step_id=step.step_id,
            )
            step.is_speculative = False
            if cancel_step:
                await self._cancel_speculative_step(step)
            return
        except Exception:
            LOG.debug(
                "Speculative extract-actions failed before metadata persistence",
                step_id=step.step_id,
                exc_info=True,
            )
            step.is_speculative = False
            if cancel_step:
                await self._cancel_speculative_step(step)
            return

        if not plan or not plan.llm_metadata:
            step.is_speculative = False
            if cancel_step:
                await self._cancel_speculative_step(step)
            return

        try:
            await self._persist_speculative_llm_metadata(
                step,
                plan.llm_metadata,
            )
            step.is_speculative = False
            if cancel_step:
                await self._cancel_speculative_step(step)
        except Exception:
            LOG.warning(
                "Failed to persist speculative llm metadata for discarded plan",
                step_id=step.step_id,
                exc_info=True,
            )

    async def _cancel_speculative_step(self, step: Step) -> None:
        if step.status == StepStatus.canceled:
            return
        try:
            updated_step = await self.update_step(step, status=StepStatus.canceled)
            step.status = updated_step.status
            step.is_speculative = False
        except Exception:
            LOG.warning(
                "Failed to cancel speculative step",
                step_id=step.step_id,
                exc_info=True,
            )

    async def complete_verify(
        self, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
    ) -> CompleteVerifyResult:
        LOG.info(
            "Checking if user goal is achieved after re-scraping the page",
            workflow_run_id=task.workflow_run_id,
        )
        scroll = True
        llm_key_override = task.llm_key
        if await service_utils.is_cua_task(task=task):
            scroll = False
            llm_key_override = None

        scraped_page_refreshed = await scraped_page.refresh(draw_boxes=False, scroll=scroll)

        actions_and_results_str = ""
        if task.include_action_history_in_verification:
            actions_and_results_str = await self._get_action_results(task, current_step=step)

        # Check if we should use the termination-aware prompt (experiment)
        use_termination_prompt = False
        try:
            distinct_id = task.workflow_run_id if task.workflow_run_id else task.task_id
            use_termination_prompt = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "USE_TERMINATION_AWARE_COMPLETE_VERIFICATION",
                distinct_id,
                properties={"organization_id": task.organization_id, "task_url": task.url},
            )
            if use_termination_prompt:
                LOG.info(
                    "Experiment enabled: using termination-aware complete verification prompt for file download block",
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                    organization_id=task.organization_id,
                    block_type="file_download",
                )
        except Exception as e:
            LOG.warning(
                "Failed to check USE_TERMINATION_AWARE_COMPLETE_VERIFICATION experiment; using legacy behavior",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                error=str(e),
            )

        # Select the appropriate template based on experiment
        template_name = "check-user-goal-with-termination" if use_termination_prompt else "check-user-goal"
        prompt_name = "check-user-goal-with-termination" if use_termination_prompt else "check-user-goal"

        verification_prompt = load_prompt_with_elements(
            element_tree_builder=scraped_page_refreshed,
            prompt_engine=prompt_engine,
            template_name=template_name,
            navigation_goal=task.navigation_goal,
            navigation_payload=task.navigation_payload,
            complete_criterion=task.complete_criterion,
            terminate_criterion=task.terminate_criterion,
            action_history=actions_and_results_str,
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        )

        # This prompt is critical for our agent, we probably should use the primary LLM handler
        # but we're experimenting with using the dedicated check-user-goal handler
        use_check_user_goal_handler = False
        try:
            # Use task_id or workflow_run_id as distinct_id
            distinct_id = task.workflow_run_id if task.workflow_run_id else task.task_id
            use_check_user_goal_handler = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "USE_CHECK_USER_GOAL_HANDLER_FOR_VERIFICATION",
                distinct_id,
                properties={"organization_id": task.organization_id},
            )
            if use_check_user_goal_handler:
                LOG.info(
                    "Experiment enabled: using CHECK_USER_GOAL_LLM_API_HANDLER for complete verification",
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                    organization_id=task.organization_id,
                )
        except Exception as e:
            LOG.warning(
                "Failed to check USE_CHECK_USER_GOAL_HANDLER_FOR_VERIFICATION experiment; using legacy behavior",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                error=str(e),
            )

        if use_check_user_goal_handler:
            # Use the dedicated check-user-goal handler (new behavior)
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                llm_key_override, default=app.CHECK_USER_GOAL_LLM_API_HANDLER
            )
        else:
            # Use the primary LLM handler (legacy behavior)
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                llm_key_override, default=app.LLM_API_HANDLER
            )

        verification_result = await llm_api_handler(
            prompt=verification_prompt,
            step=step,
            screenshots=scraped_page_refreshed.screenshots,
            prompt_name=prompt_name,
        )
        return CompleteVerifyResult.model_validate(verification_result)

    async def check_user_goal_complete(
        self, page: Page, scraped_page: ScrapedPage, task: Task, step: Step
    ) -> CompleteAction | TerminateAction | None:
        try:
            verification_result = await self.complete_verify(
                page=page,
                scraped_page=scraped_page,
                task=task,
                step=step,
            )

            # Check if we should terminate instead of complete
            # Note: This requires the USE_TERMINATION_AWARE_COMPLETE_VERIFICATION experiment to be enabled
            if verification_result.is_terminate:
                LOG.warning(
                    "Periodic verification determined task should terminate (termination-aware experiment)",
                    workflow_run_id=task.workflow_run_id,
                    thoughts=verification_result.thoughts,
                    status=verification_result.status if verification_result.status else "legacy",
                )
                return TerminateAction(
                    reasoning=verification_result.thoughts,
                )

            # We don't want to return a complete action if the user goal is not achieved since we're checking at every step
            if not verification_result.is_complete:
                return None

            return CompleteAction(
                reasoning=verification_result.thoughts,
                data_extraction_goal=task.data_extraction_goal,
                verified=True,
            )

        except Exception:
            LOG.exception(
                "Failed to check user goal complete, skipping",
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
            raise MissingBrowserStatePage()

        skyvern_frame: SkyvernFrame | None = None
        try:
            skyvern_frame = await SkyvernFrame.create_instance(frame=working_page)
            await skyvern_frame.safe_wait_for_animation_end()
        except Exception:
            LOG.info("Failed to wait for animation end, ignore it", exc_info=True)

        context = skyvern_context.ensure_context()
        scrolling_number = context.max_screenshot_scrolls
        if scrolling_number is None:
            scrolling_number = DEFAULT_MAX_SCREENSHOT_SCROLLS

        if engine in CUA_ENGINES:
            scrolling_number = 0

        artifacts: list[BulkArtifactCreationRequest | None] = []
        try:
            # get current x, y position of the page
            x: int | None = None
            y: int | None = None
            try:
                x, y = await skyvern_frame.get_scroll_x_y() if skyvern_frame else (None, None)
                LOG.debug("Current x, y position of the page before taking screenshot", x=x, y=y)
            except Exception:
                LOG.warning("Failed to get current x, y position of the page", exc_info=True)

            screenshot = await browser_state.take_post_action_screenshot(
                scrolling_number=scrolling_number,
            )
            # scroll back to the original x, y position of the page
            if skyvern_frame and x is not None and y is not None:
                await skyvern_frame.safe_scroll_to_x_y(x, y)
                LOG.debug("Scrolled back to the original x, y position of the page after taking screenshot", x=x, y=y)
                artifacts.append(
                    await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                        data=screenshot,
                        artifact_type=ArtifactType.SCREENSHOT_ACTION,
                        step=step,
                    )
                )
        except Exception:
            LOG.error(
                "Failed to record screenshot after action",
                exc_info=True,
            )

        try:
            skyvern_frame = await SkyvernFrame.create_instance(frame=working_page)
            html = await skyvern_frame.get_content()
            artifacts.append(
                await app.ARTIFACT_MANAGER.prepare_llm_artifact(
                    data=html.encode(),
                    artifact_type=ArtifactType.HTML_ACTION,
                    step=step,
                )
            )
        except Exception:
            LOG.exception("Failed to record html after action")

        if artifacts:
            try:
                await app.ARTIFACT_MANAGER.bulk_create_artifacts(artifacts)
            except Exception:
                LOG.warning("Failed to bulk create artifacts after action", exc_info=True)

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
            LOG.exception("Failed to record video after action")

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
                browser_profile_id=workflow_run.browser_profile_id,
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
            LOG.info("Try to stop loading the page before scraping")
            await browser_state.stop_page_loading()
        elif scrape_type == ScrapeType.RELOAD:
            LOG.info("Try to reload the page before scraping")
            await browser_state.reload_page()

        max_screenshot_number = settings.MAX_NUM_SCREENSHOTS
        draw_boxes = True
        scroll = True
        if engine in CUA_ENGINES:
            max_screenshot_number = 1
            draw_boxes = False
            scroll = False

        return await browser_state.scrape_website(
            url=task.url,
            cleanup_element_tree=app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step),
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
        *,
        persist_artifacts: bool = True,
    ) -> tuple[ScrapedPage, str, bool, str]:
        # Check if we have pre-scraped data from parallel verification optimization
        context = skyvern_context.current()
        scraped_page: ScrapedPage | None = None

        if (
            context
            and context.next_step_pre_scraped_data
            and context.next_step_pre_scraped_data.get("step_id") == step.step_id
        ):
            scraped_page = context.next_step_pre_scraped_data.get("scraped_page")
            if scraped_page:
                timestamp = context.next_step_pre_scraped_data.get("timestamp")
                age_seconds = (datetime.now(UTC) - timestamp).total_seconds() if timestamp else 0
                LOG.info(
                    "Using pre-scraped data from parallel verification optimization",
                    step_id=step.step_id,
                    num_elements=len(scraped_page.elements),
                    age_seconds=age_seconds,
                )
                # Clear the cached data
                context.next_step_pre_scraped_data = None

        # If we don't have pre-scraped data, scrape normally
        if scraped_page is None:
            # Check PostHog for speed optimizations BEFORE scraping
            # This decision will be used in both:
            # 1. SVG conversion skip (in agent_functions.py cleanup)
            # 2. Tree selection (economy vs regular tree)
            # By checking once and storing in context, we ensure perfect coordination
            if context:
                try:
                    distinct_id = task.workflow_run_id if task.workflow_run_id else task.task_id
                    context.enable_speed_optimizations = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                        "ENABLE_SPEED_OPTIMIZATIONS",
                        distinct_id,
                        properties={"organization_id": task.organization_id},
                    )
                except Exception:
                    LOG.warning(
                        "Failed to check ENABLE_SPEED_OPTIMIZATIONS feature flag",
                        exc_info=True,
                        task_id=task.task_id,
                    )
                    context.enable_speed_optimizations = False

            # start the async tasks while running scrape_website
            if engine not in CUA_ENGINES:
                self.async_operation_pool.run_operation(task.task_id, AgentPhase.scrape)

            # Scrape the web page and get the screenshot and the elements
            # HACK: try scrape_website three time to handle screenshot timeout
            # first time: normal scrape to take screenshot
            # second time: try again the normal scrape, (stopping window loading before scraping barely helps, but causing problem)
            # third time: reload the page before scraping
            extract_action_prompt = ""
            use_caching = False
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
                    LOG.exception(f"{e.__class__.__name__} happened in two normal attempts and reload-page retry")
                    raise e

        if scraped_page is None:
            raise EmptyScrapePage()

        extract_action_prompt = ""
        use_caching = False

        if persist_artifacts:
            await self._persist_scrape_artifacts(
                task=task,
                step=step,
                scraped_page=scraped_page,
                context=context,
            )
        LOG.info(
            "Scraped website",
            step_order=step.order,
            step_retry=step.retry_index,
            num_elements=len(scraped_page.elements),
            url=task.url,
        )
        extract_action_prompt = ""
        prompt_name = EXTRACT_ACTION_PROMPT_NAME  # Default; overwritten below for non-CUA engines
        if engine not in CUA_ENGINES:
            extract_action_prompt, use_caching, prompt_name = await self._build_extract_action_prompt(
                task,
                step,
                browser_state,
                scraped_page,
                verification_code_check=bool(task.totp_verification_url or task.totp_identifier),
                expire_verification_code=True,
            )

        return scraped_page, extract_action_prompt, use_caching, prompt_name

    async def _persist_scrape_artifacts(
        self,
        *,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        context: SkyvernContext | None,
    ) -> None:
        """
        Persist the core scrape artifacts (HTML + element metadata) for a step.
        This is used both for regular runs and when adopting a speculative plan.
        """

        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.HTML_SCRAPE,
            data=scraped_page.html.encode(),
        )

        element_tree_format = ElementTreeFormat.HTML
        element_tree_in_prompt = self._build_element_tree_for_prompt(
            scraped_page=scraped_page,
            step=step,
            task=task,
            context=context,
            element_tree_format=element_tree_format,
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

    def _build_element_tree_for_prompt(
        self,
        *,
        scraped_page: ScrapedPage,
        step: Step,
        task: Task,
        context: SkyvernContext | None,
        element_tree_format: ElementTreeFormat,
    ) -> str:
        """
        Determine which element tree representation should be captured for the prompt/artifacts.
        Mirrors the previous inline logic so that speculative runs can reuse it.
        """

        enable_speed_optimizations = context.enable_speed_optimizations if context else False
        if not enable_speed_optimizations:
            return scraped_page.build_element_tree(element_tree_format)

        if step.retry_index == 0:
            element_tree_in_prompt = scraped_page.build_economy_elements_tree(element_tree_format)
            LOG.info(
                "Speed optimization: Using economy element tree (skipping SVGs)",
                step_order=step.order,
                step_retry=step.retry_index,
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
            )
            return element_tree_in_prompt

        element_tree_in_prompt = scraped_page.build_element_tree(element_tree_format)
        LOG.info(
            "Speed optimization: Using regular tree on retry (SVGs from global cache)",
            step_order=step.order,
            step_retry=step.retry_index,
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
        )
        return element_tree_in_prompt

    @staticmethod
    def _build_extract_action_cache_variant(
        verification_code_check: bool,
        has_magic_link_page: bool,
        complete_criterion: str | None,
    ) -> str:
        """
        Build a short-but-unique cache variant identifier so extract-action prompts that
        differ meaningfully (OTP, magic link flows, complete criteria) do not reuse the
        same Vertex cache object.
        """
        variant_parts: list[str] = []
        if verification_code_check:
            variant_parts.append("vc")
        if has_magic_link_page:
            variant_parts.append("ml")
        if complete_criterion:
            normalized = " ".join(complete_criterion.split())
            digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:6]
            variant_parts.append(f"cc{digest}")
        return "-".join(variant_parts) if variant_parts else "std"

    async def _create_vertex_cache_for_task(
        self,
        task: Task,
        static_prompt: str,
        context: SkyvernContext,
        llm_key_override: str | None,
        prompt_variant: str | None = None,
    ) -> None:
        """
        Create a Vertex AI cache for the task's static prompt.

        Uses llm_key as cache key to enable cache sharing across tasks with the same model.

        Args:
            task: The task to create cache for
            static_prompt: The static prompt content to cache
            context: The Skyvern context to store the cache name in
            llm_key_override: Optional override when we explicitly pick an LLM key
            prompt_variant: Cache variant identifier (std/vc/ml/etc.)
        """
        resolved_llm_key = llm_key_override or task.llm_key

        if not resolved_llm_key:
            LOG.warning(
                "Cannot create Vertex AI cache without llm_key, skipping cache creation",
                task_id=task.task_id,
            )
            return

        cache_variant = prompt_variant or "std"

        try:
            LOG.info(
                "Attempting Vertex AI cache creation",
                task_id=task.task_id,
                llm_key=resolved_llm_key,
                cache_variant=cache_variant,
            )
            cache_manager = get_cache_manager()

            variant_suffix = f"-{cache_variant}" if cache_variant else ""

            cache_key = f"{EXTRACT_ACTION_CACHE_KEY_PREFIX}{variant_suffix}-{resolved_llm_key}"

            # Get the actual model name from LLM config to ensure correct format
            # (e.g., "gemini-2.5-flash" with decimal, not "gemini-2-5-flash")
            model_name = "gemini-2.5-flash"  # Default

            try:
                llm_config = LLMConfigRegistry.get_config(resolved_llm_key)
                extracted_name = None

                # Try to extract from model_name if it contains "vertex_ai/" or starts with "gemini-"
                if hasattr(llm_config, "model_name") and isinstance(llm_config.model_name, str):
                    if "vertex_ai/" in llm_config.model_name:
                        # Direct Vertex config: "vertex_ai/gemini-2.5-flash" -> "gemini-2.5-flash"
                        extracted_name = llm_config.model_name.split("/")[-1]
                    elif llm_config.model_name.startswith("gemini-"):
                        # Already in correct format
                        extracted_name = llm_config.model_name

                # For router/fallback configs, extract from api_base or infer from key name
                if not extracted_name and hasattr(llm_config, "litellm_params") and llm_config.litellm_params:
                    params = llm_config.litellm_params
                    api_base = getattr(params, "api_base", None)
                    if api_base and isinstance(api_base, str) and "/models/" in api_base:
                        # Extract from URL: .../models/gemini-2.5-flash -> "gemini-2.5-flash"
                        extracted_name = api_base.split("/models/")[-1]

                # For router configs without api_base, infer from the llm_key itself
                if not extracted_name:
                    # Extract version from llm_key (e.g., VERTEX_GEMINI_1_5_FLASH -> "1_5" or VERTEX_GEMINI_2.5_FLASH -> "2.5")
                    # Pattern: GEMINI_{version}_{flavor} where version can use dots, underscores, or dashes
                    version_match = re.search(r"GEMINI[_-](\d+[._-]\d+)", resolved_llm_key, re.IGNORECASE)
                    version = version_match.group(1).replace("_", ".").replace("-", ".") if version_match else "2.5"

                    # Determine flavor
                    if "_PRO_" in resolved_llm_key or resolved_llm_key.endswith("_PRO"):
                        extracted_name = f"gemini-{version}-pro"
                    elif "_FLASH_LITE_" in resolved_llm_key or resolved_llm_key.endswith("_FLASH_LITE"):
                        extracted_name = f"gemini-{version}-flash-lite"
                    else:
                        # Default to flash flavor
                        extracted_name = f"gemini-{version}-flash"

                if extracted_name:
                    model_name = extracted_name
            except Exception as e:
                LOG.debug("Failed to extract model name from config, using default", error=str(e))

            # Normalize model name to the canonical Vertex identifier (e.g., gemini-2.5-pro)
            match = re.search(r"(gemini-\d+(?:\.\d+)?-(?:flash-lite|flash|pro))", model_name, re.IGNORECASE)
            if match:
                model_name = match.group(1).lower()

            # Create cache for this task
            # Use asyncio.to_thread to offload blocking HTTP request (requests.post)
            # This prevents freezing the event loop during cache creation
            cache_data = await asyncio.to_thread(
                cache_manager.create_cache,
                model_name=model_name,
                static_content=static_prompt,
                cache_key=cache_key,
                ttl_seconds=3600,  # 1 hour
            )

            # Store cache metadata in context
            context.vertex_cache_name = cache_data["name"]
            context.vertex_cache_key = cache_key
            context.vertex_cache_variant = cache_variant

            LOG.info(
                "Created Vertex AI cache for task",
                task_id=task.task_id,
                cache_key=cache_key,
                cache_name=cache_data["name"],
                model_name=model_name,
                cache_variant=cache_variant,
            )
        except Exception as e:
            LOG.warning(
                "Failed to create Vertex AI cache, proceeding without caching",
                task_id=task.task_id,
                error=str(e),
                exc_info=True,
            )

    async def _build_extract_action_prompt(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        scraped_page: ScrapedPage,
        verification_code_check: bool = False,
        expire_verification_code: bool = False,
    ) -> tuple[str, bool, str]:
        actions_and_results_str = await self._get_action_results(task)

        # Generate the extract action prompt
        navigation_goal = task.navigation_goal
        starting_url = task.url
        page = await browser_state.get_working_page()
        current_url = (
            await SkyvernFrame.evaluate(frame=page, expression="() => document.location.href") if page else starting_url
        )
        final_navigation_payload = self._build_navigation_payload(
            task, expire_verification_code=expire_verification_code, step=step, scraped_page=scraped_page
        )
        navigation_payload_str = json.dumps(final_navigation_payload)

        task_type = task.task_type if task.task_type else TaskType.general
        template = ""
        if task_type == TaskType.general:
            template = EXTRACT_ACTION_TEMPLATE
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

        # Reset cached prompt and cache reference by default; we will set them below if caching is enabled.
        # This prevents extract-action cache from being attached to other prompts like decisive-criterion-validate.
        context.cached_static_prompt = None
        context.vertex_cache_name = None

        # Check if prompt caching is enabled for extract-action
        use_caching = False
        prompt_caching_settings = await self._get_prompt_caching_settings(context)
        effective_llm_key = task.llm_key
        if not effective_llm_key:
            handler_for_key = LLMAPIHandlerFactory.get_override_llm_api_handler(
                task.llm_key, default=app.LLM_API_HANDLER
            )
            effective_llm_key = getattr(handler_for_key, "llm_key", None)
        cache_enabled = prompt_caching_settings.get(EXTRACT_ACTION_PROMPT_NAME) or prompt_caching_settings.get(
            EXTRACT_ACTION_TEMPLATE
        )
        LOG.info(
            "Extract-action prompt caching evaluation",
            template=template,
            cache_enabled=cache_enabled,
            prompt_caching_settings=prompt_caching_settings,
            task_llm_key=task.llm_key,
            effective_llm_key=effective_llm_key,
        )
        enable_speed_optimizations = context.enable_speed_optimizations
        element_tree_format = ElementTreeFormat.HTML
        if enable_speed_optimizations:
            if step.retry_index == 0:
                elements_for_prompt = scraped_page.build_economy_elements_tree(element_tree_format)
            else:
                elements_for_prompt = scraped_page.build_element_tree(element_tree_format)
        else:
            elements_for_prompt = scraped_page.build_element_tree(element_tree_format)

        if template == EXTRACT_ACTION_TEMPLATE and cache_enabled:
            try:
                # Try to load split templates for caching
                prompt_kwargs = {
                    "navigation_goal": navigation_goal,
                    "navigation_payload_str": navigation_payload_str,
                    "starting_url": starting_url,
                    "current_url": current_url,
                    "data_extraction_goal": task.data_extraction_goal,
                    "action_history": actions_and_results_str,
                    "error_code_mapping_str": (
                        json.dumps(task.error_code_mapping) if task.error_code_mapping else None
                    ),
                    "local_datetime": datetime.now(context.tz_info).isoformat(),
                    "verification_code_check": verification_code_check,
                    "complete_criterion": task.complete_criterion.strip() if task.complete_criterion else None,
                    "terminate_criterion": task.terminate_criterion.strip() if task.terminate_criterion else None,
                    "parse_select_feature_enabled": context.enable_parse_select_in_extract,
                    "has_magic_link_page": context.has_magic_link_page(task.task_id),
                }
                cache_variant = self._build_extract_action_cache_variant(
                    verification_code_check=verification_code_check,
                    has_magic_link_page=context.has_magic_link_page(task.task_id),
                    complete_criterion=task.complete_criterion.strip() if task.complete_criterion else None,
                )
                static_prompt = prompt_engine.load_prompt(f"{template}-static", **prompt_kwargs)
                dynamic_prompt = prompt_engine.load_prompt(
                    f"{template}-dynamic",
                    elements=elements_for_prompt,
                    **prompt_kwargs,
                )

                # Store static prompt for caching and continue sending it alongside the dynamic section.
                # Vertex explicit caching expects the static content to still be present in the request so the
                # first call succeeds even if the cache is cold. The cached reference simply lets the service
                # reuse the static portion internally.
                context.cached_static_prompt = static_prompt
                context.use_prompt_caching = True
                use_caching = True

                # Create Vertex AI cache for Gemini models
                if effective_llm_key and "GEMINI" in effective_llm_key:
                    await self._create_vertex_cache_for_task(
                        task,
                        static_prompt,
                        context,
                        effective_llm_key,
                        prompt_variant=cache_variant,
                    )

                combined_prompt = f"{static_prompt.rstrip()}\n\n{dynamic_prompt.lstrip()}"

                LOG.info(
                    "Using cached prompt",
                    task_id=task.task_id,
                    prompt_name=EXTRACT_ACTION_PROMPT_NAME,
                    cache_variant=cache_variant,
                )
                # Map template to prompt_name for logging/caching guards
                prompt_name = EXTRACT_ACTION_PROMPT_NAME if template == EXTRACT_ACTION_TEMPLATE else template
                return combined_prompt, use_caching, prompt_name

            except Exception as e:
                LOG.warning("Failed to load cached prompt templates, falling back to original", error=str(e))
                # Fall through to original behavior

        # Original behavior - load full prompt
        full_prompt = load_prompt_with_elements(
            element_tree_builder=scraped_page,
            prompt_engine=prompt_engine,
            template_name=template,
            navigation_goal=navigation_goal,
            navigation_payload_str=navigation_payload_str,
            starting_url=starting_url,
            current_url=current_url,
            data_extraction_goal=task.data_extraction_goal,
            action_history=actions_and_results_str,
            error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
            local_datetime=datetime.now(context.tz_info).isoformat(),
            verification_code_check=verification_code_check,
            complete_criterion=task.complete_criterion.strip() if task.complete_criterion else None,
            terminate_criterion=task.terminate_criterion.strip() if task.terminate_criterion else None,
            parse_select_feature_enabled=context.enable_parse_select_in_extract,
            has_magic_link_page=context.has_magic_link_page(task.task_id),
        )

        # Map template to prompt_name for logging/caching guards
        prompt_name = EXTRACT_ACTION_PROMPT_NAME if template == EXTRACT_ACTION_TEMPLATE else template
        return full_prompt, use_caching, prompt_name

    async def _get_prompt_caching_settings(self, context: SkyvernContext) -> dict[str, bool]:
        """
        Resolve prompt caching settings for the current run.

        We prefer explicit overrides via LLMAPIHandlerFactory.set_prompt_caching_settings(), which
        are mostly used by scripts/tests. When no override exists, evaluate the PostHog experiment
        once per context and cache the result on the context to avoid repeated lookups.
        """
        if LLMAPIHandlerFactory._prompt_caching_settings is not None:
            return LLMAPIHandlerFactory._prompt_caching_settings

        if context.prompt_caching_settings is not None:
            return context.prompt_caching_settings

        distinct_id = context.run_id or context.workflow_run_id or context.task_id
        organization_id = context.organization_id
        context.prompt_caching_settings = {}

        if not distinct_id or not organization_id:
            return context.prompt_caching_settings

        try:
            enabled = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "PROMPT_CACHING_OPTIMIZATION",
                distinct_id,
                properties={"organization_id": organization_id},
            )
        except Exception as exc:
            LOG.warning(
                "Failed to evaluate prompt caching experiment; defaulting to disabled",
                distinct_id=distinct_id,
                organization_id=organization_id,
                error=str(exc),
            )
            return context.prompt_caching_settings

        if enabled:
            context.prompt_caching_settings = {
                EXTRACT_ACTION_PROMPT_NAME: True,
                EXTRACT_ACTION_TEMPLATE: True,
            }
            LOG.info(
                "Prompt caching optimization enabled",
                distinct_id=distinct_id,
                organization_id=organization_id,
            )

        return context.prompt_caching_settings

    def _should_process_totp(self, scraped_page: ScrapedPage | None) -> bool:
        """Detect TOTP pages by checking for multiple input fields or verification keywords."""
        if not scraped_page:
            return False

        try:
            # Count input fields that could be for TOTP (more flexible than maxlength="1")
            input_fields = [
                element
                for element in scraped_page.elements
                if element.get("tagName", "").lower() == "input"
                and element.get("attributes", {}).get("type", "text").lower() in ["text", "number", "tel"]
            ]

            # Check for multiple input fields (potential multi-field TOTP)
            if len(input_fields) >= 4:
                # Additional check: look for patterns that suggest multi-field TOTP
                # Check if inputs are close together or have similar attributes
                has_maxlength_1 = any(elem.get("attributes", {}).get("maxlength") == "1" for elem in input_fields)

                # Check for input fields with numeric patterns (type="number", pattern for digits)
                has_numeric_patterns = any(
                    elem.get("attributes", {}).get("type") == "number"
                    or elem.get("attributes", {}).get("pattern", "").isdigit()
                    or "digit" in elem.get("attributes", {}).get("pattern", "").lower()
                    for elem in input_fields
                )

                if has_maxlength_1 or has_numeric_patterns:
                    return True

            # Check for TOTP-related keywords in page content
            page_text = scraped_page.html.lower() if scraped_page.html else ""
            totp_keywords = [
                "verification code",
                "authentication code",
                "security code",
                "2fa",
                "two-factor",
                "totp",
                "authenticator",
                "verification",
                "enter code",
                "verification number",
                "security number",
            ]

            keyword_matches = sum(1 for keyword in totp_keywords if keyword in page_text)

            # If we have multiple TOTP keywords and multiple input fields, likely TOTP
            if keyword_matches >= 2 and len(input_fields) >= 6:
                return True

            # Strong single keyword match with multiple inputs
            strong_keywords = ["verification code", "authentication code", "2fa", "two-factor"]
            if any(keyword in page_text for keyword in strong_keywords) and len(input_fields) >= 3:
                return True

            return False

        except Exception:
            return False

    def _is_multi_field_totp_sequence(self, actions: list) -> bool:
        """
        Check if the action sequence represents a multi-field TOTP input (6 single-digit fields).

        Args:
            actions: List of actions to analyze

        Returns:
            bool: True if this is a multi-field TOTP sequence
        """
        # Must have at least 4 actions (minimum for TOTP)
        if len(actions) < 4:
            return False

        # Check if we have multiple consecutive single-digit INPUT_TEXT actions
        consecutive_single_digits = 0
        max_consecutive = 0

        for action in actions:
            if (
                action.action_type == ActionType.INPUT_TEXT
                and hasattr(action, "text")
                and action.text
                and len(action.text) == 1
                and action.text.isdigit()
            ):
                consecutive_single_digits += 1
                max_consecutive = max(max_consecutive, consecutive_single_digits)
            else:
                # If we hit a non-single-digit action, reset consecutive counter
                consecutive_single_digits = 0

        # Consider it a multi-field TOTP if we have 4+ consecutive single-digit inputs
        # This is more reliable than just counting total single digits
        # We use 4+ as the threshold to avoid false positives with single TOTP fields
        is_multi_field_totp = max_consecutive >= 4

        if is_multi_field_totp:
            LOG.debug(
                "Detected multi-field TOTP sequence",
                max_consecutive=max_consecutive,
                total_actions=len(actions),
            )

        return is_multi_field_totp

    def _build_navigation_payload(
        self,
        task: Task,
        expire_verification_code: bool = False,
        step: Step | None = None,
        scraped_page: ScrapedPage | None = None,
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
            elif isinstance(final_navigation_payload, list):
                verification_code_dict = str({SPECIAL_FIELD_VERIFICATION_CODE: verification_code})
                if verification_code_dict not in final_navigation_payload:
                    final_navigation_payload.append(verification_code_dict)
                else:
                    LOG.warning(
                        "Verification code already exists in navigation payload",
                        final_navigation_payload=final_navigation_payload,
                    )

            elif final_navigation_payload is None:
                final_navigation_payload = {SPECIAL_FIELD_VERIFICATION_CODE: verification_code}
            else:
                LOG.warning(
                    "Didn't add verification code to navigation payload",
                    final_navigation_payload=final_navigation_payload,
                )
            if expire_verification_code:
                current_context.totp_codes.pop(task.task_id)

        # Store TOTP secrets and provide placeholder TOTP for LLM to see format
        # Only when on a TOTP page to avoid premature processing
        if (
            task.workflow_run_id
            and step
            and isinstance(final_navigation_payload, dict)
            and self._should_process_totp(scraped_page)
        ):
            workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)

            for key, value in list(final_navigation_payload.items()):
                if isinstance(value, dict) and "totp" in value:
                    totp_placeholder = value.get("totp")
                    if totp_placeholder and isinstance(totp_placeholder, str):
                        totp_secret_key = workflow_run_context.totp_secret_value_key(totp_placeholder)
                        totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)

                        if totp_secret:
                            # Store TOTP secret for handler to use during execution
                            current_context = skyvern_context.ensure_context()
                            current_context.totp_codes[f"{task.task_id}_secret"] = totp_secret

                            # Send a placeholder TOTP for the LLM to see the format
                            final_navigation_payload[key]["totp"] = "123456"

        return final_navigation_payload

    async def _get_action_results(self, task: Task, current_step: Step | None = None) -> str:
        return json.dumps(await get_action_history(task=task, current_step=current_step))

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

        # Caches expire based on TTL (1 hour) or can be cleaned up via scheduled job
        # This allows multiple tasks with the same llm_key to share the same cache

        # log the task status as an event
        analytics.capture("skyvern-oss-agent-task-status", {"status": task.status})

        # Add task completion tag to trace
        TraceManager.add_task_completion_tag(task.status.value)
        if need_final_screenshot:
            # Take one last screenshot and create an artifact before closing the browser to see the final state
            # We don't need the artifacts and send the webhook response directly only when there is an issue with the browser
            # initialization. In this case, we don't have any artifacts to send and we can't take final screenshots etc.
            # since the browser is not initialized properly or the proxy is not working.

            browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id)
            if browser_state is not None and await browser_state.get_working_page() is not None:
                try:
                    screenshot = await browser_state.take_fullpage_screenshot()
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=last_step,
                        artifact_type=ArtifactType.SCREENSHOT_FINAL,
                        data=screenshot,
                    )
                except TargetClosedError:
                    LOG.warning(
                        "Failed to take screenshot before sending task response, page is closed",
                    )
                except Exception:
                    LOG.exception("Failed to take screenshot before sending task response")

        if task.organization_id:
            try:
                async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                    context = skyvern_context.current()
                    await app.STORAGE.save_downloaded_files(
                        organization_id=task.organization_id,
                        run_id=context.run_id if context and context.run_id else task.workflow_run_id or task.task_id,
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
            await self.execute_task_webhook(task=task, api_key=api_key)

    async def execute_task_webhook(
        self,
        task: Task,
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
        last_step = await app.DATABASE.get_latest_step(task.task_id, organization_id=task.organization_id)

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

            signed_data = generate_skyvern_webhook_signature(payload=payload_dict, api_key=api_key)

            LOG.info(
                "Sending task response to webhook callback url",
                task_id=task.task_id,
                webhook_callback_url=task.webhook_callback_url,
                payload=signed_data.signed_payload,
                headers=signed_data.headers,
            )

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    task.webhook_callback_url,
                    data=signed_data.signed_payload,
                    headers=signed_data.headers,
                    timeout=httpx.Timeout(30.0),
                )
            if resp.status_code >= 200 and resp.status_code < 300:
                LOG.info(
                    "Webhook sent successfully",
                    task_id=task.task_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_task(
                    task_id=task.task_id,
                    organization_id=task.organization_id,
                    webhook_failure_reason="",
                )
            else:
                LOG.info(
                    "Webhook failed",
                    task_id=task.task_id,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
                await app.DATABASE.update_task(
                    task_id=task.task_id,
                    organization_id=task.organization_id,
                    webhook_failure_reason=f"Webhook failed with status code {resp.status_code}, error message: {resp.text}",
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

        # Get recording url from browser session first,
        # if not found, get the recording url from the first step
        if task.browser_session_id:
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    recordings = await app.STORAGE.get_shared_recordings_in_browser_session(
                        organization_id=task.organization_id,
                        browser_session_id=task.browser_session_id,
                    )
                    # FIXME: we only support one recording for now
                    recording_url = recordings[0].url if recordings else None
            except asyncio.TimeoutError:
                LOG.warning("Timeout getting recordings", browser_session_id=task.browser_session_id)

        if recording_url is None:
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
                    context = skyvern_context.current()
                    downloaded_files = await app.STORAGE.get_downloaded_files(
                        organization_id=task.organization_id,
                        run_id=context.run_id if context and context.run_id else task.workflow_run_id or task.task_id,
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
            diff=update_comparison,
        )

        # Track step duration when step is completed or failed
        if status in [StepStatus.completed, StepStatus.failed]:
            duration_seconds = (datetime.now(UTC) - step.created_at.replace(tzinfo=UTC)).total_seconds()
            LOG.info(
                "Step duration metrics",
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
        webhook_failure_reason: str | None = None,
        errors: list[dict[str, Any]] | None = None,
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
        if errors is not None:
            updates["errors"] = errors
        update_comparison = {
            key: {"old": getattr(task, key), "new": value}
            for key, value in updates.items()
            if getattr(task, key) != value
        }

        # Track task duration when task is completed, failed, or terminated
        if status in [TaskStatus.completed, TaskStatus.failed, TaskStatus.terminated]:
            start_time = task.started_at.replace(tzinfo=UTC) if task.started_at else task.created_at.replace(tzinfo=UTC)
            queued_seconds = (start_time - task.created_at.replace(tzinfo=UTC)).total_seconds()
            duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
            LOG.info(
                "Task duration metrics",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                duration_seconds=duration_seconds,
                queued_seconds=queued_seconds,
                task_status=status,
                organization_id=task.organization_id,
                failure_reason=failure_reason,
            )

        await save_task_logs(task.task_id)
        LOG.info("Updating task in db", task_id=task.task_id, diff=update_comparison)
        return await app.DATABASE.update_task(
            task.task_id,
            organization_id=task.organization_id,
            **updates,
        )

    async def _handle_completed_step_with_parallel_verification(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        page: Page | None,
        browser_state: BrowserState,
        scraped_page: ScrapedPage,
        engine: RunEngine,
        task_block: BaseTaskBlock | None = None,
    ) -> tuple[bool | None, Step | None, Step | None]:
        """
        Handle completed step with parallel verification optimization.

        Runs two tasks in parallel:
        1. Verify if user goal is complete (check-user-goal)
        2. Pre-scrape page for next step

        If goal is complete, cancel pre-scraping and mark task done.
        If goal not complete, use pre-scraped data for next step execution.

        Note: This should only be called when verification is needed (i.e., when
        the standard flow would have called check_user_goal_complete in agent_step).
        """
        LOG.info(
            "Starting parallel user goal verification with speculative extract-actions",
            step_id=step.step_id,
            task_id=task.task_id,
        )

        verification_task = asyncio.create_task(
            self.check_user_goal_complete(
                page=page,
                scraped_page=scraped_page,
                task=task,
                step=step,
            ),
            name=f"verify_goal_{step.step_id}",
        )

        next_step = await app.DATABASE.create_step(
            task_id=task.task_id,
            order=step.order + 1,
            retry_index=0,
            organization_id=task.organization_id,
        )

        LOG.debug(
            "Waiting before launching speculative plan",
            step_id=step.step_id,
            task_id=task.task_id,
        )
        await asyncio.sleep(1.0)

        speculative_task = asyncio.create_task(
            self._speculate_next_step_plan(
                task=task,
                current_step=step,
                next_step=next_step,
                browser_state=browser_state,
                engine=engine,
            ),
            name=f"speculate_next_step_{step.step_id}",
        )

        try:
            complete_action = await verification_task
        except Exception:
            LOG.warning(
                "User goal verification failed in parallel mode, will continue with next step",
                step_id=step.step_id,
                exc_info=True,
            )
            complete_action = None

        if complete_action is not None:
            asyncio.create_task(
                self._persist_speculative_metadata_for_discarded_plan(
                    next_step,
                    speculative_task,
                    cancel_step=True,
                )
            )

            working_page = page or await browser_state.must_get_working_page()

            if step.output is None:
                step.output = AgentStepOutput(action_results=[], actions_and_results=[], errors=[])
            if step.output.action_results is None:
                step.output.action_results = []
            if step.output.actions_and_results is None:
                step.output.actions_and_results = []

            persisted_action = cast(Action, complete_action)
            if isinstance(persisted_action, (CompleteAction, TerminateAction)):
                persisted_action.organization_id = task.organization_id
                persisted_action.workflow_run_id = task.workflow_run_id
                persisted_action.task_id = task.task_id
                persisted_action.step_id = step.step_id
                persisted_action.step_order = step.order
                persisted_action.action_order = len(step.output.actions_and_results)

            action_results = await ActionHandler.handle_action(scraped_page, task, step, working_page, persisted_action)
            await self.record_artifacts_after_action(task, step, browser_state, engine)
            step.output.action_results.extend(action_results)
            step.output.actions_and_results.append((persisted_action, action_results))
            if isinstance(persisted_action, DecisiveAction) and persisted_action.errors:
                step.output.errors.extend(persisted_action.errors)

            if isinstance(persisted_action, TerminateAction):
                LOG.warning(
                    "Parallel verification: termination required, marking task as terminated",
                    step_id=step.step_id,
                    task_id=task.task_id,
                    reasoning=complete_action.reasoning,
                )
                final_status = step.speculative_original_status or StepStatus.completed
                step.speculative_original_status = None
                step.status = final_status
                last_step = await self.update_step(
                    step,
                    status=final_status,
                    output=step.output,
                    is_last=True,
                )
                task_errors = None
                if persisted_action.errors:
                    task_errors = [error.model_dump() for error in persisted_action.errors]
                failure_reason = persisted_action.reasoning
                if persisted_action.errors:
                    failure_reason = "; ".join(error.reasoning for error in persisted_action.errors)
                await self.update_task(
                    task,
                    status=TaskStatus.terminated,
                    failure_reason=failure_reason,
                    errors=task_errors,
                )
                return True, last_step, None

            if isinstance(persisted_action, CompleteAction) and task.navigation_goal and task.data_extraction_goal:
                task = await self._run_data_extraction_after_complete_action(
                    task=task,
                    step=step,
                    scraped_page=scraped_page,
                    working_page=working_page,
                )

            LOG.info(
                "Parallel verification: goal achieved, marking task as completed",
                step_id=step.step_id,
                task_id=task.task_id,
            )
            final_status = step.speculative_original_status or StepStatus.completed
            step.speculative_original_status = None
            step.status = final_status
            last_step = await self.update_step(
                step,
                status=final_status,
                output=step.output,
                is_last=True,
            )
            extracted_information = await self.get_extracted_information_for_task(task)
            await self.update_task(
                task,
                status=TaskStatus.completed,
                extracted_information=extracted_information,
            )
            return True, last_step, None

        LOG.info(
            "Parallel verification: goal not achieved, awaiting speculative extract-actions",
            step_id=step.step_id,
            task_id=task.task_id,
        )

        try:
            speculative_plan = await speculative_task
        except CancelledError:
            LOG.debug("Speculative extract-actions cancelled after verification finished", step_id=step.step_id)
            speculative_plan = None
        except Exception:
            LOG.warning(
                "Speculative extract-actions failed, next step will run sequentially",
                step_id=step.step_id,
                exc_info=True,
            )
            speculative_plan = None

        context = skyvern_context.current()
        override_max_steps_per_run = context.max_steps_override if context else None
        max_steps_per_run = (
            override_max_steps_per_run
            or task.max_steps_per_run
            or organization.max_steps_per_run
            or settings.MAX_STEPS_PER_RUN
        )

        if step.order + 1 >= max_steps_per_run:
            LOG.info(
                "Step completed but max steps reached, marking task as failed",
                step_order=step.order,
                step_retry=step.retry_index,
                max_steps=max_steps_per_run,
            )
            final_status = step.speculative_original_status or StepStatus.completed
            step.speculative_original_status = None
            step.status = final_status
            last_step = await self.update_step(
                step,
                status=final_status,
                output=step.output,
                is_last=True,
            )

            generated_failure_reason = await self.summary_failure_reason_for_max_steps(
                organization=organization,
                task=task,
                step=step,
                page=page,
            )
            failure_reason = f"Reached the maximum steps ({max_steps_per_run}). Possible failure reasons: {generated_failure_reason.reasoning}"
            errors = [ReachMaxStepsError().model_dump()] + [
                error.model_dump() for error in generated_failure_reason.errors
            ]

            await self._cancel_speculative_step(next_step)

            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=failure_reason,
                errors=errors,
            )
            return False, last_step, None

        if speculative_plan:
            context = skyvern_context.ensure_context()
            context.speculative_plans[next_step.step_id] = speculative_plan
            LOG.info(
                "Stored speculative extract-actions plan for next step",
                current_step_id=step.step_id,
                next_step_id=next_step.step_id,
            )

        step.status = step.speculative_original_status or StepStatus.completed
        step.speculative_original_status = None

        return None, None, next_step

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
                errors=[ReachMaxRetriesError().model_dump()],
            )
            return None
        else:
            LOG.warning(
                "Step failed, retrying",
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
    ) -> MaxStepsReasonResponse:
        steps_results = []
        llm_errors: list[str] = []

        try:
            steps = await app.DATABASE.get_task_steps(
                task_id=task.task_id, organization_id=organization.organization_id
            )
            for step_cnt, step in enumerate(steps):
                if step.output is None:
                    continue

                if len(step.output.errors) > 0:
                    failure_reason = ";".join([repr(err) for err in step.output.errors])
                    return MaxStepsReasonResponse(
                        page_info="",
                        reasoning=failure_reason,
                        errors=step.output.errors,
                    )

                if step.output.actions_and_results is None:
                    continue

                action_result_summary: list[str] = []
                step_result: dict[str, Any] = {
                    "order": step_cnt,
                }
                for action, action_results in step.output.actions_and_results:
                    if len(action_results) == 0:
                        continue
                    last_result = action_results[-1]

                    # Check if this is an LLM provider error
                    if not last_result.success:
                        exception_type = last_result.exception_type or ""
                        exception_message = last_result.exception_message or ""
                        if (
                            exception_type in (LLM_PROVIDER_ERROR_TYPE, LLM_PROVIDER_ERROR_RETRYABLE_TASK_TYPE)
                            or "LLMProvider" in exception_message
                        ):
                            llm_errors.append(f"Step {step_cnt}: {exception_message}")

                    action_result_summary.append(
                        f"{action.reasoning}(action_type={action.action_type}, result={'success' if last_result.success else 'failed'})"
                    )
                step_result["actions_result"] = action_result_summary
                steps_results.append(step_result)

            # If we detected LLM errors, return a clear message without calling the LLM
            if llm_errors:
                llm_error_details = "; ".join(llm_errors)
                return MaxStepsReasonResponse(
                    page_info="",
                    reasoning=(
                        f"The task failed due to LLM service errors. The LLM provider encountered errors and was unable to process the requests. "
                        f"This is typically caused by rate limiting, service outages, or resource exhaustion from the LLM provider. "
                        f"Error details: {llm_error_details}"
                    ),
                    errors=[],
                )

            scroll = True
            if await service_utils.is_cua_task(task=task):
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
                error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
                local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            )
            json_response = await app.LLM_API_HANDLER(
                prompt=prompt, screenshots=screenshots, step=step, prompt_name="summarize-max-steps-reason"
            )
            return MaxStepsReasonResponse.model_validate(json_response)
        except Exception:
            LOG.warning("Failed to summary the failure reason")
            # Check if we have LLM errors even if the summarization failed
            if llm_errors:
                llm_error_details = "; ".join(llm_errors)
                return MaxStepsReasonResponse(
                    page_info="",
                    reasoning=(
                        f"The task failed due to LLM service errors. The LLM provider encountered errors and was unable to process the requests. "
                        f"Error details: {llm_error_details}"
                    ),
                    errors=[],
                )
            if steps_results:
                last_step_result = steps_results[-1]
                return MaxStepsReasonResponse(
                    page_info="",
                    reasoning=f"Step {last_step_result['order']}: {last_step_result['actions_result']}",
                    errors=[],
                )
            return MaxStepsReasonResponse(
                page_info="",
                reasoning="",
                errors=[],
            )

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
        llm_errors: list[str] = []
        steps_without_actions = 0

        try:
            steps = await app.DATABASE.get_task_steps(
                task_id=task.task_id, organization_id=organization.organization_id
            )

            # Check for LLM provider errors in the failed steps
            for step_cnt, cur_step in enumerate(steps[-max_retries:]):
                if cur_step.status == StepStatus.failed:
                    # If step failed with no actions, it might be an LLM error during action extraction
                    if not cur_step.output or not cur_step.output.actions_and_results:
                        steps_without_actions += 1

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

                        # Check if this is an LLM provider error
                        exception_type = last_result.exception_type or ""
                        if (
                            exception_type in (LLM_PROVIDER_ERROR_TYPE, LLM_PROVIDER_ERROR_RETRYABLE_TASK_TYPE)
                            or "LLMProvider" in reason
                        ):
                            llm_errors.append(f"Step {step_cnt}: {reason}")

                        action_result_summary.append(
                            f"{action.reasoning}(action_type={action.action_type}, result=failed, reason={reason})"
                        )
                    step_result["actions_result"] = action_result_summary
                    steps_results.append(step_result)

            # If we detected LLM errors, return a clear message without calling the LLM
            if llm_errors:
                llm_error_details = "; ".join(llm_errors)
                return (
                    f"The task failed due to LLM service errors. The LLM provider encountered errors and was unable to process the requests. "
                    f"This is typically caused by rate limiting, service outages, or resource exhaustion from the LLM provider. "
                    f"Error details: {llm_error_details}"
                )

            # If multiple steps failed without producing any actions, it's likely an LLM error during action extraction
            if steps_without_actions >= max_retries:
                return (
                    f"The task failed because all {max_retries} retry attempts failed to generate actions. "
                    f"This is typically caused by LLM service errors during action extraction, such as rate limiting, "
                    f"service outages, or resource exhaustion from the LLM provider. Please check the LLM service status and try again."
                )

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
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt,
                screenshots=screenshots,
                step=step,
                prompt_name="summarize-max-retries-reason",
            )
            return json_response.get("reasoning", "")
        except Exception:
            LOG.warning("Failed to summarize the failure reason for max retries")
            # Check if we have LLM errors even if the summarization failed
            if llm_errors:
                llm_error_details = "; ".join(llm_errors)
                return (
                    f"The task failed due to LLM service errors. The LLM provider encountered errors and was unable to process the requests. "
                    f"Error details: {llm_error_details}"
                )
            # If multiple steps failed without actions during summarization failure, still report it
            if steps_without_actions >= max_retries:
                return (
                    f"The task failed because all {max_retries} retry attempts failed to generate actions. "
                    f"This is typically caused by LLM service errors during action extraction."
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
        browser_state: BrowserState | None = None,
        scraped_page: ScrapedPage | None = None,
        engine: RunEngine = RunEngine.skyvern_v1,
        complete_verification: bool = True,
    ) -> tuple[bool | None, Step | None, Step | None]:
        # Check if parallel verification should be used
        # Only use it when we have the required data AND when verification would normally happen
        task_completes_on_download = task_block and task_block.complete_on_download and task.workflow_run_id
        should_verify = (
            complete_verification
            and not step.is_goal_achieved()
            and not step.is_terminated()
            and not isinstance(task_block, ActionBlock)
            and not task_completes_on_download
            and (task.navigation_goal or task.complete_criterion)
        )

        if should_verify and browser_state and scraped_page:
            disable_user_goal_check = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "DISABLE_USER_GOAL_CHECK",
                task.workflow_run_id if task.workflow_run_id else task.task_id,
                properties={"task_url": task.url, "organization_id": task.organization_id},
            )

            if disable_user_goal_check:
                LOG.info(
                    "User goal verification disabled via feature flag",
                    step_id=step.step_id,
                    task_id=task.task_id,
                )
            else:
                return await self._handle_completed_step_with_parallel_verification(
                    organization=organization,
                    task=task,
                    step=step,
                    page=page,
                    browser_state=browser_state,
                    scraped_page=scraped_page,
                    engine=engine,
                    task_block=task_block,
                )

        if step.is_goal_achieved():
            LOG.info(
                "Step completed and goal achieved, marking task as completed",
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
                step_order=step.order,
                step_retry=step.retry_index,
                max_steps=max_steps_per_run,
            )
            last_step = await self.update_step(step, is_last=True)

            generated_failure_reason = await self.summary_failure_reason_for_max_steps(
                organization=organization,
                task=task,
                step=step,
                page=page,
            )
            failure_reason = f"Reached the maximum steps ({max_steps_per_run}). Possible failure reasons: {generated_failure_reason.reasoning}"
            errors = [ReachMaxStepsError().model_dump()] + [
                error.model_dump() for error in generated_failure_reason.errors
            ]

            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=failure_reason,
                errors=errors,
            )
            return False, last_step, None
        else:
            LOG.info(
                "Step completed, creating next step",
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

    async def handle_potential_OTP_actions(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        browser_state: BrowserState,
        json_response: dict[str, Any],
    ) -> tuple[dict[str, Any], list[Action]]:
        if not task.organization_id:
            return json_response, []

        if not task.totp_verification_url and not task.totp_identifier:
            return json_response, []

        should_verify_by_magic_link = json_response.get("should_verify_by_magic_link")
        place_to_enter_verification_code = json_response.get("place_to_enter_verification_code")
        should_enter_verification_code = json_response.get("should_enter_verification_code")

        if (
            not should_verify_by_magic_link
            and not place_to_enter_verification_code
            and not should_enter_verification_code
        ):
            return json_response, []

        if place_to_enter_verification_code and should_enter_verification_code:
            json_response = await self.handle_potential_verification_code(
                task, step, scraped_page, browser_state, json_response
            )
            actions = parse_actions(task, step.step_id, step.order, scraped_page, json_response["actions"])
            return json_response, actions

        if should_verify_by_magic_link:
            actions = await self.handle_potential_magic_link(task, step, scraped_page, browser_state, json_response)
            return json_response, actions

        return json_response, []

    async def handle_potential_magic_link(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        browser_state: BrowserState,
        json_response: dict[str, Any],
    ) -> list[Action]:
        should_verify_by_magic_link = json_response.get("should_verify_by_magic_link")
        if not should_verify_by_magic_link:
            return []

        LOG.info("Handling magic link verification", task_id=task.task_id)
        otp_value = await poll_otp_value(
            organization_id=task.organization_id,
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            totp_verification_url=task.totp_verification_url,
            totp_identifier=task.totp_identifier,
        )
        if not otp_value or otp_value.get_otp_type() != OTPType.MAGIC_LINK:
            return []

        # always open a new tab to navigate to the magic link
        page = await browser_state.new_page()
        context = skyvern_context.ensure_context()
        context.add_magic_link_page(task.task_id, page)

        return [
            GotoUrlAction(
                reasoning="Navigating to the magic link URL to verify the login",
                intention="Navigating to the magic link URL to verify the login",
                url=otp_value.value,
                organization_id=task.organization_id,
                workflow_run_id=task.workflow_run_id,
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                action_order=0,
                is_magic_link=True,
            ),
        ]

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
            LOG.info("Need verification code")
            workflow_id = workflow_permanent_id = None
            if task.workflow_run_id:
                workflow_run = await app.DATABASE.get_workflow_run(task.workflow_run_id)
                if workflow_run:
                    workflow_id = workflow_run.workflow_id
                    workflow_permanent_id = workflow_run.workflow_permanent_id
            otp_value = await poll_otp_value(
                organization_id=task.organization_id,
                task_id=task.task_id,
                workflow_id=workflow_id,
                workflow_run_id=task.workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                totp_verification_url=task.totp_verification_url,
                totp_identifier=task.totp_identifier,
            )
            if not otp_value or otp_value.get_otp_type() != OTPType.TOTP:
                return json_response

            current_context = skyvern_context.ensure_context()
            current_context.totp_codes[task.task_id] = otp_value.value

            extract_action_prompt, use_caching, prompt_name = await self._build_extract_action_prompt(
                task,
                step,
                browser_state,
                scraped_page,
                verification_code_check=False,
            )
            llm_key_override = task.llm_key
            if await service_utils.is_cua_task(task=task):
                llm_key_override = None
            llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
                llm_key_override, default=app.LLM_API_HANDLER
            )
            # Add caching flag to context for monitoring
            if use_caching:
                context = skyvern_context.current()
                if context:
                    context.use_prompt_caching = True

            return await llm_api_handler(
                prompt=extract_action_prompt,
                step=step,
                screenshots=scraped_page.screenshots,
                prompt_name=prompt_name,
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

        data_extraction_summary_resp = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=prompt, step=step, prompt_name="data-extraction-summary"
        )
        return ExtractAction(
            reasoning=data_extraction_summary_resp.get("summary", "Extracting information from the page"),
            data_extraction_goal=task.data_extraction_goal,
            data_extraction_schema=task.extracted_information_schema,
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

    async def _run_data_extraction_after_complete_action(
        self,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        working_page: Page,
    ) -> Task:
        """
        Run the extraction flow when a task with a data extraction goal completes during parallel verification.
        """
        refreshed_task = await app.DATABASE.get_task(task.task_id, task.organization_id)
        if refreshed_task:
            task = refreshed_task

        extract_action = await self.create_extract_action(task, step, scraped_page)
        extract_results = await ActionHandler.handle_action(scraped_page, task, step, working_page, extract_action)
        await app.AGENT_FUNCTION.post_action_execution(extract_action)

        if step.output is None:
            step.output = AgentStepOutput(action_results=[], actions_and_results=[], errors=[])
        if step.output.action_results is None:
            step.output.action_results = []
        if step.output.actions_and_results is None:
            step.output.actions_and_results = []

        step.output.action_results.extend(extract_results)
        step.output.actions_and_results.append((extract_action, extract_results))

        return task
