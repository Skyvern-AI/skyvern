import asyncio
import os
import pathlib
import tempfile
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv
from playwright.async_api import Playwright, async_playwright

from skyvern.client import AsyncSkyvern, BrowserSessionResponse, SkyvernEnvironment
from skyvern.client.core import RequestOptions
from skyvern.client.types.task_run_response import TaskRunResponse
from skyvern.client.types.workflow_run_response import WorkflowRunResponse
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT, DEFAULT_CDP_PORT
from skyvern.library.skyvern_browser import SkyvernBrowser
from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import ProxyLocation, RunEngine, RunStatus

LOG = structlog.get_logger()


def _get_browser_session_url(browser_session_id: str) -> str:
    return f"https://app.skyvern.com/browser-session/{browser_session_id}"


class Skyvern(AsyncSkyvern):
    """Main entry point for the Skyvern SDK.

    This class provides methods to launch and connect to browsers (both local and cloud-hosted),
    and access the Skyvern API client for task and workflow management. It combines browser
    automation capabilities with AI-powered task execution.

    Example:
        ```python

        # Remote mode: Connect to Skyvern Cloud (API key required)
        skyvern = Skyvern(api_key="your-api-key")

        # Local/embedded mode (run `skyvern quickstart` first):
        skyvern = Skyvern.local()

        # Launch a local browser (works only in local environment)
        browser = await skyvern.launch_local_browser(headless=False)
        page = await browser.get_working_page()

        # Or use a cloud browser (works only in cloud environment)
        browser = await skyvern.use_cloud_browser()
        page = await browser.get_working_page()

        # Execute AI-powered tasks
        await page.agent.run_task("Fill out the form and submit it")
        ```

    You can also mix AI-powered tasks with direct browser control in the same session:
        ```python

        # Create credentials via API
        credential = await skyvern.create_credential(
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
        await page.agent.login(
            credential_type=CredentialType.skyvern,
            credential_id=credential.credential_id,
        )

        # Continue with manual browser control
        await page.click("#invoices-button")
        await page.fill("#search", "my invoice")
        await page.screenshot(path="screenshot.png", full_page=True)
        ```
    """

    def __init__(
        self,
        *,
        api_key: str,
        environment: SkyvernEnvironment = SkyvernEnvironment.CLOUD,
        base_url: str | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = True,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Remote mode: Connect to Skyvern Cloud or self-hosted instance.

        Args:
            api_key: API key for authenticating with Skyvern.
                Can be found on the settings page: https://app.skyvern.com/settings
            environment: The Skyvern environment to connect to. Use SkyvernEnvironment.CLOUD
                for Skyvern Cloud or SkyvernEnvironment.PRODUCTION/STAGING for self-hosted
                instances. Defaults to SkyvernEnvironment.CLOUD.
            base_url: Override the base URL for the Skyvern API. If not provided, uses the default URL for
                the specified environment.
            timeout: Timeout in seconds for API requests. If not provided, uses the default timeout.
            follow_redirects: Whether to automatically follow HTTP redirects. Defaults to True.
            httpx_client: Custom httpx AsyncClient for making API requests.
                If not provided, a default client will be created.
        """
        super().__init__(
            base_url=base_url,
            environment=environment,
            api_key=api_key,
            timeout=timeout,
            follow_redirects=follow_redirects,
            httpx_client=httpx_client,
        )

        self._environment = environment
        self._api_key: str | None = api_key
        self._playwright: Playwright | None = None

    @classmethod
    def local(
        cls,
        *,
        llm_config: LLMRouterConfig | LLMConfig | None = None,
        settings: dict[str, Any] | None = None,
    ) -> "Skyvern":
        """Local/embedded mode: Run Skyvern locally in-process.

        Prerequisites:
            Run `skyvern quickstart` first to set up your local environment and create a .env file

        Args:
            llm_config: Optional custom LLM configuration (LLMConfig or LLMRouterConfig).
                If provided, this will be registered as "CUSTOM_LLM" and used as the primary LLM,
                overriding the LLM_KEY setting from your .env file.
                If not provided, uses the LLM configured via LLM_KEY in your .env file.

                Example 1 - Using .env configuration (simplest, recommended):
                    ```python
                    from skyvern import Skyvern

                    # Uses LLM_KEY and other settings from your .env file
                    # Created by running `skyvern quickstart`
                    skyvern = Skyvern.local()
                    ```

                Example 2 - Custom LLM with environment variables:
                    ```python
                    from skyvern import Skyvern
                    from skyvern.forge.sdk.api.llm.models import LLMConfig

                    # Assumes OPENAI_API_KEY is set in your environment
                    skyvern = Skyvern.local(
                        llm_config=LLMConfig(
                            model_name="gpt-4o",
                            required_env_vars=["OPENAI_API_KEY"],
                            supports_vision=True,
                            add_assistant_prefix=False,
                        )
                    )
                    ```

                Example 3 - Explicitly providing credentials:
                    ```python
                    from skyvern import Skyvern
                    from skyvern.forge.sdk.api.llm.models import LLMConfig, LiteLLMParams

                    skyvern = Skyvern.local(
                        llm_config=LLMConfig(
                            model_name="gpt-4o",
                            required_env_vars=[],  # No env vars required
                            supports_vision=True,
                            add_assistant_prefix=False,
                            litellm_params=LiteLLMParams(
                                api_base="https://api.openai.com/v1",
                                api_key="sk-...",  # Your API key
                            ),
                        )
                    )
                    ```
            settings: Optional dictionary of Skyvern settings to override.
                These override the corresponding settings from your .env file.
                Example: {"MAX_STEPS_PER_RUN": 100, "BROWSER_TYPE": "chromium-headful"}

        Returns:
            Skyvern: A Skyvern instance running in local/embedded mode.
        """
        from skyvern.library.embedded_server_factory import create_embedded_server  # noqa: PLC0415

        if not os.path.exists(".env"):
            raise ValueError("Please run `skyvern quickstart` to set up your local Skyvern environment")

        load_dotenv(".env")
        api_key = os.getenv("SKYVERN_API_KEY")
        if not api_key:
            raise ValueError("SKYVERN_API_KEY is not set. Provide api_key or set SKYVERN_API_KEY in .env file.")

        obj = cls.__new__(cls)

        AsyncSkyvern.__init__(
            obj,
            environment=SkyvernEnvironment.LOCAL,
            httpx_client=create_embedded_server(
                llm_config=llm_config,
                settings_overrides=settings,
            ),
        )

        obj._environment = SkyvernEnvironment.LOCAL
        obj._api_key = None
        obj._playwright = None

        return obj

    @property
    def environment(self) -> SkyvernEnvironment | None:
        """Get the current Skyvern environment (CLOUD, STAGING, LOCAL, or None for embedded mode)."""
        return self._environment

    async def run_task(
        self,
        prompt: str,
        engine: RunEngine = RunEngine.skyvern_v2,
        model: dict[str, Any] | None = None,
        url: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        title: str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        data_extraction_schema: dict[str, Any] | str | None = None,
        proxy_location: ProxyLocation | None = None,
        max_steps: int | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
        browser_session_id: str | None = None,
        user_agent: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        publish_workflow: bool = False,
        include_action_history_in_verification: bool | None = None,
        max_screenshot_scrolls: int | None = None,
        browser_address: str | None = None,
        request_options: RequestOptions | None = None,
    ) -> TaskRunResponse:
        task_run = await super().run_task(
            prompt=prompt,
            engine=engine,
            model=model,
            url=url,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            title=title,
            error_code_mapping=error_code_mapping,
            data_extraction_schema=data_extraction_schema,
            proxy_location=proxy_location,
            max_steps=max_steps,
            browser_session_id=browser_session_id,
            user_agent=user_agent,
            extra_http_headers=extra_http_headers,
            publish_workflow=publish_workflow,
            include_action_history_in_verification=include_action_history_in_verification,
            max_screenshot_scrolls=max_screenshot_scrolls,
            browser_address=browser_address,
            request_options=request_options,
        )

        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    task_run = await super().get_run(task_run.run_id)
                    if RunStatus(task_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return TaskRunResponse.model_validate(task_run.model_dump())

    async def run_workflow(
        self,
        workflow_id: str,
        parameters: dict[str, Any] | None = None,
        template: bool | None = None,
        title: str | None = None,
        proxy_location: ProxyLocation | None = None,
        webhook_url: str | None = None,
        totp_url: str | None = None,
        totp_identifier: str | None = None,
        browser_session_id: str | None = None,
        max_steps_override: int | None = None,
        user_agent: str | None = None,
        browser_profile_id: str | None = None,
        max_screenshot_scrolls: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        ai_fallback: bool | None = None,
        run_with: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
        request_options: RequestOptions | None = None,
    ) -> WorkflowRunResponse:
        workflow_run = await super().run_workflow(
            workflow_id=workflow_id,
            parameters=parameters,
            template=template,
            title=title,
            proxy_location=proxy_location,
            webhook_url=webhook_url,
            totp_url=totp_url,
            totp_identifier=totp_identifier,
            browser_session_id=browser_session_id,
            max_steps_override=max_steps_override,
            user_agent=user_agent,
            browser_profile_id=browser_profile_id,
            max_screenshot_scrolls=max_screenshot_scrolls,
            extra_http_headers=extra_http_headers,
            browser_address=browser_address,
            ai_fallback=ai_fallback,
            run_with=run_with,
            request_options=request_options,
        )
        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    workflow_run = await super().get_run(workflow_run.run_id)
                    if RunStatus(workflow_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def login(
        self,
        credential_type: CredentialType,
        *,
        url: str | None = None,
        credential_id: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
        onepassword_vault_id: str | None = None,
        onepassword_item_id: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        browser_session_id: str | None = None,
        browser_address: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        max_screenshot_scrolling_times: int | None = None,
        azure_vault_name: str | None = None,
        azure_vault_username_key: str | None = None,
        azure_vault_password_key: str | None = None,
        azure_vault_totp_secret_key: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
        request_options: RequestOptions | None = None,
    ) -> WorkflowRunResponse:
        workflow_run = await super().login(
            credential_type=credential_type,
            url=url,
            credential_id=credential_id,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            onepassword_vault_id=onepassword_vault_id,
            onepassword_item_id=onepassword_item_id,
            prompt=prompt,
            webhook_url=webhook_url,
            proxy_location=proxy_location,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=browser_session_id,
            browser_address=browser_address,
            extra_http_headers=extra_http_headers,
            max_screenshot_scrolling_times=max_screenshot_scrolling_times,
            azure_vault_name=azure_vault_name,
            azure_vault_username_key=azure_vault_username_key,
            azure_vault_password_key=azure_vault_password_key,
            azure_vault_totp_secret_key=azure_vault_totp_secret_key,
            request_options=request_options,
        )
        if wait_for_completion:
            async with asyncio.timeout(timeout):
                while True:
                    workflow_run = await super().get_run(workflow_run.run_id)
                    if RunStatus(workflow_run.status).is_final():
                        break
                    await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def launch_local_browser(
        self,
        *,
        headless: bool = False,
        port: int = DEFAULT_CDP_PORT,
        args: list[str] | None = None,
        user_data_dir: str | None = None,
    ) -> SkyvernBrowser:
        """Launch a new local Chromium browser with Chrome DevTools Protocol (CDP) enabled.

        This method launches a browser on your local machine with remote debugging enabled,
        allowing Skyvern to control it via CDP. Useful for development and debugging.

        Args:
            headless: Whether to run the browser in headless mode. Defaults to False.
            port: The port number for the CDP endpoint. Defaults to DEFAULT_CDP_PORT.
            args: Additional command-line arguments to pass to Chromium. Defaults to None.
                Example: ["--disable-blink-features=AutomationControlled", "--window-size=1920,1080"]

        Returns:
            SkyvernBrowser: A browser instance with Skyvern capabilities.
        """

        playwright = await self._get_playwright()

        if user_data_dir:
            user_data_path = pathlib.Path(user_data_dir)
        else:
            user_data_path = pathlib.Path(tempfile.gettempdir()) / "skyvern-browser"

        launch_args = [
            f"--remote-debugging-port={port}",
        ]
        if args:
            launch_args.extend(args)

        browser_context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_path),
            headless=headless,
            args=launch_args,
        )
        browser_address = f"http://localhost:{port}"
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
        self._ensure_cloud_environment()
        browser_session = await self.get_browser_session(browser_session_id)
        if self._environment == SkyvernEnvironment.CLOUD:
            LOG.info(
                "Connecting to existing cloud browser session",
                url=_get_browser_session_url(browser_session.browser_session_id),
            )
        else:
            LOG.info(
                "Connecting to existing cloud browser session", browser_session_id=browser_session.browser_session_id
            )
        return await self._connect_to_cloud_browser_session(browser_session)

    async def launch_cloud_browser(
        self,
        *,
        timeout: int | None = None,
        proxy_location: ProxyLocation | None = None,
    ) -> SkyvernBrowser:
        """Launch a new cloud-hosted browser session.

        This creates a new browser session in Skyvern's cloud infrastructure and connects to it.

        Args:
            timeout: Timeout in minutes for the session. Timeout is applied after the session is started.
                Must be between 5 and 1440. Defaults to 60.
            proxy_location: Geographic proxy location to route the browser traffic through.
                This is only available in Skyvern Cloud.

        Returns:
            SkyvernBrowser: A browser instance connected to the new cloud session.
        """
        self._ensure_cloud_environment()
        browser_session = await self.create_browser_session(
            timeout=timeout,
            proxy_location=proxy_location,
        )
        if self._environment == SkyvernEnvironment.CLOUD:
            LOG.info(
                "Launched new cloud browser session",
                url=_get_browser_session_url(browser_session.browser_session_id),
            )
        else:
            LOG.info("Launched new cloud browser session", browser_session_id=browser_session.browser_session_id)
        return await self._connect_to_cloud_browser_session(browser_session)

    async def use_cloud_browser(
        self,
        *,
        timeout: int | None = None,
        proxy_location: ProxyLocation | None = None,
    ) -> SkyvernBrowser:
        """Get or create a cloud browser session.

        This method attempts to reuse the most recent available cloud browser session.
        If no session exists, it creates a new one. This is useful for cost efficiency
        and session persistence.

        Args:
            timeout: Timeout in minutes for the session. Timeout is applied after the session is started.
                Must be between 5 and 1440. Defaults to 60. Only used when creating a new session.
            proxy_location: Geographic proxy location to route the browser traffic through.
                This is only available in Skyvern Cloud. Only used when creating a new session.

        Returns:
            SkyvernBrowser: A browser instance connected to an existing or new cloud session.
        """
        self._ensure_cloud_environment()
        browser_sessions = await self.get_browser_sessions()
        browser_session = max(
            (s for s in browser_sessions if s.runnable_id is None), key=lambda s: s.started_at, default=None
        )
        if browser_session is None:
            LOG.info("No existing cloud browser session found, launching a new session")
            browser_session = await self.create_browser_session(
                timeout=timeout,
                proxy_location=proxy_location,
            )
            if self._environment == SkyvernEnvironment.CLOUD:
                LOG.info(
                    "Launched new cloud browser session",
                    url=_get_browser_session_url(browser_session.browser_session_id),
                )
            else:
                LOG.info("Launched new cloud browser session", browser_session_id=browser_session.browser_session_id)
        else:
            if self._environment == SkyvernEnvironment.CLOUD:
                LOG.info(
                    "Reusing existing cloud browser session",
                    url=_get_browser_session_url(browser_session.browser_session_id),
                )
            else:
                LOG.info(
                    "Reusing existing cloud browser session", browser_session_id=browser_session.browser_session_id
                )

        return await self._connect_to_cloud_browser_session(browser_session)

    def _ensure_cloud_environment(self) -> None:
        if self._environment not in (SkyvernEnvironment.CLOUD, SkyvernEnvironment.STAGING):
            raise ValueError("Cloud browser sessions are supported only in the cloud environment")

    async def _connect_to_cloud_browser_session(self, browser_session: BrowserSessionResponse) -> SkyvernBrowser:
        if browser_session.browser_address is None:
            raise ValueError(f"Browser address is missing for session {browser_session.browser_session_id}")

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
