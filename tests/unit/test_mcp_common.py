from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import skyvern.cli.mcp_tools._common as common_tools


@pytest.mark.asyncio
async def test_raw_http_get_returns_empty_dict_for_204(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        status_code=204,
        text="",
        json=Mock(side_effect=AssertionError("json() should not be called for 204 responses")),
    )
    fake_client = SimpleNamespace(
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=AsyncMock(return_value=response)))
    )
    monkeypatch.setattr("skyvern.cli.mcp_tools._session.get_skyvern", lambda: fake_client)

    result = await common_tools.raw_http_get("v1/test")

    assert result == {}


@pytest.mark.asyncio
async def test_raw_http_get_returns_raw_text_for_non_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        status_code=200,
        text="<html>ok</html>",
        json=Mock(side_effect=ValueError("not json")),
    )
    fake_client = SimpleNamespace(
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=AsyncMock(return_value=response)))
    )
    monkeypatch.setattr("skyvern.cli.mcp_tools._session.get_skyvern", lambda: fake_client)

    result = await common_tools.raw_http_get("v1/test")

    assert result == {"raw": "<html>ok</html>"}
