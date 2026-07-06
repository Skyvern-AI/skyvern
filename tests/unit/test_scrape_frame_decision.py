"""Generic scrape-frame decision seam.

``scrape_exclude`` callbacks return a ``ScrapeFrameDecision`` that carries the
exclude flag and an optional non-interactable placeholder node. These tests pin the
OSS plumbing — frame filtering, placeholder collection / de-duplication, and that
placeholders reach the element tree but never the flat interactable elements list —
without any vendor-specific (captcha) logic, which now lives in the cloud filter.

The browser-backed plumbing test is skipped in CI when Playwright browsers are not
installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraped_page import ScrapeFrameDecision
from skyvern.webeye.scraper.scraper import filter_frames, get_interactable_element_tree


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

_PLACEHOLDER = {
    "tagName": "iframe",
    "interactable": False,
    "attributes": {"title": "placeholder"},
    "text": "placeholder signal",
    "children": [],
}


def _make_frame(detached: bool = False) -> MagicMock:
    frame = MagicMock()
    frame.is_detached = MagicMock(return_value=detached)
    frame.page = MagicMock()
    return frame


class TestFilterFrames:
    @pytest.mark.asyncio
    async def test_exclude_drops_frame_and_yields_no_placeholder(self) -> None:
        keep, drop = _make_frame(), _make_frame()

        async def _exclude(page: object, frame: object) -> ScrapeFrameDecision:
            return ScrapeFrameDecision(exclude=frame is drop)

        frames, placeholders = await filter_frames([keep, drop], _exclude)
        assert frames == [keep]
        assert placeholders == []

    @pytest.mark.asyncio
    async def test_decision_excludes_frame_and_collects_placeholder(self) -> None:
        drop = _make_frame()

        async def _exclude(page: object, frame: object) -> ScrapeFrameDecision:
            return ScrapeFrameDecision(exclude=True, placeholder=_PLACEHOLDER)

        frames, placeholders = await filter_frames([drop], _exclude)
        assert frames == []
        assert placeholders == [_PLACEHOLDER]

    @pytest.mark.asyncio
    async def test_placeholder_can_accompany_a_kept_frame(self) -> None:
        keep = _make_frame()

        async def _exclude(page: object, frame: object) -> ScrapeFrameDecision:
            return ScrapeFrameDecision(exclude=False, placeholder=_PLACEHOLDER)

        frames, placeholders = await filter_frames([keep], _exclude)
        assert frames == [keep]
        assert placeholders == [_PLACEHOLDER]

    @pytest.mark.asyncio
    async def test_identical_placeholders_dedupe(self) -> None:
        f1, f2 = _make_frame(), _make_frame()

        async def _exclude(page: object, frame: object) -> ScrapeFrameDecision:
            return ScrapeFrameDecision(exclude=True, placeholder=dict(_PLACEHOLDER))

        _, placeholders = await filter_frames([f1, f2], _exclude)
        assert placeholders == [_PLACEHOLDER]

    @pytest.mark.asyncio
    async def test_detached_frames_skip_predicate(self) -> None:
        detached = _make_frame(detached=True)

        async def _exclude(page: object, frame: object) -> ScrapeFrameDecision:
            raise AssertionError("predicate must not run for detached frames")

        frames, placeholders = await filter_frames([detached], _exclude)
        assert frames == []
        assert placeholders == []

    @pytest.mark.asyncio
    async def test_no_predicate_keeps_all_live_frames(self) -> None:
        keep, detached = _make_frame(), _make_frame(detached=True)
        frames, placeholders = await filter_frames([keep, detached], None)
        assert frames == [keep]
        assert placeholders == []


@pytest.fixture(autouse=True)
def _skyvern_ctx() -> Iterator[None]:
    skyvern_context.set(SkyvernContext())
    yield
    skyvern_context.reset()


@pytest_asyncio.fixture
async def page_factory() -> AsyncIterator[Callable[[str], Awaitable[Page]]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        async def _make_page(html: str) -> Page:
            page = await context.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await asyncio.sleep(0.2)
            return page

        yield _make_page

        await context.close()
        await browser.close()


def _flatten(tree: list[dict]) -> list[dict]:
    flat: list[dict] = []
    queue = list(tree)
    while queue:
        node = queue.pop(0)
        flat.append(node)
        queue.extend(node.get("children", []))
    return flat


@_skip_no_browser
class TestPlaceholderPlumbing:
    @pytest.mark.asyncio
    async def test_decision_placeholder_reaches_tree_but_not_elements(
        self, page_factory: Callable[[str], Awaitable[Page]]
    ) -> None:
        page = await page_factory(
            "<html><body><input id='name' type='text' />"
            "<iframe src='https://embed.example.com/widget' style='width:200px;height:80px'></iframe>"
            "</body></html>"
        )

        # Generic seam: skip the embed frame while injecting a signal node in its place.
        async def _exclude(p: object, frame: object) -> ScrapeFrameDecision:
            if frame == page.main_frame:
                return ScrapeFrameDecision(exclude=False)
            return ScrapeFrameDecision(exclude=True, placeholder=dict(_PLACEHOLDER))

        elements, element_tree = await get_interactable_element_tree(page, scrape_exclude=_exclude)

        tree_texts = [str(node.get("text", "")) for node in _flatten(element_tree)]
        assert "placeholder signal" in tree_texts
        element_texts = [str(el.get("text", "")) for el in elements]
        assert "placeholder signal" not in element_texts

        # real form controls survive in both the flat elements list and the tree
        flat_ids = {node.get("id") for node in _flatten(element_tree)}
        interactable_ids = {el["id"] for el in elements if el.get("interactable")}
        assert interactable_ids & flat_ids, "real controls stay in elements and tree"

    @pytest.mark.asyncio
    async def test_exclude_without_placeholder_injects_nothing(
        self, page_factory: Callable[[str], Awaitable[Page]]
    ) -> None:
        page = await page_factory("<html><body><input id='name' type='text' /></body></html>")

        exclude = AsyncMock(return_value=ScrapeFrameDecision(exclude=False))
        _, element_tree = await get_interactable_element_tree(page, scrape_exclude=exclude)

        assert all("placeholder signal" not in str(node.get("text", "")) for node in _flatten(element_tree))
