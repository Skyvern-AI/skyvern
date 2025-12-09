import ast
import re
from typing import Any, Dict, Match

import structlog
from openai.types.responses.response import Response as OpenAIResponse
from pydantic import ValidationError

from skyvern.constants import SCROLL_AMOUNT_MULTIPLIER
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound, UnsupportedActionType
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import poll_otp_value
from skyvern.utils.image_resizer import Resolution, scale_coordinates
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    CheckboxAction,
    ClickAction,
    ClickContext,
    ClosePageAction,
    CompleteAction,
    DownloadFileAction,
    DragAction,
    GotoUrlAction,
    HoverAction,
    InputOrSelectContext,
    InputTextAction,
    KeypressAction,
    LeftMouseAction,
    MoveAction,
    NullAction,
    ScrollAction,
    SelectOption,
    SelectOptionAction,
    SolveCaptchaAction,
    TerminateAction,
    UploadFileAction,
    VerificationCodeAction,
    WaitAction,
)
from skyvern.webeye.scraper.scraped_page import ScrapedPage

LOG = structlog.get_logger()


def parse_action(
    action: Dict[str, Any],
    scraped_page: ScrapedPage,
    data_extraction_goal: str | None = None,
    totp_code_required: bool = False,
) -> Action:
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
    input_or_select_context: InputOrSelectContext | None = None

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
        click_context = action.get("click_context", None)
        if click_context:
            click_context = ClickContext.model_validate(click_context)
        return ClickAction(
            **base_action_dict,
            file_url=file_url,
            download=action.get("download", False),
            click_context=click_context,
        )

    if action_type == ActionType.INPUT_TEXT:
        context_dict = action.get("context", {})
        if context_dict and len(context_dict) > 0:
            context_dict["intention"] = intention
            input_or_select_context = InputOrSelectContext.model_validate(context_dict)
        return InputTextAction(
            **base_action_dict,
            text=action["text"],
            input_or_select_context=input_or_select_context,
            totp_code_required=totp_code_required,
        )

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

        context_dict = action.get("context", {})
        if context_dict and len(context_dict) > 0:
            context_dict["intention"] = intention
            input_or_select_context = InputOrSelectContext.model_validate(context_dict)

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
            input_or_select_context=input_or_select_context,
            download=action.get("download", False),
        )

    if action_type == ActionType.CHECKBOX:
        return CheckboxAction(
            **base_action_dict,
            is_checked=action["is_checked"],
        )

    if action_type == ActionType.WAIT:
        return WaitAction(**base_action_dict)

    if action_type == ActionType.HOVER:
        return HoverAction(**base_action_dict, hold_seconds=action.get("hold_seconds", 0) or 0)

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

    if action_type == ActionType.CLOSE_PAGE:
        return ClosePageAction(**base_action_dict)

    raise UnsupportedActionType(action_type=action_type)


def parse_actions(
    task: Task, step_id: str, step_order: int, scraped_page: ScrapedPage, json_response: list[Dict[str, Any]]
) -> list[Action]:
    actions: list[Action] = []
    context = skyvern_context.ensure_context()
    totp_code = context.totp_codes.get(task.task_id)
    totp_code_required = bool(totp_code)
    for idx, action in enumerate(json_response):
        try:
            action_instance = parse_action(
                action=action,
                scraped_page=scraped_page,
                data_extraction_goal=task.data_extraction_goal,
                totp_code_required=totp_code_required,
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


async def parse_cua_actions(
    task: Task,
    step: Step,
    response: OpenAIResponse,
) -> list[Action]:
    computer_calls = [item for item in response.output if item.type == "computer_call"]
    reasonings = [item for item in response.output if item.type == "reasoning"]
    assistant_messages = [item for item in response.output if item.type == "message" and item.role == "assistant"]
    actions: list[Action] = []
    for idx, computer_call in enumerate(computer_calls):
        cua_action = computer_call.action
        action_type = cua_action.type
        try:
            reasoning = None
            if idx < len(reasonings):
                try:
                    reasoning = reasonings[idx].summary[0].text
                except Exception:
                    LOG.exception(
                        "Failed to parse reasoning",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                    )

            match action_type:
                case "click":
                    button = cua_action.button
                    if button != "left" and button != "right":
                        button = "left"
                    reasoning = reasoning or f"Click at: ({cua_action.x}, {cua_action.y})"
                    action = ClickAction(
                        element_id="",
                        x=cua_action.x,
                        y=cua_action.y,
                        button=button,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=f"Click at: ({cua_action.x}, {cua_action.y})",
                    )
                case "scroll":
                    reasoning = reasoning or f"Scroll by: ({cua_action.x}, {cua_action.y})"
                    action = ScrollAction(
                        element_id="",
                        x=cua_action.x,
                        y=cua_action.y,
                        scroll_x=cua_action.scroll_x,
                        scroll_y=cua_action.scroll_y,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=f"Scroll by: ({cua_action.x}, {cua_action.y})",
                    )
                case "keypress":
                    reasoning_str = f"Press keys: {cua_action.keys}"
                    if len(cua_action.keys) == 1:
                        reasoning_str = f"Press the '{cua_action.keys[0]}' key"
                    reasoning = reasoning or reasoning_str
                    action = KeypressAction(
                        element_id="",
                        keys=cua_action.keys,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=str(cua_action.keys),
                    )
                case "type":
                    action = InputTextAction(
                        element_id="",
                        text=cua_action.text,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=cua_action.text,
                    )
                case "wait":
                    action = WaitAction(
                        seconds=5,
                        reasoning=reasoning,
                        intention=reasoning,
                    )
                case "move":
                    response = f"Move mouse to: ({cua_action.x}, {cua_action.y})"
                    reasoning = reasoning or response
                    action = MoveAction(
                        x=cua_action.x,
                        y=cua_action.y,
                        reasoning=reasoning,
                        intention=reasoning,
                    )
                case "drag":
                    whole_path = cua_action.path
                    if not whole_path or len(whole_path) < 2:
                        LOG.warning(
                            "Invalid drag action",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            step_order=step.order,
                            action_order=idx,
                            whole_path=whole_path,
                        )
                        action = WaitAction(
                            seconds=5,
                            reasoning=reasoning,
                            intention=reasoning,
                        )
                    else:
                        start_x, start_y = whole_path[0][0], whole_path[0][1]
                        reasoning = reasoning or f"Drag action path: {whole_path}"
                        action = DragAction(
                            start_x=start_x,
                            start_y=start_y,
                            path=whole_path[1:],
                            reasoning=reasoning,
                            intention=reasoning,
                        )
                case "screenshot":
                    action = NullAction(
                        reasoning=reasoning,
                        intention=reasoning,
                    )
                case _:
                    raise ValueError(f"Unsupported action type: {action_type}")
            action.organization_id = task.organization_id
            action.workflow_run_id = task.workflow_run_id
            action.task_id = task.task_id
            action.step_id = step.step_id
            action.step_order = step.order
            action.action_order = idx
            actions.append(action)
        except Exception:
            LOG.exception(
                "Failed to parse action",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                action_order=idx,
            )
            break
    if not actions:
        LOG.info(
            "Empty action returned by CUA",
            task_id=task.task_id,
            step_id=step.step_id,
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            response=response.model_dump(),
        )
        reasoning = reasonings[0].summary[0].text if reasonings and reasonings[0].summary else None
        assistant_message = assistant_messages[0].content[0].text if assistant_messages else None
        actions = await generate_cua_fallback_actions(task, step, assistant_message, reasoning)
    return actions


async def parse_anthropic_actions(
    task: Task,
    step: Step,
    assistant_content: list[dict[str, Any]],
    browser_window_dimension: Resolution,
    screenshot_resize_target_dimension: Resolution,
) -> list[Action]:
    tool_calls = [block for block in assistant_content if block["type"] == "tool_use" and block["name"] == "computer"]
    reasonings = [block for block in assistant_content if block["type"] == "thinking"]
    LOG.info("Anthropic tool calls", tool_calls=tool_calls, reasonings=reasonings, assistant_content=assistant_content)
    if len(reasonings) > 1:
        LOG.warning(
            "Anthropic CUA: multiple reasonings in assistant content",
            task_id=task.task_id,
            step_id=step.step_id,
            assistant_content=assistant_content,
        )
    reasoning = reasonings[0]["thinking"] if reasonings else None
    idx = 0
    actions: list[Action] = []
    while idx < len(tool_calls):
        tool_call = tool_calls[idx]
        try:
            tool_call_id = tool_call["id"]
            tool_call_input = tool_call.get("input")
            if not tool_call_input:
                idx += 1
                continue
            action = tool_call_input["action"]
            if action == "mouse_move":
                coordinate = tool_call_input.get("coordinate")
                if not coordinate:
                    LOG.warning(
                        "Anthropic CUA error: mouse move action has no coordinate",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                # (x, y) is the coordinate in resized screenshots. We need to scale it to the browser window dimension.
                x, y = validate_and_get_coordinates(
                    coordinate, screenshot_resize_target_dimension, browser_window_dimension
                )
                response = f"Move mouse to: ({x}, {y})"
                reasoning = reasoning or response
                actions.append(
                    # TODO: add response by adding specifying the element to move to
                    MoveAction(
                        x=x,
                        y=y,
                        reasoning=reasoning,
                        intention=reasoning,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action in ["left_click", "double_click", "triple_click", "right_click"]:
                coordinate = tool_call_input.get("coordinate")
                if not coordinate and idx - 1 >= 0:
                    prev_tool_call = tool_calls[idx - 1]
                    prev_tool_call_input = prev_tool_call.get("input")
                    if prev_tool_call_input and prev_tool_call_input["action"] == "mouse_move":
                        coordinate = prev_tool_call_input.get("coordinate")

                if not coordinate:
                    LOG.warning(
                        "Anthropic CUA error: left click action has no coordinate and it doesn't have mouse_move before it",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                x, y = validate_and_get_coordinates(
                    coordinate, screenshot_resize_target_dimension, browser_window_dimension
                )
                repeat = 1
                if action == "double_click":
                    repeat = 2
                elif action == "triple_click":
                    repeat = 3

                response = f"Click at: ({x}, {y})"
                reasoning = reasoning or response
                button = "left"
                if action == "right_click":
                    button = "right"
                actions.append(
                    ClickAction(
                        element_id="",
                        x=x,
                        y=y,
                        button=button,
                        repeat=repeat,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=response,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action == "type":
                text = tool_call_input.get("text")
                if not text:
                    LOG.warning(
                        "Anthropic CUA error: type action has no text",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                actions.append(
                    InputTextAction(
                        element_id="",
                        text=text,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=text,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action in ["key", "hold_key"]:
                text = tool_call_input.get("text", "")
                if not text:
                    LOG.warning(
                        "Anthropic CUA error: key action has no text",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                response = f"Press keys: {text}"
                keys = text.split("+")
                hold = action == "hold_key"
                duration = tool_call_input.get("duration", 0)
                if hold:
                    response = f"Hold keys for {duration} seconds: {text}"
                reasoning = reasoning or response
                actions.append(
                    KeypressAction(
                        element_id="",
                        keys=keys,
                        hold=hold,
                        duration=duration,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=response,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action == "screenshot":
                actions.append(
                    NullAction(
                        reasoning=reasoning,
                        intention=reasoning,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action == "scroll":
                x, y = None, None
                coordinate = tool_call_input.get("coordinate")
                if coordinate:
                    x, y = validate_and_get_coordinates(
                        coordinate, browser_window_dimension, screenshot_resize_target_dimension
                    )
                scroll_direction = tool_call_input.get("scroll_direction")
                scroll_amount = tool_call_input.get("scroll_amount")
                if scroll_direction == "up":
                    scroll_x = 0
                    scroll_y = -scroll_amount * SCROLL_AMOUNT_MULTIPLIER
                elif scroll_direction == "down":
                    scroll_x = 0
                    scroll_y = scroll_amount * SCROLL_AMOUNT_MULTIPLIER
                elif scroll_direction == "left":
                    scroll_x = -scroll_amount * SCROLL_AMOUNT_MULTIPLIER
                    scroll_y = 0
                elif scroll_direction == "right":
                    scroll_x = scroll_amount * SCROLL_AMOUNT_MULTIPLIER
                    scroll_y = 0
                else:
                    LOG.warning(
                        "Anthropic CUA error: unsupported scroll direction",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                response = f"Scroll by: ({scroll_x}, {scroll_y})"
                reasoning = reasoning or response
                actions.append(
                    ScrollAction(
                        element_id="",
                        x=x,
                        y=y,
                        scroll_x=scroll_x,
                        scroll_y=scroll_y,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=response,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action in ["left_mouse_down", "left_mouse_up"]:
                coordinate = tool_call_input.get("coordinate")
                x, y = None, None
                if coordinate:
                    x, y = validate_and_get_coordinates(
                        coordinate, browser_window_dimension, screenshot_resize_target_dimension
                    )
                direction = "down" if action == "left_mouse_down" else "up"
                response = f"Left mouse {direction} at: ({x}, {y})"
                reasoning = reasoning or response
                actions.append(
                    LeftMouseAction(
                        x=x,
                        y=y,
                        direction=direction,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=response,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action == "left_click_drag":
                coordinate = tool_call_input.get("coordinate")
                start_coordinate = tool_call_input.get("start_coordinate")
                LOG.info(
                    "Anthropic CUA left click drag action", coordinate=coordinate, start_coordinate=start_coordinate
                )
                if not coordinate or not start_coordinate:
                    LOG.warning(
                        "Anthropic CUA error: left click drag action has no coordinate or start coordinate",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                x, y = validate_and_get_coordinates(
                    coordinate, browser_window_dimension, screenshot_resize_target_dimension
                )
                start_x, start_y = validate_and_get_coordinates(
                    start_coordinate, browser_window_dimension, screenshot_resize_target_dimension
                )
                response = f"Drag from ({start_x}, {start_y}) to ({x}, {y})"
                reasoning = reasoning or response
                actions.append(
                    DragAction(
                        start_x=start_x,
                        start_y=start_y,
                        path=[(x, y)],
                        reasoning=reasoning,
                        intention=reasoning,
                        response=response,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            elif action == "wait":
                duration = tool_call_input.get("duration", 5)
                actions.append(
                    WaitAction(
                        seconds=duration,
                        reasoning=reasoning,
                        intention=reasoning,
                        response=f"Wait for {duration} seconds",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=idx,
                        tool_call_id=tool_call_id,
                    )
                )
            else:
                LOG.error(
                    "Anthropic CUA error: unsupported action",
                    tool_call=tool_call,
                )
            idx += 1
        except Exception:
            LOG.exception(
                "Anthropic CUA error: failed to parse action",
                task_id=task.task_id,
                step_id=step.step_id,
                tool_call=tool_call,
            )
            break
    if not actions:
        reasoning = reasonings[0]["thinking"] if reasonings else None
        assistant_messages = [block for block in assistant_content if block["type"] == "text"]
        assistant_message = assistant_messages[0]["text"] if assistant_messages else None
        actions = await generate_cua_fallback_actions(task, step, assistant_message, reasoning)
    return actions


# function from anthropic's quickstart guide
# https://github.com/anthropics/anthropic-quickstarts/blob/81c4085944abb1734db411f05290b538fdc46dcd/computer-use-demo/computer_use_demo/tools/computer.py#L214C1-L221C1
def validate_and_get_coordinates(
    coordinate: tuple[int, int] | list[int],
    current_dimension: Resolution,
    target_dimension: Resolution,
) -> tuple[int, int]:
    if len(coordinate) != 2:
        raise ValueError(f"{coordinate} must be a tuple of length 2")
    if not all(isinstance(i, int) and i >= 0 for i in coordinate):
        raise ValueError(f"{coordinate} must be a tuple of non-negative ints")

    return scale_coordinates((coordinate[0], coordinate[1]), current_dimension, target_dimension)


async def generate_cua_fallback_actions(
    task: Task,
    step: Step,
    assistant_message: str | None,
    reasoning: str | None,
) -> list[Action]:
    fallback_action_prompt = prompt_engine.load_prompt(
        "cua-fallback-action",
        navigation_goal=task.navigation_goal,
        assistant_message=assistant_message,
        assistant_reasoning=reasoning,
    )

    action_response = await app.LLM_API_HANDLER(
        prompt=fallback_action_prompt,
        prompt_name="cua-fallback-action",
        step=step,
    )
    LOG.info("Fallback action response", action_response=action_response)
    skyvern_action_type = action_response.get("action")
    useful_information = action_response.get("useful_information")

    # use 'other' action as fallback in the 'cua-fallback-action' prompt
    # it can avoid LLM returning unreasonable actions, and fallback to use 'wait' action in agent instead
    action = WaitAction(
        seconds=5,
        reasoning=reasoning,
        intention=reasoning,
    )
    if skyvern_action_type == "complete":
        LOG.info(
            "Updating task with useful information",
            task_id=task.task_id,
            organization_id=task.organization_id,
            useful_information=useful_information,
            assistant_message=assistant_message,
            reasoning=reasoning,
        )
        await app.DATABASE.update_task(
            task.task_id,
            organization_id=task.organization_id,
            extracted_information=assistant_message,
        )
        action = CompleteAction(
            reasoning=reasoning,
            intention=reasoning,
            verified=True,
            data_extraction_goal=task.data_extraction_goal,
        )
    elif skyvern_action_type == "terminate":
        action = TerminateAction(
            reasoning=reasoning,
            intention=reasoning,
        )
    elif skyvern_action_type == "solve_captcha":
        action = SolveCaptchaAction(
            reasoning=reasoning,
            intention=reasoning,
        )
    elif skyvern_action_type == "get_magic_link":
        if (task.totp_verification_url or task.totp_identifier) and task.organization_id:
            LOG.info(
                "Getting magic link for CUA",
                task_id=task.task_id,
                organization_id=task.organization_id,
                workflow_run_id=task.workflow_run_id,
                totp_verification_url=task.totp_verification_url,
                totp_identifier=task.totp_identifier,
            )
            try:
                otp_value = await poll_otp_value(
                    organization_id=task.organization_id,
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                    totp_verification_url=task.totp_verification_url,
                    totp_identifier=task.totp_identifier,
                )
                if not otp_value or otp_value.get_otp_type() != OTPType.MAGIC_LINK:
                    raise NoTOTPVerificationCodeFound()
                magic_link = otp_value.value
                reasoning = reasoning or "Received magic link. Navigating to the magic link URL to verify the login"
                action = GotoUrlAction(
                    url=magic_link,
                    reasoning=reasoning,
                    intention=reasoning,
                    is_magic_link=True,
                )
            except NoTOTPVerificationCodeFound:
                reasoning_suffix = "No magic link found"
                reasoning = f"{reasoning}. {reasoning_suffix}" if reasoning else reasoning_suffix
                action = TerminateAction(
                    reasoning=reasoning,
                    intention=reasoning,
                )
            except FailedToGetTOTPVerificationCode as e:
                reasoning_suffix = f"Failed to get magic link. Reason: {e.reason}"
                reasoning = f"{reasoning}. {reasoning_suffix}" if reasoning else reasoning_suffix
                action = TerminateAction(
                    reasoning=reasoning,
                    intention=reasoning,
                )
        else:
            action = TerminateAction(
                reasoning=reasoning,
                intention=reasoning,
            )

    elif skyvern_action_type == "get_verification_code":
        if (task.totp_verification_url or task.totp_identifier) and task.organization_id:
            LOG.info(
                "Getting verification code for CUA",
                task_id=task.task_id,
                organization_id=task.organization_id,
                workflow_run_id=task.workflow_run_id,
                totp_verification_url=task.totp_verification_url,
                totp_identifier=task.totp_identifier,
            )
            try:
                otp_value = await poll_otp_value(
                    organization_id=task.organization_id,
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                    totp_verification_url=task.totp_verification_url,
                    totp_identifier=task.totp_identifier,
                )
                if not otp_value or otp_value.get_otp_type() != OTPType.TOTP:
                    raise NoTOTPVerificationCodeFound()
                verification_code = otp_value.value
                reasoning = reasoning or f"Received verification code: {verification_code}"
                action = VerificationCodeAction(
                    verification_code=verification_code,
                    reasoning=reasoning,
                    intention=reasoning,
                )
            except NoTOTPVerificationCodeFound:
                reasoning_suffix = "No verification code found"
                reasoning = f"{reasoning}. {reasoning_suffix}" if reasoning else reasoning_suffix
                action = TerminateAction(
                    reasoning=reasoning,
                    intention=reasoning,
                )
            except FailedToGetTOTPVerificationCode as e:
                reasoning_suffix = f"Failed to get verification code. Reason: {e.reason}"
                reasoning = f"{reasoning}. {reasoning_suffix}" if reasoning else reasoning_suffix
                action = TerminateAction(
                    reasoning=reasoning,
                    intention=reasoning,
                )
        else:
            action = TerminateAction(
                reasoning=reasoning,
                intention=reasoning,
            )

    action.organization_id = task.organization_id
    action.workflow_run_id = task.workflow_run_id
    action.task_id = task.task_id
    action.step_id = step.step_id
    action.step_order = step.order
    action.action_order = 0
    return [action]


async def parse_ui_tars_actions(
    task: Task,
    step: Step,
    response_content: str,
    browser_window_dimension: Resolution,
) -> list[Action]:
    """Parse UI-TARS response and convert to Skyvern actions."""
    try:
        # Parse the UI-TARS response text
        parsed_actions = _parse_ui_tars_response(response_content, browser_window_dimension)

        actions: list[Action] = []
        for idx, parsed_action in enumerate(parsed_actions):
            try:
                action = _create_ui_tars_action(parsed_action, task, step, browser_window_dimension, idx)
                if action:
                    actions.append(action)
            except Exception:
                LOG.exception(
                    "Failed to create UI-TARS action",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    parsed_action=parsed_action,
                )
                continue

        if not actions:
            LOG.warning(
                "No valid actions generated from UI-TARS response",
                task_id=task.task_id,
                step_id=step.step_id,
                response_preview=response_content[:200],
            )

        return actions

    except Exception:
        LOG.exception(
            "Failed to parse UI-TARS actions",
            task_id=task.task_id,
            step_id=step.step_id,
            response_content=response_content[:200],
        )
        return []


def _parse_ui_tars_response(response_content: str, browser_window_dimension: Resolution) -> list[dict[str, Any]]:
    """Parse UI-TARS response text into structured action data.

    Extracts essential parsing logic from action_parser.py without the complex coordinate transformations.
    """
    text = response_content.strip()

    # Convert point format to coordinates if needed
    if "<point>" in text:
        text = _convert_point_to_coordinates(text)

    # Normalize parameter names
    text = text.replace("start_point=", "start_box=")
    text = text.replace("end_point=", "end_box=")
    text = text.replace("point=", "start_box=")

    # Extract thought/reasoning
    thought = None
    thought_patterns = [
        r"Thought: (.+?)(?=\s*Action: |$)",
        r"Reflection: (.+?)Action_Summary: (.+?)(?=\s*Action: |$)",
        r"Action_Summary: (.+?)(?=\s*Action: |$)",
    ]

    for pattern in thought_patterns:
        thought_match = re.search(pattern, text, re.DOTALL)
        if thought_match:
            if len(thought_match.groups()) == 1:
                thought = thought_match.group(1).strip()
            elif len(thought_match.groups()) == 2:
                thought = thought_match.group(2).strip()  # Use Action_Summary
            break

    if "Action:" not in text:
        raise ValueError("No Action section found in UI-TARS response")

    # Extract action string
    action_str = text.split("Action: ")[-1]

    # Split multiple actions
    action_parts = action_str.split(")\n\n")
    all_actions = []

    for action_part in action_parts:
        action_part = action_part.strip()
        if not action_part:
            continue

        # Handle type action with content specially
        if "type(content" in action_part:
            if not action_part.endswith(")"):
                action_part += ")"
            # Extract content from type action
            pattern = r"type\(content='(.*?)'\)"
            match = re.search(pattern, action_part)
            if match:
                content = match.group(1)
                # Escape single quotes in content
                content = content.replace("'", "\\'")
                action_part = f"type(content='{content}')"

        if not action_part.endswith(")"):
            action_part += ")"

        all_actions.append(action_part)

    # Parse each action
    parsed_actions = []
    for action_str in all_actions:
        try:
            parsed_action = _parse_single_action(action_str)
            if parsed_action:
                parsed_action["thought"] = thought
                parsed_action["browser_window_dimension"] = browser_window_dimension
                parsed_actions.append(parsed_action)
        except Exception:
            LOG.warning(
                "Failed to parse individual UI-TARS action",
                action_str=action_str,
                exc_info=True,
            )
            continue

    return parsed_actions


def _parse_single_action(action_str: str) -> dict[str, Any] | None:
    """Parse a single action string into structured data."""

    try:
        # Clean up the action string
        action_str = action_str.replace("\n", "\\n").strip()

        # Parse as Python expression
        node = ast.parse(action_str, mode="eval")
        if not isinstance(node, ast.Expression) or not isinstance(node.body, ast.Call):
            return None

        call = node.body

        # Get function name
        if isinstance(call.func, ast.Name):
            func_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            func_name = call.func.attr
        else:
            return None

        # Get arguments
        action_inputs = {}
        for kw in call.keywords:
            if kw.arg and isinstance(kw.value, (ast.Constant, ast.Str)):
                if isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                else:  # ast.Str for older Python versions
                    value = kw.value.s
                action_inputs[kw.arg] = value

        return {
            "action_type": func_name,
            "action_inputs": action_inputs,
        }

    except Exception:
        LOG.debug(f"Failed to parse action string: {action_str}", exc_info=True)
        return None


def _convert_point_to_coordinates(text: str) -> str:
    """Convert <point>x y</point> format to (x,y) format."""
    pattern = r"<point>(\d+)\s+(\d+)</point>"

    def replace_match(match: Match[str]) -> str:
        x, y = map(int, match.groups())
        return f"({x},{y})"

    return re.sub(pattern, replace_match, text)


def _create_ui_tars_action(
    parsed_action: dict[str, Any],
    task: Task,
    step: Step,
    browser_window_dimension: Resolution,
    action_order: int,
) -> Action | None:
    """Create a Skyvern action from parsed UI-TARS data."""
    action_type = parsed_action.get("action_type", "")
    action_inputs = parsed_action.get("action_inputs", {})
    thought = parsed_action.get("thought", "")

    base_params = {
        "reasoning": thought,
        "intention": thought,
        "organization_id": task.organization_id,
        "workflow_run_id": task.workflow_run_id,
        "task_id": task.task_id,
        "step_id": step.step_id,
        "step_order": step.order,
        "action_order": action_order,
    }

    if action_type == "click":
        x, y = _extract_ui_tars_coordinates(action_inputs.get("start_box", ""), browser_window_dimension)
        if x is None or y is None:
            return None
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            response=f"Click at ({x}, {y})",
            **base_params,
        )

    elif action_type == "left_double":
        x, y = _extract_ui_tars_coordinates(action_inputs.get("start_box", ""), browser_window_dimension)
        if x is None or y is None:
            return None
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            button="left",
            repeat=2,
            response=f"Double click at ({x}, {y})",
            **base_params,
        )

    elif action_type == "right_single":
        x, y = _extract_ui_tars_coordinates(action_inputs.get("start_box", ""), browser_window_dimension)
        if x is None or y is None:
            return None
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            button="right",
            response=f"Right click at ({x}, {y})",
            **base_params,
        )

    elif action_type == "type":
        content = action_inputs.get("content", "")
        if not content:
            return None
        return InputTextAction(
            element_id="",
            text=content,
            response=f"Type: {content[:50]}{'...' if len(content) > 50 else ''}",
            **base_params,
        )

    elif action_type in ["drag", "select"]:
        start_x, start_y = _extract_ui_tars_coordinates(action_inputs.get("start_box", ""), browser_window_dimension)
        end_x, end_y = _extract_ui_tars_coordinates(action_inputs.get("end_box", ""), browser_window_dimension)
        if None in (start_x, start_y, end_x, end_y):
            return None
        return DragAction(
            start_x=start_x,
            start_y=start_y,
            path=[(end_x, end_y)],
            response=f"Drag from ({start_x}, {start_y}) to ({end_x}, {end_y})",
            **base_params,
        )

    elif action_type == "hotkey":
        key_combo = action_inputs.get("key", action_inputs.get("hotkey", ""))
        if not key_combo:
            return None
        keys = key_combo.split()
        return KeypressAction(
            keys=keys,
            response=f"Hotkey: {key_combo}",
            **base_params,
        )

    elif action_type == "scroll":
        direction = action_inputs.get("direction", "down").lower()
        x, y = _extract_ui_tars_coordinates(action_inputs.get("start_box", ""), browser_window_dimension)
        if x is None or y is None:
            # Use center of screen as fallback
            x = browser_window_dimension["width"] // 2
            y = browser_window_dimension["height"] // 2

        scroll_amount = 300
        if direction == "down":
            scroll_x, scroll_y = 0, scroll_amount
        elif direction == "up":
            scroll_x, scroll_y = 0, -scroll_amount
        elif direction == "right":
            scroll_x, scroll_y = scroll_amount, 0
        elif direction == "left":
            scroll_x, scroll_y = -scroll_amount, 0
        else:
            scroll_x, scroll_y = 0, scroll_amount

        return ScrollAction(
            element_id="",
            x=x,
            y=y,
            scroll_x=scroll_x,
            scroll_y=scroll_y,
            response=f"Scroll {direction} at ({x}, {y})",
            **base_params,
        )

    elif action_type == "wait":
        return WaitAction(
            seconds=5,
            **base_params,
        )

    elif action_type == "finished":
        return CompleteAction(
            data_extraction_goal=task.data_extraction_goal,
            verified=True,  # UI-TARS has already determined completion, skip Skyvern validation
            **base_params,
        )

    else:
        LOG.warning(f"Unsupported UI-TARS action type: {action_type}")
        return None


def _extract_ui_tars_coordinates(box_str: str, browser_window_dimension: Resolution) -> tuple[int | None, int | None]:
    """Extract coordinates from UI-TARS box format with proper coordinate conversion.

    UI-TARS coordinates need to be divided by 1000 to convert from the model's output
    format to relative coordinates (0-1 range), then multiplied by screen dimensions
    to get absolute pixel coordinates.
    """
    if not box_str:
        return None, None

    try:
        # Parse coordinates from string format like "(450,320)" or "[0.5, 0.3, 0.5, 0.3]"
        coords = ast.literal_eval(box_str)

        if not isinstance(coords, (list, tuple)):
            return None, None

        if len(coords) == 2:
            # Direct coordinates like (450, 320) or (0.5, 0.3)
            x, y = coords

            # UI-TARS specific coordinate conversion
            # UI-TARS outputs coordinates that need to be divided by 1000 first
            if x > 1 or y > 1:  # Likely UI-TARS format needing factor conversion
                original_x, original_y = x, y
                x = x / 1000.0
                y = y / 1000.0
                LOG.debug(f"Applied UI-TARS factor conversion: ({original_x}, {original_y}) -> ({x}, {y})")

            # Convert relative coordinates (0-1) to absolute screen coordinates
            if 0 <= x <= 1 and 0 <= y <= 1:
                abs_x = int(x * browser_window_dimension["width"])
                abs_y = int(y * browser_window_dimension["height"])
                LOG.debug(
                    f"Converted to absolute coordinates: ({abs_x}, {abs_y}) for screen {browser_window_dimension['width']}x{browser_window_dimension['height']}"
                )
                return abs_x, abs_y

            return int(x), int(y)

        elif len(coords) == 4:
            # Bounding box format [x1, y1, x2, y2] - take center point
            x1, y1, x2, y2 = coords
            x = (x1 + x2) / 2
            y = (y1 + y2) / 2

            # UI-TARS specific coordinate conversion for bounding boxes
            if x > 1 or y > 1:  # Likely UI-TARS format needing factor conversion
                original_x, original_y = x, y
                x = x / 1000.0
                y = y / 1000.0
                LOG.debug(
                    f"Applied UI-TARS factor conversion to bbox center: ({original_x}, {original_y}) -> ({x}, {y})"
                )

            # Convert relative coordinates (0-1) to absolute screen coordinates
            if 0 <= x <= 1 and 0 <= y <= 1:
                abs_x = int(x * browser_window_dimension["width"])
                abs_y = int(y * browser_window_dimension["height"])
                LOG.debug(
                    f"Converted bbox center to absolute coordinates: ({abs_x}, {abs_y}) for screen {browser_window_dimension['width']}x{browser_window_dimension['height']}"
                )
                return abs_x, abs_y

            return int(x), int(y)

        else:
            return None, None

    except Exception:
        LOG.debug(f"Failed to parse UI-TARS coordinates: {box_str}", exc_info=True)
        return None, None
