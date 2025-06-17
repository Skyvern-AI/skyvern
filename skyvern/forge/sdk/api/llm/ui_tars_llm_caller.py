#
# SPDX-License-Identifier: Apache-2.0

# Code partially adapted from:
# https://github.com/ByteDance-Seed/Seed1.5-VL/blob/main/GUI/gui.ipynb
#
# Licensed under the Apache License, Version 2.0
#
# For managing the conversation history of the UI-TARS agent.
#

"""
UI-TARS LLM Caller that follows the standard LLMCaller pattern.
"""

import base64
from io import BytesIO
from typing import Any, Dict

import structlog
from PIL import Image

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task

LOG = structlog.get_logger()


def _build_system_prompt(instruction: str, language: str = "English") -> str:
    """Build system prompt for UI-TARS using the prompt engine."""
    return prompt_engine.load_prompt("ui-tars-system-prompt", language=language, instruction=instruction)


def _is_image_message(message: Dict[str, Any]) -> bool:
    """Check if message contains an image."""
    return (
        message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(item.get("type") == "image_url" for item in message["content"])
    )


class UITarsLLMCaller(LLMCaller):
    """
    UI-TARS specific LLM caller that manages conversation history.
    Follows the established LLMCaller pattern used by Anthropic CUA.
    """

    def __init__(self, llm_key: str, screenshot_scaling_enabled: bool = False):
        super().__init__(llm_key, screenshot_scaling_enabled)
        self.max_history_images = 5
        self._conversation_initialized = False

    def initialize_conversation(self, task: Task) -> None:
        """Initialize conversation with system prompt for the given task."""
        if not self._conversation_initialized:
            # Handle None case for navigation_goal
            instruction = task.navigation_goal or "Default navigation task"
            system_prompt = _build_system_prompt(instruction)
            self.message_history: list = [{"role": "user", "content": system_prompt}]
            self._conversation_initialized = True
            LOG.debug("Initialized UI-TARS conversation", task_id=task.task_id)

    def add_screenshot(self, screenshot_bytes: bytes) -> None:
        """Add screenshot to conversation history."""
        if not screenshot_bytes:
            return

        # Convert to PIL Image to get format
        image = Image.open(BytesIO(screenshot_bytes))
        image_format = self._get_image_format_from_pil(image)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Add image message
        image_message = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{screenshot_b64}"}}
            ],
        }

        self.message_history.append(image_message)
        self._maintain_history_limit()

        LOG.debug("Added screenshot to conversation", total_messages=len(self.message_history))

    def add_assistant_response(self, response: str) -> None:
        """Add assistant response to conversation history."""
        self.message_history.append({"role": "assistant", "content": response})
        LOG.debug("Added assistant response to conversation")

    def _maintain_history_limit(self) -> None:
        """Maintain history limit: keep system prompt + all assistant responses + last N screenshots."""
        image_count = self._count_image_messages()

        if image_count <= self.max_history_images:
            return

        # Ensure we have a system prompt (first message should be user with string content)
        if (
            not self.message_history
            or self.message_history[0]["role"] != "user"
            or not isinstance(self.message_history[0]["content"], str)
        ):
            LOG.error("Conversation history corrupted - missing system prompt")
            return

        # Remove oldest screenshots only (keep system prompt and all assistant responses)
        removed_count = 0
        images_to_remove = image_count - self.max_history_images

        i = 1  # Start after system prompt (index 0)
        while i < len(self.message_history) and removed_count < images_to_remove:
            message = self.message_history[i]
            if _is_image_message(message):
                # Remove only the screenshot message, keep all assistant responses
                self.message_history.pop(i)
                removed_count += 1
                # Don't increment i since we removed an element
            else:
                i += 1

        LOG.debug(
            f"Maintained history limit, removed {removed_count} old images, "
            f"current messages: {len(self.message_history)}"
        )

    def _count_image_messages(self) -> int:
        """Count existing image messages in the conversation history."""
        count = 0
        for message in self.message_history:
            if _is_image_message(message):
                count += 1
        return count

    def _get_image_format_from_pil(self, image: Image.Image) -> str:
        """Extract and validate image format from PIL Image object."""
        format_str = image.format.lower() if image.format else "png"
        if format_str not in ["jpg", "jpeg", "png", "webp"]:
            return "png"  # Default to PNG for unsupported formats
        return format_str

    async def generate_ui_tars_response(self, step: Step) -> str:
        """Generate UI-TARS response using the parent LLMCaller directly."""
        response = await self.call(
            step=step,
            use_message_history=True,  # Use conversation history
            raw_response=True,  # Skip JSON parsing for plain text
        )

        content = response["choices"][0]["message"]["content"]

        # Add the response to conversation history
        self.add_assistant_response(content)

        return content
