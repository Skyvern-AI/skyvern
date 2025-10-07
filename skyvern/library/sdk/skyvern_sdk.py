import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright, Playwright, BrowserContext

from skyvern.client import AsyncSkyvern, SkyvernEnvironment, BrowserSessionResponse
from skyvern.library.constants import DEFAULT_AGENT_TIMEOUT, DEFAULT_AGENT_HEARTBEAT_INTERVAL, LOCAL_BASE_URL, \
    DEFAULT_CDP_PORT
from skyvern.schemas.runs import RunEngine, ProxyLocation, TaskRunResponse, RunStatus
from skyvern.schemas.run_blocks import CredentialType
from skyvern.client.types.workflow_run_response import WorkflowRunResponse


class SkyvernPageAi:
    def __init__(self, page: Page,
                 browser_session_id: str | None,
                 browser_address: str | None,
                 client: AsyncSkyvern) -> None:
        self._page = page
        self._browser_session_id = browser_session_id
        self._browser_address = browser_address
        self._client = client

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
            timeout: float = DEFAULT_AGENT_TIMEOUT,
            user_agent: str | None = None,
    ) -> TaskRunResponse:
        task_run = await self._client.run_task(
            prompt=prompt,
            engine=engine,
            model=model,
            url=url or self._get_page_url(),
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            title=title,
            error_code_mapping=error_code_mapping,
            data_extraction_schema=data_extraction_schema,
            proxy_location=proxy_location,
            max_steps=max_steps,
            browser_session_id=self._browser_session_id,
            browser_address=self._browser_address,
            user_agent=user_agent,
        )

        await self._wait_for_run_completion(task_run.run_id, timeout)
        return TaskRunResponse.model_validate(task_run.model_dump())

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
            extra_http_headers: dict[str, str] | None = None,
            timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        workflow_run = await self._client.login(
            credential_type=credential_type,
            url=url or self._get_page_url(),
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
            browser_session_id=self._browser_session_id,
            browser_address=self._browser_address,
            extra_http_headers=extra_http_headers,
        )

        await self._wait_for_run_completion(workflow_run.run_id, timeout)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

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
            timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        workflow_run = await self._client.run_workflow(
            workflow_id=workflow_id,
            parameters=parameters,
            template=template,
            title=title,
            proxy_location=proxy_location,
            webhook_url=webhook_url,
            totp_url=totp_url,
            totp_identifier=totp_identifier,
            browser_session_id=self._browser_session_id,
            browser_address=self._browser_address,
        )

        await self._wait_for_run_completion(workflow_run.run_id, timeout)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def _wait_for_run_completion(self, run_id: str, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while True:
                task_run = await self._client.get_run(run_id)
                if RunStatus(task_run.status).is_final():
                    break
                await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)

    def _get_page_url(self) -> str | None:
        url = self._page.url
        if url == "about:blank":
            return None
        return url


class SkyvernBrowserPage(Page):
    def __init__(self, page: Page, ai: SkyvernPageAi):
        super().__init__(page)
        self.ai = ai
        self._page = page

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_") and hasattr(self._page, name):
            return getattr(self._page, name)
        return super().__getattribute__(name)


class SkyvernBrowser:
    def __init__(self, browser_context: BrowserContext,
                 client: AsyncSkyvern,
                 *,
                 browser_session_id: str | None = None,
                 browser_address: str | None = None):
        self._browser_context = browser_context
        self._browser_session_id = browser_session_id
        self._browser_address = browser_address
        self._client = client

    async def get_working_page(self) -> SkyvernBrowserPage:
        if self._browser_context.pages:
            page = self._browser_context.pages[-1]
        else:
            page = await self._browser_context.new_page()
        return await self._create_skyvern_page(page)

    async def new_page(self) -> SkyvernBrowserPage:
        page = await self._browser_context.new_page()
        return await self._create_skyvern_page(page)

    async def _create_skyvern_page(self, page: Page) -> SkyvernBrowserPage:
        page_ai = SkyvernPageAi(page, self._browser_session_id, self._browser_address, self._client)
        return SkyvernBrowserPage(page, page_ai)


class SkyvernSdk:
    def __init__(self,
                 *,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 environment: SkyvernEnvironment = SkyvernEnvironment.PRODUCTION,
                 timeout: float | None = None,
                 follow_redirects: bool | None = True,
                 httpx_client: httpx.AsyncClient | None = None, ):

        if base_url is None or api_key is None:
            if not os.path.exists(".env"):
                raise Exception("No .env file found. Please run 'skyvern init' first to set up your environment.")

            load_dotenv(".env")

        self._base_url = base_url or LOCAL_BASE_URL
        self._api_key = api_key or os.environ["SKYVERN_API_KEY"]
        self._client = AsyncSkyvern(
            base_url=self._base_url,
            api_key=self._api_key,
            x_api_key=self._api_key,
            environment=environment,
            timeout=timeout,
            follow_redirects=follow_redirects,
            httpx_client=httpx_client,
        )

        self._playwright: Playwright | None = None

    @property
    def client(self) -> AsyncSkyvern:
        return self._client

    async def launch_local_browser(self, *, headless: bool = False, port: int = DEFAULT_CDP_PORT) -> SkyvernBrowser:
        playwright = await self._get_playwright()
        browser = await playwright.chromium.launch(
            headless=headless,
            args=[f"--remote-debugging-port={port}"],
        )
        browser_address = f"http://localhost:{port}"
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(browser_context, self._client, browser_address=browser_address)

    async def connect_to_browser_over_cdp(self, cdp_url: str) -> SkyvernBrowser:
        playwright = await self._get_playwright()
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(browser_context, self._client, browser_address=cdp_url)

    async def connect_to_cloud_browser_session(self, browser_session_id: str) -> SkyvernBrowser:
        browser_session = await self._client.get_browser_session(browser_session_id)
        return await self._connect_to_cloud_browser_session(browser_session)

    async def launch_cloud_browser(self) -> SkyvernBrowser:
        browser_session = await self._client.create_browser_session()
        return await self._connect_to_cloud_browser_session(browser_session)

    async def use_cloud_browser(self) -> SkyvernBrowser:
        browser_sessions = await self._client.get_browser_sessions()
        browser_session = max(
            (s for s in browser_sessions if s.runnable_id is None),
            key=lambda s: s.started_at,
            default=None
        )
        if browser_session is None:
            browser_session = await self._client.create_browser_session()
        return await self._connect_to_cloud_browser_session(browser_session)

    async def _connect_to_cloud_browser_session(self, browser_session: BrowserSessionResponse) -> SkyvernBrowser:
        if browser_session.browser_address is None:
            raise Exception(f"Browser session id is missing for {browser_session.browser_session_id}")

        playwright = await self._get_playwright()
        browser = await playwright.chromium.connect_over_cdp(
            browser_session.browser_address,
            headers={"x-api-key": self._api_key}
        )
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(browser_context, self._client, browser_session_id=browser_session.browser_session_id)

    async def _get_playwright(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright
