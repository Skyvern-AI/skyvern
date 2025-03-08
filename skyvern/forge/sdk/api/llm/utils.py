import base64
import json
import re
from typing import Any

import commentjson
import json_repair
import litellm
import structlog

from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

LOG = structlog.get_logger()


async def llm_messages_builder(
    prompt: str,
    screenshots: list[bytes] | None = None,
    add_assistant_prefix: bool = False,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": prompt,
        }
    ]

    if screenshots:
        for screenshot in screenshots:
            encoded_image = base64.b64encode(screenshot).decode("utf-8")
            messages.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{encoded_image}",
                    },
                }
            )
    # Anthropic models seems to struggle to always output a valid json object so we need to prefill the response to force it:
    if add_assistant_prefix:
        return [
            {"role": "user", "content": messages},
            {"role": "assistant", "content": "{"},
        ]
    return [{"role": "user", "content": messages}]


def parse_api_response(response: litellm.ModelResponse, add_assistant_prefix: bool = False) -> dict[str, Any]:
    content = None
    try:
        content = response.choices[0].message.content
        # Since we prefilled Anthropic response with "{" we need to add it back to the response to have a valid json object:
        if add_assistant_prefix:
            content = "{" + content

        return json_repair.loads(content)

    except Exception:
        LOG.warning(
            "Failed to parse LLM response using json_repair. Will retry auto-fixing the response for unescaped quotes.",
            exc_info=True,
        )
        try:
            if not content:
                raise EmptyLLMResponseError(str(response))
            content = try_to_extract_json_from_markdown_format(content)
            return commentjson.loads(content)
        except Exception as e:
            if content:
                LOG.warning(
                    "Failed to parse LLM response. Will retry auto-fixing the response for unescaped quotes.",
                    exc_info=True,
                    content=content,
                )
                try:
                    return fix_and_parse_json_string(content)
                except Exception as e2:
                    LOG.exception("Failed to auto-fix LLM response.", error=str(e2))
                    raise InvalidLLMResponseFormat(str(response)) from e2

            raise InvalidLLMResponseFormat(str(response)) from e


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
    in_string = False
    escape = False
    json_structure_chars = {",", ":", "}", "]", "{", "["}
    result = []

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
                    # Add the escape character before the quote
                    result.append(escape_char)
            else:
                # Start of the JSON string
                in_string = True
        else:
            escape = False

        # Append the current character to the result
        result.append(char)
        i += 1

    if len(result) != len(json_string):
        LOG.warning("Unescaped quotes found in JSON string. Adding escape character to fix the issue.")

    return "".join(result)


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
