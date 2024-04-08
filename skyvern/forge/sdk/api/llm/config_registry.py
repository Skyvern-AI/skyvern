import litellm
import structlog

from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateLLMConfigError,
    InvalidLLMConfigError,
    MissingLLMProviderEnvVarsError,
    NoProviderEnabledError,
)
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


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

        LOG.info("Registering LLM config", llm_key=llm_key)
        cls._configs[llm_key] = config

    @classmethod
    def get_config(cls, llm_key: str) -> LLMRouterConfig | LLMConfig:
        if llm_key not in cls._configs:
            raise InvalidLLMConfigError(llm_key)

        return cls._configs[llm_key]


# if none of the LLM providers are enabled, raise an error
if not any(
    [
        SettingsManager.get_settings().ENABLE_OPENAI,
        SettingsManager.get_settings().ENABLE_ANTHROPIC,
        SettingsManager.get_settings().ENABLE_AZURE,
        SettingsManager.get_settings().ENABLE_GEMINI,
    ]
):
    raise NoProviderEnabledError()


if SettingsManager.get_settings().ENABLE_OPENAI:
    LLMConfigRegistry.register_config("OPENAI_GPT4_TURBO", LLMConfig("gpt-4-turbo-preview", ["OPENAI_API_KEY"], False))
    LLMConfigRegistry.register_config("OPENAI_GPT4V", LLMConfig("gpt-4-vision-preview", ["OPENAI_API_KEY"], True))

if SettingsManager.get_settings().ENABLE_ANTHROPIC:
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3", LLMConfig("anthropic/claude-3-opus-20240229", ["ANTHROPIC_API_KEY"], True)
    )

if SettingsManager.get_settings().ENABLE_AZURE:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4V",
        LLMConfig(
            f"azure/{SettingsManager.get_settings().AZURE_DEPLOYMENT}",
            ["AZURE_DEPLOYMENT", "AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION"],
            True,
        ),
    )
if SettingsManager.get_settings().ENABLE_GEMINI:
    LLMConfigRegistry.register_config(
        "GEMINI_GPT4V",
        litellm.completion(
            "gemini/gemini-pro-vision",
            ["GEMINI_API_KEY"],
            True,
        ),
    )
