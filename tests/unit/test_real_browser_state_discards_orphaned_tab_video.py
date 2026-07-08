"""launch_persistent_context() auto-opens a default tab before Skyvern picks a working
page. check_and_fix_state() then opens a fresh page and discards that default tab via
_close_all_other_pages(), but the tab's near-empty video was still registered and later
uploaded as a spurious second RECORDING artifact. The video must be dropped at the same
moment the orphaned page is discarded.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.real_browser_state import RealBrowserState


def _make_page(video_path: str | None) -> MagicMock:
    page = MagicMock()
    page.close = AsyncMock()
    if video_path is None:
        page.video = None
    else:
        page.video = MagicMock()
        page.video.path = AsyncMock(return_value=video_path)
    return page


def _build_state(
    *, pages: list[MagicMock], working_page: MagicMock | None, video_artifacts: list[VideoArtifact]
) -> RealBrowserState:
    state = RealBrowserState(pw=AsyncMock(), browser_artifacts=BrowserArtifacts(video_artifacts=video_artifacts))
    context = MagicMock()
    context.pages = pages
    state.browser_context = context
    state.get_working_page = AsyncMock(return_value=working_page)
    return state


@pytest.mark.asyncio
async def test_check_and_fix_state_discards_default_tab_video_when_opening_new_page() -> None:
    """End-to-end at the real call site: opening a fresh page drops the default tab's video."""
    default_tab = _make_page("/tmp/default-tab.webm")
    default_tab.url = "about:blank"
    new_page = _make_page("/tmp/new-page.webm")
    new_page.url = "about:blank"

    state = RealBrowserState(
        pw=AsyncMock(),
        browser_artifacts=BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/default-tab.webm")]),
    )
    context = MagicMock()
    context.pages = [default_tab]
    context.new_page = AsyncMock(return_value=new_page)
    state.browser_context = context
    state.navigate_to_url = AsyncMock()

    working_page_holder: dict[str, MagicMock | None] = {"page": None}

    async def _get_working_page() -> MagicMock | None:
        return working_page_holder["page"]

    async def _set_working_page(page: MagicMock | None, index: int = 0) -> None:
        working_page_holder["page"] = page

    state.get_working_page = _get_working_page
    state.set_working_page = _set_working_page

    await state.check_and_fix_state()

    context.new_page.assert_awaited_once()
    default_tab.close.assert_awaited_once()
    # The "page" event listener (browser_factory.set_popup_video_listener) registers new_page's
    # own video separately; this test only covers the default tab's entry being dropped here.
    assert state.browser_artifacts.video_artifacts == []


@pytest.mark.asyncio
async def test_close_all_other_pages_discards_orphaned_page_video() -> None:
    """Direct unit coverage of the orphaned-page video pruning logic."""
    throwaway_page = _make_page("/tmp/throwaway.webm")
    real_page = _make_page("/tmp/real.webm")
    state = _build_state(
        pages=[throwaway_page, real_page],
        working_page=real_page,
        video_artifacts=[
            VideoArtifact(video_path="/tmp/throwaway.webm"),
            VideoArtifact(video_path="/tmp/real.webm"),
        ],
    )

    await state._close_all_other_pages(discard_orphaned_videos=True)

    throwaway_page.close.assert_awaited_once()
    real_page.close.assert_not_awaited()
    assert [va.video_path for va in state.browser_artifacts.video_artifacts] == ["/tmp/real.webm"]
    assert state.browser_artifacts.is_page_video_discarded(throwaway_page) is True


@pytest.mark.asyncio
async def test_check_and_fix_state_reuse_branch_preserves_existing_page_video() -> None:
    """browser_address/remote-session runs reuse the existing page as the working page instead
    of opening a new one, so _close_all_other_pages (and the discard inside it) never runs —
    that page's video, its ONLY recording, must survive untouched."""
    existing_page = _make_page("/tmp/existing.webm")
    existing_page.url = "about:blank"

    state = RealBrowserState(
        pw=AsyncMock(),
        browser_artifacts=BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/existing.webm")]),
    )
    context = MagicMock()
    context.pages = [existing_page]
    context.new_page = AsyncMock()
    state.browser_context = context
    state.navigate_to_url = AsyncMock()
    state.list_valid_pages = AsyncMock(return_value=[existing_page])

    working_page_holder: dict[str, MagicMock | None] = {"page": None}

    async def _get_working_page() -> MagicMock | None:
        return working_page_holder["page"]

    async def _set_working_page(page: MagicMock | None, index: int = 0) -> None:
        working_page_holder["page"] = page

    state.get_working_page = _get_working_page
    state.set_working_page = _set_working_page

    await state.check_and_fix_state(browser_address="http://remote:9222")

    context.new_page.assert_not_awaited()
    existing_page.close.assert_not_awaited()
    assert [va.video_path for va in state.browser_artifacts.video_artifacts] == ["/tmp/existing.webm"]
    assert state.browser_artifacts.is_page_video_discarded(existing_page) is False


@pytest.mark.asyncio
async def test_close_all_other_pages_keeps_videos_by_default() -> None:
    """The teardown call site (close_current_open_page) does not opt in: a page closed there
    may be a real popup the agent used, not a throwaway, so its video must survive."""
    other_page = _make_page("/tmp/popup.webm")
    real_page = _make_page("/tmp/real.webm")
    state = _build_state(
        pages=[other_page, real_page],
        working_page=real_page,
        video_artifacts=[
            VideoArtifact(video_path="/tmp/popup.webm"),
            VideoArtifact(video_path="/tmp/real.webm"),
        ],
    )

    await state._close_all_other_pages()

    other_page.close.assert_awaited_once()
    assert {va.video_path for va in state.browser_artifacts.video_artifacts} == {
        "/tmp/popup.webm",
        "/tmp/real.webm",
    }


@pytest.mark.asyncio
async def test_discard_video_artifact_noop_when_page_has_no_video() -> None:
    """Recording disabled (page.video is None) must be a harmless no-op."""
    state = _build_state(pages=[], working_page=None, video_artifacts=[VideoArtifact(video_path="/tmp/real.webm")])
    page = _make_page(None)

    await state._discard_video_artifact(page)

    assert [va.video_path for va in state.browser_artifacts.video_artifacts] == ["/tmp/real.webm"]
