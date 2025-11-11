import httpx
from httpx import ASGITransport

from skyvern.client import AsyncSkyvern, SkyvernEnvironment
from skyvern.config import settings
from skyvern.forge.api_app import app


def create_embedded_server(
    api_key: str,
    open_api_key: str | None,
) -> AsyncSkyvern:
    settings.BROWSER_LOGS_ENABLED = False

    if open_api_key:
        settings.OPENAI_API_KEY = open_api_key

    transport = ASGITransport(app=app)
    return AsyncSkyvern(
        environment=SkyvernEnvironment.LOCAL,
        api_key=api_key,
        httpx_client=httpx.AsyncClient(transport=transport, base_url="http://skyvern-embedded"),
    )
