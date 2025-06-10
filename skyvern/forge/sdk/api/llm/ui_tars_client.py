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
            
            image = Image.open(BytesIO(scraped_page.screenshots[0]))
            original_image_width, original_image_height = image.size
            model_type = "doubao"  # Use doubao model type for UI-TARS
            
            # Use the official UI-TARS parser to get structured actions
            parsed_actions = self.parse_action_to_structure_output(
                response_content, 
                factor=1000,
                origin_resized_height=original_image_height,
                origin_resized_width=original_image_width,
                model_type=model_type
            )
            
            # LOG.info(f"UI-TARS parsed actions: {parsed_actions}")
            print(f"UI-TARS parsed actions: {parsed_actions}")
            
            # Convert parsed actions to Skyvern action objects
            actions = self._convert_parsed_actions_to_skyvern_actions(parsed_actions, task, step, original_image_width, original_image_height)
            print(f"Skyvern actions: {actions}")
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
                    direction = action_inputs.get("direction", "down").lower()
                    x, y = self._extract_coordinates_from_box(action_inputs.get("start_box", ""), image_width, image_height)
                    
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
                
    def convert_point_to_coordinates(self, text: str, is_answer: bool = False) -> str:
        # Match the two integers inside each <point> … </point> tag
        pattern = r"<point>(\d+)\s+(\d+)</point>"

        def replace_match(match: re.Match) -> str:
            x1, y1 = map(int, match.groups())
            x = (x1 + x1) // 2
            y = (y1 + y1) // 2
            if is_answer:
                return f"({x},{y})"
            return f"({x},{y})"

        text = re.sub(r"\[EOS\]", "", text)
        return re.sub(pattern, replace_match, text).strip()

    def parse_action(self, action_str):
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


    def escape_single_quotes(self, text):
        # Match unescaped single quotes (not matching \\')
        pattern = r"(?<!\\)'"
        return re.sub(pattern, r"\\'", text)


    def round_by_factor(self, number: int, factor: int) -> int:
        """Returns the closest integer to 'number' that is divisible by 'factor'."""
        return round(number / factor) * factor


    def ceil_by_factor(self, number: int, factor: int) -> int:
        """Returns the smallest integer greater than or equal to 'number' that is divisible by 'factor'."""
        return math.ceil(number / factor) * factor


    def floor_by_factor(self, number: int, factor: int) -> int:
        """Returns the largest integer less than or equal to 'number' that is divisible by 'factor'."""
        return math.floor(number / factor) * factor



    def smart_resize(self, height: int,
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
        h_bar = max(factor, self.round_by_factor(height, factor))
        w_bar = max(factor, self.round_by_factor(width, factor))
        if h_bar * w_bar > max_pixels:
            beta = math.sqrt((height * width) / max_pixels)
            h_bar = self.floor_by_factor(height / beta, factor)
            w_bar = self.floor_by_factor(width / beta, factor)
        elif h_bar * w_bar < min_pixels:
            beta = math.sqrt(min_pixels / (height * width))
            h_bar = self.ceil_by_factor(height * beta, factor)
            w_bar = self.ceil_by_factor(width * beta, factor)
        return h_bar, w_bar


    def parse_action_to_structure_output(self, text,
                                        factor,
                                        origin_resized_height,
                                        origin_resized_width,
                                        model_type="qwen25vl",
                                        max_pixels=16384 * 28 * 28,
                                        min_pixels=100 * 28 * 28):
        text = text.strip()

        if "<point>" in text:
            text = self.convert_point_to_coordinates(text)
        if "start_point=" in text:
            text = text.replace("start_point=", "start_box=")
        if "end_point=" in text:
            text = text.replace("end_point=", "end_box=")
        if "point=" in text:
            text = text.replace("point=", "start_box=")

        if model_type == "qwen25vl":
            smart_resize_height, smart_resize_width = self.smart_resize(
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
                action_str = self.escape_single_quotes(content)
                action_str = "type(content='" + action_str + "')"
            if not action_str.strip().endswith(")"):
                action_str = action_str.strip() + ")"
            all_action.append(action_str)

        parsed_actions = [
            self.parse_action(action.replace("\n", "\\n").lstrip())
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
