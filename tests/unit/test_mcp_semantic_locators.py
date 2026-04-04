"""Tests for MCP semantic locator tool (skyvern_find) and do_find."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import LOCATOR_TYPES, FindResult, do_find
from skyvern.cli.core.guards import GuardError
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser

# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════


def _make_locator(count: int = 1, text: str = "Submit", visible: bool = True) -> MagicMock:
    locator = MagicMock()
    locator.count = AsyncMock(return_value=count)
    first = MagicMock()
    first.text_content = AsyncMock(return_value=text)
    first.is_visible = AsyncMock(return_value=visible)
    locator.first = first
    return locator


def _make_mock_page(locator: MagicMock | None = None) -> MagicMock:
    page = MagicMock()
    page.url = "https://example.com"
    loc = locator or _make_locator()
    page.get_by_role = MagicMock(return_value=loc)
    page.get_by_text = MagicMock(return_value=loc)
    page.get_by_label = MagicMock(return_value=loc)
    page.get_by_placeholder = MagicMock(return_value=loc)
    page.get_by_alt_text = MagicMock(return_value=loc)
    page.get_by_test_id = MagicMock(return_value=loc)
    return page


def _make_skyvern_page(page: MagicMock) -> MagicMock:
    wrapper = MagicMock()
    wrapper.page = page
    wrapper.url = page.url
    # Delegate semantic locator methods
    wrapper.get_by_role = page.get_by_role
    wrapper.get_by_text = page.get_by_text
    wrapper.get_by_label = page.get_by_label
    wrapper.get_by_placeholder = page.get_by_placeholder
    wrapper.get_by_alt_text = page.get_by_alt_text
    wrapper.get_by_test_id = page.get_by_test_id
    return wrapper


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    skyvern_page = _make_skyvern_page(page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_browser, "get_page", mock)
    return mock


# ═══════════════════════════════════════════════════
# do_find (browser_ops)
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_do_find_by_role() -> None:
    page = _make_mock_page()
    result = await do_find(page, by="role", value="button")

    assert isinstance(result, FindResult)
    assert result.count == 1
    assert result.first_text == "Submit"
    assert result.first_visible is True
    assert "role" in result.selector
    page.get_by_role.assert_called_once_with("button")


@pytest.mark.asyncio
async def test_do_find_by_text() -> None:
    page = _make_mock_page()
    result = await do_find(page, by="text", value="Click me")

    page.get_by_text.assert_called_once_with("Click me")
    assert result.count == 1


@pytest.mark.asyncio
async def test_do_find_by_label() -> None:
    page = _make_mock_page()
    await do_find(page, by="label", value="Email")

    page.get_by_label.assert_called_once_with("Email")


@pytest.mark.asyncio
async def test_do_find_by_placeholder() -> None:
    page = _make_mock_page()
    await do_find(page, by="placeholder", value="Enter email")

    page.get_by_placeholder.assert_called_once_with("Enter email")


@pytest.mark.asyncio
async def test_do_find_by_alt() -> None:
    page = _make_mock_page()
    result = await do_find(page, by="alt", value="Logo")

    page.get_by_alt_text.assert_called_once_with("Logo")
    assert result.selector == "get_by_alt_text('Logo')"


@pytest.mark.asyncio
async def test_do_find_by_testid() -> None:
    page = _make_mock_page()
    result = await do_find(page, by="testid", value="submit-btn")

    page.get_by_test_id.assert_called_once_with("submit-btn")
    assert result.selector == "get_by_test_id('submit-btn')"


@pytest.mark.asyncio
async def test_do_find_invalid_type() -> None:
    page = _make_mock_page()
    with pytest.raises(GuardError, match="Invalid locator type"):
        await do_find(page, by="invalid", value="anything")


@pytest.mark.asyncio
async def test_do_find_no_matches() -> None:
    locator = _make_locator(count=0)
    page = _make_mock_page(locator)

    result = await do_find(page, by="role", value="dialog")

    assert result.count == 0
    assert result.first_text is None
    assert result.first_visible is False


@pytest.mark.asyncio
async def test_do_find_multiple_matches() -> None:
    locator = _make_locator(count=5, text="Item 1")
    page = _make_mock_page(locator)

    result = await do_find(page, by="text", value="Item")

    assert result.count == 5
    assert result.first_text == "Item 1"


@pytest.mark.asyncio
async def test_do_find_hidden_element() -> None:
    locator = _make_locator(count=1, text="Hidden", visible=False)
    page = _make_mock_page(locator)

    result = await do_find(page, by="text", value="Hidden")

    assert result.first_visible is False


def test_locator_types_constant() -> None:
    assert "role" in LOCATOR_TYPES
    assert "text" in LOCATOR_TYPES
    assert "label" in LOCATOR_TYPES
    assert "placeholder" in LOCATOR_TYPES
    assert "alt" in LOCATOR_TYPES
    assert "testid" in LOCATOR_TYPES
    assert len(LOCATOR_TYPES) == 6


# ═══════════════════════════════════════════════════
# skyvern_find (MCP tool)
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_find_tool_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_browser.skyvern_find(by="role", value="button")

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["first_text"] == "Submit"
    assert result["data"]["first_visible"] is True
    assert result["data"]["selector"] == "get_by_role('button')"


@pytest.mark.asyncio
async def test_find_tool_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

    result = await mcp_browser.skyvern_find(by="role", value="button")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_find_tool_invalid_locator_type(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_browser.skyvern_find(by="xpath", value="//div")

    assert result["ok"] is False
    assert "Invalid locator type" in result["error"]["message"]


@pytest.mark.asyncio
async def test_find_tool_no_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    locator = _make_locator(count=0)
    page = _make_mock_page(locator)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_browser.skyvern_find(by="role", value="dialog")

    assert result["ok"] is True
    assert result["data"]["count"] == 0
    assert result["data"]["first_text"] is None


@pytest.mark.asyncio
async def test_find_tool_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    page.get_by_role = MagicMock(side_effect=RuntimeError("Locator error"))
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    result = await mcp_browser.skyvern_find(by="role", value="button")

    assert result["ok"] is False
    assert "Locator error" in result["error"]["message"]
