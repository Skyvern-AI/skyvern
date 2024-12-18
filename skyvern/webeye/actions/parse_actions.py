from typing import Any, Dict

import structlog
from pydantic import ValidationError

from skyvern.exceptions import UnsupportedActionType
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CheckboxAction,
    ClickAction,
    CompleteAction,
    DownloadFileAction,
    InputTextAction,
    NullAction,
    SelectOption,
    SelectOptionAction,
    SolveCaptchaAction,
    TerminateAction,
    UploadFileAction,
    WaitAction,
)
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()


def parse_action(action: Dict[str, Any], scraped_page: ScrapedPage, data_extraction_goal: str | None = None) -> Action:
    if "id" in action:
        element_id = action["id"]
    elif "element_id" in action:
        element_id = action["element_id"]
    else:
        element_id = None

    skyvern_element_hash = scraped_page.id_to_element_hash.get(element_id) if element_id else None
    skyvern_element_data = scraped_page.id_to_element_dict.get(element_id) if element_id else None

    reasoning = action["reasoning"] if "reasoning" in action else None
    confidence_float = action["confidence_float"] if "confidence_float" in action else None
    # TODO: currently action intention and response are only used for Q&A actions, like input_text
    # When we start supporting click action, intention will be the reasoning for the click action (why take the action)
    intention = action["user_detail_query"] if "user_detail_query" in action else None
    response = action["user_detail_answer"] if "user_detail_answer" in action else None

    base_action_dict = {
        "element_id": element_id,
        "skyvern_element_hash": skyvern_element_hash,
        "skyvern_element_data": skyvern_element_data,
        "reasoning": reasoning,
        "confidence_float": confidence_float,
        "intention": intention,
        "response": response,
    }

    if "action_type" not in action or action["action_type"] is None:
        return NullAction(**base_action_dict)

    # `.upper()` handles the case where the LLM returns a lowercase action type (e.g. "click" instead of "CLICK")
    action_type = ActionType[action["action_type"].upper()]

    if not action_type.is_web_action():
        # LLM sometimes hallucinates and returns element id for non-web actions such as WAIT, TERMINATE, COMPLETE etc.
        # That can sometimes cause cached action plan to be invalidated. This way we're making sure the element id is not
        # set for non-web actions.
        base_action_dict["element_id"] = None

    if action_type == ActionType.TERMINATE:
        return TerminateAction(**base_action_dict, errors=action["errors"] if "errors" in action else [])

    if action_type == ActionType.CLICK:
        file_url = action["file_url"] if "file_url" in action else None
        return ClickAction(**base_action_dict, file_url=file_url, download=action.get("download", False))

    if action_type == ActionType.INPUT_TEXT:
        return InputTextAction(**base_action_dict, text=action["text"])

    if action_type == ActionType.UPLOAD_FILE:
        # TODO: see if the element is a file input element. if it's not, convert this action into a click action
        return UploadFileAction(
            **base_action_dict,
            file_url=action["file_url"],
        )

    # This action is not used in the current implementation. Click actions are used instead.
    if action_type == ActionType.DOWNLOAD_FILE:
        return DownloadFileAction(**base_action_dict, file_name=action["file_name"])

    if action_type == ActionType.SELECT_OPTION:
        option = action["option"]
        if option is None:
            raise ValueError("SelectOptionAction requires an 'option' field")
        label = option.get("label")
        value = option.get("value")
        index = option.get("index")
        if label is None and value is None and index is None:
            raise ValueError("At least one of 'label', 'value', or 'index' must be provided for a SelectOption")
        return SelectOptionAction(
            **base_action_dict,
            option=SelectOption(
                label=label,
                value=value,
                index=index,
            ),
        )

    if action_type == ActionType.CHECKBOX:
        return CheckboxAction(
            **base_action_dict,
            is_checked=action["is_checked"],
        )

    if action_type == ActionType.WAIT:
        return WaitAction(**base_action_dict)

    if action_type == ActionType.COMPLETE:
        return CompleteAction(
            **base_action_dict,
            data_extraction_goal=data_extraction_goal,
            errors=action["errors"] if "errors" in action else [],
        )

    if action_type == "null":
        return NullAction(**base_action_dict)

    if action_type == ActionType.SOLVE_CAPTCHA:
        return SolveCaptchaAction(**base_action_dict)

    raise UnsupportedActionType(action_type=action_type)


def parse_actions(
    task: Task, step_id: str, step_order: int, scraped_page: ScrapedPage, json_response: list[Dict[str, Any]]
) -> list[Action]:
    actions: list[Action] = []
    for idx, action in enumerate(json_response):
        try:
            action_instance = parse_action(
                action=action, scraped_page=scraped_page, data_extraction_goal=task.data_extraction_goal
            )
            action_instance.organization_id = task.organization_id
            action_instance.workflow_run_id = task.workflow_run_id
            action_instance.task_id = task.task_id
            action_instance.step_id = step_id
            action_instance.step_order = step_order
            action_instance.action_order = idx
            if isinstance(action_instance, TerminateAction):
                LOG.warning(
                    "Agent decided to terminate",
                    task_id=task.task_id,
                    llm_response=json_response,
                    reasoning=action_instance.reasoning,
                    actions=actions,
                )
            actions.append(action_instance)

        except UnsupportedActionType:
            LOG.error(
                "Unsupported action type when parsing actions",
                task_id=task.task_id,
                raw_action=action,
                exc_info=True,
            )
        except (ValidationError, ValueError):
            LOG.warning(
                "Invalid action",
                task_id=task.task_id,
                raw_action=action,
                exc_info=True,
            )
        except Exception:
            LOG.error(
                "Failed to marshal action",
                task_id=task.task_id,
                raw_action=action,
                exc_info=True,
            )

    ############################ This part of code might not be needed ############################
    # Reason #1. validation can be done in action handler but not in parser
    # Reason #2. no need to validate whether the element_id has a hash.
    # If there's no hash, we can fall back to normal operation
    all_element_ids = [action.element_id for action in actions if action.element_id]
    missing_element_ids = [
        element_id for element_id in all_element_ids if element_id not in scraped_page.id_to_element_hash
    ]
    if missing_element_ids:
        LOG.warning(
            "Missing elements in scraped page",
            task_id=task.task_id,
            missing_element_ids=missing_element_ids,
            all_element_ids=all_element_ids,
        )
    ############################ This part of code might not be needed ############################
    return actions
