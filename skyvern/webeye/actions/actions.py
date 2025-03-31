from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Type, TypeVar

import structlog
from litellm import ConfigDict
from pydantic import BaseModel, Field

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
    RELOAD_PAGE = "reload_page"

    EXTRACT = "extract"

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
    intention: str | None = None
    field: str | None = None
    is_required: bool | None = None
    is_search_bar: bool | None = None  # don't trigger custom-selection logic when it's a search bar
    is_location_input: bool | None = None  # address input usually requires auto completion
    is_date_related: bool | None = None  # date picker mini agent requires some special logic

    def __repr__(self) -> str:
        return f"InputOrSelectContext(field={self.field}, is_required={self.is_required}, is_search_bar={self.is_search_bar}, is_location_input={self.is_location_input}, intention={self.intention})"


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
    verified: bool = False

    created_at: datetime | None = None
    modified_at: datetime | None = None

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
            elif action_type is ActionType.RELOAD_PAGE:
                return ReloadPageAction.model_validate(value)
            else:
                raise ValueError(f"Unsupported action type: {action_type}")
        else:
            raise ValueError("Invalid action data")


class WebAction(Action):
    element_id: Annotated[str, Field(coerce_numbers_to_str=True)]


class DecisiveAction(Action):
    errors: list[UserDefinedError] = []


# TODO: consider to implement this as a WebAction in the future
class ReloadPageAction(Action):
    action_type: ActionType = ActionType.RELOAD_PAGE


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


class ExtractAction(Action):
    action_type: ActionType = ActionType.EXTRACT
    data_extraction_goal: str | None = None
    data_extraction_schema: dict[str, Any] | None = None


class ScrapeResult(BaseModel):
    """
    Scraped response from a webpage, including:
    1. JSON representation of what the user is seeing
    """

    scraped_data: dict[str, Any] | list | str | None
