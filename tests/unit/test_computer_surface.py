"""Characterize the browser-backed computer surface with a real page."""

import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from skyvern.webeye.computer_surface import BrowserComputerSurface
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.utils.page import SkyvernFrame
from tests.unit.chromium_probe import chromium_available


@pytest.mark.asyncio
async def test_browser_computer_surface_preserves_frame_and_input() -> None:
    if not await asyncio.to_thread(chromium_available):
        pytest.skip("Requires Playwright Chromium")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--use-mock-keychain", "--password-store=basic"]
        )
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.set_content(
                "<button id='button' onclick='this.textContent = String(Number(this.textContent) + 1)'>0</button>"
                "<input id='field'><div style='height: 1800px'></div>"
                "<script>window.mouseEvents = [];"
                "for (const name of ['mousedown', 'mousemove', 'mouseup'])"
                " document.addEventListener(name, () => window.mouseEvents.push(name));</script>"
            )

            async def capture() -> ScrapedPage:
                return ScrapedPage.model_construct(
                    screenshots=await SkyvernFrame.take_split_screenshots(page, scroll=False)
                )

            surface = BrowserComputerSurface(page, scraped_page=SimpleNamespace(), capture=capture)

            assert (await surface.get_frame()).screenshots[0] == (
                await SkyvernFrame.take_split_screenshots(page, scroll=False)
            )[0]

            button = await page.locator("#button").bounding_box()
            assert button is not None
            x, y = int(button["x"] + button["width"] / 2), int(button["y"] + button["height"] / 2)
            await surface.click(x, y, "left", 1)
            assert await page.locator("#button").text_content() == "1"
            await surface.click(x, y, "left", 2)
            await surface.click(x, y, "left", 3)
            assert await page.locator("#button").text_content() == "6"

            await page.locator("#field").focus()
            await surface.type_text("abc")
            await surface.key(["Backspace"], repeat=2)
            assert await page.locator("#field").input_value() == "a"

            await surface.scroll(100, 100, 0, 500)
            await page.wait_for_function("window.scrollY > 0")
            await page.evaluate("window.scrollTo(0, 0)")
            await surface.drag(50, 50, [(80, 80)])
            events = await page.evaluate("window.mouseEvents")
            assert "mousedown" in events and "mouseup" in events and "mousemove" in events
            await surface.move(100, 100)
            await surface.left_mouse(None, None, "down")
            await surface.left_mouse(None, None, "up")
            await surface.wait(0)
            assert (await page.evaluate("window.mouseEvents"))[-1] == "mouseup"
        finally:
            await browser.close()
