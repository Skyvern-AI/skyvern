import structlog

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.exceptions import (
    DuplicateLLMConfigError,
    InvalidLLMConfigError,
    MissingLLMProviderEnvVarsError,
)
from skyvern.forge.sdk.api.llm.models import LiteLLMParams, LLMConfig, LLMRouterConfig

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

        LOG.debug("Registering LLM config", llm_key=llm_key)
        cls._configs[llm_key] = config

    @classmethod
    def get_config(cls, llm_key: str) -> LLMRouterConfig | LLMConfig:
        if llm_key not in cls._configs:
            raise InvalidLLMConfigError(llm_key)

        return cls._configs[llm_key]


# if none of the LLM providers are enabled, raise an error
if not any(
    [
        settings.ENABLE_OPENAI,
        settings.ENABLE_ANTHROPIC,
        settings.ENABLE_AZURE,
        settings.ENABLE_AZURE_GPT4O_MINI,
        settings.ENABLE_BEDROCK,
        settings.ENABLE_GEMINI,
        settings.ENABLE_NOVITA,
    ]
):
    LOG.warning(
        "To run skyvern locally, you need to enable at least one LLM provider. Run setup.sh and follow through the LLM provider setup, or "
        "update the .env file (check out .env.example to see the required environment variables)."
    )


if settings.ENABLE_OPENAI:
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
        "OPENAI_GPT4_5",
        LLMConfig(
            "gpt-4.5-preview",
            ["OPENAI_API_KEY"],
            supports_vision=True,
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
            "gpt-4o", ["OPENAI_API_KEY"], supports_vision=True, add_assistant_prefix=False, max_completion_tokens=16384
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_O3_MINI",
        LLMConfig(
            "o3-mini",
            ["OPENAI_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            max_completion_tokens=16384,
            temperature=None,  # Temperature isn't supported in the O-model series
            reasoning_effort="high",
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4O_MINI",
        LLMConfig(
            "gpt-4o-mini",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=16384,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT-4O-2024-08-06",
        LLMConfig(
            "gpt-4o-2024-08-06",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=16384,
        ),
    )


if settings.ENABLE_ANTHROPIC:
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
            max_completion_tokens=8192,
        ),
    )
    LLMConfigRegistry.register_config(
        "ANTHROPIC_CLAUDE3.7_SONNET",
        LLMConfig(
            "anthropic/claude-3-7-sonnet-latest",
            ["ANTHROPIC_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=True,
            max_completion_tokens=8192,
        ),
    )

if settings.ENABLE_BEDROCK:
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
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3.5_SONNET_INFERENCE_PROFILE",
        LLMConfig(
            "bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3.5_SONNET_V1",
        LLMConfig(
            "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_AMAZON_NOVA_PRO",
        LLMConfig(
            "bedrock/us.amazon.nova-pro-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )
    LLMConfigRegistry.register_config(
        "BEDROCK_AMAZON_NOVA_LITE",
        LLMConfig(
            "bedrock/us.amazon.nova-lite-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
        ),
    )


if settings.ENABLE_AZURE:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI",
        LLMConfig(
            f"azure/{settings.AZURE_DEPLOYMENT}",
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

if settings.ENABLE_AZURE_GPT4O_MINI:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4O_MINI",
        LLMConfig(
            f"azure/{settings.AZURE_GPT4O_MINI_DEPLOYMENT}",
            [
                "AZURE_GPT4O_MINI_DEPLOYMENT",
                "AZURE_GPT4O_MINI_API_KEY",
                "AZURE_GPT4O_MINI_API_BASE",
                "AZURE_GPT4O_MINI_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_GPT4O_MINI_API_BASE,
                api_key=settings.AZURE_GPT4O_MINI_API_KEY,
                api_version=settings.AZURE_GPT4O_MINI_API_VERSION,
                model_info={"model_name": "azure/gpt-4o-mini"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )

if settings.ENABLE_AZURE_O3_MINI:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_O3_MINI",
        LLMConfig(
            f"azure/{settings.AZURE_O3_MINI_DEPLOYMENT}",
            [
                "AZURE_O3_MINI_DEPLOYMENT",
                "AZURE_O3_MINI_API_KEY",
                "AZURE_O3_MINI_API_BASE",
                "AZURE_O3_MINI_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_O3_MINI_API_BASE,
                api_key=settings.AZURE_O3_MINI_API_KEY,
                api_version=settings.AZURE_O3_MINI_API_VERSION,
                model_info={"model_name": "azure/o3-mini"},
            ),
            supports_vision=False,
            add_assistant_prefix=False,
            max_completion_tokens=16384,
            temperature=None,  # Temperature isn't supported in the O-model series
            reasoning_effort="low",
        ),
    )

if settings.ENABLE_GEMINI:
    LLMConfigRegistry.register_config(
        "GEMINI_FLASH_2_0",
        LLMConfig(
            "gemini/gemini-2.0-flash-001",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
        ),
    )
    LLMConfigRegistry.register_config(
        "GEMINI_FLASH_2_0_LITE",
        LLMConfig(
            "gemini/gemini-2.0-flash-lite-preview-02-05",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
        ),
    )
    LLMConfigRegistry.register_config(
        "GEMINI_PRO",
        LLMConfig(
            "gemini/gemini-1.5-pro",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
        ),
    )
    LLMConfigRegistry.register_config(
        "GEMINI_FLASH",
        LLMConfig(
            "gemini/gemini-1.5-flash",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
        ),
    )

if settings.ENABLE_NOVITA:
    LLMConfigRegistry.register_config(
        "NOVITA_DEEPSEEK_R1",
        LLMConfig(
            "openai/deepseek/deepseek-r1",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/deepseek/deepseek-r1"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_DEEPSEEK_V3",
        LLMConfig(
            "openai/deepseek/deepseek_v3",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/deepseek/deepseek_v3"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_3_70B",
        LLMConfig(
            "openai/meta-llama/llama-3.3-70b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.3-70b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_2_1B",
        LLMConfig(
            "openai/meta-llama/llama-3.2-1b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.2-1b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_2_3B",
        LLMConfig(
            "openai/meta-llama/llama-3.2-3b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.2-3b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_2_11B_VISION",
        LLMConfig(
            "openai/meta-llama/llama-3.2-11b-vision-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.2-11b-vision-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_1_8B",
        LLMConfig(
            "openai/meta-llama/llama-3.1-8b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.1-8b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_1_70B",
        LLMConfig(
            "openai/meta-llama/llama-3.1-70b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.1-70b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_1_405B",
        LLMConfig(
            "openai/meta-llama/llama-3.1-405b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3.1-405b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_8B",
        LLMConfig(
            "openai/meta-llama/llama-3-8b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3-8b-instruct"},
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "NOVITA_LLAMA_3_70B",
        LLMConfig(
            "openai/meta-llama/llama-3-70b-instruct",
            ["NOVITA_API_KEY"],
            supports_vision=False,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_base="https://api.novita.ai/v3/openai",
                api_key=settings.NOVITA_API_KEY,
                api_version=settings.NOVITA_API_VERSION,
                model_info={"model_name": "openai/meta-llama/llama-3-70b-instruct"},
            ),
        ),
    )
