"""SCROLL action progress against a real DOM (SKY-18891).

``handle_scroll_action`` drives ``scrollNearestScrollableContainer`` from domUtils.js, so these
tests run the real JS in a real Chromium page: a fake DOM cannot reproduce ``scrollTop`` clamping,
``overflow`` resolution, or a virtualized list's re-render on scroll, which is exactly where the
reported bug lived.

Covered contracts:
  * an open virtualized dropdown whose scroll viewport is a DESCENDANT of the targeted listbox
    scrolls that viewport, not the page behind it;
  * the list advances roughly one viewport per action, so a mid-list option stays reachable
    instead of being skipped by a jump to the first/last row;
  * a scroll that moves nothing reports failure, so the planner stops repeating it;
  * scrolls on wrappers do not get stolen by nested textareas;
  * prose containers with embedded controls still reach their bottom in one smooth-scroll action;
  * page-level wheel fallback recognizes when a center-point overlay consumed the wheel.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from playwright.async_api import Page, async_playwright

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ScrollAction
from skyvern.webeye.actions.responses import ActionResult
from skyvern.webeye.utils.dom import SkyvernElement
from skyvern.webeye.utils.page import JS_FUNCTION_DEFS


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

# "United States" sits deep in the list, like the country dropdown in the report: a jump to either
# extreme lands on "Afghanistan" or "Zimbabwe" and never shows it.
_COUNTRIES = (
    ["Afghanistan", "Albania", "Algeria", "American Samoa"]
    + [f"Country {index:03d}" for index in range(220)]
    + ["United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vietnam", "Yemen", "Zambia", "Zimbabwe"]
)
_ROW_HEIGHT = 30
_VIEWPORT_ROWS = 7

# A virtualized listbox: the targeted role=listbox clips its content, and the element that actually
# owns scrollTop is the viewport DIV inside it. Only the rows in view are rendered, so scrolling
# past them loses the options in between. The tall filler below keeps the page itself scrollable,
# which is what the old code wheeled instead of the list.
_DROPDOWN_HTML = f"""
<html><body style="margin:0">
  <div id="country-listbox" role="listbox" aria-label="Country"
       style="overflow:hidden;height:{_ROW_HEIGHT * _VIEWPORT_ROWS}px;width:300px">
    <div id="viewport" style="overflow-y:auto;height:100%;position:relative">
      <div id="spacer" style="height:{_ROW_HEIGHT * len(_COUNTRIES)}px;position:relative"></div>
    </div>
  </div>
  <div style="height:4000px">filler</div>
  <script>
    const COUNTRIES = {_COUNTRIES!r};
    const ROW_HEIGHT = {_ROW_HEIGHT};
    const viewport = document.getElementById("viewport");
    const spacer = document.getElementById("spacer");
    function render() {{
      const first = Math.floor(viewport.scrollTop / ROW_HEIGHT);
      const last = Math.min(COUNTRIES.length, first + {_VIEWPORT_ROWS} + 1);
      spacer.replaceChildren();
      for (let index = first; index < last; index++) {{
        const row = document.createElement("div");
        row.id = "option-" + index;
        row.setAttribute("role", "option");
        row.style.cssText =
          "position:absolute;height:" + ROW_HEIGHT + "px;top:" + index * ROW_HEIGHT + "px";
        row.textContent = COUNTRIES[index];
        spacer.appendChild(row);
      }}
    }}
    viewport.addEventListener("scroll", render);
    render();
  </script>
</body></html>
""".replace("'", '"')

# A terms & conditions box: prose in a scrollable container, no option semantics anywhere. One
# SCROLL must still take it all the way to the bottom, which is what enables the accept button.
_TERMS_HTML = """
<html><body style="margin:0">
  <div id="terms" style="overflow-y:auto;scroll-behavior:smooth;height:200px;width:400px">
    <p style="height:3000px">Terms and conditions.</p>
    <select><option>English</option></select>
    <div role="menu"><button role="menuitem">Language</button></div>
  </div>
  <button id="accept" disabled>Accept</button>
</body></html>
"""

_CLOSED_OPTION_CONTROLS_MODAL_HTML = """
<html><body style="margin:0">
  <div id="modal" style="overflow-y:auto;height:200px;width:400px">
    <select id="native-select"><option>English</option></select>
    <div id="combobox" role="combobox" aria-expanded="false">Choose an option</div>
    <div style="height:3000px">Modal content</div>
  </div>
</body></html>
"""

_TEXTAREA_PANEL_HTML = """
<html><body style="margin:0">
  <div id="panel" style="overflow-y:auto;height:200px;width:400px">
    <div style="height:30px">Panel content</div>
    <div id="wrapper"><textarea id="inner" style="overflow-y:auto;height:80px;width:350px"></textarea></div>
    <div style="height:2000px">More panel content</div>
  </div>
  <div style="height:1000px">Page filler</div>
</body></html>
"""

# Terms rendered inline with the page body as the only scrollable area — the case the page-level
# wheel fallback exists for, since no sub-container is scrollable near the button.
_INLINE_TERMS_HTML = """
<html><body style="margin:0">
  <p style="height:4000px">Terms and conditions.</p>
  <button id="accept" disabled>Accept</button>
</body></html>
"""

# The center-point wheel can hit a nested portal overlay outside the target element's subtree.
_CENTER_OVERLAY_HTML = """
<html><body style="margin:0">
  <p style="height:4000px">Terms and conditions.</p>
  <button id="accept" disabled>Accept</button>
  <div style="position:fixed;left:400px;top:200px;z-index:10">
    <div id="overlay-panel" style="overflow-y:auto;height:400px;width:480px;background:white">
      <div style="height:10000px">Overlay content</div>
    </div>
  </div>
</body></html>
"""


@contextlib.asynccontextmanager
async def _fixture_page(html: str) -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--use-mock-keychain", "--password-store=basic"])
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            await page.set_content(html)
            # handle_scroll_action evaluates scrollNearestScrollableContainer as a page global, so
            # the page needs the same domUtils bundle the scraper injects in production.
            await page.evaluate(JS_FUNCTION_DEFS)
            yield page
        finally:
            await browser.close()


async def _scroll(page: Page, css: str, direction: str = "down") -> list[ActionResult]:
    """Run the real handler, stubbing only the element-id -> locator lookup."""

    element_id = css.lstrip("#")
    static: dict[str, Any] = {"id": element_id, "tagName": "div", "attributes": {}}

    class _StubDom:
        async def safe_get_skyvern_element_by_id(self, _: str) -> SkyvernElement:
            return SkyvernElement(page.locator(css), page, static)

    action = ScrollAction(element_id=element_id, scroll_x=0, scroll_y=500 if direction == "down" else -500)
    with patch.object(handler, "DomUtil", lambda **_: _StubDom()):
        return await handler.handle_scroll_action(
            action=action,
            page=page,
            scraped_page=MagicMock(),
            task=MagicMock(),
            step=MagicMock(),
        )


async def _visible_options(page: Page) -> list[str]:
    # The fixture re-renders its rows from the scroll event, which fires a frame after scrollTop
    # changes, so wait until the rendered rows match the current scroll position before reading.
    await page.wait_for_function(
        "() => { const viewport = document.getElementById('viewport');"
        " const first = document.querySelector('#spacer [role=option]');"
        f" return !!first && first.offsetTop === Math.floor(viewport.scrollTop / {_ROW_HEIGHT}) * {_ROW_HEIGHT}; }}"
    )
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll('#spacer [role=option]')).map((row) => row.textContent)"
    )


@_skip_no_browser
@pytest.mark.asyncio
async def test_virtualized_dropdown_scrolls_the_list_not_the_page() -> None:
    async with _fixture_page(_DROPDOWN_HTML) as page:
        before = await _visible_options(page)
        assert before[0] == "Afghanistan"

        results = await _scroll(page, "#country-listbox")

        assert results[-1].success, results
        after = await _visible_options(page)
        assert after != before, "the open dropdown did not scroll at all"
        assert after[0] != _COUNTRIES[-_VIEWPORT_ROWS], f"scroll jumped to the end of the list: {after}"
        assert await page.evaluate("() => window.scrollY") == 0, "the page scrolled instead of the list"


@_skip_no_browser
@pytest.mark.asyncio
async def test_virtualized_dropdown_scroll_from_option_row_does_not_jump_to_end() -> None:
    async with _fixture_page(_DROPDOWN_HTML) as page:
        results = await _scroll(page, "#option-0")

        assert results[-1].success, results
        scroll_top, max_scroll = await page.locator("#viewport").evaluate(
            "el => [el.scrollTop, el.scrollHeight - el.clientHeight]"
        )
        assert 0 < scroll_top < max_scroll, "scrolling from an option row jumped to the end"


@_skip_no_browser
@pytest.mark.asyncio
async def test_virtualized_dropdown_walks_to_a_mid_list_option_without_skipping() -> None:
    async with _fixture_page(_DROPDOWN_HTML) as page:
        target_index = _COUNTRIES.index("United States")
        # Generous: the contract under test is that the walk terminates and shows everything on
        # the way, not the exact step size.
        max_scrolls = len(_COUNTRIES)
        seen: set[str] = set()

        for _ in range(max_scrolls):
            options = await _visible_options(page)
            seen.update(options)
            if "United States" in options:
                break
            results = await _scroll(page, "#country-listbox")
            assert results[-1].success, results
        else:
            pytest.fail(f"never reached index {target_index} in {max_scrolls} scrolls")

        # The reported bug was a scroll that skipped the options in between, so the agent could
        # never read them. Every option above the target must have been on screen on the way down.
        assert set(_COUNTRIES[:target_index]) <= seen


@_skip_no_browser
@pytest.mark.asyncio
async def test_scroll_at_the_end_of_the_list_reports_failure() -> None:
    async with _fixture_page(_DROPDOWN_HTML) as page:
        await page.evaluate("() => { const v = document.getElementById('viewport'); v.scrollTop = v.scrollHeight; }")
        pinned = await _visible_options(page)

        results = await _scroll(page, "#country-listbox")

        assert not results[-1].success, "a scroll that revealed nothing reported success"
        assert "revealed no new content" in (results[-1].exception_message or "")
        assert results[-1].stop_execution_on_failure is False
        assert await _visible_options(page) == pinned


@_skip_no_browser
@pytest.mark.asyncio
async def test_page_level_scroll_reports_progress_then_failure_at_the_bottom() -> None:
    async with _fixture_page(_INLINE_TERMS_HTML) as page:
        first = await _scroll(page, "#accept")

        assert first[-1].success, first
        settled = await page.evaluate("() => window.scrollY")
        assert settled > 0, "the page-level fallback did not scroll the page"

        repeat = await _scroll(page, "#accept")

        assert not repeat[-1].success, "a page-level scroll that moved nothing reported success"
        assert "revealed no new content" in (repeat[-1].exception_message or "")
        assert await page.evaluate("() => window.scrollY") == settled


@_skip_no_browser
@pytest.mark.asyncio
async def test_prose_container_still_reaches_its_bottom_in_one_action() -> None:
    async with _fixture_page(_TERMS_HTML) as page:
        results = await _scroll(page, "#terms")

        assert results[-1].success, results
        assert await page.evaluate(
            "() => { const t = document.getElementById('terms');"
            " return t.scrollTop + t.clientHeight >= t.scrollHeight - 1; }"
        ), "the terms box did not reach its bottom"


@_skip_no_browser
@pytest.mark.parametrize("css", ["#native-select", "#combobox"])
@pytest.mark.asyncio
async def test_closed_option_control_scrolls_ancestor_modal_to_bottom(css: str) -> None:
    async with _fixture_page(_CLOSED_OPTION_CONTROLS_MODAL_HTML) as page:
        results = await _scroll(page, css)

        assert results[-1].success, results
        assert await page.locator("#modal").evaluate("el => el.scrollTop + el.clientHeight >= el.scrollHeight - 1"), (
            "the modal did not reach its bottom"
        )


@_skip_no_browser
@pytest.mark.asyncio
async def test_wrapper_scrolls_its_panel_not_a_nested_textarea() -> None:
    async with _fixture_page(_TEXTAREA_PANEL_HTML) as page:
        await page.locator("#inner").fill("\n".join(f"line {index}" for index in range(100)))
        await page.evaluate(
            "() => { document.getElementById('inner').scrollTop = 0; document.getElementById('panel').scrollTop = 0; }"
        )
        assert await page.locator("#inner").evaluate("el => el.scrollHeight > el.clientHeight")

        results = await _scroll(page, "#wrapper")

        assert results[-1].success, results
        assert await page.locator("#panel").evaluate("el => el.scrollTop + el.clientHeight >= el.scrollHeight - 1"), (
            "the panel did not scroll to its bottom"
        )
        assert await page.locator("#inner").evaluate("el => el.scrollTop") == 0


@_skip_no_browser
@pytest.mark.asyncio
async def test_page_level_wheel_recognizes_progress_in_center_overlay() -> None:
    async with _fixture_page(_CENTER_OVERLAY_HTML) as page:
        results = await _scroll(page, "#accept")

        assert results[-1].success, results
        assert await page.evaluate("() => window.scrollY") == 0
        assert await page.locator("#overlay-panel").evaluate("el => el.scrollTop") > 0
        assert results[-1].data == {"container_scroll": True}
        assert await page.evaluate("() => window.__skyvernScrolledElementId") == "accept"
