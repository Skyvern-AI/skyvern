import dataclasses
import json
import time
from asyncio import CancelledError
from typing import Any

import litellm
import structlog
from jinja2 import Template

from skyvern.config import settings
from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateCustomLLMProviderError,
    InvalidLLMConfigError,
    LLMProviderError,
    LLMProviderErrorRetryableTask,
)
from skyvern.forge.sdk.api.llm.models import LLMAPIHandler, LLMConfig, LLMRouterConfig, dummy_llm_api_handler
from skyvern.forge.sdk.api.llm.utils import llm_messages_builder, parse_api_response
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought

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
            disable_cooldowns=llm_config.disable_cooldowns,
            allowed_fails=llm_config.allowed_fails,
            allowed_fails_policy=llm_config.allowed_fails_policy,
            cooldown_time=llm_config.cooldown_time,
            set_verbose=(False if settings.is_cloud_environment() else llm_config.set_verbose),
            enable_pre_call_checks=True,
        )
        main_model_group = llm_config.main_model_group

        async def llm_api_handler_with_router_and_fallback(
            prompt: str,
            prompt_name: str,
            step: Step | None = None,
            task_v2: TaskV2 | None = None,
            thought: Thought | None = None,
            ai_suggestion: AISuggestion | None = None,
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
            start_time = time.time()

            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)

            context = skyvern_context.current()
            if context and len(context.hashed_href_map) > 0:
                await app.ARTIFACT_MANAGER.create_llm_artifact(
                    data=json.dumps(context.hashed_href_map, indent=2).encode("utf-8"),
                    artifact_type=ArtifactType.HASHED_HREF_MAP,
                    step=step,
                    task_v2=task_v2,
                    thought=thought,
                    ai_suggestion=ai_suggestion,
                )

            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=prompt.encode("utf-8"),
                artifact_type=ArtifactType.LLM_PROMPT,
                screenshots=screenshots,
                step=step,
                task_v2=task_v2,
                thought=thought,
            )
            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)

            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=json.dumps(
                    {
                        "model": llm_key,
                        "messages": messages,
                        **parameters,
                    }
                ).encode("utf-8"),
                artifact_type=ArtifactType.LLM_REQUEST,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )
            try:
                response = await router.acompletion(model=main_model_group, messages=messages, **parameters)
            except litellm.exceptions.APIError as e:
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except litellm.exceptions.ContextWindowExceededError as e:
                LOG.exception(
                    "Context window exceeded",
                    llm_key=llm_key,
                    model=main_model_group,
                )
                raise SkyvernContextWindowExceededError() from e
            except ValueError as e:
                LOG.exception(
                    "LLM token limit exceeded",
                    llm_key=llm_key,
                    model=main_model_group,
                )
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except Exception as e:
                LOG.exception(
                    "LLM request failed unexpectedly",
                    llm_key=llm_key,
                    model=main_model_group,
                )
                raise LLMProviderError(llm_key) from e

            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=response.model_dump_json(indent=2).encode("utf-8"),
                artifact_type=ArtifactType.LLM_RESPONSE,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )
            if step or thought:
                try:
                    llm_cost = litellm.completion_cost(completion_response=response)
                except Exception as e:
                    LOG.exception("Failed to calculate LLM cost", error=str(e))
                    llm_cost = 0
                prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)

                # TODO (suchintan): Properly support reasoning tokens
                reasoning_tokens = response.get("usage", {}).get("reasoning_tokens", 0)
                LOG.info("Reasoning tokens", reasoning_tokens=reasoning_tokens)

                completion_tokens = response.get("usage", {}).get("completion_tokens", 0) + reasoning_tokens

                if step:
                    await app.DATABASE.update_step(
                        task_id=step.task_id,
                        step_id=step.step_id,
                        organization_id=step.organization_id,
                        incremental_cost=llm_cost,
                        incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                        incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                    )
                if thought:
                    await app.DATABASE.update_thought(
                        thought_id=thought.observer_thought_id,
                        organization_id=thought.organization_id,
                        input_token_count=prompt_tokens if prompt_tokens > 0 else None,
                        output_token_count=completion_tokens if completion_tokens > 0 else None,
                        thought_cost=llm_cost,
                    )
            parsed_response = parse_api_response(response, llm_config.add_assistant_prefix)
            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )

            if context and len(context.hashed_href_map) > 0:
                llm_content = json.dumps(parsed_response)
                rendered_content = Template(llm_content).render(context.hashed_href_map)
                parsed_response = json.loads(rendered_content)
                await app.ARTIFACT_MANAGER.create_llm_artifact(
                    data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                    artifact_type=ArtifactType.LLM_RESPONSE_RENDERED,
                    step=step,
                    task_v2=task_v2,
                    thought=thought,
                    ai_suggestion=ai_suggestion,
                )

            # Track LLM API handler duration
            duration_seconds = time.time() - start_time
            LOG.info(
                "LLM API handler duration metrics",
                llm_key=llm_key,
                model=main_model_group,
                prompt_name=prompt_name,
                duration_seconds=duration_seconds,
                step_id=step.step_id if step else None,
                thought_id=thought.observer_thought_id if thought else None,
                organization_id=step.organization_id if step else (thought.organization_id if thought else None),
            )

            return parsed_response

        return llm_api_handler_with_router_and_fallback

    @staticmethod
    def get_llm_api_handler(llm_key: str, base_parameters: dict[str, Any] | None = None) -> LLMAPIHandler:
        try:
            llm_config = LLMConfigRegistry.get_config(llm_key)
        except InvalidLLMConfigError:
            return dummy_llm_api_handler

        if LLMConfigRegistry.is_router_config(llm_key):
            return LLMAPIHandlerFactory.get_llm_api_handler_with_router(llm_key)

        assert isinstance(llm_config, LLMConfig)

        async def llm_api_handler(
            prompt: str,
            prompt_name: str,
            step: Step | None = None,
            task_v2: TaskV2 | None = None,
            thought: Thought | None = None,
            ai_suggestion: AISuggestion | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            start_time = time.time()
            active_parameters = base_parameters or {}
            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)

            active_parameters.update(parameters)
            if llm_config.litellm_params:  # type: ignore
                active_parameters.update(llm_config.litellm_params)  # type: ignore

            context = skyvern_context.current()
            if context and len(context.hashed_href_map) > 0:
                await app.ARTIFACT_MANAGER.create_llm_artifact(
                    data=json.dumps(context.hashed_href_map, indent=2).encode("utf-8"),
                    artifact_type=ArtifactType.HASHED_HREF_MAP,
                    step=step,
                    task_v2=task_v2,
                    thought=thought,
                    ai_suggestion=ai_suggestion,
                )

            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=prompt.encode("utf-8"),
                artifact_type=ArtifactType.LLM_PROMPT,
                screenshots=screenshots,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )

            if not llm_config.supports_vision:
                screenshots = None

            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)
            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=json.dumps(
                    {
                        "model": llm_config.model_name,
                        "messages": messages,
                        # we're not using active_parameters here because it may contain sensitive information
                        **parameters,
                    }
                ).encode("utf-8"),
                artifact_type=ArtifactType.LLM_REQUEST,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )
            t_llm_request = time.perf_counter()
            try:
                # TODO (kerem): add a timeout to this call
                # TODO (kerem): add a retry mechanism to this call (acompletion_with_retries)
                # TODO (kerem): use litellm fallbacks? https://litellm.vercel.app/docs/tutorials/fallbacks#how-does-completion_with_fallbacks-work
                response = await litellm.acompletion(
                    model=llm_config.model_name,
                    messages=messages,
                    timeout=settings.LLM_CONFIG_TIMEOUT,
                    **active_parameters,
                )
            except litellm.exceptions.APIError as e:
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except litellm.exceptions.ContextWindowExceededError as e:
                LOG.exception(
                    "Context window exceeded",
                    llm_key=llm_key,
                    model=llm_config.model_name,
                )
                raise SkyvernContextWindowExceededError() from e
            except CancelledError:
                t_llm_cancelled = time.perf_counter()
                LOG.error(
                    "LLM request got cancelled",
                    llm_key=llm_key,
                    model=llm_config.model_name,
                    duration=t_llm_cancelled - t_llm_request,
                )
                raise LLMProviderError(llm_key)
            except Exception as e:
                LOG.exception("LLM request failed unexpectedly", llm_key=llm_key)
                raise LLMProviderError(llm_key) from e

            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=response.model_dump_json(indent=2).encode("utf-8"),
                artifact_type=ArtifactType.LLM_RESPONSE,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )

            if step or thought:
                try:
                    llm_cost = litellm.completion_cost(completion_response=response)
                except Exception as e:
                    LOG.exception("Failed to calculate LLM cost", error=str(e))
                    llm_cost = 0
                prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)
                completion_tokens = response.get("usage", {}).get("completion_tokens", 0)
                if step:
                    await app.DATABASE.update_step(
                        task_id=step.task_id,
                        step_id=step.step_id,
                        organization_id=step.organization_id,
                        incremental_cost=llm_cost,
                        incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                        incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                    )
                if thought:
                    await app.DATABASE.update_thought(
                        thought_id=thought.observer_thought_id,
                        organization_id=thought.organization_id,
                        input_token_count=prompt_tokens if prompt_tokens > 0 else None,
                        output_token_count=completion_tokens if completion_tokens > 0 else None,
                        thought_cost=llm_cost,
                    )
            parsed_response = parse_api_response(response, llm_config.add_assistant_prefix)
            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )

            if context and len(context.hashed_href_map) > 0:
                llm_content = json.dumps(parsed_response)
                rendered_content = Template(llm_content).render(context.hashed_href_map)
                parsed_response = json.loads(rendered_content)
                await app.ARTIFACT_MANAGER.create_llm_artifact(
                    data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                    artifact_type=ArtifactType.LLM_RESPONSE_RENDERED,
                    step=step,
                    task_v2=task_v2,
                    thought=thought,
                    ai_suggestion=ai_suggestion,
                )

            # Track LLM API handler duration
            duration_seconds = time.time() - start_time
            LOG.info(
                "LLM API handler duration metrics",
                llm_key=llm_key,
                prompt_name=prompt_name,
                model=llm_config.model_name,
                duration_seconds=duration_seconds,
                step_id=step.step_id if step else None,
                thought_id=thought.observer_thought_id if thought else None,
                organization_id=step.organization_id if step else (thought.organization_id if thought else None),
            )

            return parsed_response

        return llm_api_handler

    @staticmethod
    def get_api_parameters(llm_config: LLMConfig | LLMRouterConfig) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if llm_config.max_completion_tokens is not None:
            params["max_completion_tokens"] = llm_config.max_completion_tokens
        elif llm_config.max_tokens is not None:
            params["max_tokens"] = llm_config.max_tokens

        if llm_config.temperature is not None:
            params["temperature"] = llm_config.temperature

        if llm_config.reasoning_effort is not None:
            params["reasoning_effort"] = llm_config.reasoning_effort

        return params

    @classmethod
    def register_custom_handler(cls, llm_key: str, handler: LLMAPIHandler) -> None:
        if llm_key in cls._custom_handlers:
            raise DuplicateCustomLLMProviderError(llm_key)
        cls._custom_handlers[llm_key] = handler
