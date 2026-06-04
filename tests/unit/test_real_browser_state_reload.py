"""Unit tests for RealBrowserState.reload_page degradation and scoped usage.

Covers SKY-10476: extraction scrape reload must degrade through
load → domcontentloaded → commit instead of hard-failing on SPA pages.
Degradation is scoped to extraction tasks only.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.constants import ScrapeType
from skyvern.exceptions import FailedToReloadPage
from skyvern.forge.agent import ForgeAgent
from skyvern.webeye.real_browser_state import RealBrowserState

_AGENT_MODULE = "skyvern.forge.agent"


@pytest.fixture
def browser_state() -> RealBrowserState:
    state = RealBrowserState.__new__(RealBrowserState)
    return state


def _make_page(reload_side_effect=None) -> MagicMock:
    page = MagicMock()
    page.url = "https://example.test/spa"
    page.reload = AsyncMock(side_effect=reload_side_effect)
    return page


@pytest.mark.asyncio
async def test_reload_page_default_raises_on_timeout(browser_state: RealBrowserState) -> None:
    """Default reload_page (no degradation) raises FailedToReloadPage on timeout — existing behavior."""
    page = _make_page(PlaywrightTimeoutError("Page.reload: Timeout 60000ms exceeded"))

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        with pytest.raises(FailedToReloadPage):
            await browser_state.reload_page()

    page.reload.assert_called_once()
    call_kwargs = page.reload.call_args
    assert "wait_until" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_reload_page_default_succeeds_unchanged(browser_state: RealBrowserState) -> None:
    """Default reload_page succeeds without passing wait_until — no behavior change."""
    page = _make_page()
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page()

    page.reload.assert_called_once()
    call_kwargs = page.reload.call_args
    assert "wait_until" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_domcontentloaded(browser_state: RealBrowserState) -> None:
    """Degradation mode: load times out, domcontentloaded succeeds."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)
        if wait_until == "load":
            raise PlaywrightTimeoutError("Page.reload: Timeout 60000ms exceeded")

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load", "domcontentloaded"]


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_commit(browser_state: RealBrowserState) -> None:
    """Degradation mode: load and domcontentloaded time out, commit succeeds."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)
        if wait_until in ("load", "domcontentloaded"):
            raise PlaywrightTimeoutError(f"Page.reload: Timeout 60000ms exceeded ({wait_until})")

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load", "domcontentloaded", "commit"]


@pytest.mark.asyncio
async def test_reload_page_degradation_raises_when_all_strategies_fail(browser_state: RealBrowserState) -> None:
    """Degradation mode: all strategies fail, raises FailedToReloadPage."""

    async def always_timeout(timeout: int, wait_until: str = "load") -> None:
        raise PlaywrightTimeoutError(f"Page.reload: Timeout 60000ms exceeded ({wait_until})")

    page = _make_page(always_timeout)

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        with pytest.raises(FailedToReloadPage):
            await browser_state.reload_page(degradation=True)

    assert page.reload.call_count == 3


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_first_try(browser_state: RealBrowserState) -> None:
    """Degradation mode: load succeeds immediately, no degradation needed."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load"]


# --- Scrape retry integration: RELOAD always uses degradation ---


def _make_agent() -> ForgeAgent:
    return ForgeAgent.__new__(ForgeAgent)


def _make_browser_state_mock() -> MagicMock:
    bs = MagicMock()
    bs.reload_page = AsyncMock()
    bs.scrape_website = AsyncMock(return_value=MagicMock())
    return bs


@pytest.mark.asyncio
async def test_scrape_with_type_reload_uses_degradation() -> None:
    """Scrape retry RELOAD always passes degradation=True."""
    agent = _make_agent()
    bs = _make_browser_state_mock()
    task = MagicMock()
    task.url = "https://example.test"
    step = MagicMock()
    mock_app = MagicMock()

    with patch(f"{_AGENT_MODULE}.app", mock_app):
        await agent._scrape_with_type(
            task=task,
            step=step,
            browser_state=bs,
            scrape_type=ScrapeType.RELOAD,
            engine=MagicMock(),
        )

    bs.reload_page.assert_called_once_with(degradation=True)


@pytest.mark.asyncio
async def test_scrape_with_type_normal_no_reload_call() -> None:
    """NORMAL scrape type does not call reload_page at all."""
    agent = _make_agent()
    bs = _make_browser_state_mock()
    task = MagicMock()
    task.url = "https://example.test"
    step = MagicMock()
    mock_app = MagicMock()

    with patch(f"{_AGENT_MODULE}.app", mock_app):
        await agent._scrape_with_type(
            task=task,
            step=step,
            browser_state=bs,
            scrape_type=ScrapeType.NORMAL,
            engine=MagicMock(),
        )

    bs.reload_page.assert_not_called()
