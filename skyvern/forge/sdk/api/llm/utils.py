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
        system_message = {
            "role": "system",
            "content": (
                "CRITICAL INSTRUCTION: You are a JSON-only assistant. Do not respond in anything other than valid JSON and always follow the script you are given preisely.\n"
            )
        }
        
        # Build content array with images first
        content = []
        if screenshots:
            for screenshot in screenshots:
                encoded_image = base64.b64encode(screenshot).decode("utf-8")
                # Use ollama's native format without data URI prefix
                content.append({
                    "type": "image",
                    "data": encoded_image,
                    "format": "png"
                })
        
        # Add text prompt last
        content.append({
            "type": "text", 
            "text": f"{prompt} OUTPUT JSON ONLY."
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
        content = response.choices[0].message.content
        if add_assistant_prefix:
            content = "{" + content

        # Extract JSON if wrapped in markdown (for Llama)
        if is_llama and content.strip().startswith("```"):
            json_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
            match = re.search(json_pattern, content, re.MULTILINE)
            if match:
                content = match.group(1).strip()

        return commentjson.loads(content)
                
    except Exception as e:
        LOG.error("Failed to parse LLM response.", content=content)
        raise InvalidLLMResponseFormat(content) from e

def fix_cutoff_json(json_string: str, error_position: int) -> dict[str, Any]:
    """
    Fixes a cutoff JSON string by ignoring the last incomplete action and making it a valid JSON.

    Args:
    json_string (str): The cutoff JSON string to process.
    error_position (int): The position of the error in the JSON string.

    Returns:
    str: The fixed JSON string.
    """
    LOG.info("Fixing cutoff JSON string.")
    try:
        # Truncate the string to the error position
        truncated_string = json_string[:error_position]
        # Find the last valid action
        last_valid_action_pos = truncated_string.rfind("},")
        if last_valid_action_pos != -1:
            # Remove the incomplete action
            fixed_string = truncated_string[: last_valid_action_pos + 1] + "\n  ]\n}"
            return commentjson.loads(fixed_string)
        else:
            # If no valid action found, return an empty actions list
            LOG.warning("No valid action found in the cutoff JSON string.")
            return {"actions": []}
    except Exception as e:
        raise InvalidLLMResponseFormat(json_string) from e

def fix_unescaped_quotes_in_json(json_string: str) -> str:
    """
    Extracts the positions of quotation marks that define the JSON structure
    and the strings between them, handling unescaped quotation marks within strings.

    Args:
    json_string (str): The JSON-like string to process.

    Returns:
    str: The JSON-like string with unescaped quotation marks within strings.
    """
    escape_char = "\\"
    # Indices to add the escape character to. Since we're processing the string from left to right, we need to sort
    # the indices in descending order to avoid index shifting.
    indices_to_add_escape_char = []
    in_string = False
    escape = False
    json_structure_chars = {",", ":", "}", "]", "{", "["}

    i = 0
    while i < len(json_string):
        char = json_string[i]
        if char == escape_char:
            escape = not escape
        elif char == '"' and not escape:
            if in_string:
                # Check if the next non-whitespace character is a JSON structure character
                j = i + 1
                # Skip whitespace characters
                while j < len(json_string) and json_string[j].isspace():
                    j += 1
                if j < len(json_string) and json_string[j] in json_structure_chars:
                    # If the next character is a JSON structure character, the quote is the end of the JSON string
                    in_string = False
                else:
                    # If the next character is not a JSON structure character, the quote is part of the string
                    # Update the indices to add the escape character with the current index
                    indices_to_add_escape_char.append(i)
            else:
                # Start of the JSON string
                in_string = True
        else:
            escape = False
        i += 1

    # Sort the indices in descending order to avoid index shifting then add the escape character to the string
    if indices_to_add_escape_char:
        LOG.warning("Unescaped quotes found in JSON string. Adding escape character to fix the issue.")
    indices_to_add_escape_char.sort(reverse=True)
    for index in indices_to_add_escape_char:
        json_string = json_string[:index] + escape_char + json_string[index:]

    return json_string

def fix_and_parse_json_string(json_string: str) -> dict[str, Any]:
    """
    Auto-fixes a JSON string by escaping unescaped quotes and ignoring the last action if the JSON is cutoff.

    Args:
    json_string (str): The JSON string to process.

    Returns:
    dict[str, Any]: The parsed JSON object.
    """

    LOG.info("Auto-fixing JSON string.")
    # Escape unescaped quotes in the JSON string
    json_string = fix_unescaped_quotes_in_json(json_string)
    try:
        # Attempt to parse the JSON string
        return commentjson.loads(json_string)
    except Exception:
        LOG.warning("Failed to parse JSON string. Attempting to fix the JSON string.")
        try:
            # This seems redundant but we're doing this to get error position. Comment json doesn't return that
            return json.loads(json_string)
        except json.JSONDecodeError as e:
            error_position = e.pos
            # Try to fix the cutoff JSON string and see if it can be parsed
            return fix_cutoff_json(json_string, error_position)

def try_to_extract_json_from_markdown_format(text: str) -> str:
    pattern = r"```json\s*(.*?)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1)
    else:
        return text
       
def try_to_extract_json_from_markdown_format_llama(text: str) -> str:
    """Extract JSON content from markdown code blocks.
    This is particularly useful for models like Llama that may wrap their JSON responses.
    
    Args:
        text (str): The text to process, which may contain JSON in markdown blocks
        
    Returns:
        str: The extracted JSON string, or the original text if no JSON found
    """
    # First try to extract from ```json blocks
    json_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(json_pattern, text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    
    # If no code blocks found, try to extract anything that looks like a JSON object
    json_object_pattern = r"\{[\s\S]*?\}"  # Non-greedy match for nested objects
    match = re.search(json_object_pattern, text)
    if match:
        return match.group(0)
    
    # If no JSON-like content found, return original text
    return text
