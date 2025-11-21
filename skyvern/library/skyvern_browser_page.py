import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from playwright.async_api import Page

from skyvern.client import GetRunResponse
from skyvern.client.types.workflow_run_response import WorkflowRunResponse
from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT
from skyvern.library.skyvern_browser_page_ai import SdkSkyvernPageAi

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser

from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import RunEngine, RunStatus, TaskRunResponse

LOG = structlog.get_logger()


class SkyvernPageRun:
    """Provides methods to run Skyvern tasks and workflows in the context of a browser page.

    This class enables executing AI-powered browser automation tasks while sharing the
    context of an existing browser page. It supports running custom tasks, login workflows,
    and pre-defined workflows with automatic waiting for completion.
    """

    def __init__(self, browser: "SkyvernBrowser", page: Page) -> None:
        self._browser = browser
        self._page = page

    async def task(
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
        max_steps: int | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
        user_agent: str | None = None,
    ) -> TaskRunResponse:
        """Run a task in the context of this page and wait for it to finish.

        Args:
            prompt: Natural language description of the task to perform.
            engine: The execution engine to use. Defaults to skyvern_v2.
            model: LLM model configuration options.
            url: URL to navigate to. If not provided, uses the current page URL.
            webhook_url: URL to receive webhook notifications about task progress.
            totp_identifier: Identifier for TOTP (Time-based One-Time Password) authentication.
            totp_url: URL to fetch TOTP codes from.
            title: Human-readable title for this task run.
            error_code_mapping: Mapping of error codes to custom error messages.
            data_extraction_schema: Schema defining what data to extract from the page.
            max_steps: Maximum number of steps the agent can take.
            timeout: Maximum time in seconds to wait for task completion.
            user_agent: Custom user agent string to use.

        Returns:
            TaskRunResponse containing the task execution results.
        """

        LOG.info("AI run task", prompt=prompt)

        task_run = await self._browser.skyvern.run_task(
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
            max_steps=max_steps,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            user_agent=user_agent,
        )

        task_run = await self._wait_for_run_completion(task_run.run_id, timeout)
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
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        """Run a login task in the context of this page and wait for it to finish.

        Args:
            credential_type: Type of credential store to use (e.g., bitwarden, onepassword).
            url: URL to navigate to for login. If not provided, uses the current page URL.
            credential_id: ID of the credential to use.
            bitwarden_collection_id: Bitwarden collection ID containing the credentials.
            bitwarden_item_id: Bitwarden item ID for the credentials.
            onepassword_vault_id: 1Password vault ID containing the credentials.
            onepassword_item_id: 1Password item ID for the credentials.
            prompt: Additional instructions for the login process.
            webhook_url: URL to receive webhook notifications about login progress.
            totp_identifier: Identifier for TOTP authentication.
            totp_url: URL to fetch TOTP codes from.
            extra_http_headers: Additional HTTP headers to include in requests.
            timeout: Maximum time in seconds to wait for login completion.

        Returns:
            WorkflowRunResponse containing the login workflow execution results.
        """

        LOG.info("AI login", prompt=prompt)

        workflow_run = await self._browser.skyvern.login(
            credential_type=credential_type,
            url=url or self._get_page_url(),
            credential_id=credential_id,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            onepassword_vault_id=onepassword_vault_id,
            onepassword_item_id=onepassword_item_id,
            prompt=prompt,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            extra_http_headers=extra_http_headers,
        )

        workflow_run = await self._wait_for_run_completion(workflow_run.run_id, timeout)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def workflow(
        self,
        workflow_id: str,
        parameters: dict[str, Any] | None = None,
        template: bool | None = None,
        title: str | None = None,
        webhook_url: str | None = None,
        totp_url: str | None = None,
        totp_identifier: str | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        """Run a workflow in the context of this page and wait for it to finish.

        Args:
            workflow_id: ID of the workflow to execute.
            parameters: Dictionary of parameters to pass to the workflow.
            template: Whether this is a workflow template.
            title: Human-readable title for this workflow run.
            webhook_url: URL to receive webhook notifications about workflow progress.
            totp_url: URL to fetch TOTP codes from.
            totp_identifier: Identifier for TOTP authentication.
            timeout: Maximum time in seconds to wait for workflow completion.

        Returns:
            WorkflowRunResponse containing the workflow execution results.
        """

        LOG.info("AI run workflow", workflow_id=workflow_id)

        workflow_run = await self._browser.skyvern.run_workflow(
            workflow_id=workflow_id,
            parameters=parameters,
            template=template,
            title=title,
            webhook_url=webhook_url,
            totp_url=totp_url,
            totp_identifier=totp_identifier,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
        )

        workflow_run = await self._wait_for_run_completion(workflow_run.run_id, timeout)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def _wait_for_run_completion(self, run_id: str, timeout: float) -> GetRunResponse:
        async with asyncio.timeout(timeout):
            while True:
                task_run = await self._browser.skyvern.get_run(run_id)
                if RunStatus(task_run.status).is_final():
                    break
                await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return task_run

    def _get_page_url(self) -> str | None:
        url = self._page.url
        if url == "about:blank":
            return None
        return url


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
        await page.run.run_task("Fill out the contact form and submit it")
        ```

    Attributes:
        run: SkyvernPageRun instance for executing AI-powered tasks and workflows.
    """

    def __init__(self, browser: "SkyvernBrowser", page: Page):
        super().__init__(page, SdkSkyvernPageAi(browser, page))
        self._browser = browser
        self.run = SkyvernPageRun(browser, page)

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
