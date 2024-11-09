import base64
import json
import re
from typing import Any

import commentjson
import litellm
import structlog

from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

LOG = structlog.get_logger()

async def llm_messages_builder(
    prompt: str,
    screenshots: list[bytes] | None = None,
    add_assistant_prefix: bool = False,
    is_llama: bool = False,
) -> list[dict[str, Any]]:
    if is_llama:
        # Much stricter system message
        system_message = {
            "role": "system",
            "content": (
                "CRITICAL INSTRUCTION: You are a PURE JSON bot. You must NEVER write prose or explanations.\n\n"
                "NO MATTER WHAT IS ASKED:\n"
                "1. ALWAYS respond with actions array\n"
                "2. NEVER write explanations or text\n"
                "3. ONLY valid responses are:\n"
                "{\"actions\": [{\"type\": \"analyze\", \"element\": \"...\"}, ...]}\n"
                "{\"actions\": [{\"type\": \"click\", \"element\": \"...\"}, ...]}\n"
                "{\"actions\": [{\"type\": \"input\", \"element\": \"...\", \"value\": \"...\"}, ...]}\n\n"
                "Even if asked for analysis, description, or explanation, ONLY respond with actions JSON.\n"
                "Even if the question seems general, ONLY respond with actions JSON.\n"
                "NEVER use markdown. NEVER explain. NEVER add notes.\n\n"
                "CORRECT:\n"
                "{\"actions\":[{\"type\":\"analyze\",\"element\":\"search box for part lookup\"}]}\n\n"
                "INCORRECT:\n"
                "Here's what I found...\n"
                "Let me explain...\n"
                "The webpage shows...\n"
                "```json\n{...}```"
            )
        }
        
        # Build content array
        content = []
        if screenshots:
            for screenshot in screenshots:
                encoded_image = base64.b64encode(screenshot).decode("utf-8")
                content.append({
                    "type": "image",
                    "data": encoded_image,
                    "format": "png"
                })
        
        # Force action-based response in prompt
        content.append({
            "type": "text",
            "text": f"{prompt} RESPOND ONLY WITH ACTIONS JSON."
        })
        
        return [
            system_message,
            {
                "role": "user",
                "content": content
            }
        ]
    else:
        # Original format for other models (unchanged)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "You are a helpful assistant."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        if screenshots:
            for screenshot in screenshots:
                encoded_image = base64.b64encode(screenshot).decode("utf-8")
                messages.append({
                    "role": "user",
                    "content": {
                        "type": "image",
                        "image_url": f"data:image/png;base64,{encoded_image}"
                    }
                })
        
        return messages

def parse_api_response(response: litellm.ModelResponse, add_assistant_prefix: bool = False, is_llama: bool = False) -> dict[str, Any]:
    """Parse the response from the LLM API into a dictionary.
    
    Args:
        response: The response from the LLM API
        add_assistant_prefix: Whether to add a prefix to the response
        is_llama: Whether the response is from a Llama/Ollama model
    
    Returns:
        The parsed response as a dictionary
    """
    content = None
    try:
        content = response.choices[0].message.content.strip()
        if add_assistant_prefix:
            content = "{" + content

        # For Llama responses, try to extract just the JSON
        if is_llama:
            # Find anything that looks like a JSON object
            json_pattern = r"\{[^{}]*\}"
            matches = re.finditer(json_pattern, content)
            # Try each match until we find valid JSON
            for match in matches:
                try:
                    return commentjson.loads(match.group(0))
                except:
                    continue
                    
            # If no valid JSON found in matches, try the stripped content
            if content.startswith("{") and content.endswith("}"):
                try:
                    return commentjson.loads(content)
                except:
                    pass
                    
            raise ValueError("No valid JSON found in response")

        # For non-Llama models, use original parsing
        return commentjson.loads(content)
                
    except Exception as e:
        LOG.error("Failed to parse LLM response.", content=content)
        raise InvalidLLMResponseFormat(content) from e


def fix_unescaped_quotes_in_json(json_string: str) -> str:
    """Fix unescaped quotes in JSON string."""
    escape_