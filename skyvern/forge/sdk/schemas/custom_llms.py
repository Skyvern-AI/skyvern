from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from typing_extensions import Self

from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken

CUSTOM_LLM_API_KEY_MASK = "********"


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
                self.api_base = "https://openrouter.ai/api/v1"
        elif self.provider is CustomLLMProvider.OLLAMA and not self.api_base:
            self.api_base = "http://localhost:11434"

        if self.api_base:
            parsed = urlparse(self.api_base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("api_base must be an HTTP(S) URL")

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
