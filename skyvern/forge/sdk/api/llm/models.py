from dataclasses import dataclass
from typing import Any, Awaitable, Protocol

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


class LLMAPIHandler(Protocol):
    def __call__(
        self,
        prompt: str,
        step: Step | None = None,
        screenshots: list[bytes] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Awaitable[dict[str, Any]]:
        ...
