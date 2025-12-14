import os
from typing import Any

import httpx
from httpx import ASGITransport

from skyvern.config import settings
from skyvern.forge.api_app import create_api_app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig


def create_embedded_server(
    llm_config: LLMRouterConfig | LLMConfig | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None
            self._api_key: str | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                settings.BROWSER_LOGS_ENABLED = False

                if llm_config:
                    LLMConfigRegistry.register_config(
                        "CUSTOM_LLM",
                        llm_config,
                    )
                    settings.LLM_KEY = "CUSTOM_LLM"

                # Apply custom settings overrides
                if settings_overrides:
                    for key, value in settings_overrides.items():
                        if hasattr(settings, key):
                            setattr(settings, key, value)
                        else:
                            raise ValueError(f"Invalid setting: {key}")

                self._api_key = os.getenv("SKYVERN_API_KEY")
                if not self._api_key:
                    raise ValueError("SKYVERN_API_KEY is not set. Provide api_key or set SKYVERN_API_KEY in .env file.")
                api_app = create_api_app()

                self._transport = ASGITransport(app=api_app)

            if self._api_key and "x-api-key" not in request.headers:
                request.headers["x-api-key"] = self._api_key

            response = await self._transport.handle_async_request(request)
            return response

    return httpx.AsyncClient(transport=EmbeddedServerTransport(), base_url="http://skyvern-embedded")
