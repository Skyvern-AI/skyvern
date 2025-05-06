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
            # If the key is not found in registered configs, treat it as a general model
            if not llm_key:
                raise InvalidLLMConfigError(f"LLM_KEY not set for {llm_key}")
            return LLMConfig(
                llm_key,  # Use the LLM_KEY as the model name
                ["LLM_API_KEY"],
                supports_vision=settings.LLM_CONFIG_SUPPORT_VISION,
                add_assistant_prefix=settings.LLM_CONFIG_ADD_ASSISTANT_PREFIX,
                max_completion_tokens=settings.LLM_CONFIG_MAX_TOKENS,
            )

        return cls._configs[llm_key]


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
        "OPENAI_GPT4_1",
        LLMConfig(
            "gpt-4.1",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4_1_MINI",
        LLMConfig(
            "gpt-4.1-mini",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_GPT4_1_NANO",
        LLMConfig(
            "gpt-4.1-nano",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
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
    LLMConfigRegistry.register_config(
        "OPENAI_O4_MINI",
        LLMConfig(
            "o4-mini",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=100000,
            temperature=None,  # Temperature isn't supported in the O-model series
            reasoning_effort="high",
            litellm_params=LiteLLMParams(
                drop_params=True,  # type: ignore
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "OPENAI_O3",
        LLMConfig(
            "o3",
            ["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=100000,
            temperature=None,  # Temperature isn't supported in the O-model series
            reasoning_effort="high",
            litellm_params=LiteLLMParams(
                drop_params=True,  # type: ignore
            ),
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
    LLMConfigRegistry.register_config(
        "BEDROCK_ANTHROPIC_CLAUDE3.7_SONNET_INFERENCE_PROFILE",
        LLMConfig(
            "bedrock/us.anthropic.claude-3-7-sonnet-20250219-v1:0",
            ["AWS_REGION"],
            supports_vision=True,
            add_assistant_prefix=True,
            max_completion_tokens=64000,
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
            reasoning_effort="high",
        ),
    )

if settings.ENABLE_AZURE_GPT4_1:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4_1",
        LLMConfig(
            f"azure/{settings.AZURE_GPT4_1_DEPLOYMENT}",
            [
                "AZURE_GPT4_1_DEPLOYMENT",
                "AZURE_GPT4_1_API_KEY",
                "AZURE_GPT4_1_API_BASE",
                "AZURE_GPT4_1_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_GPT4_1_API_BASE,
                api_key=settings.AZURE_GPT4_1_API_KEY,
                api_version=settings.AZURE_GPT4_1_API_VERSION,
                model_info={"model_name": "azure/gpt-4.1"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
        ),
    )

if settings.ENABLE_AZURE_GPT4_1_MINI:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4_1_MINI",
        LLMConfig(
            f"azure/{settings.AZURE_GPT4_1_MINI_DEPLOYMENT}",
            [
                "AZURE_GPT4_1_MINI_DEPLOYMENT",
                "AZURE_GPT4_1_MINI_API_KEY",
                "AZURE_GPT4_1_MINI_API_BASE",
                "AZURE_GPT4_1_MINI_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_GPT4_1_MINI_API_BASE,
                api_key=settings.AZURE_GPT4_1_MINI_API_KEY,
                api_version=settings.AZURE_GPT4_1_MINI_API_VERSION,
                model_info={"model_name": "azure/gpt-4.1-mini"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
        ),
    )

if settings.ENABLE_AZURE_GPT4_1_NANO:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_GPT4_1_NANO",
        LLMConfig(
            f"azure/{settings.AZURE_GPT4_1_NANO_DEPLOYMENT}",
            [
                "AZURE_GPT4_1_NANO_DEPLOYMENT",
                "AZURE_GPT4_1_NANO_API_KEY",
                "AZURE_GPT4_1_NANO_API_BASE",
                "AZURE_GPT4_1_NANO_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_GPT4_1_NANO_API_BASE,
                api_key=settings.AZURE_GPT4_1_NANO_API_KEY,
                api_version=settings.AZURE_GPT4_1_NANO_API_VERSION,
                model_info={"model_name": "azure/gpt-4.1-nano"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=32768,
        ),
    )


if settings.ENABLE_AZURE_O4_MINI:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_O4_MINI",
        LLMConfig(
            f"azure/{settings.AZURE_O4_MINI_DEPLOYMENT}",
            [
                "AZURE_O4_MINI_DEPLOYMENT",
                "AZURE_O4_MINI_API_KEY",
                "AZURE_O4_MINI_API_BASE",
                "AZURE_O4_MINI_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_O4_MINI_API_BASE,
                api_key=settings.AZURE_O4_MINI_API_KEY,
                api_version=settings.AZURE_O4_MINI_API_VERSION,
                model_info={"model_name": "azure/o4-mini"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=100000,
        ),
    )


if settings.ENABLE_AZURE_O3:
    LLMConfigRegistry.register_config(
        "AZURE_OPENAI_O3",
        LLMConfig(
            f"azure/{settings.AZURE_O3_DEPLOYMENT}",
            [
                "AZURE_O3_DEPLOYMENT",
                "AZURE_O3_API_KEY",
                "AZURE_O3_API_BASE",
                "AZURE_O3_API_VERSION",
            ],
            litellm_params=LiteLLMParams(
                api_base=settings.AZURE_O3_API_BASE,
                api_key=settings.AZURE_O3_API_KEY,
                api_version=settings.AZURE_O3_API_VERSION,
                model_info={"model_name": "azure/o3"},
            ),
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=100000,
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
    LLMConfigRegistry.register_config(
        "GEMINI_2.5_PRO_PREVIEW_03_25",
        LLMConfig(
            "gemini/gemini-2.5-pro-preview-03-25",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=65536,
        ),
    )
    LLMConfigRegistry.register_config(
        "GEMINI_2.5_PRO_EXP_03_25",
        LLMConfig(
            "gemini/gemini-2.5-pro-exp-03-25",
            ["GEMINI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=65536,
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

# Create a GCP service account WITH the Vertex AI API access enabled
# Get the credentials json file. See documentation: https://support.google.com/a/answer/7378726?hl=en
# my_vertex_credentials = json.dumps(json.load(open("my_credentials_file.json")))
# Set the value of my_vertex_credentials as the environment variable VERTEX_CREDENTIALS
if settings.ENABLE_VERTEX_AI:
    LLMConfigRegistry.register_config(
        "VERTEX_GEMINI_2.5_FLASH_PREVIEW_04_17",
        LLMConfig(
            "vertex_ai/gemini-2.5-flash-preview-04-17",
            ["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=65535,
            litellm_params=LiteLLMParams(
                vertex_credentials=settings.VERTEX_CREDENTIALS,
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "VERTEX_GEMINI_2.5_PRO_PREVIEW_03_25",
        LLMConfig(
            "vertex_ai/gemini-2.5-pro-preview-03-25",
            ["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=65535,
            litellm_params=LiteLLMParams(
                vertex_credentials=settings.VERTEX_CREDENTIALS,
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "VERTEX_GEMINI_FLASH_2_0",
        LLMConfig(
            "vertex_ai/gemini-2.0-flash-001",
            ["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
            litellm_params=LiteLLMParams(
                vertex_credentials=settings.VERTEX_CREDENTIALS,  # type: ignore
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "VERTEX_GEMINI_PRO",
        LLMConfig(
            "vertex_ai/gemini-1.5-pro",
            ["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
            litellm_params=LiteLLMParams(
                vertex_credentials=settings.VERTEX_CREDENTIALS,  # type: ignore
            ),
        ),
    )
    LLMConfigRegistry.register_config(
        "VERTEX_GEMINI_FLASH",
        LLMConfig(
            "vertex_ai/gemini-1.5-flash",
            ["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            max_completion_tokens=8192,
            litellm_params=LiteLLMParams(
                vertex_credentials=settings.VERTEX_CREDENTIALS,  # type: ignore
            ),
        ),
    )

if settings.ENABLE_OLLAMA:
    # Register Ollama model configured in settings
    if settings.OLLAMA_MODEL:
        ollama_model_name = settings.OLLAMA_MODEL
        LLMConfigRegistry.register_config(
            "OLLAMA",
            LLMConfig(
                f"ollama/{ollama_model_name}",
                ["OLLAMA_SERVER_URL", "OLLAMA_MODEL"],
                supports_vision=False,  # Ollama does not support vision yet
                add_assistant_prefix=False,
                litellm_params=LiteLLMParams(
                    api_base=settings.OLLAMA_SERVER_URL,
                    api_key=None,
                    api_version=None,
                    model_info={"model_name": f"ollama/{ollama_model_name}"},
                ),
            ),
        )

if settings.ENABLE_OPENROUTER:
    # Register OpenRouter model configured in settings
    if settings.OPENROUTER_MODEL:
        openrouter_model_name = settings.OPENROUTER_MODEL
        LLMConfigRegistry.register_config(
            "OPENROUTER",
            LLMConfig(
                f"openrouter/{openrouter_model_name}",
                ["OPENROUTER_API_KEY", "OPENROUTER_MODEL"],
                supports_vision=settings.LLM_CONFIG_SUPPORT_VISION,
                add_assistant_prefix=False,
                max_completion_tokens=settings.LLM_CONFIG_MAX_TOKENS,
                litellm_params=LiteLLMParams(
                    api_key=settings.OPENROUTER_API_KEY,
                    api_base=settings.OPENROUTER_API_BASE,
                    api_version=None,
                    model_info={"model_name": f"openrouter/{openrouter_model_name}"},
                ),
            ),
        )
if settings.ENABLE_GROQ:
    # Register Groq model configured in settings
    if settings.GROQ_MODEL:
        groq_model_name = settings.GROQ_MODEL
        LLMConfigRegistry.register_config(
            "GROQ",
            LLMConfig(
                f"groq/{groq_model_name}",
                ["GROQ_API_KEY", "GROQ_MODEL"],
                supports_vision=settings.LLM_CONFIG_SUPPORT_VISION,
                add_assistant_prefix=False,
                max_completion_tokens=settings.LLM_CONFIG_MAX_TOKENS,
                litellm_params=LiteLLMParams(
                    api_key=settings.GROQ_API_KEY,
                    api_version=None,
                    api_base=settings.GROQ_API_BASE,
                    model_info={"model_name": f"groq/{groq_model_name}"},
                ),
            ),
        )
# Add support for dynamically configuring OpenAI-compatible LLM models
# Based on liteLLM's support for OpenAI-compatible APIs
# See documentation: https://docs.litellm.ai/docs/providers/openai_compatible
if settings.ENABLE_OPENAI_COMPATIBLE:
    # Check for required model name
    openai_compatible_model_key = settings.OPENAI_COMPATIBLE_MODEL_KEY
    openai_compatible_model_name = settings.OPENAI_COMPATIBLE_MODEL_NAME

    if not openai_compatible_model_name:
        raise InvalidLLMConfigError(
            "OPENAI_COMPATIBLE_MODEL_NAME is required but not set. OpenAI-compatible model will not be registered."
        )
    else:
        # Required environment variables to check
        required_env_vars = ["OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_MODEL_NAME", "OPENAI_COMPATIBLE_API_BASE"]

        # Configure litellm parameters - note the "openai/" prefix required for liteLLM routing
        litellm_params = LiteLLMParams(
            api_key=settings.OPENAI_COMPATIBLE_API_KEY,
            api_base=settings.OPENAI_COMPATIBLE_API_BASE,
            api_version=settings.OPENAI_COMPATIBLE_API_VERSION,
            model_info={"model_name": f"openai/{openai_compatible_model_name}"},
        )

        # Configure LLMConfig
        LLMConfigRegistry.register_config(
            openai_compatible_model_key,
            LLMConfig(
                f"openai/{openai_compatible_model_name}",  # Add openai/ prefix for liteLLM
                required_env_vars,
                supports_vision=settings.OPENAI_COMPATIBLE_SUPPORTS_VISION,
                add_assistant_prefix=settings.OPENAI_COMPATIBLE_ADD_ASSISTANT_PREFIX,
                max_completion_tokens=settings.OPENAI_COMPATIBLE_MAX_TOKENS or settings.LLM_CONFIG_MAX_TOKENS,
                temperature=settings.OPENAI_COMPATIBLE_TEMPERATURE
                if settings.OPENAI_COMPATIBLE_TEMPERATURE is not None
                else settings.LLM_CONFIG_TEMPERATURE,
                litellm_params=litellm_params,
                reasoning_effort=settings.OPENAI_COMPATIBLE_REASONING_EFFORT,
            ),
        )
        LOG.info(
            f"Registered OpenAI-compatible model with key {openai_compatible_model_key}",
            model_name=openai_compatible_model_name,
        )
