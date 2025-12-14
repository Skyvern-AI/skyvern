from typing import TYPE_CHECKING

from playwright.async_api import Page

from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.library.skyvern_browser_page_agent import SkyvernBrowserPageAgent
from skyvern.library.skyvern_browser_page_ai import SdkSkyvernPageAi

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser


class SkyvernBrowserPage(SkyvernPage):
    """A browser page wrapper that combines Playwright's page API with Skyvern's AI capabilities.

    This class provides a unified interface for both traditional browser automation (via Playwright)
    and AI-powered task execution (via Skyvern). It exposes standard page methods like click, fill,
    goto, etc., while also providing access to Skyvern's task and workflow execution through the
    `run` attribute.

    Example:
        ```python
        # Use standard Playwright methods
        await page.goto("https://example.com")
        await page.fill("#username", "user@example.com")
        await page.click("#login-button")

        # Or use Skyvern's AI capabilities
        await page.agent.run_task("Fill out the contact form and submit it")
        ```

    Attributes:
        agent: SkyvernBrowserPageAgent instance for executing AI-powered tasks and workflows.
    """

    def __init__(self, browser: "SkyvernBrowser", page: Page):
        super().__init__(page, SdkSkyvernPageAi(browser, page))
        self._browser = browser
        self.agent = SkyvernBrowserPageAgent(browser, page)

    async def act(
        self,
        prompt: str,
    ) -> None:
        """Perform an action on the page using AI based on a natural language prompt.

        Args:
            prompt: Natural language description of the action to perform.

        Examples:
            ```python
            # Simple action
            await page.act("Click the login button")
            ```
        """
        return await self._ai.ai_act(prompt)
