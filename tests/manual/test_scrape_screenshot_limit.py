"""Offline browser integration test and scrape-only benchmark; no server or LLM needed.

Run explicitly (manual tests are excluded from the default suite):
    uv run playwright install chromium
    uv run pytest tests/manual/test_scrape_screenshot_limit.py -q -s

Times cover a scrape followed by an extraction-style refresh, not a complete workflow.
"""

import json
from statistics import median
from time import perf_counter

import pytest
from playwright.async_api import Route, async_playwright

from skyvern.config import settings
from skyvern.forge import (
    begin_run_scoped_app_attributes,
    end_run_scoped_app_attributes,
    set_run_scoped_app_attribute,
)
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.real_browser_state import RealBrowserState

_URL = "https://scrape-benchmark.test/"
_HTML = (
    "<html><body>"
    + "".join(
        f'<section style="height:800px"><h2>Invoice {i}</h2><p>Amount: ${i * 12}</p>'
        f"<button>Open invoice {i}</button></section>"
        for i in range(1, 13)
    )
    + "</body></html>"
)


@pytest.mark.asyncio
async def test_scrape_budget_reduces_capture_work_without_changing_dom_text() -> None:
    """Exercise real Chromium captures, refreshes, and DOM extraction on a fixed long page."""
    timings: dict[str, list[float]] = {"default": [], "cap_1": []}
    counts: dict[str, list[int]] = {"default": [], "cap_1": []}
    texts: list[str] = []
    functions = AgentFunction()
    app_token = begin_run_scoped_app_attributes()
    set_run_scoped_app_attribute("AGENT_FUNCTION", functions)
    set_run_scoped_app_attribute("SVG_CSS_CONVERTER_LLM_API_HANDLER", None)

    async def serve_page(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=_HTML)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                browser_context = await browser.new_context(viewport={"width": 1280, "height": 800})
                async with browser_context as context:
                    await context.route("**/*", serve_page)
                    page = await context.new_page()
                    state = RealBrowserState(
                        pw,
                        browser_context=context,
                        page=page,
                        external_browser=True,
                    )
                    # Warm browser, JS, and font caches before measuring alternating arms.
                    arms = (("default", None), ("cap_1", 1))
                    for iteration in range(4):
                        for label, cap in arms:
                            await page.goto(_URL)
                            with skyvern_context.scoped(SkyvernContext(max_scrape_screenshots=cap)):
                                started = perf_counter()
                                scraped = await state.scrape_website(
                                    _URL,
                                    functions.cleanup_element_tree_factory(),
                                )
                                first_count = len(scraped.screenshots)
                                await scraped.refresh()
                                elapsed = perf_counter() - started
                            if iteration:
                                timings[label].append(elapsed)
                                counts[label].append(first_count + len(scraped.screenshots))
                                texts.append(scraped.extracted_text)
                        arms = arms[::-1]
            finally:
                await browser.close()
    finally:
        end_run_scoped_app_attributes(app_token)

    assert len(set(texts)) == 1
    assert "Invoice 12" in texts[0]
    assert counts["cap_1"] == [2, 2, 2]
    assert all(count > 2 for count in counts["default"])
    results = {}
    for label, samples in timings.items():
        results[label] = {
            "median_seconds": round(median(samples), 3),
            "screenshots": counts[label],
        }
    results["server_default"] = settings.MAX_NUM_SCREENSHOTS
    print("\nScrape + refresh benchmark:\n" + json.dumps(results, indent=2))
