"""
UI-TARS Client for Seed1.5-VL model via ByteDance Doubao API.
Implements history-5 conversation approach and action parsing.

NOTE FROM BYTEDANCE TEAM:
"Our agent is a native model-based agent instead of being implemented with a framework. Our context management is just "reserve all texts and most recent 5 images", which you can find in the cookbook. Good practice for implementing our model in any environment is listed below:
Step 1: Getting prediction from our model using the same setting as cookbook. (system prompt, inference params)
Step 2: Using the parse_action_to_structure_output function in cookbook to get the parsed action name and action input parameters
Step 3: Implement your own adapter function for mapping our model action space to your environment's action space (like pyautogui in OSWorld, or playwright action space in WebVoyager), remember that our model does not need a set-of-mark screenshot or operate on an element id in html ally tree. We directly operate and work best based solely on visual coordinates."
"""

import ast
import base64
import json
import math
import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import structlog
from openai import OpenAI
from PIL import Image

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    ClickAction,
    CompleteAction,
    DragAction,
    InputTextAction,
    KeypressAction,
    ScrollAction,
    WaitAction,
)
from skyvern.webeye.scraper.scraper import ScrapedPage

LOG = structlog.get_logger()

# Constants for action parser (from UI-TARS official repo)
IMAGE_FACTOR = 28
MIN_PIXELS = 100 * 28 * 28
MAX_PIXELS = 16384 * 28 * 28
MAX_RATIO = 200


class UITarsClient:
    """Client for UI-TARS (Seed1.5-VL) via Doubao API with history-5 management."""
    
    def __init__(
        self,
        api_key: str,
        task_id: str,
        api_base: str = "https://ark.cn-beijing.volces.com/api/v3",
        model: str = "doubao-1-5-thinking-vision-pro-250428",
        max_history: int = 5,
    ):
        self.api_key = api_key
        self.task_id = task_id
        self.api_base = api_base
        self.model = model
        self.max_history = max_history
        
        # Initialize OpenAI client for Doubao API
        self.client = OpenAI(
            base_url=api_base,
            api_key=api_key,
        )
        
        # History-5 conversation management: system prompt + last 4 screenshots + current
        self.conversation_history: List[Dict[str, Any]] = []
        self.screenshot_count = 0

    def initialize_conversation(self, task: Task) -> None:
        """Initialize conversation with system prompt."""
        system_prompt = self._build_system_prompt(task.navigation_goal)
        self.conversation_history = [
            {
                "role": "user",
                "content": system_prompt
            }
        ]
        self.screenshot_count = 0

    def add_screenshot_to_history(self, screenshot_b64: str, image_format: str = "png") -> None:
        """Add screenshot to conversation history with history-5 management."""
        screenshot_message = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/{image_format};base64,{screenshot_b64}"
                    }
                }
            ]
        }
        
        self.conversation_history.append(screenshot_message)
        self.screenshot_count += 1
        self._maintain_history_limit()

    def add_assistant_response(self, response: str) -> None:
        """Add assistant response to conversation history."""
        self.conversation_history.append({
            "role": "assistant", 
            "content": response
        })

    def _maintain_history_limit(self) -> None:
        """Maintain history-5 limit: keep system prompt + all assistant responses + last 5 screenshots.
        
        Following the pattern from gui.ipynb:
        - System prompt (user message)
        - All assistant responses (never removed)
        - Only the 5 most recent screenshots (user messages with images)
        """
        if self.screenshot_count <= self.max_history:
            return
        
        # Remove oldest screenshot only (keep all assistant responses)
        # Find first user message with image after system prompt
        for i in range(1, len(self.conversation_history)):
            message = self.conversation_history[i]
            if (message["role"] == "user" and 
                isinstance(message["content"], list) and 
                len(message["content"]) > 0 and 
                message["content"][0].get("type") == "image_url"):
                
                # Remove only the screenshot message, keep all assistant responses
                self.conversation_history.pop(i)
                self.screenshot_count -= 1
                break

    async def generate_actions(self, task: Task, step: Step, scraped_page: ScrapedPage) -> List[Action]:
        """Generate actions for the given task and screenshot using UI-TARS."""
        try:
            # Initialize conversation if empty
            if not self.conversation_history:
                self.initialize_conversation(task)
            
            # Get screenshot and add to history
            if not scraped_page.screenshots:
                raise ValueError("No screenshots available for UI-TARS")
            
            screenshot_b64 = base64.b64encode(scraped_page.screenshots[0]).decode()
            self.add_screenshot_to_history(screenshot_b64)
            
            # Generate response using OpenAI API
            response_content = await self._call_api()
            print(f"Response content: {response_content}")
            
            # Add response to history
            self.add_assistant_response(response_content)
            
            # Parse response using UI-TARS official parser
            
            image = Image.open(BytesIO(scraped_page.screenshots[0]))
            original_image_width, original_image_height = image.size
            LOG.info(f"Original image size: {original_image_width}x{original_image_height}")
            model_type = "doubao"  # Use doubao model type for UI-TARS
            
            # Use the official UI-TARS parser to get structured actions
            try:
                parsed_actions = parse_action_to_structure_output(
                    response_content, 
                    factor=1000,  # Scale factor for coordinates  
                    origin_resized_height=original_image_height,
                    origin_resized_width=original_image_width,
                    model_type=model_type
                )
                
                LOG.info(f"UI-TARS parsed actions: {parsed_actions}")
                
                # Convert parsed actions to Skyvern action objects
                actions = self._convert_parsed_actions_to_skyvern_actions(parsed_actions, task, step, original_image_width, original_image_height)
                
            except Exception as parse_error:
                LOG.error(f"UI-TARS parser failed, falling back to basic parsing", error=str(parse_error), response=response_content)
                # Fallback to basic response parsing if UI-TARS parser fails
                actions = self._parse_response_to_actions(response_content, task, step)
            
            LOG.info(
                "UI-TARS action generation completed",
                task_id=self.task_id,
                step_id=step.step_id,
                actions_count=len(actions),
                response_content=response_content[:200],  # First 200 chars for logging
            )
            
            return actions
            
        except Exception as e:
            LOG.error(
                "UI-TARS action generation failed",
                task_id=self.task_id,
                step_id=step.step_id,
                error=str(e),
                exc_info=True,
            )
            # Return fallback wait action
            return [self._create_fallback_action(task, step, str(e))]

    async def _call_api(self) -> str:
        """Call the UI-TARS API and return response content."""
        try:
            # Use synchronous call for now (Doubao's OpenAI client might not support async properly)
            chat_completion = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                top_p=None,
                temperature=0.0,
                max_tokens=400,
                stream=True,
                seed=None,
                stop=None,
                frequency_penalty=None,
                presence_penalty=None
                )
            
            response_content = ""
            for chunk in chat_completion:
                if chunk.choices[0].delta.content:
                    response_content += chunk.choices[0].delta.content
            
            return response_content.strip()
            
        except Exception as e:
            LOG.error("UI-TARS API call failed", task_id=self.task_id, error=str(e), exc_info=True)
            raise LLMProviderError(f"UI-TARS API call failed: {str(e)}")

    def _build_system_prompt(self, instruction: str, language: str = "English") -> str:
        """Build system prompt for UI-TARS following the official COMPUTER_USE_DOUBAO template.
        Sources:
        - https://github.com/ByteDance-Seed/Seed1.5-VL/blob/main/GUI/gui.ipynb
        - https://github.com/bytedance/UI-TARS/blob/main/codes/ui_tars/prompt.py"""
        
        return f"""You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space

click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='ctrl c') # Split keys with a space and use lowercase. Also, do not use more than 3 keys in one hotkey action.
type(content='xxx') # Use escape characters \\', \\\", and \\n in content part to ensure we can parse the content in normal python string format. If you want to submit your input, use \\n at the end of content. 
scroll(point='<point>x1 y1</point>', direction='down or up or right or left') # Show more information on the `direction` side.
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished(content='xxx') # Use escape characters \\', \\", and \\n in content part to ensure we can parse the content in normal python string format.


## Note
- Use {language} in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
{instruction}
"""

    def _convert_parsed_actions_to_skyvern_actions(
        self, 
        parsed_actions: List[Dict[str, Any]], 
        task: Task, 
        step: Step, 
        image_width: int, 
        image_height: int
    ) -> List[Action]:
        """Convert UI-TARS parsed actions to Skyvern action objects."""
        actions: List[Action] = []
        
        for parsed_action in parsed_actions:
            try:
                action_type = parsed_action.get("action_type", "")
                action_inputs = parsed_action.get("action_inputs", {})
                thought = parsed_action.get("thought", "")
                
                if action_type == "click":
                    x, y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    action = ClickAction(
                        element_id="",
                        x=x,
                        y=y,
                        reasoning=thought,
                        intention=thought,
                        response=f"Click at ({x}, {y})",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "left_double":
                    x, y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    action = ClickAction(
                        element_id="",
                        x=x,
                        y=y,
                        button="left",
                        repeat=2,
                        reasoning=thought,
                        intention=thought,
                        response=f"Double click at ({x}, {y})",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "right_single":
                    x, y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    action = ClickAction(
                        element_id="",
                        x=x,
                        y=y,
                        button="right",
                        reasoning=thought,
                        intention=thought,
                        response=f"Right click at ({x}, {y})",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "type":
                    content = action_inputs.get("content", "")
                    action = InputTextAction(
                        element_id="",
                        text=content,
                        reasoning=thought,
                        intention=thought,
                        response=f"Type: {content}",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "drag" or action_type == "select":
                    start_x, start_y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    end_x, end_y = self._extract_coordinates_from_box(action_inputs.get("end_box", ""), image_width, image_height)
                    action = DragAction(
                        start_x=start_x,
                        start_y=start_y,
                        path=[(end_x, end_y)],
                        reasoning=thought,
                        intention=thought,
                        response=f"Drag from ({start_x}, {start_y}) to ({end_x}, {end_y})",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "hotkey":
                    key = action_inputs.get("key", action_inputs.get("hotkey", ""))
                    # Parse space-separated hotkey string into individual keys
                    # UI-TARS format: "ctrl shift y" -> ["ctrl", "shift", "y"]
                    keys = key.split() if key else []
                    action = KeypressAction(
                        keys=keys,
                        reasoning=thought,
                        intention=thought,
                        response=f"Hotkey: {key}",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "scroll":
                    direction = action_inputs.get("direction", "down")
                    x, y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    action = ScrollAction(
                        x=x,
                        y=y,
                        direction=direction,
                        clicks=3,  # Default scroll amount
                        reasoning=thought,
                        intention=thought,
                        response=f"Scroll {direction} at ({x}, {y})",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "wait":
                    action = WaitAction(
                        reasoning=thought,
                        seconds=5,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "finished":
                    action = CompleteAction(
                        reasoning=thought,
                        data_extraction_goal=task.data_extraction_goal,
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                else:
                    # Create fallback action for unrecognized types
                    action = self._create_fallback_action(task, step, f"Unrecognized action type: {action_type}")
                
                actions.append(action)
                
            except Exception as e:
                LOG.error(f"Failed to convert action: {parsed_action}", error=str(e), exc_info=True)
                actions.append(self._create_fallback_action(task, step, f"Failed to parse action: {str(e)}"))
        
        return actions if actions else [self._create_fallback_action(task, step, "No valid actions found")]

    def _extract_coordinates_from_box(self, box_str: str, image_width: int, image_height: int) -> Tuple[int, int]:
        """Extract coordinates from UI-TARS box format."""
        try:
            if not box_str:
                return 0, 0
            
            # Parse the box coordinates from the string format like "[0.5, 0.3, 0.5, 0.3]"
            # The UI-TARS parser should return string representation of list of floats
            coords = ast.literal_eval(box_str)  # This should be a list of float values (relative coordinates)
            
            if len(coords) == 4:
                x1, y1, x2, y2 = coords
                # Take center point and convert to absolute coordinates
                x = int((x1 + x2) / 2 * image_width)
                y = int((y1 + y2) / 2 * image_height)
            elif len(coords) == 2:
                x1, y1 = coords
                x = int(x1 * image_width)
                y = int(y1 * image_height)
            else:
                raise ValueError(f"Invalid coordinate format: {box_str}")
            
            # Ensure coordinates are within image bounds
            x = max(0, min(x, image_width - 1))
            y = max(0, min(y, image_height - 1))
            
            return x, y
            
        except Exception as e:
            LOG.error(f"Failed to extract coordinates from box: {box_str}", error=str(e))
            # Return center of image as fallback
            return image_width // 2, image_height // 2

    def _parse_response_to_actions(self, response_content: str, task: Task, step: Step) -> List[Action]:
        """Parse UI-TARS text response into Skyvern actions."""
        actions: List[Action] = []
        
        try:
            # Extract thought and action from response
            thought_match = re.search(r"Thought:\s*(.*?)(?=Action:|$)", response_content, re.DOTALL)
            action_match = re.search(r"Action:\s*(.*?)(?=\n|$)", response_content, re.DOTALL)
            
            thought = thought_match.group(1).strip() if thought_match else "Processing action"
            action_text = action_match.group(1).strip() if action_match else ""
            
            if not action_text:
                LOG.warning("No action found in UI-TARS response", response=response_content)
                return [self._create_fallback_action(task, step, "No clear action identified")]
            
            # Parse different action types from UI-TARS
            action = self._parse_single_action(action_text, thought, task, step)
            actions.append(action)
            
        except Exception as e:
            LOG.error("Failed to parse UI-TARS response", error=str(e), response=response_content, exc_info=True)
            actions.append(self._create_fallback_action(task, step, f"Failed to parse action: {str(e)}"))
        
        return actions if actions else [self._create_fallback_action(task, step, "No valid actions found")]

    def _parse_single_action(self, action_text: str, thought: str, task: Task, step: Step) -> Action:
        """Parse a single action from action text."""
        action_text = action_text.strip()
        
        if action_text.startswith("click("):
            return self._parse_click_action(action_text, thought, task, step)
        elif action_text.startswith("left_double("):
            return self._parse_double_click_action(action_text, thought, task, step)
        elif action_text.startswith("right_single("):
            return self._parse_right_click_action(action_text, thought, task, step)
        elif action_text.startswith("drag("):
            return self._parse_drag_action(action_text, thought, task, step)
        elif action_text.startswith("hotkey("):
            return self._parse_hotkey_action(action_text, thought, task, step)
        elif action_text.startswith("type("):
            return self._parse_type_action(action_text, thought, task, step)
        elif action_text.startswith("scroll("):
            return self._parse_scroll_action(action_text, thought, task, step)
        elif action_text.startswith("wait("):
            return WaitAction(
                reasoning=thought,
                seconds=5,  # UI-TARS wait() sleeps for 5s
                organization_id=task.organization_id,
                workflow_run_id=task.workflow_run_id,
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                action_order=0,
            )
        elif action_text.startswith("finished("):
            return CompleteAction(
                reasoning=thought,
                data_extraction_goal=task.data_extraction_goal,
                organization_id=task.organization_id,
                workflow_run_id=task.workflow_run_id,
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                action_order=0,
            )
        else:
            # Default to wait action if action type not recognized
            return self._create_fallback_action(task, step, f"{thought}. Unrecognized action: {action_text}")

    def _parse_click_action(self, action_text: str, thought: str, task: Task, step: Step) -> ClickAction:
        """Parse click action from UI-TARS response."""
        x, y = self._extract_coordinates(action_text)
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            reasoning=thought,
            intention=thought,
            response=f"Click at ({x}, {y})",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_double_click_action(self, action_text: str, thought: str, task: Task, step: Step) -> ClickAction:
        """Parse left_double action from UI-TARS response."""
        x, y = self._extract_coordinates(action_text)
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            button="left",
            repeat=2,  # Double click
            reasoning=thought,
            intention=thought,
            response=f"Double click at ({x}, {y})",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_right_click_action(self, action_text: str, thought: str, task: Task, step: Step) -> ClickAction:
        """Parse right_single action from UI-TARS response."""
        x, y = self._extract_coordinates(action_text)
        return ClickAction(
            element_id="",
            x=x,
            y=y,
            button="right",
            reasoning=thought,
            intention=thought,
            response=f"Right click at ({x}, {y})",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_type_action(self, action_text: str, thought: str, task: Task, step: Step) -> InputTextAction:
        """Parse type action from UI-TARS response."""
        # Extract text from content='text' format
        content_match = re.search(r"content=['\"]([^'\"]*)['\"]", action_text)
        if content_match:
            text = content_match.group(1)
        else:
            raise ValueError(f"Could not parse text content from: {action_text}")
        
        return InputTextAction(
            element_id="",
            text=text,
            reasoning=thought,
            intention=thought,
            response=f"Type: {text}",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_drag_action(self, action_text: str, thought: str, task: Task, step: Step) -> DragAction:
        """Parse drag action from UI-TARS response."""
        # Extract start and end coordinates
        start_match = re.search(r"start_point=['\"]<point>(\d+)\s+(\d+)</point>['\"]", action_text)
        end_match = re.search(r"end_point=['\"]<point>(\d+)\s+(\d+)</point>['\"]", action_text)
        
        if start_match and end_match:
            start_x, start_y = int(start_match.group(1)), int(start_match.group(2))
            end_x, end_y = int(end_match.group(1)), int(end_match.group(2))
        else:
            raise ValueError(f"Could not parse drag coordinates from: {action_text}")
        
        return DragAction(
            start_x=start_x,
            start_y=start_y,
            path=[(end_x, end_y)],
            reasoning=thought,
            intention=thought,
            response=f"Drag from ({start_x}, {start_y}) to ({end_x}, {end_y})",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_hotkey_action(self, action_text: str, thought: str, task: Task, step: Step) -> KeypressAction:
        """Parse hotkey action from UI-TARS response."""
        # Extract key combination from key='key1 key2' format
        key_match = re.search(r"key=['\"]([^'\"]*)['\"]", action_text)
        if key_match:
            keys = key_match.group(1).split()
        else:
            raise ValueError(f"Could not parse key combination from: {action_text}")
        
        return KeypressAction(
            keys=keys,
            reasoning=thought,
            intention=thought,
            response=f"Hotkey: {' + '.join(keys)}",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _parse_scroll_action(self, action_text: str, thought: str, task: Task, step: Step) -> ScrollAction:
        """Parse scroll action from UI-TARS response."""
        x, y = self._extract_coordinates(action_text)
        
        # Extract direction
        direction_match = re.search(r"direction=['\"]([^'\"]*)['\"]", action_text)
        if direction_match:
            direction = direction_match.group(1).lower()
        else:
            raise ValueError(f"Could not parse scroll direction from: {action_text}")
        
        # Convert direction to scroll coordinates  
        scroll_amount = 100  # Default scroll amount
        if direction == "down":
            scroll_x, scroll_y = 0, scroll_amount
        elif direction == "up":
            scroll_x, scroll_y = 0, -scroll_amount
        elif direction == "right":
            scroll_x, scroll_y = scroll_amount, 0
        elif direction == "left":
            scroll_x, scroll_y = -scroll_amount, 0
        else:
            scroll_x, scroll_y = 0, scroll_amount  # Default to down
        
        return ScrollAction(
            x=x,
            y=y,
            scroll_x=scroll_x,
            scroll_y=scroll_y,
            reasoning=thought,
            intention=thought,
            response=f"Scroll {direction} at ({x}, {y})",
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def _extract_coordinates(self, action_text: str) -> Tuple[int, int]:
        """Extract x, y coordinates from action text."""
        # Extract coordinates from point='<point>x y</point>' format
        point_match = re.search(r"point=['\"]<point>(\d+)\s+(\d+)</point>['\"]", action_text)
        if point_match:
            return int(point_match.group(1)), int(point_match.group(2))
        
        # Try alternative coordinate formats
        coord_match = re.search(r"(\d+),?\s*(\d+)", action_text)
        if coord_match:
            return int(coord_match.group(1)), int(coord_match.group(2))
        
        raise ValueError(f"Could not parse coordinates from: {action_text}")

    def _create_fallback_action(self, task: Task, step: Step, reason: str) -> WaitAction:
        """Create a fallback wait action when parsing fails."""
        return WaitAction(
            reasoning=reason,
            seconds=2,
            organization_id=task.organization_id,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
        )

    def reset_conversation(self) -> None:
        """Reset the conversation history."""
        self.conversation_history = []
        self.screenshot_count = 0


class UITarsManager:
    """Manager for UI-TARS clients with per-task isolation."""
    
    def __init__(self):
        self.clients: Dict[str, UITarsClient] = {}

    def get_client(
        self,
        task_id: str,
        api_key: str,
        api_base: str = "https://ark.cn-beijing.volces.com/api/v3",
        model: str = "doubao-1-5-thinking-vision-pro-250428",
    ) -> UITarsClient:
        """Get or create a UI-TARS client for the given task."""
        if task_id not in self.clients:
            self.clients[task_id] = UITarsClient(
                api_key=api_key,
                task_id=task_id,
                api_base=api_base,
                model=model,
            )
        return self.clients[task_id]

    def remove_client(self, task_id: str) -> None:
        """Remove a client for the given task."""
        if task_id in self.clients:
            del self.clients[task_id]

    def clear_all(self) -> None:
        """Clear all clients."""
        self.clients.clear()


# Global manager instance
ui_tars_manager = UITarsManager() 


















import ast
import math

# Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
# SPDX-License-Identifier: Apache-2.0
import re

IMAGE_FACTOR = 28
MIN_PIXELS = 100 * 28 * 28
MAX_PIXELS = 16384 * 28 * 28
MAX_RATIO = 200


def convert_point_to_coordinates(text, is_answer=False):
    # 匹配 <bbox> 后面的四个数字
    pattern = r"<point>(\d+)\s+(\d+)</point>"

    def replace_match(match):
        x1, y1 = map(int, match.groups())
        x = (x1 + x1) // 2  # 使用截断取整
        y = (y1 + y1) // 2  # 使用截断取整
        if is_answer:
            return f"({x},{y})"  # 只返回 (x, y) 格式
        return f"({x},{y})"  # 返回带标签的格式

    # 去掉 [EOS] 并替换 <bbox> 坐标
    text = re.sub(r"\[EOS\]", "", text)
    return re.sub(pattern, replace_match, text).strip()


# 定义一个函数来解析每个 action
def parse_action(action_str):
    try:
        # 解析字符串为 AST 节点
        node = ast.parse(action_str, mode='eval')

        # 确保节点是一个表达式
        if not isinstance(node, ast.Expression):
            raise ValueError("Not an expression")

        # 获取表达式的主体
        call = node.body

        # 确保主体是一个函数调用
        if not isinstance(call, ast.Call):
            raise ValueError("Not a function call")

        # 获取函数名
        if isinstance(call.func, ast.Name):
            func_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            func_name = call.func.attr
        else:
            func_name = None

        # 获取关键字参数
        kwargs = {}
        for kw in call.keywords:
            key = kw.arg
            # 处理不同类型的值，这里假设都是常量
            if isinstance(kw.value, ast.Constant):
                value = kw.value.value
            elif isinstance(kw.value, ast.Str):  # 兼容旧版本 Python
                value = kw.value.s
            else:
                value = None
            kwargs[key] = value

        return {'function': func_name, 'args': kwargs}

    except Exception as e:
        print(f"Failed to parse action '{action_str}': {e}")
        return None


def escape_single_quotes(text):
    # 匹配未转义的单引号（不匹配 \\'）
    pattern = r"(?<!\\)'"
    return re.sub(pattern, r"\\'", text)


def round_by_factor(number: int, factor: int) -> int:
    """Returns the closest integer to 'number' that is divisible by 'factor'."""
    return round(number / factor) * factor


def ceil_by_factor(number: int, factor: int) -> int:
    """Returns the smallest integer greater than or equal to 'number' that is divisible by 'factor'."""
    return math.ceil(number / factor) * factor


def floor_by_factor(number: int, factor: int) -> int:
    """Returns the largest integer less than or equal to 'number' that is divisible by 'factor'."""
    return math.floor(number / factor) * factor


def linear_resize(height: int,
                  width: int,
                  factor: int = IMAGE_FACTOR,
                  min_pixels: int = MIN_PIXELS,
                  max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    if width * height > max_pixels:
        """
        如果图片超过/低于像素限制，则计算一个缩放因子resize_factor，使图片的像素数缩小到等于或小于max_pixels。这个缩放因子是通过开平方根计算的，确保纵横比保持不变,这样原始的相对坐标可以不经转换直接复用
        """
        resize_factor = math.sqrt(max_pixels / (width * height))
        width, height = int(width * resize_factor), int(height * resize_factor)
    if width * height < min_pixels:
        resize_factor = math.sqrt(min_pixels / (width * height))
        width, height = math.ceil(width * resize_factor), math.ceil(
            height * resize_factor)

    return height, width


def smart_resize(height: int,
                 width: int,
                 factor: int = IMAGE_FACTOR,
                 min_pixels: int = MIN_PIXELS,
                 max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    """
    Rescales the image so that the following conditions are met:

    1. Both dimensions (height and width) are divisible by 'factor'.

    2. The total number of pixels is within the range ['min_pixels', 'max_pixels'].

    3. The aspect ratio of the image is maintained as closely as possible.
    """
    if max(height, width) / min(height, width) > MAX_RATIO:
        raise ValueError(
            f"absolute aspect ratio must be smaller than {MAX_RATIO}, got {max(height, width) / min(height, width)}"
        )
    h_bar = max(factor, round_by_factor(height, factor))
    w_bar = max(factor, round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)
        w_bar = ceil_by_factor(width * beta, factor)
    return h_bar, w_bar


def parse_action_to_structure_output(text,
                                     factor,
                                     origin_resized_height,
                                     origin_resized_width,
                                     model_type="qwen25vl",
                                     max_pixels=16384 * 28 * 28,
                                     min_pixels=100 * 28 * 28):
    text = text.strip()

    if "<point>" in text:
        text = convert_point_to_coordinates(text)
    if "start_point=" in text:
        text = text.replace("start_point=", "start_box=")
    if "end_point=" in text:
        text = text.replace("end_point=", "end_box=")
    if "point=" in text:
        text = text.replace("point=", "start_box=")

    if model_type == "qwen25vl":
        smart_resize_height, smart_resize_width = smart_resize(
            origin_resized_height,
            origin_resized_width,
            factor=IMAGE_FACTOR,
            min_pixels=min_pixels,
            max_pixels=max_pixels)

    # 正则表达式匹配 Action 字符串
    if text.startswith("Thought:"):
        thought_pattern = r"Thought: (.+?)(?=\s*Action: |$)"
        thought_hint = "Thought: "
    elif text.startswith("Reflection:"):
        thought_pattern = r"Reflection: (.+?)Action_Summary: (.+?)(?=\s*Action: |$)"
        thought_hint = "Reflection: "
    elif text.startswith("Action_Summary:"):
        thought_pattern = r"Action_Summary: (.+?)(?=\s*Action: |$)"
        thought_hint = "Action_Summary: "
    else:
        thought_pattern = r"Thought: (.+?)(?=\s*Action: |$)"
        thought_hint = "Thought: "
    reflection, thought = None, None
    thought_match = re.search(thought_pattern, text, re.DOTALL)
    if thought_match:
        if len(thought_match.groups()) == 1:
            thought = thought_match.group(1).strip()
        elif len(thought_match.groups()) == 2:
            thought = thought_match.group(2).strip()
            reflection = thought_match.group(1).strip()
    assert "Action:" in text
    action_str = text.split("Action: ")[-1]

    tmp_all_action = action_str.split(")\n\n")
    all_action = []
    for action_str in tmp_all_action:
        if "type(content" in action_str:
            if not action_str.strip().endswith(")"):
                action_str = action_str.strip() + ")"
            # 正则表达式匹配 content 中的字符串并转义单引号
            def escape_quotes(match):
                content = match.group(1)  # 获取 content 的值
                return content

            # 使用正则表达式进行替换
            pattern = r"type\(content='(.*?)'\)"  # 匹配 type(content='...')
            if re.search(pattern, action_str):  # 检查是否有匹配项
                content = re.sub(pattern, escape_quotes, action_str)
            else:
                raise ValueError("Pattern not found in the input string.")

            # 处理字符串
            action_str = escape_single_quotes(content)
            action_str = "type(content='" + action_str + "')"
        if not action_str.strip().endswith(")"):
            action_str = action_str.strip() + ")"
        all_action.append(action_str)

    parsed_actions = [
        parse_action(action.replace("\n", "\\n").lstrip())
        for action in all_action
    ]
    actions = []
    for action_instance, raw_str in zip(parsed_actions, all_action):
        if action_instance == None:
            print(f"Action can't parse: {raw_str}")
            raise ValueError(f"Action can't parse: {raw_str}")
        action_type = action_instance["function"]
        params = action_instance["args"]

        # import pdb; pdb.set_trace()
        action_inputs = {}
        for param_name, param in params.items():
            if param == "": continue
            param = param.lstrip()  # 去掉引号和多余的空格
            # 处理start_box或者end_box参数格式 '<bbox>x1 y1 x2 y2</bbox>'
            action_inputs[param_name.strip()] = param

            if "start_box" in param_name or "end_box" in param_name:
                ori_box = param
                # Remove parentheses and split the string by commas
                numbers = ori_box.replace("(", "").replace(")", "").split(",")

                # Convert to float and scale by 1000
                # Qwen2.5vl output absolute coordinates, qwen2vl output relative coordinates
                if model_type == "qwen25vl":
                    float_numbers = []
                    for num_idx, num in enumerate(numbers):
                        num = float(num)
                        if (num_idx + 1) % 2 == 0:
                            float_numbers.append(
                                float(num / smart_resize_height))
                        else:
                            float_numbers.append(
                                float(num / smart_resize_width))
                else:
                    float_numbers = [float(num) / factor for num in numbers]

                if len(float_numbers) == 2:
                    float_numbers = [
                        float_numbers[0], float_numbers[1], float_numbers[0],
                        float_numbers[1]
                    ]
                action_inputs[param_name.strip()] = str(float_numbers)

        # import pdb; pdb.set_trace()
        actions.append({
            "reflection": reflection,
            "thought": thought,
            "action_type": action_type,
            "action_inputs": action_inputs,
            "text": text
        })
    return actions


def parsing_response_to_pyautogui_code(responses,
                                       image_height: int,
                                       image_width: int,
                                       input_swap: bool = True) -> str:
    '''
    将M模型的输出解析为OSWorld中的action，生成pyautogui代码字符串
    参数:
        response: 包含模型输出的字典，结构类似于：
        {
            "action_type": "hotkey",
            "action_inputs": {
                "hotkey": "v ctrl",
                "start_box": None,
                "end_box": None
            }
        }
    返回:
        生成的pyautogui代码字符串
    '''

    pyautogui_code = f"import pyautogui\nimport time\n"
    if isinstance(responses, dict):
        responses = [responses]
    for response_id, response in enumerate(responses):
        if "observation" in response:
            observation = response["observation"]
        else:
            observation = ""

        if "thought" in response:
            thought = response["thought"]
        else:
            thought = ""

        if response_id == 0:
            pyautogui_code += f"'''\nObservation:\n{observation}\n\nThought:\n{thought}\n'''\n"
        else:
            pyautogui_code += f"\ntime.sleep(1)\n"

        action_dict = response
        action_type = action_dict.get("action_type")
        action_inputs = action_dict.get("action_inputs", {})

        if action_type == "hotkey":
            # Parsing hotkey action
            if "key" in action_inputs:
                hotkey = action_inputs.get("key", "")
            else:
                hotkey = action_inputs.get("hotkey", "")

            if hotkey == "arrowleft":
                hotkey = "left"

            elif hotkey == "arrowright":
                hotkey = "right"

            elif hotkey == "arrowup":
                hotkey = "up"

            elif hotkey == "arrowdown":
                hotkey = "down"

            if hotkey:
                # Handle other hotkeys
                keys = hotkey.split()  # Split the keys by space
                convert_keys = []
                for key in keys:
                    if key == "space":
                        key = ' '
                    convert_keys.append(key)
                pyautogui_code += f"\npyautogui.hotkey({', '.join([repr(k) for k in convert_keys])})"

        elif action_type in ["press", "keydown"]:
            # Parsing press action
            if "key" in action_inputs:
                key_to_press = action_inputs.get("key", "")
            else:
                key_to_press = action_inputs.get("press", "")

            if key_to_press == "arrowleft":
                key_to_press = "left"

            elif key_to_press == "arrowright":
                key_to_press = "right"

            elif key_to_press == "arrowup":
                key_to_press = "up"

            elif key_to_press == "arrowdown":
                key_to_press = "down"

            elif key_to_press == "space":
                key_to_press = " "

            if key_to_press:
                # Simulate pressing a single key
                pyautogui_code += f"\npyautogui.keyDown({repr(key_to_press)})"

        elif action_type in ["release", "keyup"]:
            # Parsing press action
            if "key" in action_inputs:
                key_to_press = action_inputs.get("key", "")
            else:
                key_to_press = action_inputs.get("press", "")

            if key_to_press == "arrowleft":
                key_to_press = "left"

            elif key_to_press == "arrowright":
                key_to_press = "right"

            elif key_to_press == "arrowup":
                key_to_press = "up"

            elif key_to_press == "arrowdown":
                key_to_press = "down"

            elif key_to_press == "space":
                key_to_press = " "

            if key_to_press:
                # Simulate pressing a single key
                pyautogui_code += f"\npyautogui.keyUp({repr(key_to_press)})"

        elif action_type == "type":
            # Parsing typing action using clipboard
            content = action_inputs.get("content", "")
            content = escape_single_quotes(content)
            stripped_content = content
            if content.endswith("\n") or content.endswith("\\n"):
                stripped_content = stripped_content.rstrip("\\n").rstrip("\n")
            if content:
                if input_swap:
                    pyautogui_code += f"\nimport pyperclip"
                    pyautogui_code += f"\npyperclip.copy('{stripped_content}')"
                    pyautogui_code += f"\npyautogui.hotkey('ctrl', 'v')"
                    pyautogui_code += f"\ntime.sleep(0.5)\n"
                    if content.endswith("\n") or content.endswith("\\n"):
                        pyautogui_code += f"\npyautogui.press('enter')"
                else:
                    pyautogui_code += f"\npyautogui.write('{stripped_content}', interval=0.1)"
                    pyautogui_code += f"\ntime.sleep(0.5)\n"
                    if content.endswith("\n") or content.endswith("\\n"):
                        pyautogui_code += f"\npyautogui.press('enter')"

        elif action_type in ["drag", "select"]:
            # Parsing drag or select action based on start and end_boxes
            start_box = action_inputs.get("start_box")
            end_box = action_inputs.get("end_box")
            if start_box and end_box:
                x1, y1, x2, y2 = ast.literal_eval(
                    start_box)  # Assuming box is in [x1, y1, x2, y2]
                sx = round(float((x1 + x2) / 2) * image_width, 3)
                sy = round(float((y1 + y2) / 2) * image_height, 3)
                x1, y1, x2, y2 = ast.literal_eval(
                    end_box)  # Assuming box is in [x1, y1, x2, y2]
                ex = round(float((x1 + x2) / 2) * image_width, 3)
                ey = round(float((y1 + y2) / 2) * image_height, 3)
                pyautogui_code += (
                    f"\npyautogui.moveTo({sx}, {sy})\n"
                    f"\npyautogui.dragTo({ex}, {ey}, duration=1.0)\n")

        elif action_type == "scroll":
            # Parsing scroll action
            start_box = action_inputs.get("start_box")
            if start_box:
                x1, y1, x2, y2 = ast.literal_eval(
                    start_box)  # Assuming box is in [x1, y1, x2, y2]
                x = round(float((x1 + x2) / 2) * image_width, 3)
                y = round(float((y1 + y2) / 2) * image_height, 3)

                # # 先点对应区域，再滚动
                # pyautogui_code += f"\npyautogui.click({x}, {y}, button='left')"
            else:
                x = None
                y = None
            direction = action_inputs.get("direction", "")

            if x == None:
                if "up" in direction.lower():
                    pyautogui_code += f"\npyautogui.scroll(5)"
                elif "down" in direction.lower():
                    pyautogui_code += f"\npyautogui.scroll(-5)"
            else:
                if "up" in direction.lower():
                    pyautogui_code += f"\npyautogui.scroll(5, x={x}, y={y})"
                elif "down" in direction.lower():
                    pyautogui_code += f"\npyautogui.scroll(-5, x={x}, y={y})"

        elif action_type in [
                "click", "left_single", "left_double", "right_single", "hover"
        ]:
            # Parsing mouse click actions
            start_box = action_inputs.get("start_box")
            start_box = str(start_box)
            if start_box:
                start_box = ast.literal_eval(start_box)
                if len(start_box) == 4:
                    x1, y1, x2, y2 = start_box  # Assuming box is in [x1, y1, x2, y2]
                elif len(start_box) == 2:
                    x1, y1 = start_box
                    x2 = x1
                    y2 = y1
                x = round(float((x1 + x2) / 2) * image_width, 3)
                y = round(float((y1 + y2) / 2) * image_height, 3)
                if action_type == "left_single" or action_type == "click":
                    pyautogui_code += f"\npyautogui.click({x}, {y}, button='left')"
                elif action_type == "left_double":
                    pyautogui_code += f"\npyautogui.doubleClick({x}, {y}, button='left')"
                elif action_type == "right_single":
                    pyautogui_code += f"\npyautogui.click({x}, {y}, button='right')"
                elif action_type == "hover":
                    pyautogui_code += f"\npyautogui.moveTo({x}, {y})"

        elif action_type in ["finished"]:
            pyautogui_code = f"DONE"

        else:
            pyautogui_code += f"\n# Unrecognized action type: {action_type}"

    return pyautogui_code


def add_box_token(input_string):
    # Step 1: Split the string into individual actions
    if "Action: " in input_string and "start_box=" in input_string:
        suffix = input_string.split("Action: ")[0] + "Action: "
        actions = input_string.split("Action: ")[1:]
        processed_actions = []
        for action in actions:
            action = action.strip()
            # Step 2: Extract coordinates (start_box or end_box) using regex
            coordinates = re.findall(
                r"(start_box|end_box)='\((\d+),\s*(\d+)\)'", action)

            updated_action = action  # Start with the original action
            for coord_type, x, y in coordinates:
                # Convert x and y to integers
                updated_action = updated_action.replace(
                    f"{coord_type}='({x},{y})'",
                    f"{coord_type}='<|box_start|>({x},{y})<|box_end|>'")
            processed_actions.append(updated_action)

        # Step 5: Reconstruct the final string
        final_string = suffix + "\n\n".join(processed_actions)
    else:
        final_string = input_string
    return final_string