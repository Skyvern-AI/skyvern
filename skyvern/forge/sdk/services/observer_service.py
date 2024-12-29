import os
import random
import string
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel

from skyvern.exceptions import UrlGenerationFailure
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.observers import (
    ObserverCruise,
    ObserverCruiseStatus,
    ObserverMetadata,
    ObserverThought,
    ObserverThoughtScenario,
    ObserverThoughtType,
)
from skyvern.forge.sdk.schemas.organizations import Organization
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
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, ContextParameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.models.yaml import (
    BLOCK_YAML_TYPES,
    PARAMETER_YAML_TYPES,
    ContextParameterYAML,
    ExtractionBlockYAML,
    ForLoopBlockYAML,
    NavigationBlockYAML,
    TaskBlockYAML,
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

DATA_EXTRACTION_SCHEMA_FOR_LOOP = {
    "type": "object",
    "properties": {
        "loop_values": {
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


class LoopExtractionOutput(BaseModel):
    loop_values: list[str]
    is_loop_value_link: bool


async def initialize_observer_cruise(
    organization: Organization, user_prompt: str, user_url: str | None = None
) -> ObserverCruise:
    observer_cruise = await app.DATABASE.create_observer_cruise(
        prompt=user_prompt,
        organization_id=organization.organization_id,
    )

    observer_thought = await app.DATABASE.create_observer_thought(
        observer_cruise_id=observer_cruise.observer_cruise_id,
        organization_id=organization.organization_id,
        observer_thought_type=ObserverThoughtType.metadata,
        observer_thought_scenario=ObserverThoughtScenario.generate_metadata,
    )

    metadata_prompt = prompt_engine.load_prompt("observer_generate_metadata", user_goal=user_prompt, user_url=user_url)
    metadata_response = await app.SECONDARY_LLM_API_HANDLER(prompt=metadata_prompt, observer_thought=observer_thought)
    # validate
    LOG.info(f"Initialized observer initial response: {metadata_response}")
    url: str = metadata_response.get("url", "")
    if not url:
        raise UrlGenerationFailure()
    title: str = metadata_response.get("title", DEFAULT_WORKFLOW_TITLE)
    metadata = ObserverMetadata(
        url=url,
        workflow_title=title,
    )
    url = metadata.url
    if not url:
        raise UrlGenerationFailure()

    # create workflow and workflow run
    max_steps_override = 10
    new_workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(organization, metadata.workflow_title)
    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=None,
        workflow_request=WorkflowRequestBody(),
        workflow_permanent_id=new_workflow.workflow_permanent_id,
        organization_id=organization.organization_id,
        version=None,
        max_steps_override=max_steps_override,
    )
    await app.DATABASE.update_observer_thought(
        observer_thought_id=observer_thought.observer_thought_id,
        organization_id=organization.organization_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_id=new_workflow.workflow_id,
        workflow_permanent_id=new_workflow.workflow_permanent_id,
        thought=metadata_response.get("thoughts", ""),
        output=metadata.model_dump(),
    )

    # update oserver cruise
    observer_cruise = await app.DATABASE.update_observer_cruise(
        observer_cruise_id=observer_cruise.observer_cruise_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_id=new_workflow.workflow_id,
        workflow_permanent_id=new_workflow.workflow_permanent_id,
        url=url,
        organization_id=organization.organization_id,
    )
    return observer_cruise


async def run_observer_cruise(
    organization: Organization,
    observer_cruise_id: str,
    request_id: str | None = None,
    max_iterations_override: str | int | None = None,
) -> None:
    organization_id = organization.organization_id
    observer_cruise = await app.DATABASE.get_observer_cruise(observer_cruise_id, organization_id=organization_id)
    if not observer_cruise:
        LOG.error("Observer cruise not found", observer_cruise_id=observer_cruise_id, organization_id=organization_id)
        return None
    if observer_cruise.status != ObserverCruiseStatus.queued:
        LOG.error(
            "Observer cruise is not queued. Duplicate observer cruise",
            observer_cruise_id=observer_cruise_id,
            status=observer_cruise.status,
            organization_id=organization_id,
        )
        return None
    if not observer_cruise.url or not observer_cruise.prompt:
        LOG.error(
            "Observer cruise url or prompt not found",
            observer_cruise_id=observer_cruise_id,
            organization_id=organization_id,
        )
        return None
    if not observer_cruise.workflow_run_id:
        LOG.error(
            "Workflow run id not found in observer cruise",
            observer_cruise_id=observer_cruise_id,
            organization_id=organization_id,
        )
        return None

    int_max_iterations_override = None
    if max_iterations_override:
        try:
            int_max_iterations_override = int(max_iterations_override)
            LOG.info("max_iterationss_override is set", max_iterations_override=int_max_iterations_override)
        except ValueError:
            LOG.info(
                "max_iterations_override isn't an integer, won't override",
                max_iterations_override=max_iterations_override,
            )

    workflow_run_id = observer_cruise.workflow_run_id

    workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        LOG.error("Workflow run not found", workflow_run_id=workflow_run_id)
        return None
    else:
        LOG.info("Workflow run found", workflow_run_id=workflow_run_id)

    if workflow_run.status != WorkflowRunStatus.queued:
        LOG.warning("Duplicate workflow run execution", workflow_run_id=workflow_run_id, status=workflow_run.status)
        return None

    workflow_id = workflow_run.workflow_id
    workflow = await app.WORKFLOW_SERVICE.get_workflow(workflow_id, organization_id=organization_id)
    if not workflow:
        LOG.error("Workflow not found", workflow_id=workflow_id)
        return None
    else:
        LOG.info("Workflow found", workflow_id=workflow_id)

    ###################### run observer ######################

    skyvern_context.set(
        SkyvernContext(
            organization_id=organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            request_id=request_id,
        )
    )

    await app.DATABASE.update_observer_cruise(
        observer_cruise_id=observer_cruise_id, organization_id=organization_id, status=ObserverCruiseStatus.running
    )
    await app.WORKFLOW_SERVICE.mark_workflow_run_as_running(workflow_run_id=workflow_run.workflow_run_id)
    await _set_up_workflow_context(workflow_id, workflow_run_id)

    url = str(observer_cruise.url)
    user_prompt = observer_cruise.prompt
    task_history: list[dict] = []
    yaml_blocks: list[BLOCK_YAML_TYPES] = []
    yaml_parameters: list[PARAMETER_YAML_TYPES] = []

    for i in range(int_max_iterations_override or DEFAULT_MAX_ITERATIONS):
        LOG.info(f"Observer iteration i={i}", workflow_run_id=workflow_run_id, url=url)
        browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=url,
        )
        scraped_page = await scrape_website(
            browser_state,
            url,
            app.AGENT_FUNCTION.cleanup_element_tree_factory(),
            scrape_exclude=app.scrape_exclude,
        )
        element_tree_in_prompt: str = scraped_page.build_element_tree(ElementTreeFormat.HTML)
        page = await browser_state.get_working_page()
        current_url = str(
            await SkyvernFrame.evaluate(frame=page, expression="() => document.location.href") if page else url
        )

        context = skyvern_context.ensure_context()
        observer_prompt = prompt_engine.load_prompt(
            "observer",
            current_url=current_url,
            elements=element_tree_in_prompt,
            user_goal=user_prompt,
            task_history=task_history,
            local_datetime=datetime.now(context.tz_info).isoformat(),
        )
        observer_thought = await app.DATABASE.create_observer_thought(
            observer_cruise_id=observer_cruise_id,
            organization_id=organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            observer_thought_type=ObserverThoughtType.plan,
            observer_thought_scenario=ObserverThoughtScenario.generate_plan,
        )
        observer_response = await app.LLM_API_HANDLER(
            prompt=observer_prompt,
            screenshots=scraped_page.screenshots,
            observer_thought=observer_thought,
        )
        LOG.info(
            "Observer response",
            observer_response=observer_response,
            iteration=i,
            current_url=current_url,
            workflow_run_id=workflow_run_id,
        )
        # see if the user goal has achieved or not
        user_goal_achieved = observer_response.get("user_goal_achieved", False)
        observation = observer_response.get("page_info", "")
        thoughts: str = observer_response.get("thoughts", "")
        plan: str = observer_response.get("plan", "")
        task_type: str = observer_response.get("task_type", "")
        # Create and save observer thought
        await app.DATABASE.update_observer_thought(
            observer_thought_id=observer_thought.observer_thought_id,
            organization_id=organization_id,
            thought=thoughts,
            observation=observation,
            answer=plan,
            output={"task_type": task_type, "user_goal_achieved": user_goal_achieved},
        )

        if user_goal_achieved is True:
            LOG.info(
                "User goal achieved. Workflow run will complete. Observer is stopping",
                iteration=i,
                workflow_run_id=workflow_run_id,
            )
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_completed(workflow_run_id=workflow_run_id)
            break

        # parse observer repsonse and run the next task
        if not task_type:
            LOG.error("No task type found in observer response", observer_response=observer_response)
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id,
                failure_reason="Skyvern failed to generate a task. Please try again later.",
            )
            break

        block: BlockTypeVar | None = None
        if task_type == "extract":
            block, block_yaml_list, parameter_yaml_list = await _generate_extraction_task(
                observer_cruise=observer_cruise,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                current_url=current_url,
                element_tree_in_prompt=element_tree_in_prompt,
                data_extraction_goal=plan,
                task_history=task_history,
            )
            task_history.append({"type": task_type, "task": plan})
        elif task_type == "navigate":
            original_url = url if i == 0 else None
            block, block_yaml_list, parameter_yaml_list = await _generate_navigation_task(
                workflow_id=workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                original_url=original_url,
                navigation_goal=plan,
            )
            task_history.append({"type": task_type, "task": plan})
        elif task_type == "loop":
            try:
                block, block_yaml_list, parameter_yaml_list, extraction_obj, inner_task = await _generate_loop_task(
                    observer_cruise=observer_cruise,
                    workflow_id=workflow_id,
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    workflow_run_id=workflow_run_id,
                    plan=plan,
                    browser_state=browser_state,
                    original_url=url,
                    scraped_page=scraped_page,
                )
                task_history.append(
                    {
                        "type": task_type,
                        "task": plan,
                        "loop_over_values": extraction_obj.loop_values,
                        "task_inside_the_loop": inner_task,
                    }
                )
            except Exception:
                LOG.exception("Failed to generate loop task")
                await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run_id,
                    failure_reason="Failed to generate loop task.",
                )
                break
        else:
            LOG.info("Unsupported task type", task_type=task_type)
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run_id, failure_reason=f"Unsupported task type gets generated: {task_type}"
            )
            break

        # generate the extraction task
        block_result = await block.execute_safe(workflow_run_id=workflow_run_id, organization_id=organization_id)

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
            proxy_location=ProxyLocation.RESIDENTIAL,
            workflow_definition=workflow_definition_yaml,
        )
        LOG.info("Creating workflow from request", workflow_create_request=workflow_create_request)
        workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=organization,
            request=workflow_create_request,
            workflow_permanent_id=workflow.workflow_permanent_id,
        )
        LOG.info("Workflow created", workflow_id=workflow.workflow_id)

        # execute the extraction task
        workflow_run = await handle_block_result(block, block_result, workflow, workflow_run)
        if workflow_run.status != WorkflowRunStatus.running:
            LOG.info(
                "Workflow run is not running anymore, stopping the observer",
                workflow_run_id=workflow_run_id,
                status=workflow_run.status,
            )
            break
        if block_result.success is True:
            # validate completion
            observer_completion_prompt = prompt_engine.load_prompt(
                "observer_check_completion",
                user_goal=user_prompt,
                task_history=task_history,
                local_datetime=datetime.now(context.tz_info).isoformat(),
            )
            observer_thought = await app.DATABASE.create_observer_thought(
                observer_cruise_id=observer_cruise_id,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow.workflow_permanent_id,
                observer_thought_type=ObserverThoughtType.user_goal_check,
                observer_thought_scenario=ObserverThoughtScenario.user_goal_check,
            )
            completion_resp = await app.LLM_API_HANDLER(
                prompt=observer_completion_prompt,
                observer_cruise=observer_thought,
            )
            await _record_thought_screenshot(observer_thought=observer_thought, workflow_run_id=workflow_run_id)
            LOG.info(
                "Observer completion check response",
                completion_resp=completion_resp,
                iteration=i,
                workflow_run_id=workflow_run_id,
                task_history=task_history,
            )
            user_goal_achieved = completion_resp.get("user_goal_achieved", False)
            thought = completion_resp.get("thoughts", "")
            await app.DATABASE.update_observer_thought(
                observer_thought_id=observer_thought.observer_thought_id,
                organization_id=organization_id,
                thought=thought,
                output={"user_goal_achieved": user_goal_achieved},
            )
            if user_goal_achieved:
                LOG.info(
                    "User goal achieved according to the observer completion check",
                    iteration=i,
                    workflow_run_id=workflow_run_id,
                    completion_resp=completion_resp,
                )
                await app.WORKFLOW_SERVICE.mark_workflow_run_as_completed(workflow_run_id=workflow_run_id)
                break

    await app.DATABASE.update_observer_cruise(
        observer_cruise_id=observer_cruise_id,
        organization_id=organization_id,
        status=ObserverCruiseStatus.completed,
    )
    await app.WORKFLOW_SERVICE.clean_up_workflow(workflow=workflow, workflow_run=workflow_run)


async def handle_block_result(
    block: BlockTypeVar,
    block_result: BlockResult,
    workflow: Workflow,
    workflow_run: WorkflowRun,
    is_last_block: bool = True,
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
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(workflow_run_id=workflow_run.workflow_run_id)

        # TODO: we can also support webhook by adding api_key to the function signature
        await app.WORKFLOW_SERVICE.clean_up_workflow(
            workflow=workflow,
            workflow_run=workflow_run,
            need_call_webhook=False,
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
        else:
            failure_reason = f"Block with type {block.block_type} failed. failure reason: {block_result.failure_reason}"
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )

            # TODO: add api_key
            await app.WORKFLOW_SERVICE.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
            )
    elif block_result.status == BlockStatus.terminated:
        LOG.info(
            f"Block with type {block.block_type} was terminated for workflow run {workflow_run_id}, marking workflow run as terminated",
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
        else:
            failure_reason = f"Block with type {block.block_type} terminated. Reason: {block_result.failure_reason}"
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_terminated(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )
            await app.WORKFLOW_SERVICE.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
            )
    # refresh workflow run model
    return await app.WORKFLOW_SERVICE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=workflow.organization_id,
    )


async def _set_up_workflow_context(workflow_id: str, workflow_run_id: str) -> None:
    """
    TODO: see if we could remove this function as we can just set an empty workflow context
    """
    # Get all <workflow parameter, workflow run parameter> tuples
    wp_wps_tuples = await app.WORKFLOW_SERVICE.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run_id)
    workflow_output_parameters = await app.WORKFLOW_SERVICE.get_workflow_output_parameters(workflow_id=workflow_id)
    app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
        workflow_run_id,
        wp_wps_tuples,
        workflow_output_parameters,
        [],
    )


async def _generate_loop_task(
    observer_cruise: ObserverCruise,
    workflow_id: str,
    workflow_permanent_id: str,
    workflow_run_id: str,
    plan: str,
    browser_state: BrowserState,
    original_url: str,
    scraped_page: ScrapedPage,
) -> tuple[ForLoopBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES], LoopExtractionOutput, dict[str, Any]]:
    for_loop_parameter_yaml_list: list[PARAMETER_YAML_TYPES] = []
    loop_value_extraction_goal = prompt_engine.load_prompt(
        "observer_loop_task_extraction_goal",
        plan=plan,
    )
    data_extraction_thought = f"Going to generate a list of values to go through based on the plan: {plan}."
    observer_thought = await app.DATABASE.create_observer_thought(
        observer_cruise_id=observer_cruise.observer_cruise_id,
        organization_id=observer_cruise.organization_id,
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
        observer_thought_type=ObserverThoughtType.plan,
        observer_thought_scenario=ObserverThoughtScenario.extract_loop_values,
        thought=data_extraction_thought,
    )
    # generate screenshot artifact for the observer thought
    if scraped_page.screenshots:
        for screenshot in scraped_page.screenshots:
            await app.ARTIFACT_MANAGER.create_observer_thought_artifact(
                observer_thought=observer_thought,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
                data=screenshot,
            )
    label = f"extraction_task_for_loop_{_generate_random_string()}"
    extraction_block_yaml = ExtractionBlockYAML(
        label=label,
        data_extraction_goal=loop_value_extraction_goal,
        data_schema=DATA_EXTRACTION_SCHEMA_FOR_LOOP,
    )
    loop_value_extraction_output_parameter = await app.WORKFLOW_SERVICE.create_output_parameter_for_block(
        workflow_id=workflow_id,
        block_yaml=extraction_block_yaml,
    )
    extraction_block_for_loop = ExtractionBlock(
        label=label,
        data_extraction_goal=loop_value_extraction_goal,
        data_schema=DATA_EXTRACTION_SCHEMA_FOR_LOOP,
        output_parameter=loop_value_extraction_output_parameter,
    )

    # execute the extraction block
    extraction_block_result = await extraction_block_for_loop.execute_safe(
        workflow_run_id=workflow_run_id,
        organization_id=observer_cruise.organization_id,
    )
    LOG.info("Extraction block result", extraction_block_result=extraction_block_result)
    if extraction_block_result.success is False:
        LOG.error(
            "Failed to execute the extraction block for the loop task",
            extraction_block_result=extraction_block_result,
        )
        # TODO: fail the workflow run
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
            workflow_run_id=workflow_run_id,
            failure_reason="Failed to extract loop values for the loop task. Please try again later.",
        )
        raise Exception("extraction_block failed")
    # validate output parameter
    try:
        output_value_obj = LoopExtractionOutput.model_validate(
            extraction_block_result.output_parameter_value.get("extracted_information")  # type: ignore
        )
    except Exception:
        LOG.error(
            "Failed to validate the output parameter of the extraction block for the loop task",
            extraction_block_result=extraction_block_result,
        )
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed(
            workflow_run_id=workflow_run_id,
            failure_reason="Invalid output parameter of the extraction block for the loop task. Please try again later.",
        )
        raise

    # update the observer thought
    await app.DATABASE.update_observer_thought(
        observer_thought_id=observer_thought.observer_thought_id,
        organization_id=observer_cruise.organization_id,
        output=output_value_obj.model_dump(),
    )

    # create ContextParameter for the loop over pointer that ForLoopBlock needs.
    loop_for_context_parameter = ContextParameter(
        key="loop_values",
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
    if output_value_obj.is_loop_value_link:
        LOG.info("Loop values are links", loop_values=output_value_obj.loop_values)
        # create ContextParameter for the value
        url_value_context_parameter = ContextParameter(
            key="task_in_loop_url",
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
        url = "task_in_loop_url"
    else:
        LOG.info("Loop values are not links", loop_values=output_value_obj.loop_values)
        page = await browser_state.get_working_page()
        url = str(
            await SkyvernFrame.evaluate(frame=page, expression="() => document.location.href") if page else original_url
        )
    task_in_loop_label = f"task_in_loop_{_generate_random_string()}"
    context = skyvern_context.ensure_context()
    task_in_loop_metadata_prompt = prompt_engine.load_prompt(
        "observer_generate_task_block",
        plan=plan,
        local_datetime=datetime.now(context.tz_info).isoformat(),
        is_link=output_value_obj.is_loop_value_link,
        loop_values=output_value_obj.loop_values,
    )
    observer_thought_task_in_loop = await app.DATABASE.create_observer_thought(
        observer_cruise_id=observer_cruise.observer_cruise_id,
        organization_id=observer_cruise.organization_id,
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
        observer_thought_type=ObserverThoughtType.internal_plan,
        observer_thought_scenario=ObserverThoughtScenario.generate_task_in_loop,
    )
    task_in_loop_metadata_response = await app.LLM_API_HANDLER(
        task_in_loop_metadata_prompt,
        screenshots=scraped_page.screenshots,
        observer_thought=observer_thought_task_in_loop,
    )
    LOG.info("Task in loop metadata response", task_in_loop_metadata_response=task_in_loop_metadata_response)
    navigation_goal = task_in_loop_metadata_response.get("navigation_goal")
    data_extraction_goal = task_in_loop_metadata_response.get("data_extraction_goal")
    data_extraction_schema = task_in_loop_metadata_response.get("data_schema")
    thought = task_in_loop_metadata_response.get("thoughts")
    await app.DATABASE.update_observer_thought(
        observer_thought_id=observer_thought_task_in_loop.observer_thought_id,
        organization_id=observer_cruise.organization_id,
        thought=thought,
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
    observer_cruise: ObserverCruise,
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
        "observer_generate_extraction_task",
        current_url=current_url,
        elements=element_tree_in_prompt,
        data_extraction_goal=data_extraction_goal,
        local_datetime=datetime.now(context.tz_info).isoformat(),
    )
    generate_extraction_task_response = await app.LLM_API_HANDLER(
        generate_extraction_task_prompt,
        observer_cruise=observer_cruise,
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
) -> tuple[NavigationBlock, list[BLOCK_YAML_TYPES], list[PARAMETER_YAML_TYPES]]:
    LOG.info("Generating navigation task", navigation_goal=navigation_goal, original_url=original_url)
    label = f"navigation_{_generate_random_string()}"
    navigation_block_yaml = NavigationBlockYAML(
        label=label,
        url=original_url,
        navigation_goal=navigation_goal,
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
            output_parameter=output_parameter,
        ),
        [navigation_block_yaml],
        [],
    )


def _generate_random_string(length: int = 5) -> str:
    # Use the current timestamp as the seed
    random.seed(os.urandom(16))
    return "".join(random.choices(RANDOM_STRING_POOL, k=length))


async def get_observer_thought_timelines(
    observer_cruise_id: str,
    organization_id: str | None = None,
) -> list[WorkflowRunTimeline]:
    observer_thoughts = await app.DATABASE.get_observer_thoughts(
        observer_cruise_id,
        organization_id=organization_id,
        observer_thought_types=[
            ObserverThoughtType.plan,
            ObserverThoughtType.user_goal_check,
        ],
    )
    return [
        WorkflowRunTimeline(
            type=WorkflowRunTimelineType.thought,
            thought=thought,
            created_at=thought.created_at,
            modified_at=thought.modified_at,
        )
        for thought in observer_thoughts
    ]


async def _record_thought_screenshot(observer_thought: ObserverThought, workflow_run_id: str) -> None:
    # get the browser state for the workflow run
    browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id=workflow_run_id)
    if not browser_state:
        LOG.warning("No browser state found for the workflow run", workflow_run_id=workflow_run_id)
        return
    # get the screenshot for the workflow run
    screenshot = await browser_state.take_screenshot(full_page=True)
    await app.ARTIFACT_MANAGER.create_observer_thought_artifact(
        observer_thought=observer_thought,
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        data=screenshot,
    )
