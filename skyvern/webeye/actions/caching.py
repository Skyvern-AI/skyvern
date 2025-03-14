import structlog

from skyvern.exceptions import CachedActionPlanError
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.actions import Action, ActionStatus, ActionType
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()


async def retrieve_action_plan(task: Task, step: Step, scraped_page: ScrapedPage) -> list[Action]:
    try:
        return await _retrieve_action_plan(task, step, scraped_page)
    except Exception as e:
        LOG.exception("Failed to retrieve action plan", exception=e)
        return []


async def _retrieve_action_plan(task: Task, step: Step, scraped_page: ScrapedPage) -> list[Action]:
    # V0: use the previous action plan if there is a completed task with the same url and navigation goal
    # get completed task with the same url and navigation goal
    # TODO(kerem): don't use step_order, get all the previous actions instead
    cached_actions = await app.DATABASE.retrieve_action_plan(task=task)
    if not cached_actions:
        LOG.info("No cached actions found for the task, fallback to no-cache mode")
        return []

    # Get the existing actions for this task from the database. Then find the actions that are already executed by looking at
    # the source_action_id field for this task's actions.
    previous_actions = await app.DATABASE.get_previous_actions_for_task(task_id=task.task_id)

    executed_cached_actions = []
    remaining_cached_actions = []
    action_matching_complete = False
    if previous_actions:
        for idx, cached_action in enumerate(cached_actions):
            if not action_matching_complete:
                should_be_matching_action = previous_actions[idx]
                if not should_be_matching_action.source_action_id:
                    # If there is an action without a source_action_id, it means we already went back to no-cache mode
                    # and we should not try to reuse the previous action plan since it's not possible to determine which
                    # action we should execute next
                    return []

                action_id_to_match = (
                    cached_action.source_action_id if cached_action.source_action_id else cached_action.action_id
                )
                if should_be_matching_action.source_action_id == action_id_to_match:
                    executed_cached_actions.append(cached_action)
                    if idx == len(previous_actions) - 1:
                        # If we've reached the end of the previous actions, we've completed matching.
                        action_matching_complete = True
                else:
                    # If we've reached an action that doesn't match the source_action_id of the previous actions,
                    # we've completed matching.
                    action_matching_complete = True
                    remaining_cached_actions.append(cached_action)
            else:
                remaining_cached_actions.append(cached_action)
    else:
        remaining_cached_actions = cached_actions
        action_matching_complete = True

    # For any remaining cached action,
    # check if the element hash exists in the current scraped page. Add them to a list until we can't find a match. Always keep the
    # actions without an element hash.

    cached_actions_to_execute: list[Action] = []
    found_element_with_no_hash = False
    for cached_action in remaining_cached_actions:
        # The actions without an element hash: TerminateAction CompleteAction NullAction SolveCaptchaAction WaitAction
        # For these actions, we can't check if the element hash exists in the current scraped page.
        # For that reason, we're going to make sure they're executed always as the first action in each step.
        if not cached_action.skyvern_element_hash:
            if not found_element_with_no_hash and len(cached_actions_to_execute) > 0:
                # If we've already added actions with element hashes to the list before we encounter an action without an element hash,
                # we need to execute the actions we already added first. We want the actions without an element hash
                # to be executed as the first actions in each step. We're ok with executing multiple actions without an element hash
                # in a row, but we want them to be executed in a new step after we wait & scrape the page again.
                break
            cached_actions_to_execute.append(cached_action)
            found_element_with_no_hash = True
            continue

        matching_element_ids = scraped_page.hash_to_element_ids.get(cached_action.skyvern_element_hash)
        if matching_element_ids and len(matching_element_ids) == 1:
            cached_actions_to_execute.append(cached_action)
            continue
        # After this point, we can't continue adding actions to the plan, so we break and continue with what we have.
        # Because this action has either no hash-match or multiple hash-matches, we can't continue.
        elif matching_element_ids and len(matching_element_ids) > 1:
            LOG.warning(
                "Found multiple elements with the same hash, stop matching",
                element_hash=cached_action.skyvern_element_hash,
                element_ids=matching_element_ids,
            )
            break
        else:
            LOG.warning("No element found with the hash", element_hash=cached_action.skyvern_element_hash)
            break

    # If there are no items in the list we just built, we need to revert back to no-cache mode. Return empty list.
    if not cached_actions_to_execute:
        return []

    LOG.info("Found cached actions to execute", actions=cached_actions_to_execute)

    actions: list[Action] = []
    for idx, cached_action in enumerate(cached_actions_to_execute):
        updated_action = cached_action.model_copy()
        updated_action.status = ActionStatus.pending
        updated_action.source_action_id = (
            cached_action.source_action_id if cached_action.source_action_id else cached_action.action_id
        )
        updated_action.workflow_run_id = task.workflow_run_id
        updated_action.task_id = task.task_id
        updated_action.step_id = step.step_id
        updated_action.step_order = step.order
        updated_action.action_order = idx
        # Reset the action response to None so we don't use the previous answers
        updated_action.response = None

        # Update the element id with the element id from the current scraped page, matched by element hash
        if cached_action.skyvern_element_hash:
            matching_element_ids = scraped_page.hash_to_element_ids.get(cached_action.skyvern_element_hash)
            if matching_element_ids and len(matching_element_ids) == 1:
                matching_element_id = matching_element_ids[0]
                updated_action.element_id = matching_element_id
                updated_action.skyvern_element_data = scraped_page.id_to_element_dict.get(matching_element_id)
            else:
                raise CachedActionPlanError(
                    "All elements with either no hash or multiple hashes should have been already filtered out"
                )

        actions.append(updated_action)

    # Check for unsupported actions before personalizing the actions
    # Classify the supported actions into two groups:
    # 1. Actions that can be cached with a query
    # 2. Actions that can be cached without a query
    # We'll use this classification to determine if we should continue with caching or fallback to no-cache mode
    check_for_unsupported_actions(actions)

    personalized_actions = await personalize_actions(task=task, step=step, scraped_page=scraped_page, actions=actions)

    LOG.info("Personalized cached actions are ready", actions=personalized_actions)
    return personalized_actions


async def personalize_actions(
    task: Task,
    step: Step,
    actions: list[Action],
    scraped_page: ScrapedPage,
) -> list[Action]:
    queries_and_answers: dict[str, str | None] = {action.intention: None for action in actions if action.intention}

    answered_queries: dict[str, str] = {}
    if queries_and_answers:
        # Call LLM to get answers for the queries only if there are queries to answer
        answered_queries = await get_user_detail_answers(
            task=task, step=step, scraped_page=scraped_page, queries_and_answers=queries_and_answers
        )

    personalized_actions = []
    for action in actions:
        query = action.intention
        if query and (personalized_answer := answered_queries.get(query)):
            current_personized_actions = await personalize_action(
                action, query, personalized_answer, task, step, scraped_page
            )
            personalized_actions.extend(current_personized_actions)
        else:
            personalized_actions.append(action)

    return personalized_actions


async def get_user_detail_answers(
    task: Task, step: Step, scraped_page: ScrapedPage, queries_and_answers: dict[str, str | None]
) -> dict[str, str]:
    try:
        question_answering_prompt = prompt_engine.load_prompt(
            "answer-user-detail-questions",
            navigation_goal=task.navigation_goal,
            navigation_payload=task.navigation_payload,
            queries_and_answers=queries_and_answers,
        )

        llm_response = await app.SECONDARY_LLM_API_HANDLER(
            prompt=question_answering_prompt, step=step, screenshots=None, prompt_name="answer-user-detail-questions"
        )
        return llm_response
    except Exception as e:
        LOG.exception("Failed to get user detail answers", exception=e)
        # TODO: custom exception so we can fallback to no-cache mode by catching it
        raise e


async def personalize_action(
    action: Action,
    query: str,
    answer: str,
    task: Task,
    step: Step,
    scraped_page: ScrapedPage,
) -> list[Action]:
    action.intention = query
    action.response = answer

    if action.action_type == ActionType.INPUT_TEXT:
        action.text = answer
        if not answer:
            return []
    elif action.action_type == ActionType.UPLOAD_FILE:
        action.file_url = answer
    elif action.action_type == ActionType.CLICK:
        # TODO: we only use cached action.intention. send the intention, navigation payload + navigation goal, html
        # to small llm and make a decision of which elements to click. Not clicking anything is also an option here
        return [action]
    elif action.action_type == ActionType.SELECT_OPTION:
        # TODO: send the selection action with the original/previous option value. Our current selection agent
        # is already able to handle it
        return [action]
    elif action.action_type in [
        ActionType.COMPLETE,
        ActionType.WAIT,
        ActionType.SOLVE_CAPTCHA,
        ActionType.NULL_ACTION,
    ]:
        return [action]
    elif action.action_type == ActionType.TERMINATE:
        return []
    else:
        raise CachedActionPlanError(
            f"Unsupported action type for personalization, fallback to no-cache mode: {action.action_type}"
        )

    return [action]


def check_for_unsupported_actions(actions: list[Action]) -> None:
    supported_actions = [ActionType.INPUT_TEXT, ActionType.WAIT, ActionType.CLICK, ActionType.COMPLETE]
    supported_actions_with_query = [ActionType.INPUT_TEXT]
    for action in actions:
        query = action.intention
        if action.action_type not in supported_actions:
            raise CachedActionPlanError(
                f"This action type does not support caching: {action.action_type}, fallback to no-cache mode"
            )
        if query and action.action_type not in supported_actions_with_query:
            raise CachedActionPlanError(
                f"This action type does not support caching with a query: {action.action_type}, fallback to no-cache mode"
            )
