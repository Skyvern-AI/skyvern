import hashlib

import structlog
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import OperationalError

from skyvern.config import settings
from skyvern.exceptions import TaskNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration, TaskGenerationBase
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.schemas.runs import RunEngine, RunType

LOG = structlog.get_logger()


async def generate_task(user_prompt: str, organization: Organization) -> TaskGeneration:
    hash_object = hashlib.sha256()
    hash_object.update(user_prompt.encode("utf-8"))
    user_prompt_hash = hash_object.hexdigest()
    # check if there's a same user_prompt within the past x Hours
    # in the future, we can use vector db to fetch similar prompts
    existing_task_generation = await app.DATABASE.get_task_generation_by_prompt_hash(
        user_prompt_hash=user_prompt_hash, query_window_hours=settings.PROMPT_CACHE_WINDOW_HOURS
    )
    if existing_task_generation:
        new_task_generation = await app.DATABASE.create_task_generation(
            organization_id=organization.organization_id,
            user_prompt=user_prompt,
            user_prompt_hash=user_prompt_hash,
            url=existing_task_generation.url,
            navigation_goal=existing_task_generation.navigation_goal,
            navigation_payload=existing_task_generation.navigation_payload,
            data_extraction_goal=existing_task_generation.data_extraction_goal,
            extracted_information_schema=existing_task_generation.extracted_information_schema,
            llm=existing_task_generation.llm,
            llm_prompt=existing_task_generation.llm_prompt,
            llm_response=existing_task_generation.llm_response,
            source_task_generation_id=existing_task_generation.task_generation_id,
        )
        return new_task_generation

    llm_prompt = prompt_engine.load_prompt("generate-task", user_prompt=user_prompt)
    try:
        llm_response = await app.SECONDARY_LLM_API_HANDLER(
            prompt=llm_prompt, prompt_name="generate-task", organization_id=organization.organization_id
        )
        parsed_task_generation_obj = TaskGenerationBase.model_validate(llm_response)

        # generate a TaskGenerationModel
        task_generation = await app.DATABASE.create_task_generation(
            organization_id=organization.organization_id,
            user_prompt=user_prompt,
            user_prompt_hash=user_prompt_hash,
            url=parsed_task_generation_obj.url,
            navigation_goal=parsed_task_generation_obj.navigation_goal,
            navigation_payload=parsed_task_generation_obj.navigation_payload,
            data_extraction_goal=parsed_task_generation_obj.data_extraction_goal,
            extracted_information_schema=parsed_task_generation_obj.extracted_information_schema,
            suggested_title=parsed_task_generation_obj.suggested_title,
            llm=settings.LLM_KEY,
            llm_prompt=llm_prompt,
            llm_response=str(llm_response),
        )
        return task_generation
    except LLMProviderError:
        LOG.error("Failed to generate task", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to generate task. Please try again later.")
    except OperationalError:
        LOG.error("Database error when generating task", exc_info=True, user_prompt=user_prompt)
        raise HTTPException(status_code=500, detail="Failed to generate task. Please try again later.")


async def run_task(
    task: TaskRequest,
    organization: Organization,
    engine: RunEngine = RunEngine.skyvern_v1,
    x_max_steps_override: int | None = None,
    x_api_key: str | None = None,
    request: Request | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> Task:
    created_task = await app.agent.create_task(task, organization.organization_id)
    url_hash = generate_url_hash(task.url)
    run_type = RunType.task_v1
    if engine == RunEngine.openai_cua:
        run_type = RunType.openai_cua
    elif engine == RunEngine.anthropic_cua:
        run_type = RunType.anthropic_cua
    elif engine == RunEngine.ui_tars:
        run_type = RunType.ui_tars
    await app.DATABASE.create_task_run(
        task_run_type=run_type,
        organization_id=organization.organization_id,
        run_id=created_task.task_id,
        title=task.title,
        url=task.url,
        url_hash=url_hash,
    )
    if x_max_steps_override:
        LOG.info(
            "Overriding max steps per run",
            max_steps_override=x_max_steps_override,
            organization_id=organization.organization_id,
            task_id=created_task.task_id,
        )
    await AsyncExecutorFactory.get_executor().execute_task(
        request=request,
        background_tasks=background_tasks,
        task_id=created_task.task_id,
        organization_id=organization.organization_id,
        max_steps_override=x_max_steps_override,
        browser_session_id=task.browser_session_id,
        api_key=x_api_key,
    )
    return created_task


async def get_task_v1_response(task_id: str, organization_id: str | None = None) -> TaskResponse:
    task_obj = await app.DATABASE.get_task(task_id, organization_id=organization_id)
    if not task_obj:
        raise TaskNotFound(task_id=task_id)

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=organization_id)
    if not latest_step:
        return await app.agent.build_task_response(task=task_obj)

    failure_reason: str | None = None
    if task_obj.status == TaskStatus.failed and (latest_step.output or task_obj.failure_reason):
        failure_reason = ""
        if task_obj.failure_reason:
            failure_reason += task_obj.failure_reason or ""
        if latest_step.output is not None and latest_step.output.actions_and_results is not None:
            action_results_string: list[str] = []
            for action, results in latest_step.output.actions_and_results:
                if len(results) == 0:
                    continue
                if results[-1].success:
                    continue
                action_results_string.append(f"{action.action_type} action failed.")

            if len(action_results_string) > 0:
                failure_reason += "(Exceptions: " + str(action_results_string) + ")"
    return await app.agent.build_task_response(
        task=task_obj, last_step=latest_step, failure_reason=failure_reason, need_browser_log=True
    )
