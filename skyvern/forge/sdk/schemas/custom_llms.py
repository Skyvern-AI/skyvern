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
OLLAMA_DEFAULT_API_BASE = "http://localhost:11434"
OPENROUTER_DEFAULT_API_BASE = "https://openrouter.ai/api/v1"


class CustomLLMProvider(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"


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


def custom_llm_response_from_org_auth_token(token: OrganizationAuthToken) -> CustomLLM:
    custom_llm = custom_llm_from_org_auth_token(token)
    if custom_llm.config.api_key:
        custom_llm.config = custom_llm.config.model_copy(update={"api_key": CUSTOM_LLM_API_KEY_MASK})
    return custom_llm
