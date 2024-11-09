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
        # Llama 3.2 vision format
        system_message = {
            "role": "system",
            "content": "You are a helpful AI assistant. Respond with pure JSON only, without markdown formatting or explanations. "\
                       "Your response should be a valid JSON object that can be parsed directly. "\
                       "When analyzing images, provide structured responses in pure JSON format."
        }
        
        content = [{"type": "text", "text": prompt}]
        
        if screenshots:
            for screenshot in screenshots:
                encoded_image = base64.b64encode(screenshot).decode("utf-8")
                content.append({
                    "type": "image",
                    "image_url": f"data:image/png;base64,{encoded_image}"
                })
        
        return [
            system_message,
            {
                "role": "user",
                "content": content
            }
        ]
    else:
        # Original format for other models
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
    content = None
    try:
        content = response.choices[0].message.content
        if add_assistant_prefix:
            content = "{" + content

        # First try to extract JSON from markdown code blocks if present
        if content.strip().startswith("```"):
            if is_llama:
                content = try_to_extract_json_from_markdown_format_llama(content)
            else:
                content = try_to_extract_json_from_markdown_format(content)
        
        # Attempt to parse the content as JSON
        try:
            return commentjson.loads(content)
        except ValueError as e:
            LOG.warning("Failed to parse LLM response as JSON. Attempting to auto-fix.", content=content)
            # Attempt to fix unescaped quotes in the JSON string
            fixed_content = fix_unescaped_quotes_in_json(content)
            try:
                return commentjson.loads(fixed_content)
            except Exception as e2:
                LOG.error("Failed to auto-fix JSON string.", content=fixed_content)
                # Try one last time with the JSON extractor
                clean_content = try_to_extract_json_from_markdown_format(content)
                if clean_content != content:
                    try:
                        return commentjson.loads(clean_content)
                    except:
                        pass
                raise InvalidLLMResponseFormat(content) from e2
    except Exception as e:
        LOG.error("Unexpected error while parsing LLM response.", content=content)
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
