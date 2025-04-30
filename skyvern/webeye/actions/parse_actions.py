from typing import Any, Dict

import structlog
from openai.types.responses.response import Response as OpenAIResponse
from pydantic import ValidationError

from skyvern.exceptions import NoTOTPVerificationCodeFound, UnsupportedActionType
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.utils.image_resizer import Resolution, scale_coordinates
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CheckboxAction,
    ClickAction,
    CompleteAction,
    DownloadFileAction,
    DragAction,
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
from skyvern.webeye.actions.handler import poll_verification_code
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
            elif action == "left_click":
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
                response = f"Click at: ({x}, {y})"
                reasoning = reasoning or response
                actions.append(
                    ClickAction(
                        element_id="",
                        x=x,
                        y=y,
                        button="left",
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
                text = tool_call_input.get("text")
                if not text:
                    LOG.warning(
                        "Anthropic CUA error: key action has no text",
                        tool_call=tool_call,
                    )
                    idx += 1
                    continue
                response = f"Press keys: {text}"
                hold = action == "hold_key"
                duration = tool_call_input.get("duration", 0)
                if hold:
                    response = f"Hold keys for {duration} seconds: {text}"
                reasoning = reasoning or response
                actions.append(
                    KeypressAction(
                        element_id="",
                        keys=[text],
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
                    scroll_y = -scroll_amount
                elif scroll_direction == "down":
                    scroll_x = 0
                    scroll_y = scroll_amount
                elif scroll_direction == "left":
                    scroll_x = -scroll_amount
                    scroll_y = 0
                elif scroll_direction == "right":
                    scroll_x = scroll_amount
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
    )
    LOG.info("Fallback action response", action_response=action_response)
    skyvern_action_type = action_response.get("action")
    useful_information = action_response.get("useful_information")
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
                verification_code = await poll_verification_code(
                    task.task_id,
                    task.organization_id,
                    workflow_run_id=task.workflow_run_id,
                    totp_verification_url=task.totp_verification_url,
                    totp_identifier=task.totp_identifier,
                )
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
