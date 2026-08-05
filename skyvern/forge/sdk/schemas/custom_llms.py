import json
from enum import StrEnum
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import Self

from skyvern.exceptions import BlockedHost, SkyvernHTTPException
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.utils.url_validators import validate_url

CUSTOM_LLM_API_KEY_MASK = "********"
# extra_parameters keys whose nested values are credentials (e.g. Authorization / X-API-Key
# headers). Their values are masked in API responses and restored from the stored config when
# a masked value is saved back unchanged, mirroring how the top-level api_key is handled.
SECRET_EXTRA_PARAMETER_KEYS = frozenset({"extra_headers"})
OLLAMA_DEFAULT_API_BASE = "http://localhost:11434"
OPENROUTER_DEFAULT_API_BASE = "https://openrouter.ai/api/v1"

MAX_EXTRA_PARAMETER_COUNT = 30
MAX_EXTRA_PARAMETER_KEY_LENGTH = 128
MAX_EXTRA_PARAMETERS_SERIALIZED_BYTES = 10_000
# Keys litellm derives from the config itself or that carry the prompt; letting a
# customer override them would break the completion call or leak the wrong credentials.
# `drop_params`/`stream`/`tools` are passed explicitly at the invocation boundary, so a
# config value would arrive as a duplicate keyword and raise TypeError before dispatch
# (`stream` would also swap the response for a stream the handler cannot parse).
RESERVED_EXTRA_PARAMETER_KEYS = frozenset(
    {
        "model",
        "messages",
        "api_key",
        "api_base",
        "api_version",
        "model_info",
        "custom_llm_provider",
        "drop_params",
        "stream",
        "tools",
    }
)


class CustomLLMProvider(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"


class CustomLLMConfig(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)
    provider: CustomLLMProvider
    model_name: str = Field(..., min_length=1, max_length=250)
    api_base: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    api_version: str | None = Field(default=None, max_length=100)
    supports_vision: bool = True
    add_assistant_prefix: bool = False
    max_completion_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: str | None = Field(default=None, max_length=50)
    extra_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra_parameters", mode="before")
    @classmethod
    def validate_extra_parameters(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("extra_parameters must be a JSON object")
        if len(value) > MAX_EXTRA_PARAMETER_COUNT:
            raise ValueError(f"extra_parameters supports at most {MAX_EXTRA_PARAMETER_COUNT} keys")

        cleaned: dict[str, Any] = {}
        for raw_key, param_value in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("extra_parameters keys must be strings")
            key = raw_key.strip()
            if not key:
                raise ValueError("extra_parameters keys must not be empty")
            if len(key) > MAX_EXTRA_PARAMETER_KEY_LENGTH:
                raise ValueError(f"extra_parameters keys must be at most {MAX_EXTRA_PARAMETER_KEY_LENGTH} characters")
            if key.lower() in RESERVED_EXTRA_PARAMETER_KEYS:
                raise ValueError(f"extra_parameters key '{key}' is reserved and cannot be overridden")
            cleaned[key] = param_value

        try:
            serialized = json.dumps(cleaned)
        except (TypeError, ValueError) as exc:
            raise ValueError("extra_parameters must be JSON-serializable") from exc
        if len(serialized.encode("utf-8")) > MAX_EXTRA_PARAMETERS_SERIALIZED_BYTES:
            raise ValueError(f"extra_parameters must be at most {MAX_EXTRA_PARAMETERS_SERIALIZED_BYTES} bytes")

        return cleaned

    @field_validator(
        "display_name",
        "model_name",
        "api_base",
        "api_key",
        "api_version",
        "reasoning_effort",
        mode="before",
    )
    @classmethod
    def strip_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def validate_provider_config(self) -> Self:
        settings = SettingsManager.get_settings()

        if not self.display_name:
            raise ValueError("display_name is required")
        if not self.model_name:
            raise ValueError("model_name is required")

        if self.provider is CustomLLMProvider.OPENAI_COMPATIBLE:
            if not self.api_base:
                raise ValueError("api_base is required for OpenAI-compatible models")
            if not self.api_key:
                raise ValueError("api_key is required for OpenAI-compatible models")
        elif self.provider is CustomLLMProvider.OPENROUTER:
            if not self.api_key:
                raise ValueError("api_key is required for OpenRouter models")
            if not self.api_base:
                # Provider defaults are applied before URL validation.
                self.api_base = OPENROUTER_DEFAULT_API_BASE
        elif self.provider is CustomLLMProvider.GEMINI:
            # Gemini talks to Google's fixed endpoint via the API key, so no api_base.
            if not self.api_key:
                raise ValueError("api_key is required for Gemini models")
        elif self.provider is CustomLLMProvider.OLLAMA and not self.api_base:
            # Self-hosted users commonly run Ollama locally; Cloud disables local/private
            # API bases via ALLOW_CUSTOM_LLM_LOCAL_API_BASES=False below.
            self.api_base = OLLAMA_DEFAULT_API_BASE

        if self.api_base:
            if settings.ALLOW_CUSTOM_LLM_LOCAL_API_BASES:
                parsed = urlparse(self.api_base)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError("api_base must be an HTTP(S) URL")
            else:
                try:
                    validated_url = validate_url(self.api_base)
                except BlockedHost as exc:
                    message = exc.message or "api_base host is blocked by SSRF protection"
                    raise ValueError(message) from exc
                except SkyvernHTTPException as exc:
                    if getattr(exc, "status_code", None) == HTTPStatus.BAD_REQUEST:
                        raise ValueError(getattr(exc, "message", None) or "api_base must be an HTTP(S) URL") from exc
                    raise
                if not validated_url:
                    raise ValueError("api_base must be an HTTP(S) URL")
                self.api_base = validated_url

        return self


class CustomLLM(BaseModel):
    id: str
    organization_id: str
    config: CustomLLMConfig
    created_at: str
    modified_at: str
    valid: bool


class CustomLLMListResponse(BaseModel):
    custom_llms: list[CustomLLM]


class CustomLLMResponse(BaseModel):
    custom_llm: CustomLLM


class CustomLLMCreateRequest(BaseModel):
    config: CustomLLMConfig


class CustomLLMUpdateRequest(BaseModel):
    config: CustomLLMConfig


def custom_llm_from_org_auth_token(token: OrganizationAuthToken) -> CustomLLM:
    config = CustomLLMConfig.model_validate_json(token.token)
    return CustomLLM(
        id=token.id,
        organization_id=token.organization_id,
        config=config,
        created_at=token.created_at.isoformat(),
        modified_at=token.modified_at.isoformat(),
        valid=token.valid,
    )


def _mask_secret_extra_parameters(extra_parameters: dict[str, Any]) -> dict[str, Any]:
    masked = dict(extra_parameters)
    for key in SECRET_EXTRA_PARAMETER_KEYS:
        value = masked.get(key)
        if isinstance(value, dict) and value:
            masked[key] = {name: CUSTOM_LLM_API_KEY_MASK for name in value}
    return masked


def _restore_secret_extra_parameters(
    new_parameters: dict[str, Any], existing_parameters: dict[str, Any]
) -> dict[str, Any]:
    restored = dict(new_parameters)
    for key in SECRET_EXTRA_PARAMETER_KEYS:
        new_value = restored.get(key)
        existing_value = existing_parameters.get(key)
        if isinstance(new_value, dict) and isinstance(existing_value, dict):
            restored[key] = {
                name: (existing_value[name] if value == CUSTOM_LLM_API_KEY_MASK and name in existing_value else value)
                for name, value in new_value.items()
            }
    return restored


def custom_llm_response_from_org_auth_token(token: OrganizationAuthToken) -> CustomLLM:
    custom_llm = custom_llm_from_org_auth_token(token)
    config_updates: dict[str, Any] = {}
    if custom_llm.config.api_key:
        config_updates["api_key"] = CUSTOM_LLM_API_KEY_MASK
    masked_extra = _mask_secret_extra_parameters(custom_llm.config.extra_parameters)
    if masked_extra != custom_llm.config.extra_parameters:
        config_updates["extra_parameters"] = masked_extra
    if config_updates:
        custom_llm.config = custom_llm.config.model_copy(update=config_updates)
    return custom_llm


def config_with_preserved_secrets(new_config: CustomLLMConfig, existing_config: CustomLLMConfig) -> CustomLLMConfig:
    """Restore masked secrets a client saved back unchanged (top-level api_key and secret headers)."""
    config_updates: dict[str, Any] = {}
    if new_config.api_key == CUSTOM_LLM_API_KEY_MASK:
        config_updates["api_key"] = existing_config.api_key
    restored_extra = _restore_secret_extra_parameters(new_config.extra_parameters, existing_config.extra_parameters)
    if restored_extra != new_config.extra_parameters:
        config_updates["extra_parameters"] = restored_extra
    if not config_updates:
        return new_config
    return CustomLLMConfig.model_validate({**new_config.model_dump(), **config_updates})
