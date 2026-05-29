from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, cast

__all__ = [
    "LiteLLMParams",
    "LLMAllowedFailsPolicy",
    "LLMConfig",
    "LLMConfigBase",
    "LLMRouterConfig",
    "LLMRouterModelConfig",
]

_SETTINGS_DEFAULT = object()
# Sentinel replaced in __post_init__; distinct from callers explicitly passing None.
_DEFAULT_MAX_TOKENS = cast("int | None", _SETTINGS_DEFAULT)
_DEFAULT_TEMPERATURE = cast("float | None", _SETTINGS_DEFAULT)


def _assert_settings_defaults_resolved(*values: object) -> None:
    if any(value is _SETTINGS_DEFAULT for value in values):
        raise RuntimeError("settings default sentinel was not resolved")


def _resolve_generation_defaults(config: object, max_tokens: object, temperature: object) -> None:
    if max_tokens is _SETTINGS_DEFAULT or temperature is _SETTINGS_DEFAULT:
        settings = _settings()
        if max_tokens is _SETTINGS_DEFAULT:
            object.__setattr__(config, "max_tokens", settings.LLM_CONFIG_MAX_TOKENS)
        if temperature is _SETTINGS_DEFAULT:
            object.__setattr__(config, "temperature", settings.LLM_CONFIG_TEMPERATURE)

    _assert_settings_defaults_resolved(
        config.max_tokens,  # type: ignore[attr-defined]
        config.temperature,  # type: ignore[attr-defined]
    )


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
    extra_headers: dict[str, str] | None
    timeout: float | None


def _settings() -> Any:
    # Keep settings resolution lazy so importing skyvern.schemas.llm only defines the
    # public dataclasses; config defaults are read when a config is constructed.
    from skyvern.settings_manager import SettingsManager  # noqa: PLC0415

    return SettingsManager.get_settings()


@dataclass(frozen=True)
class LLMConfigBase:
    model_name: str
    required_env_vars: list[str]
    supports_vision: bool
    add_assistant_prefix: bool

    def get_missing_env_vars(self) -> list[str]:
        settings = _settings()
        missing_env_vars = []
        for env_var in self.required_env_vars:
            env_var_value = getattr(settings, env_var, None)
            if not env_var_value:
                missing_env_vars.append(env_var)

        return missing_env_vars


@dataclass(frozen=True)
class LLMConfig(LLMConfigBase):
    """Base-safe LLM config shared by SDK and server import paths.

    Default max-token and temperature fields use a private sentinel so callers can
    still pass None explicitly. __post_init__ must replace the sentinel before the
    frozen dataclass instance is observable.
    """

    litellm_params: LiteLLMParams | None = field(default=None)
    max_tokens: int | None = _DEFAULT_MAX_TOKENS
    max_completion_tokens: int | None = None
    temperature: float | None = _DEFAULT_TEMPERATURE
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        _resolve_generation_defaults(self, self.max_tokens, self.temperature)


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
    """Base-safe router config with the same settings-default sentinel invariant as LLMConfig."""

    model_list: list[LLMRouterModelConfig]
    # All three redis parameters are required. Even if there isn't a password, it should be an empty string.
    main_model_group: str
    redis_host: str | None = None
    redis_port: int | None = None
    redis_password: str | None = None
    fallback_model_group: str | list[str] | None = None
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
    max_tokens: int | None = _DEFAULT_MAX_TOKENS
    max_completion_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = _DEFAULT_TEMPERATURE

    def __post_init__(self) -> None:
        _resolve_generation_defaults(self, self.max_tokens, self.temperature)
