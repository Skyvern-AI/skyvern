import asyncio
from typing import TYPE_CHECKING, Any, overload

from playwright.async_api import Page

from skyvern.client import GetRunResponse
from skyvern.client.types.workflow_run_response import WorkflowRunResponse
from skyvern.config import settings
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT
from skyvern.library.SdkSkyvernPageAi import SdkSkyvernPageAi
from skyvern.webeye.actions import handler_utils

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser

from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import RunEngine, RunStatus, TaskRunResponse


class SkyvernPageRun:
    """Provides methods to run Skyvern tasks and workflows in the context of a browser page.

    This class enables executing AI-powered browser automation tasks while sharing the
    context of an existing browser page. It supports running custom tasks, login workflows,
    and pre-defined workflows with automatic waiting for completion.
    """

    def __init__(self, browser: "SkyvernBrowser", page: Page) -> None:
        self._browser = browser
        self._page = page

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

        task_run = await self._browser.client.run_task(
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

        workflow_run = await self._browser.client.login(
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

    async def run_workflow(
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
        workflow_run = await self._browser.client.run_workflow(
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
                task_run = await self._browser.client.get_run(run_id)
                if RunStatus(task_run.status).is_final():
                    break
                await asyncio.sleep(DEFAULT_AGENT_HEARTBEAT_INTERVAL)
        return task_run

    def _get_page_url(self) -> str | None:
        url = self._page.url
        if url == "about:blank":
            return None
        return url


class SkyvernBrowserPage:
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
        self._browser = browser
        self._page = page
        self._ai = SdkSkyvernPageAi(browser, page)
        self.run = SkyvernPageRun(browser, page)

    @overload
    async def click(
        self,
        selector: str,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None: ...

    @overload
    async def click(
        self,
        *,
        prompt: str,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None: ...

    async def click(
        self,
        selector: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None:
        """Click an element using a CSS selector, AI-powered prompt matching, or both.

        This method supports three modes:
        - **Selector-based**: Click the element matching the CSS selector
        - **AI-powered**: Use natural language to describe which element to click
        - **Fallback mode** (default): Try the selector first, fall back to AI if it fails

        Args:
            selector: CSS selector for the target element.
            prompt: Natural language description of which element to click.
            ai: AI behavior mode. Defaults to "fallback" which tries selector first, then AI.
            data: Additional context data for AI processing.
            timeout: Maximum time to wait for the click action in milliseconds.

        Returns:
            The selector string that was successfully used to click the element, or None.

        Examples:
            ```python
            # Click using a CSS selector
            await page.click("#open-invoice-button")

            # Click using AI with natural language
            await page.click(prompt="Click on the 'Open Invoice' button")

            # Try selector first, fall back to AI if selector fails
            await page.click("#open-invoice-button", prompt="Click on the 'Open Invoice' button")
            ```
        """

        if ai == "fallback":
            # try to click the element with the original selector first
            error_to_raise = None
            if selector:
                try:
                    locator = self._page.locator(selector)
                    await locator.click(timeout=timeout)
                    return selector
                except Exception as e:
                    error_to_raise = e

            # if the original selector doesn't work, try to click the element with the ai generated selector
            if prompt:
                return await self._ai.ai_click(
                    selector=selector or "",
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )
            if error_to_raise:
                raise error_to_raise
            else:
                return selector
        elif ai == "proactive":
            if prompt:
                return await self._ai.ai_click(
                    selector=selector or "",
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )

        if selector:
            locator = self._page.locator(selector)
            await locator.click(timeout=timeout)
        return selector

    @overload
    async def fill(
        self,
        selector: str,
        *,
        value: str,
        prompt: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str: ...

    @overload
    async def fill(
        self,
        *,
        prompt: str,
        value: str,
        selector: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str: ...

    async def fill(
        self,
        selector: str | None = None,
        *,
        value: str,
        prompt: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        """Fill an input field using a CSS selector, AI-powered prompt matching, or both.

        This method supports three modes:
        - **Selector-based**: Fill the input field matching the CSS selector
        - **AI-powered**: Use natural language to describe which field to fill
        - **Fallback mode** (default): Try the selector first, fall back to AI if it fails

        Args:
            selector: CSS selector for the target input element.
            prompt: Natural language description of which field to fill.
            value: The text value to input into the field.
            ai: AI behavior mode. Defaults to "fallback" which tries selector first, then AI.
            data: Additional context data for AI processing.
            timeout: Maximum time to wait for the fill action in milliseconds.
            totp_identifier: TOTP identifier for time-based one-time password fields.
            totp_url: URL to fetch TOTP codes from for authentication.

        Returns:
            The value that was successfully filled into the field.

        Examples:
            ```python
            # Fill using a CSS selector
            await page.fill("#email-input", value="user@example.com")

            # Fill using AI with natural language
            await page.fill(prompt="Fill in the email address", value="user@example.com")

            # Try selector first, fall back to AI if selector fails
            await page.fill(
                "#email-input",
                value="user@example.com",
                prompt="Fill in the email address"
            )
            ```
        """
        return await self._input_text(
            selector=selector or "",
            value=value,
            ai=ai,
            intention=prompt,
            data=data,
            timeout=timeout,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
        )

    async def goto(self, url: str, **kwargs: Any) -> None:
        """Navigate to the given URL.

        Args:
            url: URL to navigate page to.
            **kwargs: Additional options like timeout, wait_until, referer, etc.
        """
        await self._page.goto(url, **kwargs)

    async def type(self, selector: str, text: str, **kwargs: Any) -> None:
        """Type text into an element character by character.

        Args:
            selector: A selector to search for an element to type into.
            text: Text to type into the element.
            **kwargs: Additional options like delay, timeout, no_wait_after, etc.
        """
        await self._page.type(selector, text, **kwargs)

    @overload
    async def select_option(
        self,
        selector: str,
        *,
        prompt: str | None = None,
        value: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str: ...

    @overload
    async def select_option(
        self,
        *,
        prompt: str,
        value: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str: ...

    async def select_option(
        self,
        selector: str | None = None,
        *,
        prompt: str | None = None,
        value: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        value = value or ""
        if ai == "fallback":
            error_to_raise = None
            if selector:
                try:
                    locator = self._page.locator(selector)
                    await locator.select_option(value, timeout=timeout)
                    return value
                except Exception as e:
                    error_to_raise = e
            if prompt:
                return await self._ai.ai_select_option(
                    selector=selector or "",
                    value=value,
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )
            if error_to_raise:
                raise error_to_raise
            else:
                return value
        elif ai == "proactive" and prompt:
            return await self._ai.ai_select_option(
                selector=selector or "",
                value=value,
                intention=prompt,
                data=data,
                timeout=timeout,
            )
        if selector:
            locator = self._page.locator(selector)
            await locator.select_option(value, timeout=timeout)
        return value

    async def extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        return await self._ai.ai_extract(prompt, schema, error_code_mapping, intention, data)

    async def reload(self, **kwargs: Any) -> None:
        """Reload the current page.

        Args:
            **kwargs: Additional options like timeout, wait_until, etc.
        """
        await self._page.reload(**kwargs)

    async def screenshot(self, **kwargs: Any) -> bytes:
        """Take a screenshot of the page.

        Args:
            **kwargs: Additional options like path, full_page, clip, type, quality, etc.

        Returns:
            bytes: The screenshot as bytes (unless path is specified, then saves to file).
        """
        return await self._page.screenshot(**kwargs)

    async def _input_text(
        self,
        selector: str,
        value: str,
        ai: str | None = "fallback",
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Input text into an element identified by ``selector``.

        When ``intention`` and ``data`` are provided a new input text action is
        generated via the `script-generation-input-text-generation` prompt.  The model returns a
        fresh text based on the current DOM and the updated data for this run.
        The browser then inputs the text using this newly generated text.

        If the prompt generation or parsing fails for any reason we fall back to
        inputting the originally supplied ``value``.
        """

        # format the text with the actual value of the parameter if it's a secret when running a workflow
        if ai == "fallback":
            error_to_raise = None
            try:
                locator = self._page.locator(selector)
                await handler_utils.input_sequentially(locator, value, timeout=timeout)
                return value
            except Exception as e:
                error_to_raise = e

            if intention:
                return await self._ai.ai_input_text(
                    selector=selector,
                    value=value,
                    intention=intention,
                    data=data,
                    totp_identifier=totp_identifier,
                    totp_url=totp_url,
                    timeout=timeout,
                )
            if error_to_raise:
                raise error_to_raise
            else:
                return value
        elif ai == "proactive" and intention:
            return await self._ai.ai_input_text(
                selector=selector,
                value=value,
                intention=intention,
                data=data,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                timeout=timeout,
            )
        locator = self._page.locator(selector)
        await handler_utils.input_sequentially(locator, value, timeout=timeout)
        return value
