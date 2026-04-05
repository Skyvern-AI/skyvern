from typing import TYPE_CHECKING, Any

from playwright.async_api import Frame, Page

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

    async def frame_switch(
        self,
        *,
        selector: str | None = None,
        name: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Switch the working context to an iframe. Exactly one parameter required.

        Args:
            selector: CSS selector for the iframe element (uses content_frame()).
            name: Frame name attribute.
            index: Frame index in page.frames list (0 = main frame).

        Returns:
            Dict with frame name, url, and the parameter used to switch.
        """
        params = sum(p is not None for p in (selector, name, index))
        if params != 1:
            raise ValueError("Exactly one of selector, name, or index is required.")

        frame: Frame | None = None

        if selector is not None:
            element = await self.page.query_selector(selector)
            if element is None:
                raise ValueError(f"Selector '{selector}' did not match any element.")
            frame = await element.content_frame()
            if frame is None:
                raise ValueError(f"Selector '{selector}' did not resolve to an iframe.")

        elif name is not None:
            frame = self.page.frame(name=name)
            if frame is None:
                raise ValueError(f"No frame found with name '{name}'.")

        elif index is not None:
            frames = self.page.frames
            if index < 0 or index >= len(frames):
                raise ValueError(f"Frame index {index} out of range (0-{len(frames) - 1}).")
            frame = frames[index]

        self._working_frame = frame
        return {
            "name": frame.name if frame else None,
            "url": frame.url if frame else None,
            "selector": selector,
            "frame_name": name,
            "index": index,
        }

    def frame_main(self) -> dict[str, str]:
        """Switch back to the main page frame, clearing the working iframe."""
        self._working_frame = None
        return {"status": "switched_to_main_frame"}

    async def frame_list(self) -> list[dict[str, Any]]:
        """List all frames on the current page.

        Returns:
            List of dicts with name, url, is_main, and index for each frame.
        """
        frames = self.page.frames
        return [
            {
                "index": i,
                "name": f.name,
                "url": f.url,
                "is_main": f == self.page.main_frame,
            }
            for i, f in enumerate(frames)
        ]

    async def act(
        self,
        prompt: str,
        skip_refresh: bool = False,
        use_economy_tree: bool = False,
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
        return await self._ai.ai_act(prompt, skip_refresh=skip_refresh, use_economy_tree=use_economy_tree)
