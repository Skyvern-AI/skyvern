import base64
import re
from typing import Any

import commentjson
import litellm

from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat
from skyvern.forge.sdk.settings_manager import SettingsManager


async def llm_messages_builder(
    prompt: str,
    screenshots: list[bytes] | None = None,
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
    if SettingsManager.get_settings().ENABLE_ANTHROPIC:
        return [{"role": "user", "content": messages}, {"role": "assistant", "content": "{"}]
    return [{"role": "user", "content": messages}]


def parse_api_response(response: litellm.ModelResponse) -> dict[str, str]:
    try:
        content = response.choices[0].message.content
        # Since we prefilled Anthropic response with "{" we need to add it back to the response to have a valid json object:
        if SettingsManager.get_settings().ENABLE_ANTHROPIC:
            content = "{" + content
        content = try_to_extract_json_from_markdown_format(content)
        content = replace_useless_text_around_json(content)
        if not content:
            raise EmptyLLMResponseError(str(response))
        return commentjson.loads(content)
    except Exception as e:
        raise InvalidLLMResponseFormat(str(response)) from e


def replace_useless_text_around_json(input_string):
    first_occurrence_of_brace = input_string.find("{")
    last_occurrence_of_brace = input_string.rfind("}")
    return input_string[first_occurrence_of_brace : last_occurrence_of_brace + 1]


def try_to_extract_json_from_markdown_format(text):
    pattern = r"```json\s*(.*?)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1)
    else:
        return text
    