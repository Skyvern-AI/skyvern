"""SKY-10011 — economy tree portal-priority truncation.

When the economy tree still exceeds the token budget and is sliced at
percent_to_keep=2/3, portal/overlay elements (those containing
role="listbox" or role="option" descendants) must survive truncation
even if they are positioned at the end of <body> (i.e. last root elements).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraped_page import ElementTreeFormat, ScrapedPage


def _make_scraped_page(element_tree_trimmed: list[dict]) -> ScrapedPage:
    return ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=element_tree_trimmed,
        _browser_state=MagicMock(),
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )


@pytest.fixture(autouse=True)
def _scoped_context():
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        yield


def _table_row_element(row_index: int) -> dict:
    """A large, non-overlay table row element."""
    return {
        "tagName": "tr",
        "attributes": {},
        "children": [
            {
                "tagName": "td",
                "attributes": {},
                "text": f"Row {row_index} " + ("cell data " * 30),
                "children": [],
            }
        ],
    }


def _listbox_portal_element() -> dict:
    """A portal element with a listbox child, mimicking a popper dropdown."""
    return {
        "tagName": "div",
        "attributes": {},
        "children": [
            {
                "tagName": "ul",
                "attributes": {"role": "listbox"},
                "children": [
                    {
                        "tagName": "li",
                        "attributes": {"role": "option"},
                        "text": "Option A",
                        "children": [],
                        "interactable": True,
                        "id": "opt_a",
                    },
                    {
                        "tagName": "li",
                        "attributes": {"role": "option"},
                        "text": "Option B",
                        "children": [],
                        "interactable": True,
                        "id": "opt_b",
                    },
                ],
            }
        ],
    }


def test_portal_element_survives_economy_truncation() -> None:
    """Portal at end of tree must survive when first 2/3 of chars are kept."""
    # Build a tree: many large table rows first, portal last (as portals
    # are appended to <body> by React portals).
    rows = [_table_row_element(i) for i in range(100)]
    portal = _listbox_portal_element()
    page = _make_scraped_page(rows + [portal])

    # 2/3 truncation at character level would drop the portal (it's last).
    result = page.build_economy_elements_tree(
        fmt=ElementTreeFormat.HTML,
        percent_to_keep=2 / 3,
    )

    assert "Option A" in result, "Portal listbox option must be retained after truncation"
    assert "Option B" in result, "Portal listbox option must be retained after truncation"


def test_portal_element_survives_when_nested_one_level_deep() -> None:
    """Portal heuristic works even when listbox is nested one level inside the root div."""
    rows = [_table_row_element(i) for i in range(100)]
    portal = {
        "tagName": "div",
        "attributes": {},
        "children": [
            {
                "tagName": "div",
                "attributes": {},
                "children": [
                    {
                        "tagName": "ul",
                        "attributes": {"role": "listbox"},
                        "children": [
                            {
                                "tagName": "li",
                                "attributes": {"role": "option"},
                                "text": "Nested Option",
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    page = _make_scraped_page(rows + [portal])

    result = page.build_economy_elements_tree(
        fmt=ElementTreeFormat.HTML,
        percent_to_keep=2 / 3,
    )

    assert "Nested Option" in result, "Nested portal must survive truncation"


def test_no_overlay_elements_behavior_unchanged() -> None:
    """Without any overlay elements, truncation keeps the front (unchanged behavior)."""
    # Make elements with distinctive content that can be checked
    first_elem = {
        "tagName": "div",
        "attributes": {},
        "text": "FIRST_ELEMENT_MARKER",
        "children": [],
    }
    rows = [_table_row_element(i) for i in range(100)]
    last_elem = {
        "tagName": "div",
        "attributes": {},
        "text": "LAST_ELEMENT_MARKER",
        "children": [],
    }
    page = _make_scraped_page([first_elem] + rows + [last_elem])

    result = page.build_economy_elements_tree(
        fmt=ElementTreeFormat.HTML,
        percent_to_keep=2 / 3,
    )

    assert "FIRST_ELEMENT_MARKER" in result, "Front elements must be retained"
    # Last element may or may not be dropped — just verify no crash


def test_portal_retained_and_cache_cleared_after_refetch() -> None:
    """economy_element_tree cache is reset between calls with different trees."""
    rows = [_table_row_element(i) for i in range(50)]
    portal = _listbox_portal_element()
    page = _make_scraped_page(rows + [portal])

    # First call with no truncation — must also work
    result_full = page.build_economy_elements_tree(
        fmt=ElementTreeFormat.HTML,
        percent_to_keep=1,
    )
    assert "Option A" in result_full

    # Reset cache (simulates refresh)
    page.economy_element_tree = None

    result_truncated = page.build_economy_elements_tree(
        fmt=ElementTreeFormat.HTML,
        percent_to_keep=2 / 3,
    )
    assert "Option A" in result_truncated
