from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.scraper.scraped_page import ScrapedPage


class _Page:
    pass


def _scraped(
    html: str = "<p>a</p>", tree: list[dict] | None = None, browser_state: object | None = None
) -> ScrapedPage:
    return ScrapedPage(
        elements=[],
        element_tree=tree if tree is not None else [{"id": "a"}],
        element_tree_trimmed=[],
        url="https://example.test/",
        html=html,
        _browser_state=browser_state or MagicMock(),
        _clean_up_func=None,
        _scrape_exclude=None,
    )


def test_the_origin_is_the_page_whose_html_and_tree_were_read() -> None:
    page = _Page()
    scraped = _scraped()
    assert scraped.origin_page() is None
    scraped.bind_origin(page)
    assert scraped.origin_page() is page


@pytest.mark.parametrize("field", ["html", "element_tree"])
def test_content_replaced_after_the_scrape_has_no_origin(field: str) -> None:
    scraped = _scraped()
    scraped.bind_origin(_Page())
    setattr(scraped, field, "<p>b</p>" if field == "html" else [{"id": "b"}])
    assert scraped.origin_page() is None


@pytest.mark.parametrize("change", ["url", "tree"])
def test_a_scrape_changed_in_place_after_it_was_read_has_no_origin(change: str) -> None:
    page = _Page()
    scraped = _scraped()
    scraped.bind_origin(page)
    assert scraped.origin_page() is page
    if change == "url":
        scraped.url = "https://example.test/elsewhere"
    else:
        scraped.element_tree[0]["text"] = "edited"
    assert scraped.origin_page() is None and scraped.origin_captured_at() is None


def test_the_origin_keeps_the_html_read_with_the_tree_apart_from_the_public_html() -> None:
    page = _Page()
    scraped = _scraped(html="<p>after the screenshots</p>")
    scraped.bind_origin(page, source_html="<p>with the tree</p>")
    assert scraped.origin_html() == "<p>with the tree</p>" and scraped.html == "<p>after the screenshots</p>"
    scraped.html = "<p>replaced</p>"
    assert scraped.origin_html() is None and scraped.origin_page() is None

    unsplit = _scraped()
    unsplit.bind_origin(page)
    assert unsplit.origin_html() == unsplit.html


def test_the_origin_is_dated_when_the_page_was_read() -> None:
    page = _Page()
    scraped = _scraped()
    before = datetime.now(UTC)
    scraped.bind_origin(page)
    after = datetime.now(UTC)
    captured_at = scraped.origin_captured_at()
    assert captured_at is not None and before <= captured_at <= after
    scraped.html = "<p>b</p>"
    assert scraped.origin_captured_at() is None


def test_an_origin_that_cannot_be_held_is_unknown() -> None:
    scraped = _scraped()
    scraped.bind_origin(object())  # type: ignore[arg-type]
    assert scraped.origin_page() is None


@pytest.mark.asyncio
async def test_a_refresh_moves_the_origin_to_the_page_the_refresh_read() -> None:
    first, working = _Page(), _Page()
    browser_state = MagicMock()
    scraped = _scraped(browser_state=browser_state)
    scraped.bind_origin(first)
    refreshed = _scraped(html="<p>working</p>", tree=[{"id": "w"}])
    refreshed.bind_origin(working)
    browser_state.scrape_website = AsyncMock(return_value=refreshed)

    await scraped.refresh()
    assert scraped.origin_page() is working
    # The refresh's own read time, not the time this object was refreshed.
    assert scraped.origin_captured_at() == refreshed.origin_captured_at()

    browser_state.scrape_website = AsyncMock(return_value=_scraped(html="<p>unbound</p>", tree=[]))
    await scraped.refresh()
    assert scraped.origin_page() is None
