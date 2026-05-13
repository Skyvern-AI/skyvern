"""SKY-9718 Layer 1 — coverage for ScrapedPage.build_lean_elements_tree.

The pure walker (`apply_lean_to_tree`) is covered by test_lean_html.py. This
file covers the builder method on `ScrapedPage`: cache hit/miss, cache
isolation across flag combos, JSON vs HTML dispatch, `html_need_skyvern_attrs`
interaction, and `last_used_element_tree[_html]` side effects.
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
    """`json_to_html` calls `skyvern_context.ensure_context()`, so we need one."""
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        yield


def _node(tag: str, *, id: str | None = None, attributes: dict | None = None, children: list | None = None) -> dict:
    n: dict = {"tagName": tag, "attributes": dict(attributes or {}), "children": list(children or [])}
    if id is not None:
        n["id"] = id
    return n


# --- cache hit / miss ----------------------------------------------------


def test_cache_hit_same_flag_combo_returns_same_tree_object() -> None:
    """Two calls with identical flags should reuse the cached transformed tree."""
    page = _make_scraped_page([_node("a", id="AAAB", attributes={"href": "/x?utm=1"})])

    # First call populates the cache.
    page.build_lean_elements_tree(strip_url_query_strings=True)
    cache_key = (False, False, True)
    assert cache_key in page.lean_element_tree_cache
    cached_tree = page.lean_element_tree_cache[cache_key]

    # Second call with the same flags must reuse the cached list (identity, not equality).
    page.build_lean_elements_tree(strip_url_query_strings=True)
    assert page.lean_element_tree_cache[cache_key] is cached_tree


def test_cache_isolation_across_flag_combos() -> None:
    """Different flag combos populate different cache slots and don't clobber each other."""
    page = _make_scraped_page(
        [
            _node(
                "a",
                attributes={"href": "/x?utm=1"},
                children=[_node("img", attributes={"src": "/cat.jpg?cb=1", "alt": "cat"})],
            )
        ]
    )

    page.build_lean_elements_tree(compress_image_src=True)
    page.build_lean_elements_tree(strip_url_query_strings=True)
    page.build_lean_elements_tree(compress_image_src=True, strip_url_query_strings=True)

    keys = set(page.lean_element_tree_cache.keys())
    assert keys == {(False, True, False), (False, False, True), (False, True, True)}

    # Different combos produce different rendered output.
    only_img = page.build_lean_elements_tree(compress_image_src=True)
    only_qs = page.build_lean_elements_tree(strip_url_query_strings=True)
    both = page.build_lean_elements_tree(compress_image_src=True, strip_url_query_strings=True)
    assert only_img != only_qs
    assert both != only_img
    assert both != only_qs


def test_cache_isolation_default_flags_off_is_its_own_slot() -> None:
    """No-op call (all flags False) is a valid combo with its own cache key."""
    page = _make_scraped_page([_node("a", attributes={"href": "/x?utm=1"})])
    page.build_lean_elements_tree()
    assert (False, False, False) in page.lean_element_tree_cache


# --- JSON vs HTML dispatch -----------------------------------------------


def test_fmt_html_returns_rendered_html_string() -> None:
    page = _make_scraped_page([_node("a", attributes={"href": "/foo"}, children=[])])
    out = page.build_lean_elements_tree(ElementTreeFormat.HTML)
    assert isinstance(out, str)
    assert out.startswith("<a")
    assert 'href="/foo"' in out


def test_fmt_json_returns_json_string() -> None:
    page = _make_scraped_page([_node("a", attributes={"href": "/foo"}, children=[])])
    out = page.build_lean_elements_tree(ElementTreeFormat.JSON)
    assert isinstance(out, str)
    # Valid JSON serialization of the cached tree.
    import json as _json

    parsed = _json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["tagName"] == "a"


# --- html_need_skyvern_attrs interaction ---------------------------------


def test_html_need_skyvern_attrs_false_drops_top_level_id_from_rendered_html() -> None:
    """Skyvern internal IDs (element['id']) are agent-scaffolding — drop at render
    when html_need_skyvern_attrs=False."""
    page = _make_scraped_page([_node("a", id="AAAB", attributes={"href": "/x"})])
    with_ids = page.build_lean_elements_tree(html_need_skyvern_attrs=True)
    without_ids = page.build_lean_elements_tree(html_need_skyvern_attrs=False)
    assert 'id="AAAB"' in with_ids
    assert 'id="AAAB"' not in without_ids
    # Same href in both — lean recipe is independent of the ID-rendering toggle.
    assert 'href="/x"' in with_ids
    assert 'href="/x"' in without_ids


# --- last_used_element_tree[_html] side effects --------------------------


def test_html_render_writes_last_used_element_tree_html() -> None:
    page = _make_scraped_page([_node("a", attributes={"href": "/foo"})])
    out = page.build_lean_elements_tree(ElementTreeFormat.HTML)
    assert page.last_used_element_tree_html == out
    assert page.last_used_element_tree is not None


def test_json_render_clears_last_used_element_tree_html() -> None:
    """Matches the contract on build_element_tree / build_economy_elements_tree."""
    page = _make_scraped_page([_node("a", attributes={"href": "/foo"})])
    page.build_lean_elements_tree(ElementTreeFormat.HTML)
    assert page.last_used_element_tree_html is not None
    page.build_lean_elements_tree(ElementTreeFormat.JSON)
    assert page.last_used_element_tree_html is None


# --- refresh() invalidates derived caches --------------------------------


@pytest.mark.asyncio
async def test_refresh_clears_lean_and_economy_caches() -> None:
    """Regression: refresh() replaces element_tree_trimmed, so derived caches
    (lean, economy) must be cleared or subsequent build_*_elements_tree calls
    return pre-refresh data. Particularly bad on the complete_verify hot path
    where the verifier reasons post-action."""
    from unittest.mock import patch

    page = _make_scraped_page([_node("a", attributes={"href": "/before"})])

    # Populate both caches from the pre-refresh tree.
    page.build_lean_elements_tree(strip_url_query_strings=True)
    page.build_economy_elements_tree()
    page.last_used_element_tree = [_node("marker", attributes={})]
    assert len(page.lean_element_tree_cache) == 1
    assert page.economy_element_tree is not None
    assert page.last_used_element_tree is not None

    # Simulate refresh: the browser-state scrape returns a new tree.
    refreshed_tree = [_node("a", attributes={"href": "/after"})]
    fake_refreshed = _make_scraped_page(refreshed_tree)
    page._browser_state.scrape_website = AsyncMock(return_value=fake_refreshed)

    with patch.object(type(page), "model_config", page.model_config):
        await page.refresh()

    # All derived caches reset.
    assert page.lean_element_tree_cache == {}
    assert page.economy_element_tree is None
    assert page.last_used_element_tree is None
    assert page.last_used_element_tree_html is None


# --- end-to-end: flag combo actually transforms the output --------------


def test_flag_combo_actually_compresses_in_rendered_output() -> None:
    """Sanity check: a combo of flags reaches the rendered HTML."""
    long_href = "https://example.com/" + "x" * 200
    page = _make_scraped_page(
        [
            _node(
                "a",
                attributes={"href": long_href + "?utm=1"},
                children=[_node("img", attributes={"src": "/cat.jpg?cb=1", "alt": "cat"})],
            )
        ]
    )
    out = page.build_lean_elements_tree(
        compress_long_href=True,
        compress_image_src=True,
        strip_url_query_strings=True,
    )
    assert "#templated" in out  # long href compressed (after query strip)
    assert "/cat.jpg" not in out  # img src dropped
    assert "utm=1" not in out  # query strings gone
    assert "cb=1" not in out
