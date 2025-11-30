from typing import Any

import httpx
from httpx import ASGITransport

from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig


def create_embedded_server(
    llm_config: LLMRouterConfig | LLMConfig | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                from skyvern.config import settings  # noqa: PLC0415

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

                from skyvern.forge.api_app import create_api_app  # noqa: PLC0415

                self._transport = ASGITransport(app=create_api_app())

            response = await self._transport.handle_async_request(request)
            return response

    return httpx.AsyncClient(transport=EmbeddedServerTransport(), base_url="http://skyvern-embedded")
