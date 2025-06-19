from typing import Any

from playwright.async_api import async_playwright

from skyvern.core.code_generations.skyvern_page import RunContext, SkyvernPage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_factory import BrowserContextFactory


# TODO: find a better name for this function
async def setup(parameters: dict[str, Any]) -> tuple[SkyvernPage, RunContext]:
    # set up skyvern context
    skyvern_context.set(skyvern_context.SkyvernContext())
    # start playwright
    pw = await async_playwright().start()
    (
        browser_context,
        _,
        _,
    ) = await BrowserContextFactory.create_browser_context(playwright=pw)
    new_page = await browser_context.new_page()
    skyvern_page = SkyvernPage(page=new_page)
    return skyvern_page, RunContext(parameters=parameters, page=skyvern_page)
