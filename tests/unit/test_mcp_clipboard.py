"""Tests for MCP clipboard tools (skyvern_clipboard_read, skyvern_clipboard_write)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext

# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════


def _make_mock_page(url: str = "https://example.com") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.page = page  # SkyvernBrowserPage wraps raw page
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    return page


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    from skyvern.cli.mcp_tools import browser as mcp_browser

    mock = AsyncMock(return_value=(page, ctx))
    monkeypatch.setattr(mcp_browser, "get_page", mock)
    return mock


# ═══════════════════════════════════════════════════
# _ensure_clipboard_permissions
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_clipboard_permissions_calls_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import _ensure_clipboard_permissions

    page = _make_mock_page()
    await _ensure_clipboard_permissions(page)
    page.context.grant_permissions.assert_awaited_once_with(["clipboard-read", "clipboard-write"])


@pytest.mark.asyncio
async def test_ensure_clipboard_permissions_survives_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import _ensure_clipboard_permissions

    page = _make_mock_page()
    page.context.grant_permissions = AsyncMock(side_effect=Exception("not supported"))
    # Should not raise
    await _ensure_clipboard_permissions(page)


# ═══════════════════════════════════════════════════
# skyvern_clipboard_read
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clipboard_read_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value="hello world")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is True
    assert result["data"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_clipboard_read_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value="")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is True
    assert result["data"]["text"] == ""


@pytest.mark.asyncio
async def test_clipboard_read_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools import browser as mcp_browser
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

    result = await skyvern_clipboard_read()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clipboard_read_evaluate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=Exception("Clipboard API not available"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is False
    assert "Clipboard API not available" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# skyvern_clipboard_write
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clipboard_write_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=None)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_write(text="copied text")

    assert result["ok"] is True
    assert result["data"]["written"] is True
    assert result["data"]["length"] == 11


@pytest.mark.asyncio
async def test_clipboard_write_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=None)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_write(text="")

    assert result["ok"] is True
    assert result["data"]["written"] is True
    assert result["data"]["length"] == 0


@pytest.mark.asyncio
async def test_clipboard_write_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools import browser as mcp_browser
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

    result = await skyvern_clipboard_write(text="hello")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clipboard_write_evaluate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=Exception("Permission denied"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await skyvern_clipboard_write(text="hello")

    assert result["ok"] is False
    assert "Permission denied" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# Roundtrip
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clipboard_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write → Read roundtrip using a simulated clipboard."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read, skyvern_clipboard_write

    clipboard_store: dict[str, str] = {"text": ""}

    page = _make_mock_page()

    async def mock_evaluate(expr: Any, *args: Any) -> Any:
        if "writeText" in str(expr):
            clipboard_store["text"] = args[0] if args else ""
            return None
        if "readText" in str(expr):
            return clipboard_store["text"]
        return None

    page.evaluate = mock_evaluate
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    # Write
    write_result = await skyvern_clipboard_write(text="roundtrip test")
    assert write_result["ok"] is True

    # Read back
    read_result = await skyvern_clipboard_read()
    assert read_result["ok"] is True
    assert read_result["data"]["text"] == "roundtrip test"
