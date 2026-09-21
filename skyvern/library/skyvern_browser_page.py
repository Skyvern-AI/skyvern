# ruff: noqa: E402
import asyncio
from contextvars import Context
from typing import TYPE_CHECKING, Any

from skyvern.exceptions import require_local_extra_modules

require_local_extra_modules("skyvern.library.skyvern_browser_page")

from playwright.async_api import Frame, Page

from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.forge.sdk.api.files import local_file_requires_organization
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
        self._file_organization_id: str | None = None
        self.agent = SkyvernBrowserPageAgent(browser, page)

    async def _get_file_organization_id(self, file_url: str) -> str | None:
        organization_id = await super()._get_file_organization_id(file_url)
        if organization_id:
            return organization_id
        if not local_file_requires_organization(file_url):
            return None
        if self._file_organization_id is not None:
            return self._file_organization_id
        client = self._browser.skyvern
        if not client._api_key and client._embedded_client is None:
            return None
        # Embedded middleware must not inherit or mutate the caller's context object.
        response = await asyncio.create_task(
            client._client_wrapper.httpx_client.request("api/v1/organizations/me", method="GET"),
            context=Context(),
        )
        response.raise_for_status()
        organization_id = response.json().get("organization_id")
        if not isinstance(organization_id, str) or not organization_id:
            raise PermissionError("Authenticated organization response has no organization ID")
        self._file_organization_id = organization_id
        return organization_id

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

        self._working_frame = None if frame is self.page.main_frame else frame
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
