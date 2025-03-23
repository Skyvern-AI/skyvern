import os
import random
import string
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from playwright.async_api import Page
from sqlalchemy.exc import OperationalError

from skyvern.config import settings
from skyvern.exceptions import FailedToSendWebhook, TaskTerminationError, TaskV2NotFound, UrlGenerationFailure
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_headers
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_runs import TaskRunType
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Metadata, TaskV2Status, ThoughtScenario, ThoughtType
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.workflow.models.block import (
    BlockResult,
    BlockStatus,
    BlockTypeVar,
    ExtractionBlock,
    ForLoopBlock,
    NavigationBlock,
    TaskBlock,
    UrlBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, ContextParameter
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.models.yaml import (
    BLOCK_YAML_TYPES,
    PARAMETER_YAML_TYPES,
    ContextParameterYAML,
    ExtractionBlockYAML,
    ForLoopBlockYAML,
    NavigationBlockYAML,
    TaskBlockYAML,
    UrlBlockYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
)
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ElementTreeFormat, ScrapedPage, scrape_website
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
DEFAULT_WORKFLOW_TITLE = "New Workflow"
RANDOM_STRING_POOL = string.ascii_letters + string.digits
DEFAULT_MAX_ITERATIONS = 10

MINI_GOAL_TEMPLATE = """Achieve the following mini goal and once it's achieved, complete: {mini_goal}

This mini goal is part of the big goal the user wants to achieve and use the big goal as context to achieve the mini goal: {main_goal}"""


def _generate_data_extraction_schema_for_loop(loop_values_key: str) -> dict:
    return {
        "type": "object",
        "properties": {
            loop_values_key: {
                "type": "array",
                "description": 'User will later iterate through this array of values to achieve their "big goal" in the web. In each iteration, the user will try to take the same actions in the web but with a different value of its own. If the value is a url link, make sure it is a full url with http/https protocol, domain and path if any, based on the current url. For examples: \n1. When the goal is "Open up to 10 links from an ecomm search result page, and extract information like the price of each product.", user will iterate through an array of product links or URLs. In each iteration, the user will go to the linked page and extrat price information of the product. As a result, the array consists of 10 product urls scraped from the search result page.\n2. When the goal is "download 10 documents found on a page", user will iterate through an array of document names. In each iteration, the user will use a different value variant to start from the same page (the existing page) and take actions based on the variant. As a result, the array consists of up to 10 document names scraped from the page that the user wants to download.',
                "items": {"type": "string", "description": "The relevant value"},
            },
            "is_loop_value_link": {
                "type": "boolean",
                "description": "true if the loop_values is an array of urls to be visited for each task. false if the loop_values is an array of non-link values to be used in each task (for each task they start from the same page / link).",
            },
        },
    }


async def initialize_task_v2(
    organization: Organization,
    user_prompt: str,
    user_url: str | None = None,
    proxy_location: ProxyLocation | None = None,
    totp_identifier: str | None = None,
    totp_verification_url: str | None = None,
    webhook_callback_url: str | None = None,
    publish_workflow: bool = False,
    parent_workflow_run_id: str | None = None,
    create_task_run: bool = False,
) -> TaskV2:
    task_v2 = await app.DATABASE.create_task_v2(
        prompt=user_prompt,
        organization_id=organization.organization_id,
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
        webhook_callback_url=webhook_callback_url,
        proxy_location=proxy_location,
    )
    # set task_v2_id in context
    context = skyvern_context.current()
    if context:
        context.task_v2_id = task_v2.observer_cruise_id

    thought = await app.DATABASE.create_thought(
        task_v2_id=task_v2.observer_cruise_id,
        organization_id=organization.organization_id,
        thought_type=ThoughtType.metadata,
        thought_scenario=ThoughtScenario.generate_metadata,
    )

    metadata_prompt = prompt_engine.load_prompt("task_v2_generate_metadata", user_goal=user_prompt, user_url=user_url)
    metadata_response = await app.LLM_API_HANDLER(
        prompt=metadata_prompt,
        thought=thought,
        prompt_name="task_v2_generate_metadata",
    )
    # validate
    LOG.info(f"Initialized task v2 initial response: {metadata_response}")
    url: str = user_url or metadata_response.get("url", "")
    if not url:
        raise UrlGenerationFailure()
    title: str = metadata_response.get("title", DEFAULT_WORKFLOW_TITLE)
    metadata = TaskV2Metadata(
        url=url,
        workflow_title=title,
    )
    url = metadata.url
    if not url:
        raise UrlGenerationFailure()

    # create workflow and workflow run
    max_steps_override = 10
    try:
        workflow_status = WorkflowStatus.published if publish_workflow else WorkflowStatus.auto_generated
        new_workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(
            organization,
            metadata.workflow_title,
            proxy_location=proxy_location,
            status=workflow_status,
        )
        workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
            request_id=None,
            workflow_request=WorkflowRequestBody(),
            workflow_permanent_id=new_workflow.workflow_permanent_id,
            organization_id=organization.organization_id,
            version=None,
            max_steps_override=max_steps_override,
            parent_workflow_run_id=parent_workflow_run_id,
        )
    except Exception:
        LOG.error("Failed to setup cruise workflow run", exc_info=True)
        # fail the workflow run
        await mark_task_v2_as_failed(
            task_v2_id=task_v2.observer_cruise_id,
            workflow_run_id=task_v2.workflow_run_id,
            failure_reason="Skyvern failed to setup the workflow run",
            organization_id=organization.organization_id,
        )
        raise

    try:
        await app.DATABASE.update_thought(
            thought_id=thought.observer_thought_id,
            organization_id=organization.organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=new_workflow.workflow_id,
            workflow_permanent_id=new_workflow.workflow_permanent_id,
            thought=metadata_response.get("thoughts", ""),
            output=metadata.model_dump(),
        )
    except Exception:
        LOG.warning("Failed to update thought", exc_info=True)

    # update oserver cruise
    try:
        task_v2 = await app.DATABASE.update_task_v2(
            task_v2_id=task_v2.observer_cruise_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=new_workflow.workflow_id,
            workflow_permanent_id=new_workflow.workflow_permanent_id,
            url=url,
            organization_id=organization.organization_id,
        )
        if create_task_run:
            await app.DATABASE.create_task_run(
                task_run_type=TaskRunType.task_v2,
                organization_id=organization.organization_id,
                run_id=task_v2.observer_cruise_id,
                title=new_workflow.title,
                url=url,
                url_hash=generate_url_hash(url),
            )
    except Exception:
        LOG.warning("Failed to update task 2.0", exc_info=True)
        # fail the workflow run
        await mark_task_v2_as_failed(
            task_v2_id=task_v2.observer_cruise_id,
            workflow_run_id=workflow_run.workflow_run_id,
            failure_reason="Skyvern failed to update the task 2.0 after initializing the workflow run",
            organization_id=organization.organization_id,
        )
        raise

    return task_v2


async def run_task_v2(
    organization: Organization,
    task_v2_id: str,
    request_id: str | None = None,
    max_steps_override: str | int | None = None,
    browser_session_id: str | None = None,
) -> TaskV2:
    organization_id = organization.organization_id
    try:
        task_v2 = await app.DATABASE.get_task_v2(task_v2_id, organization_id=organization_id)
    except Exception:
        LOG.error(
            "Failed to get task v2",
            task_v2_id=task_v2_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return await mark_task_v2_as_failed(
            task_v2_id,
            organization_id=organization_id,
            failure_reason="Failed to get task v2",
        )
    if not task_v2:
        LOG.error("Task v2 not found", task_v2_id=task_v2_id, organization_id=organization_id)
        raise TaskV2NotFound(task_v2_id=task_v2_id)

    workflow, workflow_run = None, None
    try:
        workflow, workflow_run, task_v2 = await run_task_v2_helper(
            organization=organization,
            task_v2=task_v2,
            request_id=request_id,
            max_steps_override=max_steps_override,
            browser_session_id=browser_session_id,
        )
    except TaskTerminationError as e:
        task_v2 = await mark_task_v2_as_terminated(
            task_v2_id=task_v2_id,
            workflow_run_id=task_v2.workflow_run_id,
            organization_id=organization_id,
            failure_reason=e.message,
        )
        LOG.info("Task v2 is terminated", task_v2_id=task_v2_id, failure_reason=e.message)
        return task_v2
    except OperationalError:
        LOG.error("Database error when running task v2", exc_info=True)
        task_v2 = await mark_task_v2_as_failed(
            task_v2_id,
            workflow_run_id=task_v2.workflow_run_id,
            failure_reason="Database error when running task 2.0",
            organization_id=organization_id,
        )
    except Exception as e:
        LOG.error("Failed to run task v2", exc_info=True)
        failure_reason = f"Failed to run task 2.0: {str(e)}"
        task_v2 = await mark_task_v2_as_failed(
            task_v2_id,
            workflow_run_id=task_v2.workflow_run_id,
            failure_reason=failure_reason,
            organization_id=organization_id,
        )
    finally:
        if workflow and workflow_run and workflow_run.parent_workflow_run_id is None:
            await app.WORKFLOW_SERVICE.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
                browser_session_id=browser_session_id,
                close_browser_on_completion=browser_session_id is None,
            )
        else:
            LOG.warning("Workflow or workflow run not found")

        skyvern_context.reset()

    return task_v2


async def run_task_v2_helper(
    organization: Organization,
    task_v2: TaskV2,
    request_id: str | None = None,
    max_steps_override: str | int | None = None,
    browser_session_id: str | None = None,
) -> tuple[Workflow, WorkflowRun, TaskV2] | tuple[None, None, TaskV2]:
    organization_id = organization.organization_id
    task_v2_id = task_v2.observer_cruise_id
    if task_v2.status != TaskV2Status.queued:
        LOG.error(
            "Task v2 is not queued. Duplicate task v2",
            task_v2_id=task_v2_id,
            status=task_v2.status,
            organization_id=organization_id,
        )
        return None, None, task_v2
    if not task_v2.url or not task_v2.prompt:
        LOG.error(
            "Task v2 url or prompt not found",
            task_v2_id=task_v2_id,
            organization_id=organization_id,
        )
        return None, None, task_v2
    if not task_v2.workflow_run_id:
        LOG.error(
            "Workflow run id not found in task v2",
            task_v2_id=task_v2_id,
            organization_id=organization_id,
        )
        return None, None, task_v2

    int_max_steps_override = None
    if max_steps_override:
        try:
            int_max_steps_override = int(max_steps_override)
            LOG.info("max_steps_override is set", max_steps=int_max_steps_override)
        except ValueError:
            LOG.info(
                "max_steps_override isn't an integer, won't override",
                max_steps_override=max_steps_override,
            )

    workflow_run_id = task_v2.workflow_run_id

    workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        LOG.error("Workflow run not found", workflow_run_id=workflow_run_id)
        return None, None, task_v2
    else:
        LOG.info("Workflow run found", workflow_run_id=workflow_run_id)

    if workflow_run.status != WorkflowRunStatus.queued:
        LOG.warning("Duplicate workflow run execution", workflow_run_id=workflow_run_id, status=workflow_run.status)
        return None, None, task_v2

    workflow_id = workflow_run.workflow_id
    workflow = await app.WORKFLOW_SERVICE.get_workflow(workflow_id, organization_id=organization_id)
    if not workflow:
        LOG.error("Workflow not found", workflow_id=workflow_id)
        return None, None, task_v2

    ###################### run task v2 ######################

    skyvern_context.set(
        SkyvernContext(
            organization_id=organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            request_id=request_id,
            task_v2_id=task_v2_id,
            browser_session_id=browser_session_id,
        )
    )

    task_v2 = await app.DATABASE.update_task_v2(
        task_v2_id=task_v2_id, organization_id=organization_id, status=TaskV2Status.running
    )
    await app.WORKFLOW_SERVICE.mark_workflow_run_as_running(workflow_run_id=workflow_run.workflow_run_id)
    await _set_up_workflow_context(workflow_id, workflow_run_id, organization)

    url = str(task_v2.url)
    user_prompt = task_v2.prompt
    task_history: list[dict] = []
    yaml_blocks: list[BLOCK_YAML_TYPES] = []
    yaml_parameters: list[PARAMETER_YAML_TYPES] = []

    max_steps = int_max_steps_override or settings.MAX_STEPS_PER_TASK_V2
    for i in range(DEFAULT_MAX_ITERATIONS):
        # validate the task execution
        await app.AGENT_FUNCTION.validate_task_execution(
            organization_id=organization_id,
            task_id=task_v2_id,
            task_version="v2",
        )

        # check the status of the workflow run
        workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id, organization_id=organization_id)
        if not workflow_run:
            LOG.error("Workflow run not found", workflow_run_id=workflow_run_id)
            break

        if workflow_run.status == WorkflowRunStatus.canceled:
            LOG.info(
                "Task v2 is canceled. Stopping task v2",
                workflow_run_id=workflow_run_id,
                task_v2_id=task_v2_id,
            )
            await mark_task_v2_as_canceled(
                task_v2_id=task_v2_id,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            return workflow, workflow_run, task_v2

        LOG.info(f"Task v2 iteration i={i}", workflow_run_id=workflow_run_id, url=url)
        task_type = ""
        plan = ""
        block: BlockTypeVar | None = None
        task_history_record: dict[str, Any] = {}
        context = skyvern_context.ensure_context()

        current_url: str | None = None
        page: Page | None = None
        browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id, workflow_run.parent_workflow_run_id)
        if browser_state:
            page = await browser_state.get_working_page()
            if page:
                current_url = await SkyvernFrame.get_url(page)

        if i == 0 and current_url != url:
            # The first iteration is always a GOTO_URL task
            task_type = "goto_url"
            plan = f"Go to this website: {url}"
            task_history_record = {"type": task_type, "task": plan}
            block, block_yaml_list, parameter_yaml_list = await _generate_goto_url_task(
                workflow_id=workflow_id,
                url=url,
            )
        else:
            try:
                if browser_state is None:
                    browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                        workflow_run=workflow_run,
                        url=url,
                        browser_session_id=browser_session_id,
                    )
                scraped_page = await scrape_website(
                    browser_state,
                    url,
                    app.AGENT_FUNCTION.cleanup_element_tree_factory(),
                    scrape_exclude=app.scrape_exclude,
                )
                element_tree_in_prompt: str = scraped_page.build_element_tree(ElementTreeFormat.HTML)
                if page is None:
                    page = await browser_state.get_working_page()
            except Exception:
                LOG.exception(
                    "Failed to get browser state or scrape website in task v2 iteration", iteration=i, url=url
                )
                continue
            current_url = current_url if current_url else str(await SkyvernFrame.get_url(frame=page) if page else url)

            task_v2_prompt = prompt_engine.load_prompt(
                "task_v2",
                current_url=current_url,
                elements=element_tree_in_prompt,
                user_goal=user_prompt,
                task_history=task_history,
                local_datetime=datetime.now(context.tz_info).isoformat(),
            )
            thought = await app.DATABASE.create_thought(
                task_v2_id=task_v2_id,
                organization_id=organization_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_id=workflow.workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                thought_type=ThoughtType.plan,
                thought_scenario=ThoughtScenario.generate_plan,
            )
            task_v2_response = await app.LLM_API_HANDLER(
                prompt=task_v2_prompt,
                screenshots=scraped_page.screenshots,
                thought=thought,
                prompt_name="task_v2",
            )
            LOG.info(
                "Task v2 response",
                task_v2_response=task_v2_response,
                iteration=i,
                current_url=current_url,
                workflow_run_id=workflow_run_id,
            )
            # see if the user goal has achieved or not
            user_goal_achieved = task_v2_response.get("user_goal_achieved", False)
            observation = task_v2_response.get("page_info", "")
            thoughts: str = task_v2_response.get("thoughts", "")
            plan = task_v2_response.get("plan", "")
            task_type = task_v2_response.get("task_type", "")
            # Create and save task thought
            await app.DATABASE.update_thought(
                thought_id=thought.observer_thought_id,
                organization_id=organization_id,
                thought=thoughts,
                observation=observation,
                answer=plan,
                output={"task_type": task_type, "user_goal_achieved": user_goal_achieved},
            )

            if user_goal_achieved is True:
                LOG.info(
                    "User goal achieved. Workflow run will complete. Task v2 is stopping",
                    iteration=i,
                    workflow_run_id=workflow_run_id,
                )
                task_v2 = await _summarize_task_v2(
                    task_v2=task_v2,
                    task_history=task_history,
                    context=context,
                    screenshots=scraped_page.screenshots,
                )
                break

            if not plan:
                LOG.warning("No plan found in task v2 response", task_v2_response=task_v2_response)
                continue

            # parse task v2 response and run the next task
            if not task_type:
                LOG.error("No task type found in task v2 response", task_v2_response=task_v2_response)
                await mark_task_v2_as_failed(
                    task_v2_id=task_v2_id,
                    workflow_run_id=workflow_run_id,
                    failure_reason="Skyvern failed to generate a task. Please try again later.",
                )
                break

            if task_type == "extract":
                block, block_yaml_list, parameter_yaml_list = await _generate_extraction_task(
                    task_v2=task_v2,
                    workflow_id=workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    workflow_run_id=workflow_run_id,
                    current_url=current_url,
                    element_tree_in_prompt=element_tree_in_prompt,
                    data_extraction_goal=plan,
                    task_history=task_history,
                )
                task_history_record = {"type": task_type, "task": plan}
            elif task_type == "navigate":
                original_url = url if i == 0 else None
                navigation_goal = MINI_GOAL_TEMPLATE.format(main_goal=user_prompt, mini_goal=plan)
                block, block_yaml_list, parameter_yaml_list = await _generate_navigation_task(
                    workflow_id=workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    workflow_run_id=workflow_run_id,
                    original_url=original_url,
                    navigation_goal=navigation_goal,
                    totp_verification_url=task_v2.totp_verification_url,
                    totp_identifier=task_v2.totp_identifier,
                )
                task_history_record = {"type": task_type, "task": plan}
            elif task_type == "loop":
                try:
                    block, block_yaml_list, parameter_yaml_list, extraction_obj, inner_task = await _generate_loop_task(
                        task_v2=task_v2,
                        workflow_id=workflow_id,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        workflow_run_id=workflow_run_id,
                        plan=plan,
                        browser_state=browser_state,
                        original_url=url,
                        scraped_page=scraped_page,
                    )
                    task_history_record = {
                        "type": task_type,
                        "task": plan,
                        "loop_over_values": extraction_obj.get("loop_values"),
                        "task_inside_the_loop": inner_task,
                    }
                except Exception:
                    LOG.exception("Failed to generate loop task")
                    await mark_task_v2_as_failed(
                        task_v2_id=task_v2_id,
                        workflow_run_id=workflow_run_id,
                        failure_reason="Failed to generate the loop.",
                    )
                    break
            else:
                LOG.info("Unsupported task type", task_type=task_type)
                await mark_task_v2_as_failed(
                    task_v2_id=task_v2_id,
                    workflow_run_id=workflow_run_id,
                    failure_reason=f"Unsupported task block type gets generated: {task_type}",
                )
                break

        # generate the extraction task
        block_result = await block.execute_safe(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        task_history_record["status"] = str(block_result.status)
        if block_result.failure_reason:
            task_history_record["reason"] = block_result.failure_reason

        extracted_data = _get_extracted_data_from_block_result(
            block_result,
            task_type,
            task_v2_id=task_v2_id,
            workflow_run_id=workflow_run_id,
        )
        if extracted_data is not None:
            task_history_record["extracted_data"] = extracted_data
        task_history.append(task_history_record)
        # refresh workflow
        yaml_blocks.extend(block_yaml_list)
        yaml_parameters.extend(parameter_yaml_list)

        # Update workflow definition
        workflow_definition_yaml = WorkflowDefinitionYAML(
            parameters=yaml_parameters,
            blocks=yaml_blocks,
        )
        workflow_create_request = WorkflowCreateYAMLRequest(
            title=workflow.title,
            description=workflow.description,
            proxy_location=task_v2.proxy_location or ProxyLocation.RESIDENTIAL,
            workflow_definition=workflow_definition_yaml,
            status=workflow.status,
        )
        LOG.info("Creating workflow from request", workflow_create_request=workflow_create_request)
        workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=organization,
            request=workflow_create_request,
            workflow_permanent_id=workflow.workflow_permanent_id,
        )
        LOG.info("Workflow created", workflow_id=workflow.workflow_id)

        # execute the extraction task
        workflow_run = await handle_block_result(
            task_v2_id,
            block,
            block_result,
            workflow,
            workflow_run,
            browser_session_id=browser_session_id,
        )
        if workflow_run.status != WorkflowRunStatus.running:
            LOG.info(
                "Workflow run is not running anymore, stopping the task v2",
                workflow_run_id=workflow_run_id,
                status=workflow_run.status,
            )
            break
        if block_result.success is True:
            completion_screenshots = []
            try:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url=url,
                    browser_session_id=browser_session_id,
                )
                scraped_page = await scrape_website(
                    browser_state,
                    url,
                    app.AGENT_FUNCTION.cleanup_element_tree_factory(),
                    scrape_exclude=app.scrape_exclude,
                )
                completion_screenshots = scraped_page.screenshots
            except Exception:
                LOG.warning("Failed to scrape the website for task v2 completion check")

            # validate completion only happens at the last iteration
            task_v2_completion_prompt = prompt_engine.load_prompt(
                "task_v2_check_completion",
                user_goal=user_prompt,
                task_history=task_history,
                local_datetime=datetime.now(context.tz_info).isoformat(),
            )
            thought = await app.DATABASE.create_thought(
                task_v2_id=task_v2_id,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                thought_type=ThoughtType.user_goal_check,
                thought_scenario=ThoughtScenario.user_goal_check,
            )
            completion_resp = await app.LLM_API_HANDLER(
                prompt=task_v2_completion_prompt,
                screenshots=completion_screenshots,
                thought=thought,
                prompt_name="task_v2_check_completion",
            )
            LOG.info(
                "Task v2 completion check response",
                completion_resp=completion_resp,
                iteration=i,
                workflow_run_id=workflow_run_id,
                task_history=task_history,
            )
            user_goal_achieved = completion_resp.get("user_goal_achieved", False)
            thought_content = completion_resp.get("thoughts", "")
            await app.DATABASE.update_thought(
                thought_id=thought.observer_thought_id,
                organization_id=organization_id,
                thought=thought_content,
                output={"user_goal_achieved": user_goal_achieved},
            )
            if user_goal_achieved:
                LOG.info(
                    "User goal achieved according to the task v2 completion check",
                    iteration=i,
                    workflow_run_id=workflow_run_id,
                    completion_resp=completion_resp,
                )
                task_v2 = await _summarize_task_v2(
                    task_v2=task_v2,
                    task_history=task_history,
                    context=context,
                    screenshots=completion_screenshots,
                )
                break

        # total step number validation
        workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        total_step_count = await app.DATABASE.get_total_unique_step_order_count_by_task_ids(
            task_ids=[task.task_id for task in workflow_run_tasks],
            organization_id=organization_id,
        )
        if total_step_count >= max_steps:
            LOG.info("Task v2 failed - run out of steps", max_steps=max_steps, workflow_run_id=workflow_run_id)
            await mark_task_v2_as_failed(
                task_v2_id=task_v2_id,
                workflow_run_id=workflow_run_id,
                failure_reason=f'Reached the max number of {max_steps} steps. If you need more steps, update the "Max Steps Override" configuration when running the task. Or add/update the "x-max-steps-override" header with your desired number of steps in the API request.',
                organization_id=organization_id,
            )
            return workflow, workflow_run, task_v2
    else:
        LOG.info(
            "Task v2 failed - run out of iterations",
            max_iterations=DEFAULT_MAX_ITERATIONS,
            max_steps=max_steps,
            workflow_run_id=workflow_run_id,
        )
        task_v2 = await mark_task_v2_as_failed(
            task_v2_id=task_v2_id,
            workflow_run_id=workflow_run_id,
            # TODO: add a better failure reason with LLM
            failure_reason="Max iterations reached",
            organization_id=organization_id,
        )

    return workflow, workflow_run, task_v2


async def handle_block_result(
    task_v2_id: str,
    block: BlockTypeVar,
    block_result: BlockResult,
    workflow: Workflow,
    workflow_run: WorkflowRun,
    is_last_block: bool = True,
    browser_session_id: str | None = None,
) -> WorkflowRun:
    workflow_run_id = workflow_run.workflow_run_id
    if block_result.status == BlockStatus.canceled:
        LOG.info(
            "Block with type {block.block_type} was canceled for workflow run {workflow_run_id}, cancelling workflow run",
            block_type=block.block_type,
            workflow_run_id=workflow_run.workflow_run_id,
            block_result=block_result,
            block_type_var=block.block_type,
            block_label=block.label,
        )
        await mark_task_v2_as_canceled(
            task_v2_id=task_v2_id,
            workflow_run_id=workflow_run_id,
            organization_id=workflow_run.organization_id,
        )

    elif block_result.status == BlockStatus.failed:
        LOG.error(
            f"Block with type {block.block_type} failed for workflow run {workflow_run_id}",
            block_type=block.block_type,
            workflow_run_id=workflow_run.workflow_run_id,
            block_result=block_result,
            block_type_var=block.block_type,
            block_label=block.label,
        )
        if block.continue_on_failure and not is_last_block:
            LOG.warning(
                f"Block with type {block.block_type} failed but will continue executing the workflow run {workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run.workflow_run_id,
                block_result=block_result,
                continue_on_failure=block.continue_on_failure,
                block_type_var=block.block_type,
                block_label=block.label,
            )
        # task v2 will continue running the workflow
    elif block_result.status == BlockStatus.terminated:
        LOG.info(
            f"Block with type {block.block_type} was terminated for workflow run {workflow_run_id}",
            block_type=block.block_type,
            workflow_run_id=workflow_run.workflow_run_id,
            block_result=block_result,
            block_type_var=block.block_type,
            block_label=block.label,
        )
        if block.continue_on_failure and not is_last_block:
            LOG.warning(
                f"Block with type {block.block_type} was terminated for workflow run {workflow_run_id}, but will continue executing the workflow run",
                block_type=block.block_type,
                workflow_run_id=workflow_run.workflow_run_id,
                block_result=block_result,
                continue_on_failure=block.continue_on_failure,
                block_type_var=block.block_type,
                block_label=block.label,
            )
    # refresh workflow run model
    return await app.WORKFLOW_SERVICE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=workflow_run.organization_id,
    )


async def _set_up_workflow_context(workflow_id: str, workflow_run_id: str, organization: Organization) -> None:
    """
    TODO: see if we could remove this function as we can just set an empty workflow context
    """
    # Get all <workflow parameter, workflow run parameter> tuples
    wp_wps_tuples = await app.WORKFLOW_SERVICE.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run_id)
    workflow_output_parameters = await app.WORKFLOW_SERVICE.get_workflow_output_parameters(workflow_id=workflow_id)
    await app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
        organization,
        workflow_run_id,
        wp_wps_tuples,
        workflow_output_parameters,
        [],
        [],
    )


async def _generate_loop_task(
    task_v2: TaskV2,
    workflow_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    plan: str,
    browser_state: BrowserState,
    original_url: str,
    scraped_page: ScrapedPage,
) -> tuple[ForLoopBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES], dict[str, Any], dict[str, Any]]:
    for_loop_parameter_yaml_list: list[PARAMETER_YAML_TYPES] = []
    loop_value_extraction_goal = prompt_engine.load_prompt(
        "task_v2_loop_task_extraction_goal",
        plan=plan,
    )
    data_extraction_thought = f"Going to generate a list of values to go through based on the plan: {plan}."
    thought = await app.DATABASE.create_thought(
        task_v2_id=task_v2.observer_cruise_id,
        organization_id=task_v2.organization_id,
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
        thought_type=ThoughtType.plan,
        thought_scenario=ThoughtScenario.extract_loop_values,
        thought=data_extraction_thought,
    )
    # generate screenshot artifact for the thought
    if scraped_page.screenshots:
        for screenshot in scraped_page.screenshots:
            await app.ARTIFACT_MANAGER.create_thought_artifact(
                thought=thought,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
                data=screenshot,
            )
    loop_random_string = _generate_random_string()
    label = f"extraction_task_for_loop_{loop_random_string}"
    loop_values_key = f"loop_values_{loop_random_string}"
    extraction_block_yaml = ExtractionBlockYAML(
        label=label,
        data_extraction_goal=loop_value_extraction_goal,
        data_schema=_generate_data_extraction_schema_for_loop(loop_values_key),
    )
    loop_value_extraction_output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=extraction_block_yaml,
    )
    extraction_block_for_loop = ExtractionBlock(
        label=label,
        data_extraction_goal=loop_value_extraction_goal,
        data_schema=_generate_data_extraction_schema_for_loop(loop_values_key),
        output_parameter=loop_value_extraction_output_parameter,
    )

    # execute the extraction block
    extraction_block_result = await extraction_block_for_loop.execute_safe(
        workflow_run_id=workflow_run_id,
        organization_id=task_v2.organization_id,
    )
    LOG.info("Extraction block result", extraction_block_result=extraction_block_result)
    if extraction_block_result.success is False:
        LOG.error(
            "Failed to execute the extraction block for the loop task",
            extraction_block_result=extraction_block_result,
        )
        # wofklow run and task v2 status update is handled in the upper caller layer
        raise Exception("extraction_block failed")
    # validate output parameter
    try:
        output_value_obj: dict[str, Any] = extraction_block_result.output_parameter_value.get("extracted_information")  # type: ignore
        if not output_value_obj or not isinstance(output_value_obj, dict):
            raise Exception("Invalid output parameter of the extraction block for the loop task")
        if loop_values_key not in output_value_obj:
            raise Exception("loop_values_key not found in the output parameter of the extraction block")
        if "is_loop_value_link" not in output_value_obj:
            raise Exception("is_loop_value_link not found in the output parameter of the extraction block")
        loop_values = output_value_obj.get(loop_values_key, [])
        is_loop_value_link = output_value_obj.get("is_loop_value_link")
    except Exception:
        LOG.error(
            "Failed to validate the output parameter of the extraction block for the loop task",
            extraction_block_result=extraction_block_result,
        )
        raise

    # update the thought
    await app.DATABASE.update_thought(
        thought_id=thought.observer_thought_id,
        organization_id=task_v2.organization_id,
        output=output_value_obj,
    )

    # create ContextParameter for the loop over pointer that ForLoopBlock needs.
    loop_for_context_parameter = ContextParameter(
        key=loop_values_key,
        source=loop_value_extraction_output_parameter,
    )
    for_loop_parameter_yaml_list.append(
        ContextParameterYAML(
            key=loop_for_context_parameter.key,
            description=loop_for_context_parameter.description,
            source_parameter_key=loop_value_extraction_output_parameter.key,
        )
    )
    app.WORKFLOW_CONTEXT_MANAGER.add_context_parameter(workflow_run_id, loop_for_context_parameter)
    await app.WORKFLOW_CONTEXT_MANAGER.set_parameter_values_for_output_parameter_dependent_blocks(
        workflow_run_id=workflow_run_id,
        output_parameter=loop_value_extraction_output_parameter,
        value=extraction_block_result.output_parameter_value,
    )
    task_parameters: list[PARAMETER_TYPE] = []
    if is_loop_value_link is True:
        LOG.info("Loop values are links", loop_values=loop_values)
        context_parameter_key = url = f"task_in_loop_url_{loop_random_string}"
    else:
        LOG.info("Loop values are not links", loop_values=loop_values)
        page = await browser_state.get_working_page()
        url = str(
            await SkyvernFrame.evaluate(frame=page, expression="() => document.location.href") if page else original_url
        )
        context_parameter_key = "target"

    # create ContextParameter for the value
    url_value_context_parameter = ContextParameter(
        key=context_parameter_key,
        source=loop_for_context_parameter,
    )
    task_parameters.append(url_value_context_parameter)
    for_loop_parameter_yaml_list.append(
        ContextParameterYAML(
            key=url_value_context_parameter.key,
            description=url_value_context_parameter.description,
            source_parameter_key=loop_for_context_parameter.key,
        )
    )
    app.WORKFLOW_CONTEXT_MANAGER.add_context_parameter(workflow_run_id, url_value_context_parameter)

    task_in_loop_label = f"task_in_loop_{_generate_random_string()}"
    context = skyvern_context.ensure_context()
    task_in_loop_metadata_prompt = prompt_engine.load_prompt(
        "task_v2_generate_task_block",
        plan=plan,
        local_datetime=datetime.now(context.tz_info).isoformat(),
        is_link=is_loop_value_link,
        loop_values=loop_values,
    )
    thought_task_in_loop = await app.DATABASE.create_thought(
        task_v2_id=task_v2.observer_cruise_id,
        organization_id=task_v2.organization_id,
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
        thought_type=ThoughtType.internal_plan,
        thought_scenario=ThoughtScenario.generate_task_in_loop,
    )
    task_in_loop_metadata_response = await app.LLM_API_HANDLER(
        task_in_loop_metadata_prompt,
        screenshots=scraped_page.screenshots,
        thought=thought_task_in_loop,
        prompt_name="task_v2_generate_task_block",
    )
    LOG.info("Task in loop metadata response", task_in_loop_metadata_response=task_in_loop_metadata_response)
    navigation_goal = task_in_loop_metadata_response.get("navigation_goal")
    data_extraction_goal = task_in_loop_metadata_response.get("data_extraction_goal")
    data_extraction_schema = task_in_loop_metadata_response.get("data_schema")
    thought_content = task_in_loop_metadata_response.get("thoughts")
    await app.DATABASE.update_thought(
        thought_id=thought_task_in_loop.observer_thought_id,
        organization_id=task_v2.organization_id,
        thought=thought_content,
        output=task_in_loop_metadata_response,
    )
    if data_extraction_goal and navigation_goal:
        navigation_goal = (
            navigation_goal
            + " Optimize for extracting as much data as possible. Complete when most data is seen even if some data is partially missing."
        )
    block_yaml = TaskBlockYAML(
        label=task_in_loop_label,
        url=url,
        title=task_in_loop_label,
        navigation_goal=navigation_goal,
        data_extraction_goal=data_extraction_goal,
        data_schema=data_extraction_schema,
        parameter_keys=[param.key for param in task_parameters],
        continue_on_failure=True,
    )
    block_yaml_output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=block_yaml,
    )
    task_in_loop_block = TaskBlock(
        label=task_in_loop_label,
        url=url,
        title=task_in_loop_label,
        navigation_goal=navigation_goal,
        data_extraction_goal=data_extraction_goal,
        data_schema=data_extraction_schema,
        output_parameter=block_yaml_output_parameter,
        parameters=task_parameters,
        continue_on_failure=True,
    )

    # use the output parameter of the extraction block to create the for loop block
    for_loop_yaml = ForLoopBlockYAML(
        label=f"loop_{_generate_random_string()}",
        loop_over_parameter_key=loop_for_context_parameter.key,
        loop_blocks=[block_yaml],
    )
    output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=for_loop_yaml,
    )
    return (
        ForLoopBlock(
            label=for_loop_yaml.label,
            # TODO: this loop over parameter needs to be a context parameter
            loop_over=loop_for_context_parameter,
            loop_blocks=[task_in_loop_block],
            output_parameter=output_parameter,
        ),
        [extraction_block_yaml, for_loop_yaml],
        for_loop_parameter_yaml_list,
        output_value_obj,
        {
            "inner_task_label": task_in_loop_block.label,
            "inner_task_navigation_goal": navigation_goal,
            "inner_task_data_extraction_goal": data_extraction_goal,
        },
    )


async def _generate_extraction_task(
    task_v2: TaskV2,
    workflow_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    current_url: str,
    element_tree_in_prompt: str,
    data_extraction_goal: str,
    task_history: list[dict] | None = None,
) -> tuple[ExtractionBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES]]:
    LOG.info("Generating extraction task", data_extraction_goal=data_extraction_goal, current_url=current_url)
    # extract the data
    context = skyvern_context.ensure_context()
    generate_extraction_task_prompt = prompt_engine.load_prompt(
        "task_v2_generate_extraction_task",
        current_url=current_url,
        elements=element_tree_in_prompt,
        data_extraction_goal=data_extraction_goal,
        local_datetime=datetime.now(context.tz_info).isoformat(),
    )
    generate_extraction_task_response = await app.LLM_API_HANDLER(
        generate_extraction_task_prompt,
        task_v2=task_v2,
        prompt_name="task_v2_generate_extraction_task",
    )
    LOG.info("Data extraction response", data_extraction_response=generate_extraction_task_response)

    # create OutputParameter for the data_extraction block
    data_schema: dict[str, Any] | list | None = generate_extraction_task_response.get("schema")
    label = f"data_extraction_{_generate_random_string()}"
    url: str | None = None
    if not task_history:
        # data extraction is the very first block
        url = current_url
    extraction_block_yaml = ExtractionBlockYAML(
        label=label,
        data_extraction_goal=data_extraction_goal,
        data_schema=data_schema,
        url=url,
    )
    output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=extraction_block_yaml,
    )
    # create ExtractionBlock
    return (
        ExtractionBlock(
            label=label,
            url=url,
            data_extraction_goal=data_extraction_goal,
            data_schema=data_schema,
            output_parameter=output_parameter,
        ),
        [extraction_block_yaml],
        [],
    )


async def _generate_navigation_task(
    workflow_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    navigation_goal: str,
    original_url: str | None = None,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
) -> tuple[NavigationBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES]]:
    LOG.info("Generating navigation task", navigation_goal=navigation_goal, original_url=original_url)
    label = f"navigation_{_generate_random_string()}"
    navigation_block_yaml = NavigationBlockYAML(
        label=label,
        url=original_url,
        navigation_goal=navigation_goal,
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
    )
    output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=navigation_block_yaml,
    )
    return (
        NavigationBlock(
            label=label,
            url=original_url,
            navigation_goal=navigation_goal,
            totp_verification_url=totp_verification_url,
            totp_identifier=totp_identifier,
            output_parameter=output_parameter,
        ),
        [navigation_block_yaml],
        [],
    )


async def _generate_goto_url_task(
    workflow_id: str,
    url: str,
) -> tuple[UrlBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES]]:
    LOG.info("Generating goto url task", url=url)
    # create OutputParameter for the data_extraction block
    label = f"goto_url_{_generate_random_string()}"

    url_block_yaml = UrlBlockYAML(
        label=label,
        url=url,
    )
    output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=url_block_yaml,
    )
    # create UrlBlock
    return (
        UrlBlock(
            label=label,
            url=url,
            output_parameter=output_parameter,
        ),
        [url_block_yaml],
        [],
    )


def _generate_random_string(length: int = 5) -> str:
    # Use the current timestamp as the seed
    random.seed(os.urandom(16))
    return "".join(random.choices(RANDOM_STRING_POOL, k=length))


async def get_thought_timelines(
    task_v2_id: str,
    organization_id: str | None = None,
) -> list[WorkflowRunTimeline]:
    thoughts = await app.DATABASE.get_thoughts(
        task_v2_id,
        organization_id=organization_id,
        thought_types=[
            ThoughtType.plan,
            ThoughtType.user_goal_check,
        ],
    )
    return [
        WorkflowRunTimeline(
            type=WorkflowRunTimelineType.thought,
            thought=thought,
            created_at=thought.created_at,
            modified_at=thought.modified_at,
        )
        for thought in thoughts
    ]


async def get_task_v2(task_v2_id: str, organization_id: str | None = None) -> TaskV2 | None:
    return await app.DATABASE.get_task_v2(task_v2_id, organization_id=organization_id)


async def mark_task_v2_as_failed(
    task_v2_id: str,
    workflow_run_id: str | None = None,
    failure_reason: str | None = None,
    organization_id: str | None = None,
) -> TaskV2:
    task_v2 = await app.DATABASE.update_task_v2(
        task_v2_id,
        organization_id=organization_id,
        status=TaskV2Status.failed,
    )
    if workflow_run_id:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
            workflow_run_id, failure_reason=failure_reason or "Skyvern task 2.0 failed"
        )
    await send_task_v2_webhook(task_v2)
    return task_v2


async def mark_task_v2_as_completed(
    task_v2_id: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
    summary: str | None = None,
    output: dict[str, Any] | None = None,
) -> TaskV2:
    task_v2 = await app.DATABASE.update_task_v2(
        task_v2_id,
        organization_id=organization_id,
        status=TaskV2Status.completed,
        summary=summary,
        output=output,
    )
    if workflow_run_id:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_completed(workflow_run_id)

    # Track task v2 duration when completed
    duration_seconds = (datetime.now(UTC) - task_v2.created_at.replace(tzinfo=UTC)).total_seconds()
    LOG.info(
        "Task v2 duration metrics",
        task_v2_id=task_v2_id,
        workflow_run_id=workflow_run_id,
        duration_seconds=duration_seconds,
        task_v2_status=TaskV2Status.completed,
        organization_id=organization_id,
    )

    await send_task_v2_webhook(task_v2)
    return task_v2


async def mark_task_v2_as_canceled(
    task_v2_id: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> TaskV2:
    task_v2 = await app.DATABASE.update_task_v2(
        task_v2_id,
        organization_id=organization_id,
        status=TaskV2Status.canceled,
    )
    if workflow_run_id:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(workflow_run_id)
    await send_task_v2_webhook(task_v2)
    return task_v2


async def mark_task_v2_as_terminated(
    task_v2_id: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
    failure_reason: str | None = None,
) -> TaskV2:
    task_v2 = await app.DATABASE.update_task_v2(
        task_v2_id,
        organization_id=organization_id,
        status=TaskV2Status.terminated,
    )
    if workflow_run_id:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_terminated(workflow_run_id, failure_reason)
    await send_task_v2_webhook(task_v2)
    return task_v2


def _get_extracted_data_from_block_result(
    block_result: BlockResult,
    task_type: str,
    task_v2_id: str | None = None,
    workflow_run_id: str | None = None,
) -> Any | None:
    """Extract data from block result based on task type.

    Args:
        block_result: The result from block execution
        task_type: Type of task ("extract" or "loop")
        task_v2_id: Optional ID for logging
        workflow_run_id: Optional ID for logging

    Returns:
        Extracted data if available, None otherwise
    """
    if task_type == "extract":
        if (
            isinstance(block_result.output_parameter_value, dict)
            and "extracted_information" in block_result.output_parameter_value
            and block_result.output_parameter_value["extracted_information"]
        ):
            return block_result.output_parameter_value["extracted_information"]
    elif task_type == "loop":
        # if loop task has data extraction, add it to the task history
        # WARNING: the assumption here is that the output_paremeter_value is a list of list of dicts
        #          output_parameter_value data structure is not consistent across all the blocks
        if block_result.output_parameter_value and isinstance(block_result.output_parameter_value, list):
            loop_output_overall = []
            for inner_loop_output in block_result.output_parameter_value:
                inner_loop_output_overall = []
                if not isinstance(inner_loop_output, list):
                    LOG.warning(
                        "Inner loop output is not a list",
                        inner_loop_output=inner_loop_output,
                        task_v2_id=task_v2_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=block_result.workflow_run_block_id,
                    )
                    continue
                for inner_output in inner_loop_output:
                    if not isinstance(inner_output, dict):
                        LOG.warning(
                            "inner output is not a dict",
                            inner_output=inner_output,
                            task_v2_id=task_v2_id,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=block_result.workflow_run_block_id,
                        )
                        continue
                    output_value = inner_output.get("output_value", {})
                    if not isinstance(output_value, dict):
                        LOG.warning(
                            "output_value is not a dict",
                            output_value=output_value,
                            task_v2_id=task_v2_id,
                            workflow_run_id=workflow_run_id,
                            workflow_run_block_id=block_result.workflow_run_block_id,
                        )
                        continue
                    else:
                        if "extracted_information" in output_value and output_value["extracted_information"]:
                            inner_loop_output_overall.append(output_value["extracted_information"])
                loop_output_overall.append(inner_loop_output_overall)
            return loop_output_overall if loop_output_overall else None
    return None


async def _summarize_task_v2(
    task_v2: TaskV2,
    task_history: list[dict],
    context: SkyvernContext,
    screenshots: list[bytes] | None = None,
) -> TaskV2:
    thought = await app.DATABASE.create_thought(
        task_v2_id=task_v2.observer_cruise_id,
        organization_id=task_v2.organization_id,
        workflow_run_id=task_v2.workflow_run_id,
        workflow_id=task_v2.workflow_id,
        workflow_permanent_id=task_v2.workflow_permanent_id,
        thought_type=ThoughtType.user_goal_check,
        thought_scenario=ThoughtScenario.summarization,
    )
    # summarize the task v2 and format the output
    task_v2_summary_prompt = prompt_engine.load_prompt(
        "task_v2_summary",
        user_goal=task_v2.prompt,
        task_history=task_history,
        local_datetime=datetime.now(context.tz_info).isoformat(),
    )
    task_v2_summary_resp = await app.LLM_API_HANDLER(
        prompt=task_v2_summary_prompt,
        screenshots=screenshots,
        thought=thought,
        prompt_name="task_v2_summary",
    )
    LOG.info("Task v2 summary response", task_v2_summary_resp=task_v2_summary_resp)

    summary_description = task_v2_summary_resp.get("description")
    summarized_output = task_v2_summary_resp.get("output")
    await app.DATABASE.update_thought(
        thought_id=thought.observer_thought_id,
        organization_id=task_v2.organization_id,
        thought=summary_description,
        output=task_v2_summary_resp,
    )

    return await mark_task_v2_as_completed(
        task_v2_id=task_v2.observer_cruise_id,
        workflow_run_id=task_v2.workflow_run_id,
        organization_id=task_v2.organization_id,
        summary=summary_description,
        output=summarized_output,
    )


async def send_task_v2_webhook(task_v2: TaskV2) -> None:
    if not task_v2.webhook_callback_url:
        return
    organization_id = task_v2.organization_id
    if not organization_id:
        return
    api_key = await app.DATABASE.get_valid_org_auth_token(
        organization_id,
        OrganizationAuthTokenType.api,
    )
    if not api_key:
        LOG.warning(
            "No valid API key found for the organization of task v2",
            task_v2_id=task_v2.observer_cruise_id,
        )
        return
    # build the task v2 response
    payload = task_v2.model_dump_json(by_alias=True)
    headers = generate_skyvern_webhook_headers(payload=payload, api_key=api_key.token)
    LOG.info(
        "Sending task v2 response to webhook callback url",
        task_v2_id=task_v2.observer_cruise_id,
        webhook_callback_url=task_v2.webhook_callback_url,
        payload=payload,
        headers=headers,
    )
    try:
        resp = await httpx.AsyncClient().post(
            task_v2.webhook_callback_url, data=payload, headers=headers, timeout=httpx.Timeout(30.0)
        )
        if resp.status_code == 200:
            LOG.info(
                "Task v2 webhook sent successfully",
                task_v2_id=task_v2.observer_cruise_id,
                resp_code=resp.status_code,
                resp_text=resp.text,
            )
        else:
            LOG.info(
                "Task v2 webhook failed",
                task_v2_id=task_v2.observer_cruise_id,
                resp=resp,
                resp_code=resp.status_code,
                resp_text=resp.text,
            )
    except Exception as e:
        raise FailedToSendWebhook(task_v2_id=task_v2.observer_cruise_id) from e
