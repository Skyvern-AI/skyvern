import os
from typing import Callable

import httpx
from dotenv import load_dotenv
from playwright.async_api import Playwright, async_playwright
from typing_extensions import overload

from skyvern.client import AsyncSkyvern, BrowserSessionResponse, SkyvernEnvironment
from skyvern.library.constants import DEFAULT_CDP_PORT
from skyvern.library.skyvern_browser import SkyvernBrowser


class SkyvernSdk:
    """Main entry point for the Skyvern SDK.

    This class provides methods to launch and connect to browsers (both local and cloud-hosted),
    and access the Skyvern API client for task and workflow management. It combines browser
    automation capabilities with AI-powered task execution.

    Example:
        ```python

        # Initialize with remote environment and API key
        skyvern = SkyvernSdk(environment=SkyvernEnvironment.CLOUD, api_key="your-api-key")

        # Or in embedded mode (run `skyvern quickstart` first):
        skyvern = SkyvernSdk()

        # Launch a local browser
        browser = await skyvern.launch_local_browser(headless=False)
        page = await browser.get_working_page()

        # Or use a cloud browser (works only in cloud environment)
        browser = await skyvern.use_cloud_browser()
        page = await browser.get_working_page()

        # Execute AI-powered tasks
        await page.run.run_task("Fill out the form and submit it")
        ```

    You can also mix AI-powered tasks with direct browser control in the same session:
        ```python

        # Create credentials via API
        credential = await skyvern.api.create_credential(
            name="my_user",
            credential_type="password",
            credential=NonEmptyPasswordCredential(username="user@example.com", password="my_password"),
        )

        # Get a browser page
        browser = await skyvern.launch_cloud_browser()
        page = await browser.get_working_page()

        # Navigate manually
        await page.goto("https://example.com")

        # Use AI to handle login
        await page.run.login(
            credential_type=CredentialType.skyvern,
            credential_id=credential.credential_id,
        )

        # Continue with manual browser control
        await page.click("#invoices-button")
        await page.fill("#search", "my invoice")
        await page.screenshot(path="screenshot.png", full_page=True)
        ```
    """

    @overload
    def __init__(
        self,
        *,
        environment: SkyvernEnvironment,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = True,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Remote mode: Connect to Skyvern Cloud or self-hosted instance.

        Args:
            environment: The Skyvern environment to connect to. Use SkyvernEnvironment.CLOUD
                for Skyvern Cloud or SkyvernEnvironment.PRODUCTION/STAGING for self-hosted
                instances.
            api_key: API key for authenticating with Skyvern.
                Can be found on the settings page: https://app.skyvern.com/settings
            base_url: Override the base URL for the Skyvern API. If not provided, uses the default URL for
                the specified environment.
            timeout: Timeout in seconds for API requests. If not provided, uses the default timeout.
            follow_redirects: Whether to automatically follow HTTP redirects. Defaults to True.
            httpx_client: Custom httpx AsyncClient for making API requests.
                If not provided, a default client will be created.
        """
        ...

    @overload
    def __init__(
        self,
        *,
        open_api_key: str | None = None,
    ) -> None:
        """Embedded mode: Run Skyvern locally in-process.

        To use this mode, run `skyvern quickstart` first.

        Args:
            open_api_key: Optional OpenAI API key override for LLM operations.
                If not provided, the one from the .env file will be used.
        """
        ...

    def __init__(
        self,
        *,
        environment: SkyvernEnvironment | None = None,
        open_api_key: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = True,
        httpx_client: httpx.AsyncClient | None = None,
    ):
        if environment is None:
            if httpx_client is not None:
                raise ValueError("httpx_client is not supported in embedded mode")

            if not os.path.exists(".env"):
                raise ValueError("Please run `skyvern quickstart` to set up your local Skyvern environment")

            load_dotenv(".env")
            api_key = os.getenv("SKYVERN_API_KEY")
            if not api_key:
                raise ValueError("SKYVERN_API_KEY is not set. Provide api_key or set SKYVERN_API_KEY in .env file.")

            def create_embedded_api() -> AsyncSkyvern:
                from skyvern.library.embedded_server_factory import create_embedded_server  # noqa: PLC0415

                return create_embedded_server(
                    api_key=api_key,
                    open_api_key=open_api_key,
                )

            api_factory = create_embedded_api
        else:
            if not api_key:
                raise ValueError(f"Missing api_key for {environment.name}")

            def create_remote_api() -> AsyncSkyvern:
                return AsyncSkyvern(
                    environment=environment,
                    base_url=base_url,
                    api_key=api_key,
                    timeout=timeout,
                    follow_redirects=follow_redirects,
                    httpx_client=httpx_client,
                )

            api_factory = create_remote_api

        self._api_factory: Callable[[], AsyncSkyvern] = api_factory

        self._environment = environment
        self._api_key = api_key

        self._api: AsyncSkyvern | None = None
        self._playwright: Playwright | None = None

    @property
    def api(self) -> AsyncSkyvern:
        """Get the AsyncSkyvern API client for direct API access."""
        if not self._api:
            self._api = self._api_factory()
        return self._api

    async def launch_local_browser(self, *, headless: bool = False, port: int = DEFAULT_CDP_PORT) -> SkyvernBrowser:
        """Launch a new local Chromium browser with Chrome DevTools Protocol (CDP) enabled.

        This method launches a browser on your local machine with remote debugging enabled,
        allowing Skyvern to control it via CDP. Useful for development and debugging.

        Args:
            headless: Whether to run the browser in headless mode. Defaults to False.
            port: The port number for the CDP endpoint. Defaults to DEFAULT_CDP_PORT.

        Returns:
            SkyvernBrowser: A browser instance with Skyvern capabilities.
        """
        playwright = await self._get_playwright()
        browser = await playwright.chromium.launch(
            headless=headless,
            args=[f"--remote-debugging-port={port}"],
        )
        browser_address = f"http://localhost:{port}"
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(self, browser_context, browser_address=browser_address)

    async def connect_to_browser_over_cdp(self, cdp_url: str) -> SkyvernBrowser:
        """Connect to an existing browser instance via Chrome DevTools Protocol (CDP).

        Use this to connect to a browser that's already running with CDP enabled,
        whether local or remote.

        Args:
            cdp_url: The CDP WebSocket URL (e.g., "http://localhost:9222").

        Returns:
            SkyvernBrowser: A browser instance connected to the existing browser.
        """
        playwright = await self._get_playwright()
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(self, browser_context, browser_address=cdp_url)

    async def connect_to_cloud_browser_session(self, browser_session_id: str) -> SkyvernBrowser:
        """Connect to an existing cloud-hosted browser session by ID.

        Args:
            browser_session_id: The ID of the cloud browser session to connect to.

        Returns:
            SkyvernBrowser: A browser instance connected to the cloud session.
        """
        if self._environment != SkyvernEnvironment.CLOUD and self._environment != SkyvernEnvironment.STAGING:
            raise Exception("Cloud browser sessions are supported only in the cloud environment")

        browser_session = await self.api.get_browser_session(browser_session_id)
        return await self._connect_to_cloud_browser_session(browser_session)

    async def launch_cloud_browser(self) -> SkyvernBrowser:
        """Launch a new cloud-hosted browser session.

        This creates a new browser session in Skyvern's cloud infrastructure and connects to it.

        Returns:
            SkyvernBrowser: A browser instance connected to the new cloud session.
        """
        if self._environment != SkyvernEnvironment.CLOUD and self._environment != SkyvernEnvironment.STAGING:
            raise Exception("Cloud browser sessions are supported only in the cloud environment")

        browser_session = await self.api.create_browser_session()
        return await self._connect_to_cloud_browser_session(browser_session)

    async def use_cloud_browser(self) -> SkyvernBrowser:
        """Get or create a cloud browser session.

        This method attempts to reuse the most recent available cloud browser session.
        If no session exists, it creates a new one. This is useful for cost efficiency
        and session persistence.

        Returns:
            SkyvernBrowser: A browser instance connected to an existing or new cloud session.
        """
        if self._environment != SkyvernEnvironment.CLOUD and self._environment != SkyvernEnvironment.STAGING:
            raise Exception("Cloud browser sessions are supported only in the cloud environment")

        browser_sessions = await self.api.get_browser_sessions()
        browser_session = max(
            (s for s in browser_sessions if s.runnable_id is None), key=lambda s: s.started_at, default=None
        )
        if browser_session is None:
            browser_session = await self.api.create_browser_session()
        return await self._connect_to_cloud_browser_session(browser_session)

    async def _connect_to_cloud_browser_session(self, browser_session: BrowserSessionResponse) -> SkyvernBrowser:
        if browser_session.browser_address is None:
            raise Exception(f"Browser address is missing for session {browser_session.browser_session_id}")

        playwright = await self._get_playwright()
        browser = await playwright.chromium.connect_over_cdp(
            browser_session.browser_address, headers={"x-api-key": self._api_key}
        )
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(self, browser_context, browser_session_id=browser_session.browser_session_id)

    async def _get_playwright(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def aclose(self) -> None:
        """Close Playwright and release resources."""
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
