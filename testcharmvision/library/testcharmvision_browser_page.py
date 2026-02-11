from typing import TYPE_CHECKING

from playwright.async_api import Page

from testcharmvision.core.script_generations.testcharmvision_page import TestcharmvisionPage
from testcharmvision.library.testcharmvision_browser_page_agent import TestcharmvisionBrowserPageAgent
from testcharmvision.library.testcharmvision_browser_page_ai import SdkTestcharmvisionPageAi

if TYPE_CHECKING:
    from testcharmvision.library.testcharmvision_browser import TestcharmvisionBrowser


class TestcharmvisionBrowserPage(TestcharmvisionPage):
    """A browser page wrapper that combines Playwright's page API with Testcharmvision's AI capabilities.

    This class provides a unified interface for both traditional browser automation (via Playwright)
    and AI-powered task execution (via Testcharmvision). It exposes standard page methods like click, fill,
    goto, etc., while also providing access to Testcharmvision's task and workflow execution through the
    `run` attribute.

    Example:
        ```python
        # Use standard Playwright methods
        await page.goto("https://example.com")
        await page.fill("#username", "user@example.com")
        await page.click("#login-button")

        # Or use Testcharmvision's AI capabilities
        await page.agent.run_task("Fill out the contact form and submit it")
        ```

    Attributes:
        agent: TestcharmvisionBrowserPageAgent instance for executing AI-powered tasks and workflows.
    """

    def __init__(self, browser: "TestcharmvisionBrowser", page: Page):
        super().__init__(page, SdkTestcharmvisionPageAi(browser, page))
        self._browser = browser
        self.agent = TestcharmvisionBrowserPageAgent(browser, page)

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
