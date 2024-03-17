import base64
from typing import Any

import commentjson
import litellm

from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat


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

    return [{"role": "user", "content": messages}]


def parse_api_response(response: litellm.ModelResponse) -> dict[str, str]:
    try:
        content = response.choices[0].message.content
        content = content.replace("```json", "")
        content = content.replace("```", "")
        if not content:
            raise EmptyLLMResponseError(str(response))
        return commentjson.loads(content)
    except Exception as e:
        raise InvalidLLMResponseFormat(str(response)) from e
