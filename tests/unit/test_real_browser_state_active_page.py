"""Tests for the deterministic active-tab pin in RealBrowserState.get_working_page."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.real_browser_state import RealBrowserState


def _mock_page(url: str = "https://example.test", closed: bool = False) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.is_closed = MagicMock(return_value=closed)
    return page


def _make_state() -> RealBrowserState:
    return RealBrowserState(pw=MagicMock(), browser_context=MagicMock())


@pytest.mark.asyncio
async def test_no_pin_returns_last_page() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_working_page(page0)

    assert await state.get_working_page() is page1


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_url", ["about:blank", ":"])
async def test_no_pin_returns_last_blank_page(blank_url: str) -> None:
    page0 = _mock_page("https://example.test/bills")
    blank_page = _mock_page(blank_url)
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, blank_page])
    await state.set_working_page(page0)

    assert await state.get_working_page() is blank_page


@pytest.mark.asyncio
async def test_pin_overrides_last_page() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    # page0 is the pinned (earlier) tab, not the last page — the pin must win.
    assert await state.get_working_page() is page0


@pytest.mark.asyncio
async def test_pin_dropped_when_pinned_page_closed() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    page0.is_closed.return_value = True
    assert await state.get_working_page() is page1


@pytest.mark.asyncio
async def test_pin_dropped_when_new_tab_opens() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    # A brand-new tab appears (count grows past pin time) -> auto-follow the newest tab.
    page2 = _mock_page()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1, page2])
    assert await state.get_working_page() is page2


@pytest.mark.asyncio
async def test_pin_dropped_when_about_blank_tab_opens() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    blank_page = _mock_page("about:blank")
    state.list_valid_pages = AsyncMock(return_value=[page0, page1, blank_page])
    assert await state.get_working_page() is blank_page


@pytest.mark.asyncio
async def test_pin_survives_when_recoverable_blank_marker_opens() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    blank_page = _mock_page(":")
    state.list_valid_pages = AsyncMock(return_value=[page0, page1, blank_page])
    assert await state.get_working_page() is page0


@pytest.mark.asyncio
async def test_pin_survives_when_other_tab_closes() -> None:
    page0, page1, page2 = _mock_page(), _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1, page2])
    await state.set_active_page(page0)

    # A different tab closes (count shrinks) -> the pin still holds.
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    assert await state.get_working_page() is page0


@pytest.mark.asyncio
async def test_pin_dropped_when_tab_closed_then_new_tab_opens() -> None:
    # A close+open leaves the tab count unchanged, but a genuinely new tab must still
    # take focus rather than being masked by the pin.
    page0, page1, page2 = _mock_page(), _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1, page2])
    await state.set_active_page(page0)

    page3 = _mock_page()
    state.list_valid_pages = AsyncMock(return_value=[page0, page2, page3])
    assert await state.get_working_page() is page3


@pytest.mark.asyncio
async def test_reset_working_page_clears_pin() -> None:
    page0, page1 = _mock_page(), _mock_page()
    state = _make_state()
    state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    await state.set_active_page(page0)

    await state.set_working_page(None)
    assert await state.get_working_page() is None
