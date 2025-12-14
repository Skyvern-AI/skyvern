from typing import TYPE_CHECKING, Any

from playwright.async_api import BrowserContext, Page

from skyvern.library.skyvern_browser_page import SkyvernBrowserPage

if TYPE_CHECKING:
    from skyvern.library.skyvern import Skyvern


class SkyvernBrowser(BrowserContext):
    """A browser context wrapper that creates Skyvern-enabled pages.

    This class extends Playwright BrowserContext and provides methods to create
    SkyvernBrowserPage instances that combine traditional browser automation with
    AI-powered task execution capabilities. It manages browser session state and
    enables persistent browser sessions across multiple pages.

    Example:
        ```python
            skyvern = Skyvern.local()
            browser = await skyvern.launch_local_browser()

            # Get or create the working page
            page = await browser.get_working_page()

            # Create a new page
            new_page = await browser.new_page()
        ```

    Attributes:
        _browser_context: The underlying Playwright BrowserContext.
        _browser_session_id: Optional session ID for persistent browser sessions.
        _browser_address: Optional address for remote browser connections.
    """

    def __init__(
        self,
        skyvern: "Skyvern",
        browser_context: BrowserContext,
        *,
        browser_session_id: str | None = None,
        browser_address: str | None = None,
    ):
        super().__init__(browser_context)
        self._skyvern = skyvern
        self._browser_context = browser_context
        self._browser_session_id = browser_session_id
        self._browser_address = browser_address

        self.workflow_run_id: None | str = None

    def __getattribute__(self, name: str) -> Any:
        browser_context = object.__getattribute__(self, "_browser_context")
        if hasattr(browser_context, name):
            for cls in type(self).__mro__:
                if cls is BrowserContext:
                    break
                if name in cls.__dict__:
                    return object.__getattribute__(self, name)
            return getattr(browser_context, name)

        return object.__getattribute__(self, name)

    @property
    def browser_session_id(self) -> str | None:
        return self._browser_session_id

    @property
    def browser_address(self) -> str | None:
        return self._browser_address

    @property
    def skyvern(self) -> "Skyvern":
        return self._skyvern

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

    async def close(self, **kwargs: Any) -> None:
        """Close the browser and optionally close the browser session.

        This method closes the browser context. If the browser is associated with a
        cloud browser session (has a browser_session_id), it will also close the
        browser session via the API, marking it as completed.

        Args:
            **kwargs: Arguments passed to the underlying BrowserContext.close() method.

        Example:
            ```python
            browser = await skyvern.launch_cloud_browser()
            # ... use the browser ...
            await browser.close()  # Closes both browser and cloud session
            ```
        """
        await self._browser_context.close(**kwargs)

        if self._browser_session_id:
            await self._skyvern.close_browser_session(self._browser_session_id)
