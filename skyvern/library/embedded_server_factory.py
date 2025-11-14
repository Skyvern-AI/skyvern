from typing import Any

import httpx
from httpx import ASGITransport

from skyvern.library.llm_config_types import AzureConfig, GroqConfig, VertexConfig


def create_embedded_server(
    openai_api_key: str | None = None,
    anthropic_api_key: str | None = None,
    azure_config: AzureConfig | None = None,
    gemini_api_key: str | None = None,
    vertex_config: VertexConfig | None = None,
    groq_config: GroqConfig | None = None,
    llm_key: str | None = None,
    secondary_llm_key: str | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                from skyvern.config import settings  # noqa: PLC0415

                settings.BROWSER_LOGS_ENABLED = False

                # OpenAI
                if openai_api_key:
                    settings.OPENAI_API_KEY = openai_api_key
                    settings.ENABLE_OPENAI = True

                # Anthropic
                if anthropic_api_key:
                    settings.ANTHROPIC_API_KEY = anthropic_api_key
                    settings.ENABLE_ANTHROPIC = True

                # Azure
                if azure_config and azure_config.api_key:
                    settings.AZURE_API_KEY = azure_config.api_key
                    if azure_config.deployment:
                        settings.AZURE_DEPLOYMENT = azure_config.deployment
                    if azure_config.api_base:
                        settings.AZURE_API_BASE = azure_config.api_base
                    if azure_config.api_version:
                        settings.AZURE_API_VERSION = azure_config.api_version
                    settings.ENABLE_AZURE = True

                # Gemini
                if gemini_api_key:
                    settings.GEMINI_API_KEY = gemini_api_key
                    settings.ENABLE_GEMINI = True

                # Vertex AI
                if vertex_config and vertex_config.credentials:
                    settings.VERTEX_CREDENTIALS = vertex_config.credentials
                    if vertex_config.project_id:
                        settings.VERTEX_PROJECT_ID = vertex_config.project_id
                    if vertex_config.location:
                        settings.VERTEX_LOCATION = vertex_config.location
                    settings.ENABLE_VERTEX_AI = True

                # Groq
                if groq_config and groq_config.api_key:
                    settings.GROQ_API_KEY = groq_config.api_key
                    if groq_config.model:
                        settings.GROQ_MODEL = groq_config.model
                    if groq_config.api_base:
                        settings.GROQ_API_BASE = groq_config.api_base
                    settings.ENABLE_GROQ = True

                # LLM configuration
                if llm_key:
                    settings.LLM_KEY = llm_key
                if secondary_llm_key:
                    settings.SECONDARY_LLM_KEY = secondary_llm_key

                # Apply custom settings overrides
                if settings_overrides:
                    for key, value in settings_overrides.items():
                        if hasattr(settings, key):
                            setattr(settings, key, value)
                        else:
                            raise ValueError(f"Invalid setting: {key}")

                from skyvern.forge.api_app import app  # noqa: PLC0415

                self._transport = ASGITransport(app=app)

            response = await self._transport.handle_async_request(request)
            return response

    return httpx.AsyncClient(transport=EmbeddedServerTransport(), base_url="http://skyvern-embedded")
