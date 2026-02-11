from typing import Any

import httpx


class AsyncHttpClient:
    """
    A wrapper of httpx client.
    Functionalities:
        1. It comes with a default httpx.AsyncClient instance.
        2. The httx client could be replaced by a set_client method.
    """

    def __init__(self, proxy: str | None = None) -> None:
        self.client = httpx.AsyncClient(proxy=proxy)

    async def get(self, url: str) -> httpx.Response:
        return await self.client.get(url)

    async def post(self, url: str, data: dict[Any, Any] | None = None) -> httpx.Response:
        return await self.client.post(url, data=data)

    async def close(self) -> None:
        await self.client.aclose()
