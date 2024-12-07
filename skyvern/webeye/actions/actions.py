from enum import StrEnum
from typing import Annotated, Any, Dict, Type, TypeVar

import structlog
from litellm import ConfigDict
from pydantic import BaseModel, Field, ValidationError

from skyvern.exceptions import UnsupportedActionType
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()
T = TypeVar("T", bound="Action")


class ActionType(StrEnum):
    CLICK = "click"
    INPUT_TEXT = "input_text"
    UPLOAD_FILE = "upload_file"

    # This action is not used in the current implementation. Click actions are used instead."
    DOWNLOAD_FILE = "download_file"

    SELECT_OPTION = "select_option"
    CHECKBOX = "checkbox"
    WAIT = "wait"
    NULL_ACTION = "null_action"
    SOLVE_CAPTCHA = "solve_captcha"
    TERMINATE = "terminate"
    COMPLETE = "complete"

    def is_web_action(self) -> bool:
        return self in [
            ActionType.CLICK,
            ActionType.INPUT_TEXT,
            ActionType.UPLOAD_FILE,
            ActionType.DOWNLOAD_FILE,
            ActionType.SELECT_OPTION,
            ActionType.CHECKBOX,
        ]


class ActionStatus(StrEnum):
    pending = "pending"
    skipped = "skipped"
    failed = "failed"
    completed = "completed"


class UserDefinedError(BaseModel):
    error_code: str
    reasoning: str
    confidence_float: float = Field(..., ge=0, le=1)

    def __repr__(self) -> str:
        return f"{self.reasoning}(error_code={self.error_code}, confidence_float={self.confidence_float})"


class SelectOption(BaseModel):
    label: str | None = None
    value: str | None = None
    index: int | None = None

    def __repr__(self) -> str:
        return f"SelectOption(label={self.label}, value={self.value}, index={self.index})"


class CompleteVerifyResult(BaseModel):
    user_goal_achieved: bool
    thoughts: str
    page_info: str | None = None

    def __repr__(self) -> str:
        return f"CompleteVerifyResponse(thoughts={self.thoughts}, user_goal_achieved={self.user_goal_achieved}, page_info={self.page_info})"


class InputOrSelectContext(BaseModel):
    field: str | None = None
    is_required: bool | None = None
    is_search_bar: bool | None = None  # don't trigger custom-selection logic when it's a search bar
    is_location_input: bool | None = None  # address input usually requires auto completion

    def __repr__(self) -> str:
        return f"InputOrSelectContext(field={self.field}, is_required={self.is_required}, is_search_bar={self.is_search_bar}, is_location_input={self.is_location_input})"


class Action(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_type: ActionType
    status: ActionStatus = ActionStatus.pending
    action_id: str | None = None
    source_action_id: str | None = None
    organization_id: str | None = None
    workflow_run_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    step_order: int | None = None
    action_order: int | None = None
    confidence_float: float | None = None
    description: str | None = None
    reasoning: str | None = None
    intention: str | None = None
    response: str | None = None
    element_id: Annotated[str, Field(coerce_numbers_to_str=True)] | None = None
    skyvern_element_hash: str | None = None
    skyvern_element_data: dict[str, Any] | None = None

    # DecisiveAction (CompleteAction, TerminateAction) fields
    errors: list[UserDefinedError] | None = None
    data_extraction_goal: str | None = None

    # WebAction fields
    file_name: str | None = None
    file_url: str | None = None
    download: bool | None = None
    is_upload_file_tag: bool | None = None
    text: str | None = None
    option: SelectOption | None = None
    is_checked: bool | None = None

    @classmethod
    def validate(cls: Type[T], value: Any) -> T:
        if isinstance(value, dict):
            action_type = value["action_type"]

            if action_type is ActionType.CLICK:
                return ClickAction.model_validate(value)
            elif action_type is ActionType.INPUT_TEXT:
                return InputTextAction.model_validate(value)
            elif action_type is ActionType.UPLOAD_FILE:
                return UploadFileAction.model_validate(value)
            elif action_type is ActionType.DOWNLOAD_FILE:
                return DownloadFileAction.model_validate(value)
            elif action_type is ActionType.NULL_ACTION:
                return NullAction.model_validate(value)
            elif action_type is ActionType.TERMINATE:
                return TerminateAction.model_validate(value)
            elif action_type is ActionType.COMPLETE:
                return CompleteAction.model_validate(value)
            elif action_type is ActionType.SELECT_OPTION:
                return SelectOptionAction.model_validate(value)
            elif action_type is ActionType.CHECKBOX:
                return CheckboxAction.model_validate(value)
            elif action_type is ActionType.WAIT:
                return WaitAction.model_validate(value)
            elif action_type is ActionType.SOLVE_CAPTCHA:
                return SolveCaptchaAction.model_validate(value)
            else:
                raise ValueError(f"Unsupported action type: {action_type}")
        else:
            raise ValueError("Invalid action data")


class WebAction(Action):
    element_id: Annotated[str, Field(coerce_numbers_to_str=True)]


class DecisiveAction(Action):
    errors: list[UserDefinedError] = []


class ClickAction(WebAction):
    action_type: ActionType = ActionType.CLICK
    file_url: str | None = None
    download: bool = False

    def __repr__(self) -> str:
        return f"ClickAction(element_id={self.element_id}, file_url={self.file_url}, download={self.download})"


class InputTextAction(WebAction):
    action_type: ActionType = ActionType.INPUT_TEXT
    text: str

    def __repr__(self) -> str:
        return f"InputTextAction(element_id={self.element_id}, text={self.text})"


class UploadFileAction(WebAction):
    action_type: ActionType = ActionType.UPLOAD_FILE
    file_url: str
    is_upload_file_tag: bool = True

    def __repr__(self) -> str:
        return f"UploadFileAction(element_id={self.element_id}, file={self.file_url}, is_upload_file_tag={self.is_upload_file_tag})"


# this is a deprecated action type
class DownloadFileAction(WebAction):
    action_type: ActionType = ActionType.DOWNLOAD_FILE
    file_name: str

    def __repr__(self) -> str:
        return f"DownloadFileAction(element_id={self.element_id}, file_name={self.file_name})"


class NullAction(Action):
    action_type: ActionType = ActionType.NULL_ACTION


class SolveCaptchaAction(Action):
    action_type: ActionType = ActionType.SOLVE_CAPTCHA


class SelectOptionAction(WebAction):
    action_type: ActionType = ActionType.SELECT_OPTION
    option: SelectOption

    def __repr__(self) -> str:
        return f"SelectOptionAction(element_id={self.element_id}, option={self.option})"


###
# This action causes more harm than it does good.
# It frequently mis-behaves, or gets stuck in click loops.
# Treating checkbox actions as click actions seem to perform way more reliably
# Developers who tried this and failed: 2 (Suchintan and Shu 😂)
###
class CheckboxAction(WebAction):
    action_type: ActionType = ActionType.CHECKBOX
    is_checked: bool

    def __repr__(self) -> str:
        return f"CheckboxAction(element_id={self.element_id}, is_checked={self.is_checked})"


class WaitAction(Action):
    action_type: ActionType = ActionType.WAIT


class TerminateAction(DecisiveAction):
    action_type: ActionType = ActionType.TERMINATE


class CompleteAction(DecisiveAction):
    action_type: ActionType = ActionType.COMPLETE
    verified: bool = False
    data_extraction_goal: str | None = None


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


class ScrapeResult(BaseModel):
    """
    Scraped response from a webpage, including:
    1. JSON representation of what the user is seeing
    """

    scraped_data: dict[str, Any] | list[dict[str, Any]]
