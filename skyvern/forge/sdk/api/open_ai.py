import base64
import json
import random
from datetime import datetime, timedelta
from typing import Any

import commentjson
import openai
import structlog
from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion

from skyvern.exceptions import InvalidOpenAIResponseFormat, NoAvailableOpenAIClients, OpenAIRequestTooBigError
from skyvern.forge import app
from skyvern.forge.sdk.api.chat_completion_price import ChatCompletionPrice
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class OpenAIKeyClientWrapper:
    client: AsyncOpenAI
    key: str
    remaining_requests: int | None

    def __init__(self, key: str, remaining_requests: int | None) -> None:
        self.key = key
        self.remaining_requests = remaining_requests
        self.updated_at = datetime.utcnow()
        self.client = AsyncOpenAI(api_key=self.key)

    def update_remaining_requests(self, remaining_requests: int | None) -> None:
        self.remaining_requests = remaining_requests
        self.updated_at = datetime.utcnow()

    def is_available(self) -> bool:
        # If remaining_requests is None, then it's the first time we're trying this key
        # so we can assume it's available, otherwise we check if it's greater than 0
        if self.remaining_requests is None:
            return True

        if self.remaining_requests > 0:
            return True

        # If we haven't checked this in over 1 minutes, check it again
        # Most of our failures are because of Tokens-per-minute (TPM) limits
        if self.updated_at < (datetime.utcnow() - timedelta(minutes=1)):
            return True

        return False


class OpenAIClientManager:
    # TODO Support other models for requests without screenshots, track rate limits for each model and key as well if any
    clients: list[OpenAIKeyClientWrapper]

    def __init__(self, api_keys: list[str] = SettingsManager.get_settings().OPENAI_API_KEYS) -> None:
        self.clients = [OpenAIKeyClientWrapper(key, None) for key in api_keys]

    def get_available_client(self) -> OpenAIKeyClientWrapper | None:
        available_clients = [client for client in self.clients if client.is_available()]

        if not available_clients:
            return None

        # Randomly select an available client to distribute requests across our accounts
        return random.choice(available_clients)

    async def content_builder(
        self,
        step: Step,
        screenshots: list[bytes] | None = None,
        prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []

        if prompt is not None:
            content.append(
                {
                    "type": "text",
                    "text": prompt,
                }
            )

            await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.LLM_PROMPT,
                data=prompt.encode("utf-8"),
            )
        if screenshots:
            for screenshot in screenshots:
                encoded_image = base64.b64encode(screenshot).decode("utf-8")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{encoded_image}",
                        },
                    }
                )
                # create artifact for each image
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )

        return content

    async def chat_completion(
        self,
        step: Step,
        model: str = "gpt-4-vision-preview",
        max_tokens: int = 4096,
        temperature: int = 0,
        screenshots: list[bytes] | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        LOG.info(
            f"Sending LLM request",
            task_id=step.task_id,
            step_id=step.step_id,
            num_screenshots=len(screenshots) if screenshots else 0,
        )
        messages = [
            {
                "role": "user",
                "content": await self.content_builder(
                    step=step,
                    screenshots=screenshots,
                    prompt=prompt,
                ),
            }
        ]

        chat_completion_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.LLM_REQUEST,
            data=json.dumps(chat_completion_kwargs).encode("utf-8"),
        )
        available_client = self.get_available_client()
        if available_client is None:
            raise NoAvailableOpenAIClients()
        try:
            response = await available_client.client.chat.completions.with_raw_response.create(**chat_completion_kwargs)
        except openai.RateLimitError as e:
            # If we get a RateLimitError, we can assume the key is not available anymore
            if e.code == 429:
                raise OpenAIRequestTooBigError(e.message)
            LOG.warning(
                "OpenAI rate limit exceeded, marking key as unavailable.", error_code=e.code, error_message=e.message
            )
            available_client.update_remaining_requests(remaining_requests=0)
            available_client = self.get_available_client()
            if available_client is None:
                raise NoAvailableOpenAIClients()
            return await self.chat_completion(
                step=step,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                screenshots=screenshots,
                prompt=prompt,
            )
        # TODO: https://platform.openai.com/docs/guides/rate-limits/rate-limits-in-headers
        # use other headers, x-ratelimit-limit-requests, x-ratelimit-limit-tokens, x-ratelimit-remaining-tokens
        # x-ratelimit-reset-requests, x-ratelimit-reset-tokens to write a more accurate algorithm for managing api keys

        # If we get a response, we can assume the key is available and update the remaining requests
        ratelimit_remaining_requests = response.headers.get("x-ratelimit-remaining-requests")

        if not ratelimit_remaining_requests:
            LOG.warning("Invalid x-ratelimit-remaining-requests from OpenAI", response.headers)

        available_client.update_remaining_requests(remaining_requests=int(ratelimit_remaining_requests))
        chat_completion = response.parse()

        if chat_completion.usage is not None:
            # TODO (Suchintan): Is this bad design?
            step = await app.DATABASE.update_step(
                step_id=step.step_id,
                task_id=step.task_id,
                organization_id=step.organization_id,
                chat_completion_price=ChatCompletionPrice(
                    input_token_count=chat_completion.usage.prompt_tokens,
                    output_token_count=chat_completion.usage.completion_tokens,
                    model_name=model,
                ),
            )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.LLM_RESPONSE,
            data=chat_completion.model_dump_json(indent=2).encode("utf-8"),
        )
        parsed_response = self.parse_response(chat_completion)
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
            data=json.dumps(parsed_response, indent=2).encode("utf-8"),
        )
        return parsed_response

    def parse_response(self, response: ChatCompletion) -> dict[str, str]:
        try:
            content = response.choices[0].message.content
            content = content.replace("```json", "")
            content = content.replace("```", "")
            if not content:
                raise Exception("openai response content is empty")
            return commentjson.loads(content)
        except Exception as e:
            raise InvalidOpenAIResponseFormat(str(response)) from e
