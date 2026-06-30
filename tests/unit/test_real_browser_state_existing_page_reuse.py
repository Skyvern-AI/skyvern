"""Some remote browser sessions bind capture/streaming to the CDP target that
existed at session creation. ``check_and_fix_state`` must reuse that existing
page on the first task instead of opening a new tab and closing the original,
so the remote session stays aligned with the page the agent navigates.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.real_browser_state import RealBrowserState


def _build_state(*, session_id: str | None, existing_pages: list[MagicMock]) -> RealBrowserState:
    state = RealBrowserState(
        pw=AsyncMock(),
        browser_artifacts=BrowserArtifacts(remote_browser_session_id=session_id),
    )
    context = MagicMock()
    context.pages = existing_pages
    context.new_page = AsyncMock()
    state.browser_context = context
    state._close_all_other_pages = AsyncMock()
    state.list_valid_pages = AsyncMock(return_value=list(existing_pages))
    state.navigate_to_url = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_check_and_fix_state_reuses_existing_page_for_remote_browser_session() -> None:
    existing_page = MagicMock()
    existing_page.url = "about:blank"
    state = _build_state(session_id="remote-session-abc", existing_pages=[existing_page])

    await state.check_and_fix_state(url="https://example.com/")

    assert state.browser_context.new_page.await_count == 0, "remote browser session must not open a new tab"
    state._close_all_other_pages.assert_not_awaited()
    state.navigate_to_url.assert_awaited_once()
    args, kwargs = state.navigate_to_url.call_args
    assert kwargs.get("page") is existing_page or (args and args[0] is existing_page)
    assert await state.get_working_page() is existing_page


@pytest.mark.asyncio
async def test_check_and_fix_state_creates_new_page_when_context_has_no_pages() -> None:
    state = _build_state(session_id=None, existing_pages=[])

    new_page = MagicMock()
    new_page.url = "about:blank"
    state.browser_context.new_page = AsyncMock(return_value=new_page)

    await state.check_and_fix_state(url="https://example.com/")

    state.browser_context.new_page.assert_awaited_once()
    state._close_all_other_pages.assert_awaited_once()
    state.navigate_to_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_and_fix_state_reuses_existing_page_with_browser_address() -> None:
    existing_page = MagicMock()
    existing_page.url = "about:blank"
    state = _build_state(session_id=None, existing_pages=[existing_page])

    await state.check_and_fix_state(url="https://example.com/", browser_address="http://remote:9222")

    assert state.browser_context.new_page.await_count == 0
    state._close_all_other_pages.assert_not_awaited()
    state.navigate_to_url.assert_awaited_once()
