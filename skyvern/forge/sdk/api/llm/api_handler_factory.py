import dataclasses
import json
import time
from asyncio import CancelledError
from typing import Any, AsyncIterator

import litellm
import structlog
from anthropic import NOT_GIVEN
from anthropic.types.beta.beta_message import BetaMessage as AnthropicMessage
from jinja2 import Template
from litellm.utils import CustomStreamWrapper, ModelResponse
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from pydantic import BaseModel

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
from skyvern.forge.sdk.api.llm.ui_tars_response import UITarsResponse
from skyvern.forge.sdk.api.llm.utils import llm_messages_builder, llm_messages_builder_with_history, parse_api_response
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.trace import TraceManager
from skyvern.utils.image_resizer import Resolution, get_resize_target_dimension, resize_screenshots

LOG = structlog.get_logger()


class LLMCallStats(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    llm_cost: float | None = None


class LLMAPIHandlerFactory:
    _custom_handlers: dict[str, LLMAPIHandler] = {}
    _thinking_budget_settings: dict[str, int] | None = None
    _prompt_caching_settings: dict[str, bool] | None = None

    @staticmethod
    def _apply_thinking_budget_optimization(
        parameters: dict[str, Any], new_budget: int, llm_config: LLMConfig | LLMRouterConfig, prompt_name: str
    ) -> None:
        """Apply thinking budget optimization based on model type and LiteLLM reasoning support."""
        # Compute a safe model label and a representative model for capability checks
        model_label = getattr(llm_config, "model_name", None)
        if model_label is None and isinstance(llm_config, LLMRouterConfig):
            model_label = getattr(llm_config, "main_model_group", "router")
        check_model = model_label
        if isinstance(llm_config, LLMRouterConfig) and getattr(llm_config, "model_list", None):
            try:
                check_model = llm_config.model_list[0].model_name or model_label  # type: ignore[attr-defined]
            except Exception:
                check_model = model_label
        try:
            # Early return if model doesn't support reasoning
            if check_model and not litellm.supports_reasoning(model=check_model):
                LOG.info(
                    "Thinking budget optimization not supported for model",
                    prompt_name=prompt_name,
                    budget=new_budget,
                    model=model_label,
                )
                return

            # Apply optimization based on model type
            model_label_lower = (model_label or "").lower()
            if "gemini" in model_label_lower:
                # Gemini models use the exact integer budget value
                LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
                    parameters, new_budget, llm_config, prompt_name
                )
            elif "anthropic" in model_label_lower or "claude" in model_label_lower:
                # Anthropic/Claude models use "low" for all budget values (per LiteLLM constants)
                LLMAPIHandlerFactory._apply_anthropic_thinking_optimization(
                    parameters, new_budget, llm_config, prompt_name
                )
            else:
                # Other reasoning-capable models (Deepseek, etc.) - use "low" for all budget values
                parameters["reasoning_effort"] = "low"
                LOG.info(
                    "Applied thinking budget optimization (reasoning_effort)",
                    prompt_name=prompt_name,
                    budget=new_budget,
                    reasoning_effort="low",
                    model=model_label,
                )

        except (AttributeError, KeyError, TypeError) as e:
            LOG.warning(
                "Failed to apply thinking budget optimization",
                prompt_name=prompt_name,
                budget=new_budget,
                model=model_label,
                error=str(e),
                exc_info=True,
            )

    @staticmethod
    def _apply_anthropic_thinking_optimization(
        parameters: dict[str, Any], new_budget: int, llm_config: LLMConfig | LLMRouterConfig, prompt_name: str
    ) -> None:
        """Apply thinking optimization for Anthropic/Claude models."""
        if llm_config.reasoning_effort is not None:
            # Use reasoning_effort if configured in LLM config - always use "low" per LiteLLM constants
            parameters["reasoning_effort"] = "low"
            # Get safe model label for logging
            model_label = getattr(llm_config, "model_name", None)
            if model_label is None and isinstance(llm_config, LLMRouterConfig):
                model_label = getattr(llm_config, "main_model_group", "router")

            LOG.info(
                "Applied thinking budget optimization (reasoning_effort)",
                prompt_name=prompt_name,
                budget=new_budget,
                reasoning_effort="low",
                model=model_label,
            )
        else:
            # Use thinking parameter with budget_tokens for Anthropic models
            if "thinking" in parameters and isinstance(parameters["thinking"], dict):
                parameters["thinking"]["budget_tokens"] = new_budget
            else:
                parameters["thinking"] = {"budget_tokens": new_budget, "type": "enabled"}
            # Get safe model label for logging
            model_label = getattr(llm_config, "model_name", None)
            if model_label is None and isinstance(llm_config, LLMRouterConfig):
                model_label = getattr(llm_config, "main_model_group", "router")

            LOG.info(
                "Applied thinking budget optimization (thinking)",
                prompt_name=prompt_name,
                budget=new_budget,
                model=model_label,
            )

    @staticmethod
    def _apply_gemini_thinking_optimization(
        parameters: dict[str, Any], new_budget: int, llm_config: LLMConfig | LLMRouterConfig, prompt_name: str
    ) -> None:
        """Apply thinking optimization for Gemini models using exact integer budget value."""
        if "thinking" in parameters and isinstance(parameters["thinking"], dict):
            parameters["thinking"]["budget_tokens"] = new_budget
        else:
            thinking_payload: dict[str, Any] = {"budget_tokens": new_budget}
            if settings.GEMINI_INCLUDE_THOUGHT:
                thinking_payload["type"] = "enabled"
            parameters["thinking"] = thinking_payload
        # Get safe model label for logging
        model_label = getattr(llm_config, "model_name", None)
        if model_label is None and isinstance(llm_config, LLMRouterConfig):
            model_label = getattr(llm_config, "main_model_group", "router")

        LOG.info(
            "Applied thinking budget optimization (budget_tokens)",
            prompt_name=prompt_name,
            budget=new_budget,
            model=model_label,
        )

    @staticmethod
    def get_override_llm_api_handler(override_llm_key: str | None, *, default: LLMAPIHandler) -> LLMAPIHandler:
        if not override_llm_key:
            return default
        try:
            return LLMAPIHandlerFactory.get_llm_api_handler(override_llm_key)
        except Exception:
            LOG.warning(
                "Failed to get override LLM API handler, going to use the default.",
                override_llm_key=override_llm_key,
                exc_info=True,
            )
            return default

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

        @TraceManager.traced_async(tags=[llm_key], ignore_inputs=["prompt", "screenshots", "parameters"])
        async def llm_api_handler_with_router_and_fallback(
            prompt: str,
            prompt_name: str,
            step: Step | None = None,
            task_v2: TaskV2 | None = None,
            thought: Thought | None = None,
            ai_suggestion: AISuggestion | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
            organization_id: str | None = None,
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

            # Apply thinking budget optimization if settings are available
            if (
                LLMAPIHandlerFactory._thinking_budget_settings
                and prompt_name in LLMAPIHandlerFactory._thinking_budget_settings
            ):
                new_budget = LLMAPIHandlerFactory._thinking_budget_settings[prompt_name]
                LLMAPIHandlerFactory._apply_thinking_budget_optimization(
                    parameters, new_budget, llm_config, prompt_name
                )

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
            # Build messages and apply caching in one step
            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)

            # Inject context caching system message when available
            try:
                context_cached_static_prompt = getattr(context, "cached_static_prompt", None)
                if (
                    context_cached_static_prompt
                    and isinstance(llm_config, LLMConfig)
                    and isinstance(llm_config.model_name, str)
                ):
                    # Check if this is a Vertex AI model
                    if "vertex_ai/" in llm_config.model_name:
                        caching_system_message = {
                            "role": "system",
                            "content": [
                                {
                                    "type": "text",
                                    "text": context_cached_static_prompt,
                                    "cache_control": {"type": "ephemeral", "ttl": "3600s"},
                                }
                            ],
                        }
                        messages = [caching_system_message] + messages
                        LOG.info(
                            "Applied Vertex context caching",
                            prompt_name=prompt_name,
                            model=llm_config.model_name,
                            ttl_seconds=3600,
                        )
                    # Check if this is an OpenAI model
                    elif (
                        llm_config.model_name.startswith("gpt-")
                        or llm_config.model_name.startswith("o1-")
                        or llm_config.model_name.startswith("o3-")
                    ):
                        # For OpenAI models, we need to add the cached content as a system message
                        # and mark it for caching using the cache_control parameter
                        caching_system_message = {
                            "role": "system",
                            "content": [
                                {
                                    "type": "text",
                                    "text": context_cached_static_prompt,
                                }
                            ],
                        }
                        messages = [caching_system_message] + messages
                        LOG.info(
                            "Applied OpenAI context caching",
                            prompt_name=prompt_name,
                            model=llm_config.model_name,
                        )
            except Exception as e:
                LOG.warning("Failed to apply context caching system message", error=str(e), exc_info=True)

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
                response = await router.acompletion(
                    model=main_model_group, messages=messages, timeout=settings.LLM_CONFIG_TIMEOUT, **parameters
                )
            except litellm.exceptions.APIError as e:
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except litellm.exceptions.ContextWindowExceededError as e:
                duration_seconds = time.time() - start_time
                LOG.exception(
                    "Context window exceeded",
                    llm_key=llm_key,
                    model=main_model_group,
                    prompt_name=prompt_name,
                    duration_seconds=duration_seconds,
                )
                raise SkyvernContextWindowExceededError() from e
            except ValueError as e:
                duration_seconds = time.time() - start_time
                LOG.exception(
                    "LLM token limit exceeded",
                    llm_key=llm_key,
                    model=main_model_group,
                    prompt_name=prompt_name,
                    duration_seconds=duration_seconds,
                )
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except Exception as e:
                duration_seconds = time.time() - start_time
                LOG.exception(
                    "LLM request failed unexpectedly",
                    llm_key=llm_key,
                    model=main_model_group,
                    prompt_name=prompt_name,
                    duration_seconds=duration_seconds,
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
            prompt_tokens = 0
            completion_tokens = 0
            reasoning_tokens = 0
            cached_tokens = 0
            completion_token_detail = None
            cached_token_detail = None
            try:
                # FIXME: volcengine doesn't support litellm cost calculation.
                llm_cost = litellm.completion_cost(completion_response=response)
            except Exception as e:
                LOG.info("Failed to calculate LLM cost", error=str(e), exc_info=True)
                llm_cost = 0
            prompt_tokens = 0
            completion_tokens = 0
            reasoning_tokens = 0
            cached_tokens = 0

            if hasattr(response, "usage") and response.usage:
                prompt_tokens = getattr(response.usage, "prompt_tokens", 0)
                completion_tokens = getattr(response.usage, "completion_tokens", 0)

                # Extract reasoning tokens from completion_tokens_details
                completion_token_detail = getattr(response.usage, "completion_tokens_details", None)
                if completion_token_detail:
                    reasoning_tokens = getattr(completion_token_detail, "reasoning_tokens", 0) or 0

                # Extract cached tokens from prompt_tokens_details
                cached_token_detail = getattr(response.usage, "prompt_tokens_details", None)
                if cached_token_detail:
                    cached_tokens = getattr(cached_token_detail, "cached_tokens", 0) or 0

                # Fallback for Vertex/Gemini: LiteLLM exposes cache_read_input_tokens on usage
                if cached_tokens == 0:
                    cached_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            if step:
                await app.DATABASE.update_step(
                    task_id=step.task_id,
                    step_id=step.step_id,
                    organization_id=step.organization_id,
                    incremental_cost=llm_cost,
                    incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                    incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                    incremental_reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
                    incremental_cached_tokens=cached_tokens if cached_tokens > 0 else None,
                )
            if thought:
                await app.DATABASE.update_thought(
                    thought_id=thought.observer_thought_id,
                    organization_id=thought.organization_id,
                    input_token_count=prompt_tokens if prompt_tokens > 0 else None,
                    output_token_count=completion_tokens if completion_tokens > 0 else None,
                    thought_cost=llm_cost,
                    reasoning_token_count=reasoning_tokens if reasoning_tokens > 0 else None,
                    cached_token_count=cached_tokens if cached_tokens > 0 else None,
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

            # Track LLM API handler duration, token counts, and cost
            organization_id = organization_id or (
                step.organization_id if step else (thought.organization_id if thought else None)
            )
            duration_seconds = time.time() - start_time
            LOG.info(
                "LLM API handler duration metrics",
                llm_key=llm_key,
                model=main_model_group,
                prompt_name=prompt_name,
                duration_seconds=duration_seconds,
                step_id=step.step_id if step else None,
                thought_id=thought.observer_thought_id if thought else None,
                organization_id=organization_id,
                input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                output_tokens=completion_tokens if completion_tokens > 0 else None,
                reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
                cached_tokens=cached_tokens if cached_tokens > 0 else None,
                llm_cost=llm_cost if llm_cost > 0 else None,
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

        @TraceManager.traced_async(tags=[llm_key], ignore_inputs=["prompt", "screenshots", "parameters"])
        async def llm_api_handler(
            prompt: str,
            prompt_name: str,
            step: Step | None = None,
            task_v2: TaskV2 | None = None,
            thought: Thought | None = None,
            ai_suggestion: AISuggestion | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
            organization_id: str | None = None,
        ) -> dict[str, Any]:
            start_time = time.time()
            active_parameters = base_parameters or {}
            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)

            active_parameters.update(parameters)
            if llm_config.litellm_params:  # type: ignore
                active_parameters.update(llm_config.litellm_params)  # type: ignore

            # Apply thinking budget optimization if settings are available
            if (
                LLMAPIHandlerFactory._thinking_budget_settings
                and prompt_name in LLMAPIHandlerFactory._thinking_budget_settings
            ):
                new_budget = LLMAPIHandlerFactory._thinking_budget_settings[prompt_name]
                LLMAPIHandlerFactory._apply_thinking_budget_optimization(
                    active_parameters, new_budget, llm_config, prompt_name
                )

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

            model_name = llm_config.model_name

            messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)

            # Inject context caching system message when available
            try:
                context_cached_static_prompt = getattr(context, "cached_static_prompt", None)
                if (
                    context_cached_static_prompt
                    and isinstance(llm_config, LLMConfig)
                    and isinstance(llm_config.model_name, str)
                ):
                    # Check if this is a Vertex AI model
                    if "vertex_ai/" in llm_config.model_name:
                        caching_system_message = {
                            "role": "system",
                            "content": [
                                {
                                    "type": "text",
                                    "text": context_cached_static_prompt,
                                    "cache_control": {"type": "ephemeral", "ttl": "3600s"},
                                }
                            ],
                        }
                        messages = [caching_system_message] + messages
                        LOG.info(
                            "Applied Vertex context caching",
                            prompt_name=prompt_name,
                            model=llm_config.model_name,
                            ttl_seconds=3600,
                        )
                    # Check if this is an OpenAI model
                    elif (
                        llm_config.model_name.startswith("gpt-")
                        or llm_config.model_name.startswith("o1-")
                        or llm_config.model_name.startswith("o3-")
                    ):
                        # For OpenAI models, we need to add the cached content as a system message
                        # and mark it for caching using the cache_control parameter
                        caching_system_message = {
                            "role": "system",
                            "content": [
                                {
                                    "type": "text",
                                    "text": context_cached_static_prompt,
                                }
                            ],
                        }
                        messages = [caching_system_message] + messages
                        LOG.info(
                            "Applied OpenAI context caching",
                            prompt_name=prompt_name,
                            model=llm_config.model_name,
                        )
            except Exception as e:
                LOG.warning("Failed to apply context caching system message", error=str(e), exc_info=True)
            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=json.dumps(
                    {
                        "model": model_name,
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
                    model=model_name,
                    messages=messages,
                    timeout=settings.LLM_CONFIG_TIMEOUT,
                    drop_params=True,  # Drop unsupported parameters gracefully
                    **active_parameters,
                )
            except litellm.exceptions.APIError as e:
                raise LLMProviderErrorRetryableTask(llm_key) from e
            except litellm.exceptions.ContextWindowExceededError as e:
                duration_seconds = time.time() - start_time
                LOG.exception(
                    "Context window exceeded",
                    llm_key=llm_key,
                    model=model_name,
                    prompt_name=prompt_name,
                    duration_seconds=duration_seconds,
                )
                raise SkyvernContextWindowExceededError() from e
            except CancelledError:
                t_llm_cancelled = time.perf_counter()
                LOG.error(
                    "LLM request got cancelled",
                    llm_key=llm_key,
                    model=model_name,
                    prompt_name=prompt_name,
                    duration=t_llm_cancelled - t_llm_request,
                )
                raise LLMProviderError(llm_key)
            except Exception as e:
                duration_seconds = time.time() - start_time
                LOG.exception(
                    "LLM request failed unexpectedly",
                    llm_key=llm_key,
                    model=model_name,
                    prompt_name=prompt_name,
                    duration_seconds=duration_seconds,
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

            prompt_tokens = 0
            completion_tokens = 0
            reasoning_tokens = 0
            cached_tokens = 0
            completion_token_detail = None
            cached_token_detail = None
            try:
                # FIXME: volcengine doesn't support litellm cost calculation.
                llm_cost = litellm.completion_cost(completion_response=response)
            except Exception as e:
                LOG.info("Failed to calculate LLM cost", error=str(e), exc_info=True)
                llm_cost = 0
            prompt_tokens = 0
            completion_tokens = 0
            reasoning_tokens = 0
            cached_tokens = 0

            if hasattr(response, "usage") and response.usage:
                prompt_tokens = getattr(response.usage, "prompt_tokens", 0)
                completion_tokens = getattr(response.usage, "completion_tokens", 0)

                # Extract reasoning tokens from completion_tokens_details
                completion_token_detail = getattr(response.usage, "completion_tokens_details", None)
                if completion_token_detail:
                    reasoning_tokens = getattr(completion_token_detail, "reasoning_tokens", 0) or 0

                # Extract cached tokens from prompt_tokens_details
                cached_token_detail = getattr(response.usage, "prompt_tokens_details", None)
                if cached_token_detail:
                    cached_tokens = getattr(cached_token_detail, "cached_tokens", 0) or 0

                # Fallback for Vertex/Gemini: LiteLLM exposes cache_read_input_tokens on usage
                if cached_tokens == 0:
                    cached_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0

            if step:
                await app.DATABASE.update_step(
                    task_id=step.task_id,
                    step_id=step.step_id,
                    organization_id=step.organization_id,
                    incremental_cost=llm_cost,
                    incremental_input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                    incremental_output_tokens=completion_tokens if completion_tokens > 0 else None,
                    incremental_reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
                    incremental_cached_tokens=cached_tokens if cached_tokens > 0 else None,
                )
            if thought:
                await app.DATABASE.update_thought(
                    thought_id=thought.observer_thought_id,
                    organization_id=thought.organization_id,
                    input_token_count=prompt_tokens if prompt_tokens > 0 else None,
                    output_token_count=completion_tokens if completion_tokens > 0 else None,
                    reasoning_token_count=reasoning_tokens if reasoning_tokens > 0 else None,
                    cached_token_count=cached_tokens if cached_tokens > 0 else None,
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

            # Track LLM API handler duration, token counts, and cost
            organization_id = organization_id or (
                step.organization_id if step else (thought.organization_id if thought else None)
            )
            duration_seconds = time.time() - start_time
            LOG.info(
                "LLM API handler duration metrics",
                llm_key=llm_key,
                prompt_name=prompt_name,
                model=llm_config.model_name,
                duration_seconds=duration_seconds,
                step_id=step.step_id if step else None,
                thought_id=thought.observer_thought_id if thought else None,
                organization_id=organization_id,
                input_tokens=prompt_tokens if prompt_tokens > 0 else None,
                output_tokens=completion_tokens if completion_tokens > 0 else None,
                reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
                cached_tokens=cached_tokens if cached_tokens > 0 else None,
                llm_cost=llm_cost if llm_cost > 0 else None,
            )

            return parsed_response

        return llm_api_handler

    @staticmethod
    def get_api_parameters(llm_config: LLMConfig | LLMRouterConfig) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if not llm_config.model_name.startswith("ollama/"):
            # OLLAMA does not support max_completion_tokens
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

    @classmethod
    def set_thinking_budget_settings(cls, settings: dict[str, int] | None) -> None:
        """Set thinking budget optimization settings for the current task/workflow."""
        cls._thinking_budget_settings = settings
        if settings:
            LOG.info("Thinking budget optimization settings applied", settings=settings)

    @classmethod
    def set_prompt_caching_settings(cls, settings: dict[str, bool] | None) -> None:
        """Set prompt caching optimization settings for the current task/workflow."""
        cls._prompt_caching_settings = settings
        if settings:
            LOG.info("Prompt caching optimization settings applied", settings=settings)


class LLMCaller:
    """
    An LLMCaller instance defines the LLM configs and keeps the chat history if needed.
    """

    def __init__(
        self,
        llm_key: str,
        screenshot_scaling_enabled: bool = False,
        base_parameters: dict[str, Any] | None = None,
    ):
        self.llm_key = llm_key
        self.llm_config = LLMConfigRegistry.get_config(llm_key)
        self.base_parameters = base_parameters
        self.message_history: list[dict[str, Any]] = []
        self.current_tool_results: list[dict[str, Any]] = []
        self.screenshot_scaling_enabled = screenshot_scaling_enabled
        self.browser_window_dimension = Resolution(width=settings.BROWSER_WIDTH, height=settings.BROWSER_HEIGHT)
        self.screenshot_resize_target_dimension = self.browser_window_dimension
        if screenshot_scaling_enabled:
            self.screenshot_resize_target_dimension = get_resize_target_dimension(self.browser_window_dimension)

    def add_tool_result(self, tool_result: dict[str, Any]) -> None:
        self.current_tool_results.append(tool_result)

    def clear_tool_results(self) -> None:
        self.current_tool_results = []

    async def call(
        self,
        prompt: str | None = None,
        prompt_name: str | None = None,
        step: Step | None = None,
        task_v2: TaskV2 | None = None,
        thought: Thought | None = None,
        ai_suggestion: AISuggestion | None = None,
        screenshots: list[bytes] | None = None,
        parameters: dict[str, Any] | None = None,
        tools: list | None = None,
        use_message_history: bool = False,
        raw_response: bool = False,
        window_dimension: Resolution | None = None,
        organization_id: str | None = None,
        **extra_parameters: Any,
    ) -> dict[str, Any]:
        start_time = time.perf_counter()
        active_parameters = self.base_parameters or {}
        if parameters is None:
            parameters = LLMAPIHandlerFactory.get_api_parameters(self.llm_config)

        active_parameters.update(parameters)
        if extra_parameters:
            active_parameters.update(extra_parameters)
        if self.llm_config.litellm_params:  # type: ignore
            active_parameters.update(self.llm_config.litellm_params)  # type: ignore

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

        if screenshots and self.screenshot_scaling_enabled:
            target_dimension = self.get_screenshot_resize_target_dimension(window_dimension)
            if window_dimension and window_dimension != self.browser_window_dimension and tools:
                # THIS situation only applies to Anthropic CUA
                LOG.info(
                    "Window dimension is different from the default browser window dimension when making LLM call",
                    window_dimension=window_dimension,
                    browser_window_dimension=self.browser_window_dimension,
                )
                # update the tools to use the new target dimension
                for tool in tools:
                    if "display_height_px" in tool:
                        tool["display_height_px"] = target_dimension["height"]
                    if "display_width_px" in tool:
                        tool["display_width_px"] = target_dimension["width"]
            screenshots = resize_screenshots(screenshots, target_dimension)

        if prompt:
            await app.ARTIFACT_MANAGER.create_llm_artifact(
                data=prompt.encode("utf-8"),
                artifact_type=ArtifactType.LLM_PROMPT,
                screenshots=screenshots,
                step=step,
                task_v2=task_v2,
                thought=thought,
                ai_suggestion=ai_suggestion,
            )

        if not self.llm_config.supports_vision:
            screenshots = None

        message_pattern = "openai"
        if "ANTHROPIC" in self.llm_key:
            message_pattern = "anthropic"

        if use_message_history:
            # self.message_history will be updated in place
            messages = await llm_messages_builder_with_history(
                prompt,
                screenshots,
                self.message_history,
                message_pattern=message_pattern,
            )
        else:
            messages = await llm_messages_builder_with_history(
                prompt,
                screenshots,
                message_pattern=message_pattern,
            )
        await app.ARTIFACT_MANAGER.create_llm_artifact(
            data=json.dumps(
                {
                    "model": self.llm_config.model_name,
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
            response = await self._dispatch_llm_call(
                messages=messages,
                tools=tools,
                timeout=settings.LLM_CONFIG_TIMEOUT,
                **active_parameters,
            )
            if use_message_history:
                # only update message_history when the request is successful
                self.message_history = messages
        except litellm.exceptions.APIError as e:
            raise LLMProviderErrorRetryableTask(self.llm_key) from e
        except litellm.exceptions.ContextWindowExceededError as e:
            LOG.exception(
                "Context window exceeded",
                llm_key=self.llm_key,
                model=self.llm_config.model_name,
            )
            raise SkyvernContextWindowExceededError() from e
        except CancelledError:
            t_llm_cancelled = time.perf_counter()
            LOG.error(
                "LLM request got cancelled",
                llm_key=self.llm_key,
                model=self.llm_config.model_name,
                duration=t_llm_cancelled - t_llm_request,
            )
            raise LLMProviderError(self.llm_key)
        except Exception as e:
            LOG.exception("LLM request failed unexpectedly", llm_key=self.llm_key)
            raise LLMProviderError(self.llm_key) from e

        await app.ARTIFACT_MANAGER.create_llm_artifact(
            data=response.model_dump_json(indent=2).encode("utf-8"),
            artifact_type=ArtifactType.LLM_RESPONSE,
            step=step,
            task_v2=task_v2,
            thought=thought,
            ai_suggestion=ai_suggestion,
        )

        call_stats = await self.get_call_stats(response)
        if step:
            await app.DATABASE.update_step(
                task_id=step.task_id,
                step_id=step.step_id,
                organization_id=step.organization_id,
                incremental_cost=call_stats.llm_cost,
                incremental_input_tokens=call_stats.input_tokens,
                incremental_output_tokens=call_stats.output_tokens,
                incremental_reasoning_tokens=call_stats.reasoning_tokens,
                incremental_cached_tokens=call_stats.cached_tokens,
            )
        if thought:
            await app.DATABASE.update_thought(
                thought_id=thought.observer_thought_id,
                organization_id=thought.organization_id,
                input_token_count=call_stats.input_tokens,
                output_token_count=call_stats.output_tokens,
                reasoning_token_count=call_stats.reasoning_tokens,
                cached_token_count=call_stats.cached_tokens,
                thought_cost=call_stats.llm_cost,
            )

        organization_id = organization_id or (
            step.organization_id if step else (thought.organization_id if thought else None)
        )
        # Track LLM API handler duration, token counts, and cost
        duration_seconds = time.perf_counter() - start_time
        LOG.info(
            "LLM API handler duration metrics",
            llm_key=self.llm_key,
            prompt_name=prompt_name,
            model=self.llm_config.model_name,
            duration_seconds=duration_seconds,
            step_id=step.step_id if step else None,
            thought_id=thought.observer_thought_id if thought else None,
            organization_id=organization_id,
            input_tokens=call_stats.input_tokens if call_stats and call_stats.input_tokens else None,
            output_tokens=call_stats.output_tokens if call_stats and call_stats.output_tokens else None,
            reasoning_tokens=call_stats.reasoning_tokens if call_stats and call_stats.reasoning_tokens else None,
            cached_tokens=call_stats.cached_tokens if call_stats and call_stats.cached_tokens else None,
            llm_cost=call_stats.llm_cost if call_stats and call_stats.llm_cost else None,
        )
        if raw_response:
            return response.model_dump(exclude_none=True)

        parsed_response = parse_api_response(response, self.llm_config.add_assistant_prefix)
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

        return parsed_response

    def get_screenshot_resize_target_dimension(self, window_dimension: Resolution | None) -> Resolution:
        if window_dimension and window_dimension != self.browser_window_dimension:
            return get_resize_target_dimension(window_dimension)
        return self.screenshot_resize_target_dimension

    @TraceManager.traced_async(ignore_input=True)
    async def _dispatch_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list | None = None,
        timeout: float = settings.LLM_CONFIG_TIMEOUT,
        **active_parameters: dict[str, Any],
    ) -> ModelResponse | CustomStreamWrapper | AnthropicMessage | UITarsResponse:
        if self.llm_key and "ANTHROPIC" in self.llm_key:
            return await self._call_anthropic(messages, tools, timeout, **active_parameters)

        # Route UI-TARS models to custom handler instead of LiteLLM
        if self.llm_key and "UI_TARS" in self.llm_key:
            return await self._call_ui_tars(messages, tools, timeout, **active_parameters)

        return await litellm.acompletion(
            model=self.llm_config.model_name,
            messages=messages,
            tools=tools,
            timeout=timeout,
            drop_params=True,  # Drop unsupported parameters gracefully
            **active_parameters,
        )

    async def _call_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list | None = None,
        timeout: float = settings.LLM_CONFIG_TIMEOUT,
        **active_parameters: dict[str, Any],
    ) -> AnthropicMessage:
        max_tokens = active_parameters.get("max_completion_tokens") or active_parameters.get("max_tokens") or 4096
        model_name = self.llm_config.model_name.replace("bedrock/", "").replace("anthropic/", "")
        betas = active_parameters.get("betas", NOT_GIVEN)
        thinking = active_parameters.get("thinking", NOT_GIVEN)
        LOG.info(
            "Anthropic request",
            model_name=model_name,
            betas=betas,
            tools=tools,
            timeout=timeout,
            messages_length=len(messages),
        )
        response = await app.ANTHROPIC_CLIENT.beta.messages.create(
            max_tokens=max_tokens,
            messages=messages,
            model=model_name,
            tools=tools or NOT_GIVEN,
            timeout=timeout,
            betas=betas,
            thinking=thinking,
        )
        LOG.info(
            "Anthropic response",
            model_name=model_name,
            response=response,
            betas=betas,
            tools=tools,
            timeout=timeout,
        )
        return response

    async def _call_ui_tars(
        self,
        messages: list[dict[str, Any]],
        tools: list | None = None,
        timeout: float = settings.LLM_CONFIG_TIMEOUT,
        **active_parameters: dict[str, Any],
    ) -> UITarsResponse:
        """Custom UI-TARS API call using OpenAI client with VolcEngine endpoint."""
        max_tokens = active_parameters.get("max_completion_tokens") or active_parameters.get("max_tokens") or 400
        model_name = self.llm_config.model_name.replace("volcengine/", "")

        if not app.UI_TARS_CLIENT:
            raise ValueError(
                "UI_TARS_CLIENT not initialized. Please ensure ENABLE_VOLCENGINE=true and VOLCENGINE_API_KEY is set."
            )

        LOG.info(
            "UI-TARS request",
            model_name=model_name,
            timeout=timeout,
            messages_length=len(messages),
        )

        # Use the UI-TARS client (which is OpenAI-compatible with VolcEngine)
        chat_completion: AsyncIterator[ChatCompletionChunk] = await app.UI_TARS_CLIENT.chat.completions.create(
            model=model_name,
            messages=messages,
            top_p=None,
            temperature=active_parameters.get("temperature", 0.0),
            max_tokens=max_tokens,
            stream=True,
            seed=None,
            stop=None,
            frequency_penalty=None,
            presence_penalty=None,
            timeout=timeout,
        )

        # Aggregate streaming response like in ByteDance example
        response_content = ""
        async for message in chat_completion:
            if message.choices[0].delta.content:
                response_content += message.choices[0].delta.content

        response = UITarsResponse(response_content, model_name)

        LOG.info(
            "UI-TARS response",
            model_name=model_name,
            response_length=len(response_content),
            timeout=timeout,
        )
        return response

    async def get_call_stats(
        self, response: ModelResponse | CustomStreamWrapper | AnthropicMessage | UITarsResponse
    ) -> LLMCallStats:
        empty_call_stats = LLMCallStats()

        # Handle UI-TARS response (UITarsResponse object from _call_ui_tars)
        if isinstance(response, UITarsResponse):
            ui_tars_usage = response.usage
            return LLMCallStats(
                llm_cost=0,  # TODO: calculate the cost according to the price: https://www.volcengine.com/docs/82379/1544106
                input_tokens=ui_tars_usage.get("prompt_tokens", 0),
                output_tokens=ui_tars_usage.get("completion_tokens", 0),
                cached_tokens=0,  # only part of model support cached tokens
                reasoning_tokens=0,
            )

        if isinstance(response, AnthropicMessage):
            usage = response.usage
            input_token_cost = (3.0 / 1000000) * usage.input_tokens
            output_token_cost = (15.0 / 1000000) * usage.output_tokens
            cached_token_cost = (0.3 / 1000000) * usage.cache_read_input_tokens
            llm_cost = input_token_cost + output_token_cost + cached_token_cost
            return LLMCallStats(
                llm_cost=llm_cost,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cache_read_input_tokens,
                reasoning_tokens=0,
            )
        elif isinstance(response, (ModelResponse, CustomStreamWrapper)):
            try:
                llm_cost = litellm.completion_cost(completion_response=response)
            except Exception as e:
                LOG.info("Failed to calculate LLM cost", error=str(e), exc_info=True)
                llm_cost = 0
            input_tokens = 0
            output_tokens = 0
            reasoning_tokens = 0
            cached_tokens = 0

            if hasattr(response, "usage") and response.usage:
                input_tokens = getattr(response.usage, "prompt_tokens", 0)
                output_tokens = getattr(response.usage, "completion_tokens", 0)

                # Extract reasoning tokens from completion_tokens_details
                completion_token_detail = getattr(response.usage, "completion_tokens_details", None)
                if completion_token_detail:
                    reasoning_tokens = getattr(completion_token_detail, "reasoning_tokens", 0) or 0

                # Extract cached tokens from prompt_tokens_details
                cached_token_detail = getattr(response.usage, "prompt_tokens_details", None)
                if cached_token_detail:
                    cached_tokens = getattr(cached_token_detail, "cached_tokens", 0) or 0

                # Fallback for Vertex/Gemini: LiteLLM exposes cache_read_input_tokens on usage
                if cached_tokens == 0:
                    cached_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            return LLMCallStats(
                llm_cost=llm_cost,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        return empty_call_stats


class LLMCallerManager:
    _llm_callers: dict[str, LLMCaller] = {}

    @classmethod
    def get_llm_caller(cls, uid: str) -> LLMCaller | None:
        return cls._llm_callers.get(uid)

    @classmethod
    def set_llm_caller(cls, uid: str, llm_caller: LLMCaller) -> None:
        cls._llm_callers[uid] = llm_caller

    @classmethod
    def clear_llm_caller(cls, uid: str) -> None:
        if uid in cls._llm_callers:
            del cls._llm_callers[uid]
