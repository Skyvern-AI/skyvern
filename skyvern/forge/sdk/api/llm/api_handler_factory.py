import dataclasses
import json
import time
from asyncio import CancelledError
from typing import Any

import litellm
import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateCustomLLMProviderError,
    InvalidLLMConfigError,
    LLMProviderError,
    LLMProviderErrorRetryableTask,
)
from skyvern.forge.sdk.api.llm.models import LLMAPIHandler, LLMConfig, LLMRouterConfig
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
            disable_cooldowns=llm_config.disable_cooldowns,
            allowed_fails=llm_config.allowed_fails,
            allowed_fails_policy=llm_config.allowed_fails_policy,
            cooldown_time=llm_config.cooldown_time,
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
            except litellm.exceptions.APIError as e:
                raise LLMProviderErrorRetryableTask(llm_key) from e
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

        assert isinstance(llm_config, LLMConfig)

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
            if llm_config.litellm_params:
                active_parameters.update(llm_config.litellm_params)

            # Get timeout and max_retries from settings
            timeout = active_parameters.pop('timeout', SettingsManager.get_settings().LLM_CONFIG_TIMEOUT)
            max_retries = active_parameters.pop('max_retries', 3)

            # Handle Ollama-specific options
            ollama_options = {
                "num_ctx": SettingsManager.get_settings().OLLAMA_CONTEXT_WINDOW,
                "temperature": SettingsManager.get_settings().LLM_CONFIG_TEMPERATURE,
                "num_predict": SettingsManager.get_settings().LLM_CONFIG_MAX_TOKENS,
            }

            # Format messages for Ollama
            async def format_ollama_messages(messages: list) -> str:
                formatted_prompt = ""
                for message in messages:
                    if message["role"] == "user":
                        content = message["content"]
                        if isinstance(content, list):
                            # Handle multi-modal content
                            for item in content:
                                if item["type"] == "text":
                                    formatted_prompt += f"{item['text']}\n"
                        else:
                            formatted_prompt += f"{content}\n"
                return formatted_prompt.strip()

            try:
                if llm_config.model_name.startswith("ollama/") and screenshots:
                    # Use vision model for image-related prompts
                    vision_config = LLMConfigRegistry.get_config("OLLAMA_VISION")
                    text_config = LLMConfigRegistry.get_config("OLLAMA_TEXT")
                    
                    # First, process the image with vision model
                    vision_messages = await llm_messages_builder(prompt, screenshots, vision_config.add_assistant_prefix)
                    vision_prompt = await format_ollama_messages(vision_messages)
                    
                    LOG.info("Calling Vision LLM API", model=vision_config.model_name)
                    vision_response = await litellm.acompletion(
                        model=vision_config.model_name,
                        messages=[{"role": "user", "content": vision_prompt}],
                        timeout=timeout,
                        max_retries=max_retries,
                        options=ollama_options
                    )
                    
                    # Log the raw vision response
                    LOG.info("Raw Vision LLM Response", 
                            model=vision_config.model_name,
                            response=vision_response.choices[0].message.content if vision_response.choices else "No response")
                    
                    # Extract vision analysis from response
                    vision_analysis = vision_response.choices[0].message.content
                    
                    # Then, process with text model
                    text_prompt = f"Vision Analysis:\n{vision_analysis}\n\nOriginal Prompt:\n{prompt}"
                    text_messages = await llm_messages_builder(text_prompt, None, text_config.add_assistant_prefix)
                    formatted_text_prompt = await format_ollama_messages(text_messages)
                    
                    LOG.info("Calling Text LLM API", model=text_config.model_name)
                    response = await litellm.acompletion(
                        model=text_config.model_name,
                        messages=[{"role": "user", "content": formatted_text_prompt}],
                        timeout=timeout,
                        max_retries=max_retries,
                        options=ollama_options
                    )
                    
                    # Log the raw text response
                    LOG.info("Raw Text LLM Response", 
                            model=text_config.model_name,
                            response=response.choices[0].message.content if response.choices else "No response")
                else:
                    # Use regular handling for non-Ollama models or text-only requests
                    messages = await llm_messages_builder(prompt, screenshots, llm_config.add_assistant_prefix)
                    
                    if llm_config.model_name.startswith("ollama/"):
                        # Format messages for Ollama
                        formatted_prompt = await format_ollama_messages(messages)
                        LOG.info("Full prompt being sent to LLM", 
                                llm_key=llm_key, 
                                model=llm_config.model_name, 
                                messages=formatted_prompt)
                        response = await litellm.acompletion(
                            model=llm_config.model_name,
                            messages=[{"role": "user", "content": formatted_prompt}],
                            timeout=timeout,
                            max_retries=max_retries,
                            options=ollama_options
                        )
                        
                        # Log the raw response
                        LOG.info("Raw LLM Response", 
                                model=llm_config.model_name,
                                response=response.choices[0].message.content if response.choices else "No response")
                    else:
                        # Regular handling for other models
                        LOG.info("Full prompt being sent to LLM", 
                                llm_key=llm_key, 
                                model=llm_config.model_name, 
                                messages=json.dumps(messages, indent=2))
                        response = await litellm.acompletion(
                            model=llm_config.model_name,
                            messages=messages,
                            timeout=timeout,
                            max_retries=max_retries,
                            **active_parameters
                        )
                        
                        # Log the raw response
                        LOG.info("Raw LLM Response", 
                                model=llm_config.model_name,
                                response=response.choices[0].message.content if response.choices else "No response")

                # Create artifact for raw LLM response
                if step:
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=step,
                        artifact_type=ArtifactType.LLM_RESPONSE,
                        data=response.model_dump_json(indent=2).encode("utf-8"),
                    )

                    # Handle cost calculation if needed
                    if not llm_config.skip_cost_calculation:
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

                # Process the response content
                if response and response.choices:
                    content = response.choices[0].message.content
                    
                    # Clean up the response content and extract JSON if present
                    def extract_json_from_content(content: str) -> str:
                        content = content.strip()
                        # Look for JSON code block
                        if "```json" in content:
                            # Extract content between ```json and ```
                            parts = content.split("```json")
                            if len(parts) > 1:
                                json_part = parts[1].split("```")[0]
                                return json_part.strip()
                        # Look for just ``` code block
                        elif "```" in content:
                            # Extract content between ``` and ```
                            parts = content.split("```")
                            if len(parts) > 1:
                                return parts[1].strip()
                        # If no code blocks found, return the original content
                        return content

                    try:
                        # Parse JSON if expected
                        if "JSON" in prompt:
                            # Extract and clean JSON content
                            json_content = extract_json_from_content(content)
                            LOG.debug("Attempting to parse JSON response", content=json_content)
                            
                            try:
                                parsed_content = json.loads(json_content)
                            except json.JSONDecodeError:
                                # If JSON parsing fails, try to clean the content further
                                cleaned_content = json_content.strip()
                                if cleaned_content.startswith('json'):
                                    cleaned_content = cleaned_content.split('\n', 1)[1]
                                LOG.debug("Retrying JSON parse with cleaned content", content=cleaned_content)
                                parsed_content = json.loads(cleaned_content)
                            
                            # Ensure response has required keys for action parsing
                            if "actions" not in parsed_content and "action" in parsed_content:
                                # Handle case where LLM returns single action
                                parsed_content = {"actions": [parsed_content]}
                            elif "actions" not in parsed_content and not any(key in parsed_content for key in ["action", "actions"]):
                                # If no action-related keys found, wrap the entire response
                                if "confidence_float" in parsed_content and "shape" in parsed_content:
                                    # Special case for SVG shape analysis
                                    return parsed_content
                                else:
                                    LOG.warning("Response missing actions key, wrapping content", content=parsed_content)
                                    parsed_content = {"actions": [{"action": "UNKNOWN", "data": parsed_content}]}
                            
                            # Create artifact for parsed response
                            if step:
                                await app.ARTIFACT_MANAGER.create_artifact(
                                    step=step,
                                    artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                                    data=json.dumps(parsed_content, indent=2).encode("utf-8"),
                                )
                            
                            return parsed_content
                        
                        # Handle non-JSON responses
                        result = {"content": content}
                        if step:
                            await app.ARTIFACT_MANAGER.create_artifact(
                                step=step,
                                artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                                data=json.dumps(result, indent=2).encode("utf-8"),
                            )
                        return result

                    except json.JSONDecodeError as e:
                        LOG.error("Failed to parse JSON response", 
                                content=content,
                                error=str(e),
                                raw_content=content)
                        return {"error": "Invalid JSON response", "raw_content": content}
                
                LOG.error("Empty response from LLM")
                return {"error": "Empty response from LLM"}

            except Exception as e:
                LOG.exception("LLM request failed unexpectedly", llm_key=llm_key, error=str(e))
                raise LLMProviderError(llm_key) from e

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
