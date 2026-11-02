import asyncio
import base64
import copy
import io
import json
import re
from typing import Any

import filetype
import json_repair
import litellm
import structlog
from PIL import Image

from skyvern.constants import MAX_IMAGE_BLOCKS
from skyvern.forge.sdk.api.llm import commentjson
from skyvern.forge.sdk.api.llm.exceptions import (
    EmptyLLMResponseError,
    InvalidLLMResponseFormat,
    InvalidLLMResponseType,
    LLMOutputTruncatedError,
)
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig

LOG = structlog.get_logger()

_PASS_THROUGH_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
# A file under the upload cap can still decode to hundreds of megapixels, which RGBA conversion
# would expand to gigabytes in the worker process.
_MAX_DECODED_IMAGE_PIXELS = 25_000_000
_MAX_DECODED_IMAGE_FRAMES = MAX_IMAGE_BLOCKS
# Keep the frame cap as the limiting guard for roughly 4 MP fax pages.
_MAX_TOTAL_DECODED_IMAGE_PIXELS = _MAX_DECODED_IMAGE_FRAMES * 4_000_000


def is_anthropic_model(model: str) -> bool:
    try:
        provider_model, provider, _, _ = litellm.get_llm_provider(model)
    except litellm.exceptions.BadRequestError:
        return False
    return provider == "anthropic" or (provider == "bedrock" and "anthropic." in provider_model)


def uses_only_anthropic_models(llm_config: LLMConfig | LLMRouterConfig) -> bool:
    if isinstance(llm_config, LLMConfig):
        return is_anthropic_model(llm_config.model_name)
    return all(is_anthropic_model(deployment.litellm_params["model"]) for deployment in llm_config.model_list)


def is_image_message(message: dict[str, Any]) -> bool:
    """Check if message contains an image."""
    return (
        message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(item.get("type") == "image_url" for item in message["content"])
    )


async def normalize_llm_image(image: bytes) -> tuple[str, bytes] | list[tuple[str, bytes]]:
    media_type = filetype.guess_mime(image) or "image/png"
    if media_type in _PASS_THROUGH_IMAGE_MEDIA_TYPES:
        return media_type, image
    return await asyncio.to_thread(_reencode_llm_image_as_png, image, media_type)


def _reencode_llm_image_as_png(image: bytes, media_type: str) -> tuple[str, bytes] | list[tuple[str, bytes]]:
    # Converting would drop extra frames, clip high-bit-depth samples, or expand a heavily compressed
    # image to gigabytes in the worker. Those are refused here rather than sent in a format the
    # provider will not decode, which would fail later and less clearly.
    refusal = ""
    png_images: list[bytes] = []
    try:
        with Image.open(io.BytesIO(image)) as decoded:
            frame_count = getattr(decoded, "n_frames", 1)
            if frame_count > 1 and media_type != "image/tiff":
                refusal = "it has more than one frame"
            elif frame_count > _MAX_DECODED_IMAGE_FRAMES:
                refusal = f"it has more than {_MAX_DECODED_IMAGE_FRAMES} frames"
            else:
                total_pixels = 0
                for frame_index in range(frame_count):
                    decoded.seek(frame_index)
                    if decoded.mode.startswith("I") or decoded.mode == "F":
                        refusal = "it stores more than 8 bits per channel"
                        break
                    frame_pixels = decoded.width * decoded.height
                    if frame_pixels > _MAX_DECODED_IMAGE_PIXELS:
                        refusal = f"it decodes to more than {_MAX_DECODED_IMAGE_PIXELS} pixels"
                        break
                    if total_pixels + frame_pixels > _MAX_TOTAL_DECODED_IMAGE_PIXELS:
                        refusal = f"it decodes to more than {_MAX_TOTAL_DECODED_IMAGE_PIXELS} pixels across frames"
                        break
                    total_pixels += frame_pixels
                    buffer = io.BytesIO()
                    decoded.convert("RGBA").save(buffer, format="PNG")
                    png_images.append(buffer.getvalue())
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(f"This {media_type} image could not be decoded to send to the model") from error
    if refusal:
        raise ValueError(f"This {media_type} image cannot be sent to the model because {refusal}")
    if len(png_images) == 1:
        return "image/png", png_images[0]
    return [("image/png", png_image) for png_image in png_images]


async def llm_messages_builder(
    prompt: str,
    screenshots: list[bytes] | None = None,
    add_assistant_prefix: bool = False,
    message_pattern: str = "openai",
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": prompt,
        }
    ]

    if screenshots:
        image_count = 0
        for screenshot in screenshots:
            normalized_images = await normalize_llm_image(screenshot)
            for media_type, image_bytes in (
                normalized_images if isinstance(normalized_images, list) else [normalized_images]
            ):
                if image_count >= MAX_IMAGE_BLOCKS:
                    raise ValueError(f"Cannot send a model request with more than {MAX_IMAGE_BLOCKS} image blocks")
                image_count += 1
                encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                if message_pattern == "anthropic":
                    message = {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded_image,
                        },
                    }
                else:
                    message = {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded_image}",
                        },
                    }
                messages.append(message)
    # Anthropic models seems to struggle to always output a valid json object so we need to prefill the response to force it:
    if add_assistant_prefix:
        return [
            {"role": "user", "content": messages},
            {"role": "assistant", "content": "{"},
        ]
    return [{"role": "user", "content": messages}]


async def llm_messages_builder_with_history(
    prompt: str | None = None,
    screenshots: list[bytes] | None = None,
    message_history: list[dict[str, Any]] | None = None,
    message_pattern: str = "openai",
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if message_history:
        messages = copy.deepcopy(message_history)

    current_user_messages: list[dict[str, Any]] = []
    if prompt:
        current_user_messages.append(
            {
                "type": "text",
                "text": prompt,
            }
        )

    current_image_count = 0
    if screenshots:
        for screenshot in screenshots:
            normalized_images = await normalize_llm_image(screenshot)
            for media_type, image_bytes in (
                normalized_images if isinstance(normalized_images, list) else [normalized_images]
            ):
                if current_image_count >= MAX_IMAGE_BLOCKS:
                    raise ValueError(f"Cannot send a model request with more than {MAX_IMAGE_BLOCKS} image blocks")
                current_image_count += 1
                encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                message: dict[str, Any]
                if message_pattern == "anthropic":
                    message = {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded_image,
                        },
                    }
                else:
                    message = {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded_image}",
                        },
                    }
                current_user_messages.append(message)

    history_image_count = sum(
        1
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") in ("image", "image_url")
    )
    images_to_delete = history_image_count + current_image_count - MAX_IMAGE_BLOCKS
    if images_to_delete > 0:
        new_messages = []
        for message in messages:
            blocks = message.get("content")
            if not isinstance(blocks, list) or images_to_delete == 0:
                new_messages.append(message)
                continue
            new_content = []
            for block in blocks:
                if images_to_delete > 0 and isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    images_to_delete -= 1
                else:
                    new_content.append(block)
            if new_content or not blocks:
                message["content"] = new_content
                new_messages.append(message)
        messages = new_messages

    if current_user_messages:
        messages.append({"role": "user", "content": current_user_messages})

    return messages


def _dedupe_consecutive_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for item in items:
        if deduped and deduped[-1] == item:
            continue
        deduped.append(item)
    return deduped


# Prompts consumed as an actions array; only these wrap a bare action_type-dict
# list into {"actions": [...]}. Single-object callers (custom-select) must not.
_ACTIONS_ARRAY_PROMPT_NAMES = frozenset({"extract-actions", "decisive-criterion-validate"})


def _reassemble_list_response(response: list[Any], allow_action_list_wrap: bool) -> dict[str, Any] | None:
    """Recover the intended single-object response when a model splits it into
    several top-level JSON values. "action_type" is the Action model's
    discriminator field, so its presence identifies action dicts structurally.
    Returns None when the list doesn't match a recoverable shape.
    """
    dicts = [item for item in response if isinstance(item, dict)]
    lists = [item for item in response if isinstance(item, list)]

    # Reasoning object + nested action array. This shape is unambiguous (exactly one
    # nested action array beside dicts that carry no decision of their own), so it
    # never collides with single-object prompts and is recovered regardless of
    # caller. An unescaped quote inside the reasoning text ends that string early
    # and strands the remainder as extra fragments, so surplus non-action fragments
    # are tolerated as long as only one action array survives.
    action_lists = [
        item for item in lists if item and all(isinstance(entry, dict) and "action_type" in entry for entry in item)
    ]
    if dicts and len(action_lists) == 1 and all("actions" not in d and "action_type" not in d for d in dicts):
        return {**dicts[0], "actions": action_lists[0]}

    # Bare list of action dicts. Collides with single-object callers whose object
    # carries action_type, so only wrap when the caller consumes an actions array.
    if allow_action_list_wrap and dicts and not lists and all("action_type" in d for d in dicts):
        return {"actions": _dedupe_consecutive_dicts(dicts)}

    return None


def llm_response_log_metadata(response: Any) -> dict[str, str | int]:
    """Return content-free metadata suitable for logging an LLM response."""
    metadata: dict[str, str | int] = {"response_type": type(response).__name__}
    if isinstance(response, dict):
        metadata["response_keys_count"] = len(response)
    elif isinstance(response, (list, str)):
        metadata["response_length"] = len(response)
    return metadata


def _coerce_response_to_dict(response: Any, prompt_name: str | None = None) -> dict[str, Any]:
    """Ensure parsed LLM responses expose a dict interface to callers."""
    if isinstance(response, dict):
        return response

    LOG.warning(
        "Parsed LLM response is not a dict",
        response_type=type(response).__name__,
        response_length=len(response) if isinstance(response, (list, str)) else None,
        prompt_name=prompt_name,
    )

    if isinstance(response, list):
        reassembled = _reassemble_list_response(response, prompt_name in _ACTIONS_ARRAY_PROMPT_NAMES)
        if reassembled is not None:
            LOG.info(
                "Recovered single-object response from list-shaped LLM output",
                response_length=len(response),
                recovered_keys_count=len(reassembled),
                prompt_name=prompt_name,
            )
            return reassembled

        first_dict = next((item for item in response if isinstance(item, dict)), None)
        LOG.warning(
            "Parsed LLM response is a list; using first dict element",
            response_length=len(response),
            first_item_type=type(response[0]).__name__ if response else None,
            first_item_keys_count=len(first_dict) if first_dict else None,
            prompt_name=prompt_name,
        )
        if first_dict is not None:
            return first_dict

        LOG.warning("List response contained no dict entries; returning empty dict")
        raise InvalidLLMResponseType("list")

    raise InvalidLLMResponseType(type(response).__name__)


def is_truncated_response(response: litellm.ModelResponse) -> bool:
    if not response.choices:
        return False
    choice = response.choices[0]
    message = getattr(choice, "message", None)
    if getattr(message, "content", None) is not None:
        return False
    # A tool call is the answer, so an empty content field alongside one is complete, not truncated.
    if getattr(message, "tool_calls", None):
        return False
    # A model that spent its completion budget reasoning returns no answer at all, and upstreams
    # label that two ways: `length` where the cap is counted against the reply, `stop` where the
    # reasoning stream reads as a normal completion. Only `length` used to be recognised, so the
    # `stop` form skipped the retry below and failed as an unexplained JSON parse error
    # (SKY-19198). `content_filter` is a refusal with its own recovery path and must not land here.
    return getattr(choice, "finish_reason", None) in ("length", "stop")


def is_content_filtered_response(response: litellm.ModelResponse) -> bool:
    # Gemini's non-configurable safety filters (prohibited-content / SPII) block PII-heavy
    # prompts at the input stage, returning finish_reason=content_filter with empty content.
    # This is a valid ModelResponse, so litellm's router fallback never fires — the caller
    # must detect it and retry on a non-Gemini fallback explicitly (SKY-11766).
    if not response.choices:
        return False
    choice = response.choices[0]
    return (
        getattr(choice, "finish_reason", None) == "content_filter"
        and getattr(getattr(choice, "message", None), "content", None) is None
    )


def parse_api_response(
    response: litellm.ModelResponse,
    add_assistant_prefix: bool = False,
    force_dict: bool = True,
    prompt_name: str | None = None,
) -> dict[str, Any] | Any:
    if is_truncated_response(response):
        usage = response.usage if hasattr(response, "usage") and response.usage else None
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        detail = getattr(usage, "completion_tokens_details", None) if usage else None
        reasoning_tokens = (getattr(detail, "reasoning_tokens", 0) or 0) if detail else 0
        raise LLMOutputTruncatedError(
            model=response.model or "unknown",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            finish_reason=getattr(response.choices[0], "finish_reason", None) if response.choices else None,
        )

    content = None
    try:
        content = response.choices[0].message.content
        if content is not None and response.choices and getattr(response.choices[0], "finish_reason", None) == "length":
            LOG.warning(
                "LLM response has finish_reason=length with partial content — output may be incomplete",
                model=response.model,
            )

        # Since we prefilled Anthropic response with "{" we need to add it back to the response to have a valid json object:
        if add_assistant_prefix:
            content = "{" + content

        parsed = json_repair.loads(content)
        if not force_dict:
            return parsed
        return _coerce_response_to_dict(parsed, prompt_name)

    except Exception as parse_error:
        LOG.warning(
            "Failed to parse LLM response using json_repair. Will retry auto-fixing the response for unescaped quotes.",
            error_type=type(parse_error).__name__,
        )
        try:
            if not content:
                raise EmptyLLMResponseError("response content was empty")
            content = _try_to_extract_json_from_markdown_format(content)
            parsed = commentjson.loads(content)
            if not force_dict:
                return parsed
            return _coerce_response_to_dict(parsed, prompt_name)
        except Exception as e:
            if content:
                choices = getattr(response, "choices", None) or []
                choice = choices[0] if choices else None
                usage = getattr(response, "usage", None)
                LOG.warning(
                    "Failed to parse LLM response. Will retry auto-fixing the response for unescaped quotes.",
                    error_type=type(e).__name__,
                    model=getattr(response, "model", None),
                    prompt_name=prompt_name,
                    response_type=type(response).__name__,
                    content_length=len(content),
                    stop_reason=getattr(choice, "finish_reason", None),
                    token_usage=(
                        {
                            "prompt_tokens": getattr(usage, "prompt_tokens", None),
                            "completion_tokens": getattr(usage, "completion_tokens", None),
                            "total_tokens": getattr(usage, "total_tokens", None),
                        }
                        if usage
                        else None
                    ),
                    response_id=getattr(response, "id", None),
                )
                try:
                    parsed = _fix_and_parse_json_string(content)
                    if not force_dict:
                        return parsed
                    return _coerce_response_to_dict(parsed, prompt_name)
                except Exception as e2:
                    LOG.warning("Failed to auto-fix LLM response.", error_type=type(e2).__name__)
                    raise InvalidLLMResponseFormat("unable to parse response as JSON") from None

            raise InvalidLLMResponseFormat("unable to parse response as JSON") from None


def _fix_cutoff_json(json_string: str, error_position: int) -> dict[str, Any]:
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


def _fix_unescaped_quotes_in_json(json_string: str) -> str:
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


def _fix_and_parse_json_string(json_string: str) -> dict[str, Any]:
    """
    Auto-fixes a JSON string by escaping unescaped quotes and ignoring the last action if the JSON is cutoff.

    Args:
    json_string (str): The JSON string to process.

    Returns:
    dict[str, Any]: The parsed JSON object.
    """

    LOG.info("Auto-fixing JSON string.")
    # Escape unescaped quotes in the JSON string
    json_string = _fix_unescaped_quotes_in_json(json_string)
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
            return _fix_cutoff_json(json_string, error_position)


def loads_with_repair(content: str) -> Any:
    """Parse JSON, repairing invalid control characters instead of raising.

    Rendering the hashed-href map back into a serialized LLM response injects raw
    page values (hrefs, attribute text) that can carry literal control characters,
    which strict ``json.loads`` rejects. json_repair tolerates them.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        LOG.warning("Strict JSON parse failed; falling back to json_repair.", exc_info=True)
        return json_repair.loads(content)


def _try_to_extract_json_from_markdown_format(text: str) -> str:
    pattern = r"```json\s*(.*?)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1)
    else:
        return text
