from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext, Page

from skyvern.client import AsyncSkyvern
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage

if TYPE_CHECKING:
    from skyvern.library.skyvern_sdk import SkyvernSdk


class SkyvernBrowser:
    """A browser context wrapper that creates Skyvern-enabled pages.

    This class wraps a Playwright BrowserContext and provides methods to create
    SkyvernBrowserPage instances that combine traditional browser automation with
    AI-powered task execution capabilities. It manages browser session state and
    enables persistent browser sessions across multiple pages.

    Example:
        ```python
            sdk = SkyvernSdk()
            browser = await sdk.launch_local_browser()

            # Get or create the working page
            page = await browser.get_working_page()

            # Create a new page
            new_page = await browser.new_page()
        ```

    Attributes:
        _browser_context: The underlying Playwright BrowserContext.
        _browser_session_id: Optional session ID for persistent browser sessions.
        _browser_address: Optional address for remote browser connections.
        _client: The AsyncSkyvern client for API communication.
    """

    def __init__(
        self,
        sdk: "SkyvernSdk",
        browser_context: BrowserContext,
        *,
        browser_session_id: str | None = None,
        browser_address: str | None = None,
    ):
        self._sdk = sdk
        self._browser_context = browser_context
        self._browser_session_id = browser_session_id
        self._browser_address = browser_address

        self.workflow_run_id: None | str = None

    @property
    def browser_session_id(self) -> str | None:
        return self._browser_session_id

    @property
    def browser_address(self) -> str | None:
        return self._browser_address

    @property
    def client(self) -> AsyncSkyvern:
        return self._sdk.api

    @property
    def sdk(self) -> "SkyvernSdk":
        return self._sdk

    async def get_working_page(self) -> SkyvernBrowserPage:
        """Get the most recent page or create a new one if none exists.

        This method returns the last page in the browser context, or creates a new page
        if the context has no pages. This is useful for continuing work on an existing
        page without creating unnecessary new tabs.

        Returns:
            SkyvernBrowserPage: The most recent page wrapped with Skyvern capabilities.
        """
        if self._browser_context.pages:
            page = self._browser_context.pages[-1]
        else:
            page = await self._browser_context.new_page()
        return await self._create_skyvern_page(page)

    async def new_page(self) -> SkyvernBrowserPage:
        """Create a new page (tab) in the browser context.

        This method always creates a new page, similar to opening a new tab in a browser.
        The new page will have both Playwright's standard API and Skyvern's AI capabilities.

        Returns:
            SkyvernBrowserPage: A new page wrapped with Skyvern capabilities.
        """
        page = await self._browser_context.new_page()
        return await self._create_skyvern_page(page)

    async def _create_skyvern_page(self, page: Page) -> SkyvernBrowserPage:
        return SkyvernBrowserPage(self, page)
