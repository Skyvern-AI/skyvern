from collections.abc import Callable
from typing import Any

import httpx
import pytest

from skyvern.forge.sdk.services import google_sheets_service


@pytest.fixture
def mock_sheets_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(google_sheets_service.httpx, "AsyncClient", fake_async_client)

    return install
