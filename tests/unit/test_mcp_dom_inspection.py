"""Tests for MCP DOM inspection tools (get_html, get_value, get_styles)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import inspection as mcp_inspection

# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════


def _make_mock_page(url: str = "https://example.com") -> MagicMock:
    page = MagicMock()
    page.url = url
    locator = MagicMock()
    locator.evaluate = AsyncMock(return_value="<span>hello</span>")
    locator.input_value = AsyncMock(return_value="test-value")
    page.locator = MagicMock(return_value=locator)
    return page


def _make_skyvern_page(page: MagicMock) -> MagicMock:
    """Mimic SkyvernBrowserPage which delegates attribute access to the raw page."""
    wrapper = MagicMock()
    wrapper.page = page
    wrapper.locator = page.locator
    wrapper.url = page.url
    return wrapper


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    skyvern_page = _make_skyvern_page(page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_inspection, "get_page", mock)
    return mock


# ═══════════════════════════════════════════════════
# skyvern_get_html
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_html_inner(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value="<span>hello</span>")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_html(selector="#content")

    assert result["ok"] is True
    assert result["data"]["html"] == "<span>hello</span>"
    assert result["data"]["outer"] is False
    assert result["data"]["length"] == len("<span>hello</span>")
    page.locator.assert_called_with("#content")


@pytest.mark.asyncio
async def test_get_html_outer(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value='<div id="content"><span>hello</span></div>')
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_html(selector="#content", outer=True)

    assert result["ok"] is True
    assert result["data"]["outer"] is True
    assert "<div" in result["data"]["html"]


@pytest.mark.asyncio
async def test_get_html_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_inspection.skyvern_get_html(selector="#x")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_html_bad_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(side_effect=RuntimeError("Element not found"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_html(selector="#nonexistent")

    assert result["ok"] is False
    assert "Element not found" in result["error"]["message"]


@pytest.mark.asyncio
async def test_get_html_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value="")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_html(selector="#empty")

    assert result["ok"] is True
    assert result["data"]["html"] == ""
    assert result["data"]["length"] == 0


# ═══════════════════════════════════════════════════
# skyvern_get_value
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_value(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.input_value = AsyncMock(return_value="user@example.com")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_value(selector="#email")

    assert result["ok"] is True
    assert result["data"]["value"] == "user@example.com"
    assert result["data"]["selector"] == "#email"


@pytest.mark.asyncio
async def test_get_value_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.input_value = AsyncMock(return_value="")
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_value(selector="#empty-input")

    assert result["ok"] is True
    assert result["data"]["value"] == ""


@pytest.mark.asyncio
async def test_get_value_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_inspection.skyvern_get_value(selector="#x")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_value_not_input(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.input_value = AsyncMock(side_effect=RuntimeError("Not an input element"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_value(selector="#div-element")

    assert result["ok"] is False
    assert "Not an input element" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# skyvern_get_styles
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_styles_specific_props(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value={"color": "rgb(0, 0, 0)", "font-size": "16px"})
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_styles(selector="#heading", properties=["color", "font-size"])

    assert result["ok"] is True
    assert result["data"]["styles"]["color"] == "rgb(0, 0, 0)"
    assert result["data"]["styles"]["font-size"] == "16px"
    assert result["data"]["count"] == 2


@pytest.mark.asyncio
async def test_get_styles_all(monkeypatch: pytest.MonkeyPatch) -> None:
    styles = {f"prop-{i}": f"value-{i}" for i in range(50)}
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value=styles)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_styles(selector="body")

    assert result["ok"] is True
    assert result["data"]["count"] == 50


@pytest.mark.asyncio
async def test_get_styles_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_inspection.skyvern_get_styles(selector="#x")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_styles_bad_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(side_effect=RuntimeError("Selector not found"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_styles(selector="#nope", properties=["color"])

    assert result["ok"] is False
    assert "Selector not found" in result["error"]["message"]


@pytest.mark.asyncio
async def test_get_styles_empty_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.locator.return_value.evaluate = AsyncMock(return_value={})
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_inspection.skyvern_get_styles(selector="#hidden", properties=[])

    assert result["ok"] is True
    assert result["data"]["count"] == 0
