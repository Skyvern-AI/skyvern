# SPDX-License-Identifier: Apache-2.0
#
# Adapted from:
# https://github.com/ByteDance-Seed/Seed1.5-VL/blob/main/GUI/gui.ipynb
# Licensed under the Apache License, Version 2.0
#
# Modifications by Skyvern AI:
# - Extracted conversation management logic into reusable classes and functions
# - Added proper error handling and logging
# - Integrated with Skyvern's logging and configuration system

import base64
import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import structlog
from openai import OpenAI
from PIL import Image

LOG = structlog.get_logger()


def encode_image(image_path: str) -> str:
    """Encode image file into base64 string.
    
    Args:
        image_path: Path to the image file
        
    Returns:
        Base64 encoded string of the image
    """
    with open(image_path, "rb") as image_file:
        image = base64.b64encode(image_file.read()).decode('utf-8')
    return image


def encode_image_from_bytes(image_bytes: bytes) -> str:
    """Encode image bytes into base64 string.
    
    Args:
        image_bytes: Raw image bytes
        
    Returns:
        Base64 encoded string of the image
    """
    return base64.b64encode(image_bytes).decode('utf-8')


def get_image_format(image_path: str) -> str:
    """Extract and validate image format from file path.
    
    Args:
        image_path: Path to the image file
        
    Returns:
        Image format string (jpg, jpeg, png, webp)
        
    Raises:
        ValueError: If image format is not supported
    """
    image_format = image_path.split('.')[-1].lower()
    if image_format not in ['jpg', 'jpeg', 'png', 'webp']:
        raise ValueError(f"Unsupported image format: {image_format}. Supported formats: jpg, jpeg, png, webp")
    return image_format


def get_image_format_from_pil(image: Image.Image) -> str:
    """Extract and validate image format from PIL Image object.
    
    Args:
        image: PIL Image object
        
    Returns:
        Image format string, defaults to 'png' if unknown
    """
    format_str = image.format.lower() if image.format else "png"
    if format_str not in ['jpg', 'jpeg', 'png', 'webp']:
        return "png"  # Default to PNG for unsupported formats
    return format_str


class ConversationManager:
    """Manages conversation history with history-5 logic for UI-TARS.
    
    Following the pattern from GUI cookbook:
    - System prompt (user message)
    - All assistant responses (never removed)
    - Only the 5 most recent screenshots (user messages with images)
    
    History-5 example for 8th round:
    - from user (system prompt)
    - from assistant (1st round response)
    - from assistant (2nd round response) 
    - from assistant (3rd round response)
    - from user (4th round image)
    - from assistant (4th round response)
    - from user (5th round image)
    - from assistant (5th round response)
    - from user (6th round image)
    - from assistant (6th round response)
    - from user (7th round image)
    - from assistant (7th round response)
    - from user (8th round image)
    """
    
    def __init__(self, max_history_images: int = 5):
        """Initialize conversation manager.
        
        Args:
            max_history_images: Maximum number of screenshot images to keep in history
        """
        self.max_history_images = max_history_images
        self.conversation_history: List[Dict[str, Any]] = []
        self.image_count = 0
        
    def initialize_conversation(self, system_prompt: str) -> None:
        """Initialize conversation with system prompt.
        
        Args:
            system_prompt: The system prompt to start the conversation
        """
        self.conversation_history = [
            {
                "role": "user",
                "content": system_prompt
            }
        ]
        self.image_count = 0
        LOG.debug("Conversation initialized with system prompt")
        
    def add_image_message(self, image_base64: str, image_format: str = "png") -> None:
        """Add image message to conversation history with history-5 management.
        
        Args:
            image_base64: Base64 encoded image string
            image_format: Image format (jpg, jpeg, png, webp)
        """
        if image_format not in ['jpg', 'jpeg', 'png', 'webp']:
            LOG.warning(f"Unsupported image format: {image_format}, defaulting to png")
            image_format = "png"
            
        image_message = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/{image_format};base64,{image_base64}"
                    }
                }
            ]
        }
        
        self.conversation_history.append(image_message)
        self.image_count += 1
        self._maintain_history_limit()
        LOG.debug(f"Added image message, total images: {self.image_count}")
        
    def add_assistant_response(self, response: str) -> None:
        """Add assistant response to conversation history.
        
        Args:
            response: The assistant's response text
        """
        self.conversation_history.append({
            "role": "assistant", 
            "content": response
        })
        LOG.debug("Added assistant response to conversation")
        
    def _maintain_history_limit(self) -> None:
        """Maintain history-5 limit: keep system prompt + all assistant responses + last N screenshots.
        
        Following the pattern from GUI cookbook:
        - System prompt (user message) - never removed
        - All assistant responses - never removed  
        - Only the N most recent screenshots (user messages with images)
        """
        if self.image_count <= self.max_history_images:
            return
        
        # Remove oldest screenshot only (keep all assistant responses)
        # Find first user message with image after system prompt
        removed_count = 0
        images_to_remove = self.image_count - self.max_history_images
        
        i = 1  # Start after system prompt
        while i < len(self.conversation_history) and removed_count < images_to_remove:
            message = self.conversation_history[i]
            if (message["role"] == "user" and 
                isinstance(message["content"], list) and 
                len(message["content"]) > 0 and 
                message["content"][0].get("type") == "image_url"):
                
                # Remove only the screenshot message, keep all assistant responses
                self.conversation_history.pop(i)
                self.image_count -= 1
                removed_count += 1
                # Don't increment i since we removed an element
            else:
                i += 1
                
        LOG.debug(f"Maintained history limit, removed {removed_count} old images, current images: {self.image_count}")
        
    def get_messages(self) -> List[Dict[str, Any]]:
        """Get the current conversation messages.
        
        Returns:
            List of conversation messages in OpenAI format
        """
        return self.conversation_history.copy()
        
    def reset(self) -> None:
        """Reset the conversation history."""
        self.conversation_history = []
        self.image_count = 0
        LOG.debug("Conversation history reset")


class APIClient:
    """Wrapper for OpenAI API client with UI-TARS specific settings."""
    
    def __init__(
        self,
        api_key: str,
        model: str = "doubao-1-5-thinking-vision-pro-250428",
        api_base: str = "https://ark.cn-beijing.volces.com/api/v3",
    ):
        """Initialize API client.
        
        Args:
            api_key: API key for authentication
            model: Model name to use
            api_base: Base URL for the API
        """
        self.api_key = api_key
        self.model = model
        self.api_base = api_base
        
        self.client = OpenAI(
            base_url=api_base,
            api_key=api_key,
        )
        
    def inference(self, messages: List[Dict[str, Any]]) -> str:
        """Run inference with the given messages using UI-TARS cookbook settings.
        
        Args:
            messages: List of conversation messages
            
        Returns:
            Generated response text
            
        Raises:
            Exception: If API call fails
        """
        try:
            chat_completion = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                top_p=None,
                temperature=0.0,
                max_tokens=400,
                stream=True,
                seed=None,
                stop=None,
                frequency_penalty=None,
                presence_penalty=None
            )

            response = ""
            for message in chat_completion:
                if message.choices[0].delta.content is not None:
                    response += message.choices[0].delta.content
            return response.strip()
            
        except Exception as e:
            LOG.error(f"API inference failed: {str(e)}")
            raise
