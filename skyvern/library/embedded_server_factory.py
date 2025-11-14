from typing import Any

import httpx
from httpx import ASGITransport


def create_embedded_server(
    openai_api_key: str | None = None,
    anthropic_api_key: str | None = None,
    azure_api_key: str | None = None,
    azure_deployment: str | None = None,
    azure_api_base: str | None = None,
    azure_api_version: str | None = None,
    gemini_api_key: str | None = None,
    vertex_credentials: str | None = None,
    vertex_project_id: str | None = None,
    vertex_location: str | None = None,
    groq_api_key: str | None = None,
    groq_model: str | None = None,
    groq_api_base: str | None = None,
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
                if azure_api_key:
                    settings.AZURE_API_KEY = azure_api_key
                    if azure_deployment:
                        settings.AZURE_DEPLOYMENT = azure_deployment
                    if azure_api_base:
                        settings.AZURE_API_BASE = azure_api_base
                    if azure_api_version:
                        settings.AZURE_API_VERSION = azure_api_version
                    settings.ENABLE_AZURE = True

                # Gemini
                if gemini_api_key:
                    settings.GEMINI_API_KEY = gemini_api_key
                    settings.ENABLE_GEMINI = True

                # Vertex AI
                if vertex_credentials:
                    settings.VERTEX_CREDENTIALS = vertex_credentials
                    if vertex_project_id:
                        settings.VERTEX_PROJECT_ID = vertex_project_id
                    if vertex_location:
                        settings.VERTEX_LOCATION = vertex_location
                    settings.ENABLE_VERTEX_AI = True

                # Groq
                if groq_api_key:
                    settings.GROQ_API_KEY = groq_api_key
                    if groq_model:
                        settings.GROQ_MODEL = groq_model
                    if groq_api_base:
                        settings.GROQ_API_BASE = groq_api_base
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
