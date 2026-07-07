from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import ScrapeFrameDecision


def _make_frame(url: str, child_frames: list | None = None) -> MagicMock:
    """Build a Playwright-Frame stand-in good enough to drive get_frame_text."""
    frame = MagicMock()
    frame.url = url
    frame.child_frames = child_frames or []
    frame.is_detached = MagicMock(return_value=False)

    element = MagicMock()
    element.is_visible = AsyncMock(return_value=True)
    frame.frame_element = AsyncMock(return_value=element)

    page = MagicMock()
    page.url = "https://example.com/"
    frame.page = page
    return frame


@pytest.mark.asyncio
async def test_get_frame_text_evaluates_main_frame_without_exclude() -> None:
    main = _make_frame("https://example.com/")

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(return_value="hello")):
        text = await scraper.get_frame_text(main)

    assert text == "hello"


@pytest.mark.asyncio
async def test_get_frame_text_default_walks_every_child_for_backwards_compat() -> None:
    child_ad = _make_frame("https://ads.example.invalid/iframe")
    child_real = _make_frame("https://example.com/embed")
    main = _make_frame("https://example.com/", child_frames=[child_ad, child_real])

    visited: list[str] = []

    async def _evaluate(frame, expression):
        visited.append(frame.url)
        return f"text({frame.url})"

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(side_effect=_evaluate)):
        text = await scraper.get_frame_text(main)

    # No predicate => historical behavior: every visible non-detached child is walked.
    assert visited == [
        "https://example.com/",
        "https://ads.example.invalid/iframe",
        "https://example.com/embed",
    ]
    assert "text(https://ads.example.invalid/iframe)" in text


@pytest.mark.asyncio
async def test_get_frame_text_skips_excluded_child_and_its_subtree() -> None:
    grandchild_under_ad = _make_frame("https://ads.example.invalid/grandchild")
    child_ad = _make_frame("https://ads.example.invalid/iframe", child_frames=[grandchild_under_ad])
    child_real = _make_frame("https://example.com/embed")
    main = _make_frame("https://example.com/", child_frames=[child_ad, child_real])

    async def _exclude(page, frame) -> ScrapeFrameDecision:
        hostname = urlparse(frame.url or "").hostname or ""
        return ScrapeFrameDecision(exclude=hostname == "ads.example.invalid")

    visited: list[str] = []

    async def _evaluate(frame, expression):
        visited.append(frame.url)
        return f"text({frame.url})"

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(side_effect=_evaluate)):
        text = await scraper.get_frame_text(main, scrape_exclude=_exclude)

    # The excluded child must be dropped *before* recursion, so its grandchild
    # is unreachable too.
    assert visited == [
        "https://example.com/",
        "https://example.com/embed",
    ]
    assert "ads.example.invalid" not in text
    # frame_element() must not be probed on excluded frames either, so we don't
    # pay a CDP round trip for frames we already decided to drop.
    child_ad.frame_element.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_frame_text_keeps_child_when_exclude_returns_false() -> None:
    child_keep = _make_frame("https://other.example.com/iframe")
    main = _make_frame("https://example.com/", child_frames=[child_keep])

    exclude_calls: list[tuple[object, object]] = []

    async def _exclude(page, frame) -> ScrapeFrameDecision:
        exclude_calls.append((page, frame))
        return ScrapeFrameDecision(exclude=False)

    visited: list[str] = []

    async def _evaluate(frame, expression):
        visited.append(frame.url)
        return f"text({frame.url})"

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(side_effect=_evaluate)):
        await scraper.get_frame_text(main, scrape_exclude=_exclude)

    # The predicate is invoked exactly once with (child_frame.page, child_frame)
    # — matching filter_frames' contract.
    assert len(exclude_calls) == 1
    seen_page, seen_frame = exclude_calls[0]
    assert seen_page is child_keep.page
    assert seen_frame is child_keep
    assert visited == [
        "https://example.com/",
        "https://other.example.com/iframe",
    ]


@pytest.mark.asyncio
async def test_get_frame_text_still_skips_detached_children_before_predicate() -> None:
    detached_child = _make_frame("https://ads.example.invalid/dead")
    detached_child.is_detached = MagicMock(return_value=True)
    main = _make_frame("https://example.com/", child_frames=[detached_child])

    async def _exclude(page, frame) -> ScrapeFrameDecision:
        raise AssertionError("predicate must not run for detached frames")

    with patch.object(scraper.SkyvernFrame, "evaluate", new=AsyncMock(return_value="root")):
        text = await scraper.get_frame_text(main, scrape_exclude=_exclude)

    assert text == "root"
