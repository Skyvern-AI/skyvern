"""Unit tests for skyvern.webeye.real_browser_state.navigate_to_url.

Covers SKY-8818: pages whose subresources never finish loading must still
succeed if the DOM has parsed, via progressive wait_until degradation.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import FailedToNavigateToUrl
from skyvern.webeye.real_browser_state import RealBrowserState


@pytest.fixture
def browser_state() -> RealBrowserState:
    # Bypass __init__; navigate_to_url only uses `self` for LOG context and _wait_for_settle.
    state = RealBrowserState.__new__(RealBrowserState)
    return state


@pytest.mark.asyncio
async def test_navigate_to_url_progresses_from_load_to_domcontentloaded(
    browser_state: RealBrowserState,
) -> None:
    """If wait_until='load' times out but 'domcontentloaded' succeeds, we succeed."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        if wait_until == "load":
            raise PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded (load)")
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/slow-subresources",
        wait_until="load",
    )

    assert "load" in calls
    assert "domcontentloaded" in calls
    assert calls.index("domcontentloaded") > calls.index("load")


@pytest.mark.asyncio
async def test_navigate_to_url_raises_when_all_strategies_fail(
    browser_state: RealBrowserState,
) -> None:
    """If every wait_until strategy times out, raise FailedToNavigateToUrl."""
    page = MagicMock()

    async def always_timeout(url: str, timeout: int, wait_until: str = "load") -> None:
        raise PlaywrightTimeoutError(f"Page.goto: Timeout 60000ms exceeded ({wait_until})")

    page.goto = AsyncMock(side_effect=always_timeout)

    with pytest.raises(FailedToNavigateToUrl):
        await browser_state.navigate_to_url(
            page=page,
            url="https://example.test/fully-dead",
            wait_until="load",
        )


@pytest.mark.asyncio
async def test_navigate_to_url_honors_caller_supplied_wait_until_on_first_try(
    browser_state: RealBrowserState,
) -> None:
    """FileDownloadBlock passes wait_until='domcontentloaded' — it must be honored on first try."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/fast",
        wait_until="domcontentloaded",
    )

    assert calls == ["domcontentloaded"]


@pytest.mark.asyncio
async def test_navigate_to_url_succeeds_on_first_try_with_default_load(
    browser_state: RealBrowserState,
) -> None:
    """Existing callers that use default wait_until='load' must keep working untouched."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/fast-load",
    )

    assert calls == ["load"]
