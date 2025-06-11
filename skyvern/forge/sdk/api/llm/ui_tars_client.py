"""
UI-TARS Client for Seed1.5-VL model via ByteDance Doubao API.
"""

import ast
from io import BytesIO
from typing import Any, Dict, List, Tuple

import structlog
from PIL import Image

from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.api.llm.ui_tars import action_parser, prompts
from skyvern.forge.sdk.api.llm.ui_tars.conversation_manager import (
    APIClient,
    ConversationManager,
    encode_image_from_bytes,
    get_image_format_from_pil,
)
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.actions import (
    Action,
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
        self.task_id = task_id
        
        # Use the APIClient from conversation_manager for consistent API handling
        self.api_client = APIClient(
            api_key=api_key,
            model=model,
            api_base=api_base,
        )
        
        # Use ConversationManager for history-5 management
        self.conversation_manager = ConversationManager(max_history_images=max_history)

    async def generate_actions(self, task: Task, step: Step, scraped_page: ScrapedPage) -> List[Action]:
        """Generate actions for the given task and screenshot using UI-TARS."""
        try:
            # Initialize conversation if empty
            if not self.conversation_manager.get_messages():
                self._initialize_conversation(task)
            
            # Get screenshot and add to history
            if not scraped_page.screenshots:
                raise ValueError("No screenshots available for UI-TARS")
            
            current_screenshot = scraped_page.screenshots[0]
            image = Image.open(BytesIO(current_screenshot))
            image_format = get_image_format_from_pil(image)
            
            screenshot_b64 = encode_image_from_bytes(current_screenshot)
            self.conversation_manager.add_image_message(screenshot_b64, image_format)
            
            # Generate response using API client
            response_content = await self._call_api()
            LOG.debug("UI-TARS response received", task_id=self.task_id, response_length=len(response_content))
            
            # Validate response format
            if not response_content or not response_content.strip():
                raise ValueError("Empty response from UI-TARS API")
            
            if "Action:" not in response_content:
                raise ValueError(f"Invalid UI-TARS response format - missing 'Action:' section: {response_content[:200]}...")
            
            # Add response to history
            self.conversation_manager.add_assistant_response(response_content)
            
            # Get image dimensions for coordinate conversion
            original_image_width, original_image_height = image.size
            LOG.debug("Image dimensions", width=original_image_width, height=original_image_height)
            
            # Use the official UI-TARS parser to get structured actions
            parsed_actions = action_parser.parse_action_to_structure_output(
                response_content, 
                factor=1000,
                origin_resized_height=original_image_height,
                origin_resized_width=original_image_width,
                model_type="doubao"
            )
            
            LOG.info("UI-TARS parsed actions", task_id=self.task_id, parsed_actions=parsed_actions)
            
            # Convert parsed actions to Skyvern action objects
            actions = self._convert_parsed_actions_to_skyvern_actions(
                parsed_actions, task, step, original_image_width, original_image_height
            )
            LOG.info("Converted to Skyvern actions", task_id=self.task_id, actions=actions)
            
            if not actions:
                LOG.warning("No valid actions generated from UI-TARS response", 
                           task_id=self.task_id, response_preview=response_content[:200])
            
            return actions

        except Exception as e:
            LOG.error(
                "UI-TARS action generation failed",
                task_id=self.task_id,
                step_id=step.step_id,
                error=str(e),
                exc_info=True,
            )
            # Return empty actions to trigger retry mechanism
            return []

    async def _call_api(self) -> str:
        """Call the UI-TARS API and return response content."""
        try:
            # Get current conversation messages
            messages = self.conversation_manager.get_messages()
            
            # Use the APIClient for inference (currently sync, but wrapped in async)
            response = self.api_client.inference(messages)
            return response.strip()
            
        except Exception as e:
            LOG.error("UI-TARS API call failed", task_id=self.task_id, error=str(e), exc_info=True)
            raise LLMProviderError(f"UI-TARS API call failed: {str(e)}")

    def _initialize_conversation(self, task: Task) -> None:
        """Initialize conversation with system prompt."""
        system_prompt = self._build_system_prompt(task.navigation_goal)
        self.conversation_manager.initialize_conversation(system_prompt)

    def _build_system_prompt(self, instruction: str, language: str = "English") -> str:
        """Build system prompt for UI-TARS using the imported template."""
        return prompts.COMPUTER_USE_DOUBAO.format(
            language=language,
            instruction=instruction
        )

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
                    start_box = action_inputs.get("start_box", "")
                    if not start_box:
                        LOG.warning("Click action missing start_box coordinates", task_id=self.task_id)
                        continue
                    x, y = self._extract_coordinates_from_box(start_box, image_width, image_height)
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
                    start_box = action_inputs.get("start_box", "")
                    if not start_box:
                        LOG.warning("Left double click action missing start_box coordinates", task_id=self.task_id)
                        continue
                    x, y = self._extract_coordinates_from_box(start_box, image_width, image_height)
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
                    start_box = action_inputs.get("start_box", "")
                    if not start_box:
                        LOG.warning("Right click action missing start_box coordinates", task_id=self.task_id)
                        continue
                    x, y = self._extract_coordinates_from_box(start_box, image_width, image_height)
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
                    if not content:
                        LOG.warning("Type action missing content", task_id=self.task_id)
                        continue
                    # Truncate long content for response message
                    display_content = content[:50] + "..." if len(content) > 50 else content
                    action = InputTextAction(
                        element_id="",
                        text=content,
                        reasoning=thought,
                        intention=thought,
                        response=f"Type: {display_content}",
                        organization_id=task.organization_id,
                        workflow_run_id=task.workflow_run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        action_order=len(actions),
                    )
                elif action_type == "drag" or action_type == "select":
                    start_box = action_inputs.get("start_box", "")
                    end_box = action_inputs.get("end_box", "")
                    if not start_box or not end_box:
                        LOG.warning("Drag action missing start_box or end_box coordinates", 
                                   task_id=self.task_id, start_box=start_box, end_box=end_box)
                        continue
                    start_x, start_y = self._extract_coordinates_from_box(start_box, image_width, image_height)
                    end_x, end_y = self._extract_coordinates_from_box(end_box, image_width, image_height)
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
                    if not key:
                        LOG.warning("Hotkey action missing key combination", task_id=self.task_id)
                        continue
                    # Parse space-separated hotkey string into individual keys
                    # UI-TARS format: "ctrl shift y" -> ["ctrl", "shift", "y"]
                    keys = key.split() if key else []
                    if not keys:
                        LOG.warning("Hotkey action has empty key list", task_id=self.task_id, key=key)
                        continue
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
                    direction = action_inputs.get("direction", "down").lower()
                    start_box = action_inputs.get("start_box", "")
                    if not start_box:
                        LOG.warning("Scroll action missing start_box coordinates", task_id=self.task_id)
                        continue
                    if direction not in ["down", "up", "left", "right"]:
                        LOG.warning("Invalid scroll direction", task_id=self.task_id, direction=direction)
                        direction = "down"  # Default fallback
                    x, y = self._extract_coordinates_from_box(start_box, image_width, image_height)
                    
                    # Convert direction to scroll amounts
                    scroll_amount = 300  # Default scroll amount in pixels
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
                    
                    action = ScrollAction(
                        x=x,
                        y=y,
                        scroll_x=scroll_x,  # Required field
                        scroll_y=scroll_y,  # Required field
                        reasoning=thought,
                        intention=thought,
                        response=f"Scroll {direction} by ({scroll_x}, {scroll_y}) at ({x}, {y})",
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
                    # Extract content from finished action if available
                    finished_content = action_inputs.get("content", "")
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
                    LOG.error(f"Unrecognized action type: {action_type}")
                    continue
                
                actions.append(action)
                
            except Exception as e:
                LOG.error(f"Failed to convert action: {parsed_action}", error=str(e), exc_info=True)
                # Skip failed actions instead of adding fallback wait actions
                continue
        
        return actions

    def _extract_coordinates_from_box(self, box_str: str, image_width: int, image_height: int) -> Tuple[int, int]:
        """Extract coordinates from UI-TARS box format."""
        try:
            if not box_str:
                LOG.warning("Empty box string provided", task_id=self.task_id)
                return image_width // 2, image_height // 2
            
            # Parse the box coordinates from the string format like "[0.5, 0.3, 0.5, 0.3]"
            # The UI-TARS parser should return string representation of list of floats (relative coordinates)
            coords = ast.literal_eval(box_str)
            
            if not isinstance(coords, (list, tuple)):
                raise ValueError(f"Expected list/tuple, got {type(coords)}: {box_str}")
            
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
                raise ValueError(f"Expected 2 or 4 coordinates, got {len(coords)}: {box_str}")
            
            # Validate coordinate ranges for relative coordinates
            if len(coords) == 4:
                if not all(0 <= coord <= 1 for coord in coords):
                    LOG.warning("Relative coordinates outside expected range [0,1]", 
                               task_id=self.task_id, coords=coords)
            elif len(coords) == 2:
                if not (0 <= x1 <= 1 and 0 <= y1 <= 1):
                    LOG.warning("Relative coordinates outside expected range [0,1]", 
                               task_id=self.task_id, coords=coords)
            
            # Ensure coordinates are within image bounds
            x = max(0, min(x, image_width - 1))
            y = max(0, min(y, image_height - 1))
            
            return x, y
            
        except Exception as e:
            LOG.error("Failed to extract coordinates from box", 
                     task_id=self.task_id, box_str=box_str, error=str(e), exc_info=True)
            # Return center of image as fallback
            return image_width // 2, image_height // 2

    def reset_conversation(self) -> None:
        """Reset the conversation history."""
        self.conversation_manager.reset()


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
