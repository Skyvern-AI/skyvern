"""No-browser unit coverage for the SKY-14275 opportunistic stale-action remap.

The browser_e2e replay for this behaviour self-skips on CI shards without Chromium, so the critical
contract is protected here in a normal unit shard: the remap helper drives its REAL logic (per-instance
identity anchor, position-independent structural uniqueness, document/URL continuity, in-place rebind,
provenance refresh, and fall-through) with only the browser primitives (``resolve_locator``/``count``/
re-scrape) mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction

pytestmark = pytest.mark.asyncio

PAGE_URL = "https://example.test/page"


def _element(
    text: str,
    *,
    html_id: str | None = None,
    name: str | None = None,
    aria_label: str | None = None,
    data_testid: str | None = None,
    xpath: str = "/x",
) -> dict[str, Any]:
    # ``html_id`` (document-unique by spec) is the only per-instance identity. ``name`` is NOT reliably
    # per-instance (repeated rows/wizard states expose one same-name control per snapshot), and
    # ``aria_label``/``data_testid`` (plus the always-present ``aria-expanded`` state) are generic
    # role/state attributes shared across repeated component instances.
    attributes: dict[str, str] = {"class": "ctl", "aria-expanded": "false"}
    if html_id:
        attributes["id"] = html_id
    if name:
        attributes["name"] = name
    if aria_label:
        attributes["aria-label"] = aria_label
    if data_testid:
        attributes["data-testid"] = data_testid
    return {"tagName": "button", "attributes": attributes, "text": text, "children": [], "xpath": xpath}


def _scraped(
    elements_by_id: dict[str, dict[str, Any]],
    *,
    rescrape: Any = None,
    url: str = PAGE_URL,
    frames: dict[str, str] | None = None,
) -> SimpleNamespace:
    # ``frames`` maps element_id -> frame token; unspecified elements default to the main frame. Only
    # ``main.frame`` is stable across scrapes -- an iframe token is a per-scrape skyvern id.
    frame_by_id = {eid: (frames or {}).get(eid, "main.frame") for eid in elements_by_id}
    page = SimpleNamespace(
        id_to_element_dict=elements_by_id,
        id_to_css_dict={eid: f'[unique_id="{eid}"]' for eid in elements_by_id},
        id_to_frame_dict=frame_by_id,
        id_to_element_hash={eid: f"hash-{eid}" for eid in elements_by_id},
        url=url,
    )
    if rescrape is not None:
        page.generate_scraped_page_without_screenshots = rescrape
    return page


def _live_page(url: str = PAGE_URL) -> SimpleNamespace:
    # A live-page stand-in whose ``url`` is a real string (the pre-refresh document-continuity check
    # only engages when both urls are real strings; resolve_locator is mocked so nothing else is used).
    return SimpleNamespace(url=url)


def _mock_resolve_locator(
    monkeypatch: pytest.MonkeyPatch, count: int, *, marker_count: int = 1, marker_raises: bool = False
) -> None:
    # ``count`` is the exact injected node's live count; ``marker_count`` is how many injected markers
    # survive in the planned element's frame (0 == the whole document was replaced).
    locator = MagicMock()
    locator.count = AsyncMock(return_value=count)
    marker_locator = MagicMock()
    if marker_raises:
        marker_locator.count = AsyncMock(side_effect=RuntimeError("marker probe failed"))
    else:
        marker_locator.count = AsyncMock(return_value=marker_count)
    frame_content = MagicMock()
    frame_content.locator = MagicMock(return_value=marker_locator)
    monkeypatch.setattr(handler, "resolve_locator", AsyncMock(return_value=(locator, frame_content)))


async def test_successful_remap_rebinds_caller_action_and_provenance_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale but structurally-unique id-anchored target is remapped, and the CALLER's own Action is
    rebound in place -- element_id AND the provenance used by get_xpath()/cached-action matching
    (skyvern_element_hash, skyvern_element_data) -- to the fresh element, not left stale."""
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field", xpath="/old")})
    fresh = _scraped({"FRESH": _element("Approve", html_id="approve_field", xpath="/fresh")})
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)  # exact injected node is gone -> stale

    action = ClickAction(
        element_id="OLD",
        skyvern_element_hash="hash-OLD",
        skyvern_element_data={"xpath": "/old", "page_url": PAGE_URL},
    )
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is not None
    fresh_page, rebound = result
    assert fresh_page is fresh
    assert rebound is action  # same object -> caller-visible state carries the remap
    assert action.element_id == "FRESH"
    assert action.skyvern_element_hash == "hash-FRESH"
    assert action.get_xpath() == "/fresh"  # provenance refreshed from the fresh element
    assert action.skyvern_element_data is not None
    assert action.skyvern_element_data["page_url"] == PAGE_URL


async def test_name_only_anchor_declines_remap(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED guard: ``name`` is not reliably per-instance -- a single-visible same-name control can be a
    different repeated row/wizard-state instance after a transition -- so a target anchored only by
    ``name`` (no id) declines and falls through to the legacy path."""
    batch = _scraped({"OLD": _element("Approve", name="approve")})  # only name; no id
    fresh = _scraped({"NEW": _element("Approve", name="approve")})
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"


async def test_generic_aria_anchor_declines_remap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target anchored only by generic role/state attributes (aria-label / aria-expanded) is not a
    per-instance identity, so the helper declines and falls through to the legacy path."""
    batch = _scraped({"OLD": _element("Delete", aria_label="Delete")})  # only aria-*; no id
    fresh = _scraped({"NEW": _element("Delete", aria_label="Delete")})
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"


async def test_generic_data_testid_anchor_declines_remap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic data-testid (e.g. ``delete``) is not a per-instance identity either, so it declines
    rather than remap across a repeated-component replacement."""
    batch = _scraped({"OLD": _element("Delete", data_testid="delete")})  # only data-*; no id
    fresh = _scraped({"NEW": _element("Delete", data_testid="delete")})
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"


async def test_navigation_live_url_mismatch_declines_before_rescrape(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED guard: if an earlier batch action navigated/switched document, the live page url no longer
    matches the page the batch was planned on, so the helper declines BEFORE re-scraping -- it must not
    remap the planned action onto an identically-structured control on the destination page."""
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape, url=PAGE_URL)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(
        batch, _live_page(url="https://example.test/destination"), action
    )

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_navigation_refreshed_url_mismatch_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED guard: even if the pre-refresh url looked consistent, a refresh that lands on a different
    document (the destination page carries an identically-structured control) is rejected -- the remap
    must not bind the planned action onto the destination page's look-alike."""
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, url=PAGE_URL)
    fresh = _scraped({"DEST": _element("Approve", html_id="approve_field")}, url="https://example.test/destination")
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, MagicMock(), action)

    assert result is None
    assert action.element_id == "OLD"


async def test_cross_frame_only_candidate_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED guard: the main-frame target is gone and the only structurally-identical id-anchored match
    lives in a DIFFERENT frame. HTML ids are only document-unique, so rebinding across frames would
    actuate the wrong document. The refresh candidate must be restricted to the target's frame, so with
    a match only in another frame the helper declines and leaves the action/provenance untouched."""
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")})  # main.frame
    fresh = _scraped(
        {"IN_IFRAME": _element("Approve", html_id="approve_field")}, frames={"IN_IFRAME": "iframe_token_1"}
    )
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD", skyvern_element_hash="hash-OLD", skyvern_element_data={"xpath": "/old"})
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    assert action.skyvern_element_hash == "hash-OLD"  # provenance untouched on decline
    assert action.get_xpath() == "/old"


async def test_iframe_target_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED guard: the target itself lives in an iframe, whose frame token is a per-scrape skyvern id
    (not stable across scrapes), so its frame identity cannot be established for a refresh -- decline
    without re-scraping rather than risk rebinding across documents."""
    rescrape = AsyncMock()
    batch = _scraped(
        {"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape, frames={"OLD": "iframe_a"}
    )
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_same_main_frame_candidate_still_remaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-frame (main) unique id-anchored match still remaps -- the frame restriction does not
    break main-frame recovery even when a decoy with the same id exists in another frame."""
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")})
    fresh = _scraped(
        {
            "FRESH": _element("Approve", html_id="approve_field"),
            "IN_IFRAME": _element("Approve", html_id="approve_field"),
        },
        frames={"IN_IFRAME": "iframe_token_1"},
    )
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is not None
    _, rebound = result
    assert rebound is action
    assert action.element_id == "FRESH"  # the main-frame match, never the iframe decoy


async def test_full_document_replacement_zero_markers_declines_before_rescrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED guard: a same-URL full document replacement (reload / postback) leaves zero injected markers
    in the planned frame. The helper must decline WITHOUT re-scraping -- re-scraping the replacement
    would re-inject markers on scan-order strangers and can recycle a later sibling's id onto the wrong
    element."""
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape)
    _mock_resolve_locator(monkeypatch, count=0, marker_count=0)  # stale, and no markers survive

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_marker_survival_probe_exception_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the marker-survival probe cannot be evaluated (raises), the helper declines and falls through
    to the legacy path without re-scraping."""
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape)
    _mock_resolve_locator(monkeypatch, count=0, marker_raises=True)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_live_exact_node_dispatches_unchanged_without_rescrape(monkeypatch: pytest.MonkeyPatch) -> None:
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape)
    _mock_resolve_locator(monkeypatch, count=1)  # exact injected node still live

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_ambiguous_identity_falls_through_without_rescrape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two controls sharing an id (a malformed / repeated page) are structurally identical, so the
    pre-batch identity is non-unique: the helper declines to remap BEFORE re-scraping and the original
    binding falls through to the legacy path (no synthesized failure/skip)."""
    rescrape = AsyncMock()
    batch = _scraped(
        {"A": _element("Approve", html_id="dup"), "B": _element("Approve", html_id="dup")}, rescrape=rescrape
    )
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="A")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "A"
    rescrape.assert_not_awaited()


async def test_unresolved_remap_after_rescrape_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale, anchored, and unique before the refresh, but the refreshed page has no unique structural
    match (removed / volatile identity): the original binding falls through unchanged."""
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")})
    fresh = _scraped({}, url=PAGE_URL)  # target gone after remount, same document
    batch.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"


async def test_anchorless_target_falls_through_without_rescrape(monkeypatch: pytest.MonkeyPatch) -> None:
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve")}, rescrape=rescrape)  # only class + aria-expanded state
    _mock_resolve_locator(monkeypatch, count=0)

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()


async def test_coordinate_click_falls_through() -> None:
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")})
    action = ClickAction(element_id="OLD", x=5, y=5)
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None


async def test_indeterminate_probe_falls_through_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """If liveness cannot be confirmed (resolve_locator raises), the helper declines to remap and
    falls through with the original binding -- the legacy handler (incl. its positional xpath fallback)
    stays authoritative. It must not re-scrape or rebind, even when a fresh match would exist."""
    rescrape = AsyncMock()
    batch = _scraped({"OLD": _element("Approve", html_id="approve_field")}, rescrape=rescrape)
    monkeypatch.setattr(handler, "resolve_locator", AsyncMock(side_effect=RuntimeError("cdp hiccup")))

    action = ClickAction(element_id="OLD")
    result = await handler._refresh_stale_web_action_before_dispatch(batch, _live_page(), action)

    assert result is None
    assert action.element_id == "OLD"
    rescrape.assert_not_awaited()
