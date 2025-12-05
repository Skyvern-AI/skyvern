import os
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv
from httpx import ASGITransport

from skyvern.config import settings
from skyvern.forge.api_app import create_api_app, create_fast_api_app
from skyvern.forge.forge_app_initializer import setup_local_organization, start_forge_app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig
from skyvern.forge.sdk.db.client import AgentDB

LOG = structlog.get_logger()


def create_embedded_server(
    llm_config: LLMRouterConfig | LLMConfig | None = None,
    settings_overrides: dict[str, Any] | None = None,
    use_in_memory_db: bool = False,
) -> httpx.AsyncClient:
    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None
            self._api_key: str | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                load_dotenv(".env")

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

                if use_in_memory_db:
                    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

                    from skyvern.forge.sdk.db.models import Base  # noqa: PLC0415

                    settings.DATABASE_STRING = "sqlite+aiosqlite:///:memory:"
                    engine = create_async_engine(settings.DATABASE_STRING)
                    async with engine.begin() as conn:
                        await conn.run_sync(Base.metadata.create_all)

                    db = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE, db_engine=engine)
                    forge_app_instance = start_forge_app(db)
                    api_app = create_fast_api_app(forge_app_instance)
                    self._api_key = await setup_local_organization()
                    LOG.info("Embedded server initialized with in-memory database")
                else:
                    self._api_key = os.getenv("SKYVERN_API_KEY")
                    if not self._api_key:
                        raise ValueError(
                            "SKYVERN_API_KEY is not set. Provide api_key or set SKYVERN_API_KEY in .env file."
                        )
                    api_app = create_api_app()

                self._transport = ASGITransport(app=api_app)

            if self._api_key and "x-api-key" not in request.headers:
                request.headers["x-api-key"] = self._api_key

            response = await self._transport.handle_async_request(request)
            return response

    return httpx.AsyncClient(transport=EmbeddedServerTransport(), base_url="http://skyvern-embedded")
