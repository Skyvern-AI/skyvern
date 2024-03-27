from dataclasses import dataclass
from typing import Any, Awaitable, Literal, Protocol

from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.settings_manager import SettingsManager


@dataclass(frozen=True)
class LLMConfig:
    model_name: str
    required_env_vars: list[str]
    supports_vision: bool

    def get_missing_env_vars(self) -> list[str]:
        missing_env_vars = []
        for env_var in self.required_env_vars:
            env_var_value = getattr(SettingsManager.get_settings(), env_var, None)
            if not env_var_value:
                missing_env_vars.append(env_var)

        return missing_env_vars


@dataclass(frozen=True)
class LLMRouterModelConfig:
    model_name: str
    # https://litellm.vercel.app/docs/routing
    litellm_params: dict[str, Any]
    tpm: int | None = None
    rpm: int | None = None


@dataclass(frozen=True)
class LLMRouterConfig(LLMConfig):
    model_list: list[LLMRouterModelConfig]
    redis_host: str
    redis_port: int
    main_model_group: str
    fallback_model_group: str | None = None
    routing_strategy: Literal[
        "simple-shuffle",
        "least-busy",
        "usage-based-routing",
        "latency-based-routing",
    ] = "usage-based-routing"
    num_retries: int = 2
    retry_delay_seconds: int = 15
    set_verbose: bool = True


class LLMAPIHandler(Protocol):
    def __call__(
        self,
        prompt: str,
        step: Step | None = None,
        screenshots: list[bytes] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Awaitable[dict[str, Any]]:
        ...
