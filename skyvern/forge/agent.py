import asyncio
import json
import os
import random
import string
from asyncio.exceptions import CancelledError
from datetime import datetime
from typing import Any, Tuple

import requests
import structlog
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Page

from skyvern import analytics
from skyvern.constants import SCRAPE_TYPE_ORDER, SPECIAL_FIELD_VERIFICATION_CODE, ScrapeType
from skyvern.exceptions import (
    BrowserStateMissingPage,
    EmptyScrapePage,
    FailedToNavigateToUrl,
    FailedToSendWebhook,
    FailedToTakeScreenshot,
    InvalidTaskStatusTransition,
    InvalidWorkflowTaskURLState,
    MissingBrowserStatePage,
    SkyvernException,
    StepTerminationError,
    StepUnableToExecuteError,
    TaskAlreadyCanceled,
    TaskNotFound,
)
from skyvern.forge import app
from skyvern.forge.async_operations import AgentPhase, AsyncOperationPool
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import get_path_for_workflow_download_directory, list_files_in_directory, rename_file
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.core.validators import validate_url
from skyvern.forge.sdk.models import Organization, Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskStatus
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CompleteAction,
    DecisiveAction,
    UserDefinedError,
    WebAction,
    parse_actions,
)
from skyvern.webeye.actions.caching import retrieve_action_plan
from skyvern.webeye.actions.handler import (
    ActionHandler,
    extract_information_for_navigation_goal,
    poll_verification_code,
)
from skyvern.webeye.actions.models import AgentStepOutput, DetailedAgentStepOutput
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
        if SettingsManager.get_settings().ADDITIONAL_MODULES:
            for module in SettingsManager.get_settings().ADDITIONAL_MODULES:
                LOG.info("Loading additional module", module=module)
                __import__(module)
            LOG.info(
                "Additional modules loaded",
                modules=SettingsManager.get_settings().ADDITIONAL_MODULES,
            )
        LOG.info(
            "Initializing ForgeAgent",
            env=SettingsManager.get_settings().ENV,
            execute_all_steps=SettingsManager.get_settings().EXECUTE_ALL_STEPS,
            browser_type=SettingsManager.get_settings().BROWSER_TYPE,
            max_scraping_retries=SettingsManager.get_settings().MAX_SCRAPING_RETRIES,
            video_path=SettingsManager.get_settings().VIDEO_PATH,
            browser_action_timeout_ms=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            max_steps_per_run=SettingsManager.get_settings().MAX_STEPS_PER_RUN,
            long_running_task_warning_ratio=SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO,
            debug_mode=SettingsManager.get_settings().DEBUG_MODE,
        )
        self.async_operation_pool = AsyncOperationPool()

    async def create_task_and_step_from_block(
        self,
        task_block: TaskBlock,
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
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(workflow_run=workflow_run)
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

        task_url = validate_url(task_url)
        task = await app.DATABASE.create_task(
            url=task_url,
            title=task_block.title,
            webhook_callback_url=None,
            totp_verification_url=task_block.totp_verification_url,
            totp_identifier=task_block.totp_identifier,
            navigation_goal=task_block.navigation_goal,
            data_extraction_goal=task_block.data_extraction_goal,
            navigation_payload=navigation_payload,
            organization_id=workflow.organization_id,
            proxy_location=workflow_run.proxy_location,
            extracted_information_schema=task_block.data_schema,
            workflow_run_id=workflow_run.workflow_run_id,
            order=task_order,
            retry=task_retry,
            max_steps_per_run=task_block.max_steps_per_run,
            error_code_mapping=task_block.error_code_mapping,
        )
        LOG.info(
            "Created new task for workflow run",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            task_id=task.task_id,
            url=task.url,
            title=task.title,
            nav_goal=task.navigation_goal,
            data_goal=task.data_extraction_goal,
            error_code_mapping=task.error_code_mapping,
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

    async def create_task(self, task_request: TaskRequest, organization_id: str | None = None) -> Task:
        webhook_callback_url = str(task_request.webhook_callback_url) if task_request.webhook_callback_url else None
        totp_verification_url = str(task_request.totp_verification_url) if task_request.totp_verification_url else None
        task = await app.DATABASE.create_task(
            url=str(task_request.url),
            title=task_request.title,
            webhook_callback_url=webhook_callback_url,
            totp_verification_url=totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_payload,
            organization_id=organization_id,
            proxy_location=task_request.proxy_location,
            extracted_information_schema=task_request.extracted_information_schema,
            error_code_mapping=task_request.error_code_mapping,
        )
        LOG.info(
            "Created new task",
            task_id=task.task_id,
            url=task.url,
            proxy_location=task.proxy_location,
            organization_id=organization_id,
        )
        return task

    def register_async_operations(self, organization: Organization, task: Task, page: Page) -> None:
        operations = app.AGENT_FUNCTION.generate_async_operations(organization, task, page)
        self.async_operation_pool.add_operations(task.task_id, operations)

    async def execute_step(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        api_key: str | None = None,
        workflow_run: WorkflowRun | None = None,
        close_browser_on_completion: bool = True,
        task_block: TaskBlock | None = None,
    ) -> Tuple[Step, DetailedAgentStepOutput | None, Step | None]:
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
            # We don't send task response for now as the task is canceled
            # TODO: shall we send task response here?
            return step, None, None

        context = skyvern_context.current()
        override_max_steps_per_run = context.max_steps_override if context else None
        max_steps_per_run = (
            override_max_steps_per_run
            or task.max_steps_per_run
            or organization.max_steps_per_run
            or SettingsManager.get_settings().MAX_STEPS_PER_RUN
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
            ) = await self._initialize_execution_state(task, step, workflow_run)

            if page := await browser_state.get_working_page():
                self.register_async_operations(organization, task, page)

            step, detailed_output = await self.agent_step(
                task, step, browser_state, organization=organization, task_block=task_block
            )
            task = await self.update_task_errors_from_detailed_output(task, detailed_output)
            retry = False

            if task_block and task_block.complete_on_download and task.workflow_run_id:
                workflow_download_directory = get_path_for_workflow_download_directory(task.workflow_run_id)
                list_files_after = list_files_in_directory(workflow_download_directory)
                if len(list_files_after) > len(list_files_before):
                    files_to_rename = list(set(list_files_after) - set(list_files_before))
                    for file in files_to_rename:
                        random_file_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                        random_file_name = f"download-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{random_file_id}"
                        if task_block.download_suffix:
                            random_file_name = f"{random_file_name}-{task_block.download_suffix}"
                        rename_file(os.path.join(workflow_download_directory, file), random_file_name)

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
                    await self.send_task_response(
                        task=completed_task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
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
                    await self.send_task_response(
                        task=task,
                        last_step=step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
                    )
                    await self.async_operation_pool.remove_task(task.task_id)
                    return step, detailed_output, None
            elif step.status == StepStatus.completed:
                # TODO (kerem): keep the task object uptodate at all times so that send_task_response can just use it
                (
                    is_task_completed,
                    maybe_last_step,
                    maybe_next_step,
                ) = await self.handle_completed_step(organization, task, step)
                if is_task_completed is not None and maybe_last_step:
                    last_step = maybe_last_step
                    await self.send_task_response(
                        task=task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
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

            if retry and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    task_block=task_block,
                )
            elif SettingsManager.get_settings().execute_all_steps() and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    task_block=task_block,
                )
            else:
                LOG.info(
                    "Step executed but continuous execution is disabled.",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    is_cloud_env=SettingsManager.get_settings().is_cloud_environment(),
                    execute_all_steps=SettingsManager.get_settings().execute_all_steps(),
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
        except StepTerminationError as e:
            LOG.warning(
                "Step cannot be executed, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )
            is_task_marked_as_failed = await self.fail_task(task, step, e.message)
            if is_task_marked_as_failed:
                await self.send_task_response(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
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
                await self.send_task_response(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
                    skip_artifacts=True,
                )
            return step, detailed_output, next_step
        except TaskAlreadyCanceled:
            LOG.info(
                "Task is already canceled, stopping execution",
                task_id=task.task_id,
            )
            # We don't send task response for now as the task is canceled
            return step, detailed_output, None
        except InvalidTaskStatusTransition:
            LOG.warning(
                "Invalid task status transition",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            # TODO: shall we send task response here?
            return step, detailed_output, None
        except Exception as e:
            LOG.exception(
                "Got an unexpected exception in step, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
            )

            failure_reason = "unexpected exception"
            if isinstance(e, SkyvernException):
                failure_reason = f"unexpected SkyvernException({e.__class__.__name__})"

            is_task_marked_as_failed = await self.fail_task(task, step, failure_reason)
            if is_task_marked_as_failed:
                await self.send_task_response(
                    task=task,
                    last_step=step,
                    api_key=api_key,
                    close_browser_on_completion=close_browser_on_completion,
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

    async def agent_step(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        organization: Organization | None = None,
        task_block: TaskBlock | None = None,
    ) -> tuple[Step, DetailedAgentStepOutput]:
        detailed_agent_step_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
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
            ) = await self._build_and_record_step_prompt(
                task,
                step,
                browser_state,
                organization,
            )
            detailed_agent_step_output.scraped_page = scraped_page
            detailed_agent_step_output.extract_action_prompt = extract_action_prompt
            json_response = None
            actions: list[Action]

            using_cached_action_plan = False
            if not task.navigation_goal:
                actions = [
                    CompleteAction(
                        reasoning="Task has no navigation goal.",
                        data_extraction_goal=task.data_extraction_goal,
                        organization_id=task.organization_id,
                        task_id=task.task_id,
                        workflow_run_id=task.workflow_run_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=0,
                        confidence_float=1.0,
                    )
                ]
            elif (
                task_block
                and task_block.cache_actions
                and (actions := await retrieve_action_plan(task, step, scraped_page))
            ):
                using_cached_action_plan = True
            else:
                self.async_operation_pool.run_operation(task.task_id, AgentPhase.llm)
                json_response = await app.LLM_API_HANDLER(
                    prompt=extract_action_prompt,
                    step=step,
                    screenshots=scraped_page.screenshots,
                )
                json_response = await self.handle_potential_verification_code(
                    task,
                    step,
                    scraped_page,
                    browser_state,
                    json_response,
                )
                detailed_agent_step_output.llm_response = json_response

                actions = parse_actions(task, step.step_id, step.order, scraped_page, json_response["actions"])

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
                detailed_agent_step_output = DetailedAgentStepOutput(
                    scraped_page=scraped_page,
                    extract_action_prompt=extract_action_prompt,
                    llm_response=json_response,
                    actions=actions,
                    action_results=[],
                    actions_and_results=[],
                    step_exception=None,
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

                self.async_operation_pool.run_operation(task.task_id, AgentPhase.action)
                results = await ActionHandler.handle_action(scraped_page, task, step, browser_state, action)
                detailed_agent_step_output.actions_and_results[action_idx] = (
                    action,
                    results,
                )
                # wait random time between actions to avoid detection
                await asyncio.sleep(random.uniform(1.0, 2.0))
                await self.record_artifacts_after_action(task, step, browser_state)
                for result in results:
                    result.step_retry_number = step.retry_index
                    result.step_order = step.order
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
                    # if the action triggered javascript calls
                    # this action should be the last action this round and do not take more actions.
                    # for now, we're being optimistic and assuming that
                    # js call doesn't have impact on the following actions
                    if results[-1].javascript_triggered:
                        LOG.info(
                            "Action triggered javascript. Stop executing reamaining actions.",
                            action=action,
                        )
                        # stop executing the rest actions
                        break
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
                and app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                    "CHECK_USER_GOAL_SUCCESS_EVERY_STEP",
                    task.workflow_run_id or task.task_id,
                    properties={
                        "organization_id": task.organization_id,
                        "organization_created_at": str(organization.created_at) if organization else None,
                    },
                )
            ):
                LOG.info("Checking if user goal is achieved after re-scraping the page")
                # Check if navigation goal is achieved after re-scraping the page
                new_scraped_page = await self._scrape_with_type(
                    task=task,
                    step=step,
                    browser_state=browser_state,
                    scrape_type=ScrapeType.NORMAL,
                    organization=organization,
                )
                if new_scraped_page is None:
                    LOG.warning("Failed to scrape the page before checking user goal success, skipping check...")
                else:
                    working_page = await browser_state.get_working_page()
                    result_tuple = await self.check_user_goal_success(
                        page=working_page,
                        scraped_page=new_scraped_page,
                        task=task,
                        step=step,
                    )
                    if result_tuple is not None:
                        complete_action, action_results = result_tuple
                        detailed_agent_step_output.actions_and_results.append((complete_action, action_results))
                        await self.record_artifacts_after_action(task, step, browser_state)
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

    @staticmethod
    async def check_user_goal_success(
        page: Page, scraped_page: ScrapedPage, task: Task, step: Step
    ) -> tuple[CompleteAction, list[ActionResult]] | None:
        try:
            verification_prompt = prompt_engine.load_prompt(
                "check-user-goal",
                navigation_goal=task.navigation_goal,
                navigation_payload=task.navigation_payload,
                elements=scraped_page.build_element_tree(ElementTreeFormat.HTML),
            )

            # this prompt is critical to our agent so let's use the primary LLM API handler
            verification_response = await app.LLM_API_HANDLER(prompt=verification_prompt, step=step, screenshots=None)
            if "user_goal_achieved" not in verification_response or "thoughts" not in verification_response:
                LOG.error(
                    "Invalid LLM response for user goal success verification, skipping verification",
                    verification_response=verification_response,
                )
                return None

            user_goal_achieved: bool = verification_response["user_goal_achieved"]
            complete_action = CompleteAction(
                reasoning=verification_response["thoughts"],
                data_extraction_goal=task.data_extraction_goal,
            )
            # We don't want to return a complete action if the user goal is not achieved since we're checking at every step
            if not user_goal_achieved:
                return None

            LOG.info("User goal achieved, executing complete action")
            extracted_data = None
            if complete_action.data_extraction_goal:
                scrape_action_result = await extract_information_for_navigation_goal(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                )
                extracted_data = scrape_action_result.scraped_data

            return complete_action, [ActionSuccess(data=extracted_data)]

        except Exception:
            LOG.error("LLM verification failed for complete action, skipping LLM verification", exc_info=True)
            return None

    async def record_artifacts_after_action(self, task: Task, step: Step, browser_state: BrowserState) -> None:
        working_page = await browser_state.get_working_page()
        if not working_page:
            raise BrowserStateMissingPage()
        try:
            screenshot = await browser_state.take_screenshot(full_page=True)
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

    async def _initialize_execution_state(
        self, task: Task, step: Step, workflow_run: WorkflowRun | None = None
    ) -> tuple[Step, BrowserState, DetailedAgentStepOutput]:
        if workflow_run:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                workflow_run=workflow_run, url=task.url
            )
        else:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_task(task)
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
        organization: Organization | None = None,
    ) -> ScrapedPage | None:
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

        return await scrape_website(
            browser_state,
            task.url,
            app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step, organization=organization),
            scrape_exclude=app.scrape_exclude,
        )

    async def _build_and_record_step_prompt(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        organization: Organization | None = None,
    ) -> tuple[ScrapedPage, str]:
        # start the async tasks while running scrape_website
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
                    organization=organization,
                )
                break
            except FailedToTakeScreenshot as e:
                if idx < len(SCRAPE_TYPE_ORDER) - 1:
                    continue
                LOG.error(
                    "Failed to take screenshot after two normal attempts and reload-page retry",
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
                raise e

        if scraped_page is None:
            raise EmptyScrapePage

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
        extract_action_prompt = await self._build_extract_action_prompt(
            task,
            browser_state,
            element_tree_in_prompt,
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
        browser_state: BrowserState,
        element_tree_in_prompt: str,
        verification_code_check: bool = False,
        expire_verification_code: bool = False,
    ) -> str:
        actions_and_results_str = await self._get_action_results(task)

        # Generate the extract action prompt
        navigation_goal = task.navigation_goal
        starting_url = task.url
        page = await browser_state.get_working_page()
        current_url = await page.evaluate("() => document.location.href") if page else starting_url
        final_navigation_payload = self._build_navigation_payload(
            task, expire_verification_code=expire_verification_code
        )
        return prompt_engine.load_prompt(
            "extract-action",
            navigation_goal=navigation_goal,
            navigation_payload_str=json.dumps(final_navigation_payload),
            starting_url=starting_url,
            current_url=current_url,
            elements=element_tree_in_prompt,
            data_extraction_goal=task.data_extraction_goal,
            action_history=actions_and_results_str,
            error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
            utc_datetime=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            verification_code_check=verification_code_check,
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

    async def _get_action_results(self, task: Task) -> str:
        # Get action results from the last app.SETTINGS.PROMPT_ACTION_HISTORY_WINDOW steps
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        # the last step is always the newly created one and it should be excluded from the history window
        window_steps = steps[-1 - SettingsManager.get_settings().PROMPT_ACTION_HISTORY_WINDOW : -1]
        actions_and_results: list[tuple[Action, list[ActionResult]]] = []
        for window_step in window_steps:
            if window_step.output and window_step.output.actions_and_results:
                actions_and_results.extend(window_step.output.actions_and_results)

        # exclude successful action from history
        return json.dumps(
            [
                {
                    "action": action.model_dump(
                        exclude_none=True,
                        exclude={
                            "text",
                            "confidence_float",
                            "organization_id",
                            "task_id",
                            "step_id",
                            "step_order",
                            "action_order",
                            "skyvern_element_hash",
                            "skyvern_element_data",
                        },
                    ),
                    "results": [
                        result.model_dump(
                            exclude_none=True,
                            exclude={
                                "javascript_triggered",
                                "interacted_with_sibling",
                                "interacted_with_parent",
                                "step_retry_number",
                                "step_order",
                                "stop_execution_on_failure",
                            },
                        )
                        for result in results
                    ],
                }
                for action, results in actions_and_results
                if len(results) > 0
            ]
        )

    async def get_extracted_information_for_task(self, task: Task) -> dict[str, Any] | list | str | None:
        """
        Find the last successful ScrapeAction for the task and return the extracted information.
        """
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
                if action.action_type != ActionType.COMPLETE:
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

    async def send_task_response(
        self,
        task: Task,
        last_step: Step,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        skip_artifacts: bool = False,
        skip_cleanup: bool = False,
    ) -> None:
        """
        send the task response to the webhook callback url
        """
        # refresh the task from the db to get the latest status
        try:
            refreshed_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
            if not refreshed_task:
                LOG.error("Failed to get task from db when sending task response")
                raise TaskNotFound(task_id=task.task_id)
        except Exception as e:
            LOG.error(
                "Failed to get task from db when sending task response",
                task_id=task.task_id,
                error=e,
            )
            raise TaskNotFound(task_id=task.task_id) from e
        task = refreshed_task
        if skip_cleanup:
            await self.execute_task_webhook(task=task, last_step=last_step, api_key=api_key)
            return

        # log the task status as an event
        analytics.capture("skyvern-oss-agent-task-status", {"status": task.status})
        # We skip the artifacts and send the webhook response directly only when there is an issue with the browser
        # initialization. In this case, we don't have any artifacts to send and we can't take final screenshots etc.
        # since the browser is not initialized properly or the proxy is not working.
        if skip_artifacts:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks_for_task(task.task_id)
            await self.execute_task_webhook(task=task, last_step=last_step, api_key=api_key)
            return

        # Take one last screenshot and create an artifact before closing the browser to see the final state
        browser_state: BrowserState = await app.BROWSER_MANAGER.get_or_create_for_task(task)
        await browser_state.get_or_create_page()
        try:
            screenshot = await browser_state.take_screenshot(full_page=True)
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

        if task.workflow_run_id:
            LOG.info(
                "Task is part of a workflow run, not sending a webhook response",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
            )
            return

        await self.cleanup_browser_and_create_artifacts(close_browser_on_completion, last_step, task)

        # Wait for all tasks to complete before generating the links for the artifacts
        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks_for_task(task.task_id)

        await self.execute_task_webhook(task=task, last_step=last_step, api_key=api_key)

    async def execute_task_webhook(
        self,
        task: Task,
        last_step: Step,
        api_key: str | None,
        skip_artifacts: bool = False,
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

        if not skip_artifacts:
            # get the artifact of the screenshot and get the screenshot_url
            screenshot_artifact = await app.DATABASE.get_artifact(
                task_id=task.task_id,
                step_id=last_step.step_id,
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                organization_id=task.organization_id,
            )
            screenshot_url = None
            if screenshot_artifact:
                screenshot_url = await app.ARTIFACT_MANAGER.get_share_link(screenshot_artifact)

            recording_artifact = await app.DATABASE.get_artifact(
                task_id=task.task_id,
                step_id=last_step.step_id,
                artifact_type=ArtifactType.RECORDING,
                organization_id=task.organization_id,
            )
            recording_url = None
            if recording_artifact:
                recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

            # get the artifact of the last TASK_RESPONSE_ACTION_SCREENSHOT_COUNT screenshots and get the screenshot_url
            latest_action_screenshot_artifacts = await app.DATABASE.get_latest_n_artifacts(
                task_id=task.task_id,
                organization_id=task.organization_id,
                artifact_types=[ArtifactType.SCREENSHOT_ACTION],
                n=SettingsManager.get_settings().TASK_RESPONSE_ACTION_SCREENSHOT_COUNT,
            )
            latest_action_screenshot_urls: list[str] | None = []
            if latest_action_screenshot_artifacts:
                latest_action_screenshot_urls = await app.ARTIFACT_MANAGER.get_share_links(
                    latest_action_screenshot_artifacts
                )
            else:
                LOG.error("Failed to get latest action screenshots")

            # get the latest task from the db to get the latest status, extracted_information, and failure_reason
            task_from_db = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
            if not task_from_db:
                LOG.error("Failed to get task from db when sending task response")
                raise TaskNotFound(task_id=task.task_id)

            task = task_from_db
            task_response = task.to_task_response(
                action_screenshot_urls=latest_action_screenshot_urls,
                screenshot_url=screenshot_url,
                recording_url=recording_url,
            )
        else:
            task_response = task.to_task_response()

        if not task.webhook_callback_url:
            LOG.info("Task has no webhook callback url. Not sending task response")
            return

        # send task_response to the webhook callback url
        # TODO: use async requests (httpx)
        timestamp = str(int(datetime.utcnow().timestamp()))
        payload = task_response.model_dump_json(exclude={"request"})
        signature = generate_skyvern_signature(
            payload=payload,
            api_key=api_key,
        )
        headers = {
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        }
        LOG.info(
            "Sending task response to webhook callback url",
            task_id=task.task_id,
            webhook_callback_url=task.webhook_callback_url,
            payload=payload,
            headers=headers,
        )
        try:
            resp = requests.post(task.webhook_callback_url, data=payload, headers=headers)
            if resp.ok:
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

    async def cleanup_browser_and_create_artifacts(
        self, close_browser_on_completion: bool, last_step: Step, task: Task
    ) -> None:
        # We need to close the browser even if there is no webhook callback url or api key
        browser_state = await app.BROWSER_MANAGER.cleanup_for_task(task.task_id, close_browser_on_completion)
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
        LOG.info(
            "Updating step in db",
            task_id=step.task_id,
            step_id=step.step_id,
            diff=update_comparison,
        )
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
            else SettingsManager.get_settings().MAX_RETRIES_PER_STEP
        )
        if step.retry_index >= max_retries_per_step:
            LOG.warning(
                "Step failed after max retries, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                max_retries=SettingsManager.get_settings().MAX_RETRIES_PER_STEP,
            )
            await self.update_task(
                task,
                TaskStatus.failed,
                failure_reason=f"Max retries per step ({max_retries_per_step}) exceeded",
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

    async def handle_completed_step(
        self, organization: Organization, task: Task, step: Step
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
            or SettingsManager.get_settings().MAX_STEPS_PER_RUN
        )
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
            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=f"Max steps per task ({max_steps_per_run}) exceeded",
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

            if step.order == int(
                max_steps_per_run * SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO - 1
            ):
                LOG.info(
                    "Long running task warning",
                    order=step.order,
                    max_steps=max_steps_per_run,
                    warning_ratio=SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO,
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
        need_verification_code = json_response.get("need_verification_code")
        if need_verification_code and (task.totp_verification_url or task.totp_identifier) and task.organization_id:
            LOG.info("Need verification code", step_id=step.step_id)
            verification_code = await poll_verification_code(
                task.task_id,
                task.organization_id,
                totp_verification_url=task.totp_verification_url,
                totp_identifier=task.totp_identifier,
            )
            current_context = skyvern_context.ensure_context()
            current_context.totp_codes[task.task_id] = verification_code

            element_tree_in_prompt: str = scraped_page.build_element_tree(ElementTreeFormat.HTML)
            extract_action_prompt = await self._build_extract_action_prompt(
                task,
                browser_state,
                element_tree_in_prompt,
                verification_code_check=False,
                expire_verification_code=True,
            )
            return await app.LLM_API_HANDLER(
                prompt=extract_action_prompt,
                step=step,
                screenshots=scraped_page.screenshots,
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
