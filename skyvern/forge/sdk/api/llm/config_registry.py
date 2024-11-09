import structlog
import logging

from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateLLMConfigError,
    InvalidLLMConfigError,
    MissingLLMProviderEnvVarsError,
    NoProviderEnabledError,
)
from skyvern.forge.sdk.api.llm.models import LiteLLMParams, LLMConfig, LLMRouterConfig
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Add debug logging at the top of the file
print("Initializing config registry...")

settings = SettingsManager.get_settings()
print("Config Registry Settings:", {
    "ENABLE_LLAMA": settings.ENABLE_LLAMA,
    "LLAMA_API_BASE": settings.LLAMA_API_BASE,
    "LLAMA_MODEL_NAME": settings.LLAMA_MODEL_NAME,
    "LLM_KEY": settings.LLM_KEY,
    "ENV_FILE": settings.model_config.get('env_file', '.env')  # Use model_config instead of Config
})

# First check if any providers are enabled
provider_check = any([
    settings.ENABLE_OPENAI,
    settings.ENABLE_ANTHROPIC,
    settings.ENABLE_AZURE,
    settings.ENABLE_BEDROCK,
    settings.ENABLE_LLAMA,
])
print("Provider check result:", provider_check)

if not provider_check:
    print("No providers enabled, raising NoProviderEnabledError")
    raise NoProviderEnabledError()

class LLMConfigRegistry:
    _configs: dict[str, LLMRouterConfig | LLMConfig] = {}

    @staticmethod
    def is_router_config(llm_key: str) -> bool:
        return isinstance(LLMConfigRegistry.get_config(llm_key), LLMRouterConfig)

    @staticmethod
    def validate_config(llm_key: str, config: LLMRouterConfig | LLMConfig) -> None:
        missing_env_vars = config.get_missing_env_vars()
        if missing_env_vars:
            raise MissingLLMProviderEnvVarsError(llm_key, missing_env_vars)

    @classmethod
    def register_config(cls, llm_key: str, config: LLMRouterConfig | LLMConfig) -> None:
        if llm_key in cls._configs:
            raise DuplicateLLMConfigError(llm_key)

        cls.validate_config(llm_key, config)

        LOG.debug("Registering LLM config", llm_key=llm_key)
        cls._configs[llm_key] = config

    @classmethod
    def get_config(cls, llm_key: str) -> LLMRouterConfig | LLMConfig:
        if llm_key not in cls._configs:
            raise InvalidLLMConfigError(llm_key)

        return cls._configs[llm_key]


# Before the provider check, add debug logging
logger.debug("Current settings: %s", {
    "ENABLE_LLAMA": SettingsManager.get_settings().ENABLE_LLAMA,
    "LLAMA_API_BASE": SettingsManager.get_settings().LLAMA_API_BASE,
    "LLAMA_MODEL_NAME": SettingsManager.get_settings().LLAMA_MODEL_NAME,
    "LLM_KEY": SettingsManager.get_settings().LLM_KEY
})

# Add this before the provider check
logger.debug("Checking environment settings:")
settings = SettingsManager.get_settings()
logger.debug("Environment variables: %s", {
    "ENABLE_LLAMA": settings.ENABLE_LLAMA,
    "LLAMA_API_BASE": settings.LLAMA_API_BASE,
    "LLAMA_MODEL_NAME": settings.LLAMA_MODEL_NAME,
    "LLAMA_API_ROUTE": settings.LLAMA_API_ROUTE,
    "LLM_KEY": settings.LLM_KEY,
    "ENV_FILE": settings.model_config.get('env_file', '.env')
})

# First check if any providers are enabled
if not any([
    SettingsManager.get_settings().ENABLE_OPENAI,
    SettingsManager.get_settings().ENABLE_ANTHROPIC,
    SettingsManager.get_settings().ENABLE_AZURE,
    SettingsManager.get_settings().ENABLE_BEDROCK,
    SettingsManager.get_settings().ENABLE_LLAMA,  # Make sure Llama is included
]):
    raise NoProviderEnabledError()

# First register Llama configuration
if SettingsManager.get_settings().ENABLE_LLAMA:
    print("Registering Llama configuration...")
    LLMConfigRegistry.register_config(
        "LLAMA3",
        LLMConfig(
            model_name="ollama/llama3.2-vision",  # Move model name here with ollama/ prefix
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            max_output_tokens=16384,
            litellm_params=LiteLLMParams(
                api_base=settings.LLAMA_API_BASE,
                api_key="",
                model_info={
                    "completion_route": "/api/chat"
                }
            )
        )
    )

# Add after LLMConfigRegistry.register_config
logger.debug("Registered configs after Llama registration: %s", LLMConfigRegistry._configs)

# After registration, check registered configs
logger.debug("Registered configs: %s", LLMConfigRegistry._configs)

# Then register other provider configurations
if SettingsManager.get_settings().ENABLE_OPENAI:
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4_TURBO",
        LLMConfig(
            "gpt-4-turbo",
            ["OPENAI_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4V",
        LLMConfig(
            "gpt-4-turbo",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4O",
        LLMConfig(
            "gpt-4o", ["OPENAI_API_KEY"], supports_vision=True, add_assistant_prefix=False, max_output_tokens=16384
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4O_MINI",
        LLMConfig(
            "gpt-4o-mini",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_output_tokens=16384,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT-4O-2024-08-06",
        LLMConfig(
            "gpt-4o-2024-08-06",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_output_tokens=16384,
        ),
    )


if SettingsManager.get_settings().ENABLE_ANTHROPIC:
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3",
        LLMConfig(
            "anthropic/claude-3-sonnet-20240229",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3_OPUS",
        LLMConfig(
            "anthropic/claude-3-opus-20240229",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3_SONNET",
        LLMConfig(
            "anthropic/claude-3-sonnet-20240229",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3_HAIKU",
        LLMConfig(
            "anthropic/claude-3-haiku-20240307",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3.5_SONNET",
        LLMConfig(
            "anthropic/claude-3-5-sonnet-latest",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
            max_output_tokens=8192,
        ),
    )

if SettingsManager.get_settings().ENABLE_BEDROCK:
    # Supported through AWS IAM authentication
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3_OPUS",
        LLMConfig(
            "bedrock/anthropic.claude-3-opus-20240229-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3_SONNET",
        LLMConfig(
            "bedrock/anthropic.claude-3-sonnet-20240229-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3_HAIKU",
        LLMConfig(
            "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3.5_SONNET",
        LLMConfig(
            "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )

if SettingsManager.get_settings().ENABLE_AZURE:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI",
        LLMConfig(
            f"azure/{SettingsManager.get_settings().AZURE_DEPLOYMENT}",
            [
                "AZURE_DEPLOYMENT",
                "AZURE_API_KEY",
                "AZURE_API_BASE",
                "AZURE_API_VERSION",
            ],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )

if SettingsManager.get_settings().ENABLE_AZURE_GPT4O_MINI:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4O_MINI",
        LLMConfig(
            f"azure/{SettingsManager.get_settings().AZURE_GPT4O_MINI_DEPLOYMENT}",
            [
                "AZURE_GPT4O_MINI_DEPLOYMENT",
                "AZURE_GPT4O_MINI_API_KEY",
                "AZURE_GPT4O_MINI_API_BASE",
                "AZURE_GPT4O_MINI_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=SettingsManager.get_settings().AZURE_GPT4O_MINI_API_BASE,
                api_key=SettingsManager.get_settings().AZURE_GPT4O_MINI_API_KEY,
                api_version=SettingsManager.get_settings().AZURE_GPT4O_MINI_API_VERSION,
                model_info={"model_name": "azure/gpt-4o-mini"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )
