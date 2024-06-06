import dataclasses
import json
from typing import Any

import litellm
import openai
import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateCustomLLMProviderError,
    InvalidLLMConfigError,
    LLMProviderError,
)
from skyvern.forge.sdk.api.llm.models import LLMAPIHandler, LLMRouterConfig
from skyvern.forge.sdk.api.llm.utils import llm_messages_builder, parse_api_response
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class LLMAPIHandlerFactory:
    _custom_handlers: dict[str, LLMAPIHandler] = {}

    @staticmethod
    def get_llm_api_handler_with_router(llm_key: str) -> LLMAPIHandler:
        llm_config = LLMConfigRegistry.get_config(llm_key)
        if not isinstance(llm_config, LLMRouterConfig):
            raise InvalidLLMConfigError(llm_key)

        router = litellm.Router(
            model_list=[dataclasses.asdict(model) for model in llm_config.model_list],
            redis_host=llm_config.redis_host,
            redis_port=llm_config.redis_port,
            redis_password=llm_config.redis_password,
            routing_strategy=llm_config.routing_strategy,
            fallbacks=(
                [{llm_config.main_model_group: [llm_config.fallback_model_group]}]
                if llm_config.fallback_model_group
                else []
            ),
            num_retries=llm_config.num_retries,
            retry_after=llm_config.retry_delay_seconds,
            set_verbose=(False if SettingsManager.get_settings().is_cloud_environment() else llm_config.set_verbose),
            enable_pre_call_checks=True,
        )
        main_model_group = llm_config.main_model_group

        async def llm_api_handler_with_router_and_fallback(
            prompt: str,
            step: Step | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """
            Custom LLM API handler that utilizes the LiteLLM router and fallbacks to OpenAI GPT-4 Vision.

            Args:
                prompt: The prompt to generate completions for.
                step: The step object associated with the prompt.
                screenshots: The screenshots associated with the prompt.
                parameters: Additional parameters to be passed to the LLM router.

            Returns:
                The response from the LLM router.
            """
            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters()

            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_PROMPT,
                    data=prompt.encode("utf-8"),
                )
                for screenshot in screenshots or []:
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=step,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_REQUEST,
                    data=json.dumps(
                        {
                            "model": llm_key,
                            "messages": messages,
                            **parameters,
                        }
                    ).encode("utf-8"),
                )
            try:
                LOG.info("Calling LLM API", llm_key=llm_key, model=llm_config.model_name)
                response = await router.acompletion(model=main_model_group, messages=messages, **parameters)
                LOG.info("LLM API call successful", llm_key=llm_key, model=llm_config.model_name)
            except openai.OpenAIError as e:
                raise LLMProviderError(llm_key) from e
            except Exception as e:
                LOG.exception(
                    "LLM request failed unexpectedly",
                    llm_key=llm_key,
                    model=main_model_group,
                )
                raise LLMProviderError(llm_key) from e

            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE,
                    data=response.model_dump_json(indent=2).encode("utf-8"),
                )
                llm_cost = litellm.completion_cost(completion_response=response)
                prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)
                completion_tokens = response.get("usage", {}).get("completion_tokens", 0)
                await app.DATABASE.update_step(
                    task_id=step.task_id,
                    step_id=step.step_id,
                    organization_id=step.organization_id,
                    incremental_cost=llm_cost,
                    incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                    incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                )
            parsed_response = parse_api_response(response, llm_config.add_assistant_prefix)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                    data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                )
            return parsed_response

        return llm_api_handler_with_router_and_fallback

    @staticmethod
    def get_llm_api_handler(llm_key: str, base_parameters: dict[str, Any] | None = None) -> LLMAPIHandler:
        llm_config = LLMConfigRegistry.get_config(llm_key)

        if LLMConfigRegistry.is_router_config(llm_key):
            return LLMAPIHandlerFactory.get_llm_api_handler_with_router(llm_key)

        async def llm_api_handler(
            prompt: str,
            step: Step | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            active_parameters = base_parameters or {}
            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters()

            active_parameters.update(parameters)

            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_PROMPT,
                    data=prompt.encode("utf-8"),
                )
                for screenshot in screenshots or []:
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=step,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            # TODO (kerem): instead of overriding the screenshots, should we just not take them in the first place?
            if not llm_config.supports_vision:
                screenshots = None

            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_REQUEST,
                    data=json.dumps(
                        {
                            "model": llm_config.model_name,
                            "messages": messages,
                            # we're not using active_parameters here because it may contain sensitive information
                            **parameters,
                        }
                    ).encode("utf-8"),
                )
            try:
                # TODO (kerem): add a timeout to this call
                # TODO (kerem): add a retry mechanism to this call (acompletion_with_retries)
                # TODO (kerem): use litellm fallbacks? https://litellm.vercel.app/docs/tutorials/fallbacks#how-does-completion_with_fallbacks-work
                LOG.info("Calling LLM API", llm_key=llm_key, model=llm_config.model_name)
                response = await litellm.acompletion(
                    model=llm_config.model_name,
                    messages=messages,
                    timeout=SettingsManager.get_settings().LLM_CONFIG_TIMEOUT,
                    **active_parameters,
                )
                LOG.info("LLM API call successful", llm_key=llm_key, model=llm_config.model_name)
            except openai.OpenAIError as e:
                raise LLMProviderError(llm_key) from e
            except Exception as e:
                LOG.exception("LLM request failed unexpectedly", llm_key=llm_key)
                raise LLMProviderError(llm_key) from e
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE,
                    data=response.model_dump_json(indent=2).encode("utf-8"),
                )
                llm_cost = litellm.completion_cost(completion_response=response)
                prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)
                completion_tokens = response.get("usage", {}).get("completion_tokens", 0)
                await app.DATABASE.update_step(
                    task_id=step.task_id,
                    step_id=step.step_id,
                    organization_id=step.organization_id,
                    incremental_cost=llm_cost,
                    incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                    incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                )
            parsed_response = parse_api_response(response, llm_config.add_assistant_prefix)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                    data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                )
            return parsed_response

        return llm_api_handler

    @staticmethod
    def get_api_parameters() -> dict[str, Any]:
        return {
            "max_tokens": SettingsManager.get_settings().LLM_CONFIG_MAX_TOKENS,
            "temperature": SettingsManager.get_settings().LLM_CONFIG_TEMPERATURE,
        }

    @classmethod
    def register_custom_handler(cls, llm_key: str, handler: LLMAPIHandler) -> None:
        if llm_key in cls._custom_handlers:
            raise DuplicateCustomLLMProviderError(llm_key)
        cls._custom_handlers[llm_key] = handler
