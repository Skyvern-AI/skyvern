"""
UI-TARS Client for Seed1.5-VL model via ByteDance Doubao API.
Implements history-5 conversation approach and action parsing.
"""

import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import structlog
from openai import OpenAI

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
            
            # Add response to history
            self.add_assistant_response(response_content)
            
            # Parse response into actions
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