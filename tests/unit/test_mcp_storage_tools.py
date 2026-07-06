"""Tests for MCP web storage tools (sessionStorage + localStorage clear)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import storage as mcp_storage
from tests.unit._mcp_browser_fakes import make_mock_page as _make_mock_page
from tests.unit._mcp_browser_fakes import patch_get_page

# ═══════════════════════════════════════════════════
# skyvern_get_session_storage
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_session_storage_all(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value={"token": "abc", "lang": "en"})
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_get_session_storage()

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert result["data"]["items"]["token"] == "abc"


@pytest.mark.asyncio
async def test_get_session_storage_specific_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=["abc", None])
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_get_session_storage(keys=["token", "missing"])

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"] == {"token": "abc"}


@pytest.mark.asyncio
async def test_get_session_storage_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value={})
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_get_session_storage()

    assert result["ok"] is True
    assert result["data"]["count"] == 0


@pytest.mark.asyncio
async def test_get_session_storage_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_storage, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_storage.skyvern_get_session_storage()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_session_storage_evaluate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=RuntimeError("page crashed"))
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_get_session_storage()

    assert result["ok"] is False
    assert "page crashed" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# skyvern_set_session_storage
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_set_session_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=None)
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_set_session_storage(key="theme", value="dark")

    assert result["ok"] is True
    assert result["data"]["key"] == "theme"
    assert result["data"]["value_length"] == 4
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_session_storage_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_storage, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_storage.skyvern_set_session_storage(key="k", value="v")
    assert result["ok"] is False


# ═══════════════════════════════════════════════════
# skyvern_clear_session_storage
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clear_session_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=3)
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_clear_session_storage()

    assert result["ok"] is True
    assert result["data"]["cleared_count"] == 3


@pytest.mark.asyncio
async def test_clear_session_storage_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_storage, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_storage.skyvern_clear_session_storage()
    assert result["ok"] is False


# ═══════════════════════════════════════════════════
# skyvern_clear_local_storage
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clear_local_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=5)
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_clear_local_storage()

    assert result["ok"] is True
    assert result["data"]["cleared_count"] == 5


@pytest.mark.asyncio
async def test_clear_local_storage_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_storage, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_storage.skyvern_clear_local_storage()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clear_local_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=RuntimeError("security error"))
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_storage, page, ctx)

    result = await mcp_storage.skyvern_clear_local_storage()

    assert result["ok"] is False
    assert "security error" in result["error"]["message"]
