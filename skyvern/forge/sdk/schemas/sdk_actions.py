from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from skyvern.config import settings


class SdkActionType(str, Enum):
    """Enum for SDK action types that can be executed."""

    AI_CLICK = "ai_click"
    AI_INPUT_TEXT = "ai_input_text"
    AI_SELECT_OPTION = "ai_select_option"
    AI_ACT = "ai_act"
    EXTRACT = "extract"


# Base action class
class SdkActionBase(BaseModel):
    """Base class for SDK actions."""

    type: str = Field(..., description="The type of action")


# Specific action types
class ClickAction(SdkActionBase):
    """Click action parameters."""

    type: Literal["ai_click"] = "ai_click"
    selector: str | None = Field(default="", description="CSS selector for the element")
    intention: str = Field(default="", description="The intention or goal of the click")
    data: str | dict[str, Any] | None = Field(None, description="Additional context data")
    timeout: float = Field(default=settings.BROWSER_ACTION_TIMEOUT_MS, description="Timeout in milliseconds")


class InputTextAction(SdkActionBase):
    """Input text action parameters."""

    type: Literal["ai_input_text"] = "ai_input_text"
    selector: str | None = Field(default="", description="CSS selector for the element")
    value: str | None = Field(default="", description="Value to input")
    intention: str = Field(default="", description="The intention or goal of the input")
    data: str | dict[str, Any] | None = Field(None, description="Additional context data")
    totp_identifier: str | None = Field(None, description="TOTP identifier for input_text actions")
    totp_url: str | None = Field(None, description="TOTP URL for input_text actions")
    timeout: float = Field(default=settings.BROWSER_ACTION_TIMEOUT_MS, description="Timeout in milliseconds")


class SelectOptionAction(SdkActionBase):
    """Select option action parameters."""

    type: Literal["ai_select_option"] = "ai_select_option"
    selector: str | None = Field(default="", description="CSS selector for the element")
    value: str | None = Field(default="", description="Value to select")
    intention: str = Field(default="", description="The intention or goal of the selection")
    data: str | dict[str, Any] | None = Field(None, description="Additional context data")
    timeout: float = Field(default=settings.BROWSER_ACTION_TIMEOUT_MS, description="Timeout in milliseconds")


class ActAction(SdkActionBase):
    """AI act action parameters."""

    type: Literal["ai_act"] = "ai_act"
    intention: str = Field(default="", description="Natural language prompt for the action")
    data: str | dict[str, Any] | None = Field(None, description="Additional context data")


class ExtractAction(SdkActionBase):
    """Extract data action parameters."""

    type: Literal["extract"] = "extract"
    prompt: str = Field(default="", description="Extraction prompt")
    extract_schema: dict[str, Any] | list | str | None = Field(None, description="Schema for extraction")
    error_code_mapping: dict[str, str] | None = Field(None, description="Error code mapping for extraction")
    intention: str | None = Field(None, description="The intention or goal of the extraction")
    data: str | dict[str, Any] | None = Field(None, description="Additional context data")


# Discriminated union of all action types
SdkAction = Annotated[
    Union[ClickAction, InputTextAction, SelectOptionAction, ActAction, ExtractAction],
    Field(discriminator="type"),
]


class RunActionResponse(BaseModel):
    """Response from running an action."""

    workflow_run_id: str = Field(..., description="The workflow run ID used for this action")


class RunSdkActionRequest(BaseModel):
    """Request to run a single SDK action."""

    url: str = Field(..., description="The URL where the action should be executed")
    browser_session_id: str | None = Field(None, description="The browser session ID")
    browser_address: str | None = Field(None, description="The browser address")
    workflow_run_id: str | None = Field(
        None, description="Optional workflow run ID to continue an existing workflow run"
    )
    action: SdkAction = Field(..., description="The action to execute with its specific parameters")


class RunSdkActionResponse(BaseModel):
    """Response from running an SDK action."""

    workflow_run_id: str = Field(..., description="The workflow run ID used for this action")
    result: Any | None = Field(None, description="The result from the action (e.g., selector, value, extracted data)")
