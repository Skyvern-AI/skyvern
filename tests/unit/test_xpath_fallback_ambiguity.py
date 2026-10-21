from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.exceptions import MissingElement, MultipleElementsFound, SkyvernPageAnalysisTimeout
from skyvern.webeye import action_deadline
from skyvern.webeye.utils.dom import DomUtil


def _make_dom() -> DomUtil:
    scraped_page = MagicMock()
    element = {"id": "el-1", "tagName": "span", "xpath": "//div/span"}
    scraped_page.id_to_element_dict = {"el-1": element}
    scraped_page.id_to_frame_dict = {"el-1": "main.frame"}
    scraped_page.id_to_css_dict = {"el-1": "span.foo"}
    scraped_page.id_to_element_hash = {}
    return DomUtil(scraped_page, MagicMock())


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, *, css_count: int, xpath_count: int) -> tuple[MagicMock, MagicMock]:
    css_locator = MagicMock()
    css_locator.count = AsyncMock(return_value=css_count)
    xpath_locator = MagicMock()
    xpath_locator.count = AsyncMock(return_value=xpath_count)
    frame_content = MagicMock()
    frame_content.locator.return_value = xpath_locator

    async def _resolve(
        scrape_page: object, page: object, frame: str, css: str, engine_selection: object = None
    ) -> tuple[MagicMock, MagicMock]:
        return css_locator, frame_content

    monkeypatch.setattr("skyvern.webeye.utils.dom.resolve_locator", _resolve)
    return css_locator, xpath_locator


@pytest.mark.asyncio
async def test_xpath_fallback_rejects_multiple_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolve(monkeypatch, css_count=0, xpath_count=2)
    with pytest.raises(MultipleElementsFound):
        await _make_dom().get_skyvern_element_by_id("el-1")


@pytest.mark.asyncio
async def test_xpath_fallback_accepts_single_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolve(monkeypatch, css_count=0, xpath_count=1)
    element = await _make_dom().get_skyvern_element_by_id("el-1")
    assert element.get_id() == "el-1"


@pytest.mark.asyncio
async def test_xpath_fallback_missing_when_zero_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolve(monkeypatch, css_count=0, xpath_count=0)
    with pytest.raises(MissingElement):
        await _make_dom().get_skyvern_element_by_id("el-1")


@pytest.mark.asyncio
async def test_css_multiple_matches_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolve(monkeypatch, css_count=2, xpath_count=1)
    with pytest.raises(MultipleElementsFound):
        await _make_dom().get_skyvern_element_by_id("el-1")


@pytest.mark.asyncio
async def test_lookup_against_a_frozen_page_ends_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """``count()`` takes no Playwright timeout, so a renderer that stopped answering held one action
    for 15.5 minutes until the page recovered on its own."""
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    monkeypatch.setattr(settings, "BROWSER_ELEMENT_LOOKUP_TIMEOUT_MS", 50)
    css_locator, _ = _patch_resolve(monkeypatch, css_count=0, xpath_count=1)
    frozen = asyncio.Event()

    async def never_answers() -> int:
        await frozen.wait()
        return 0

    css_locator.count = never_answers

    with pytest.raises(SkyvernPageAnalysisTimeout):
        # A lookup that ignores its budget fails the test here instead of hanging the suite.
        async with asyncio.timeout(10):
            await _make_dom().get_skyvern_element_by_id("el-1")


@pytest.mark.asyncio
async def test_safe_lookup_propagates_analysis_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    monkeypatch.setattr(settings, "BROWSER_ELEMENT_LOOKUP_TIMEOUT_MS", 50)
    css_locator, _ = _patch_resolve(monkeypatch, css_count=0, xpath_count=1)
    frozen = asyncio.Event()

    async def never_answers() -> int:
        await frozen.wait()
        return 0

    css_locator.count = never_answers

    with pytest.raises(SkyvernPageAnalysisTimeout):
        async with asyncio.timeout(10):
            await _make_dom().safe_get_skyvern_element_by_id("el-1")
