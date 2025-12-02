import httpx
from httpx import ASGITransport


def create_embedded_server(
    openai_api_key: str | None,
) -> httpx.AsyncClient:
    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                from skyvern.config import settings  # noqa: PLC0415

                settings.BROWSER_LOGS_ENABLED = False

                if openai_api_key:
                    settings.OPENAI_API_KEY = openai_api_key

                from skyvern.forge.api_app import create_api_app  # noqa: PLC0415

                self._transport = ASGITransport(app=create_api_app())

            response = await self._transport.handle_async_request(request)
            return response

    return httpx.AsyncClient(transport=EmbeddedServerTransport(), base_url="http://skyvern-embedded")
