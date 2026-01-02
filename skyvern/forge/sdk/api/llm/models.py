from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypedDict

from skyvern.forge.sdk.settings_manager import SettingsManager


class LiteLLMParams(TypedDict, total=False):
    api_key: str | None
    api_version: str | None
    api_base: str | None
    model_info: dict[str, Any] | None
    vertex_credentials: str | None
    vertex_location: str | None
    thinking: dict[str, Any] | None
    thinking_level: str | None
    service_tier: str | None
    timeout: float | None


@dataclass(frozen=True)
class LLMConfigBase:
    model_name: str
    required_env_vars: list[str]
    supports_vision: bool
    add_assistant_prefix: bool

    def get_missing_env_vars(self) -> list[str]:
        missing_env_vars = []
        for env_var in self.required_env_vars:
            env_var_value = getattr(SettingsManager.get_settings(), env_var, None)
            if not env_var_value:
                missing_env_vars.append(env_var)

        return missing_env_vars


@dataclass(frozen=True)
class LLMConfig(LLMConfigBase):
    litellm_params: Optional[LiteLLMParams] = field(default=None)
    max_tokens: int | None = SettingsManager.get_settings().LLM_CONFIG_MAX_TOKENS
    max_completion_tokens: int | None = None
    temperature: float | None = SettingsManager.get_settings().LLM_CONFIG_TEMPERATURE
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class LLMAllowedFailsPolicy:
    bad_request_error_allowed_fails: int | None = None
    authentication_error_allowed_fails: int | None = None
    timeout_error_allowed_fails: int | None = None
    rate_limit_error_allowed_fails: int | None = None
    content_policy_violation_error_allowed_fails: int | None = None
    internal_server_error_allowed_fails: int | None = None


@dataclass(frozen=True)
class LLMRouterModelConfig:
    model_name: str
    # https://litellm.vercel.app/docs/routing
    litellm_params: dict[str, Any]
    model_info: dict[str, Any] = field(default_factory=dict)
    tpm: int | None = None
    rpm: int | None = None


@dataclass(frozen=True)
class LLMRouterConfig(LLMConfigBase):
    model_list: list[LLMRouterModelConfig]
    # All three redis parameters are required. Even if there isn't a password, it should be an empty string.
    main_model_group: str
    redis_host: str | None = None
    redis_port: int | None = None
    redis_password: str | None = None
    fallback_model_group: str | None = None
    routing_strategy: Literal[
        "simple-shuffle",
        "least-busy",
        "usage-based-routing",
        "usage-based-routing-v2",
        "latency-based-routing",
    ] = "usage-based-routing"
    num_retries: int = 1
    retry_delay_seconds: int = 15
    set_verbose: bool = False
    disable_cooldowns: bool | None = None
    allowed_fails: int | None = None
    allowed_fails_policy: LLMAllowedFailsPolicy | None = None
    cooldown_time: float | None = None
    max_tokens: int | None = SettingsManager.get_settings().LLM_CONFIG_MAX_TOKENS
    max_completion_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = SettingsManager.get_settings().LLM_CONFIG_TEMPERATURE
